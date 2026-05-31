import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Parts research",
  description: "Ресерч запчастей: очередь задач и куратор каталога.",
};

// Set the theme before first paint (no flash). Stored choice wins; otherwise
// follow the OS preference. Default surface is dark slate.
const themeScript = `(function(){try{var t=localStorage.getItem('theme');if(t!=='light'&&t!=='dark'){t=window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}document.documentElement.setAttribute('data-theme',t);}catch(e){document.documentElement.setAttribute('data-theme','dark');}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
