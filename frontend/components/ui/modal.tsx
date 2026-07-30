"use client";

import * as React from "react";

/**
 * A modal dialog, built on the native `<dialog>` element.
 *
 * Native rather than a positioned `<div>`, and the reason is not
 * minimalism — it is that `showModal()` gives four things correctly which a
 * hand-rolled modal has to reimplement and usually gets partly wrong:
 *
 *   * **Focus is trapped** by the browser, including the cases people
 *     forget: Shift+Tab off the first element, and focusable content that
 *     appears while the dialog is open.
 *   * **Escape closes it**, firing `cancel` — no keydown listener on
 *     `document` that has to be added and removed in step with mounting.
 *   * **The rest of the page is inert**, so a screen reader cannot wander
 *     into content behind the overlay and a click cannot reach it.
 *   * **It renders in the top layer**, which ends z-index arguments
 *     permanently. This matters here: the console's tables and sticky
 *     header would otherwise need a stacking context nobody maintains.
 *
 * What it does NOT do for free is close on a backdrop click, because the
 * backdrop is a pseudo-element rather than a node. The click handler below
 * is the standard trick: a click whose target is the dialog ITSELF landed
 * outside the content box, since every real child is nested deeper.
 *
 * `onClose` is wired to the element's own `close` event rather than called
 * from each dismissal path. Escape, the backdrop, and the close button all
 * end in `close`, so one listener cannot disagree with another about
 * whether the dialog is shut.
 */
export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  /** Optional supporting line under the title. */
  description?: string;
  children: React.ReactNode;
  /** Footer actions, e.g. Cancel + Save. */
  footer?: React.ReactNode;
}

export function Modal({ open, onClose, title, description, children, footer }: ModalProps) {
  const ref = React.useRef<HTMLDialogElement>(null);
  const headingId = React.useId();
  const descriptionId = React.useId();

  React.useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    // Guarded both ways: showModal() on an already-open dialog throws
    // InvalidStateError, and close() on a closed one silently fires another
    // `close` event, which would loop back through onClose.
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={headingId}
      aria-describedby={description ? descriptionId : undefined}
      onClose={onClose}
      onClick={(e) => {
        // Target is the dialog itself only when the click missed the
        // content box — i.e. it landed on the backdrop.
        if (e.target === ref.current) onClose();
      }}
      className="m-auto w-[min(32rem,calc(100vw-2rem))] rounded-lg border border-slate-200 bg-white p-0 text-slate-900 shadow-xl backdrop:bg-slate-900/40"
    >
      <div className="flex flex-col gap-4 p-6">
        <div className="flex items-start justify-between gap-4">
          <div className="flex flex-col gap-1">
            <h2 id={headingId} className="text-lg font-semibold">
              {title}
            </h2>
            {description && (
              <p id={descriptionId} className="text-sm text-slate-500">
                {description}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="-mr-1 -mt-1 rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
          >
            ✕
          </button>
        </div>

        {children}

        {footer && <div className="flex justify-end gap-2 pt-2">{footer}</div>}
      </div>
    </dialog>
  );
}
