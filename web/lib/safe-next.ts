// Clamp a user-supplied post-auth `next` redirect target to a safe LOCAL path.
// Prevents an open redirect: an attacker-crafted `?next=https://evil.com` (or a
// protocol-relative `//evil.com`) would otherwise bounce an authenticated user
// off-site. Only same-origin absolute paths ("/dashboard") are allowed; anything
// else falls back to /dashboard.
export function safeNext(next: string | null): string {
  if (next && next.startsWith("/") && !next.startsWith("//")) {
    return next;
  }
  return "/dashboard";
}
