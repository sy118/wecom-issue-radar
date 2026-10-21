import { app, session, shell } from "electron";

const ALLOWED_DEV_ORIGINS = new Set([
  "http://localhost:1420",
  "http://127.0.0.1:1420",
]);

function contentSecurityPolicy(): string {
  return [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: file: blob:",
    "font-src 'self' data:",
    "connect-src 'self' http://localhost:1420 ws://localhost:1420 http://127.0.0.1:1420",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join("; ");
}

function isAllowedNavigation(rawUrl: string): boolean {
  try {
    const url = new URL(rawUrl);
    if (url.protocol === "file:") return true;
    return ALLOWED_DEV_ORIGINS.has(url.origin);
  } catch {
    return false;
  }
}

/**
 * Locks the renderer down: no remote navigation, no popups, no permission
 * prompts, and a strict CSP for every response.
 */
export function installSecurity(): void {
  app.on("web-contents-created", (_event, contents) => {
    contents.setWindowOpenHandler(({ url }) => {
      if (url.startsWith("https://")) void shell.openExternal(url);
      return { action: "deny" };
    });
    contents.on("will-navigate", (event, url) => {
      if (!isAllowedNavigation(url)) event.preventDefault();
    });
    contents.session.setPermissionRequestHandler((_webContents, _permission, callback) => {
      callback(false);
    });
  });

  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [contentSecurityPolicy()],
      },
    });
  });
}
