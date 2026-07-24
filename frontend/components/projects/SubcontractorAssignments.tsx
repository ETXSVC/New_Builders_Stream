"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";
import { formatDate } from "@/lib/format";

interface Assignment {
  id: string;
  subcontractor_id: string;
  override_reason: string | null;
  created_at: string;
}

interface SubcontractorOption {
  id: string;
  name: string;
}

export function SubcontractorAssignments({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [assignments, setAssignments] = React.useState<Assignment[]>([]);
  const [subcontractors, setSubcontractors] = React.useState<SubcontractorOption[]>([]);
  const [selectedId, setSelectedId] = React.useState("");
  const [overrideReason, setOverrideReason] = React.useState("");
  // The override textarea only appears after the backend rejects the
  // assignment for expired compliance — admins then retry with a reason.
  const [needsOverride, setNeedsOverride] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [planGate, setPlanGate] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  const canAssign = role === "admin" || role === "project_manager";
  const isAdmin = role === "admin";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const [assignmentsResponse, subsResponse] = await Promise.all([
        fetch(`/api/projects/${projectId}/subcontractor-assignments`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        }),
        fetch("/api/subcontractors", {
          headers: { Authorization: `Bearer ${accessToken}` },
        }),
      ]);
      const assignmentsData = await assignmentsResponse.json();
      if (!assignmentsResponse.ok) {
        setError(assignmentsData.detail ?? "Failed to load assignments");
        return;
      }
      setAssignments(assignmentsData.items ?? []);
      const subsData = await subsResponse.json();
      if (subsResponse.ok) setSubcontractors(subsData.items ?? []);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  const nameById = React.useMemo(
    () => Object.fromEntries(subcontractors.map((s) => [s.id, s.name])),
    [subcontractors]
  );

  async function handleAssign(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken || !selectedId) return;
    setSubmitting(true);
    setError(null);
    setPlanGate(null);
    try {
      const response = await fetch(`/api/projects/${projectId}/subcontractor-assignments`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          subcontractor_id: selectedId,
          override_reason: needsOverride && overrideReason ? overrideReason : null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          // Surface the backend's detail verbatim — the expired-compliance
          // rejection explains itself. For admins, also reveal the
          // override-reason field so they can retry with one; PMs can't
          // override at all, so they just see the block.
          setError(typeof data.detail === "string" ? data.detail : "Failed to assign subcontractor");
          if (isAdmin) setNeedsOverride(true);
        }
        return;
      }
      setSelectedId("");
      setOverrideReason("");
      setNeedsOverride(false);
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {planGate && <PlanUpgradeNotice detail={planGate} />}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {assignments.length === 0 && (
        <p className="text-sm text-slate-600">No subcontractors assigned to this project.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
        {assignments.map((assignment) => (
          <li key={assignment.id} className="flex items-center gap-4 px-4 py-3 text-sm">
            <Link
              href={`/subcontractors/${assignment.subcontractor_id}`}
              className="flex-1 font-medium hover:underline"
            >
              {nameById[assignment.subcontractor_id] ?? "Subcontractor"}
            </Link>
            {assignment.override_reason && (
              <span className="text-amber-700" title={assignment.override_reason}>
                compliance override
              </span>
            )}
            <span className="text-slate-500">assigned {formatDate(assignment.created_at)}</span>
          </li>
        ))}
      </ul>

      {canAssign && (
        <form onSubmit={handleAssign} className="flex flex-col gap-3 max-w-md">
          <div className="flex items-end gap-3">
            <div className="flex flex-col gap-1.5 flex-1">
              <Label htmlFor="assignSubcontractor">Assign a subcontractor</Label>
              <Select
                id="assignSubcontractor"
                value={selectedId}
                onChange={(e) => setSelectedId(e.target.value)}
                disabled={submitting}
              >
                <option value="">Select…</option>
                {subcontractors.map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
              </Select>
            </div>
            <Button type="submit" disabled={submitting || !selectedId}>
              {submitting ? "Assigning…" : "Assign"}
            </Button>
          </div>
          {needsOverride && isAdmin && (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="overrideReason">Override reason (admin only)</Label>
              <Textarea
                id="overrideReason"
                value={overrideReason}
                onChange={(e) => setOverrideReason(e.target.value)}
                disabled={submitting}
                placeholder="Why this assignment should proceed despite expired compliance"
              />
            </div>
          )}
        </form>
      )}
    </div>
  );
}
