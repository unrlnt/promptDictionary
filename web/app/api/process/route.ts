import { NextResponse, type NextRequest } from "next/server";

import { createClient } from "@/lib/supabase/server";

// Internal address of the FastAPI engine on the Docker network. Server-only —
// never exposed to the browser (the browser only ever calls this route handler).
const API_BASE = "http://api:8000";

// Node runtime so we can stream the multipart upload body straight through
// (duplex: "half") without buffering the whole file into memory.
export const runtime = "nodejs";

// BFF: the browser POSTs the file here instead of calling FastAPI directly. We
// read the session cookie server-side, attach the JWT as a Bearer token, and
// forward the multipart body verbatim to the engine. FastAPI verifies the JWT
// exactly as before.
export async function POST(request: NextRequest) {
  const supabase = await createClient();
  // getSession() reads the token from the cookie; we only need it to forward —
  // FastAPI is what verifies it, so no server-side revalidation is needed here.
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const token = session?.access_token;
  if (!token) {
    return NextResponse.json({ detail: "Not authenticated" }, { status: 401 });
  }

  const upstream = await fetch(`${API_BASE}/process`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      // Preserve the original multipart boundary by passing content-type through.
      "content-type": request.headers.get("content-type") ?? "",
    },
    body: request.body,
    // Required when streaming a request body with the fetch spec.
    duplex: "half",
  } as RequestInit & { duplex: "half" });

  // Return status + body verbatim to the browser.
  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: {
      "content-type":
        upstream.headers.get("content-type") ?? "application/json",
    },
  });
}
