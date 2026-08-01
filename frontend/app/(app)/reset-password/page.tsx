"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Spend a reset link.
 *
 * The token arrives as `?token=`, which is the one place this app puts a
 * credential in a URL — and the reason `frontend/sentry.shared.ts` scrubs
 * query strings including a bare `id`: the invitation-accept page already
 * had this shape, and an error report carrying either link would be
 * carrying a live key. `SENSITIVE_QUERY_KEYS` covers `token` too.
 *
 * A successful reset sends the user to the login form rather than signing
 * them in. That is deliberate: the reset revokes every session the account
 * holds, so minting a fresh one here would undo the point of it.
 */
function ResetPasswordForm() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";

  const [password, setPassword] = React.useState("");
  const [confirmation, setConfirmation] = React.useState("");
  const [totpCode, setTotpCode] = React.useState("");
  const [needsTotp, setNeedsTotp] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const mismatch = confirmation !== "" && password !== confirmation;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting || mismatch) return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/auth/password-reset/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token,
          new_password: password,
          totp_code: totpCode || null,
        }),
      });

      if (response.status === 204) {
        router.push("/login?reset=1");
        return;
      }

      const data = await response.json().catch(() => null);
      const detail: string = data?.detail ?? "Could not reset the password.";
      // 401 means the account has two-factor authentication on and the
      // code was missing or wrong — the form grows a field rather than
      // sending the user back to the start, because the link is still
      // good and is single-use.
      if (response.status === 401) {
        setNeedsTotp(true);
      }
      setError(detail);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (!token) {
    return (
      <div className="flex flex-col gap-3">
        <p role="alert" className="text-sm text-red-600">
          This page needs the link from your reset email.
        </p>
        <Link href="/forgot-password" className="text-sm text-slate-600 hover:text-slate-900">
          Request a new link
        </Link>
      </div>
    );
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="new-password">New password</Label>
        <Input
          id="new-password"
          type="password"
          autoComplete="new-password"
          minLength={8}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="confirm-password">Confirm password</Label>
        <Input
          id="confirm-password"
          type="password"
          autoComplete="new-password"
          value={confirmation}
          onChange={(e) => setConfirmation(e.target.value)}
          disabled={submitting}
          required
        />
        {mismatch && <p className="text-xs text-red-600">The two passwords don&apos;t match.</p>}
      </div>

      {needsTotp && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="reset-totp">Authenticator code</Label>
          <Input
            id="reset-totp"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={totpCode}
            onChange={(e) => setTotpCode(e.target.value)}
            disabled={submitting}
          />
          <p className="text-xs text-slate-500">
            This account has two-factor authentication turned on, so the code is needed here
            too.
          </p>
        </div>
      )}

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {/* `mismatch` stays — it is a real, hydrated-state condition. The
          empty-password check does not, for the autofill reason the
          forgot-password page explains; `required` covers it. */}
      <Button type="submit" disabled={submitting || mismatch}>
        {submitting ? "Saving…" : "Set new password"}
      </Button>
      <Link href="/forgot-password" className="text-sm text-slate-600 hover:text-slate-900">
        Request a new link
      </Link>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex w-full max-w-sm flex-col gap-6">
        <h1 className="text-xl font-semibold">Set a new password</h1>
        {/* useSearchParams needs a Suspense boundary to prerender — the
            same wrapper the accept-invitation page uses for its own
            `?id=`. */}
        <React.Suspense fallback={<p className="text-sm text-slate-500">Loading…</p>}>
          <ResetPasswordForm />
        </React.Suspense>
      </div>
    </main>
  );
}
