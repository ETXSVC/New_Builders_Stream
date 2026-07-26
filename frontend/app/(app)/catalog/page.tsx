"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Tabs } from "@/components/ui/tabs";
import { CatalogItemsTab } from "@/components/catalog/CatalogItemsTab";
import { MarkupProfilesTab } from "@/components/catalog/MarkupProfilesTab";
import { VendorsTab } from "@/components/catalog/VendorsTab";
import { BrandingTab } from "@/components/catalog/BrandingTab";

const TABS = ["Cost items", "Markup profiles", "Vendors", "PDF template"] as const;
type Tab = (typeof TABS)[number];

export default function CatalogPage() {
  const { role } = useAuth();
  const [tab, setTab] = React.useState<Tab>("Cost items");
  const visibleTabs = role === "admin" ? TABS : TABS.filter((t) => t !== "PDF template");

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <h1 className="text-xl font-semibold">Catalog</h1>
      <Tabs
        idPrefix="catalog"
        tabs={visibleTabs}
        value={tab}
        onChange={setTab}
        panels={{
          "Cost items": <CatalogItemsTab />,
          "Markup profiles": <MarkupProfilesTab />,
          Vendors: <VendorsTab />,
          // `visibleTabs` already withholds this tab from non-admins, so
          // the role check here is belt-and-braces against `tab` holding a
          // stale value if the role changes mid-session.
          "PDF template": role === "admin" ? <BrandingTab /> : null,
        }}
      />
    </main>
  );
}
