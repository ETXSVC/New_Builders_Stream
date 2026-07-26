"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDate } from "@/lib/format";
import { useLatestOnly } from "@/lib/use-latest-only";

interface DashboardItem {
  compliance_document_id: string;
  subcontractor_id: string;
  subcontractor_name: string;
  doc_type: string;
  expires_on: string;
  status: string;
}

interface NotificationItem {
  id: string;
  subcontractor_name: string;
  doc_type: string;
  expires_on: string;
  threshold: number;
  fired_at: string;
  read_at: string | null;
}

const DOC_TYPE_LABELS: Record<string, string> = {
  insurance_certificate: "Insurance certificate",
  license: "License",
};

export default function CompliancePage() {
  const { accessToken, role } = useAuth();
  const [items, setItems] = React.useState<DashboardItem[]>([]);
  const [notifications, setNotifications] = React.useState<NotificationItem[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  // Kept separate from `error` on purpose. The dashboard and the
  // notifications are two independent fetches, and a failure in one says
  // nothing about the other — folding both into one banner would either
  // blank a section that loaded fine or hide a failure behind a page that
  // looks healthy.
  const [notificationsError, setNotificationsError] = React.useState<string | null>(null);

  const isAdmin = role === "admin";

  const beginLoad = useLatestOnly();

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    // setLoading/setError above the awaits are pre-load resets, not results,
    // so they stay unguarded — only what comes back from the network is.
    const isCurrent = beginLoad();
    setLoading(true);
    setError(null);
    setNotificationsError(null);
    try {
      const dashboardResponse = await fetch("/api/compliance/dashboard", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const dashboardData = await dashboardResponse.json();
      if (!isCurrent()) return;
      if (!dashboardResponse.ok) {
        setError(dashboardData.detail ?? "Failed to load compliance dashboard");
        return;
      }
      setItems(dashboardData.items ?? []);

      // Notifications are admin-only on the backend — don't request them
      // for roles that would just get a 403.
      if (isAdmin) {
        const notificationsResponse = await fetch("/api/compliance/notifications?unread_only=true", {
          headers: { Authorization: `Bearer ${accessToken}` },
        });
        const notificationsData = await notificationsResponse.json();
        if (!isCurrent()) return;
        if (notificationsResponse.ok) {
          setNotifications(notificationsData.items ?? []);
        } else {
          // Without this the section simply renders nothing, and "no
          // documents are expiring" is indistinguishable from "we could
          // not find out whether any are" — the more dangerous of the two
          // to show silently on a compliance page.
          setNotifications([]);
          setNotificationsError(
            notificationsData.detail ?? "Couldn't load expiry notifications.",
          );
        }
      }
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, [accessToken, beginLoad, isAdmin]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function dismiss(notificationId: string) {
    if (!accessToken) return;
    setNotificationsError(null);
    try {
      const response = await fetch(`/api/compliance/notifications/${notificationId}/dismiss`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (response.ok) {
        setNotifications((prev) => prev.filter((n) => n.id !== notificationId));
        return;
      }
      // The row stays put on failure — which is correct, since the
      // notification genuinely wasn't dismissed — but on its own that is
      // indistinguishable from the click not registering. Saying so is the
      // difference between "nothing happened" and "this didn't work."
      const data = await response.json().catch(() => ({}));
      setNotificationsError(data.detail ?? "Couldn't dismiss that notification. Please try again.");
    } catch {
      setNotificationsError("Unable to reach the server. Check your connection and try again.");
    }
  }

  return (
    <main className="p-6 flex flex-col gap-5 max-w-3xl">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Compliance</h1>
        <Link href="/subcontractors" className="text-sm text-slate-600 underline hover:text-slate-900">
          Manage subcontractors
        </Link>
      </div>

      {error && (
        <p role="alert" aria-live="assertive" className="text-sm text-red-600">
          {error}
        </p>
      )}

      {/* Rendered when there is EITHER something to show or something to
          say. Gating the whole section on `notifications.length > 0` would
          hide the error too, which is the failure this section exists to
          make visible. */}
      {isAdmin && (notifications.length > 0 || notificationsError) && (
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">Expiry notifications</h2>
          {notificationsError && (
            <p role="alert" aria-live="assertive" className="text-sm text-red-600">
              {notificationsError}{" "}
              <button
                type="button"
                onClick={() => void load()}
                className="underline hover:text-red-700"
              >
                Retry
              </button>
            </p>
          )}
          <ul className="flex flex-col divide-y divide-amber-200 border border-amber-300 bg-amber-50 rounded-lg empty:hidden">
            {notifications.map((n) => (
              <li key={n.id} className="flex items-center gap-4 px-4 py-3 text-sm">
                <span className="flex-1">
                  {n.subcontractor_name} — {DOC_TYPE_LABELS[n.doc_type] ?? n.doc_type} expires{" "}
                  {formatDate(n.expires_on)}
                </span>
                <Button variant="ghost" size="sm" onClick={() => dismiss(n.id)}>
                  Dismiss
                </Button>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">Document status</h2>
        {!loading && items.length === 0 && !error && (
          <p className="text-sm text-slate-600">
            No compliance documents on file yet — upload insurance certificates and licenses from a
            subcontractor&apos;s page.
          </p>
        )}
        <ul className="flex flex-col divide-y divide-slate-200 border border-slate-200 rounded-lg empty:hidden">
          {items.map((item) => (
            <li key={item.compliance_document_id}>
              <Link
                href={`/subcontractors/${item.subcontractor_id}`}
                className="flex items-center gap-4 px-4 py-3 hover:bg-slate-50"
              >
                <span className="flex-1 text-sm font-medium">{item.subcontractor_name}</span>
                <span className="text-sm text-slate-600">
                  {DOC_TYPE_LABELS[item.doc_type] ?? item.doc_type}
                </span>
                <span className="text-sm text-slate-500">{formatDate(item.expires_on)}</span>
                <StatusBadge status={item.status} />
              </Link>
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}
