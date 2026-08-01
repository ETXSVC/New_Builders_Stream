"use client";

import { ProfitabilityReport } from "@/components/reports/ProfitabilityReport";

export default function ReportsPage() {
  return (
    <main className="p-6 flex flex-col gap-5 max-w-5xl">
      <h1 className="text-xl font-semibold">Reports</h1>
      <ProfitabilityReport />
    </main>
  );
}
