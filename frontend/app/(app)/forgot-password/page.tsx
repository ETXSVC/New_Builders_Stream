"use client";

import * as React from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Ask for a reset link.
 *
 * The confirmation deliberately does NOT say whether the address has an
 * account — it says what was done, not what was found. The backend answers
 * 202 either way for the same reason: whether a given company uses this
 * product is exactly what a competitor would probe for, and a page that
 * says "no account with that email" is a free enumeration oracle however
 * careful the API was.
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [sent, setSent] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const response = await fetch("/api/auth/password-reset/request", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        // A 429 is the one failure worth naming: it is the user's own
        // repeated attempts, and telling them to wait is actionable.
        setError(data?.detail ?? "Could not send the reset link. Try again shortly.");
        return;
      }
      setSent(true);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex w-full max-w-sm flex-col gap-6">
        <h1 className="text-xl font-semibold">Reset your password</h1>

        {sent ? (
          <div className="flex flex-col gap-3">
            <p role="status" className="text-sm text-slate-700">
              If that address has an account, a reset link is on its way. It works once and
              expires in an hour.
            </p>
            <p className="text-sm text-slate-500">
              Nothing arrived? Check the spam folder, then{" "}
              <button
                type="button"
                className="underline"
                onClick={() => setSent(false)}
              >
                try another address
              </button>
              .
            </p>
            <Link href="/login" className="text-sm text-slate-600 hover:text-slate-900">
              ← Back to log in
            </Link>
          </div>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-4">
            <p className="text-sm text-slate-500">
              Enter the email you sign in with and we&apos;ll send you a link to set a new
              password.
            </p>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="reset-email">Email</Label>
              <Input
                id="reset-email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={submitting}
                required
              />
            </div>
            {error && (
              <p role="alert" aria-live="assertive" className="text-sm text-red-600">
                {error}
              </p>
            )}
            {/* Gated on `submitting` only. Disabling on an empty `email`
                reads as safer and is not: a value filled before React
                hydrates (browser autofill, a password manager, a test
                driver) never reaches state, so the button stays disabled
                with a visibly filled field. `required` on the input
                already stops an empty submit. */}
            <Button type="submit" disabled={submitting}>
              {submitting ? "Sending…" : "Send reset link"}
            </Button>
            <Link href="/login" className="text-sm text-slate-600 hover:text-slate-900">
              ← Back to log in
            </Link>
          </form>
        )}
      </div>
    </main>
  );
}
