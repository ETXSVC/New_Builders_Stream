"use client";

import * as React from "react";

interface AuthState {
  accessToken: string | null;
  mfaEnrollmentRequired: boolean;
}

interface AuthContextValue extends AuthState {
  // True until the mount-time hydration attempt below resolves. A page
  // with valid refresh cookie but a fresh load (bookmark, hard refresh,
  // new tab) starts with accessToken === null for one round-trip; callers
  // that render differently for "definitely logged out" vs. "haven't
  // heard back yet" can check this instead of misreading null as logged out.
  isHydrating: boolean;
  setSession: (accessToken: string, mfaEnrollmentRequired: boolean) => void;
  clearSession: () => void;
}

const AuthContext = React.createContext<AuthContextValue | null>(null);

// Refresh 60s before the access token's known 15-minute lifetime — see
// docs/superpowers/specs/2026-07-16-auth-token-lifecycle-design.md.
const ACCESS_TOKEN_LIFETIME_MS = 15 * 60 * 1000;
const REFRESH_MARGIN_MS = 60 * 1000;

// Cross-tab coordination for /api/auth/refresh (closes the documented
// multi-tab refresh race — docs/superpowers/plans/2026-07-16-frontend-
// foundation.md, Task 7 review). The backend's refresh tokens are
// single-use with family-level reuse detection: two tabs whose scheduled
// refreshes fire close enough together can both send the SAME
// not-yet-rotated cookie, and the backend treats the loser as a replay
// attack and revokes the whole family, logging both tabs out. The Web
// Locks API serializes every tab's refresh network call through one
// origin-wide mutex, so a queued tab's request always carries whatever
// cookie value is current by the time it actually runs — never a stale,
// already-rotated one — which is what removes the race, not knowing
// which tab "won." Locks auto-release if their holding tab closes or
// crashes (a core Web Locks guarantee), so there's no leader/heartbeat
// bookkeeping to get wrong here, unlike a hand-rolled coordination
// scheme would need. Each tab still ends up with its own independent
// access token, same as before this fix — this only serializes the
// network calls, it doesn't share one token across tabs.
const REFRESH_LOCK_NAME = "builders-stream-auth-refresh";

type RefreshResult = { access_token: string; mfa_enrollment_required: boolean };

async function performRealRefresh(): Promise<RefreshResult | null> {
  try {
    const response = await fetch("/api/auth/refresh", { method: "POST" });
    if (!response.ok) return null;
    return (await response.json()) as RefreshResult;
  } catch {
    // Network-level failure (offline, DNS, backend unreachable) — treat
    // the same as a failed refresh, not an unhandled rejection.
    return null;
  }
}

