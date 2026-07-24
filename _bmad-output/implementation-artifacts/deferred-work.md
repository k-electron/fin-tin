# Deferred Work

## Deferred from: code review of story-1.2 (2026-07-23)

- **Schema migrations** (medium) — `fintin/adapters/store/schema.py` uses `CREATE … IF NOT EXISTS`, which silently keeps a stale table/MV definition if its DDL later changes; only the `screening_mart` view (`CREATE OR REPLACE`) is refreshed. There is no migration/versioning story, so a DDL change (e.g. the F1 resolution-rank fix) won't apply to an already-created deployment without a manual drop/recreate. Deferred because the v1 schema is still stabilizing and this is a solo local tool; a create-only limitation note is documented in `schema.py` now. Revisit with a proper migration mechanism when the schema settles or a second environment appears.
