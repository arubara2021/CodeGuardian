"""Optional cross-encoder reranker for retrieval candidates.

A cross-encoder reads the query and the document together and produces
a relevance score. Unlike the bi-encoder used for the initial
embedding, the cross-encoder attends to the interaction between query
and document, which measurably improves ordering on the top of the
list. The measured accuracy drop when the skill body is removed from
the reranker input is 29 to 44 percentage points, which is why the
reranker reads the body and not just the description.

The reranker is optional and flag-gated. When disabled or when the
model cannot be loaded, ``rerank`` returns its input unchanged. When
the model call exceeds the timeout, the call is cancelled and the
input is returned unchanged. A failure never propagates; the caller
always receives a usable ordering.

The model is loaded lazily on the first call that actually needs it.
Loading the ONNX weights costs a few hundred milliseconds and roughly
one hundred megabytes of resident memory, which is enough cost to
avoid paying on a process that never turns reranking on.

The timeout is enforced by running the synchronous rerank call in a
worker thread and awaiting the result with ``asyncio.wait_for``.
The thread cannot be killed cleanly on timeout, but it is bounded by
the model's own internal step count, so the abandoned work completes
shortly after and does not accumulate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .types import RetrievalItem

__all__ = ["Reranker"]

logger = logging.getLogger(__name__)


# The default cross-encoder model. It is a MiniLM-class model in the
# MS MARCO reranking family, distributed by FastEmbed for ONNX
# inference. It runs on a laptop CPU without a GPU in 50 to 100
# milliseconds per query on batches of twenty candidates.
_DEFAULT_MODEL: str = "Xenova/ms-marco-MiniLM-L-6-v2"


class Reranker:
    """A wrapper around a FastEmbed cross-encoder.

    The model loads lazily on the first ``rerank`` call. The class is
    safe to construct with a name that will fail to load; the failure
    surfaces on the first call and is degraded to passthrough rather
    than raised.

    Args:
        model_name: The FastEmbed model identifier.
        timeout_ms: The wall-clock budget per call, in milliseconds.
            When the call exceeds the budget, the input is returned
            unchanged.
    """

    def __init__(
        self,
        model_name: str = _DEFAULT_MODEL,
        timeout_ms: int = 120,
    ) -> None:
        self._model_name = model_name
        self._timeout_ms = timeout_ms
        self._model: Any | None = None
        self._load_failed: bool = False
        self._lock = asyncio.Lock()

    def _get_model(self) -> Any:
        """Return the loaded model, or ``None`` if loading failed."""
        if self._model is not None:
            return self._model
        if self._load_failed:
            return None
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError:
            logger.info("fastembed reranker module unavailable; reranker disabled")
            self._load_failed = True
            return None
        try:
            self._model = TextCrossEncoder(model_name=self._model_name)
        except Exception as exc:
            logger.warning(
                "Failed to load reranker model %s: %s",
                self._model_name,
                exc,
            )
            self._load_failed = True
            return None
        return self._model

    def _score(
        self,
        query: str,
        documents: list[str],
    ) -> list[float]:
        """Synchronous scoring, executed in a worker thread."""
        model = self._get_model()
        if model is None:
            return []
        try:
            return list(model.rerank(query, documents))
        except Exception as exc:
            logger.warning("Reranker scoring failed: %s", exc)
            return []

    async def rerank(
        self,
        query: str,
        items: tuple[RetrievalItem, ...],
    ) -> tuple[RetrievalItem, ...]:
        """Reorder the items by cross-encoder relevance to the query.

        Args:
            query: The user query or directive.
            items: The items to rerank. Only items whose ``body`` is
                populated participate; items without a body retain
                their original position relative to each other and are
                placed after the reranked items.

        Returns:
            The reordered items. On any failure (model unavailable,
            timeout, empty input), the input is returned unchanged.
        """
        if not items:
            return items

        with_body = [item for item in items if item.body]
        without_body = [item for item in items if not item.body]

        if len(with_body) < 2:
            return items

        documents = [item.body or "" for item in with_body]

        try:
            async with self._lock:
                scores = await asyncio.wait_for(
                    asyncio.to_thread(self._score, query, documents),
                    timeout=self._timeout_ms / 1000.0,
                )
        except TimeoutError:
            logger.info(
                "Reranker timeout after %d ms; using passthrough",
                self._timeout_ms,
            )
            return items
        except Exception as exc:
            logger.warning("Reranker failed: %s; using passthrough", exc)
            return items

        if len(scores) != len(with_body):
            logger.warning(
                "Reranker returned %d scores for %d documents; using passthrough",
                len(scores),
                len(with_body),
            )
            return items

        paired = list(zip(with_body, scores, strict=False))
        paired.sort(key=lambda pair: (-pair[1], pair[0].name))

        reordered = [
            RetrievalItem(
                name=item.name,
                score=float(score),
                reason="reranked",
                body=item.body,
                token_count=item.token_count,
                is_negative=item.is_negative,
            )
            for item, score in paired
        ]
        return tuple(reordered) + tuple(without_body)
