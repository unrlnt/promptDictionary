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

Uploads are processed in the **background**: the `api` enqueues a job (Postgres
`jobs` table — apply migration `…_jobs.sql`), and the `worker` service claims it with
`FOR UPDATE SKIP LOCKED` and runs the full, uncapped pipeline through the gateway.
**No Redis/broker.** The api and worker share the upload spool via a named volume.

```bash
# Build images (api + worker share the same python image)
docker compose build

# Local dev: api + worker (default services). The override publishes api to
# 127.0.0.1:8000; the worker has no host port.
docker compose up
curl http://127.0.0.1:8000/health        # -> {"status":"ok"}

# Local dev: also run the web app in a container (optional web profile)
docker compose --profile web up
```

Building the web image needs the browser-safe `NEXT_PUBLIC_*` values available to
compose (export them or add to the root `.env`) — they are inlined at build time.
The web app is also deployable to **Vercel**; compose's web service is optional.

The dashboard upload flow: POST `/process` returns a `job_id` (202); the browser
polls `GET /jobs/{job_id}` (owner-scoped) showing stage + processed/total, then
fetches `/checklists` when the job is `done`. Temp uploads are deleted after the
worker finishes (success or error). Job errors stored in the DB are safe messages
only (no raw data/PII); full detail goes to the worker logs.

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

## 6. Signup allow-list (Before User Created hook)

Account creation is gated to a managed list of approved emails. A Supabase
**Before User Created** auth hook runs the SQL function
`public.restrict_signup_to_allowlist(jsonb)` on every signup — email/password
**and** OAuth (Google/Microsoft) — and rejects anyone whose email is not in
`public.allowed_emails`. Nothing is written to `auth.users` for a rejected signup.

**Why it exists:** it doubles as signup abuse control. If you ever remove the
allow-list, you MUST add app-level signup **rate limiting** in its place — they
are a pair. Don't drop one without the other.

### Apply the migration
Apply `supabase/migrations/20260628152644_signup_allowlist.sql` like the others —
paste it into the SQL editor or run `supabase db push`. It creates the
`allowed_emails` table, the `supabase_auth_admin` grants + RLS policy, and the
hook function. It is additive and safe to re-run (`if not exists`,
`create or replace`).

### Enable the hook (one-time, dashboard)
1. **Authentication → Hooks** (Auth Hooks).
2. Add a hook for the **Before User Created** event.
3. Choose **Postgres** as the hook type and point it at the function
   `public.restrict_signup_to_allowlist` (schema `public`).
4. Enable it and save. New signups now go through the allow-list immediately.

### Add / remove approved emails (SQL)
Emails are stored lowercased; the hook lowercases the incoming email before
comparing, so always insert lowercased.

```sql
-- approve an email
insert into public.allowed_emails (email, note)
    values ('person@example.com', 'why they were approved')
    on conflict (email) do nothing;

-- revoke access to sign up (does not delete an already-created account)
delete from public.allowed_emails where email = 'person@example.com';

-- see the current list
select email, note, created_at from public.allowed_emails order by created_at;
```

Run these as an operator (SQL editor / service role). The browser-facing roles
(`anon`, `authenticated`) have no access to the table by design.

### Known quirk
On rejection, Supabase may surface a generic **"Invalid payload"** (or similarly
vague) message to the client instead of the 403 message above. This is a known
Supabase behavior — the signup is still correctly **blocked**; only the
client-side wording is generic. Verify a rejection by confirming no new row
appears in `auth.users`, not by the client message text.

## GDPR notes
- EU region (above).
- `on delete cascade` throughout means deleting a user removes all their data —
  the basis for right-to-erasure.
- Raw text never leaves a user's private rows except sanitized; team sharing
  exchanges only derived checklist items.
