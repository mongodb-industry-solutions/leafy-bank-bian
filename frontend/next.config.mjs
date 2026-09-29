/** @type {import('next').NextConfig} */
const nextConfig = {
  skipTrailingSlashRedirect: true,
  // All API proxies use Route Handlers (app/api/) so backend URLs are
  // resolved at runtime, not baked in at build time.
  //   - /api/backend/* → app/api/backend/[...path]/route.js
  //   - /api/chatbot/* → app/api/chatbot/[...path]/route.js

  // Windows dev-only: Next's persistent webpack filesystem cache does an
  // atomic rename (`0.pack.gz_` -> `0.pack.gz`) that Windows Defender / other
  // real-time file-lockers can lose the race against, producing "Caching
  // failed... ENOENT: no such file or directory, rename ...". The failed
  // write can leave .next/cache in a state that causes later dev-server
  // requests to 500 on files that do exist on disk. Trades a little rebuild
  // speed for not hitting this class of bug — production builds (`next
  // build`) are unaffected, since `dev` is false there.
  webpack: (config, { dev }) => {
    if (dev) {
      config.cache = false;
    }
    return config;
  },
};

export default nextConfig;
