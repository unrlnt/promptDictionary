import { type NextRequest } from "next/server";
import { updateSession } from "@/lib/supabase/middleware";

export async function middleware(request: NextRequest) {
  return await updateSession(request);
}

export const config = {
  matcher: [
    // Run on all paths except Next.js internals and common static assets.
    //
    // `api/process` is also excluded: when the proxy (middleware) is in the
    // request path, Next buffers a clone of the request body capped at 10MB
    // (proxyClientMaxBodySize), which truncates large chat-export uploads and
    // makes FastAPI reject the partial multipart with HTTP 422. The upload
    // route reads the session itself, so it does not need the proxy — skipping
    // it lets the body stream straight through, uncapped.
    "/((?!api/process|_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)",
  ],
};
