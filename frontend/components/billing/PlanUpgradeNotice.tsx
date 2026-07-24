import * as React from "react";

// Tier-gated backend writes 403 with a "...requires the <tier> plan..."
// detail message. Rendering that as a styled upgrade callout (instead of a
// generic red error) is shared across Billing, Compliance, and
// Integrations. isPlanGateError decides which rendering a caught error
// deserves.
export function isPlanGateError(status: number, detail: string | undefined): boolean {
  return status === 403 && typeof detail === "string" && detail.toLowerCase().includes("plan");
}

export function PlanUpgradeNotice({ detail }: { detail: string }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"
    >
      <p className="font-medium">Plan upgrade required</p>
      <p>{detail}</p>
    </div>
  );
}
