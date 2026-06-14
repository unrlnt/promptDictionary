import { createBrowserClient } from "@supabase/ssr";

// Supabase client for Client Components (runs in the browser). Uses the
// publishable (anon-level) key only — never the secret key.
export function createClient() {
  return createBrowserClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
  );
}
