import { LoginForm } from "@/components/auth/LoginForm";

export default function LoginPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col gap-6 items-center">
        <h1 className="text-xl font-semibold">Log in to Builders Stream</h1>
        <LoginForm />
      </div>
    </main>
  );
}
