"""Persistence for the procedural skill graph.

The graph is a derived index. Every row in ``skill_graph_nodes`` and
``skill_graph_edges`` can be reconstructed by reading the Markdown
skill files and re-running the builder. Dropping both tables and
rebuilding from the source of truth restores the exact state, which is
what makes the graph safe to add to a running system: if anything goes
wrong, the graph is deleted and rebuilt.

The store connects to the same ``SQLiteStore`` as the rest of the
storage layer. It does not add methods to ``SkillStore`` because the
graph is a distinct concern and a future replacement should not require
changing the Markdown-backed store.

Failure behavior is deliberately narrow. A rebuild that encounters an
unparseable Markdown file logs a warning and skips the file rather than
failing the whole rebuild; one corrupt skill should not disable the
entire graph. A rebuild whose SQL writes fail raises ``SYS_INTERNAL``,
because a database error is not something the caller can ignore and
retrying may resolve a transient lock.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from codeguardian.core.errors import CodeGuardianError
from codeguardian.core.storage.skill_store import _parse_skill_markdown
from codeguardian.core.storage.sqlite_store import SQLiteStore

from .graph import build_edges
from .types import EdgeKind, SkillEdge, SkillNode

__all__ = [
    "count_edges",
    "count_nodes",
    "drop_and_rebuild",
    "get_all_edges",
    "get_all_nodes",
    "get_node",
    "neighbors",
    "upsert_edge",
    "upsert_node",
]

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def upsert_node(store: SQLiteStore, node: SkillNode) -> None:
    """Insert or replace a node.

    Args:
        store: An open ``SQLiteStore``.
        node: The node to persist.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if the write
            fails.
    """
    store.execute(
        """
        INSERT OR REPLACE INTO skill_graph_nodes
            (node_id, kind, name, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            node.node_id,
            node.kind,
            node.name,
            json.dumps(dict(node.metadata)),
            _now_iso(),
        ),
    )


