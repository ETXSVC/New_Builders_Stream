"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import { StatusBadge } from "@/components/ui/status-badge";

interface Vendor {
  id: string;
  name: string;
}

interface BomLine {
  id: string;
  description: string;
  unit: string;
  quantity: string;
  quantity_received: string;
  ordered: boolean;
  vendor_id: string | null;
  status: string;
}

export function MaterialsTab({ projectId }: { projectId: string }) {
  const { accessToken, role } = useAuth();
  const [lines, setLines] = React.useState<BomLine[]>([]);
  const [vendors, setVendors] = React.useState<Vendor[]>([]);
  const [description, setDescription] = React.useState("");
  const [unit, setUnit] = React.useState("");
  const [quantity, setQuantity] = React.useState("");
  const [receiptQuantities, setReceiptQuantities] = React.useState<Record<string, string>>({});
  const [submitting, setSubmitting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const canWrite = role === "admin" || role === "project_manager";

  const loadLines = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: BomLine[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/projects/${projectId}/materials?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) {
          setError(data.detail ?? "Failed to load materials");
          return;
        }
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setLines(all);
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    }
  }, [accessToken, projectId]);

  const loadVendors = React.useCallback(async () => {
    if (!accessToken) return;
    try {
      const all: Vendor[] = [];
      let cursor: string | null = null;
      do {
        const params = new URLSearchParams();
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(`/api/vendors?${params}`, {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const data = await response.json();
        if (!response.ok) return;
        all.push(...data.items);
        cursor = data.next_cursor ?? null;
      } while (cursor);
      setVendors(all);
    } catch {
      // Vendor list is a supporting dropdown, not the primary data this
      // tab exists for — a failure here shouldn't blank out the materials
      // list itself, so it's swallowed rather than surfaced via `error`.
    }
  }, [accessToken]);

  React.useEffect(() => {
    void Promise.resolve().then(() => {
      void loadLines();
      void loadVendors();
    });
  }, [loadLines, loadVendors]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch(`/api/projects/${projectId}/materials`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ description, unit, quantity }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to add material");
        return;
      }
      setDescription("");
      setUnit("");
      setQuantity("");
      await loadLines();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleMarkOrdered(lineId: string, vendorId: string) {
    if (!accessToken) return;
    const response = await fetch(`/api/materials/${lineId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ ordered: true, vendor_id: vendorId || null }),
    });
    if (response.ok) await loadLines();
  }

  async function handleRecordReceipt(lineId: string) {
    if (!accessToken) return;
    const value = receiptQuantities[lineId];
    if (!value) return;
    const response = await fetch(`/api/materials/${lineId}/receipts`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
      body: JSON.stringify({ quantity: value }),
    });
    if (response.ok) {
      setReceiptQuantities((prev) => ({ ...prev, [lineId]: "" }));
      await loadLines();
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="material-description">Description</Label>
            <Input id="material-description" value={description} onChange={(e) => setDescription(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="material-unit">Unit</Label>
            <Input id="material-unit" className="w-24" value={unit} onChange={(e) => setUnit(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="material-quantity">Quantity</Label>
            <Input id="material-quantity" className="w-24" type="number" step="0.01" value={quantity} onChange={(e) => setQuantity(e.target.value)} disabled={submitting} required />
          </div>
          <Button type="submit" disabled={submitting}>Add material</Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {lines.map((line) => (
          <li key={line.id} className="flex flex-col gap-2 px-4 py-3 text-sm">
            <div className="flex items-center gap-3">
              <span className="flex-1">{line.description}</span>
              <span className="text-slate-500">
                {line.quantity_received} / {line.quantity} {line.unit}
              </span>
              <StatusBadge status={line.status} />
            </div>
            {canWrite && (
              <div className="flex items-center gap-2">
                {!line.ordered && (
                  <>
                    <Select
                      className="w-40"
                      defaultValue=""
                      onChange={(e) => handleMarkOrdered(line.id, e.target.value)}
                    >
                      <option value="" disabled>
                        Mark ordered…
                      </option>
                      {vendors.map((vendor) => (
                        <option key={vendor.id} value={vendor.id}>
                          {vendor.name}
                        </option>
                      ))}
                    </Select>
                  </>
                )}
                <Input
                  className="w-24"
                  type="number"
                  step="0.01"
                  placeholder="Qty received"
                  value={receiptQuantities[line.id] ?? ""}
                  onChange={(e) =>
                    setReceiptQuantities((prev) => ({ ...prev, [line.id]: e.target.value }))
                  }
                />
                <Button type="button" size="sm" variant="outline" onClick={() => handleRecordReceipt(line.id)}>
                  Record receipt
                </Button>
              </div>
            )}
          </li>
        ))}
        {lines.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No materials yet.</li>}
      </ul>
    </div>
  );
}
