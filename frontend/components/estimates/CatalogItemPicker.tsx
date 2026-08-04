"use client";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { formatCurrency } from "@/lib/format";

export interface PickableCatalogItem {
  id: string;
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
}

/**
 * The catalog, grouped by category, with a "+" per row — and nothing about
 * where the items came from.
 *
 * Split out of `CatalogPanel` when the offline capture screen needed the
 * same picker over items read from IndexedDB rather than fetched. The two
 * differ only in where the list and the search come from, so the search is
 * controlled: the online panel forwards it to the API as a query parameter
 * (the backend does the matching), while the offline screen filters the
 * cached array in place, because there is no backend to ask.
 */
export function CatalogItemPicker({
  items,
  search,
  onSearchChange,
  onAdd,
  error,
  emptyMessage = "No catalog items yet.",
}: {
  items: PickableCatalogItem[];
  search: string;
  onSearchChange: (search: string) => void;
  onAdd: (item: PickableCatalogItem) => void;
  error?: string | null;
  emptyMessage?: string;
}) {
  const grouped = new Map<string, PickableCatalogItem[]>();
  for (const item of items) {
    const list = grouped.get(item.category) ?? [];
    list.push(item);
    grouped.set(item.category, list);
  }

  return (
    <div className="flex flex-col gap-3">
      <Input
        aria-label="Search catalog"
        placeholder="Search catalog…"
        value={search}
        onChange={(e) => onSearchChange(e.target.value)}
      />
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <div className="flex flex-col gap-3 max-h-96 overflow-y-auto">
        {Array.from(grouped.entries()).map(([category, categoryItems]) => (
          <div key={category}>
            <p className="text-xs uppercase text-slate-500 font-medium mb-1">{category}</p>
            {categoryItems.map((item) => (
              <div key={item.id} className="flex items-center gap-2 py-1 text-sm">
                <span className="flex-1">
                  {item.name} · {formatCurrency(item.unit_rate)}/{item.unit}
                </span>
                <Button type="button" size="sm" variant="outline" onClick={() => onAdd(item)}>
                  +
                </Button>
              </div>
            ))}
          </div>
        ))}
        {items.length === 0 && !error && <p className="text-sm text-slate-500">{emptyMessage}</p>}
      </div>
    </div>
  );
}
