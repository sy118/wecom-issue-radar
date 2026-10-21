#!/usr/bin/env node
/**
 * Generate the Tauri-compatible `latest.json` asset that every Electron Release
 * keeps publishing for legacy (v3.x) clients.
 *
 * The old Tauri updater reads:
 *   https://github.com/<owner>/<repo>/releases/latest/download/latest.json
 * and expects a manifest that points at the last Tauri installer (v3.5.3) with
 * its minisign signature. Because the pinned version never exceeds v3.5.3, an
 * already-migrated v3.5.3 client simply sees "no update" instead of failing on
 * the Electron `latest.yml`, and an older v3.x client only ever upgrades to the
 * final Tauri build, never to the Electron installer.
 *
 * The script deliberately fails hard when the legacy Release assets are
 * missing: publishing an empty/broken manifest would make old clients show
 * update errors.
 *
 * Environment:
 *   GH_TOKEN            GitHub token used for API calls (optional for public
 *                       repositories, required in CI to avoid rate limits).
 *   GITHUB_REPOSITORY   "owner/repo" (provided by GitHub Actions).
 *   LEGACY_TAURI_TAG    Tag of the last Tauri release, defaults to "v3.5.3".
 *   GITHUB_API_URL      API base URL, defaults to "https://api.github.com".
 *   GITHUB_SERVER_URL   Web base URL, defaults to "https://github.com".
 *
 * Usage:
 *   node scripts/make-legacy-updater-manifest.mjs --output release/latest.json
 */

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_LEGACY_TAG = "v3.5.3";
const DEFAULT_OUTPUT = "latest.json";
const DEFAULT_API_URL = "https://api.github.com";
const DEFAULT_SERVER_URL = "https://github.com";
const API_VERSION = "2022-11-28";
const MAX_ATTEMPTS = 3;
const RETRY_DELAY_MS = 1000;

function fail(message) {
  throw new Error(message);
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

function parseArguments(argv) {
  const options = { output: DEFAULT_OUTPUT, help: false };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--help" || argument === "-h") {
      options.help = true;
    } else if (argument === "--output" || argument === "-o") {
      const value = argv[index + 1];
      if (!value) fail("--output requires a file path");
      options.output = value;
      index += 1;
    } else if (argument.startsWith("--output=")) {
      const value = argument.slice("--output=".length);
      if (!value) fail("--output requires a file path");
      options.output = value;
    } else {
      fail(`Unknown argument: ${argument}`);
    }
  }
  return options;
}

function printHelp() {
  console.log(
    [
      "Usage: node scripts/make-legacy-updater-manifest.mjs [--output <path>]",
      "",
      "Environment: GH_TOKEN, GITHUB_REPOSITORY, LEGACY_TAURI_TAG (default v3.5.3).",
      `Output defaults to ${DEFAULT_OUTPUT}.`,
    ].join("\n"),
  );
}

function readEnvironment(env) {
  const repository = (env.GITHUB_REPOSITORY ?? "").trim();
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
    fail("GITHUB_REPOSITORY must be set to owner/repo");
  }
  const [owner, repo] = repository.split("/");

  const legacyTag = (env.LEGACY_TAURI_TAG ?? DEFAULT_LEGACY_TAG).trim();
  if (!/^v[0-9]+\.[0-9]+\.[0-9]+$/.test(legacyTag)) {
    fail(`LEGACY_TAURI_TAG must look like vX.Y.Z, got: ${legacyTag}`);
  }

  return {
    repository,
    owner,
    repo,
    legacyTag,
    token: (env.GH_TOKEN ?? env.GITHUB_TOKEN ?? "").trim(),
    apiUrl: (env.GITHUB_API_URL ?? DEFAULT_API_URL).replace(/\/+$/, ""),
    serverUrl: (env.GITHUB_SERVER_URL ?? DEFAULT_SERVER_URL).replace(/\/+$/, ""),
  };
}

async function githubFetch(environment, url, accept) {
  const headers = {
    Accept: accept,
    "X-GitHub-Api-Version": API_VERSION,
    "User-Agent": "wecom-issue-radar-legacy-updater-manifest",
  };
  if (environment.token) {
    headers.Authorization = `Bearer ${environment.token}`;
  }

  let lastError = null;
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
    let response;
    try {
      response = await fetch(url, { headers, redirect: "follow" });
    } catch (error) {
      lastError = new Error(`Cannot reach GitHub API ${url}: ${error.message}`);
      if (attempt < MAX_ATTEMPTS) await delay(RETRY_DELAY_MS * attempt);
      continue;
    }

    if (response.ok) return response;

    const retryable = response.status === 429 || response.status >= 500;
    lastError = new Error(
      `GitHub API request failed (${response.status} ${response.statusText}): ${url}`,
    );
    if (!retryable) throw lastError;
    if (attempt < MAX_ATTEMPTS) await delay(RETRY_DELAY_MS * attempt);
  }
  throw lastError;
}

