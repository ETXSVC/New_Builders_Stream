"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";

// Route-segment error boundary: catches render/data errors anywhere under
// app/ and offers recovery instead of Next's default error screen.
export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="flex min-h-screen items-center justify-center p-6">
      <div className="flex flex-col items-center gap-4 text-center">
        <h1 className="text-xl font-semibold">Something went wrong</h1>
        <p className="text-sm text-slate-600 max-w-sm">
          An unexpected error occurred. Your data is safe — try again, or reload the page if the
          problem persists.
        </p>
        <Button onClick={reset}>Try again</Button>
      </div>
    </main>
  );
}
