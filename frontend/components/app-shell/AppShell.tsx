"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import { useAuth } from "@/contexts/AuthContext";
import { Nav } from "@/components/app-shell/Nav";

// Pre-auth screens that live inside the (app) route group (they need its
// Tailwind globals and AuthProvider) but must not show the app chrome.
const PRE_AUTH_PATHS = ["/login", "/register"];

// Mounts the shared Nav above every authenticated app screen. Rendered by
// the (app) layout so role-landing pages (field_crew → /my-tasks, client →
// /projects) get navigation and logout without each page wiring Nav itself.
export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { accessToken } = useAuth();

  if (PRE_AUTH_PATHS.includes(pathname)) return <>{children}</>;

  return (
    <>
      <Nav companyId={decodeCompanyId(accessToken)} />
      {children}
    </>
  );
}

function decodeCompanyId(accessToken: string | null): string {
  if (!accessToken) return "";
  try {
    const payload = JSON.parse(atob(accessToken.split(".")[1]));
    return payload.default_company_id ?? "";
  } catch {
    return "";
  }
}
