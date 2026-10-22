import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Electron loads the production build from a file:// URL, so every asset
  // reference must be relative.
  base: "./",
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    // Editors that write files atomically through short-lived temp directories
    // can make the native Windows watcher throw EBUSY and kill the dev server.
    // Opt into polling only when explicitly requested (VITE_USE_POLLING=1).
    watch: process.env.VITE_USE_POLLING === "1" ? { usePolling: true, interval: 250 } : undefined,
  },
  build: {
    outDir: "frontend-dist",
    target: "chrome130",
    minify: "esbuild",
    sourcemap: false,
  },
});
