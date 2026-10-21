import { rm } from "node:fs/promises";
import path from "node:path";
import { build } from "esbuild";

const root = process.cwd();
const outdir = path.join(root, "dist-electron");
await rm(outdir, { recursive: true, force: true });

/** @type {import("esbuild").BuildOptions} */
const shared = {
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node22",
  sourcemap: false,
  logLevel: "info",
  // Electron is provided by the runtime; electron-updater is a production
  // dependency shipped in the app's node_modules.
  external: ["electron", "electron-updater"],
};

await build({
  ...shared,
  entryPoints: [path.join(root, "electron/main/index.ts")],
  outfile: path.join(outdir, "main.cjs"),
});

await build({
  ...shared,
  entryPoints: [path.join(root, "electron/preload/index.ts")],
  outfile: path.join(outdir, "preload.cjs"),
});
