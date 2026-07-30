import { PlatformShell } from "@/components/platform/PlatformShell";
import { TenantList } from "@/components/platform/TenantList";

export default function PlatformHomePage() {
  return (
    <PlatformShell>
      <TenantList />
    </PlatformShell>
  );
}
