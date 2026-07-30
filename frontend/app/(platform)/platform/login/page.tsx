import { PlatformLoginForm } from "@/components/platform/PlatformLoginForm";

export default function PlatformLoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col gap-6 items-center">
        <div className="flex flex-col gap-1 items-center text-center">
          <h1 className="text-xl font-semibold">Platform console</h1>
          <p className="text-sm text-slate-500 max-w-xs">
            Cross-tenant administration. Requires a platform administrator account with an
            authenticator enrolled.
          </p>
        </div>
        <PlatformLoginForm />
      </div>
    </main>
  );
}
