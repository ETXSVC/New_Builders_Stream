"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

// A static pre-auth path taking the invitation id as a query param
// (?id=<uuid>) rather than a dynamic segment: AppShell's PRE_AUTH_PATHS
// check is an exact pathname match (usePathname() excludes the query
// string), so this slots into the existing mechanism without rewriting it
// to prefix matching. Deliberately NOT in middleware.ts's matcher — the
// invitee has no session yet; this page must be reachable logged-out.

function AcceptInvitationForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const invitationId = searchParams.get("id");

  const [fullName, setFullName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [confirmPassword, setConfirmPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [accepted, setAccepted] = React.useState(false);
  const [submitting, setSubmitting] = React.useState(false);

  if (!invitationId) {
    return (
      <p role="alert" className="text-sm text-red-600">
        This invitation link is missing its invitation id. Ask your administrator to send the link
        again.
      </p>
    );
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || accepted) return;
    if (password !== confirmPassword) {
      setError("Passwords don't match.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/invitations/${encodeURIComponent(invitationId!)}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ full_name: fullName, password }),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        if (response.status === 404) {
          setError("This invitation link is invalid. Ask your administrator to send a new one.");
        } else if (response.status === 409) {
          setError(
            "This invitation was already accepted (or the email already has an account) — try logging in instead."
          );
        } else if (response.status === 410) {
          setError("This invitation has expired. Ask your administrator for a new invitation.");
        } else {
          setError(data?.detail ?? "Failed to accept the invitation.");
        }
        return;
      }
      setAccepted(true);
      router.push("/login");
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 w-full max-w-sm">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="fullName">Your name</Label>
        <Input
          id="fullName"
          autoComplete="name"
          value={fullName}
          onChange={(e) => setFullName(e.target.value)}
          disabled={submitting || accepted}
          required
          minLength={2}
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
          disabled={submitting || accepted}
          required
          minLength={8}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="confirmPassword">Confirm password</Label>
        <Input
          id="confirmPassword"
          type="password"
          autoComplete="new-password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          disabled={submitting || accepted}
          required
          minLength={8}
        />
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {error?.includes("logging in") && (
        <p className="text-sm">
          <Link href="/login" className="underline">
            Go to login
          </Link>
        </p>
      )}
      <Button type="submit" disabled={submitting || accepted}>
        {accepted ? "Account created" : submitting ? "Creating account…" : "Accept invitation"}
      </Button>
    </form>
  );
}

export default function AcceptInvitationPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col gap-6 items-center">
        <h1 className="text-xl font-semibold">Join your team on Builders Stream</h1>
        {/* useSearchParams requires a Suspense boundary in the App Router. */}
        <React.Suspense fallback={null}>
          <AcceptInvitationForm />
        </React.Suspense>
      </div>
    </main>
  );
}
