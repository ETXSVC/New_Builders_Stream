"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";

export default function NewSubcontractorPage() {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [name, setName] = React.useState("");
  const [trade, setTrade] = React.useState("");
  const [contactEmail, setContactEmail] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [planGate, setPlanGate] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setSubmitting(true);
    setError(null);
    setPlanGate(null);
    try {
      const response = await fetch("/api/subcontractors", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          name,
          trade: trade || null,
          contact_email: contactEmail || null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          setError(data.detail ?? "Failed to create subcontractor");
        }
        return;
      }
      router.push(`/subcontractors/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">New subcontractor</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4 max-w-sm">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="subName">Name</Label>
          <Input
            id="subName"
            value={name}
            onChange={(e) => setName(e.target.value)}
            disabled={submitting}
            required
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="subTrade">Trade (optional)</Label>
          <Input
            id="subTrade"
            value={trade}
            onChange={(e) => setTrade(e.target.value)}
            disabled={submitting}
            placeholder="e.g. Electrical"
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="subEmail">Contact email (optional)</Label>
          <Input
            id="subEmail"
            type="email"
            value={contactEmail}
            onChange={(e) => setContactEmail(e.target.value)}
            disabled={submitting}
          />
        </div>
        {planGate && <PlanUpgradeNotice detail={planGate} />}
        {error && (
          <p role="alert" aria-live="assertive" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <Button type="submit" disabled={submitting}>
          {submitting ? "Creating…" : "Create subcontractor"}
        </Button>
      </form>
    </main>
  );
}
