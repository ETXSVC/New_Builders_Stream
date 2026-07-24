"use client";

import * as React from "react";
import Link from "next/link";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatDate } from "@/lib/format";

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

  const isAdmin = role === "admin";

  const load = React.useCallback(async () => {
    if (!accessToken) return;
    setLoading(true);
    setError(null);
    try {
      const dashboardResponse = await fetch("/api/compliance/dashboard", {
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      const dashboardData = await dashboardResponse.json();
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
        if (notificationsResponse.ok) {
          setNotifications(notificationsData.items ?? []);
        }
      }
    } catch {
      setError("Unable to reach the server. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  }, [accessToken, isAdmin]);

  React.useEffect(() => {
    void Promise.resolve().then(() => load());
  }, [load]);

  async function dismiss(notificationId: string) {
    if (!accessToken) return;
    try {
      const response = await fetch(`/api/compliance/notifications/${notificationId}/dismiss`, {
        method: "POST",
        headers: { Authorization: `Bearer ${accessToken}` },
      });
      if (response.ok) {
        setNotifications((prev) => prev.filter((n) => n.id !== notificationId));
      }
    } catch {
      // Non-critical: leave the notification in place; a reload re-fetches.
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

      {isAdmin && notifications.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold">Expiry notifications</h2>
          <ul className="flex flex-col divide-y divide-amber-200 border border-amber-300 bg-amber-50 rounded-lg">
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
