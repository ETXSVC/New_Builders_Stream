"use client";

import * as React from "react";
import * as Sentry from "@sentry/nextjs";

// Last-resort boundary: replaces the ROOT layout when it crashes, so it
// must render its own <html>/<body> and cannot rely on app components or
// global styles.
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    console.error(error);
    // The root layout has crashed, so this is the only place this class of
    // error is observable at all. A no-op when Sentry is unconfigured.
    Sentry.captureException(error);
  }, [error]);

  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif", margin: 0 }}>
        <main
          style={{
            minHeight: "100vh",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "1.5rem",
          }}
        >
          <div style={{ textAlign: "center", maxWidth: "24rem" }}>
            <h1 style={{ fontSize: "1.25rem", fontWeight: 600 }}>Something went wrong</h1>
            <p style={{ fontSize: "0.875rem", color: "#475569" }}>
              An unexpected error occurred. Try again, or reload the page if the problem persists.
            </p>
            <button
              onClick={reset}
              style={{
                marginTop: "1rem",
                padding: "0.5rem 1rem",
                borderRadius: "0.375rem",
                border: "1px solid #cbd5e1",
                background: "#0f172a",
                color: "#fff",
                cursor: "pointer",
              }}
            >
              Try again
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
