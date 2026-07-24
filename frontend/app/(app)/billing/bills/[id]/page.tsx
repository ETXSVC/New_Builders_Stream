"use client";

import * as React from "react";
import { useParams } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { StatusBadge } from "@/components/ui/status-badge";
import { PlanUpgradeNotice, isPlanGateError } from "@/components/billing/PlanUpgradeNotice";
import { formatCurrency, formatDate } from "@/lib/format";

interface Payment {
  id: string;
  amount: string;
  paid_date: string;
}

interface BillDetail {
  id: string;
  bill_number: string | null;
  vendor_name: string | null;
  amount: string;
  status: string;
  due_date: string | null;
  outstanding_balance: string;
  payments: Payment[];
}

export default function BillDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { accessToken, role } = useAuth();
  const [bill, setBill] = React.useState<BillDetail | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [planGate, setPlanGate] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [confirmingVoid, setConfirmingVoid] = React.useState(false);

  const [paymentAmount, setPaymentAmount] = React.useState("");
  const [paymentDate, setPaymentDate] = React.useState("");

  const canAct = role === "admin" || role === "accountant";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/bills/${id}`, {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to load bill");
        return;
      }
      setBill(data);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, id]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function act(path: string, body?: unknown) {
    if (busy || !accessToken) return;
    setBusy(true);
    setError(null);
    setPlanGate(null);
    try {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok) {
        if (isPlanGateError(response.status, data.detail)) {
          setPlanGate(data.detail);
        } else {
          setError(data.detail ?? "Action failed");
        }
        return;
      }
      setPaymentAmount("");
      setPaymentDate("");
      setConfirmingVoid(false);
      await load();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!bill) {
    return (
      <main className="p-6">
        {error ? (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </main>
    );
  }

  const isOpen = bill.status !== "void" && bill.status !== "paid";

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">
          {bill.vendor_name ?? "Subcontractor bill"}
          {bill.bill_number ? ` · ${bill.bill_number}` : ""}
        </h1>
        <StatusBadge status={bill.status} />
      </div>
      <dl className="flex flex-col gap-2 rounded-lg border border-slate-200 p-4 text-sm max-w-md">
        <div className="flex justify-between">
          <dt className="text-slate-600">Amount</dt>
          <dd className="font-medium">{formatCurrency(bill.amount)}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-slate-600">Outstanding</dt>
          <dd>{formatCurrency(bill.outstanding_balance)}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-slate-600">Due date</dt>
          <dd>{formatDate(bill.due_date)}</dd>
        </div>
      </dl>

      {planGate && <PlanUpgradeNotice detail={planGate} />}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {canAct && isOpen && (
        <div className="flex flex-col gap-4">
          <div className="flex items-center gap-3">
            {confirmingVoid ? (
              <span className="flex items-center gap-2 text-sm">
                Void this bill?
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => act(`/api/bills/${bill.id}/void`)}
                  disabled={busy}
                >
                  Yes, void
                </Button>
                <Button variant="ghost" size="sm" onClick={() => setConfirmingVoid(false)}>
                  Cancel
                </Button>
              </span>
            ) : (
              <Button variant="outline" onClick={() => setConfirmingVoid(true)} disabled={busy}>
                Void
              </Button>
            )}
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              void act(`/api/bills/${bill.id}/payments`, {
                amount: paymentAmount,
                paid_date: paymentDate,
              });
            }}
            className="flex flex-wrap items-end gap-3"
          >
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="paymentAmount">Payment amount</Label>
              <Input
                id="paymentAmount"
                type="number"
                step="0.01"
                min="0.01"
                value={paymentAmount}
                onChange={(e) => setPaymentAmount(e.target.value)}
                disabled={busy}
                required
                className="w-36"
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="paymentDate">Paid on</Label>
              <Input
                id="paymentDate"
                type="date"
                value={paymentDate}
                onChange={(e) => setPaymentDate(e.target.value)}
                disabled={busy}
                required
                className="w-40"
              />
            </div>
            <Button type="submit" disabled={busy}>
              Record payment
            </Button>
          </form>
        </div>
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">Payments</h2>
        {bill.payments.length === 0 ? (
          <p className="text-sm text-slate-600">No payments recorded.</p>
        ) : (
          <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
            {bill.payments.map((payment) => (
              <li key={payment.id} className="flex items-center gap-4 px-4 py-3 text-sm">
                <span className="flex-1">{formatDate(payment.paid_date)}</span>
                <span>{formatCurrency(payment.amount)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}
