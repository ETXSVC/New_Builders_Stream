"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export interface LoggableProject {
  id: string;
  name: string;
}

export interface DailyLogDraft {
  project_id: string;
  project_name: string;
  log_date: string;
  weather: string | null;
  notes: string | null;
}

/**
 * A daily log, written from the crew member's own task list.
 *
 * The project options come from their assigned tasks, which is not a
 * shortcut around the backend's scope — it IS the scope:
 * `get_project_or_404` admits a field-crew caller only to a project they
 * hold an assigned task on, so anything wider would be offered here and
 * refused hours later, on a device with no way to fix it.
 *
 * Queued rather than posted: the form's job ends at handing back a draft.
 * Whether that reaches the server now or at the end of the day is the
 * queue's business, and this component is identical either way.
 */
export function DailyLogForm({
  projects,
  onSubmit,
  disabled = false,
}: {
  projects: LoggableProject[];
  onSubmit: (draft: DailyLogDraft) => void;
  disabled?: boolean;
}) {
  const [projectId, setProjectId] = React.useState("");
  // Today, in the browser's own timezone — `new Date().toISOString()` would
  // be UTC, which writes yesterday's date for a crew starting early on the
  // American west coast.
  const [logDate, setLogDate] = React.useState(() => {
    const now = new Date();
    const month = String(now.getMonth() + 1).padStart(2, "0");
    const day = String(now.getDate()).padStart(2, "0");
    return `${now.getFullYear()}-${month}-${day}`;
  });
  const [weather, setWeather] = React.useState("");
  const [notes, setNotes] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const project = projects.find((candidate) => candidate.id === projectId);
    if (!project || !logDate) {
      setError("Pick a project and a date.");
      return;
    }
    setError(null);
    onSubmit({
      project_id: project.id,
      project_name: project.name,
      log_date: logDate,
      weather: weather.trim() || null,
      notes: notes.trim() || null,
    });
    setWeather("");
    setNotes("");
  }

  if (projects.length === 0) return null;

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="daily-log-project">Project</Label>
          <Select
            id="daily-log-project"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            disabled={disabled}
          >
            <option value="">Select…</option>
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="daily-log-date">Date</Label>
          <Input
            id="daily-log-date"
            type="date"
            value={logDate}
            onChange={(e) => setLogDate(e.target.value)}
            disabled={disabled}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="daily-log-weather">Weather</Label>
        <Input
          id="daily-log-weather"
          value={weather}
          onChange={(e) => setWeather(e.target.value)}
          maxLength={100}
          placeholder="Sunny, 75F"
          disabled={disabled}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="daily-log-notes">Notes</Label>
        <Textarea
          id="daily-log-notes"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          rows={3}
          placeholder="What happened on site today"
          disabled={disabled}
        />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <Button type="submit" className="self-start" disabled={disabled}>
        Save daily log
      </Button>
    </form>
  );
}
