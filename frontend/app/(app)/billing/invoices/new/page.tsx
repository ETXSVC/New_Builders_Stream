"use client";

import * as React from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ProjectScopeSelect } from "@/components/billing/ProjectScopeSelect";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";

function NewInvoiceForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { accessToken } = useAuth();
  const [projectId, setProjectId] = React.useState(searchParams.get("project_id") ?? "");
  const [amount, setAmount] = React.useState("");
  const [dueDate, setDueDate] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [planGate, setPlanGate] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken || !projectId) return;
    setSubmitting(true);
    setError(null);
    setPlanGate(null);
    try {
      const response = await fetch(`/api/projects/${projectId}/invoices`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ amount, due_date: dueDate || null }),
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          setError(data.detail ?? "Failed to create invoice");
        }
        return;
      }
      router.push(`/billing/invoices/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4 max-w-sm">
      <div className="flex flex-col gap-1.5">
        <Label>Project</Label>
        <ProjectScopeSelect value={projectId} onChange={setProjectId} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="invoiceAmount">Amount</Label>
        <Input
          id="invoiceAmount"
          type="number"
          step="0.01"
          min="0.01"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          disabled={submitting}
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="invoiceDueDate">Due date (optional)</Label>
        <Input
          id="invoiceDueDate"
          type="date"
          value={dueDate}
          onChange={(e) => setDueDate(e.target.value)}
          disabled={submitting}
        />
      </div>
      {planGate && <PlanUpgradeNotice detail={planGate} />}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <Button type="submit" disabled={submitting || !projectId}>
        {submitting ? "Creating…" : "Create invoice"}
      </Button>
    </form>
  );
}

export default function NewInvoicePage() {
  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">New invoice</h1>
      <React.Suspense fallback={null}>
        <NewInvoiceForm />
      </React.Suspense>
    </main>
  );
}
