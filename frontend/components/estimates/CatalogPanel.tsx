"use client";

import * as React from "react";
import { useCursorAll } from "@/lib/use-cursor-list";
import { CatalogItemPicker, type PickableCatalogItem } from "./CatalogItemPicker";

/**
 * The catalog picker, fed from the API.
 *
 * The rendering moved to `CatalogItemPicker` when the offline capture screen
 * needed the same picker over cached items; what stays here is the half that
 * is actually about this screen — walking the cursor to exhaustion, and
 * letting the BACKEND do the search. The capture screen filters in memory
 * instead, having no backend to ask.
 */
export function CatalogPanel({ onAdd }: { onAdd: (item: PickableCatalogItem) => void }) {
  const [search, setSearch] = React.useState("");

  // Follows next_cursor to exhaustion — the catalog panel needs the whole
  // browsable set, not one page (same pagination-completeness reasoning the
  // CRM+PM tabs settled on for lists a user must see in full).
  const { items, error } = useCursorAll<PickableCatalogItem>({
    path: "/api/catalog/items",
    params: { search },
    label: "catalog",
  });

  return (
    <CatalogItemPicker
      items={items}
      search={search}
      onSearchChange={setSearch}
      onAdd={onAdd}
      error={error}
    />
  );
}
