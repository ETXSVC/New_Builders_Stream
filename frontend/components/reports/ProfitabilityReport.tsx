"use client";

import * as React from "react";
import { useAuth } from "@/contexts/AuthContext";
import { useLatestOnly } from "@/lib/use-latest-only";
import { formatCurrency, formatDate } from "@/lib/format";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface ProjectRow {
  project_id: string;
  project_name: string | null;
  billed_revenue: string;
  actual_cost: string;
  profitability: string;
}

interface AgingRow {
  id: string;
  outstanding_balance: string;
  due_date: string | null;
  bucket: string;
}

interface Report {
  projects: ProjectRow[];
  ar_aging: AgingRow[];
  ap_aging: AgingRow[];
  tax_liability_estimate: string;
}

// Fixed order, oldest debt last — the sequence IS the meaning of an aging
// report, so it is declared here rather than derived from whatever buckets
// happen to appear in a response.
const BUCKETS = ["current", "1-30", "31-60", "61-90", "90+"] as const;

const BUCKET_LABELS: Record<string, string> = {
  current: "Current",
  "1-30": "1–30 days",
  "31-60": "31–60 days",
  "61-90": "61–90 days",
  "90+": "90+ days",
};

function toISODate(date: Date): string {
  // Local parts, not toISOString() — that converts to UTC first, so anyone
  // west of Greenwich gets yesterday's date for "today".
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function startOfYear(): string {
  return `${new Date().getFullYear()}-01-01`;
}

function sum(values: string[]): number {
  return values.reduce((total, value) => total + Number(value), 0);
}

export function ProfitabilityReport() {
  const { accessToken } = useAuth();
  const [startDate, setStartDate] = React.useState(startOfYear);
  const [endDate, setEndDate] = React.useState(() => toISODate(new Date()));
  const [report, setReport] = React.useState<Report | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);

  // The stale-response guard, shared with lib/use-cursor-list.ts rather
  // than hand-rolled: changing the date range twice quickly can land the
  // two responses out of order, and the slower first one would otherwise
  // win. This page cannot use the cursor-list hook itself — the report is
  // one object, not a paginated list — but the hazard is identical.
  const beginLoad = useLatestOnly();

  // Derived during render, not stored: an inverted range is a fact about
  // the two inputs, so computing it is simpler than keeping a piece of
  // state in step with them. The backend 422s on this too — catching it
  // here just puts the message next to the controls that caused it.
  const rangeInvalid = startDate > endDate;

  const load = React.useCallback(async () => {
    if (!accessToken || rangeInvalid) return;
    const isCurrent = beginLoad();
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(
        `/api/reports/profitability?start_date=${startDate}&end_date=${endDate}`,
        { headers: { Authorization: `Bearer ${accessToken}` } }
      );
      const data = await response.json();
      if (!isCurrent()) return;
      if (!response.ok) {
        setError(data.detail ?? "Failed to load the profitability report");
        setReport(null);
        return;
      }
      setReport(data);
    } catch {
      if (isCurrent()) {
        setError("Unable to reach the server. Check your connection and try again.");
        setReport(null);
      }
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [accessToken, beginLoad, endDate, rangeInvalid, startDate]);

  React.useEffect(() => {
    // Deferred into a promise callback so no setState in load's call path
    // runs synchronously inside the effect body — the same shape, and the
    // same lint rule, as lib/use-cursor-list.ts's own effects.
    void Promise.resolve().then(() => load());
  }, [load]);

  const totals = React.useMemo(() => {
    if (!report) return null;
    const revenue = sum(report.projects.map((p) => p.billed_revenue));
    const cost = sum(report.projects.map((p) => p.actual_cost));
    return { revenue, cost, profit: revenue - cost };
  }, [report]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end gap-4">
        <div className="flex flex-col gap-1">
          <Label htmlFor="report-start">From</Label>
          <Input
            id="report-start"
            type="date"
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="report-end">To</Label>
          <Input
            id="report-end"
            type="date"
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
          />
        </div>
        {loading && !rangeInvalid && <p className="text-sm text-slate-500 pb-2">Loading…</p>}
      </div>

      {(rangeInvalid || error) && (
        <p role="alert" className="text-sm text-red-700">
          {rangeInvalid ? "The start date must not be after the end date." : error}
        </p>
      )}

      {/* Figures are hidden while the range is invalid rather than left on
          screen: they belong to the last valid range, and showing them under
          changed controls invites reading them as this range's answer. */}
      {report && totals && !rangeInvalid && (
        <>
          <section aria-labelledby="totals-heading" className="flex flex-col gap-3">
            <h2 id="totals-heading" className="text-sm font-medium text-slate-700">
              For the selected period
            </h2>
            <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
              <StatTile label="Billed revenue" value={formatCurrency(totals.revenue)} />
              <StatTile label="Actual cost" value={formatCurrency(totals.cost)} />
              <StatTile
                label="Profitability"
                value={formatCurrency(totals.profit)}
                tone={totals.profit < 0 ? "loss" : "profit"}
              />
              <StatTile
                label="Est. tax liability"
                value={formatCurrency(report.tax_liability_estimate)}
                // The rate behind this is a documented placeholder
                // (DEFAULT_TAX_RATE = 0.00), so the figure is currently
                // always zero. Saying so is better than letting somebody
                // read a real-looking zero as a real answer.
                note="Placeholder rate — not yet configured"
              />
            </div>
          </section>

          <section aria-labelledby="projects-heading" className="flex flex-col gap-2">
            <h2 id="projects-heading" className="text-sm font-medium text-slate-700">
              By project
            </h2>
            {report.projects.length === 0 ? (
              <p className="text-sm text-slate-500">
                No invoices, bills or expenses fall in this period.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm border-collapse">
                  <caption className="sr-only">
                    Billed revenue, cost and profitability per project, worst first
                  </caption>
                  <thead>
                    <tr className="text-left text-slate-600 border-b border-slate-200">
                      <th scope="col" className="py-2 pr-4 font-medium">
                        Project
                      </th>
                      <th scope="col" className="py-2 px-4 font-medium text-right">
                        Billed revenue
                      </th>
                      <th scope="col" className="py-2 px-4 font-medium text-right">
                        Actual cost
                      </th>
                      <th scope="col" className="py-2 pl-4 font-medium text-right">
                        Profitability
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.projects.map((project) => {
                      const value = Number(project.profitability);
                      return (
                        <tr key={project.project_id} className="border-b border-slate-100">
                          <th scope="row" className="py-2 pr-4 font-normal text-left">
                            {project.project_name ?? (
                              <span className="text-slate-500 italic">Deleted project</span>
                            )}
                          </th>
                          <td className="py-2 px-4 text-right tabular-nums">
                            {formatCurrency(project.billed_revenue, { precise: true })}
                          </td>
                          <td className="py-2 px-4 text-right tabular-nums">
                            {formatCurrency(project.actual_cost, { precise: true })}
                          </td>
                          <td
                            className={`py-2 pl-4 text-right tabular-nums ${
                              value < 0 ? "text-red-700" : "text-slate-900"
                            }`}
                          >
                            {formatCurrency(project.profitability, { precise: true })}
                            {/* Never colour alone: a loss is stated in text
                                as well, so it survives a colourblind reader,
                                a greyscale print and forced-colors mode. */}
                            {value < 0 && <span className="sr-only"> (loss)</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <AgingTable
              heading="Money owed to you (AR)"
              description="Unpaid invoices, as of today"
              rows={report.ar_aging}
            />
            <AgingTable
              heading="Money you owe (AP)"
              description="Unpaid bills, as of today"
              rows={report.ap_aging}
            />
          </div>
        </>
      )}
    </div>
  );
}

function StatTile({
  label,
  value,
  tone,
  note,
}: {
  label: string;
  value: string;
  tone?: "profit" | "loss";
  note?: string;
}) {
  return (
    <div className="rounded-lg bg-slate-50 p-4">
      <p className="text-sm text-slate-600">{label}</p>
      <p
        className={`text-2xl font-medium tabular-nums ${
          tone === "loss" ? "text-red-700" : "text-slate-900"
        }`}
      >
        {value}
        {tone === "loss" && <span className="sr-only"> (loss)</span>}
      </p>
      {note && <p className="text-xs text-slate-500 mt-1">{note}</p>}
    </div>
  );
}

function AgingTable({
  heading,
  description,
  rows,
}: {
  heading: string;
  description: string;
  rows: AgingRow[];
}) {
  // Aggregated into the fixed bucket order. The API returns one row per
  // invoice/bill, which is the wrong grain to read — what an aging report
  // answers is "how much is how late", not "which document is which".
  const byBucket = BUCKETS.map((bucket) => {
    const matching = rows.filter((row) => row.bucket === bucket);
    return {
      bucket,
      count: matching.length,
      total: sum(matching.map((row) => row.outstanding_balance)),
      oldestDue: matching
        .map((row) => row.due_date)
        .filter((due): due is string => Boolean(due))
        .sort()[0],
    };
  });
  const grandTotal = byBucket.reduce((total, entry) => total + entry.total, 0);

  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-sm font-medium text-slate-700">{heading}</h2>
      <p className="text-xs text-slate-500">{description}</p>
      {grandTotal === 0 ? (
        <p className="text-sm text-slate-500">Nothing outstanding.</p>
      ) : (
        <table className="w-full text-sm border-collapse">
          <caption className="sr-only">{`${heading} — ${description}`}</caption>
          <thead>
            <tr className="text-left text-slate-600 border-b border-slate-200">
              <th scope="col" className="py-2 pr-4 font-medium">
                Age
              </th>
              <th scope="col" className="py-2 px-4 font-medium text-right">
                Count
              </th>
              <th scope="col" className="py-2 pl-4 font-medium text-right">
                Outstanding
              </th>
            </tr>
          </thead>
          <tbody>
            {byBucket.map((entry) => (
              <tr key={entry.bucket} className="border-b border-slate-100">
                <th scope="row" className="py-2 pr-4 font-normal text-left">
                  {BUCKET_LABELS[entry.bucket]}
                  {entry.oldestDue && (
                    <span className="block text-xs text-slate-500">
                      oldest due {formatDate(entry.oldestDue)}
                    </span>
                  )}
                </th>
                <td className="py-2 px-4 text-right tabular-nums text-slate-600">
                  {entry.count || "—"}
                </td>
                <td className="py-2 pl-4 text-right tabular-nums">
                  {entry.total ? formatCurrency(entry.total, { precise: true }) : "—"}
                </td>
              </tr>
            ))}
            <tr className="font-medium">
              <th scope="row" className="py-2 pr-4 text-left">
                Total
              </th>
              <td className="py-2 px-4 text-right tabular-nums">
                {byBucket.reduce((total, entry) => total + entry.count, 0)}
              </td>
              <td className="py-2 pl-4 text-right tabular-nums">
                {formatCurrency(grandTotal, { precise: true })}
              </td>
            </tr>
          </tbody>
        </table>
      )}
    </section>
  );
}
