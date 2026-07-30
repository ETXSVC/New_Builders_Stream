"use client";

import * as React from "react";
import Link from "next/link";
import type { components } from "@/lib/api/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CreateTenantModal } from "@/components/platform/CreateTenantModal";
import { EditTenantModal } from "@/components/platform/EditTenantModal";
import { endPlatformSession } from "@/lib/platform/client";
import { useCursorListCore } from "@/lib/use-cursor-list";

type TenantSummary = components["schemas"]["TenantSummary"];

export function TenantList() {
  const [creating, setCreating] = React.useState(false);
  const [editing, setEditing] = React.useState<TenantSummary | null>(null);
  const [showDeleted, setShowDeleted] = React.useState(false);
  // `search` is what is typed; `applied` is what is queried. Submitting
  // rather than debouncing keeps one request per intent — this list is an
  // operator tool, not a type-ahead, and every keystroke firing a
  // cross-tenant query is a cost with no payoff.
  const [search, setSearch] = React.useState("");
  const [applied, setApplied] = React.useState("");
  const [rootsOnly, setRootsOnly] = React.useState(false);

  const { items, nextCursor, loading, error, loadMore, reload } =
    useCursorListCore<TenantSummary>({
    path: "/api/platform/companies",
    label: "tenants",
    // Inline object is fine: the hook depends on the SERIALISED query, not on
    // this object's identity — see its docstring.
    params: {
      search: applied || undefined,
      roots_only: rootsOnly ? "true" : undefined,
      include_deleted: showDeleted ? "true" : undefined,
    },
    // Same reaction the detail view gets from `platformFetch`: a 401 here is
    // an expired token or a privilege revoked mid-session, and either way
    // reporting "failed to load" on a console you are no longer signed in to
    // is the wrong answer. Module-level function, so the reference is stable.
    onUnauthorized: endPlatformSession,
  });

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Tenants</h1>
          <p className="text-sm text-slate-500">
            Entitlements are held by each tree&apos;s root company. Child branches inherit them and
            cannot be changed on their own.
          </p>
        </div>
        <Button onClick={() => setCreating(true)}>New tenant</Button>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setApplied(search.trim());
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="tenant-search">Search by name</Label>
            <Input
              id="tenant-search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="e.g. Acme"
              className="w-64"
            />
          </div>
          <Button type="submit" variant="outline">
            Search
          </Button>
          {applied && (
            <Button
              type="button"
              variant="ghost"
              onClick={() => {
                setSearch("");
                setApplied("");
              }}
            >
              Clear
            </Button>
          )}
        </form>

        <label className="flex items-center gap-2 pb-2.5 text-sm">
          <input
            type="checkbox"
            checked={rootsOnly}
            onChange={(e) => setRootsOnly(e.target.checked)}
            className="h-4 w-4"
          />
          Root companies only
        </label>

        <label className="flex items-center gap-2 pb-2.5 text-sm">
          <input
            type="checkbox"
            checked={showDeleted}
            onChange={(e) => setShowDeleted(e.target.checked)}
            className="h-4 w-4"
          />
          Include out of service
        </label>
      </div>

      {error && (
        <p role="alert" className="text-sm text-red-600">
          {error}
        </p>
      )}

      <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table className="w-full text-sm">
          <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-4 py-2 font-medium">Company</th>
              <th className="px-4 py-2 font-medium">Tier</th>
              <th className="px-4 py-2 font-medium">Status</th>
              <th className="px-4 py-2 font-medium">Seats</th>
              <th className="px-4 py-2 font-medium">Users</th>
              <th className="px-4 py-2 font-medium">Writes</th>
              <th className="px-4 py-2 font-medium">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {items.map((tenant) => (
              <tr key={tenant.company_id} className="border-b border-slate-100 last:border-0">
                <td className="px-4 py-2">
                  <Link
                    href={`/platform/${tenant.company_id}`}
                    className="font-medium hover:underline"
                  >
                    {tenant.name}
                  </Link>
                  {!tenant.is_root && (
                    <span className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">
                      branch
                    </span>
                  )}
                  {tenant.deleted_at && (
                    <span
                      className="ml-2 rounded bg-red-100 px-1.5 py-0.5 text-xs text-red-800"
                      title={`Taken out of service ${new Date(tenant.deleted_at).toLocaleString()}`}
                    >
                      out of service
                    </span>
                  )}
                </td>
                <td className="px-4 py-2">{tenant.tier ?? "—"}</td>
                <td className="px-4 py-2">
                  {tenant.status ?? "—"}
                  {tenant.manual_status_override && (
                    <span
                      className="ml-2 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800"
                      title="Set by an operator; Stripe events will not overwrite it"
                    >
                      manual
                    </span>
                  )}
                </td>
                <td className="px-4 py-2">{tenant.included_seats ?? "—"}</td>
                <td className="px-4 py-2">{tenant.user_count}</td>
                <td className="px-4 py-2">
                  {tenant.writes_enabled ? (
                    <span className="text-slate-600">enabled</span>
                  ) : (
                    <span className="font-medium text-red-600">read-only</span>
                  )}
                </td>
                <td className="px-4 py-2 text-right">
                  <Button variant="outline" size="sm" onClick={() => setEditing(tenant)}>
                    Edit
                  </Button>
                </td>
              </tr>
            ))}
            {!loading && items.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-slate-500">
                  No tenants match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-3">
        {nextCursor && (
          <Button variant="outline" onClick={loadMore} disabled={loading}>
            {loading ? "Loading…" : "Load more"}
          </Button>
        )}
        {loading && !nextCursor && <span className="text-sm text-slate-500">Loading…</span>}
      </div>

      <CreateTenantModal
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={reload}
      />
      {/* Keyed by tenant so the form's state initialisers re-run per row
          rather than needing an effect to re-seed them — the same trick
          TenantDetailView uses for its subscription card. */}
      {editing && (
        <EditTenantModal
          key={editing.company_id}
          tenant={editing}
          onClose={() => setEditing(null)}
          onSaved={reload}
        />
      )}
    </div>
  );
}
