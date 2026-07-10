"use client";

import { useState } from "react";

import { createClient } from "@/lib/supabase/client";

type UiStatus = "idle" | "working" | "done" | "error";
type Result = { improved_prompt: string; cluster_label: string | null };

async function accessToken(): Promise<string | null> {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}

export function ImprovePanel() {
  const [prompt, setPrompt] = useState("");
  const [status, setStatus] = useState<UiStatus>("idle");
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!prompt.trim()) return;
    setStatus("working");
    setError("");
    setResult(null);
    setCopied(false);

    try {
      const token = await accessToken();
      if (!token) throw new Error("Your session expired — please sign in again.");

      const res = await fetch("/api/improve", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "content-type": "application/json",
        },
        body: JSON.stringify({ prompt }),
      });
      if (!res.ok) {
        throw new Error(`Could not improve your prompt (HTTP ${res.status}).`);
      }
      setResult((await res.json()) as Result);
      setStatus("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      setStatus("error");
    }
  }

  async function onCopy() {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.improved_prompt);
      setCopied(true);
    } catch {
      setError("Couldn’t copy to clipboard.");
    }
  }

  return (
    <section className="panel">
      <h2>Improve your prompt</h2>
      <p className="muted">
        Paste your draft prompt and we’ll rewrite it based on what you tend to forget.
      </p>

      <form onSubmit={onSubmit} className="form">
        <textarea
          rows={4}
          value={prompt}
          placeholder="Paste your draft prompt…"
          onChange={(e) => setPrompt(e.target.value)}
          required
        />
        <button type="submit" disabled={status === "working" || !prompt.trim()}>
          {status === "working" ? "Improving…" : "Improve"}
        </button>
      </form>

      {status === "working" ? (
        <p className="notice" role="status">
          Improving your prompt…
        </p>
      ) : null}

      {error ? (
        <p className="alert" role="alert">
          {error}
        </p>
      ) : null}

      {result ? (
        <div className="results">
          {result.cluster_label ? (
            <p className="muted">Matched cluster: {result.cluster_label}</p>
          ) : null}
          <textarea readOnly rows={8} value={result.improved_prompt} />
          <button type="button" className="secondary" onClick={onCopy}>
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>
      ) : null}
    </section>
  );
}
