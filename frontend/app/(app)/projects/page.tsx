"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDate } from "@/lib/format";

interface StaffProject {
  id: string;
  name: string;
  site_address: string;
  status: string;
  projected_start_date: string | null;
}

interface ClientProject extends StaffProject {
  phase_count: number;
  task_count: number;
  completed_task_count: number;
}

export default function ProjectsPage() {
  const { accessToken, role } = useAuth();
  const [items, setItems] = React.useState<(StaffProject | ClientProject)[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load projects");
          return;
        }
        setItems((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        setError("Unable to reach the server. Check your connection and try again.");
      } finally {
        setLoading(false);
      }
    },
    [accessToken]
  );

  React.useEffect(() => {
    load(null, true);
  }, [load]);

  const canCreate = role === "admin" || role === "project_manager";

  return (
    <main className="p-6 flex flex-col gap-4 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Projects</h1>
        {canCreate && (
          <Link href="/projects/new">
            <Button>New project</Button>
          </Link>
        )}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && items.length === 0 && !error && (
        <p className="text-sm text-slate-600">
          {canCreate ? "No projects yet — create your first project." : "No projects yet."}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {items.map((project) => (
          <li key={project.id}>
            <Link href={`/projects/${project.id}`} className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50">
              <span className="flex-1">
                <span className="block text-sm font-medium">{project.name}</span>
                <span className="block text-sm text-slate-600">{project.site_address || "No site address yet"}</span>
              </span>
              {"task_count" in project && (
                <span className="text-sm text-slate-600">
                  {project.completed_task_count}/{project.task_count} tasks done
                </span>
              )}
              <span className="text-sm text-slate-500">{formatDate(project.projected_start_date)}</span>
              <StatusBadge status={project.status} />
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
