"""Similarity search over `knowledge_chunks`, used by the orchestrator to
build RAG context before each LLM call (SPEC.md section 6.4).
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from sai.config import Settings, get_settings
from sai.db.models import KnowledgeChunk
from sai.db.session import session_scope
from sai.rag.ingest import get_embedding

logger = logging.getLogger(__name__)


async def similarity_search(
    query: str,
    top_k: int = 8,
    source_types: list[str] | None = None,
    settings: Settings | None = None,
) -> list[dict]:
    """Embed `query` and return the top_k most similar knowledge_chunks,
    optionally filtered by `source_types` (e.g. ["runbook", "incident_history"]).
    """
    settings = settings or get_settings()
    try:
        query_embedding = await get_embedding(query, settings)
    except Exception:
        logger.exception("embedding query failed; returning no RAG context")
        return []

    async with session_scope() as session:
        stmt = select(KnowledgeChunk).order_by(KnowledgeChunk.embedding.cosine_distance(query_embedding)).limit(top_k)
        if source_types:
            stmt = stmt.where(KnowledgeChunk.source_type.in_(source_types))
        result = await session.execute(stmt)
        chunks = result.scalars().all()

    return [
        {"id": str(c.id), "source_type": c.source_type, "source_ref": str(c.source_ref) if c.source_ref else None, "content": c.content}
        for c in chunks
    ]


async def retrieve_context(query: str, top_k: int = 8) -> str:
    """Formats the top matches as a plain-text context block suitable for
    inclusion in the orchestrator's system prompt (see
    sai/llm/orchestrator.py::_build_system_blocks).
    """
    matches = await similarity_search(query, top_k=top_k)
    if not matches:
        return ""

    lines = []
    for m in matches:
        lines.append(f"### [{m['source_type']}] chunk {m['id']}\n{m['content']}\n")
    return "\n".join(lines)
