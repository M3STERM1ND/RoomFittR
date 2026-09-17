import { Navbar } from "@/components/site/navbar";
import { Hero } from "@/components/site/hero";
import { RoomPreview } from "@/components/site/room-preview";
import { HowItWorks } from "@/components/site/how-it-works";
import { Features } from "@/components/site/features";
import { Why } from "@/components/site/why";
import { Faq } from "@/components/site/faq";
import { FinalCta } from "@/components/site/final-cta";
import { Footer } from "@/components/site/footer";

export default function Home() {
  return (
    <>
      <Navbar />
      <main className="flex-1">
        <Hero />
        <RoomPreview />
        <HowItWorks />
        <Features />
        <Why />
        <Faq />
        <FinalCta />
      </main>
      <Footer />
    </>
  );
}
