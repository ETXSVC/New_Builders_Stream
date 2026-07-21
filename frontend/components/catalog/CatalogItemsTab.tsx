"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { formatCurrency } from "@/lib/format";

interface CatalogItem {
  id: string;
  category: string;
  name: string;
  unit: string;
  unit_rate: string;
  is_override: boolean;
}

export function CatalogItemsTab() {
  const { accessToken, role } = useAuth();
  const [items, setItems] = React.useState<CatalogItem[]>([]);
  const [editingId, setEditingId] = React.useState<string | null>(null);
  const [editRate, setEditRate] = React.useState("");
  const [category, setCategory] = React.useState("");
  const [name, setName] = React.useState("");
  const [unit, setUnit] = React.useState("");
  const [unitRate, setUnitRate] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager";

  const loadAll = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: CatalogItem[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/catalog/items?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load catalog items");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setItems(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => loadAll());
  }, [loadAll]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/catalog/items", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ category, name, unit, unit_rate: unitRate }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create catalog item");
        return;
      }
      setCategory("");
      setName("");
      setUnit("");
      setUnitRate("");
      await loadAll();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleSaveRate(itemId: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/catalog/items/${itemId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ unit_rate: editRate }),
    });
    if (response.ok) {
      setEditingId(null);
      await loadAll();
    }
  }

  async function handleDelete(itemId: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/catalog/items/${itemId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${accessToken}` },
    });
    if (response.status === 204) {
      await loadAll();
    } else {
      const data = await response.json();
      setError(data.detail ?? "Failed to delete catalog item");
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-category">Category</Label>
            <Input id="cat-category" value={category} onChange={(e) => setCategory(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-name">Name</Label>
            <Input id="cat-name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-unit">Unit</Label>
            <Input id="cat-unit" className="w-20" value={unit} onChange={(e) => setUnit(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="cat-rate">Unit rate</Label>
            <Input id="cat-rate" className="w-28" type="number" step="0.01" value={unitRate} onChange={(e) => setUnitRate(e.target.value)} disabled={submitting} required />
          </div>
          <Button type="submit" disabled={submitting}>Add item</Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {items.map((item) => (
          <li key={item.id} className="flex items-center gap-3 px-4 py-2 text-sm">
            <span className="w-32 text-slate-500">{item.category}</span>
            <span className="flex-1">{item.name}{item.is_override && <span className="ml-2 text-xs text-blue-600">override</span>}</span>
            {editingId === item.id ? (
              <>
                <Input className="w-24 h-8" value={editRate} onChange={(e) => setEditRate(e.target.value)} />
                <Button type="button" size="sm" onClick={() => handleSaveRate(item.id)}>Save</Button>
              </>
            ) : (
              <span>{formatCurrency(item.unit_rate)}/{item.unit}</span>
            )}
            {canWrite && editingId !== item.id && (
              <>
                <button type="button" onClick={() => { setEditingId(item.id); setEditRate(item.unit_rate); }} className="text-slate-400 hover:text-slate-700">Edit</button>
                <button type="button" onClick={() => handleDelete(item.id)} className="text-slate-400 hover:text-red-600">Delete</button>
              </>
            )}
          </li>
        ))}
        {items.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No catalog items yet.</li>}
      </ul>
    </div>
  );
}
