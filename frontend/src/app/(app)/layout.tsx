import { AppShell } from "@/components/AppShell";

export default function Layout({ children }: LayoutProps<"/">) {
  return <AppShell>{children}</AppShell>;
}
