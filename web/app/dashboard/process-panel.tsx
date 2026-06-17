"use client";

import { useState } from "react";

import { createClient } from "@/lib/supabase/client";

const API_URL = process.env.NEXT_PUBLIC_API_URL!;

type ChecklistItem = {
  kind: string;
  conversation_count: number;
  total_count: number;
  rank: number;
  sample_notes: string[];
  graduation: string | null;
};

type ClusterChecklist = {
  cluster_id: string;
  label: string | null;
  items: ChecklistItem[];
};

type Checklists = { global: ChecklistItem[]; clusters: ClusterChecklist[] };

type Summary = {
  ingested: number;
  embedded: number;
  clusters: number;
  forgotten_rows: number;
};

type Status = "idle" | "processing" | "done" | "error";

async function accessToken(): Promise<string | null> {
  const supabase = createClient();
  const {
    data: { session },
  } = await supabase.auth.getSession();
  return session?.access_token ?? null;
}

function ChecklistRow({ item }: { item: ChecklistItem }) {
  return (
    <li>
      <strong>{item.kind}</strong> — {item.conversation_count} conv / {item.total_count}{" "}
      total
      {item.graduation ? <span className="tag"> {item.graduation}</span> : null}
      {item.sample_notes[0] ? (
        <span className="muted"> · e.g. “{item.sample_notes[0]}”</span>
      ) : null}
    </li>
  );
}

export function ProcessPanel() {
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<Status>("idle");
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [checklists, setChecklists] = useState<Checklists | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setStatus("processing");
    setError(null);
    setSummary(null);
    setChecklists(null);

    try {
      const token = await accessToken();
      if (!token) throw new Error("Your session expired — please sign in again.");

      const form = new FormData();
      form.append("file", file);

      const processRes = await fetch(`${API_URL}/process`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: form,
      });
      if (!processRes.ok) {
        throw new Error(`Processing failed (HTTP ${processRes.status}).`);
      }
      setSummary((await processRes.json()) as Summary);

      const listRes = await fetch(`${API_URL}/checklists`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!listRes.ok) {
        throw new Error(`Could not load checklists (HTTP ${listRes.status}).`);
      }
      setChecklists((await listRes.json()) as Checklists);
      setStatus("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong.");
      setStatus("error");
    }
  }

  return (
    <section className="panel">
      <h2>Analyze a chat export</h2>
      <p className="muted">
        Upload a ChatGPT or Claude export (.json). A capped slice is processed for now.
      </p>

      <form onSubmit={onSubmit} className="form">
        <input
          type="file"
          accept="application/json,.json"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          required
        />
        <button type="submit" disabled={!file || status === "processing"}>
          {status === "processing" ? "Processing…" : "Process"}
        </button>
      </form>

      {error ? (
        <p className="alert" role="alert">
          {error}
        </p>
      ) : null}

      {summary ? (
        <p className="notice" role="status">
          Ingested {summary.ingested} · embedded {summary.embedded} · {summary.clusters}{" "}
          cluster(s) · {summary.forgotten_rows} forgotten requirement(s).
        </p>
      ) : null}

      {checklists ? (
        <div className="results">
          <h3>What you tend to forget (global)</h3>
          {checklists.global.length ? (
            <ul>
              {checklists.global.map((item) => (
                <ChecklistRow key={item.kind} item={item} />
              ))}
            </ul>
          ) : (
            <p className="muted">No forgotten requirements found yet.</p>
          )}

          {checklists.clusters.map((cluster) => (
            <div key={cluster.cluster_id}>
              <h3>{cluster.label ?? "(unlabelled task type)"}</h3>
              <ul>
                {cluster.items.map((item) => (
                  <ChecklistRow key={item.kind} item={item} />
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}
