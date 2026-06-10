# Deployment runbook

What this repo gives you: production-shaped, deployable code. What you run once
on your side: the Supabase project, the provider keys, and the NER models. Live
auth flows and real cloud calls can't run from the build sandbox, so they're set
up here.

## 1. Supabase project
1. Create a Supabase project in an **EU region** (data residency for GDPR).
2. Apply the migration in `supabase/migrations/`. This creates the schema, enables
   `pgvector`, and turns on row-level security with the owner-only / team-shared
   policies. Apply it either by pasting the file into the SQL editor or by running
   `supabase db push`.
3. The migration ships at `vector(1024)` to match Mistral Embed (`mistral-embed`).
   Only change the dimension if you swap embedding providers.
4. Auth → enable email and/or OAuth sign-in.

## 2. Keys (server-side only — never ship the service role key to the browser)
- `SUPABASE_URL`, `SUPABASE_ANON_KEY` — used by the app/client.
- `SUPABASE_SERVICE_ROLE_KEY` — used ONLY by the background worker. It bypasses
  RLS, which is why writes to `team_checklist_items` are worker-only.
- `MISTRAL_API_KEY` — covers both extraction (Mistral Small) and embeddings
  (Mistral Embed). Use the **EU endpoint**.
- Configure the provider for **zero data retention / no training**. Sanitization
  lowers risk; the provider agreement is what makes egress defensible.

## 3. The worker (NER models)
The background worker needs the production NER recognizer:

```bash
pip install presidio-analyzer phonenumbers
python -m spacy download en_core_web_lg
python -m spacy download nl_core_news_lg   # Dutch; add languages as needed
```

`promptdict.sanitize.default_sanitizer()` auto-uses Presidio when the models are
present and falls back to pattern + phone recognizers otherwise.

## 4. Local development
SQLite backs the same `Store` interface, so you can build and test the pipeline
with no cloud at all:

```bash
python -m promptdict.cli ingest path/to/export.json
python -m promptdict.cli list
```

## GDPR notes
- EU region (above).
- `on delete cascade` throughout means deleting a user removes all their data —
  the basis for right-to-erasure.
- Raw text never leaves a user's private rows except sanitized; team sharing
  exchanges only derived checklist items.
