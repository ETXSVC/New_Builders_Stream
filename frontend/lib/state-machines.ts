// Mirrors of the backend's transition tables, used ONLY to decide which
// action buttons to render (spec Decision 6). The backend's transition
// validation is the sole enforcement; a 409 from it is surfaced verbatim.

export const LEAD_STATUSES = ["new", "contacted", "estimating", "qualified", "won", "lost"] as const;

export const LEAD_TRANSITIONS: Record<string, string[]> = {
  new: ["contacted", "lost"],
  contacted: ["estimating", "lost"],
  estimating: ["qualified", "lost"],
  qualified: ["won", "lost"],
  won: [],
  lost: [],
};

// The linear "pipeline" path shown in the breadcrumb (lost is an exit, not
// a pipeline stage).
export const LEAD_PIPELINE = ["new", "contacted", "estimating", "qualified", "won"] as const;

export const PROJECT_TRANSITIONS: Record<string, string[]> = {
  draft: ["pre_construction"],
  pre_construction: ["active"],
  active: ["suspended", "completed"],
  suspended: ["active", "completed"],
  completed: ["archived"],
  archived: [],
};

export const TASK_STATUSES = ["open", "in_progress", "done"] as const;

export const STATUS_LABELS: Record<string, string> = {
  new: "New",
  contacted: "Contacted",
  estimating: "Estimating",
  qualified: "Qualified",
  won: "Won",
  lost: "Lost",
  draft: "Draft",
  pre_construction: "Pre-construction",
  active: "Active",
  suspended: "Suspended",
  completed: "Completed",
  archived: "Archived",
  open: "Open",
  in_progress: "In progress",
  done: "Done",
  needed: "Needed",
  ordered: "Ordered",
  partially_received: "Partially received",
  received: "Received",
};

export function labelFor(status: string): string {
  return STATUS_LABELS[status] ?? status;
}
