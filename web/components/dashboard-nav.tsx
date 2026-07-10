"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// Shared dashboard navigation. Top horizontal nav on desktop, fixed bottom bar on
// mobile (styled in globals.css). Active link is derived from the current path,
// which is why this is a client component (usePathname).
const LINKS = [
  { href: "/dashboard", label: "My Clusters" },
  { href: "/dashboard/improve", label: "Improve Prompt" },
];

export function DashboardNav() {
  const pathname = usePathname();

  return (
    <nav className="dashboard-nav">
      {LINKS.map((link) => {
        const active = pathname === link.href;
        return (
          <Link
            key={link.href}
            href={link.href}
            className={active ? "active" : undefined}
            aria-current={active ? "page" : undefined}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
