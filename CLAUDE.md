# CLAUDE.md — promptdict

Project context for Claude Code. Read this before making any changes.

## What this is
promptdict learns prompting patterns from a user's own AI chat history (exported
from ChatGPT / Claude). It clusters past conversations by task, mines the follow-up
turns to find what users repeatedly forget, and offers a checklist + template when
they start a similar task. Teams pool the *derived* knowledge without ever exposing
each other's raw chats.

## The one rule that must never break
**All text that leaves the device goes through `SanitizingGateway`
(`promptdict/cloud.py`).** Never call an LLM or embedding provider directly. The
gateway sanitizes (PII redaction) before every outbound call and exposes only
`extract()` and `embed()`. If you need a new kind of cloud call, add a method to the
gateway that sanitizes first — never add a path that bypasses it.

## Architecture
```
import (raw, private)  ->  Store (Postgres + pgvector, RLS)
                              |
                              |  the ONLY egress boundary:
                              v
                       SanitizingGateway  ->  cloud LLM / embeddings
```
- Raw conversation text is private to its owner, enforced by row-level security.
- It leaves the private store only **sanitized**, through the one gateway.
- Teams share derived artifacts (checklists, templates) — never raw text.
- Sanitization is an **egress** step, never an ingest step. Ingest stores raw.

## The four seams (keep stable; extension must be additive)
1. **Normalized schema** (`models.py`) — the contract every stage reads. Never read
   a raw export downstream; read `Conversation`/`Message`.
2. **Source adapters** (`adapters.py`) — a new tool = one new adapter + register it.
3. **Store interface** (`store.py`) — SQLite local, Postgres/Supabase prod.
   `owner_id` / `source` / `external_id` exist from row one so accounts are additive.
4. **Egress gateway** (`cloud.py`) — the single sanitized chokepoint.

## Module map
- `models.py` — `Conversation` / `Message`; the stable contract
- `adapters.py` — ChatGPT (tree walk) + Claude (flat) parsers + registry
- `store.py` — `Store` ABC + `SQLiteStore` (`PostgresStore`: to build)
- `sanitize.py` — recognizers (Presidio NER, pattern, phone) + `Sanitizer`
- `cloud.py` — `SanitizingGateway` + provider interfaces + `MockProvider`
- `pipeline.py` — ingest; attach point for clustering / extraction
- `cli.py` — `ingest` / `list` for local dev
- `supabase/0001_init.sql` — accounts/teams/conversations/refinements + RLS + pgvector

## Stack
Python 3.11+ (stdlib-only core; cloud deps optional). Supabase (Postgres + pgvector
+ auth + RLS), EU region.

**Supabase keys:** the project uses the **new publishable/secret API keys**. Server/
worker code uses the **secret key** (`SUPABASE_SECRET_KEY`, `sb_secret_…`, browser-
blocked); the legacy `SUPABASE_SERVICE_ROLE_KEY` is kept only as a transition
fallback. Resolve via `config.Settings.supabase_secret` (prefers the new key, falls
back to legacy) — call sites never read the env vars directly. JWT verification has
been migrated to **asymmetric signing keys** (standby key in place, not yet rotated).

Cloud egress is **Mistral for everything, EU-native**:
- Embeddings: **Mistral Embed** (`mistral-embed`), `vector(1024)`, EU endpoint + ZDR on.
- Extraction LLM: **Mistral Small**, EU endpoint + ZDR on.
Both sit behind the provider interfaces in `cloud.py`. User BYOK (paste own API key)
is a future *optional* path, not the MVP default — never the "log in with a
subscription" pattern, which isn't a real capability.
NER: `presidio-analyzer` + spaCy (`en_core_web_lg`, `nl_core_news_lg`) + `phonenumbers`.

## Conventions
- Type hints everywhere; dataclasses for data; ABCs for swappable pieces.
- No secrets in code or git. Config via env vars / `.env` (gitignored).
- Every cloud-touching feature gets a test using `MockProvider` that proves no raw
  PII can egress.
- Keep `SQLiteStore` working for local dev even after Postgres lands.
- Use only synthetic data in tests and examples — never real chat exports.

## Status
- Built + tested: ingestion / normalization, sanitization, egress enforcement.
- Reviewed / deployable: Supabase schema + RLS.
- To build: `PostgresStore`, real providers, clustering + refinement extraction
  (step 2), app / auth / sharing (step 3).

## Commands
- Ingest (local): `python -m promptdict.cli ingest <export.json>`
- List: `python -m promptdict.cli list`
- Tests: `pytest`

## Don't
- Don't call providers outside the gateway.
- Don't sanitize at ingest — raw is stored privately; sanitize at egress.
- Don't let teammates read raw conversations — only derived `team_checklist_items`.
- Don't commit `.env`, `*.db`, or downloaded model files.
