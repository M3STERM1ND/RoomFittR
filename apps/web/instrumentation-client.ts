// Sentry in the browser. Next 16 loads `instrumentation-client` from the app
// root before the app becomes interactive (see the bundled docs at
// `01-app/03-api-reference/03-file-conventions/instrumentation-client.md`).
import * as Sentry from "@sentry/nextjs";

Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.SENTRY_ENVIRONMENT ?? process.env.NODE_ENV,
  tracesSampleRate: process.env.NODE_ENV === "production" ? 0.1 : 1,

  // Session Replay is deliberately NOT enabled, though the Sentry wizard turns
  // it on by default. Replay records the DOM, and this app's DOM is a
  // reconstruction of the inside of someone's home plus the dimensions of it.
  // Runbook section 4 step 5 rules that out, and a replay of a room scan is a
  // worse privacy exposure than a stack trace is a debugging win. Revisit only
  // with explicit masking and a decision recorded in the Masterplan.
  integrations: [],

  sendDefaultPii: false,
  dataCollection: {
    userInfo: false,
    httpBodies: [],
  },
});

export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
