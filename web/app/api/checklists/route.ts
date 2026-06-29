import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

// Internal address of the FastAPI engine on the Docker network. Server-only.
const API_BASE = "http://api:8000";

// BFF: the browser fetches derived checklists here; we read the session cookie
// server-side, attach the JWT, and forward to the engine. Returned verbatim.
export async function GET(_request: NextRequest) {
  const supabase = await createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const token = session?.access_token;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const upstream = await fetch(`${API_BASE}/checklists`, {
    headers: { Authorization: `Bearer ${token}` },
  });

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type":
        upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
