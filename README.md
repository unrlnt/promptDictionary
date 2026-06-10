# promptdict

Learns prompting patterns from your own AI chat history, so when you start a
similar task you get a checklist of what you tend to forget and a template.
Teams pool the derived knowledge without exposing each other's raw chats.

## Architecture (production from day one)

```
import (raw, private)  -->  Store (Postgres + pgvector, RLS)
                              |
                              |  the ONLY egress boundary:
                              v
                       SanitizingGateway  -->  cloud LLM / embeddings
                       (sanitize-before-send, no bypass)
```

- Raw conversation text is private to its owner, enforced by row-level security.
- It only ever leaves that private store **sanitized**, through one gateway.
- Teams share **derived** artifacts (checklists, templates) — never raw text.

## Modules

| File | Role |
|------|------|
| `models.py` | Normalized schema — the stable contract every stage reads |
| `adapters.py` | Per-source parsers (ChatGPT tree, Claude flat) + registry |
| `store.py` | Storage interface; SQLite for local dev, Postgres/Supabase in prod |
| `sanitize.py` | Recognizers (Presidio NER + pattern + phone) and the span anonymizer |
| `cloud.py` | The single egress gateway: sanitizes before any provider call |
| `pipeline.py` | Wires ingest; attach point for clustering / extraction |
| `cli.py` | `ingest` / `list` for local development |
| `supabase/0001_init.sql` | Accounts, teams, conversations, refinements + RLS + pgvector |
| `RUNBOOK.md` | One-time deployment steps |

## Status

Built and tested here: ingestion + normalization, the sanitization layer, and
egress enforcement (proven that raw PII — names, email, IBAN, phone — cannot
reach a provider). The Supabase schema/RLS is a reviewed, deployable migration;
it goes live when you run it on a project. NER name-detection uses Presidio in
production (the build sandbox can't download the model, so names are exercised
via a deny-list in tests).

## What's next

Step 2 attaches to `pipeline.py`: embed + cluster conversations by task (through
`gateway.embed`), then extract refinement signals from follow-up turns (through
`gateway.extract`). Step 3: graduation detection, team checklists, templates.
