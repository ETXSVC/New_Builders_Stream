/**
 * The shape of a refused save, in one place.
 *
 * `PUT /estimates/{id}/lines` answers a moved catalog rate with a 409 whose
 * `detail` is a DICT — `{message, rate_conflicts}` — carrying the current
 * rate per conflicting line, so the caller can show a human old → new
 * without a second round trip. Every other failure from that route answers
 * with `detail` as a plain string.
 *
 * Two callers now read that response: the online builder
 * (`components/estimates/EstimateBuilder.tsx`) and the offline flush
 * (`lib/offline/flush.ts`). Rendering the dict as text puts
 * "[object Object]" in the banner, and each caller getting that right
 * separately is how the two would drift.
 */

export interface RateConflict {
  cost_catalog_item_id: string;
  name: string;
  /** What the estimator saw — and, offline, may have quoted on site. */
  expected_unit_rate: string;
  /** What the catalog says now. */
  current_unit_rate: string;
}

export interface SaveError {
  message: string;
  /** Empty for every failure that is not a rate conflict. */
  conflicts: RateConflict[];
}

export function readSaveError(detail: unknown): SaveError {
  if (detail && typeof detail === "object" && "rate_conflicts" in detail) {
    const structured = detail as { message?: string; rate_conflicts?: RateConflict[] };
    return {
      message: structured.message ?? "Catalog rates changed since this estimate was built.",
      conflicts: structured.rate_conflicts ?? [],
    };
  }
  return {
    message: typeof detail === "string" ? detail : "Failed to save line items",
    conflicts: [],
  };
}
