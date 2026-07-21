"use client";

import { useSearchParams } from "next/navigation";
import { NewEstimateForm } from "@/components/estimates/NewEstimateForm";

export default function NewEstimatePage() {
  const params = useSearchParams();
  const projectId = params.get("project_id") ?? undefined;
  const leadId = params.get("lead_id") ?? undefined;

  return (
    <main className="p-6">
      <h1 className="text-xl font-semibold mb-4">New estimate</h1>
      <NewEstimateForm projectId={projectId} leadId={leadId} />
    </main>
  );
}
