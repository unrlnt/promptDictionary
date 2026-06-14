import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "promptdict",
  description: "Learn prompting patterns from your own AI chat history.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
