"use client";

import * as React from "react";
import { useSearchParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";
import { formatDate } from "@/lib/format";

const PROVIDERS = [
  { key: "quickbooks", label: "QuickBooks" },
  { key: "freshbooks", label: "FreshBooks" },
] as const;

const SYNC_STATUSES = ["pending", "success", "failed"] as const;

interface SyncRecord {
  id: string;
  entity_type: string;
  entity_id: string;
  status: string;
  attempt_count: number;
  last_error: string | null;
  last_attempted_at: string | null;
  external_record_id: string | null;
}

export default function IntegrationsPage() {
  return (
    <main className="p-6 flex flex-col gap-6 max-w-3xl">
      <h1 className="text-xl font-semibold">Integrations</h1>
      {/* The backend's OAuth callback 303s back here with ?connected=<provider>
          after a successful connect — surface that as a success notice.
          useSearchParams requires a Suspense boundary in the App Router. */}
      <React.Suspense fallback={null}>
        <ConnectedNotice />
      </React.Suspense>
      {PROVIDERS.map((provider) => (
        <ProviderCard key={provider.key} providerKey={provider.key} label={provider.label} />
      ))}
    </main>
  );
}

function ConnectedNotice() {
  const connected = useSearchParams().get("connected");
  const provider = PROVIDERS.find((p) => p.key === connected);
  if (!provider) return null;
  return (
    <p
      role="status"
      className="rounded-lg border border-green-300 bg-green-50 px-4 py-3 text-sm text-green-900"
    >
      {provider.label} connected successfully.
    </p>
  );
}

function ProviderCard({ providerKey, label }: { providerKey: string; label: string }) {
  const { accessToken } = useAuth();
  // null = still loading; false = not connected; string = connected_at.
  const [connectedAt, setConnectedAt] = React.useState<string | false | null>(null);
  const [records, setRecords] = React.useState<SyncRecord[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [statusFilter, setStatusFilter] = React.useState("");
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [planGate, setPlanGate] = React.useState<string | null>(null);
  const [connecting, setConnecting] = React.useState(false);
  const requestGenRef = React.useRef(0);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      const generation = replace ? ++requestGenRef.current : requestGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (statusFilter) params.set("status", statusFilter);
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/integrations/${providerKey}/sync-status?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          // 404 means "no connection yet" — the card's normal
          // pre-connection state, not an error.
          if (response.status === 404) {
            setConnectedAt(false);
            setRecords([]);
            setNextCursor(null);
            return;
          }
          setError(data.detail ?? "Failed to load sync status");
          return;
        }
        setConnectedAt(data.connected_at);
        setRecords((prev) => (replace ? data.records : [...prev, ...data.records]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === requestGenRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === requestGenRef.current) setLoading(false);
      }
    },
    [accessToken, providerKey, statusFilter]
  );

  React.useEffect(() => {
    void Promise.resolve().then(() => load(null, true));
  }, [load]);

  async function connect() {
    if (connecting || !accessToken) return;
    setConnecting(true);
    setError(null);
    setPlanGate(null);
    try {
      const response = await fetch(`/api/integrations/${providerKey}/connect`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          setError(data.detail ?? "Failed to start the connection");
        }
        return;
      }
      window.location.assign(data.authorization_url);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setConnecting(false);
    }
  }

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-slate-200 p-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold">{label}</h2>
          <p className="text-sm text-slate-600">
            {connectedAt === null
              ? "Checking connection…"
              : connectedAt === false
                ? "Not connected"
                : `Connected ${formatDate(connectedAt)}`}
          </p>
        </div>
        {connectedAt === false && (
          <Button onClick={connect} disabled={connecting}>
            {connecting ? "Redirecting…" : `Connect ${label}`}
          </Button>
        )}
      </div>

      {planGate && <PlanUpgradeNotice detail={planGate} />}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {typeof connectedAt === "string" && (
        <div className="flex flex-col gap-3">
          <Select
            aria-label={`Filter ${label} sync records by status`}
            className="w-44"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">All statuses</option>
            {SYNC_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s[0].toUpperCase() + s.slice(1)}
              </option>
            ))}
          </Select>
          {!loading && records.length === 0 && !error && (
            <p className="text-sm text-slate-600">
              No sync records yet — invoices, bills, and expenses sync automatically as they&apos;re
              created.
            </p>
          )}
          <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
            {records.map((record) => (
              <li key={record.id} className="flex items-center gap-4 px-4 py-3 text-sm">
                <span className="flex-1 font-medium capitalize">{record.entity_type}</span>
                <span className="text-slate-500">
                  {record.last_attempted_at ? formatDate(record.last_attempted_at) : "—"}
                </span>
                {record.last_error && (
                  <span className="text-red-600 truncate max-w-48" title={record.last_error}>
                    {record.last_error}
                  </span>
                )}
                <StatusBadge status={record.status} />
              </li>
            ))}
          </ul>
          {nextCursor && (
            <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
              Load more
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
