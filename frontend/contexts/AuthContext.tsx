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

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = React.useState<AuthState>({
    accessToken: null,
    mfaEnrollmentRequired: false,
  });
  const [isHydrating, setIsHydrating] = React.useState(true);
  const refreshTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearSession = React.useCallback(() => {
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    setState({ accessToken: null, mfaEnrollmentRequired: false });
  }, []);

  // Named function expression (not an arrow assigned to the outer const):
  // the recursive call inside the timeout below binds to this function's
  // own name, not to the `scheduleRefresh` const it's assigned to. That
  // avoids a forward reference to a binding that isn't finished
  // initializing yet from the compiler/lint's point of view, while
  // preserving the exact same recursive-timer behavior.
  const scheduleRefresh = React.useCallback(function scheduleRefresh() {
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    refreshTimerRef.current = setTimeout(async () => {
      try {
        const response = await fetch("/api/auth/refresh", { method: "POST" });
        if (!response.ok) {
          clearSession();
          return;
        }
        const data = await response.json();
        setState({ accessToken: data.access_token, mfaEnrollmentRequired: data.mfa_enrollment_required });
        scheduleRefresh();
      } catch {
        // Network-level failure (offline, DNS, backend unreachable) — an
        // unhandled rejection here would silently kill the recursive
        // refresh chain forever, leaving a stale accessToken in state
        // with no path back to a valid session. Treat it the same as a
        // failed refresh: clear the session rather than leave the UI
        // believing it's authenticated.
        clearSession();
      }
    }, ACCESS_TOKEN_LIFETIME_MS - REFRESH_MARGIN_MS);
  }, [clearSession]);

  const setSession = React.useCallback(
    (accessToken: string, mfaEnrollmentRequired: boolean) => {
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
    // validity) is permanently stuck at accessToken === null: nothing else
    // in this file ever calls /api/auth/refresh except scheduleRefresh's
    // own timer, and that timer is only armed by setSession, which nothing
    // calls on mount.
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch("/api/auth/refresh", { method: "POST" });
        if (cancelled) return;
        if (!response.ok) return;
        const data = await response.json();
        if (cancelled) return;
        setSession(data.access_token, data.mfa_enrollment_required);
      } catch {
        // No refresh cookie, or backend unreachable — stay logged out.
        // Mirrors scheduleRefresh's own failure handling (silent, no retry).
      } finally {
        if (!cancelled) setIsHydrating(false);
      }
    })();
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
