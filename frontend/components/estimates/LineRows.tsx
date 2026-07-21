"use client";

import { Input } from "@/components/ui/input";
import { formatCurrency } from "@/lib/format";

export interface DraftLine {
  cost_catalog_item_id: string;
  name: string;
  unit: string;
  unit_rate: string;
  quantity: string;
}

export function LineRows({
  lines,
  onQuantityChange,
  onRemove,
}: {
  lines: DraftLine[];
  onQuantityChange: (costCatalogItemId: string, quantity: string) => void;
  onRemove: (costCatalogItemId: string) => void;
}) {
  const subtotal = lines.reduce(
    (sum, line) => sum + Number(line.quantity || 0) * Number(line.unit_rate || 0),
    0
  );

  return (
    <div className="flex flex-col gap-2">
      {lines.length === 0 && <p className="text-sm text-slate-500">No line items yet — add some from the catalog.</p>}
      {lines.map((line) => (
        <div key={line.cost_catalog_item_id} className="flex items-center gap-2 text-sm">
          <span className="flex-1">{line.name}</span>
          <Input
            aria-label={`Quantity for ${line.name}`}
            type="number"
            min="0"
            step="any"
            className="w-24 h-8"
            value={line.quantity}
            onChange={(e) => onQuantityChange(line.cost_catalog_item_id, e.target.value)}
          />
          <span className="w-16 text-slate-500">{line.unit}</span>
          <span className="w-24 text-right">
            {formatCurrency(Number(line.quantity || 0) * Number(line.unit_rate || 0))}
          </span>
          <button
            type="button"
            onClick={() => onRemove(line.cost_catalog_item_id)}
            className="text-slate-400 hover:text-red-600"
            aria-label={`Remove ${line.name}`}
          >
            ✕
          </button>
        </div>
      ))}
      <div className="border-t border-slate-200 pt-2 flex justify-between text-sm font-medium">
        <span>Subtotal (before markup)</span>
        <span>{formatCurrency(subtotal)}</span>
      </div>
    </div>
  );
}
