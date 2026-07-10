import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

// Internal address of the FastAPI engine on the Docker network. Server-only.
const API_BASE = "http://api:8000";

// BFF: the browser POSTs a draft prompt here; we read the session cookie
// server-side, attach the JWT, and forward the JSON body to the engine's
// /improve endpoint. Response returned verbatim.
export async function POST(request: NextRequest) {
  const supabase = await createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const token = session?.access_token;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const body = await request.text();
  const upstream = await fetch(`${API_BASE}/improve`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "content-type": "application/json",
    },
    body,
  });

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type":
        upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
