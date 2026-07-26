"use client";

import * as React from "react";
import { Tabs } from "@/components/ui/tabs";
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

      <Tabs
        idPrefix="billing"
        tabs={TABS}
        value={tab}
        onChange={setTab}
        panels={{
          Invoices: <InvoiceList />,
          Bills: <BillList />,
          Expenses: <ExpensePanel />,
          Subscription: <SubscriptionPanel />,
        }}
      />
    </main>
  );
}
