"use client";

import { useState } from "react";

import { createClient } from "@/lib/supabase/client";

// OAuth sign-in buttons. signInWithOAuth must run in the BROWSER (it needs to
// navigate the window to the provider's consent screen), so this is a client
// component using the browser Supabase client. redirectTo points back at our
// PKCE callback route, which exchanges the returned `code` for a session.
export function OAuthButtons() {
  const [pending, setPending] = useState<"google" | "azure" | null>(null);

  async function signIn(provider: "google" | "azure") {
    setPending(provider);
    const supabase = createClient();

    const { data, error } = await supabase.auth.signInWithOAuth({
      provider,
      options: {
        redirectTo: `${location.origin}/auth/callback`,
        // Azure requires the email scope explicitly to return the user's email.
        ...(provider === "azure" ? { scopes: "email" } : {}),
        // We perform the redirect ourselves with the returned URL.
        skipBrowserRedirect: true,
      },
    });

    if (error || !data?.url) {
      setPending(null);
      const message = error?.message ?? "Could not start sign-in.";
      window.location.href = `/login?error=${encodeURIComponent(message)}`;
      return;
    }

    window.location.href = data.url;
  }

  return (
    <div className="oauth">
      <button
        type="button"
        className="secondary"
        onClick={() => signIn("google")}
        disabled={pending !== null}
      >
        {pending === "google" ? "Redirecting…" : "Continue with Google"}
      </button>
      <button
        type="button"
        className="secondary"
        onClick={() => signIn("azure")}
        disabled={pending !== null}
      >
        {pending === "azure" ? "Redirecting…" : "Continue with Microsoft"}
      </button>
    </div>
  );
}
