"use client";

import * as React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { StatusBadge } from "@/components/ui/status-badge";
import { ProjectStatusActions } from "@/components/projects/ProjectStatusActions";
import { PhasesTasksTab } from "@/components/projects/PhasesTasksTab";
import { DocumentsTab } from "@/components/projects/DocumentsTab";
import { DailyLogsTab } from "@/components/projects/DailyLogsTab";
import { ClientProjectDashboard, ClientProjectShape } from "@/components/projects/ClientProjectDashboard";
import { ChangeOrdersTab } from "@/components/change-orders/ChangeOrdersTab";
import { MaterialsTab } from "@/components/materials/MaterialsTab";
import { formatCurrency, formatDate } from "@/lib/format";
import { cn } from "@/lib/utils";

interface StaffProject {
  id: string;
  lead_id: string | null;
  name: string;
  site_address: string;
  status: string;
  projected_start_date: string | null;
}

const TABS = ["Overview", "Phases & tasks", "Documents", "Daily logs", "Change orders", "Estimates", "Materials"] as const;
type Tab = (typeof TABS)[number];

export default function ProjectDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { accessToken } = useAuth();
  const [project, setProject] = React.useState<StaffProject | ClientProjectShape | null>(null);
  const [tab, setTab] = React.useState<Tab>("Overview");
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/projects/${id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load project");
        return;
      }
      setProject(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, id]);

  React.useEffect(() => {
    // Deferred to a promise callback so no setState in load's call path
    // runs synchronously inside the effect (react-hooks/set-state-in-effect).
    void Promise.resolve().then(() => load());
  }, [load]);

  if (!project) {
    return (
      <main className="p-6">
        {error ? (
          <p role="alert" className="text-sm text-red-600">{error}</p>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </main>
    );
  }

  // The backend shapes the response by role: the sanitized client shape
  // carries phase/task counts and no lead_id (spec Decision 1).
  if ("phase_count" in project) {
    return (
      <main className="p-6">
        <ClientProjectDashboard project={project} />
      </main>
    );
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">{project.name}</h1>
        <StatusBadge status={project.status} />
      </div>
      <p className="text-sm text-slate-600 -mt-4">
        {project.site_address || "No site address yet"} · projected start {formatDate(project.projected_start_date)}
      </p>

      <ProjectStatusActions projectId={project.id} status={project.status} onChanged={load} />

      <div className="flex gap-1 border-b border-slate-200" role="tablist">
        {TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={cn(
              "px-3 py-2 text-sm",
              tab === t
                ? "border-b-2 border-blue-600 font-medium text-slate-900"
                : "text-slate-600 hover:text-slate-900"
            )}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "Overview" && <OverviewTab project={project} onSaved={load} />}
      {tab === "Phases & tasks" && <PhasesTasksTab projectId={project.id} />}
      {tab === "Documents" && <DocumentsTab projectId={project.id} />}
      {tab === "Daily logs" && <DailyLogsTab projectId={project.id} />}
      {tab === "Change orders" && <ChangeOrdersTab projectId={project.id} />}
      {tab === "Estimates" && <ProjectEstimatesTab projectId={project.id} />}
      {tab === "Materials" && <MaterialsTab projectId={project.id} />}
    </main>
  );
}

function OverviewTab({ project, onSaved }: { project: StaffProject; onSaved: () => void }) {
  const { accessToken, role } = useAuth();
  const [name, setName] = React.useState(project.name);
  const [siteAddress, setSiteAddress] = React.useState(project.site_address);
  const [startDate, setStartDate] = React.useState(project.projected_start_date ?? "");
  const [submitting, setSubmitting] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canEdit = role === "admin" || role === "project_manager";

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSaved(false);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${project.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ name, site_address: siteAddress, projected_start_date: startDate || null }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to save project");
        return;
      }
      setSaved(true);
      onSaved();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!canEdit) {
    return (
      <p className="text-sm text-slate-600">
        {project.site_address || "No site address yet"} · projected start {formatDate(project.projected_start_date)}
      </p>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-md">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ov-name">Project name</Label>
        <Input id="ov-name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ov-site">Site address</Label>
        <Input id="ov-site" value={siteAddress} onChange={(e) => setSiteAddress(e.target.value)} disabled={submitting} required />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="ov-start">Projected start date</Label>
        <Input id="ov-start" type="date" value={startDate} onChange={(e) => setStartDate(e.target.value)} disabled={submitting} />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {saved && <p className="text-sm text-green-700">Saved.</p>}
      <Button type="submit" disabled={submitting}>
        Save changes
      </Button>
    </form>
  );
}

function ProjectEstimatesTab({ projectId }: { projectId: string }) {
  const { accessToken } = useAuth();
  const [estimates, setEstimates] = React.useState<{ id: string; status: string; total: string | null }[]>([]);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    // Client-side filter: no ?project_id= query param exists on
    // GET /estimates (out of this plan's scope to add one). All pages
    // are fetched to exhaustion so the filter sees the full result set.
    try {
      const all: { id: string; status: string; total: string | null; project_id?: string }[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/estimates?${params}`, { headers: { Authorization: `Bearer ${accessToken}` } });
        if (!response.ok) return;
        const data = await response.json();
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setEstimates(all.filter((e) => e.project_id === projectId));
    } catch {
      // Non-blocking — the list just stays empty if this fails.
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  return (
    <div className="flex flex-col gap-3">
      <Link href={`/estimates/new?project_id=${projectId}`}>
        <Button size="sm">New estimate</Button>
      </Link>
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {estimates.map((e) => (
          <li key={e.id}>
            <Link href={`/estimates/${e.id}`} className="flex items-center justify-between px-4 py-2 text-sm hover:bg-slate-50">
              <StatusBadge status={e.status} />
              <span>{formatCurrency(e.total)}</span>
            </Link>
          </li>
        ))}
        {estimates.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No estimates yet.</li>}
      </ul>
    </div>
  );
}
