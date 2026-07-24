"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatCurrency } from "@/lib/format";

interface BillRow {
  id: string;
  bill_number: string | null;
  vendor_name: string | null;
  amount: string;
  status: string;
  outstanding_balance: string;
}

export function BillList() {
  const { accessToken } = useAuth();
  const [bills, setBills] = React.useState<BillRow[]>([]);
  const [nextCursor, setNextCursor] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const requestGenRef = React.useRef(0);

  const load = React.useCallback(
    async (cursor: string | null, replace: boolean) => {
      if (!accessToken) return;
      const generation = replace ? ++requestGenRef.current : requestGenRef.current;
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/bills?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (generation !== requestGenRef.current) return;
        if (!response.ok) {
          setError(data.detail ?? "Failed to load bills");
          return;
        }
        setBills((prev) => (replace ? data.items : [...prev, ...data.items]));
        setNextCursor(data.next_cursor);
      } catch {
        if (generation === requestGenRef.current) {
          setError("Unable to reach the server. Check your connection and try again.");
        }
      } finally {
        if (generation === requestGenRef.current) setLoading(false);
      }
    },
    [accessToken]
  );

  React.useEffect(() => {
    void Promise.resolve().then(() => load(null, true));
  }, [load]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex justify-end">
        <Link href="/billing/bills/new">
          <Button>New bill</Button>
        </Link>
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!loading && bills.length === 0 && !error && (
        <p className="text-sm text-slate-600">No bills yet — record your first vendor bill.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
        {bills.map((bill) => (
          <li key={bill.id}>
            <Link
              href={`/billing/bills/${bill.id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <span className="flex-1 text-sm font-medium">
                {bill.vendor_name ?? "Subcontractor bill"}
                {bill.bill_number ? ` · ${bill.bill_number}` : ""}
              </span>
              <span className="text-sm text-slate-600">{formatCurrency(bill.amount)}</span>
              <span className="text-sm text-slate-500">
                {formatCurrency(bill.outstanding_balance)} due
              </span>
              <StatusBadge status={bill.status} />
            </Link>
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
