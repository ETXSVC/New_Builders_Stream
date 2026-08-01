import { LoginForm } from "@/components/auth/LoginForm";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reset?: string }>;
}) {
  // `?reset=1` is set by the reset page, which deliberately does not sign
  // the user in: the reset revoked every session the account held, so
  // minting a fresh one there would undo the point of it. This line is the
  // acknowledgement that would otherwise be missing.
  const { reset } = await searchParams;

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col gap-6 items-center">
        <h1 className="text-xl font-semibold">Log in to Builders Stream</h1>
        {reset && (
          <p role="status" className="text-sm text-green-700">
            Your password has been reset. Log in with your new password.
          </p>
        )}
        <LoginForm />
      </div>
    </main>
  );
}
