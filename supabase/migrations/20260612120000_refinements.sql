-- promptdict — migration 0002: refinement extraction support
--
-- Additive only. Does not modify existing columns, constraints, or RLS policies.
-- (The owner-only RLS on conversations/refinements from migration 0001 still applies
--  to these new columns automatically.)

-- Marks when a conversation's follow-up refinements were last extracted (NULL =
-- not yet extracted). Lets extraction be incremental and idempotent.
alter table public.conversations
    add column if not exists refinements_extracted_at timestamptz;

-- A short, generalized, non-sensitive description of the refinement
-- (e.g. "specify the target audience"). Nullable.
alter table public.refinements
    add column if not exists note text;
