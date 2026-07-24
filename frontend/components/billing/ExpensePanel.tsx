"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ProjectScopeSelect } from "@/components/billing/ProjectScopeSelect";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";
import { formatCurrency, formatDate } from "@/lib/format";

interface ExpenseRow {
  id: string;
  description: string;
  amount: string;
  incurred_on: string;
}

// Expenses are list+create only (no detail endpoint), so creation is an
// inline form here rather than a separate page.
export function ExpensePanel() {
  const { accessToken } = useAuth();
  const [projectId, setProjectId] = React.useState("");
  const [expenses, setExpenses] = React.useState<ExpenseRow[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [planGate, setPlanGate] = React.useState<string | null>(null);

  const [description, setDescription] = React.useState("");
  const [amount, setAmount] = React.useState("");
  const [incurredOn, setIncurredOn] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const requestGenRef = React.useRef(0);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken || !projectId) return;
      const generation = replace ? ++requestGenRef.current : requestGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects/${projectId}/expenses?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load expenses");
          return;
        }
        setExpenses((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === requestGenRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === requestGenRef.current) setLoading(false);
      }
    },
    [accessToken, projectId]
  );

  React.useEffect(() => {
    // Deferred so no setState runs synchronously inside the effect body
    // (react-hooks/set-state-in-effect), same pattern as the estimates page.
    void Promise.resolve().then(() => {
      setExpenses([]);
      setNextCursor(null);
      if (projectId) void load(null, true);
    });
  }, [projectId, load]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken || !projectId) return;
    setSubmitting(true);
    setError(null);
    setPlanGate(null);
    try {
      const response = await fetch(`/api/projects/${projectId}/expenses`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ description, amount, incurred_on: incurredOn }),
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          setError(data.detail ?? "Failed to record expense");
        }
        return;
      }
      setDescription("");
      setAmount("");
      setIncurredOn("");
      await load(null, true);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <ProjectScopeSelect value={projectId} onChange={setProjectId} />
      {planGate && <PlanUpgradeNotice detail={planGate} />}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!projectId && (
        <p className="text-sm text-slate-600">Select a project to see and record its expenses.</p>
      )}
      {projectId && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="expenseDescription">Description</Label>
            <Input
              id="expenseDescription"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={submitting}
              required
              className="w-64"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="expenseAmount">Amount</Label>
            <Input
              id="expenseAmount"
              type="number"
              step="0.01"
              min="0.01"
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              disabled={submitting}
              required
              className="w-32"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="expenseDate">Incurred on</Label>
            <Input
              id="expenseDate"
              type="date"
              value={incurredOn}
              onChange={(e) => setIncurredOn(e.target.value)}
              disabled={submitting}
              required
              className="w-40"
            />
          </div>
          <Button type="submit" disabled={submitting}>
            {submitting ? "Recording…" : "Record expense"}
          </Button>
        </form>
      )}
      {projectId && !loading && expenses.length === 0 && !error && (
        <p className="text-sm text-slate-600">No expenses for this project yet.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
        {expenses.map((expense) => (
          <li key={expense.id} className="flex items-center gap-4 px-4 py-3">
            <span className="flex-1 text-sm font-medium">{expense.description}</span>
            <span className="text-sm text-slate-500">{formatDate(expense.incurred_on)}</span>
            <span className="text-sm text-slate-600">{formatCurrency(expense.amount)}</span>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={() => load(nextCursor, false)} disabled={loading}>
          Load more
        </Button>
      )}
    </div>
  );
}
