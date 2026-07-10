import { redirect } from "next/navigation";

import { createClient } from "@/lib/supabase/server";
import { DashboardNav } from "@/components/dashboard-nav";
import { logout } from "../actions";
import { ImprovePanel } from "./improve-panel";

export default async function ImprovePage() {
  const supabase = await createClient();

  // getUser() verifies the token with the Supabase auth server (unlike
  // getSession(), which only reads the cookie). Gate the page on it.
  const {
    data: { user },
    error,
  } = await supabase.auth.getUser();

  if (error || !user) {
    redirect("/login");
  }

  return (
    <main className="container">
      <h1>Dashboard</h1>
      <p className="muted">You are signed in.</p>

      <dl className="kv">
        <dt>Email</dt>
        <dd>{user.email}</dd>
      </dl>

      <form action={logout}>
        <button type="submit">Log out</button>
      </form>

      <DashboardNav />

      <ImprovePanel />
    </main>
  );
}
