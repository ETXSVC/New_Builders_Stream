import { cn } from "@/lib/utils";
import { labelFor } from "@/lib/state-machines";

const TONE_CLASSES: Record<string, string> = {
  green: "bg-green-50 text-green-700",
  blue: "bg-blue-50 text-blue-700",
  amber: "bg-amber-50 text-amber-700",
  red: "bg-red-50 text-red-700",
  slate: "bg-slate-100 text-slate-600",
};

const STATUS_TONES: Record<string, keyof typeof TONE_CLASSES> = {
  new: "blue",
  contacted: "blue",
  estimating: "amber",
  qualified: "amber",
  won: "green",
  lost: "red",
  draft: "slate",
  pre_construction: "amber",
  active: "green",
  suspended: "red",
  completed: "blue",
  archived: "slate",
  open: "slate",
  in_progress: "amber",
  done: "green",
  needed: "slate",
  ordered: "amber",
  partially_received: "blue",
  received: "green",
};

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={cn(
        "inline-block rounded-full px-2.5 py-0.5 text-xs font-medium",
        TONE_CLASSES[STATUS_TONES[status] ?? "slate"]
      )}
    >
      {labelFor(status)}
    </span>
  );
}
