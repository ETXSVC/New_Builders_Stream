"use client";

import * as React from "react";
import { Button, type ButtonProps } from "@/components/ui/button";

/**
 * A destructive action that asks once, inline, before it fires.
 *
 * This app had two confirmation patterns. Billing's invoice/bill void
 * buttons swapped themselves for an inline "Void this invoice? [Yes,
 * void] [Cancel]" row; Phases/Tasks called `window.confirm`. Two patterns
 * for one interaction is a papercut on its own, but `window.confirm` is
 * the worse of the two for three concrete reasons:
 *
 *   * it blocks the main thread and renders outside React, so nothing
 *     about it can be styled, themed, or made consistent with the rest of
 *     the UI;
 *   * Playwright never sees it unless the spec registers a `page.on
 *     ("dialog")` handler first — and if it forgets, the dialog
 *     auto-dismisses and the test silently exercises the *cancel* path
 *     while looking like it tested the delete. That is a test that passes
 *     for the wrong reason, which is worse than no test;
 *   * it cannot carry a busy state, so a double-click during a slow
 *     DELETE fires the request twice.
 *
 * So the inline pattern won, and this component is it — extracted rather
 * than copied a fourth time.
 *
 * The confirm state is deliberately local to the button. Lifting it would
 * mean every caller declares a `confirmingX` boolean, which is exactly
 * the duplication the two billing pages already had.
 *
 * Accessibility: the prompt is `role="alert"` so a screen reader
 * announces the question when the button swaps, rather than leaving a
 * non-sighted user to discover that the control they just activated
 * changed into something else. Escape cancels, matching what a dialog
 * would do.
 */
export interface ConfirmButtonProps extends Omit<ButtonProps, "onClick"> {
  /** The question, e.g. "Delete this phase and all of its tasks?" */
  confirmMessage: string;
  /** Label on the affirmative button once armed. Defaults to "Confirm". */
  confirmLabel?: string;
  onConfirm: () => void;
}

export function ConfirmButton({
  confirmMessage,
  confirmLabel = "Confirm",
  onConfirm,
  children,
  disabled,
  ...props
}: ConfirmButtonProps) {
  const [armed, setArmed] = React.useState(false);

  // No effect resetting `armed` when `disabled` flips. If a sibling
  // request starts mid-confirm the prompt stays up with its affirmative
  // button disabled and Cancel still live, which is the honest state —
  // tearing the question down under the user because something else
  // became busy is worse, and an effect that calls setState on a prop
  // change is a cascading render the lint rule rightly objects to.
  if (!armed) {
    return (
      <Button {...props} disabled={disabled} onClick={() => setArmed(true)}>
        {children}
      </Button>
    );
  }

  return (
    <span
      role="alert"
      className="inline-flex items-center gap-2 text-sm"
      onKeyDown={(e) => {
        if (e.key === "Escape") setArmed(false);
      }}
    >
      {confirmMessage}
      <Button
        variant="outline"
        size="sm"
        disabled={disabled}
        onClick={() => {
          // Close first: the caller's handler is async and usually
          // triggers a reload, and leaving the prompt armed through it
          // invites a second click on the same row.
          setArmed(false);
          onConfirm();
        }}
      >
        {confirmLabel}
      </Button>
      <Button variant="ghost" size="sm" onClick={() => setArmed(false)}>
        Cancel
      </Button>
    </span>
  );
}
