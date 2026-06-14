-- promptdict — migration 0003: aggregated checklists
--
-- Additive only. Derived, non-sensitive aggregates over a user's own refinement
-- rows (global + per-cluster), plus a global graduation trend per kind. Owner-only
-- RLS mirrors the other private tables; the service-role worker writes these rows.

create table if not exists public.checklists (
    id                 uuid primary key default gen_random_uuid(),
    owner_id           uuid not null references auth.users (id) on delete cascade,
    scope              text not null,                       -- 'global' | 'cluster'
    cluster_id         uuid references public.clusters (id) on delete cascade,  -- null when global
    kind               text not null,
    conversation_count int  not null,                       -- distinct conversations forgetting this kind
    total_count        int  not null,                       -- total forgotten rows for this kind in scope
    rank               int  not null,                       -- 1 = most-forgotten in this scope
    sample_notes       text[] not null default '{}',        -- a few representative generalized notes
    graduation         text,                                -- 'graduated'|'improving'|'persistent'|null (global only)
    updated_at         timestamptz not null default now()
);
create index if not exists checklists_owner_scope on public.checklists (owner_id, scope);

alter table public.checklists enable row level security;

-- Owner-only, mirroring conversations/refinements/clusters. Writes happen via the
-- service role (which bypasses RLS); clients only read their own rows.
create policy checklist_owner on public.checklists
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());
