import { cn } from "@/lib/utils";
import { LEAD_PIPELINE, labelFor } from "@/lib/state-machines";

export function LeadStatusPipeline({ status }: { status: string }) {
  if (status === "lost") {
    return <p className="text-sm text-red-600 font-medium">This lead was marked lost.</p>;
  }
  const currentIndex = LEAD_PIPELINE.indexOf(status as (typeof LEAD_PIPELINE)[number]);
  return (
    <ol className="flex items-center gap-2 text-xs" aria-label="Lead pipeline">
      {LEAD_PIPELINE.map((stage, index) => (
        <li key={stage} className="flex items-center gap-2">
          {index > 0 && <span aria-hidden="true" className="text-slate-300">→</span>}
          <span
            className={cn(
              index === currentIndex ? "font-semibold text-blue-700" : "text-slate-400"
            )}
            aria-current={index === currentIndex ? "step" : undefined}
          >
            {labelFor(stage)}
          </span>
        </li>
      ))}
    </ol>
  );
}