def upsert_edge(store: SQLiteStore, edge: SkillEdge) -> None:
    """Insert or replace an edge.

    Args:
        store: An open ``SQLiteStore``.
        edge: The edge to persist.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if the write
            fails. Note that a foreign-key violation means one of the
            edge's endpoints is not present in ``skill_graph_nodes``.
    """
    store.execute(
        """
        INSERT OR REPLACE INTO skill_graph_edges
            (edge_id, src, dst, kind, weight, provenance, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            edge.edge_id,
            edge.src,
            edge.dst,
            edge.kind.value,
            edge.weight,
            edge.provenance or None,
            _now_iso(),
        ),
    )


def get_node(store: SQLiteStore, node_id: str) -> SkillNode | None:
    """Return one node by identifier, or ``None``."""
    rows = store.query(
        "SELECT node_id, kind, name, metadata_json FROM skill_graph_nodes WHERE node_id = ?",
        (node_id,),
    )
    if not rows:
        return None
    row = rows[0]
    metadata: dict = {}
    if row["metadata_json"]:
        try:
            metadata = json.loads(row["metadata_json"])
        except json.JSONDecodeError:
            metadata = {}
    return SkillNode(
        node_id=row["node_id"],
        kind=row["kind"],
        name=row["name"],
        metadata=metadata,
    )


def get_all_nodes(store: SQLiteStore) -> tuple[SkillNode, ...]:
    """Return every node in the graph."""
    rows = store.query(
        "SELECT node_id, kind, name, metadata_json FROM skill_graph_nodes ORDER BY node_id"
    )
    result: list[SkillNode] = []
    for row in rows:
        metadata: dict = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                metadata = {}
        result.append(
            SkillNode(
                node_id=row["node_id"],
                kind=row["kind"],
                name=row["name"],
                metadata=metadata,
            )
        )
    return tuple(result)


def get_all_edges(store: SQLiteStore) -> tuple[SkillEdge, ...]:
    """Return every edge in the graph."""
    rows = store.query(
        "SELECT edge_id, src, dst, kind, weight, provenance FROM skill_graph_edges ORDER BY edge_id"
    )
    result: list[SkillEdge] = []
    for row in rows:
        result.append(
            SkillEdge(
                edge_id=row["edge_id"],
                src=row["src"],
                dst=row["dst"],
                kind=EdgeKind(row["kind"]),
                weight=float(row["weight"]),
                provenance=row["provenance"] or "",
            )
        )
    return tuple(result)


def neighbors(
    store: SQLiteStore,
    node_id: str,
    *,
    kind: EdgeKind | None = None,
    direction: str = "out",
) -> tuple[str, ...]:
    """Return the neighbours of a node along edges of a given kind.

    Args:
        store: An open ``SQLiteStore``.
        node_id: The node to look up.
        kind: When given, restrict the result to edges of that kind.
            When ``None``, all kinds are returned.
        direction: ``"out"`` follows outgoing edges and returns the
            ``dst`` of each. ``"in"`` follows incoming edges and
            returns the ``src`` of each. ``"both"`` returns the union.

    Returns:
        The neighbouring node identifiers, in deterministic order.
    """
    if direction not in ("out", "in", "both"):
        return ()

    conditions: list[str] = []
    params: list[object] = []
    if kind is not None:
        conditions.append("kind = ?")
        params.append(kind.value)

    result: set[str] = set()

    if direction in ("out", "both"):
        where = " AND ".join(["src = ?", *conditions])
        rows = store.query(
            f"SELECT dst FROM skill_graph_edges WHERE {where}",
            (node_id, *params),
        )
        result.update(row["dst"] for row in rows)

    if direction in ("in", "both"):
        where = " AND ".join(["dst = ?", *conditions])
        rows = store.query(
            f"SELECT src FROM skill_graph_edges WHERE {where}",
            (node_id, *params),
        )
        result.update(row["src"] for row in rows)

    return tuple(sorted(result))


def count_nodes(store: SQLiteStore) -> int:
    """Return the number of nodes in the graph."""
    rows = store.query("SELECT COUNT(*) AS c FROM skill_graph_nodes")
    return int(rows[0]["c"])


def count_edges(store: SQLiteStore) -> int:
    """Return the number of edges in the graph."""
    rows = store.query("SELECT COUNT(*) AS c FROM skill_graph_edges")
    return int(rows[0]["c"])


def _collect_skill_files(skills_dir: Path) -> Iterable[Path]:
    """Yield every ``SKILL.md`` file under the given directory."""
    if not skills_dir.exists():
        return
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        candidate = entry / "SKILL.md"
        if candidate.exists():
            yield candidate


def drop_and_rebuild(
    store: SQLiteStore,
    skills_dir: Path,
    embeddings: Mapping[str, list[float]] | None = None,
    *,
    semantic_threshold: float = 0.85,
) -> tuple[int, int]:
    """Rebuild the graph from the Markdown skill files.

    The rebuild is atomic from the caller's perspective: either the
    tables are replaced with the new graph or the previous rows are
    left untouched, depending on where a failure occurs. Edges are
    deleted before nodes so that the foreign-key constraints are not
    violated during the transition.

    A ``SKILL.md`` file that cannot be parsed is skipped with a warning.
    One corrupt file does not fail the rebuild, and the rest of the
    graph is still usable.

    Args:
        store: An open ``SQLiteStore``.
        skills_dir: The directory containing one subdirectory per
            skill, each with a ``SKILL.md`` inside.
        embeddings: A mapping from skill name to embedding vector. A
            skill with no embedding still participates in authored
            edges but not in semantic edges.
        semantic_threshold: The cosine threshold above which two
            skills receive a semantic edge.

    Returns:
        A tuple of ``(node_count, edge_count)`` reflecting the state of
        the graph after the rebuild.

    Raises:
        CodeGuardianError: With code ``SYS_INTERNAL`` if a database
            operation fails. A rebuild whose writes fail leaves the
            tables partially populated; the caller should retry the
            rebuild, which is idempotent.
    """
    frontmatters: dict[str, dict] = {}

    for skill_file in _collect_skill_files(skills_dir):
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot read skill file %s: %s", skill_file, exc)
            continue
        try:
            frontmatter, _ = _parse_skill_markdown(text, skill_file)
        except CodeGuardianError as exc:
            logger.warning("Skipping unparseable skill file %s: %s", skill_file, exc.message)
            continue
        name = frontmatter.get("name")
        if not isinstance(name, str) or not name:
            logger.warning("Skill file %s has no name; skipping", skill_file)
            continue
        frontmatters[name] = frontmatter

    edges, warnings = build_edges(
        frontmatters,
        embeddings,
        semantic_threshold=semantic_threshold,
    )
    for warning in warnings:
        logger.warning("Graph build: %s", warning)

    skill_nodes = [
        SkillNode(node_id=name, kind="skill", name=name) for name in sorted(frontmatters.keys())
    ]
    capability_names: set[str] = set()
    for edge in edges:
        for endpoint in (edge.src, edge.dst):
            if endpoint.startswith("cap:"):
                capability_names.add(endpoint)
    capability_nodes = [
        SkillNode(node_id=cid, kind="capability", name=cid[len("cap:") :])
        for cid in sorted(capability_names)
    ]

    store.execute("DELETE FROM skill_graph_edges")
    store.execute("DELETE FROM skill_graph_nodes")

    for node in skill_nodes:
        upsert_node(store, node)
    for node in capability_nodes:
        upsert_node(store, node)

    seen: set[str] = set()
    for edge in edges:
        if edge.edge_id in seen:
            continue
        seen.add(edge.edge_id)
        if get_node(store, edge.src) is None or get_node(store, edge.dst) is None:
            logger.warning(
                "Skipping edge %s: one endpoint is not a known node",
                edge.edge_id,
            )
            continue
        upsert_edge(store, edge)

    return count_nodes(store), count_edges(store)
