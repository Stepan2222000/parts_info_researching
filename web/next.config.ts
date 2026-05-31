import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Standalone output for a slim production Docker image (stage 4 deploy).
  output: "standalone",
};

export default nextConfig;