// Module-level, not a hook: it has no component state of its own beyond
// the in-flight-promise ref each caller passes in, and the Web Locks call
// itself needs no React lifecycle.
async function coordinatedRefresh(
  inFlightRef: React.RefObject<Promise<RefreshResult | null> | null>
): Promise<RefreshResult | null> {
  // Shared by every caller in THIS tab that wants a refresh right now, so
  // React's dev-only Strict Mode double-invoking the mount effect below —
  // or any other source of overlapping calls within one tab — triggers at
  // most one real network request, not two.
  if (inFlightRef.current) return inFlightRef.current;

  const promise = (async () => {
    try {
      const hasWebLocks = typeof navigator !== "undefined" && "locks" in navigator;
      if (!hasWebLocks) {
        // Very old/unusual environment without the Web Locks API — fall
        // back to firing the request directly. Not worse than before this
        // fix; it just doesn't get the cross-tab serialization benefit.
        return await performRealRefresh();
      }
      return await navigator.locks.request(REFRESH_LOCK_NAME, () => performRealRefresh());
    } catch {
      // navigator.locks.request itself can reject (e.g. AbortError if the
      // document stops being fully active mid-request, such as during a
      // hard navigation) even though performRealRefresh() never throws on
      // its own. Without this, a rejection here would propagate out of
      // coordinatedRefresh and past scheduleRefresh's `await` — skipping
      // its own re-arm call and silently ending the tab's refresh cycle
      // for good, with nothing to bring it back except a full reload.
      // Treat it the same as a failed refresh.
      return null;
    }
  })();

  // inFlightRef.current is guaranteed cleared before any caller's `await
  // coordinatedRefresh(...)` resumes: `.finally()` is registered here,
  // before this function returns the SAME promise object it was called
  // on, and promise handlers run in registration order. Don't reorder
  // these two statements or swap `promise` below for `promise.finally(...)`'s
  // own derived promise — either change breaks that guarantee silently,
  // with no test currently exercising the ordering directly.
  inFlightRef.current = promise;
  promise.finally(() => {
    inFlightRef.current = null;
  });
  return promise;
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = React.useState<AuthState>({
    accessToken: null,
    mfaEnrollmentRequired: false,
  });
  const [isHydrating, setIsHydrating] = React.useState(true);
  const refreshTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);
  // Bumped by every explicit session change (setSession/clearSession). An
  // async refresh in flight when one of those happens captures the
  // generation it started with and discards its own result if the
  // generation has since moved on — otherwise a slow refresh that's
  // already stale by the time it resolves could silently resurrect a
  // session the user (or another code path) already explicitly ended.
  const sessionGenerationRef = React.useRef(0);
  const inFlightRefreshRef = React.useRef<Promise<RefreshResult | null> | null>(null);

  const clearSession = React.useCallback(() => {
    sessionGenerationRef.current += 1;
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    setState({ accessToken: null, mfaEnrollmentRequired: false });
  }, []);

  // Named function expression (not an arrow assigned to the outer const):
  // the recursive call inside the timeout below binds to this function's
  // own name, not to the `scheduleRefresh` const it's assigned to. That
  // avoids a forward reference to a binding that isn't finished
  // initializing yet from the compiler/lint's point of view, while
  // preserving the exact same recursive-timer behavior.
  const scheduleRefresh = React.useCallback(
    function scheduleRefresh() {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
      refreshTimerRef.current = setTimeout(async () => {
        const generationAtStart = sessionGenerationRef.current;
        const data = await coordinatedRefresh(inFlightRefreshRef);
        if (sessionGenerationRef.current !== generationAtStart) return;
        if (data === null) {
          clearSession();
          return;
        }
        setState({ accessToken: data.access_token, mfaEnrollmentRequired: data.mfa_enrollment_required });
        scheduleRefresh();
      }, ACCESS_TOKEN_LIFETIME_MS - REFRESH_MARGIN_MS);
    },
    [clearSession]
  );

  const setSession = React.useCallback(
    (accessToken: string, mfaEnrollmentRequired: boolean) => {
      sessionGenerationRef.current += 1;
      setState({ accessToken, mfaEnrollmentRequired });
      scheduleRefresh();
    },
    [scheduleRefresh]
  );

  React.useEffect(() => {
    // Cold-load hydration (spec: "the app re-derives a fresh access token
    // from [the refresh cookie] on mount"). accessToken lives only in this
    // component's memory, so a fresh page load — bookmark, hard refresh,
    // new tab — starts with accessToken null even when a valid
    // refresh_token cookie exists. Without this, every cold load onto a
    // page middleware let through (it only checks cookie presence, not
    // validity) is permanently stuck at accessToken === null.
    let cancelled = false;
    const generationAtStart = sessionGenerationRef.current;
    coordinatedRefresh(inFlightRefreshRef)
      .then((data) => {
        if (cancelled || sessionGenerationRef.current !== generationAtStart) return;
        if (data) setSession(data.access_token, data.mfa_enrollment_required);
      })
      .finally(() => {
        if (!cancelled) setIsHydrating(false);
      });
    return () => {
      cancelled = true;
    };
    // Intentionally mount-only: re-running this on every setSession identity
    // change would refresh on each render, not just on mount. setSession's
    // identity is stable for the component's lifetime (it only depends on
    // scheduleRefresh, which only depends on clearSession, which has no
    // deps), so this is safe to omit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  React.useEffect(() => {
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    };
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, isHydrating, setSession, clearSession }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
