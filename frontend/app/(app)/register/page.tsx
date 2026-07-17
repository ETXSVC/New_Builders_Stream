import { RegisterForm } from "@/components/auth/RegisterForm";

export default function RegisterPage() {
  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col gap-6 items-center">
        <h1 className="text-xl font-semibold">Create your Builders Stream account</h1>
        <RegisterForm />
      </div>
    </main>
  );
}
