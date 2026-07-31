"use client";

import * as React from "react";
import type { components } from "@/lib/api/types";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type Profession = components["schemas"]["ProfessionResponse"];

/**
 * The trades this company recognises.
 *
 * Deliberately on the team page rather than behind a settings screen of its
 * own: the list exists to be picked from on the member record next to it,
 * and a company adds to it while filling one in, not on a separate errand.
 *
 * Removal is a `ConfirmButton` because it is quiet but not harmless —
 * whoever holds the trade keeps their record and simply loses it (the FK is
 * ON DELETE SET NULL), which is exactly the kind of change nobody notices
 * until someone asks who the electricians are.
 */
export function ProfessionsPanel({ onChanged }: { onChanged?: () => void }) {
  const { accessToken, role } = useAuth();
  const [professions, setProfessions] = React.useState<Profession[]>([]);
  const [name, setName] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const canEdit = role === "admin";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch("/api/team/professions", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load professions");
        return;
      }
      setProfessions(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    if (busy || !accessToken || !name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/team/professions", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ name: name.trim() }),
      });
      const data = await response.json();
      if (!response.ok) {
        // 409 is the case worth reading: the backend's unique index is
        // case-insensitive, so "electrician" collides with "Electrician"
        // and says so.
        setError(data.detail ?? "Failed to add the profession");
        return;
      }
      setName("");
      await load();
      onChanged?.();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  async function remove(profession: Profession) {
    if (busy || !accessToken) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`/api/team/professions/${profession.id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        setError(data?.detail ?? "Failed to remove the profession");
        return;
      }
      await load();
      // The list behind this panel shows each member's trade, and one of
      // them may have just lost theirs.
      onChanged?.();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-slate-200 p-4">
      <div>
        <h2 className="text-sm font-semibold">Professions</h2>
        <p className="text-xs text-slate-500">
          The trades you can assign on a team member&apos;s record.
        </p>
      </div>

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      <ul className="flex flex-wrap gap-2 empty:hidden">
        {professions.map((profession) => (
          <li
            key={profession.id}
            className="flex items-center gap-2 rounded-full bg-slate-100 py-1 pl-3 pr-1 text-sm"
          >
            {profession.name}
            {canEdit && (
              <ConfirmButton
                variant="ghost"
                size="sm"
                confirmMessage={`Remove ${profession.name}?`}
                confirmLabel="Remove"
                onConfirm={() => void remove(profession)}
                disabled={busy}
                aria-label={`Remove ${profession.name}`}
              >
                ×
              </ConfirmButton>
            )}
          </li>
        ))}
      </ul>

      {professions.length === 0 && (
        <p className="text-sm text-slate-600">No professions yet.</p>
      )}

      {canEdit && (
        <form className="flex items-end gap-2" onSubmit={add}>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="profession-name">Add a profession</Label>
            <Input
              id="profession-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={100}
              placeholder="e.g. Electrician"
              className="w-56"
            />
          </div>
          <Button type="submit" variant="outline" disabled={busy || !name.trim()}>
            Add
          </Button>
        </form>
      )}
    </section>
  );
}
