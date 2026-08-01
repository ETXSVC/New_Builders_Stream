"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useCursorAll } from "@/lib/use-cursor-list";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";

const CHANNELS = ["call", "email", "note", "sms"] as const;
const CHANNEL_LABELS: Record<string, string> = { call: "Call", email: "Email", note: "Note", sms: "SMS" };

interface Entry {
  id: string;
  channel: string;
  body: string;
  created_at: string;
}

export function CommunicationLog({ leadId }: { leadId: string }) {
  const { accessToken } = useAuth();
  const {
    items: entries,
    error,
    setError,
    reload,
  } = useCursorAll<Entry>({ path: `/api/leads/${leadId}/communications`, label: "communications" });
  const [channel, setChannel] = React.useState<string>("call");
  const [body, setBody] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);



  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/leads/${leadId}/communications`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ channel, body }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to log communication");
        return;
      }
      setBody("");
      reload();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-semibold">Communication log</h2>
      <form onSubmit={handleAdd} className="flex gap-2">
        <Select aria-label="Channel" className="w-28" value={channel} onChange={(e) => setChannel(e.target.value)} disabled={submitting}>
          {CHANNELS.map((c) => (
            <option key={c} value={c}>
              {CHANNEL_LABELS[c]}
            </option>
          ))}
        </Select>
        <Input
          aria-label="Communication summary"
          placeholder="Log a call, email, or note"
          className="flex-1"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          disabled={submitting}
          required
        />
        <Button type="submit" disabled={submitting}>
          Add
        </Button>
      </form>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {entries.length === 0 && <p className="text-sm text-slate-600">No communications logged yet.</p>}
      <ul className="flex flex-col gap-3">
        {entries.map((entry) => (
          <li key={entry.id} className="border-b border-slate-200 pb-3">
            <div className="flex justify-between text-xs">
              <span className="font-medium">{CHANNEL_LABELS[entry.channel] ?? entry.channel}</span>
              <span className="text-slate-500">{new Date(entry.created_at).toLocaleString()}</span>
            </div>
            <p className="mt-1 text-sm text-slate-700">{entry.body}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}
