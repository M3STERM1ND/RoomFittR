import type { NextConfig } from "next";
// Imported from `@sentry/nextjs/config`, not `@sentry/nextjs`: the latter is
// deprecated and stops working in v11.
import { withSentryConfig } from "@sentry/nextjs/config";

const nextConfig: NextConfig = {/* config options here */};

export default withSentryConfig(nextConfig, {
  // Org and project slugs, and the auth token, are only used at build time to
  // upload source maps. They are absent locally and in CI until SENTRY_ORG,
  // SENTRY_PROJECT and SENTRY_AUTH_TOKEN are set, and the build proceeds
  // without them rather than failing.
  org: process.env.SENTRY_ORG,
  project: process.env.SENTRY_PROJECT,
  authToken: process.env.SENTRY_AUTH_TOKEN,

  silent: !process.env.CI,
  widenClientFileUpload: true,

  // Hide source maps from the client bundle: they are uploaded to Sentry for
  // readable stack traces, not served to browsers.
  sourcemaps: { deleteSourcemapsAfterUpload: true },

  // The wizard enables a Next.js rewrite that proxies browser events through
  // our own domain to dodge ad-blockers. It is off here because it puts event
  // traffic through the same functions that serve scans, and section 1.3 budgets
  // that capacity for the product.
  tunnelRoute: undefined,

  // `disableLogger` is deprecated in favour of webpack.treeshake.removeDebugLogging,
  // which Turbopack does not support — and Next 16 builds with Turbopack. Left
  // out entirely rather than set to a value that does nothing.
});
