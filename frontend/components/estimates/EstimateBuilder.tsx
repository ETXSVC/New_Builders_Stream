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
    setSaving(true);
    try {
      const linesResponse = await fetch(`/api/estimates/${estimateId}/lines`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          items: lines.map((l) => ({ cost_catalog_item_id: l.cost_catalog_item_id, quantity: l.quantity })),
        }),
      });
      const linesData = await linesResponse.json();
      if (!linesResponse.ok) {
        setError(linesData.detail ?? "Failed to save line items");
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
        <Button type="button" onClick={handleSave} disabled={saving}>
          {saving ? "Saving…" : "Save & calculate"}
        </Button>
      </div>
    </div>
  );
}
