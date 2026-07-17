import "./globals.css";
import { AuthProvider } from "@/contexts/AuthContext";

export default function AppLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <AuthProvider>{children}</AuthProvider>;
}
