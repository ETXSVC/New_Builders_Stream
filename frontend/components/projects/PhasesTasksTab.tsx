"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";
import { TASK_STATUSES, labelFor } from "@/lib/state-machines";
import { formatDate } from "@/lib/format";

interface Task {
  id: string;
  name: string;
  assignee_id: string | null;
  due_date: string | null;
  status: string;
}

interface Phase {
  id: string;
  name: string;
  sequence: number;
  tasks: Task[];
}

interface Member {
  user_id: string;
  full_name: string;
  role: string;
}

export function PhasesTasksTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [phases, setPhases] = React.useState<Phase[]>([]);
  const [members, setMembers] = React.useState<Member[]>([]);
  const [expanded, setExpanded] = React.useState<Record<string, boolean>>({});
  const [newPhaseName, setNewPhaseName] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  // Per-task in-flight guard: disables a row's status Select while its
  // PATCH is pending so rapid changes can't interleave.
  const [pendingTasks, setPendingTasks] = React.useState<Record<string, boolean>>({});
  const [error, setError] = React.useState<string | null>(null);

  const canEdit = role === "admin" || role === "project_manager";
  const authHeaders = React.useMemo(
    () => ({ Authorization: `Bearer ${accessToken}` }),
    [accessToken]
  );

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/projects/${projectId}/phases`, { headers: authHeaders });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load phases");
        return;
      }
      setPhases(data.items);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, authHeaders, projectId]);

  React.useEffect(() => {
    load();
  }, [load]);

  React.useEffect(() => {
    if (!accessToken || !canEdit) return;
    fetch("/api/companies/members", { headers: authHeaders })
      .then(async (r) => {
        const data = await r.json();
        if (r.ok) setMembers(data.items);
      })
      .catch(() => {});
  }, [accessToken, authHeaders, canEdit]);

  async function addPhase(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/phases`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({ name: newPhaseName, sequence: phases.length + 1 }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to add phase");
        return;
      }
      setNewPhaseName("");
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function patchTask(taskId: string, body: Record<string, unknown>) {
    if (!accessToken || pendingTasks[taskId]) return;
    setError(null);
    setPendingTasks((prev) => ({ ...prev, [taskId]: true }));
    try {
      const response = await fetch(`/api/tasks/${taskId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to update task");
        return;
      }
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setPendingTasks((prev) => ({ ...prev, [taskId]: false }));
    }
  }

  const memberName = (userId: string | null) =>
    userId ? members.find((m) => m.user_id === userId)?.full_name ?? "Assigned" : "Unassigned";

  return (
    <section className="flex flex-col gap-4">
      {canEdit && (
        <form onSubmit={addPhase} className="flex gap-2">
          <Input
            aria-label="New phase name"
            placeholder="New phase name"
            className="max-w-xs"
            value={newPhaseName}
            onChange={(e) => setNewPhaseName(e.target.value)}
            disabled={submitting}
            required
          />
          <Button type="submit" disabled={submitting}>
            Add phase
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {phases.length === 0 && <p className="text-sm text-slate-600">No phases yet.</p>}
      {phases.map((phase) => {
        const isOpen = expanded[phase.id] ?? true;
        const done = phase.tasks.filter((t) => t.status === "done").length;
        return (
          <div key={phase.id} className="border border-slate-200 rounded-lg overflow-hidden">
            <button
              type="button"
              className="w-full flex items-center justify-between bg-slate-50 px-4 py-3 text-sm font-medium"
              aria-expanded={isOpen}
              onClick={() => setExpanded((prev) => ({ ...prev, [phase.id]: !isOpen }))}
            >
              <span>{phase.name}</span>
              <span className="text-xs text-slate-500">
                {phase.tasks.length} tasks · {done} done
              </span>
            </button>
            {isOpen && (
              <div className="px-4 pb-3">
                {phase.tasks.map((task) => (
                  <div key={task.id} className="flex items-center gap-3 border-t border-slate-200 py-2 text-sm">
                    <span className="flex-1">{task.name}</span>
                    <span className="text-slate-500 text-xs">{memberName(task.assignee_id)}</span>
                    <span className="text-slate-500 text-xs">{formatDate(task.due_date)}</span>
                    {canEdit || role === "field_crew" ? (
                      <Select
                        aria-label={`Status for ${task.name}`}
                        className="w-32 h-8"
                        value={task.status}
                        onChange={(e) => patchTask(task.id, { status: e.target.value })}
                        disabled={!!pendingTasks[task.id]}
                      >
                        {TASK_STATUSES.map((s) => (
                          <option key={s} value={s}>
                            {labelFor(s)}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <StatusBadge status={task.status} />
                    )}
                  </div>
                ))}
                {canEdit && <NewTaskRow phaseId={phase.id} projectId={projectId} members={members} onCreated={load} />}
              </div>
            )}
          </div>
        );
      })}
    </section>
  );
}

function NewTaskRow({
  phaseId,
  projectId,
  members,
  onCreated,
}: {
  phaseId: string;
  projectId: string;
  members: Member[];
  onCreated: () => void;
}) {
  const { accessToken } = useAuth();
  const [name, setName] = React.useState("");
  const [dueDate, setDueDate] = React.useState("");
  const [assignee, setAssignee] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          name,
          phase_id: phaseId,
          due_date: dueDate || null,
          assignee_id: assignee || null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to add task");
        return;
      }
      setName("");
      setDueDate("");
      setAssignee("");
      onCreated();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-wrap items-center gap-2 border-t border-slate-200 pt-3 mt-1">
      <Input
        aria-label="New task name"
        placeholder="New task"
        className="flex-1 min-w-40 h-8"
        value={name}
        onChange={(e) => setName(e.target.value)}
        disabled={submitting}
        required
      />
      <Input aria-label="Due date" type="date" className="w-36 h-8" value={dueDate} onChange={(e) => setDueDate(e.target.value)} disabled={submitting} />
      <Select aria-label="Assignee" className="w-40 h-8" value={assignee} onChange={(e) => setAssignee(e.target.value)} disabled={submitting}>
        <option value="">Unassigned</option>
        {members.map((m) => (
          <option key={m.user_id} value={m.user_id}>
            {m.full_name}
          </option>
        ))}
      </Select>
      <Button type="submit" size="sm" disabled={submitting}>
        Add task
      </Button>
      {error && (
        <p role="alert" aria-live="assertive" className="w-full text-sm text-red-600">
          {error}
        </p>
      )}
    </form>
  );
}
