"use client";

import * as React from "react";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { platformSignOut } from "@/lib/platform/client";

/**
 * The console's chrome. Lives in the two authenticated pages rather than in
 * the route group's layout, because the layout also wraps the login page and
 * a "Sign out" button there would be nonsense.
 */
export function PlatformShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-6 py-3">
          <div className="flex items-baseline gap-3">
            <Link href="/platform" className="font-semibold">
              Platform console
            </Link>
            <span className="text-xs text-slate-500">
              Cross-tenant administration — every change is audited
            </span>
          </div>
          <Button variant="outline" size="sm" onClick={() => void platformSignOut()}>
            Sign out
          </Button>
        </div>
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-8">{children}</main>
    </div>
  );
}
