"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { IconChat, IconQueue, Sparkle } from "@/lib/icons";
import { ThemeToggle } from "./ThemeToggle";

export function Rail({ children, footer }: { children?: React.ReactNode; footer?: React.ReactNode }) {
  const path = usePathname();
  return (
    <aside className="rail">
      <div className="brand">
        <span className="mark"><Sparkle width={18} height={18} /></span>
        Parts research
      </div>
      <Link href="/" className={`nav-link ${path === "/" ? "active" : ""}`}>
        <IconQueue /> Очередь
      </Link>
      <Link href="/chat" className={`nav-link ${path.startsWith("/chat") ? "active" : ""}`}>
        <IconChat /> Куратор
      </Link>
      {children}
      <div className="rail-bottom">
        {footer}
        <ThemeToggle />
      </div>
    </aside>
  );
}

export function WorkerStatus({ alive }: { alive: boolean }) {
  return (
    <div className="worker-status">
      <span className={`dot ${alive ? "live" : "off"}`} />
      {alive ? "Воркер на связи" : "Воркер офлайн"}
    </div>
  );
}
