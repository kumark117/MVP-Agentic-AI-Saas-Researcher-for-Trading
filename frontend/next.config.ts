import type { NextConfig } from "next";

// Hosted (Vercel): /api/* is proxied to the Render API so the browser sees one origin and
// the session cookies stay first-party. Locally Caddy does this, so API_ORIGIN is unset.
const apiOrigin = process.env.API_ORIGIN?.replace(/\/+$/, "");

const nextConfig: NextConfig = {
  ...(apiOrigin && {
    async rewrites() {
      return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
    },
  }),
  cacheComponents: true,
  partialPrefetching: true,
  poweredByHeader: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
