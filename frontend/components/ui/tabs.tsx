"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * The ARIA tabs pattern, once, instead of three near-identical copies.
 *
 * Billing, Catalog and Project detail each hand-rolled the same markup:
 * a `role="tablist"` div of `role="tab"` buttons carrying `aria-selected`,
 * followed by `{tab === "X" && <XTab />}`. That gets the *labelling* right
 * and the *interaction* wrong, in two ways that matter to anyone not using
 * a mouse:
 *
 *   * **No `aria-controls` / no `role="tabpanel"`.** A screen reader
 *     announced "tab, selected" for a control with no associated panel —
 *     there was nothing to tell the user what the tab governs, and no way
 *     to jump to it. The content below was, as far as the accessibility
 *     tree was concerned, unrelated to the tabs above it.
 *   * **No arrow-key handling.** WAI-ARIA's tabs pattern expects Left/
 *     Right (and Home/End) to move between tabs while Tab moves *out* of
 *     the tablist to the panel. Plain buttons instead put every tab in the
 *     tab order, so a keyboard user had to Tab through every tab to reach
 *     the content — the exact thing the pattern exists to avoid.
 *
 * This implements both, plus the roving tabindex the pattern requires
 * (only the selected tab is tabbable; the rest are `tabIndex={-1}` and
 * reachable by arrow key). Activation is automatic — selecting a tab with
 * an arrow key shows its panel immediately — which is the correct choice
 * for panels that are cheap and already mounted client-side.
 *
 * `idPrefix` must be unique per tablist on a page; it is what wires
 * `aria-controls` to `aria-labelledby` in both directions.
 *
 * Only the active panel is rendered, matching what the three call sites
 * did before. Elements for inactive panels are constructed but never
 * mounted, which costs nothing — React elements are plain objects.
 */
export interface TabsProps<T extends string> {
  tabs: readonly T[];
  value: T;
  onChange: (tab: T) => void;
  /** Unique per tablist on the page — used to build the ARIA id pairs. */
  idPrefix: string;
  /** Content per tab. A tab with no entry renders an empty panel. */
  panels: Partial<Record<T, React.ReactNode>>;
  className?: string;
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  idPrefix,
  panels,
  className,
}: TabsProps<T>) {
  const tabRefs = React.useRef<Record<string, HTMLButtonElement | null>>({});

  const tabId = (tab: T) => `${idPrefix}-tab-${slug(tab)}`;
  const panelId = (tab: T) => `${idPrefix}-panel-${slug(tab)}`;

  function select(next: T) {
    onChange(next);
    // Follow focus, or the arrow key would change the panel while leaving
    // focus on the previously-selected tab — and the next arrow press
    // would then move relative to the wrong one.
    tabRefs.current[next]?.focus();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    const i = tabs.indexOf(value);
    if (i < 0) return;
    // Wrapping at both ends, per the pattern: from the last tab, Right
    // returns to the first.
    if (e.key === "ArrowRight") {
      e.preventDefault();
      select(tabs[(i + 1) % tabs.length]);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      select(tabs[(i - 1 + tabs.length) % tabs.length]);
    } else if (e.key === "Home") {
      e.preventDefault();
      select(tabs[0]);
    } else if (e.key === "End") {
      e.preventDefault();
      select(tabs[tabs.length - 1]);
    }
  }

  return (
    <>
      <div
        className={cn("flex gap-1 border-b border-slate-200", className)}
        role="tablist"
        onKeyDown={onKeyDown}
      >
        {tabs.map((t) => (
          <button
            key={t}
            type="button"
            id={tabId(t)}
            role="tab"
            aria-selected={value === t}
            aria-controls={panelId(t)}
            // Roving tabindex: Tab enters the tablist at the selected tab
            // and leaves it on the next press, rather than stepping
            // through every tab.
            tabIndex={value === t ? 0 : -1}
            ref={(el) => {
              tabRefs.current[t] = el;
            }}
            onClick={() => onChange(t)}
            className={cn(
              "px-3 py-2 text-sm",
              value === t
                ? "border-b-2 border-blue-600 font-medium text-slate-900"
                : "text-slate-600 hover:text-slate-900"
            )}
          >
            {t}
          </button>
        ))}
      </div>
      <div
        id={panelId(value)}
        role="tabpanel"
        aria-labelledby={tabId(value)}
        // Panels containing no focusable element are unreachable by
        // keyboard without this; 0 would put every panel in the tab order
        // even when its content is already focusable, so the pattern
        // specifies -1 plus programmatic focus.
        tabIndex={-1}
      >
        {panels[value]}
      </div>
    </>
  );
}

function slug(tab: string) {
  return tab.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}
