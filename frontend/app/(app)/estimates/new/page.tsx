"use client";

import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import { NewEstimateForm } from "@/components/estimates/NewEstimateForm";

function NewEstimateFormWithParams() {
  const params = useSearchParams();
  const projectId = params.get("project_id") ?? undefined;
  const leadId = params.get("lead_id") ?? undefined;

  return <NewEstimateForm projectId={projectId} leadId={leadId} />;
}

export default function NewEstimatePage() {
  return (
    <main className="p-6">
      <h1 className="text-xl font-semibold mb-4">New estimate</h1>
      <Suspense fallback={null}>
        <NewEstimateFormWithParams />
      </Suspense>
    </main>
  );
}
