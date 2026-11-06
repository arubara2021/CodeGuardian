"""Local text embedding generation using fastembed (ONNX Runtime).

This module wraps the ``fastembed`` library to convert skill text into
384-dimensional float arrays. It uses the ``BAAI/bge-small-en-v1.5``
model, which is a high-performance, 384-dimension model optimized for
ONNX Runtime.

Unlike the standard ``sentence-transformers`` library which depends on
PyTorch (a 500MB+ dependency with CUDA binaries), ``fastembed`` uses
ONNX Runtime. It requires no GPU, installs in seconds, and uses minimal
RAM while providing the same embedding quality.

Because embedding generation is CPU-bound, it runs in a thread pool
via ``asyncio.to_thread`` to prevent blocking the main event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from codeguardian.core.errors import CodeGuardianError, ErrorCode

__all__ = ["EmbeddingProvider"]


class EmbeddingProvider:
    """Generates 384-dimensional embeddings using a local ONNX model.

    The model is lazily loaded on the first call to ``embed_text`` or
    ``embed_batch``. This prevents the model from consuming memory
    during the initial import of CodeGuardian if the skills module is
    never actually used in a given run.

    Args:
        model_name: The FastEmbed model identifier. Defaults to
            ``BAAI/bge-small-en-v1.5`` which outputs 384 dimensions.
    """

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        self._model_name = model_name
        self._model: Any | None = None
        self._lock = asyncio.Lock()

    def _get_model(self) -> Any:
        """Load and return the fastembed model.

        Raises:
            CodeGuardianError: With code SYS_NOT_IMPLEMENTED if the
                ``fastembed`` package is not installed.
            CodeGuardianError: With code SYS_INTERNAL if the model
                fails to load.
        """
        if self._model is not None:
            return self._model

        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_NOT_IMPLEMENTED,
                "fastembed is required for skill embeddings. Run: pip install fastembed",
                details={"model": self._model_name},
            ) from exc

        try:
            # FastEmbed downloads the ONNX model weights (~90MB) on first load
            self._model = TextEmbedding(model_name=self._model_name)
        except Exception as exc:
            raise CodeGuardianError(
                ErrorCode.SYS_INTERNAL,
                f"Failed to load embedding model {self._model_name!r}: {exc}",
                details={"model": self._model_name},
            ) from exc

        return self._model

    async def embed_text(self, text: str) -> list[float]:
        """Generate a 384-dimensional embedding for a single string.

        Args:
            text: The text to embed.

        Returns:
            A list of 384 floats.

        Raises:
            CodeGuardianError: With code STORAGE_SCHEMA_INVALID if the
                text is empty.
        """
        if not text.strip():
            raise CodeGuardianError(
                ErrorCode.STORAGE_SCHEMA_INVALID,
                "Cannot embed empty text.",
            )

        async with self._lock:
            model = await asyncio.to_thread(self._get_model)

        # FastEmbed returns a generator. We grab the first (and only) item.
        def _encode() -> list[float]:
            embeddings = list(model.embed([text]))
            if not embeddings:
                raise RuntimeError("fastembed returned no embeddings")
            return embeddings[0].tolist()

        return await asyncio.to_thread(_encode)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple strings in one pass.

        This is significantly faster than calling ``embed_text`` in a
        loop because it batches the matrix multiplication.

        Args:
            texts: A list of strings to embed.

        Returns:
            A list of 384-dimensional float arrays.
        """
        if not texts:
            return []

        async with self._lock:
            model = await asyncio.to_thread(self._get_model)

        def _encode_batch() -> list[list[float]]:
            embeddings = list(model.embed(texts))
            return [v.tolist() for v in embeddings]

        return await asyncio.to_thread(_encode_batch)
