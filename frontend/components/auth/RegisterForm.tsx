"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function RegisterForm() {
  const router = useRouter();
  const { setSession } = useAuth();
  const [companyName, setCompanyName] = React.useState("");
  const [fullName, setFullName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  // Set once /auth/register has succeeded, so the account-already-exists
  // fallback below can never suggest resubmitting the form (which would
  // just hit a 409 for an email that's now taken) — it instead points the
  // user at /login. Also drives the button label so a slow auto-login
  // doesn't look like a hung "Create account" click.
  const [accountCreated, setAccountCreated] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    setSubmitting(true);
    let justRegistered = false;
    try {
      const registerResponse = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company_name: companyName,
          admin_full_name: fullName,
          admin_email: email,
          admin_password: password,
        }),
      });
      const registerData = await registerResponse.json();
      if (!registerResponse.ok) {
        setError(registerData.detail ?? "Registration failed");
        return;
      }
      justRegistered = true;
      setAccountCreated(true);

      const loginResponse = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const loginData = await loginResponse.json();
      if (!loginResponse.ok) {
        setError("Your account was created, but automatic sign-in failed. Use the link below to log in.");
        return;
      }
      setSession(loginData.access_token, loginData.mfa_enrollment_required);
      router.push(loginData.mfa_enrollment_required ? "/account" : "/dashboard");
    } catch {
      // Network-level failure (offline, DNS, backend unreachable). If it
      // struck after registration already succeeded, resubmitting this
      // form would just hit a 409 for the now-taken email — point at
      // /login instead of inviting a retry.
      setError(
        justRegistered
          ? "Your account was created, but we couldn't reach the server to sign you in. Use the link below to log in."
          : "Unable to reach the server. Check your connection and try again."
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="companyName">Company name</Label>
        <Input
          id="companyName"
          autoComplete="organization"
          value={companyName}
          onChange={(e) => setCompanyName(e.target.value)}
          disabled={submitting}
          required
          minLength={2}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="fullName">Your name</Label>
        <Input
          id="fullName"
          autoComplete="name"
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
          disabled={submitting}
          required
          minLength={2}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          disabled={submitting}
          required
          minLength={8}
        />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {accountCreated && !submitting && (
        <p className="text-sm">
          <Link href="/login" className="underline">
            Go to login
          </Link>
        </p>
      )}
      <Button type="submit" disabled={submitting || accountCreated}>
        {accountCreated ? (submitting ? "Signing you in…" : "Account created") : submitting ? "Creating account…" : "Create account"}
      </Button>
    </form>
  );
}
