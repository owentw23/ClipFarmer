import type { Metadata } from "next";
import { Wix_Madefor_Display, Wix_Madefor_Text } from "next/font/google";
import "./globals.css";
import { AppShell } from "@/components/AppShell";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

const madeforDisplay = Wix_Madefor_Display({
  variable: "--font-madefor-display",
  subsets: ["latin"],
  weight: ["500"],
  display: "swap",
});

const madeforText = Wix_Madefor_Text({
  variable: "--font-madefor-text",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "Clip Pipeline",
  description: "Record live streams, cut them into Shorts, approve and publish them to YouTube.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${madeforDisplay.variable} ${madeforText.variable} dark h-full antialiased`}
    >
      <body className="min-h-full font-sans">
        <div className="dusk-backdrop" aria-hidden="true">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/dusk-fog.jpg" alt="" width={1920} height={1254} fetchPriority="high" />
        </div>
        <TooltipProvider>
          <AppShell>{children}</AppShell>
          <Toaster position="bottom-right" theme="dark" />
        </TooltipProvider>
      </body>
    </html>
  );
}
