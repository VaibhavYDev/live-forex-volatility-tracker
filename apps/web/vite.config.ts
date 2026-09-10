import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // GitHub Pages serves a project repo under /<repo>/, so the built asset URLs
  // have to carry that prefix. Left at "/" for every other target, including
  // the nginx image, which serves from the root.
  base: process.env.VITE_BASE ?? "/",
  server: {
    host: "0.0.0.0",
    port: 5173,
    // Proxy in dev so the browser talks to one origin and CORS never enters the
    // picture. Production serves the built assets from nginx with the same paths.
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    // No sourcemap on the published demo: it is ~900 kB of the repo for a build
    // nobody debugs from a browser. Kept everywhere else, where it is the
    // difference between a readable stack trace and a minified one.
    sourcemap: process.env.VITE_DEMO !== "1",
  },
});
