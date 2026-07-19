"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { formatDate } from "@/lib/format";

interface DailyLog {
  id: string;
  log_date: string;
  weather: string | null;
  notes: string | null;
}

export function DailyLogsTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [logs, setLogs] = React.useState<DailyLog[]>([]);
  const [logDate, setLogDate] = React.useState(() => {
    // Built from local date parts — toISOString() is UTC and shifts the
    // default to yesterday/tomorrow for users near midnight in other zones.
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${now.getFullYear()}-${month}-${day}`;
  });
  const [weather, setWeather] = React.useState("");
  const [notes, setNotes] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager" || role === "field_crew";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      // The backend pages at 25/entry ascending by created_at, so a
      // just-created log lands on the LAST page — follow next_cursor to
      // exhaustion so it (and everything else) is always visible. List
      // sizes here are small enough that a few sequential requests are fine.
      const all: DailyLog[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects/${projectId}/daily-logs?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load daily logs");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setLogs(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    loadAll();
  }, [loadAll]);

  async function handleAdd(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/daily-logs`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ log_date: logDate, weather: weather || null, notes: notes || null }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to add daily log");
        return;
      }
      setWeather("");
      setNotes("");
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleAdd} className="flex flex-col gap-3 max-w-md">
          <div className="flex gap-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="log-date">Date</Label>
              <Input id="log-date" type="date" value={logDate} onChange={(e) => setLogDate(e.target.value)} disabled={submitting} required />
            </div>
            <div className="flex flex-col gap-1.5 flex-1">
              <Label htmlFor="log-weather">Weather (optional)</Label>
              <Input id="log-weather" value={weather} onChange={(e) => setWeather(e.target.value)} disabled={submitting} maxLength={100} />
            </div>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="log-notes">Notes</Label>
            <Textarea id="log-notes" value={notes} onChange={(e) => setNotes(e.target.value)} disabled={submitting} />
          </div>
          <Button type="submit" disabled={submitting} className="self-start">
            Add log entry
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {logs.length === 0 && <p className="text-sm text-slate-600">No daily logs yet.</p>}
      <ul className="flex flex-col gap-3">
        {logs.map((log) => (
          <li key={log.id} className="border-b border-slate-200 pb-3 text-sm">
            <div className="flex justify-between">
              <span className="font-medium">{formatDate(log.log_date)}</span>
              {log.weather && <span className="text-slate-500">{log.weather}</span>}
            </div>
            {log.notes && <p className="mt-1 text-slate-700 whitespace-pre-wrap">{log.notes}</p>}
          </li>
        ))}
      </ul>
    </section>
  );
}
