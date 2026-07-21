"use client";

import * as React from "react";
import Link from "next/link";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatCurrency, formatDate } from "@/lib/format";
import { useAuth } from "@/contexts/AuthContext";
import { SigningPanel } from "@/components/esign/SigningPanel";

function AwaitingSignatureCard({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const [sentEstimates, setSentEstimates] = React.useState<{ id: string; total: string | null }[]>([]);
  const [pendingChangeOrders, setPendingChangeOrders] = React.useState<
    { id: string; description: string; cost_delta: string }[]
  >([]);
  const [expandedCoId, setExpandedCoId] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    const [allEstimates, allChangeOrders] = await Promise.all([
      (async () => {
        const all: { id: string; total: string | null; project_id?: string }[] = [];
        let cursor: string | null = null;
        do {
          const params = new URLSearchParams({ status: "sent" });
          if (cursor) params.set("cursor", cursor);
          const response = await fetch(`/api/estimates?${params}`, { headers: { Authorization: `Bearer ${accessToken}` } });
          if (!response.ok) return all;
          const data = await response.json();
          all.push(...data.items);
          cursor = data.next_cursor ?? null;
        } while (cursor);
        return all;
      })(),
      (async () => {
        const all: { id: string; description: string; cost_delta: string; project_id?: string }[] = [];
        let cursor: string | null = null;
        do {
          const params = new URLSearchParams({ status: "pending" });
          if (cursor) params.set("cursor", cursor);
          const response = await fetch(`/api/change-orders?${params}`, { headers: { Authorization: `Bearer ${accessToken}` } });
          if (!response.ok) return all;
          const data = await response.json();
          all.push(...data.items);
          cursor = data.next_cursor ?? null;
        } while (cursor);
        return all;
      })(),
    ]);
    setSentEstimates(allEstimates.filter((e) => e.project_id === projectId));
    setPendingChangeOrders(allChangeOrders.filter((co) => co.project_id === projectId));
  }, [accessToken, projectId]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  if (sentEstimates.length === 0 && pendingChangeOrders.length === 0) return null;

  return (
    <div className="flex flex-col gap-3 border border-amber-200 bg-amber-50 rounded-md p-4">
      <p className="text-sm font-medium">Awaiting your signature</p>
      {sentEstimates.map((e) => (
        <Link key={e.id} href={`/estimates/${e.id}`} className="text-sm text-blue-600 hover:underline">
          Estimate — {formatCurrency(e.total)}
        </Link>
      ))}
      {pendingChangeOrders.map((co) => (
        <div key={co.id} className="flex flex-col gap-2">
          <button
            type="button"
            onClick={() => setExpandedCoId(expandedCoId === co.id ? null : co.id)}
            className="text-sm text-left text-blue-600 hover:underline"
          >
            Change order — {co.description} ({formatCurrency(co.cost_delta)})
          </button>
          {expandedCoId === co.id && accessToken && (
            <SigningPanel
              approveUrl={`/api/change-orders/${co.id}/approve`}
              rejectUrl={`/api/change-orders/${co.id}/reject`}
              accessToken={accessToken}
              onDone={load}
            />
          )}
        </div>
      ))}
    </div>
  );
}

export interface ClientProjectShape {
  id: string;
  name: string;
  status: string;
  site_address: string;
  projected_start_date: string | null;
  phase_count: number;
  task_count: number;
  completed_task_count: number;
}

export function ClientProjectDashboard({ project }: { project: ClientProjectShape }) {
  return (
    <div className="flex flex-col gap-4 max-w-md">
      <AwaitingSignatureCard projectId={project.id} />
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>{project.name}</CardTitle>
            <StatusBadge status={project.status} />
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-2 text-sm text-slate-600">
          <p>{project.site_address || "Site address pending"}</p>
          <p>Projected start: {formatDate(project.projected_start_date)}</p>
          <p>
            {project.phase_count} {project.phase_count === 1 ? "phase" : "phases"} ·{" "}
            {project.completed_task_count} of {project.task_count} tasks complete
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
