"use client";

import * as React from "react";

/**
 * Guards an async loader against its own superseded invocations.
 *
 * Every data-loading component in this app has the same shape: a
 * `React.useCallback(async () => { ...await fetch...; setState(data) })`,
 * invoked BOTH from a `useEffect` on mount/dependency-change AND imperatively
 * after a mutation ("POST the thing, then reload the list"). Nothing sequences
 * those invocations, so two can be in flight at once and whichever resolves
 * LAST wins — regardless of which was issued last.
 *
 * That is not hypothetical. Two ways it bites in practice:
 *
 *  1. **Reload-after-write.** `CommunicationLog` POSTs an entry then calls
 *     `loadAll()`. If the mount-time `loadAll()` (a multi-page cursor walk, so
 *     several round trips) is still running, it can land afterwards and
 *     overwrite the list with its pre-insert snapshot. The user watches the
 *     row they just added disappear.
 *  2. **Route-param change.** `/estimates/[id]` reloads when `id` changes —
 *     e.g. "Duplicate as new draft" navigating from the approved estimate to
 *     the new one. The outgoing estimate's in-flight response can land after
 *     the new one's and put the OLD estimate back on screen, under the new
 *     URL.
 *
 * Both were reproduced locally: 2 of 3 full e2e runs failed, at those two
 * spots, which is also the scattered-failure signature CI had been showing.
 *
 * `accessToken` makes it worse rather than causing it: the token lives in
 * `useAuth` state, so a refresh changes it, which re-creates every loader
 * `useCallback` that closes over it and re-runs the effect — a second loader
 * starts while the first is still going.
 *
 * Usage — call at the top of the loader, check before every `setState`:
 *
 *     const beginLoad = useLatestOnly();
 *     const load = React.useCallback(async () => {
 *       const isCurrent = beginLoad();
 *       const data = await fetchThings();
 *       if (!isCurrent()) return;   // a newer load started; drop this result
 *       setThings(data);
 *     }, [beginLoad, ...]);
 *
 * A counter rather than `AbortController` deliberately: aborting only covers
 * the effect path (where a cleanup function exists to call it from), and the
 * imperative reload-after-write path — the one that actually loses a user's
 * data from the screen — has no cleanup hook to hang an abort on. The counter
 * covers both entry points with the same three lines. It also leaves the
 * superseded request running rather than cancelling it, which is the right
 * trade here: these are small GETs, and a cancelled request that the server
 * already handled buys nothing.
 *
 * The returned `beginLoad` is referentially stable, so it is safe in a
 * `useCallback`/`useEffect` dependency array.
 */
export function useLatestOnly(): () => () => boolean {
  const latest = React.useRef(0);

  return React.useCallback(() => {
    const mine = ++latest.current;
    return () => mine === latest.current;
  }, []);
}
