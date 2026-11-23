"use client";

import type { ComponentType, ReactNode } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { motion } from "motion/react";
import { Activity, Clapperboard, Film, LayoutDashboard, Link2, Radio, Scissors } from "lucide-react";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
} from "@/components/ui/sidebar";

interface NavItem {
  href: string;
  label: string;
  icon: ComponentType<{ className?: string }>;
}

const NAV_ITEMS: NavItem[] = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/streamers", label: "Streamers", icon: Radio },
  { href: "/clips", label: "Clips", icon: Film },
  { href: "/studio", label: "Studio", icon: Scissors },
  { href: "/accounts", label: "Accounts", icon: Link2 },
  { href: "/system", label: "System", icon: Activity },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const current = NAV_ITEMS.find((item) => isActive(pathname, item.href));

  return (
    <SidebarProvider className="relative z-(--z-base)">
      <Sidebar variant="sidebar" collapsible="icon">
        <SidebarHeader className="px-(--space-sm) py-(--space-md) group-data-[collapsible=icon]:px-(--space-xs)">
          <Link
            href="/"
            aria-label="Clip Pipeline, home"
            className="press flex items-center gap-(--space-sm) rounded-(--radius-inner) px-(--space-2xs) py-(--space-2xs) text-foreground group-data-[collapsible=icon]:justify-center group-data-[collapsible=icon]:px-0"
          >
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-(--color-signal) text-(--color-signal-ink)">
              <Clapperboard className="size-4" />
            </span>
            <span className="truncate font-heading text-base tracking-(--tracking-display) group-data-[collapsible=icon]:hidden">
              Clip Pipeline
            </span>
          </Link>
        </SidebarHeader>

        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu className="gap-(--space-2xs)">
                {NAV_ITEMS.map((item, i) => {
                  const active = isActive(pathname, item.href);
                  const Icon = item.icon;
                  return (
                    <SidebarMenuItem
                      key={item.href}
                      className="rise-row"
                      style={{ "--i": i } as React.CSSProperties}
                    >
                      <SidebarMenuButton
                        render={<Link href={item.href} />}
                        isActive={active}
                        tooltip={item.label}
                        className="press relative h-9 rounded-full text-(--color-ink-3) hover:bg-transparent hover:text-foreground data-active:bg-transparent data-active:text-foreground"
                      >
                        {active && (
                          <motion.span
                            layoutId="nav-marker"
                            aria-hidden="true"
                            className="absolute inset-0 rounded-full bg-(--rule-glass) shadow-[inset_0_1px_0_var(--rule-glass)]"
                            transition={{ type: "tween", duration: 0.28, ease: [0.16, 1, 0.3, 1] }}
                          />
                        )}
                        <Icon className="relative size-4" />
                        <span className="relative">{item.label}</span>
                      </SidebarMenuButton>
                    </SidebarMenuItem>
                  );
                })}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter className="px-(--space-md) py-(--space-md) text-xs leading-snug text-(--color-ink-3) group-data-[collapsible=icon]:hidden">
          <p>
            Photograph:{" "}
            <a
              href="https://commons.wikimedia.org/wiki/File:Vallee-brouillard.jpg"
              target="_blank"
              rel="noreferrer"
              className="underline-offset-2 hover:text-foreground hover:underline"
            >
              Thomas Bresson
            </a>
            , CC BY 3.0
          </p>
        </SidebarFooter>

        <SidebarRail />
      </Sidebar>

      <SidebarInset className="min-w-0">
        <header className="glass-bar sticky top-0 z-(--z-sticky-nav) flex h-12 shrink-0 items-center gap-(--space-xs) px-(--space-md) md:hidden">
          <SidebarTrigger className="press -ml-1" />
          <span className="text-sm font-medium text-foreground">{current?.label ?? "Clip Pipeline"}</span>
        </header>
        <div className="hidden px-(--space-md) pt-(--space-md) md:block">
          <SidebarTrigger className="press glass size-8 rounded-full" />
        </div>

        <main className="flex-1 px-(--space-md) pt-(--space-md) pb-(--space-3xl) md:px-(--space-xl)">
          <div key={pathname} className="mx-auto w-full max-w-(--container)">
            {children}
          </div>
        </main>
      </SidebarInset>
    </SidebarProvider>
  );
}
