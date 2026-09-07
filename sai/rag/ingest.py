"""RAG ingestion pipeline (SPEC.md section 6.4 / 14).

Three sources feed `knowledge_chunks`:
  1. `inventory_items` — re-embedded whenever new/changed (called from
     sai/inventory/sync.py after each sync).
  2. Runbooks/scripts dropped by the team into `docs/` (.md, .txt, .py,
     .sh, .sql) — ingested by running this module as a script.
  3. Incident history — fed automatically whenever an `Action` reaches
     `status='succeeded'` (called from sai/approval/engine.py's execution
     path, or from a small hook wired in by the caller).

Embeddings provider — Voyage AI
--------------------------------
Anthropic does not currently offer a first-party embeddings endpoint,
so `get_embedding()` calls Voyage AI (Anthropic's own recommended
embeddings partner) over its documented HTTP API via `httpx`, configured
through `EMBEDDINGS_API_BASE` / `EMBEDDINGS_API_KEY` / `EMBEDDINGS_MODEL`.
The function signature is deliberately provider-agnostic — swapping to any
other OpenAI-compatible embeddings endpoint only requires changing these
env vars and, if the wire format differs, this one function.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from pathlib import Path

import httpx
from sqlalchemy import delete

from sai.config import Settings, get_settings
from sai.db.models import InventoryItem, KnowledgeChunk
from sai.db.session import session_scope

logger = logging.getLogger(__name__)

DOC_EXTENSIONS = {".md", ".txt", ".py", ".sh", ".sql"}
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150


def recursive_character_split(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Simple recursive-ish character splitter: tries to break on paragraph,
    then line, then hard-cuts, always keeping `overlap` chars of context
    carried into the next chunk."""
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    separators = ["\n\n", "\n", ". ", " "]
    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_size, text_len)
        if end < text_len:
            # try to find a natural break point within the window, searching backwards
            window = text[start:end]
            split_at = -1
            for sep in separators:
                idx = window.rfind(sep)
                if idx > chunk_size * 0.5:  # don't break too early in the window
                    split_at = idx + len(sep)
                    break
            if split_at != -1:
                end = start + split_at

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start = max(end - overlap, start + 1)

    return chunks


async def get_embedding(text: str, settings: Settings | None = None) -> list[float]:
    """Call Voyage AI's embeddings endpoint. Returns a vector of
    `settings.embeddings_dimensions` floats.

    Raises `httpx.HTTPStatusError` on API failure; callers should handle
    and log rather than silently continuing with a zero vector.
    """
    settings = settings or get_settings()
    if not settings.embeddings_api_key:
        raise RuntimeError(
            "EMBEDDINGS_API_KEY is not configured — set it to a Voyage AI API key "
            "(see .env.example) before running RAG ingestion."
        )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.embeddings_api_base}/embeddings",
            headers={"Authorization": f"Bearer {settings.embeddings_api_key}", "Content-Type": "application/json"},
            json={"input": [text], "model": settings.embeddings_model, "input_type": "document"},
        )
        resp.raise_for_status()
        body = resp.json()
        return body["data"][0]["embedding"]


async def _store_chunk(session, source_type: str, source_ref: uuid.UUID | None, content: str, settings: Settings) -> None:
    embedding = await get_embedding(content, settings)
    session.add(KnowledgeChunk(source_type=source_type, source_ref=source_ref, content=content, embedding=embedding))


async def ingest_docs_dir(docs_dir: str | Path, settings: Settings | None = None) -> int:
    """Ingest every runbook/script under `docs_dir` into `knowledge_chunks`
    with `source_type='runbook'`. Returns the number of chunks stored."""
    settings = settings or get_settings()
    docs_path = Path(docs_dir)
    if not docs_path.exists():
        logger.warning("docs dir does not exist: %s", docs_path)
        return 0

    stored = 0
    async with session_scope() as session:
        for file_path in sorted(docs_path.rglob("*")):
            if not file_path.is_file() or file_path.suffix.lower() not in DOC_EXTENSIONS:
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                logger.exception("failed reading %s", file_path)
                continue

            chunks = recursive_character_split(text)
            for chunk in chunks:
                annotated = f"[source file: {file_path.relative_to(docs_path)}]\n{chunk}"
                await _store_chunk(session, source_type="runbook", source_ref=None, content=annotated, settings=settings)
                stored += 1
            logger.info("ingested %s (%d chunks)", file_path, len(chunks))

    return stored


async def ingest_inventory_items(settings: Settings | None = None) -> int:
    """Re-embed all current `inventory_items` rows into `knowledge_chunks`
    with `source_type='inventory_item'`. Called after each inventory sync
    (sai/inventory/sync.py). Replaces prior inventory-derived chunks to
    avoid unbounded growth / stale duplicates."""
    settings = settings or get_settings()
    stored = 0
    async with session_scope() as session:
        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.source_type == "inventory_item"))

        result = await session.execute(InventoryItem.__table__.select())
        for row in result.mappings().all():
            content = (
                f"Inventory item: {row['name']} (source={row['source']}, type={row['resource_type']}, "
                f"external_id={row['external_id']})\nMetadata: {row['metadata']}\nTags: {row['tags']}"
            )
            await _store_chunk(session, source_type="inventory_item", source_ref=row["id"], content=content, settings=settings)
            stored += 1

    return stored


async def ingest_incident(action_id: uuid.UUID, description: str, settings: Settings | None = None) -> None:
    """Feed a resolved incident (an Action with status='succeeded') into
    the knowledge base as `source_type='incident_history'` (SPEC 6.4
    source 3). Called from the approval engine after successful execution."""
    settings = settings or get_settings()
    async with session_scope() as session:
        for chunk in recursive_character_split(description):
            await _store_chunk(session, source_type="incident_history", source_ref=action_id, content=chunk, settings=settings)


async def _main() -> None:
    parser = argparse.ArgumentParser(description="SAI RAG ingestion CLI")
    parser.add_argument("--docs-dir", default="docs", help="Directory of runbooks/scripts to ingest")
    parser.add_argument("--inventory", action="store_true", help="Also (re)ingest inventory_items")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    n = await ingest_docs_dir(args.docs_dir)
    logger.info("ingested %d chunks from %s", n, args.docs_dir)
    if args.inventory:
        n2 = await ingest_inventory_items()
        logger.info("ingested %d inventory chunks", n2)


if __name__ == "__main__":
    asyncio.run(_main())
