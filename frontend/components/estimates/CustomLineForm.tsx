"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export interface CustomLineDraft {
  description: string;
  unit: string;
  unit_rate: string;
}

/**
 * Adding a line the cost catalog does not price.
 *
 * Site cleanup, a permit fee, a one-off allowance — the last line of most
 * real estimates, and until migration 0035 there was nowhere to put it. The
 * field workaround was a catalog item called "Miscellaneous" at $1.00 with
 * the dollar amount typed into the quantity box, which then printed
 * "400 × $1.00" on the document a customer signs.
 *
 * The rate here is the estimator's own, and that is the only place in this
 * application where a price a caller typed is stored as-is. It is safe
 * precisely because there is no catalog item behind the line for it to
 * contradict — a catalogued line's rate still comes from the catalog and the
 * API refuses to let a caller override it.
 */
export function CustomLineForm({
  onAdd,
  disabled = false,
}: {
  onAdd: (draft: CustomLineDraft) => void;
  disabled?: boolean;
}) {
  const [description, setDescription] = React.useState("");
  const [unit, setUnit] = React.useState("lot");
  const [unitRate, setUnitRate] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  function handleAdd() {
    if (!description.trim() || !unit.trim() || unitRate.trim() === "") {
      // All three or none: a description with no rate is half a line, and
      // the alternative to saying so is inventing a price for a customer's
      // estimate. The API refuses it too — this just says so sooner.
      setError("A custom line needs a description, a unit and a rate.");
      return;
    }
    setError(null);
    onAdd({ description: description.trim(), unit: unit.trim(), unit_rate: unitRate });
    setDescription("");
    setUnit("lot");
    setUnitRate("");
  }

  return (
    <div className="flex flex-col gap-2 rounded-md border border-slate-200 p-3">
      <p className="text-sm font-medium">Custom line</p>
      <p className="text-xs text-slate-600">
        For work your catalog does not price. You set the rate.
      </p>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        <div className="flex flex-col gap-1 sm:col-span-3">
          <Label htmlFor="custom-line-description">Description</Label>
          <Input
            id="custom-line-description"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={255}
            placeholder="Site cleanup and haul-off"
            disabled={disabled}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="custom-line-unit">Unit</Label>
          <Input
            id="custom-line-unit"
            value={unit}
            onChange={(e) => setUnit(e.target.value)}
            maxLength={50}
            placeholder="lot"
            disabled={disabled}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="custom-line-rate">Rate</Label>
          <Input
            id="custom-line-rate"
            type="number"
            min="0"
            step="any"
            value={unitRate}
            onChange={(e) => setUnitRate(e.target.value)}
            placeholder="400.00"
            disabled={disabled}
          />
        </div>
        <div className="flex items-end">
          <Button type="button" variant="outline" onClick={handleAdd} disabled={disabled}>
            Add line
          </Button>
        </div>
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
    </div>
  );
}
