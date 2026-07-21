"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { cn } from "@/lib/utils";
import { CatalogItemsTab } from "@/components/catalog/CatalogItemsTab";
import { MarkupProfilesTab } from "@/components/catalog/MarkupProfilesTab";

const TABS = ["Cost items", "Markup profiles", "PDF template"] as const;
type Tab = (typeof TABS)[number];

export default function CatalogPage() {
  const { role } = useAuth();
  const [tab, setTab] = React.useState<Tab>("Cost items");
  const visibleTabs = role === "admin" ? TABS : TABS.filter((t) => t !== "PDF template");

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">Catalog</h1>
      <div className="flex gap-1 border-b border-slate-200" role="tablist">
        {visibleTabs.map((t) => (
          <button
            key={t}
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={cn(
              "px-3 py-2 text-sm",
              tab === t ? "border-b-2 border-blue-600 font-medium text-slate-900" : "text-slate-600 hover:text-slate-900"
            )}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === "Cost items" && <CatalogItemsTab />}
      {tab === "Markup profiles" && <MarkupProfilesTab />}
      {tab === "PDF template" && role === "admin" && <PdfTemplatePlaceholder />}
    </main>
  );
}

// Replaced by BrandingTab in Task 20 — left as an inline placeholder here
// so this task's own tsc/lint/build checks pass in isolation before that
// task wires in the real component.
function PdfTemplatePlaceholder() {
  return <p className="text-sm text-slate-500">Loading…</p>;
}
