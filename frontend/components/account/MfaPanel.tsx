"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

type Step = "idle" | "activating";

export function MfaPanel({ mfaActive }: { mfaActive: boolean }) {
  const router = useRouter();
  const { accessToken, isHydrating, clearSession } = useAuth();
  const [step, setStep] = React.useState<Step>("idle");
  const [secret, setSecret] = React.useState("");
  const [totpCode, setTotpCode] = React.useState("");
  const [currentPassword, setCurrentPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);
  // Set the moment /mfa/activate returns 200, and OR'd into `mfaActive`
  // below.
  //
  // `mfaActive` is a prop derived from the auth context's
  // `mfaEnrollmentRequired`, which is populated at login and never
  // recomputed client-side. `router.refresh()` re-runs server components;
  // it cannot change a value held in React context. So without this, a
  // successful enrolment sent `step` back to "idle" with `mfaActive` still
  // false and the panel rendered "Enable two-factor authentication" again
  // — telling the user their enrolment had failed when it had in fact
  // succeeded, and inviting them to enrol a second time.
  //
  // A dedicated "am I MFA-active" field on the session (which
  // app/(app)/account/page.tsx's comment already anticipates) would let
  // both this and the prop derive from one source; until then this is the
  // narrower fix, local to the component that knows the activation
  // happened.
  const [justActivated, setJustActivated] = React.useState(false);

  async function startEnroll() {
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/mfa/enroll", {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Enrollment failed");
        return;
      }
      setSecret(data.secret);
      setStep("activating");
    } catch {
      // Network-level failure (offline, DNS, backend unreachable) — same
      // treatment as LoginForm/RegisterForm's fetch handlers.
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmActivate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/mfa/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ totp_code: totpCode }),
      });
      if (!response.ok) {
        const data = await response.json();
        setError(data.detail ?? "Activation failed");
        return;
      }
      setStep("idle");
      setTotpCode("");
      setJustActivated(true);
      router.refresh();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function disableMfa(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/mfa/disable", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ current_password: currentPassword, totp_code: totpCode }),
      });
      if (!response.ok) {
        const data = await response.json();
        setError(data.detail ?? "Disable failed");
        return;
      }
      // Disabling MFA revoked this session's refresh token server-side
      // (see the Route Handler's comment) — treat it as a logout.
      clearSession();
      router.push("/login");
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  // mfaActive (AccountPage's !mfaEnrollmentRequired proxy) is only
  // trustworthy once a session has actually been confirmed. Before that —
  // hydration still in flight, OR hydration finished but the refresh
  // cookie turned out to be stale/invalid (middleware only checks cookie
  // *presence*, not validity, so this page is reachable in that state) —
  // mfaEnrollmentRequired sits at its unauthenticated default (false),
  // making mfaActive read as true regardless of the real, unknown MFA
  // status. Gating on isHydrating alone only covers the first case and
  // leaves the second permanently showing (and enabling) the "Disable
  // two-factor authentication" form — asking an unauthenticated visitor
  // for their password — with no guard once isHydrating flips to false.
  const hasConfirmedSession = !isHydrating && accessToken !== null;
  // The prop is the login-time answer; `justActivated` is this session's
  // more recent one. Either being true means MFA is on.
  const isActive = mfaActive || justActivated;

  return (
    <Card className="max-w-md">
      <CardHeader>
        <CardTitle>Two-factor authentication</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        )}

        {!hasConfirmedSession && step === "idle" && (
          <p className="text-sm text-slate-500">Loading account status…</p>
        )}

        {hasConfirmedSession && step === "idle" && !isActive && (
          <Button onClick={startEnroll} disabled={submitting}>
            Enable two-factor authentication
          </Button>
        )}

        {step === "activating" && (
          <form onSubmit={confirmActivate} className="flex flex-col gap-3">
            {/* No QR rendering in Foundation (would need an extra client
                dependency) — the base32 secret is the universal manual-entry
                path every authenticator app supports; a scannable QR code
                (built from the same otpauth_uri the backend also returns)
                is a natural, low-effort follow-up once this ships. */}
            <p className="text-sm text-slate-600">
              Enter this code manually in your authenticator app (Google Authenticator, 1Password, etc. all support
              &quot;Enter a setup key&quot;), then enter the 6-digit code it generates:
            </p>
            <code className="text-sm tracking-wider break-all bg-slate-50 p-2 rounded font-mono">{secret}</code>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="activate-code">Code</Label>
              <Input
                id="activate-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={totpCode}
                onChange={(e) => setTotpCode(e.target.value)}
                disabled={submitting}
                required
                autoFocus
              />
            </div>
            <Button type="submit" disabled={submitting}>
              Confirm
            </Button>
          </form>
        )}

        {hasConfirmedSession && step === "idle" && isActive && (
          <form onSubmit={disableMfa} className="flex flex-col gap-3">
            <p className="text-sm text-slate-600">Two-factor authentication is on. Disabling it will log you out everywhere.</p>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="disable-password">Current password</Label>
              <Input
                id="disable-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                disabled={submitting}
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="disable-code">Authenticator code</Label>
              <Input
                id="disable-code"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={totpCode}
                onChange={(e) => setTotpCode(e.target.value)}
                disabled={submitting}
                required
              />
            </div>
            <Button type="submit" variant="outline" disabled={submitting}>
              Disable two-factor authentication
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
