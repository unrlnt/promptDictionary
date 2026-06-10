-- promptdict — Supabase / Postgres schema (migration 0001)
--
-- Security model in one sentence: raw conversation text is private to its owner
-- via RLS; teams share only DERIVED, non-sensitive artifacts (checklist items,
-- templates). Linking accounts into a team never grants access to a teammate's
-- raw chats.
--
-- Deploy: paste into the Supabase SQL editor (or run as a migration). Supabase
-- already provides auth.users and the auth.uid() of the current request.

create extension if not exists vector;      -- pgvector, for clustering embeddings

-- ---------------------------------------------------------------------------
-- Profiles (1:1 with auth.users). `default_region` feeds the phone sanitizer.
-- ---------------------------------------------------------------------------
create table public.profiles (
    id             uuid primary key references auth.users (id) on delete cascade,
    display_name   text,
    default_region text default 'NL',                 -- locale hint for sanitization
    created_at     timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Teams and membership.
-- ---------------------------------------------------------------------------
create table public.teams (
    id         uuid primary key default gen_random_uuid(),
    name       text not null,
    owner_id   uuid not null references auth.users (id) on delete cascade,
    created_at timestamptz not null default now()
);

create table public.team_members (
    team_id   uuid not null references public.teams (id) on delete cascade,
    user_id   uuid not null references auth.users (id) on delete cascade,
    role      text not null default 'member',          -- 'owner' | 'admin' | 'member'
    joined_at timestamptz not null default now(),
    primary key (team_id, user_id)
);

-- SECURITY DEFINER helper avoids recursive RLS when policies check membership.
create or replace function public.is_team_member(p_team uuid)
returns boolean language sql security definer stable
set search_path = public as $$
    select exists (
        select 1 from public.team_members m
        where m.team_id = p_team and m.user_id = auth.uid()
    );
$$;

-- ---------------------------------------------------------------------------
-- Conversations + messages — RAW text, private to the owner.
-- owner_id is denormalised onto messages so the RLS policy is a simple, fast
-- equality check rather than a join.
-- ---------------------------------------------------------------------------
create table public.conversations (
    id           uuid primary key default gen_random_uuid(),
    owner_id     uuid not null references auth.users (id) on delete cascade,
    source       text not null,                         -- 'chatgpt' | 'claude' | ...
    external_id  text not null,
    title        text,
    model        text,
    project      text,
    created_at   timestamptz,
    updated_at   timestamptz,
    content_hash text not null,                          -- skip re-analysis when unchanged
    cluster_id   uuid,                                   -- set by step 2 (FK added below)
    embedding    vector(1024),                           -- set dimension to match your provider
    unique (owner_id, source, external_id)
);

create table public.messages (
    id              uuid primary key default gen_random_uuid(),
    conversation_id uuid not null references public.conversations (id) on delete cascade,
    owner_id        uuid not null references auth.users (id) on delete cascade,
    role            text not null,                       -- 'user' | 'assistant' | ...
    text            text not null,                       -- RAW; private
    idx             int  not null,
    created_at      timestamptz
);
create index on public.messages (conversation_id, idx);

-- ---------------------------------------------------------------------------
-- Per-user task clusters and the refinement signals mined from follow-ups.
-- Refinements are STRUCTURAL (kind + position), not raw text, so they are safe
-- to aggregate and share.
-- ---------------------------------------------------------------------------
create table public.clusters (
    id         uuid primary key default gen_random_uuid(),
    owner_id   uuid not null references auth.users (id) on delete cascade,
    label      text,                                     -- e.g. "prepare a presentation"
    centroid   vector(1024),
    created_at timestamptz not null default now()
);
alter table public.conversations
    add constraint conversations_cluster_fk
    foreign key (cluster_id) references public.clusters (id) on delete set null;

create table public.refinements (
    id               uuid primary key default gen_random_uuid(),
    owner_id         uuid not null references auth.users (id) on delete cascade,
    conversation_id  uuid not null references public.conversations (id) on delete cascade,
    cluster_id       uuid references public.clusters (id) on delete set null,
    kind             text not null,                      -- 'format'|'length'|'audience'|'tone'|'citation'|...
    turn_index       int  not null,                      -- which follow-up introduced it
    in_first_prompt  boolean not null default false,     -- graduation signal
    created_at       timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Team-shared, non-sensitive derived knowledge.
-- ---------------------------------------------------------------------------
create table public.team_checklist_items (
    id          uuid primary key default gen_random_uuid(),
    team_id     uuid not null references public.teams (id) on delete cascade,
    cluster_label text not null,
    item        text not null,                           -- "Specify the target audience"
    kind        text not null,
    weight      real not null default 1.0,               -- how often / how many members
    updated_at  timestamptz not null default now()
);

-- ===========================================================================
-- Row-level security
-- ===========================================================================
alter table public.profiles             enable row level security;
alter table public.teams                 enable row level security;
alter table public.team_members          enable row level security;
alter table public.conversations         enable row level security;
alter table public.messages              enable row level security;
alter table public.clusters              enable row level security;
alter table public.refinements           enable row level security;
alter table public.team_checklist_items  enable row level security;

-- Profiles: each user manages only their own.
create policy profiles_self on public.profiles
    for all using (id = auth.uid()) with check (id = auth.uid());

-- Owner-only data. These policies are what keep raw chats private even from teammates.
create policy conv_owner on public.conversations
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy msg_owner on public.messages
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy cluster_owner on public.clusters
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());
create policy refine_owner on public.refinements
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- Teams: members read; only the owner mutates the team row.
create policy team_read on public.teams
    for select using (owner_id = auth.uid() or public.is_team_member(id));
create policy team_write on public.teams
    for all using (owner_id = auth.uid()) with check (owner_id = auth.uid());

-- Membership: a member sees their team's roster; the team owner manages it.
create policy member_read on public.team_members
    for select using (public.is_team_member(team_id));
create policy member_manage on public.team_members
    for all using (exists (select 1 from public.teams t where t.id = team_id and t.owner_id = auth.uid()))
    with check (exists (select 1 from public.teams t where t.id = team_id and t.owner_id = auth.uid()));

-- Shared checklists: any team member may read; writes happen via the service role
-- (the background worker), which bypasses RLS, so no member-write policy is granted.
create policy checklist_read on public.team_checklist_items
    for select using (public.is_team_member(team_id));
