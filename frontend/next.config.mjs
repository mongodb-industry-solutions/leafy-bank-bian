/** @type {import('next').NextConfig} */
const nextConfig = {
  skipTrailingSlashRedirect: true,
  // All API proxies use Route Handlers (app/api/) so backend URLs are
  // resolved at runtime, not baked in at build time.
  //   - /api/backend/* → app/api/backend/[...path]/route.js
  //   - /api/chatbot/* → app/api/chatbot/[...path]/route.js
};

export default nextConfig;
