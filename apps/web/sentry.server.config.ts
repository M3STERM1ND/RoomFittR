// Sentry for the Next.js server runtime. Loaded by `instrumentation.ts`.
//
// The DSN is read from the environment rather than inlined, because
// `.env.local` is dev-only and prod credentials live in Vercel (runbook rule 1).
// An absent DSN disables the SDK, which is what we want in CI and in tests.
import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.SENTRY_ENVIRONMENT ?? process.env.NODE_ENV,

  // 100% tracing is a free-tier quota fire at any real traffic. Dev keeps full
  // traces because the volume is one developer; production samples.
  tracesSampleRate: process.env.NODE_ENV === "production" ? 0.1 : 1,

  // Runbook section 4 step 5. Scans are video of people's homes, and a request
  // body or a presigned R2 URL in an event is a privacy incident, not a
  // debugging aid.
  sendDefaultPii: false,
  dataCollection: {
    userInfo: false,
    httpBodies: [],
  },
});
