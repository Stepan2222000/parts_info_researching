"use client";

import { useEffect, useState } from "react";
import { IconMoon, IconSun } from "@/lib/icons";

type Theme = "light" | "dark";

// The no-flash inline script in layout already set data-theme before paint;
// here we just read it and let the user flip + persist the choice.
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    const current = (document.documentElement.getAttribute("data-theme") as Theme) || "dark";
    setTheme(current);
  }, []);

  function toggle() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch { /* private mode */ }
    setTheme(next);
  }

  return (
    <button className="theme-toggle" onClick={toggle} aria-label="Переключить тему">
      {theme === "dark" ? <IconSun /> : <IconMoon />}
      {theme === "dark" ? "Светлая тема" : "Тёмная тема"}
    </button>
  );
}
