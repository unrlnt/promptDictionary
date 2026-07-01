import { redirect } from "next/navigation";
import { type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";
import { safeNext } from "@/lib/safe-next";

// OAuth (PKCE) callback. Providers (Google / Azure) redirect the browser here
// with a `code`; we exchange it for a session using the server client (which
// reads/writes the auth cookies) and then send the user into the app.
//
// Separate from /auth/confirm, which handles the email magic-link OTP flow.
export async function GET(request: NextRequest) {
  const { searchParams } = new URL(request.url);
  const code = searchParams.get("code");
  const next = safeNext(searchParams.get("next"));

  if (code) {
    const supabase = await createClient();
    const { error } = await supabase.auth.exchangeCodeForSession(code);
    if (!error) {
      redirect(next);
    }
  }

  redirect(
    `/login?error=${encodeURIComponent("Could not sign in. Please try again.")}`,
  );
}
