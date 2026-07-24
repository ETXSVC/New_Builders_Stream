"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ProjectScopeSelect } from "@/components/billing/ProjectScopeSelect";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";

export default function NewBillPage() {
  const router = useRouter();
  const { accessToken } = useAuth();
  const [vendorName, setVendorName] = React.useState("");
  const [projectId, setProjectId] = React.useState("");
  const [amount, setAmount] = React.useState("");
  const [billNumber, setBillNumber] = React.useState("");
  const [dueDate, setDueDate] = React.useState("");
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
      const response = await fetch("/api/bills", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          vendor_name: vendorName,
          project_id: projectId || null,
          amount,
          bill_number: billNumber || null,
          due_date: dueDate || null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          setError(data.detail ?? "Failed to create bill");
        }
        return;
      }
      router.push(`/billing/bills/${data.id}`);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">New bill</h1>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4 max-w-sm">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="vendorName">Vendor name</Label>
          <Input
            id="vendorName"
            value={vendorName}
            onChange={(e) => setVendorName(e.target.value)}
            disabled={submitting}
            required
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label>Project (optional)</Label>
          <ProjectScopeSelect value={projectId} onChange={setProjectId} />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="billAmount">Amount</Label>
          <Input
            id="billAmount"
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
          <Label htmlFor="billNumber">Bill number (optional)</Label>
          <Input
            id="billNumber"
            value={billNumber}
            onChange={(e) => setBillNumber(e.target.value)}
            disabled={submitting}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="billDueDate">Due date (optional)</Label>
          <Input
            id="billDueDate"
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
        <Button type="submit" disabled={submitting}>
          {submitting ? "Creating…" : "Create bill"}
        </Button>
      </form>
    </main>
  );
}
