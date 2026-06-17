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

## 5. Docker (containerized Python services)

The engine + API ship as a shared Python base image (`docker/Dockerfile.python`,
Python 3.13-slim). The 8d worker will reuse this same image. **The spaCy NER models
(`en_core_web_lg`, `nl_core_news_lg`) are baked in at build time**, so the
in-container sanitizer runs full Presidio NER — not the pattern-only fallback — with
no runtime download. No secrets are baked in; all config comes from the root `.env`
at runtime (`DATABASE_URL`, `MISTRAL_API_KEY`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`
with `SUPABASE_SERVICE_ROLE_KEY` as fallback, and optional `WEB_ORIGIN`).

```bash
# Build images
docker compose build

# Local dev: api only. The override publishes it to 127.0.0.1:8000.
docker compose up api
curl http://127.0.0.1:8000/health        # -> {"status":"ok"}

# Local dev: api + web together (optional web profile)
docker compose --profile web up
```

Building the web image needs the browser-safe `NEXT_PUBLIC_*` values available to
compose (export them or add to the root `.env`) — they are inlined at build time.
The web app is also deployable to **Vercel**; compose's web service is optional.

### Port posture (production-safe by default)

The base `docker-compose.yml` is the **production** posture:

- **api** has no `ports:` entry — no host-facing port. It is reachable only on the
  internal docker network as `api:8000` (by the web service and a host nginx
  reverse-proxy).
- **web** publishes to **loopback only** (`127.0.0.1:3000:3000`). Never
  `3000:3000` / `0.0.0.0`: Docker inserts its own iptables rules that bypass the
  host firewall (ufw), which would expose the port publicly. A host nginx proxies
  to the loopback port.

`docker-compose.override.yml` is **local dev only** and is auto-applied by
`docker compose up`; it publishes the api to `127.0.0.1:8000` for direct testing.

```bash
# PRODUCTION — no override, api stays unpublished (nginx proxies internally):
docker compose -f docker-compose.yml up -d
```

Intended production topology: **Cloudflare → VPS nginx → web (127.0.0.1:3000) +
api (proxied over the internal docker network)**. The nginx/Cloudflare/TLS config
itself is out of scope here.

Tests still run on the host against the (host) Python env — Docker doesn't change
that: `pytest`.

## GDPR notes
- EU region (above).
- `on delete cascade` throughout means deleting a user removes all their data —
  the basis for right-to-erasure.
- Raw text never leaves a user's private rows except sanitized; team sharing
  exchanges only derived checklist items.
