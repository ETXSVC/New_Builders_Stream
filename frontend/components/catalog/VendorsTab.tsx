"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useCursorAll } from "@/lib/use-cursor-list";

interface Vendor {
  id: string;
  name: string;
  contact_email: string | null;
  contact_phone: string | null;
}

export function VendorsTab() {
  const { accessToken, role } = useAuth();
  // Every page, not the first one: this list is chosen FROM (a vendor
  // picker on a material line), so "the first 25 and a button" would be
  // the wrong shape. `useCursorAll` is the shared walk — see its docstring
  // for why the two loaders are separate hooks rather than one with a flag.
  const {
    items: vendors,
    error,
    setError,
    reload,
  } = useCursorAll<Vendor>({ path: "/api/vendors", label: "vendors" });
  const [name, setName] = React.useState("");
  const [contactEmail, setContactEmail] = React.useState("");
  const [contactPhone, setContactPhone] = React.useState("");
  const [submitting, setSubmitting] = React.useState(false);

  const canWrite = role === "admin" || role === "project_manager";

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (submitting || !accessToken) return;
    setError(null);
    setSubmitting(true);
    try {
      const response = await fetch("/api/vendors", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({
          name,
          contact_email: contactEmail || null,
          contact_phone: contactPhone || null,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        setError(data.detail ?? "Failed to create vendor");
        return;
      }
      setName("");
      setContactEmail("");
      setContactPhone("");
      reload();
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {canWrite && (
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="vendor-name">Name</Label>
            <Input id="vendor-name" value={name} onChange={(e) => setName(e.target.value)} disabled={submitting} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="vendor-email">Contact email</Label>
            <Input id="vendor-email" type="email" value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} disabled={submitting} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="vendor-phone">Contact phone</Label>
            <Input id="vendor-phone" value={contactPhone} onChange={(e) => setContactPhone(e.target.value)} disabled={submitting} />
          </div>
          <Button type="submit" disabled={submitting}>Add vendor</Button>
        </form>
      )}
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg">
        {vendors.map((vendor) => (
          <li key={vendor.id} className="flex items-center gap-3 px-4 py-2 text-sm">
            <span className="flex-1">{vendor.name}</span>
            <span className="text-slate-500">{vendor.contact_email ?? "—"}</span>
            <span className="text-slate-500">{vendor.contact_phone ?? "—"}</span>
          </li>
        ))}
        {vendors.length === 0 && <li className="px-4 py-3 text-sm text-slate-500">No vendors yet.</li>}
      </ul>
    </div>
  );
}
