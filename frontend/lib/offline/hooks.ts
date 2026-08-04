"use client";

/**
 * The three things both offline screens need, in one place.
 *
 * Estimator capture and the field-crew queue send completely different
 * writes, and share every question around them: whose data is this, is the
 * server actually reachable, and when do we try again. Those answers were
 * written once for the capture screen; a second copy on `/my-tasks` is how
 * two screens end up disagreeing about whether the device is offline.
 *
 * The FLUSHES stay separate (`flush.ts` and `crew.ts`) — those genuinely
 * differ.
 */

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import type { OfflineIdentity } from "./identity";
import { readIdentity } from "./identity";
import { readRememberedIdentity } from "./store";

/**
 * How often anything waiting tries again.
 *
 * A timer rather than the `online` event alone, because that event is a
 * hint and not a fact: `navigator.onLine` reads `true` behind a captive
 * portal, on a Wi-Fi with no route out, and under Playwright's own offline
 * emulation. The only way to know whether the server is reachable is to ask
 * it — so this asks, but only while there is something to send.
 */
export const RETRY_MS = 10 * 1000;

/**
 * Whose cached data and whose queue this is.
 *
 * The token when there is one; the identity this device last held a session
 * for when there is not. An offline cold start CANNOT have a token — it
 * lives in memory and re-deriving it needs the network — so the remembered
 * value is the only thing that knows.
 */
export function useOfflineIdentity(): { identity: OfflineIdentity | null; resolved: boolean } {
  const { accessToken, isHydrating } = useAuth();
  const [identity, setIdentity] = React.useState<OfflineIdentity | null>(null);
  const [resolved, setResolved] = React.useState(false);

  React.useEffect(() => {
    if (isHydrating) return;
    void (async () => {
      const fromToken = readIdentity(accessToken);
      setIdentity(fromToken ?? (await readRememberedIdentity()));
      setResolved(true);
    })();
  }, [accessToken, isHydrating]);

  return { identity, resolved };
}

/**
 * Whether the server is reachable — as evidence, never as a claim.
 *
 * Two inputs, both of them things that actually happened: AuthContext's
 * `sessionUnreachable` (a cold start whose session could not be
 * re-established, true before this screen has attempted anything) and
 * whether this screen's own last send arrived. `navigator.onLine` is not
 * consulted; the browser's `offline` event is taken only as a hint, in the
 * direction it is reliable.
 */
export function useConnectionEvidence(): {
  reachable: boolean;
  noteAttempt: (arrived: boolean) => void;
} {
  const { sessionUnreachable } = useAuth();
  const [lastAttemptArrived, setLastAttemptArrived] = React.useState(true);

  React.useEffect(() => {
    const goingOffline = () => setLastAttemptArrived(false);
    window.addEventListener("offline", goingOffline);
    return () => window.removeEventListener("offline", goingOffline);
  }, []);

  return {
    reachable: lastAttemptArrived && !sessionUnreachable,
    noteAttempt: setLastAttemptArrived,
  };
}

/**
 * A counter that advances while there is work waiting.
 *
 * Callers key their "already tried this one" bookkeeping by the tick rather
 * than clearing a set on a timer, so an item is attempted at most once per
 * tick and a device with no signal makes one request every `RETRY_MS`
 * instead of spinning. Stops entirely when `active` goes false — nothing
 * waiting, nothing polling.
 */
export function useRetryTicker(active: boolean): { tick: number; retryNow: () => void } {
  const [tick, setTick] = React.useState(0);
  const advance = React.useCallback(() => setTick((value) => value + 1), []);

  React.useEffect(() => {
    if (!active) return;
    const timer = setTimeout(advance, RETRY_MS);
    return () => clearTimeout(timer);
  }, [active, advance, tick]);

  React.useEffect(() => {
    // Shortens the wait when the browser volunteers that it is back. Never
    // the only path, because that event fires late or not at all.
    window.addEventListener("online", advance);
    return () => window.removeEventListener("online", advance);
  }, [advance]);

  return { tick, retryNow: advance };
}
