# docs/ — Runbooks & Scripts for RAG Ingestion

Drop your existing internal documentation here: runbooks, standard
operating procedures, incident postmortems, and any scripts that describe
routines that already work in your environment.

Supported file types: `.md`, `.txt`, `.py`, `.sh`, `.sql`.

Once you've added files, run the ingestion pipeline from the project root:

```bash
python -m sai.rag.ingest --docs-dir docs/
```

This chunks each file (~1000 characters with overlap), generates
embeddings via Voyage AI (configure `EMBEDDINGS_API_KEY` in `.env` first),
and stores them in the `knowledge_chunks` table for retrieval during chat
and during the scheduler's automated diagnosis (SPEC.md section 6.4).

Re-run this command any time you add or update files — it does not
currently de-duplicate previously ingested content from the same file, so
for a clean re-ingest of a changed file, remove its old chunks first (see
`sai/rag/ingest.py` for the storage schema) or truncate `knowledge_chunks`
where `source_type='runbook'` and re-run.
