"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { formatCurrency } from "@/lib/format";

interface CatalogItem {
  id: string;
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
}

export function CatalogPanel({ onAdd }: { onAdd: (item: CatalogItem) => void }) {
  const { accessToken } = useAuth();
  const [items, setItems] = React.useState<CatalogItem[]>([]);
  const [search, setSearch] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      // Follows next_cursor to exhaustion — the catalog panel needs the
      // whole browsable set, not one page (same pagination-completeness
      // reasoning the CRM+PM tabs settled on for lists a user must see in
      // full).
      const all: CatalogItem[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (search) params.set("search", search);
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/catalog/items?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load catalog");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setItems(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, search]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  const grouped = React.useMemo(() => {
    const groups = new Map<string, CatalogItem[]>();
    for (const item of items) {
      const list = groups.get(item.category) ?? [];
      list.push(item);
      groups.set(item.category, list);
    }
    return groups;
  }, [items]);

  return (
    <div className="flex flex-col gap-3">
      <Input
        aria-label="Search catalog"
        placeholder="Search catalog…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
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
        {items.length === 0 && !error && (
          <p className="text-sm text-slate-500">No catalog items yet.</p>
        )}
      </div>
    </div>
  );
}
