-- promptdict — migration 0005: signup allow-list (Before User Created auth hook)
--
-- Additive only. Gates account creation to a managed list of approved emails.
-- A Supabase "Before User Created" auth hook calls the function below for every
-- signup (email/password AND OAuth); approved emails pass, everyone else is
-- rejected with a 403 before any auth.users row is written.
--
-- The hook runs as the `supabase_auth_admin` role, so that role (and only that
-- role) is granted read access to the table and execute on the function. The
-- function is intentionally NOT security definer (per Supabase guidance) — it
-- already runs as supabase_auth_admin.
--
-- Operational note: this allow-list is a substitute for app-level signup rate
-- limiting. If it is ever removed, rate limiting must be added in its place —
-- see the "Signup allow-list" section in RUNBOOK.md.

-- ---- managed table: approved emails (stored lowercased) -------------------
create table if not exists public.allowed_emails (
    email      text primary key,            -- always lowercased; see hook below
    note       text,                        -- optional free-text (who/why)
    created_at timestamptz not null default now()
);

-- ---- lock the table down -------------------------------------------------
-- The hook role needs to reach into the public schema and read this table.
grant usage on schema public to supabase_auth_admin;
grant select on public.allowed_emails to supabase_auth_admin;

-- No application role may touch the allow-list. It is managed via SQL by an
-- operator (service role / dashboard), never by the browser-facing roles.
revoke all on public.allowed_emails from anon, authenticated, public;

-- RLS on, with a single policy that lets the hook role read it even with RLS
-- enabled. No policy for any other role => no access for anyone else.
alter table public.allowed_emails enable row level security;

create policy allowed_emails_auth_admin_select
    on public.allowed_emails
    for select
    to supabase_auth_admin
    using (true);

-- ---- the Before User Created hook function -------------------------------
-- Receives the hook event; the user record about to be created is at
-- event->'user' and matches the auth.users shape, so the signup email is at
-- event->'user'->>'email' for both email/password and OAuth signups.
--
-- Returns '{}'::jsonb to allow, or an error object to reject. Errors from this
-- hook are non-retryable, so keep the body to a single indexed PK lookup to
-- stay well inside the 2-second hook budget.
create or replace function public.restrict_signup_to_allowlist(event jsonb)
returns jsonb
language plpgsql
as $$
declare
    signup_email text;
begin
    signup_email := lower(event->'user'->>'email');

    if signup_email is not null
       and exists (
           select 1 from public.allowed_emails
           where email = signup_email
       )
    then
        -- Approved: allow the signup to proceed.
        return '{}'::jsonb;
    end if;

    -- Not approved: reject with a clear, non-sensitive message.
    return jsonb_build_object(
        'error', jsonb_build_object(
            'http_code', 403,
            'message', 'This email is not approved for access.'
        )
    );
end;
$$;

-- The hook is invoked as supabase_auth_admin; grant it execute.
grant execute on function public.restrict_signup_to_allowlist(jsonb)
    to supabase_auth_admin;

-- Keep the function private to the hook role only.
revoke execute on function public.restrict_signup_to_allowlist(jsonb)
    from anon, authenticated, public;

-- ---- seeding the allow-list (examples; emails stored lowercased) ---------
-- Add approved emails via SQL. `on conflict do nothing` keeps re-runs safe.
--
-- insert into public.allowed_emails (email, note)
--     values ('founder@example.com', 'founding team')
--     on conflict (email) do nothing;
--
-- insert into public.allowed_emails (email, note)
--     values ('teammate@example.com', 'design — invited 2026-06')
--     on conflict (email) do nothing;
