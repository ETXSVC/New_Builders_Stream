"use client";

import { Button } from "@/components/ui/button";
import { formatCurrency } from "@/lib/format";
import type { RateConflict } from "@/lib/estimates/rate-conflicts";

/**
 * What a refused save looks like, and the one way to recover from it.
 *
 * Every conflicting line at once, each with what the estimator saw and what
 * the catalog says now, and a button that adopts the new rates into the
 * draft — **without saving**. Auto-accepting would be exactly the silent
 * re-pricing the server-side guard exists to prevent, moved into the client:
 * the estimator may have quoted the old number to a customer, and they have
 * to see the new one and choose.
 *
 * Shared by the online builder and the offline flush deliberately. The two
 * arrive here differently — one from a save the estimator just pressed, the
 * other from a background flush of work captured days ago — but the remedy
 * is identical, and two copies of it would drift. The drift would be silent
 * re-pricing appearing in whichever copy lost the argument.
 */
export function RateConflictNotice({
  conflicts,
  onAdopt,
  disabled = false,
  note,
}: {
  conflicts: RateConflict[];
  onAdopt: () => void;
  disabled?: boolean;
  /** What happens next, which differs between the builder and a parked draft. */
  note: string;
}) {
  if (conflicts.length === 0) return null;

  return (
    <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 p-3">
      <ul className="flex flex-col gap-1 text-sm">
        {conflicts.map((conflict) => (
          <li key={conflict.cost_catalog_item_id} className="flex items-center gap-2">
            <span className="flex-1">{conflict.name}</span>
            {/* `precise` on both: whole-dollar rounding is the default
                because list screens read better without a column of ".00",
                and it is exactly wrong here — a conflict between 4.00 and
                4.05 would render as "$4 → $4". */}
            <span className="text-slate-500 line-through">
              {formatCurrency(conflict.expected_unit_rate, { precise: true })}
            </span>
            <span aria-hidden="true">→</span>
            <span className="font-medium">
              {formatCurrency(conflict.current_unit_rate, { precise: true })}
            </span>
          </li>
        ))}
      </ul>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="self-start"
        onClick={onAdopt}
        disabled={disabled}
      >
        Use new rates
      </Button>
      <p className="text-xs text-slate-600">{note}</p>
    </div>
  );
}
