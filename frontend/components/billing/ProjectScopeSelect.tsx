"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Select } from "@/components/ui/select";

interface ProjectOption {
  id: string;
  name: string;
}

// Invoices and Expenses are project-scoped in the backend (no global list
// endpoint), so their tabs need a project picker before a list can load.
export function ProjectScopeSelect({
  value,
  onChange,
}: {
  value: string;
  onChange: (projectId: string) => void;
}) {
  const { accessToken } = useAuth();
  const [projects, setProjects] = React.useState<ProjectOption[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!accessToken) return;
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch("/api/projects", {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (cancelled) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load projects");
          return;
        }
        setProjects(data.items ?? []);
      } catch {
        if (!cancelled) setError("Unable to reach the server.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [accessToken]);

  if (error) {
    return (
      <p role="alert" className="text-sm text-red-600">
        {error}
      </p>
    );
  }

  return (
    <Select
      aria-label="Select project"
      className="w-64"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">Select a project…</option>
      {projects.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </Select>
  );
}
