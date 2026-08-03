"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { CatalogPanel } from "./CatalogPanel";
import { LineRows, DraftLine } from "./LineRows";

interface ExistingLineItem {
  cost_catalog_item_id: string;
  quantity: string;
  unit_rate_snapshot: string;
}

interface CategorySubtotal {
  category: string;
  subtotal: string;
}

/** One entry from the 409 `detail.rate_conflicts` the lines route returns. */
interface RateConflict {
  cost_catalog_item_id: string;
  name: string;
  expected_unit_rate: string;
  current_unit_rate: string;
}

/**
 * `detail` is a plain string for most failures and a dict for the
 * rate-conflict 409 — the route needs to hand back the current rates so
 * adopting them costs no extra round trip. Rendering the dict as text would
 * put "[object Object]" in the banner, so both shapes are handled here
 * rather than at the call site.
 */
function readSaveError(detail: unknown): { message: string; conflicts: RateConflict[] } {
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

export function EstimateBuilder({
  estimateId,
  initialLines,
  onSaved,
}: {
  estimateId: string;
  initialLines: ExistingLineItem[];
  onSaved: (total: string, breakdown: CategorySubtotal[]) => void;
}) {
  const { accessToken } = useAuth();
  const [lines, setLines] = React.useState<DraftLine[]>(
    initialLines.map((li) => ({
      cost_catalog_item_id: li.cost_catalog_item_id,
      // Name/unit aren't in the persisted line item shape (only the
      // snapshot rate is) — resolved lazily as "—" until the user re-adds
      // via the catalog panel, or left blank; a full re-hydration would
      // need a catalog lookup by id, which the initial builder pass
      // doesn't do. Acceptable: a draft estimate that already has lines
      // still shows quantity/rate/total correctly, just without a
      // re-derived name label. If this reads poorly in practice during
      // manual verification, extend this constructor to look up names
      // from a fetched catalog map before setting initial state.
      name: "—",
      unit: "",
      unit_rate: li.unit_rate_snapshot,
      quantity: li.quantity,
    }))
  );
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [rateConflicts, setRateConflicts] = React.useState<RateConflict[]>([]);

  /**
   * Adopt the catalog's current rates into the draft — and stop there.
   *
   * Deliberately does NOT re-save. Auto-accepting would be exactly the
   * silent re-pricing the server-side guard exists to prevent, just moved
   * into the client: the estimator may have quoted the old number to a
   * customer, and they have to see the new one and choose. So this updates
   * the rows, clears the banner, and leaves them looking at a changed
   * estimate with the save button still to press.
   */
  function handleAdoptNewRates() {
    const byId = new Map(rateConflicts.map((c) => [c.cost_catalog_item_id, c.current_unit_rate]));
    setLines((prev) =>
      prev.map((l) =>
        byId.has(l.cost_catalog_item_id)
          ? { ...l, unit_rate: byId.get(l.cost_catalog_item_id) as string }
          : l
      )
    );
    setRateConflicts([]);
    setError(null);
  }

  function handleAdd(item: { id: string; name: string; unit: string; unit_rate: string }) {
    setLines((prev) => {
      if (prev.some((l) => l.cost_catalog_item_id === item.id)) return prev;
      return [
        ...prev,
        { cost_catalog_item_id: item.id, name: item.name, unit: item.unit, unit_rate: item.unit_rate, quantity: "1" },
      ];
    });
  }

  function handleQuantityChange(id: string, quantity: string) {
    setLines((prev) => prev.map((l) => (l.cost_catalog_item_id === id ? { ...l, quantity } : l)));
  }

  function handleRemove(id: string) {
    setLines((prev) => prev.filter((l) => l.cost_catalog_item_id !== id));
  }

  async function handleSave() {
    if (saving || !accessToken) return;
    setError(null);
    setRateConflicts([]);
    setSaving(true);
    try {
      const linesResponse = await fetch(`/api/estimates/${estimateId}/lines`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          // `expected_unit_rate` is the rate THIS screen showed while the
          // estimate was being built — `l.unit_rate`, set when the line was
          // added from the catalog panel or loaded from an existing line's
          // snapshot. The server refuses the save with a 409 if the catalog
          // moved underneath, rather than silently re-pricing every line to
          // whatever the rate is at save-time. The error lands in the same
          // banner as any other save failure, naming both rates.
          items: lines.map((l) => ({
            cost_catalog_item_id: l.cost_catalog_item_id,
            quantity: l.quantity,
            expected_unit_rate: l.unit_rate,
          })),
        }),
      });
      const linesData = await linesResponse.json();
      if (!linesResponse.ok) {
        const { message, conflicts } = readSaveError(linesData.detail);
        setError(message);
        setRateConflicts(conflicts);
        return;
      }

      const calcResponse = await fetch(`/api/estimates/${estimateId}/calculate`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const calcData = await calcResponse.json();
      if (!calcResponse.ok) {
        setError(calcData.detail ?? "Failed to calculate estimate");
        return;
      }
      onSaved(calcData.total, calcData.category_breakdown);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
      <CatalogPanel onAdd={handleAdd} />
      <div className="flex flex-col gap-3">
        <LineRows lines={lines} onQuantityChange={handleQuantityChange} onRemove={handleRemove} />
        {error && (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        )}
        {rateConflicts.length > 0 && (
          <div className="flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 p-3">
            <ul className="flex flex-col gap-1 text-sm">
              {rateConflicts.map((conflict) => (
                <li key={conflict.cost_catalog_item_id} className="flex items-center gap-2">
                  <span className="flex-1">{conflict.name}</span>
                  <span className="text-slate-500 line-through">
                    {conflict.expected_unit_rate}
                  </span>
                  <span aria-hidden="true">→</span>
                  <span className="font-medium">{conflict.current_unit_rate}</span>
                </li>
              ))}
            </ul>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="self-start"
              onClick={handleAdoptNewRates}
              disabled={saving}
            >
              Use new rates
            </Button>
            <p className="text-xs text-slate-600">
              Nothing has been saved. Using the new rates updates the lines below so you can
              review the totals before saving.
            </p>
          </div>
        )}
        <Button type="button" onClick={handleSave} disabled={saving}>
          {saving ? "Saving…" : "Save & calculate"}
        </Button>
      </div>
    </div>
  );
}
