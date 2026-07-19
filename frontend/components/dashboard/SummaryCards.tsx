"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";

interface Summary {
  open_leads: number;
  active_projects: number;
  tasks_due_this_week: number;
}

export function SummaryCards() {
  const { accessToken } = useAuth();
  const [summary, setSummary] = React.useState<Summary | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!accessToken) return;
    fetch("/api/dashboard/summary", { headers: { Authorization: `Bearer ${accessToken}` } })
      .then(async (r) => {
        const data = await r.json();
        if (!r.ok) {
          setError(data.detail ?? "Failed to load summary");
          return;
        }
        setSummary(data);
      })
      .catch(() => setError("Unable to reach the server. Check your connection and try again."));
  }, [accessToken]);

  if (error) {
    return (
      <p role="alert" className="text-sm text-red-600">
        {error}
      </p>
    );
  }

  const cards = [
    { label: "Open leads", value: summary?.open_leads },
    { label: "Active projects", value: summary?.active_projects },
    { label: "Tasks due this week", value: summary?.tasks_due_this_week },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 max-w-2xl">
      {cards.map((card) => (
        <div key={card.label} className="rounded-lg bg-slate-50 p-4">
          <p className="text-sm text-slate-600">{card.label}</p>
          <p className="text-2xl font-medium">{card.value ?? "—"}</p>
        </div>
      ))}
    </div>
  );
}
