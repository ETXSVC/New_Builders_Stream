"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatCurrency } from "@/lib/format";

interface ChangeOrder {
  id: string;
  description: string;
  cost_delta: string;
  schedule_impact_days: number;
  status: string;
}

export function ChangeOrdersTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [changeOrders, setChangeOrders] = React.useState<ChangeOrder[]>([]);
  const [description, setDescription] = React.useState("");
  const [costDelta, setCostDelta] = React.useState("");
  const [scheduleImpactDays, setScheduleImpactDays] = React.useState("0");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: ChangeOrder[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects/${projectId}/change-orders?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load change orders");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setChangeOrders(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/change-orders`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          description,
          cost_delta: costDelta,
          schedule_impact_days: Number(scheduleImpactDays) || 0,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create change order");
        return;
      }
      setDescription("");
      setCostDelta("");
      setScheduleImpactDays("0");
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSendForSignature(id: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/change-orders/${id}/send-for-signature`, {
      method: "POST",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.ok) await loadAll();
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-col gap-3 max-w-md">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="co-description">Description</Label>
            <Textarea id="co-description" value={description} onChange={(e) => setDescription(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex gap-2">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="co-cost">Cost delta</Label>
              <Input id="co-cost" type="number" step="0.01" value={costDelta} onChange={(e) => setCostDelta(e.target.value)} disabled={submitting} required />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="co-days">Schedule impact (days)</Label>
              <Input id="co-days" type="number" value={scheduleImpactDays} onChange={(e) => setScheduleImpactDays(e.target.value)} disabled={submitting} />
            </div>
          </div>
          <Button type="submit" disabled={submitting} className="self-start">
            Add change order
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {changeOrders.map((co) => (
          <li key={co.id} className="flex items-center gap-3 px-4 py-3 text-sm">
            <span className="flex-1">{co.description}</span>
            <span className={Number(co.cost_delta) < 0 ? "text-green-700" : "text-slate-700"}>
              {formatCurrency(co.cost_delta)}
            </span>
            <StatusBadge status={co.status} />
            {canWrite && co.status === "pending" && (
              <Button type="button" size="sm" variant="outline" onClick={() => handleSendForSignature(co.id)}>
                Send for signature
              </Button>
            )}
          </li>
        ))}
        {changeOrders.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No change orders yet.</li>}
      </ul>
    </div>
  );
}
