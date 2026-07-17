"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function LoginForm() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [totpCode, setTotpCode] = React.useState("");
  // Set once the backend's first attempt (no code) comes back with
  // "TOTP code required" — reveals the second input, per the backend's
  // own two-step design (password proven first, spec Decision 6 of the
  // MFA design).
  const [needsTotp, setNeedsTotp] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          needsTotp ? { email, password, totp_code: totpCode } : { email, password }
        ),
      });
      const data = await response.json();
      if (!response.ok) {
        if (data.detail === "TOTP code required") {
          setNeedsTotp(true);
          return;
        }
        setError(data.detail ?? "Login failed");
        return;
      }
      setSession(data.access_token, data.mfa_enrollment_required);
      router.push(data.mfa_enrollment_required ? "/account" : "/dashboard");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      {!needsTotp && (
        <>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="password">Password</Label>
            <Input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
        </>
      )}
      {needsTotp && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="totp">Authenticator code</Label>
          <Input
            id="totp"
            inputMode="numeric"
            autoComplete="one-time-code"
            value={totpCode}
            onChange={(e) => setTotpCode(e.target.value)}
            required
            autoFocus
          />
        </div>
      )}
      {error && <p className="text-sm text-red-600">{error}</p>}
      <Button type="submit" disabled={submitting}>
        {needsTotp ? "Verify" : "Log in"}
      </Button>
    </form>
  );
}
