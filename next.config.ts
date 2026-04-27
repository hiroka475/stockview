import type { NextConfig } from "next";
import { initOpenNextCloudflareForDev } from "@opennextjs/cloudflare";

const nextConfig: NextConfig = {
  async redirects() {
    return [
      { source: "/", destination: "/index.html", permanent: false },
    ];
  },
};

initOpenNextCloudflareForDev();

export default nextConfig;