async function loadLegacyRelease(environment) {
  const url = [
    environment.apiUrl,
    "repos",
    encodeURIComponent(environment.owner),
    encodeURIComponent(environment.repo),
    "releases",
    "tags",
    encodeURIComponent(environment.legacyTag),
  ].join("/");
  const response = await githubFetch(environment, url, "application/vnd.github+json");
  const release = await response.json();
  if (!release || typeof release !== "object" || !Array.isArray(release.assets)) {
    fail(`GitHub Release ${environment.legacyTag} did not return an asset list`);
  }
  if (release.tag_name !== environment.legacyTag) {
    fail(
      `GitHub returned release ${String(release.tag_name)} for requested tag ${environment.legacyTag}`,
    );
  }
  return release;
}

function selectLegacyInstaller(release, legacyTag) {
  const installers = release.assets.filter(
    (asset) => typeof asset?.name === "string" && asset.name.toLowerCase().endsWith(".exe"),
  );
  if (installers.length === 0) {
    fail(`No *.exe asset found in legacy Tauri Release ${legacyTag}`);
  }
  if (installers.length > 1) {
    fail(
      `Expected exactly one *.exe asset in legacy Tauri Release ${legacyTag}, found ` +
        installers.map((asset) => asset.name).join(", "),
    );
  }
  const installer = installers[0];

  const signatureName = `${installer.name}.sig`;
  const signatureAsset = release.assets.find((asset) => asset?.name === signatureName);
  if (!signatureAsset) {
    fail(`Cannot find companion signature asset ${signatureName} in legacy Tauri Release ${legacyTag}`);
  }
  if (typeof signatureAsset.url !== "string" || signatureAsset.url.length === 0) {
    fail(`Signature asset ${signatureName} does not expose an API URL`);
  }
  return { installer, signatureAsset };
}

async function downloadSignature(environment, signatureAsset) {
  const response = await githubFetch(environment, signatureAsset.url, "application/octet-stream");
  const signature = (await response.text()).trim();
  if (signature.length === 0) {
    fail(`Signature asset ${signatureAsset.name} is empty`);
  }
  if (!/^[A-Za-z0-9+/=\r\n]+$/.test(signature)) {
    fail(`Signature asset ${signatureAsset.name} does not look like a base64 minisign signature`);
  }
  return signature;
}

function buildManifest(environment, release, installer, signature) {
  const version = environment.legacyTag.replace(/^v/, "");
  const installerUrl = [
    environment.serverUrl,
    encodeURIComponent(environment.owner),
    encodeURIComponent(environment.repo),
    "releases",
    "download",
    encodeURIComponent(environment.legacyTag),
    encodeURIComponent(installer.name),
  ].join("/");

  const platformEntry = { signature, url: installerUrl };
  const publishedAt = release.published_at ?? release.created_at ?? new Date().toISOString();

  return {
    version,
    notes: typeof release.body === "string" ? release.body : "",
    pub_date: publishedAt,
    platforms: {
      "windows-x86_64": { ...platformEntry },
      "windows-x86_64-nsis": { ...platformEntry },
    },
  };
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  if (options.help) {
    printHelp();
    return;
  }

  const environment = readEnvironment(process.env);
  const release = await loadLegacyRelease(environment);
  const { installer, signatureAsset } = selectLegacyInstaller(release, environment.legacyTag);
  const signature = await downloadSignature(environment, signatureAsset);
  const manifest = buildManifest(environment, release, installer, signature);

  const outputPath = path.resolve(options.output);
  await mkdir(path.dirname(outputPath), { recursive: true });
  await writeFile(outputPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");

  console.log(`Wrote legacy Tauri updater manifest: ${outputPath}`);
  console.log(
    `  version=${manifest.version} installer=${installer.name} ` +
      "platforms=windows-x86_64,windows-x86_64-nsis",
  );
}

const scriptPath = fileURLToPath(import.meta.url);
const invokedPath = process.argv[1] ? path.resolve(process.argv[1]) : "";
const invokedDirectly =
  process.platform === "win32"
    ? scriptPath.toLowerCase() === invokedPath.toLowerCase()
    : scriptPath === invokedPath;

if (invokedDirectly) {
  main().catch((error) => {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`::error::${message}`);
    process.exitCode = 1;
  });
}
