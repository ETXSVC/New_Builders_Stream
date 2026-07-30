"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Unlike the product's LoginForm this asks for the TOTP code up front rather
 * than revealing it after a "TOTP code required" round-trip. Two factors are
 * OPTIONAL for a tenant user and MANDATORY here — `/platform/auth/login`
 * refuses any account with `mfa_activated_at` unset — so there is no
 * code-less path to discover, and prompting for it in one step saves an
 * exchange that can only ever have one outcome.
 */
export function PlatformLoginForm() {
  const router = useRouter();
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [totpCode, setTotpCode] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/platform/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, totp_code: totpCode }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Sign-in failed");
        return;
      }
      // The token is already in an httpOnly cookie set by the BFF — there is
      // nothing to hand to a context here. `refresh` rather than a bare push
      // so middleware re-evaluates with the new cookie.
      router.replace("/platform");
      router.refresh();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="platform-email">Email</Label>
        <Input
          id="platform-email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="platform-password">Password</Label>
        <Input
          id="platform-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="platform-totp">Authenticator code</Label>
        <Input
          id="platform-totp"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={totpCode}
          onChange={(e) => setTotpCode(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <Button type="submit" disabled={submitting}>
        {submitting ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
