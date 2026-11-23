import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /* config options here */
  allowedDevOrigins: ["127.0.0.1", "localhost", "100.126.145.52", "*.ts.net"],
  cacheComponents: true,
  partialPrefetching: true,
  experimental: {
    proxyTimeout: 300_000,
  },
  async rewrites() {
    const backend = (
      process.env.BACKEND_INTERNAL_URL || "http://127.0.0.1:8000"
    ).replace(/\/+$/, "");
    return [
      {
        source: "/api/:path*",
        destination: `${backend}/api/:path*`,
      },
    ];
  },
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
