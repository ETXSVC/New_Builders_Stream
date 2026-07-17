import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Builders Stream | Construction work, in one clear flow",
  description: "A connected operating system for growing construction and renovation teams.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
