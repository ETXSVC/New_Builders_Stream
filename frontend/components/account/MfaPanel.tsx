"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";

type Step = "idle" | "enrolling" | "activating";

export function MfaPanel({ mfaActive }: { mfaActive: boolean }) {
  const router = useRouter();
  const { accessToken, isHydrating, clearSession } = useAuth();
  const [step, setStep] = React.useState<Step>("idle");
  const [secret, setSecret] = React.useState("");
  const [totpCode, setTotpCode] = React.useState("");
  const [currentPassword, setCurrentPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

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

        {step === "idle" && !mfaActive && (
          // Disabled while isHydrating: on a fresh page load (bookmark, hard
          // refresh) accessToken is still null for one round-trip while the
          // app re-derives it from the refresh cookie (AuthContext). Without
          // this guard, a click during that window fires the request with
          // the literal header "Bearer null".
          <Button onClick={startEnroll} disabled={submitting || isHydrating}>
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

        {step === "idle" && mfaActive && (
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
            {/* Same isHydrating guard as the enable button above. */}
            <Button type="submit" variant="outline" disabled={submitting || isHydrating}>
              Disable two-factor authentication
            </Button>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
