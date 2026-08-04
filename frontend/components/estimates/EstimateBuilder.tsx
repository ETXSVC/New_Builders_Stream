"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { CatalogPanel } from "./CatalogPanel";
import { LineRows, DraftLine } from "./LineRows";
import { CustomLineForm, type CustomLineDraft } from "./CustomLineForm";
import { RateConflictNotice } from "./RateConflictNotice";
import { readSaveError, type RateConflict } from "@/lib/estimates/rate-conflicts";

interface ExistingLineItem {
  id: string;
  // null on a free-form line (migration 0035), where `description`/`unit`
  // carry what a catalog item would otherwise supply.
  cost_catalog_item_id: string | null;
  description: string | null;
  unit: string | null;
  quantity: string;
  unit_rate_snapshot: string;
}

interface CategorySubtotal {
  category: string;
  subtotal: string;
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
      key: li.id,
      cost_catalog_item_id: li.cost_catalog_item_id,
      // A CATALOGUED line's name and unit aren't in the persisted line-item
      // shape (only the snapshot rate is), so they resolve lazily as "—"
      // until the user re-adds via the catalog panel; re-deriving them would
      // need a catalog lookup by id, which this initial pass doesn't do.
      // Acceptable: quantity, rate and total are all still correct.
      //
      // A FREE-FORM line has no such gap — the estimator's own description
      // and unit are stored on the row, so they round-trip exactly.
      name: li.description ?? "—",
      unit: li.unit ?? "",
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
        // Catalogued rows only. A free-form row has no catalog id and can
        // never be in a rate conflict; saying so explicitly keeps the intent
        // readable rather than relying on `byId.has(null)` being false.
        l.cost_catalog_item_id !== null && byId.has(l.cost_catalog_item_id)
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
        {
          key: item.id,
          cost_catalog_item_id: item.id,
          name: item.name,
          unit: item.unit,
          unit_rate: item.unit_rate,
          quantity: "1",
        },
      ];
    });
  }

  /**
   * A line the catalog does not price. Unlike `handleAdd` there is no
   * de-duplication: two custom lines with the same wording are a legitimate
   * thing to want (two allowances, two permit fees), whereas adding the same
   * catalog item twice is always a mistake.
   */
  function handleAddCustom(draft: CustomLineDraft) {
    setLines((prev) => [
      ...prev,
      {
        key: crypto.randomUUID(),
        cost_catalog_item_id: null,
        name: draft.description,
        unit: draft.unit,
        unit_rate: draft.unit_rate,
        quantity: "1",
      },
    ]);
  }

  function handleQuantityChange(key: string, quantity: string) {
    setLines((prev) => prev.map((l) => (l.key === key ? { ...l, quantity } : l)));
  }

  function handleRateChange(key: string, unitRate: string) {
    // Free-form rows only — `LineRows` renders the editable rate for those
    // alone, so a catalogued line can never reach this.
    setLines((prev) => prev.map((l) => (l.key === key ? { ...l, unit_rate: unitRate } : l)));
  }

  function handleRemove(key: string) {
    setLines((prev) => prev.filter((l) => l.key !== key));
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
          //
          // A free-form line sends the other shape entirely: its own
          // description, unit and rate, and NO `expected_unit_rate` — there
          // is no catalog entry for it to be stale against, and the API
          // rejects the field on those lines rather than implying a check
          // that never ran.
          items: lines.map((l) =>
            l.cost_catalog_item_id === null
              ? {
                  description: l.name,
                  unit: l.unit,
                  quantity: l.quantity,
                  unit_rate: l.unit_rate,
                }
              : {
                  cost_catalog_item_id: l.cost_catalog_item_id,
                  quantity: l.quantity,
                  expected_unit_rate: l.unit_rate,
                }
          ),
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
      <div className="flex flex-col gap-4">
        <CatalogPanel onAdd={handleAdd} />
        <CustomLineForm onAdd={handleAddCustom} disabled={saving} />
      </div>
      <div className="flex flex-col gap-3">
        <LineRows
          lines={lines}
          onQuantityChange={handleQuantityChange}
          onRemove={handleRemove}
          onRateChange={handleRateChange}
        />
        {error && (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <RateConflictNotice
          conflicts={rateConflicts}
          onAdopt={handleAdoptNewRates}
          disabled={saving}
          note="Nothing has been saved. Using the new rates updates the lines below so you can review the totals before saving."
        />
        <Button type="button" onClick={handleSave} disabled={saving}>
          {saving ? "Saving…" : "Save & calculate"}
        </Button>
      </div>
    </div>
  );
}
