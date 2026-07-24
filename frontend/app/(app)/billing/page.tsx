"use client";

import * as React from "react";
import { cn } from "@/lib/utils";
import { InvoiceList } from "@/components/billing/InvoiceList";
import { BillList } from "@/components/billing/BillList";
import { ExpensePanel } from "@/components/billing/ExpensePanel";
import { SubscriptionPanel } from "@/components/billing/SubscriptionPanel";

const TABS = ["Invoices", "Bills", "Expenses", "Subscription"] as const;
type Tab = (typeof TABS)[number];

export default function BillingPage() {
  const [tab, setTab] = React.useState<Tab>("Invoices");

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">Billing</h1>

      <div className="flex gap-1 border-b border-slate-200" role="tablist">
        {TABS.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={cn(
              "px-3 py-2 text-sm",
              tab === t
                ? "border-b-2 border-blue-600 font-medium text-slate-900"
                : "text-slate-600 hover:text-slate-900"
            )}
          >
            {t}
          </button>
        ))}
      </div>

      {tab === "Invoices" && <InvoiceList />}
      {tab === "Bills" && <BillList />}
      {tab === "Expenses" && <ExpensePanel />}
      {tab === "Subscription" && <SubscriptionPanel />}
    </main>
  );
}
