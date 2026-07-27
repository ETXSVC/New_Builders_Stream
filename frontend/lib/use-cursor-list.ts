"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";

/**
 * One cursor-paginated list loader, instead of seven copies of it.
 *
 * Every list surface in this app walks the backend's opaque cursor
 * pagination (`app/core/pagination.py`) the same way: fetch a page, guard
 * against a superseded response, replace-or-append, remember
 * `next_cursor`, surface an error. Six components had that written out by
 * hand — `estimates`, `leads`, `subcontractors`, `BillList`,
 * `ExpensePanel`, `InvoiceList` — byte-identical apart from the endpoint,
 * the entity noun in the error message, and which setter they call.
 *
 * `app/(app)/integrations/page.tsx` looks like a seventh and is
 * deliberately NOT converted. Its response is not this shape: the envelope
 * is `{connected_at, records, next_cursor}` rather than `{items,
 * next_cursor}`, it sets connection state from the same payload, and a
 * **404 is a normal pre-connection state rather than an error**. Fitting
 * it would mean an envelope mapper plus a per-status escape hatch, both
 * used exactly once — more shared surface than the duplication costs.
 * Sharing code that only nearly matches is how a helper acquires four
 * boolean options.
 *
 * The duplication was not free. `projects/page.tsx` was written from the
 * same template and the stale-response guard was simply left out of it,
 * which nobody noticed until a review read all eight side by side — and
 * that page APPENDS, so a superseded response duplicated rows rather than
 * merely overwriting them. A shared hook makes that omission unavailable
 * rather than merely discouraged.
 *
 * ## The guard
 *
 * A generation counter, bumped on every `replace` load. A response whose
 * generation is stale is dropped — including its `setLoading(false)`, so a
 * superseded request cannot clear the spinner belonging to the one still
 * in flight.
 *
 * The check sits ABOVE the `!response.ok` branch. Below it, only the error
 * path would be protected and the success write — the one that actually
 * corrupts the list — would stay exposed. That is the exact mistake made
 * once already while applying the guard by hand.
 *
 * `loadMore` deliberately does NOT bump the generation: appending page 2
 * must not invalidate page 1's own in-flight state. It still reads the
 * current generation, so a `reload()` racing a `loadMore()` discards the
 * append rather than stitching a page of the old filter onto the new list.
 *
 * ## Why not SWR / TanStack Query
 *
 * Both would do this and more. Neither is in `package.json`, and this is
 * ~60 lines against `fetch` with no cache-invalidation model to learn.
 * Adding a data-fetching library is a reasonable future call; making it a
 * side effect of deduplicating seven loaders is not.
 */
export interface CursorListResult<T> {
  items: T[];
  nextCursor: string | null;
  loading: boolean;
  error: string | null;
  /** Fetch the next page and append it. No-op when there is no cursor. */
  loadMore: () => void;
  /** Re-fetch from the start, replacing everything. */
  reload: () => void;
  /**
   * For the handful of callers that mutate a row in place after a PATCH
   * rather than re-fetching the whole list.
   */
  setItems: React.Dispatch<React.SetStateAction<T[]>>;
  /**
   * Callers that also WRITE (ExpensePanel's inline create form) surface
   * their failures in the same banner the list uses, so they need to be
   * able to set it. A separate error state per component would render two
   * banners stacked.
   */
  setError: React.Dispatch<React.SetStateAction<string | null>>;
}

export interface CursorListOptions {
  /** BFF path without query string, e.g. "/api/leads". */
  path: string;
  /**
   * Extra query parameters. Undefined/empty values are omitted, so a
   * cleared filter produces `/api/leads` rather than `/api/leads?status=`.
   *
   * Pass a stable object (literal, or memoised) — it is serialised into
   * the effect's dependency, so an inline object rebuilt every render is
   * fine, but a NEW value each render would re-fetch each render.
   */
  params?: Record<string, string | null | undefined>;
  /** Entity noun for the failure message: "Failed to load {label}". */
  label: string;
  /**
   * Skip loading entirely while false — for lists nested behind a parent
   * id that has not resolved yet (InvoiceList's `projectId`).
   */
  enabled?: boolean;
}

export function useCursorList<T>({
  path,
  params,
  label,
  enabled = true,
}: CursorListOptions): CursorListResult<T> {
  const { accessToken } = useAuth();
  const [items, setItems] = React.useState<T[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  // Seeded from `enabled`: a list waiting on a parent selection is not
  // loading, it is idle, and starting it at `true` would render a spinner
  // for something that is never going to be fetched.
  const [loading, setLoading] = React.useState(enabled);
  const [error, setError] = React.useState<string | null>(null);
  const generationRef = React.useRef(0);

  // Serialised so the effect below depends on the params' VALUE rather
  // than on object identity; callers can then pass an inline literal.
  const query = React.useMemo(() => {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params ?? {})) {
      if (value) search.set(key, value);
    }
    return search.toString();
  }, [params]);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken || !enabled) return;
      const generation = replace ? ++generationRef.current : generationRef.current;
      setLoading(true);
      setError(null);
      try {
        const search = new URLSearchParams(query);
        if (cursor) search.set("cursor", cursor);
        const response = await fetch(`${path}?${search}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        // Above the !response.ok branch on purpose — see the module docstring.
        if (generation !== generationRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? `Failed to load ${label}`);
          return;
        }
        setItems((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === generationRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === generationRef.current) setLoading(false);
      }
    },
    [accessToken, enabled, label, path, query]
  );

  React.useEffect(() => {
    // Deferred to a promise callback so no setState in load's call path
    // runs synchronously inside the effect (react-hooks/set-state-in-effect).
    void Promise.resolve().then(() => {
      if (!enabled) {
        // Clear rather than keep. A nested list whose parent was
        // DESELECTED must not go on showing the previous parent's rows —
        // that reads as "this project has these invoices", which is false.
        // Bump the generation too, so a request still in flight for the
        // old parent cannot land after this.
        generationRef.current += 1;
        setItems([]);
        setNextCursor(null);
        setError(null);
        setLoading(false);
        return;
      }
      void load(null, true);
    });
  }, [enabled, load]);

  const loadMore = React.useCallback(() => {
    if (!nextCursor) return;
    void load(nextCursor, false);
  }, [load, nextCursor]);

  const reload = React.useCallback(() => {
    void load(null, true);
  }, [load]);

  return { items, nextCursor, loading, error, loadMore, reload, setItems, setError };
}
