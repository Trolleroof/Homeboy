import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Homeboy",
  description: "Teach a SO-101 task, index its video, and retrieve the demonstration from what the camera recorded.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return <html lang="en"><body>{children}</body></html>;
}
