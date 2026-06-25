"use client";

import { useState } from "react";

import { createClient } from "@/lib/supabase/client";

const API_URL = process.env.NEXT_PUBLIC_API_URL!;
const POLL_MS = 2000;

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

type Job = {
  job_id: string;
  status: "pending" | "running" | "done" | "error";
  stage: string | null;
  total: number;
  processed: number;
  error: string | null;
};

type UiStatus = "idle" | "working" | "done" | "error";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

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
  const [status, setStatus] = useState<UiStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [checklists, setChecklists] = useState<Checklists | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setStatus("working");
    setError(null);
    setJob(null);
    setChecklists(null);

    try {
      const token = await accessToken();
      if (!token) throw new Error("Your session expired — please sign in again.");
      const auth = { Authorization: `Bearer ${token}` };

      // 1) Enqueue.
      const form = new FormData();
      form.append("file", file);
      const enqueue = await fetch(`${API_URL}/process`, {
        method: "POST",
        headers: auth,
        body: form,
      });
      if (enqueue.status !== 202) {
        throw new Error(`Upload failed (HTTP ${enqueue.status}).`);
      }
      const { job_id } = (await enqueue.json()) as { job_id: string };

      // 2) Poll until done/error.
      for (;;) {
        await sleep(POLL_MS);
        const res = await fetch(`${API_URL}/jobs/${job_id}`, { headers: auth });
        if (!res.ok) throw new Error(`Lost track of the job (HTTP ${res.status}).`);
        const j = (await res.json()) as Job;
        setJob(j);
        if (j.status === "done") break;
        if (j.status === "error") {
          setError(j.error ?? "Processing failed.");
          setStatus("error");
          return;
        }
      }

      // 3) Render results.
      const listRes = await fetch(`${API_URL}/checklists`, { headers: auth });
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

  const progressLabel = () => {
    if (!job) return "Uploading…";
    const stage = job.stage ?? job.status;
    if (job.total > 0) return `Processing — ${stage} ${job.processed}/${job.total}…`;
    return `Processing — ${stage}…`;
  };

  return (
    <section className="panel">
      <h2>Analyze a chat export</h2>
      <p className="muted">
        Upload a ChatGPT or Claude export (.json). Your full history is processed in the
        background.
      </p>

      <form onSubmit={onSubmit} className="form">
        <input
          type="file"
          accept="application/json,.json"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          required
        />
        <button type="submit" disabled={!file || status === "working"}>
          {status === "working" ? "Processing…" : "Process"}
        </button>
      </form>

      {status === "working" ? (
        <p className="notice" role="status">
          {progressLabel()}
        </p>
      ) : null}

      {error ? (
        <p className="alert" role="alert">
          {error}
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
