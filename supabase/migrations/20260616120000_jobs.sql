-- promptdict — migration 0004: background job queue
--
-- Additive only. A Postgres-backed queue for processing uploads off the request
-- path (no Redis/broker). The worker claims rows with FOR UPDATE SKIP LOCKED.
-- Owner-only RLS mirrors the other private tables; the worker writes via a direct
-- service connection that bypasses RLS, but the policy governs the API/client read
-- path (the /jobs poll endpoint).

create table if not exists public.jobs (
    id          uuid primary key default gen_random_uuid(),
    owner_id    uuid not null references auth.users (id) on delete cascade,
    status      text not null default 'pending',   -- pending | running | done | error
    stage       text,                              -- ingest|embed|cluster|extract|label|checklist
    total       int default 0,
    processed   int default 0,
    error       text,                              -- SAFE message only (no raw data/PII)
    created_at  timestamptz not null default now(),
    started_at  timestamptz,
    finished_at timestamptz
);

-- Claiming index: find the oldest pending job fast.
create index if not exists jobs_claim_idx on public.jobs (status, created_at);

alter table public.jobs enable row level security;

create policy jobs_owner on public.jobs
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());
