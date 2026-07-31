"use client";

import * as React from "react";
import type { components } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type PhoneEntry = components["schemas"]["PhoneEntry"];

/**
 * The phone list, shared by the admin's view of somebody's record and a
 * member's view of their own.
 *
 * Extracted rather than copied: the API has no per-phone id, so "the list
 * you send IS the list", and both callers have to get the same three things
 * right — replace-not-append, blank rows dropped on save, and removal by
 * position. Two copies of that is one copy that drifts.
 *
 * Rows are keyed by index because there is nothing else to key them by, and
 * that is safe here for one reason worth stating: the list is only ever
 * edited through these controls, never reordered or patched from the
 * server mid-edit.
 */
export function PhonesEditor({
  phones,
  onChange,
  disabled = false,
  idPrefix = "phone",
}: {
  phones: PhoneEntry[];
  onChange: (next: PhoneEntry[]) => void;
  disabled?: boolean;
  /** Keeps input ids unique if two of these ever share a page. */
  idPrefix?: string;
}) {
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="text-sm font-medium">Phone numbers</legend>
      {phones.map((phone, index) => (
        <div key={index} className="flex items-end gap-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${idPrefix}-label-${index}`}>Label</Label>
            <Input
              id={`${idPrefix}-label-${index}`}
              value={phone.label ?? ""}
              maxLength={50}
              placeholder="mobile"
              disabled={disabled}
              className="w-40"
              onChange={(event) =>
                onChange(
                  phones.map((entry, i) =>
                    i === index ? { ...entry, label: event.target.value } : entry
                  )
                )
              }
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${idPrefix}-number-${index}`}>Number</Label>
            <Input
              id={`${idPrefix}-number-${index}`}
              value={phone.number}
              maxLength={40}
              disabled={disabled}
              className="w-56"
              onChange={(event) =>
                onChange(
                  phones.map((entry, i) =>
                    i === index ? { ...entry, number: event.target.value } : entry
                  )
                )
              }
            />
          </div>
          {!disabled && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label={`Remove phone ${index + 1}`}
              onClick={() => onChange(phones.filter((_, i) => i !== index))}
            >
              Remove
            </Button>
          )}
        </div>
      ))}
      {phones.length === 0 && <p className="text-sm text-slate-600">No phone numbers.</p>}
      {!disabled && (
        <div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => onChange([...phones, { label: "", number: "" }])}
          >
            Add a phone
          </Button>
        </div>
      )}
    </fieldset>
  );
}

/**
 * Drop the rows nobody filled in.
 *
 * Somebody who clicks "Add a phone" and changes their mind leaves an empty
 * row behind; sending it would store a blank number.
 */
export function filledPhones(phones: PhoneEntry[]): PhoneEntry[] {
  return phones.filter((phone) => phone.number.trim() !== "");
}
