"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";

interface SubcontractorRow {
  id: string;
  name: string;
  trade: string | null;
  contact_email: string | null;
}

export default function SubcontractorsPage() {
  const { accessToken, role } = useAuth();
  const [subcontractors, setSubcontractors] = React.useState<SubcontractorRow[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const requestGenRef = React.useRef(0);

  // Creation is admin-only on the backend (POST /subcontractors).
  const canCreate = role === "admin";

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      const generation = replace ? ++requestGenRef.current : requestGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/subcontractors?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load subcontractors");
          return;
        }
        setSubcontractors((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === requestGenRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === requestGenRef.current) setLoading(false);
      }
    },
    [accessToken]
  );

  React.useEffect(() => {
    void Promise.resolve().then(() => load(null, true));
  }, [load]);

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Subcontractors</h1>
        {canCreate && (
          <Link href="/subcontractors/new">
            <Button>New subcontractor</Button>
          </Link>
        )}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && subcontractors.length === 0 && !error && (
        <p className="text-sm text-slate-600">No subcontractors yet.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
        {subcontractors.map((sub) => (
          <li key={sub.id}>
            <Link
              href={`/subcontractors/${sub.id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <span className="flex-1 text-sm font-medium">{sub.name}</span>
              <span className="text-sm text-slate-600">{sub.trade ?? "—"}</span>
              <span className="text-sm text-slate-500">{sub.contact_email ?? ""}</span>
            </Link>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
          Load more
        </Button>
      )}
    </main>
  );
}
