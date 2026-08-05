"use client";

import { Input } from "@/components/ui/input";
import { formatCurrency } from "@/lib/format";

/**
 * One line being drafted, in either of the two shapes the API accepts
 * (migration 0035).
 *
 * `cost_catalog_item_id === null` means **free-form**: a line the estimator
 * wrote for work the catalog does not price, carrying their own description,
 * unit and rate. Anything else is catalogued, and its rate is the catalog's
 * to state — which is why only free-form rows below render an editable rate.
 *
 * `key` exists because `cost_catalog_item_id` used to serve as the row's
 * identity, and cannot any more: every free-form line has `null` there, so
 * two of them would collide as React keys and as the handle passed to
 * `onRemove`. For a catalogued row it is just the catalog item's id.
 */
export interface DraftLine {
  key: string;
  cost_catalog_item_id: string | null;
  name: string;
  unit: string;
  unit_rate: string;
  quantity: string;
}

export function LineRows({
  lines,
  onQuantityChange,
  onRemove,
  onRateChange,
  showSubtotal = true,
}: {
  lines: DraftLine[];
  onQuantityChange: (key: string, quantity: string) => void;
  onRemove: (key: string) => void;
  /**
   * Free-form rows only. Omitted by callers that cannot create them (the
   * site survey screen), which is also why a catalogued row never shows an
   * editable rate even when this is supplied.
   */
  onRateChange?: (key: string, unitRate: string) => void;
  /**
   * False on the site survey screen, and that is a product rule rather than
   * a layout preference: a survey records what was measured, and the priced
   * quote is produced by the server. A running total computed here would be
   * a price offered by a screen that is not allowed to produce one — and it
   * is arithmetic done in JS `Number`, so it could disagree with the
   * server's `Decimal` figure by a cent anyway.
   */
  showSubtotal?: boolean;
}) {
  const subtotal = showSubtotal
    ? lines.reduce(
        (sum, line) => sum + Number(line.quantity || 0) * Number(line.unit_rate || 0),
        0
      )
    : 0;

  return (
    <div className="flex flex-col gap-2">
      {lines.length === 0 && <p className="text-sm text-slate-500">No line items yet — add some from the catalog.</p>}
      {lines.map((line) => {
        const isFreeForm = line.cost_catalog_item_id === null;
        return (
          <div key={line.key} className="flex items-center gap-2 text-sm">
            <span className="flex-1">
              {line.name}
              {isFreeForm && (
                <span className="ml-2 text-xs text-slate-500 rounded-full bg-slate-100 px-2 py-0.5">
                  custom
                </span>
              )}
            </span>
            <Input
              aria-label={`Quantity for ${line.name}`}
              type="number"
              min="0"
              step="any"
              className="w-24 h-8"
              value={line.quantity}
              onChange={(e) => onQuantityChange(line.key, e.target.value)}
            />
            <span className="w-16 text-slate-500">{line.unit}</span>
            {isFreeForm && onRateChange ? (
              // Editable, because on a free-form line the rate IS the
              // estimator's number — a typo in it should not mean deleting
              // the row and writing it again.
              <Input
                aria-label={`Unit rate for ${line.name}`}
                type="number"
                min="0"
                step="any"
                className="w-24 h-8"
                value={line.unit_rate}
                onChange={(e) => onRateChange(line.key, e.target.value)}
              />
            ) : (
              <span className="w-24 text-right text-slate-500">
                {formatCurrency(line.unit_rate, { precise: true })}
              </span>
            )}
            <span className="w-24 text-right">
              {formatCurrency(Number(line.quantity || 0) * Number(line.unit_rate || 0))}
            </span>
            <button
              type="button"
              onClick={() => onRemove(line.key)}
              className="text-slate-400 hover:text-red-600"
              aria-label={`Remove ${line.name}`}
            >
              ✕
            </button>
          </div>
        );
      })}
      {showSubtotal && (
        <div className="border-t border-slate-200 pt-2 flex justify-between text-sm font-medium">
          <span>Subtotal (before markup)</span>
          <span>{formatCurrency(subtotal)}</span>
        </div>
      )}
    </div>
  );
}
