"use client";

import * as React from "react";
import Link from "next/link";
import { useCursorList } from "@/lib/use-cursor-list";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { ProjectScopeSelect } from "@/components/billing/ProjectScopeSelect";
import { formatCurrency } from "@/lib/format";

interface InvoiceRow {
  id: string;
  invoice_number: string;
  amount: string;
  status: string;
  due_date: string | null;
  outstanding_balance: string;
}

export function InvoiceList() {
  const [projectId, setProjectId] = React.useState("");
  // `enabled` carries what the old effect did by hand: nothing is fetched
  // until a project is chosen, and deselecting one clears the rows rather
  // than leaving the previous project's invoices on screen.
  const {
    items: invoices,
    nextCursor,
    loading,
    error,
    loadMore,
  } = useCursorList<InvoiceRow>({
    path: `/api/projects/${projectId}/invoices`,
    label: "invoices",
    enabled: Boolean(projectId),
  });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-4">
        <ProjectScopeSelect value={projectId} onChange={setProjectId} />
        {projectId && (
          <Link href={`/billing/invoices/new?project_id=${projectId}`}>
            <Button>New invoice</Button>
          </Link>
        )}
      </div>
      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}
      {!projectId && (
        <p className="text-sm text-slate-600">Select a project to see its invoices.</p>
      )}
      {projectId && !loading && invoices.length === 0 && !error && (
        <p className="text-sm text-slate-600">No invoices for this project yet.</p>
      )}
      <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
        {invoices.map((invoice) => (
          <li key={invoice.id}>
            <Link
              href={`/billing/invoices/${invoice.id}`}
              className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
            >
              <span className="flex-1 text-sm font-medium">{invoice.invoice_number}</span>
              <span className="text-sm text-slate-600">{formatCurrency(invoice.amount)}</span>
              <span className="text-sm text-slate-500">
                {formatCurrency(invoice.outstanding_balance)} due
              </span>
              <StatusBadge status={invoice.status} />
            </Link>
          </li>
        ))}
      </ul>
      {nextCursor && (
        <Button variant="outline" onClick={loadMore} disabled={loading}>
          Load more
        </Button>
      )}
    </div>
  );
}
