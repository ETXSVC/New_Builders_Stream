"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Select } from "@/components/ui/select";
import { TASK_STATUSES, labelFor } from "@/lib/state-machines";
import { formatDate } from "@/lib/format";

interface MyTask {
  id: string;
  name: string;
  status: string;
  due_date: string | null;
  project_id: string;
  project_name: string;
}

export default function MyTasksPage() {
  const { accessToken } = useAuth();
  const [tasks, setTasks] = React.useState<MyTask[]>([]);
  const [loading, setLoading] = React.useState(true);
  // Per-task in-flight guard: disables a row's status Select while its
  // PATCH is pending so rapid changes can't interleave.
  const [pendingTasks, setPendingTasks] = React.useState<Record<string, boolean>>({});
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    try {
      const response = await fetch("/api/my-tasks", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load tasks");
        return;
      }
      setTasks(data.items);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, [accessToken]);

  React.useEffect(() => {
    // Deferred to a promise callback so no setState in load's call path
    // runs synchronously inside the effect (react-hooks/set-state-in-effect).
    void Promise.resolve().then(() => load());
  }, [load]);

  async function setStatus(taskId: string, status: string) {
    if (!accessToken || pendingTasks[taskId]) return;
    setError(null);
    setPendingTasks((prev) => ({ ...prev, [taskId]: true }));
    try {
      const response = await fetch(`/api/tasks/${taskId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ status }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to update task");
        return;
      }
      setTasks((prev) => prev.map((t) => (t.id === taskId ? { ...t, status } : t)));
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setPendingTasks((prev) => ({ ...prev, [taskId]: false }));
    }
  }

  return (
    <main className="p-6 flex flex-col gap-4 max-w-2xl">
      <h1 className="text-xl font-semibold">My tasks</h1>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && tasks.length === 0 && !error && (
        <p className="text-sm text-slate-600">No tasks assigned to you right now.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {tasks.map((task) => (
          <li key={task.id} className="flex items-center gap-4 px-4 py-3 text-sm">
            <span className="flex-1">
              <span className="block font-medium">{task.name}</span>
              <Link href={`/projects/${task.project_id}`} className="text-slate-600 hover:underline">
                {task.project_name}
              </Link>
            </span>
            <span className="text-slate-500">{formatDate(task.due_date)}</span>
            <Select
              aria-label={`Status for ${task.name}`}
              className="w-32 h-8"
              value={task.status}
              onChange={(e) => setStatus(task.id, e.target.value)}
              disabled={!!pendingTasks[task.id]}
            >
              {TASK_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {labelFor(s)}
                </option>
              ))}
            </Select>
          </li>
        ))}
      </ul>
    </main>
  );
}
