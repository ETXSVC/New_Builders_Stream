"use client";

import * as React from "react";

interface AuthState {
  accessToken: string | null;
  mfaEnrollmentRequired: boolean;
}

interface AuthContextValue extends AuthState {
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
  const refreshTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearSession = React.useCallback(() => {
    if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    setState({ accessToken: null, mfaEnrollmentRequired: false });
  }, []);

  const scheduleRefresh = React.useCallback(() => {
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
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    };
  }, []);

  return (
    <AuthContext.Provider value={{ ...state, setSession, clearSession }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = React.useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}
