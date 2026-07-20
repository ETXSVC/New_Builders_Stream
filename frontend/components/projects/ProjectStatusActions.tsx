"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { PROJECT_TRANSITIONS, labelFor } from "@/lib/state-machines";

export function ProjectStatusActions({
  projectId,
  status,
  onChanged,
}: {
  projectId: string;
  status: string;
  onChanged: () => void;
}) {
  const { accessToken, role } = useAuth();
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canTransition = role === "admin" || role === "project_manager";
  const nextStatuses = PROJECT_TRANSITIONS[status] ?? [];
  if (!canTransition || nextStatuses.length === 0) return null;

  async function transition(next: string) {
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/status`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ status: next }),
      });
      const data = await response.json();
      if (!response.ok) {
        // Includes the backend's 409 for completion blocked by pending
        // change orders — surfaced verbatim (spec Decision 6).
        setError(data.detail ?? "Failed to change status");
        return;
      }
      onChanged();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        {nextStatuses.map((next) => (
          <Button key={next} variant="outline" size="sm" disabled={submitting} onClick={() => transition(next)}>
            Move to {labelFor(next).toLowerCase()}
          </Button>
        ))}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
    </div>
  );
}
