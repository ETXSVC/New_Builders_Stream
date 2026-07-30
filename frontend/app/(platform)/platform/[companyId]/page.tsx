import { PlatformShell } from "@/components/platform/PlatformShell";
import { TenantDetailView } from "@/components/platform/TenantDetailView";

export default async function PlatformTenantPage({
  params,
}: {
  params: Promise<{ companyId: string }>;
}) {
  const { companyId } = await params;
  return (
    <PlatformShell>
      <TenantDetailView companyId={companyId} />
    </PlatformShell>
  );
}
