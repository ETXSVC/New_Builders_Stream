"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface MarkupProfile {
  id: string;
  name: string;
  overhead_pct: string;
  profit_pct: string;
}

export function MarkupProfilesTab() {
  const { accessToken, role } = useAuth();
  const [profiles, setProfiles] = React.useState<MarkupProfile[]>([]);
  const [name, setName] = React.useState("");
  const [overheadPct, setOverheadPct] = React.useState("0");
  const [profitPct, setProfitPct] = React.useState("0");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: MarkupProfile[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/markup-profiles?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load markup profiles");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setProfiles(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/markup-profiles", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ name, overhead_pct: overheadPct, profit_pct: profitPct }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create markup profile");
        return;
      }
      setName("");
      setOverheadPct("0");
      setProfitPct("0");
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDelete(profileId: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/markup-profiles/${profileId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.status === 204) {
      await loadAll();
    } else {
      const data = await response.json();
      setError(data.detail ?? "Failed to delete markup profile");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="markup-name">Name</Label>
            <Input id="markup-name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="markup-overhead">Overhead %</Label>
            <Input id="markup-overhead" className="w-24" type="number" step="0.01" value={overheadPct} onChange={(e) => setOverheadPct(e.target.value)} disabled={submitting} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="markup-profit">Profit %</Label>
            <Input id="markup-profit" className="w-24" type="number" step="0.01" value={profitPct} onChange={(e) => setProfitPct(e.target.value)} disabled={submitting} />
          </div>
          <Button type="submit" disabled={submitting}>Add profile</Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {profiles.map((profile) => (
          <li key={profile.id} className="flex items-center gap-3 px-4 py-2 text-sm">
            <span className="flex-1">{profile.name}</span>
            <span className="text-slate-500">{profile.overhead_pct}% overhead · {profile.profit_pct}% profit</span>
            {canWrite && (
              <button type="button" onClick={() => handleDelete(profile.id)} className="text-slate-400 hover:text-red-600">Delete</button>
            )}
          </li>
        ))}
        {profiles.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No markup profiles yet.</li>}
      </ul>
    </div>
  );
}
