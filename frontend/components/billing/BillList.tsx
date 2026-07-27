"use client";

import * as React from "react";
import Link from "next/link";
import { useCursorList } from "@/lib/use-cursor-list";
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
  const {
    items: bills,
    nextCursor,
    loading,
    error,
    loadMore,
  } = useCursorList<BillRow>({ path: "/api/bills", label: "bills" });

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
        <Button variant="outline" onClick={loadMore} disabled={loading}>
          Load more
        </Button>
      )}
    </div>
  );
}
