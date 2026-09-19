import type { Metadata } from "next";
import { Jost, Nunito } from "next/font/google";
import "./globals.css";

const jost = Jost({
  variable: "--font-jost",
  subsets: ["latin"],
  weight: ["300", "400", "500"],
  display: "swap",
});

const nunito = Nunito({
  variable: "--font-nunito",
  subsets: ["latin"],
  weight: ["400", "600", "700"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "RoomFittr — See it in your room before you buy it",
  description:
    "Record a walkthrough of your room and get it back as a navigable 3D space, cleared out and furnished with real products that fit your measurements and your budget.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html
      lang="en"
      className={`${jost.variable} ${nunito.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col bg-mist-50 text-hur-700">
        {/* Scroll reveals animate inline styles, so nothing in the stylesheet
            can reach them once the bundle fails to run. This does: an author
            !important rule outranks an inline declaration, so a visitor
            without JS gets every section at full opacity. */}
        <noscript>
          <style
            dangerouslySetInnerHTML={{
              __html: ".reveal{opacity:1!important;transform:none!important}",
            }}
          />
        </noscript>
        {children}
      </body>
    </html>
  );
}
