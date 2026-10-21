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
  },
  build: {
    outDir: "frontend-dist",
    target: "chrome130",
    minify: "esbuild",
    sourcemap: false,
  },
});
