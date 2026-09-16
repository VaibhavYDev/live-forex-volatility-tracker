import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // RELATIVE paths for the published build, absolute for everything else.
  //
  // GitHub Pages serves a project repo under /<repo>/, so an absolute "/assets/..."
  // 404s there. Hardcoding "/<repo>/" instead only moves the problem: it then
  // breaks if Pages is configured to serve from the repo root rather than /docs,
  // or if the repository is ever renamed - and both failures look identical to a
  // visitor, a blank page with one 404 in a console nobody opens.
  //
  // "./" is immune to all of it. The same bundle works under any subpath, at a
  // domain root, and opened straight off disk. The nginx image keeps "/" because
  // it always serves from the root and absolute paths survive client-side
  // routing there.
  base: process.env.VITE_BASE ?? (process.env.VITE_DEMO === "1" ? "./" : "/"),
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
