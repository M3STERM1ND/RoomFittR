// Next 16 calls `register()` once per server instance, before the first
// request is handled. See the bundled docs at
// `01-app/03-api-reference/03-file-conventions/instrumentation.md`.
import * as Sentry from "@sentry/nextjs";

export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") {
    await import("./sentry.server.config");
  }

  if (process.env.NEXT_RUNTIME === "edge") {
    await import("./sentry.edge.config");
  }
}

export const onRequestError = Sentry.captureRequestError;
