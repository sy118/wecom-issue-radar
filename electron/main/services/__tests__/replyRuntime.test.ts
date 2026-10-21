import { describe, expect, it } from "vitest";

import {
  isSecretKey,
  redactSecrets,
  routeRuntimeEvent,
} from "../replyRuntime";

describe("reply runtime secret redaction", () => {
  it("detects secret-looking keys in common spellings", () => {
    for (const key of [
      "api_key",
      "apiKey",
      "API-KEY",
      "secret",
      "client_secret",
      "token",
      "accessToken",
      "refreshToken",
      "password",
      "Authorization",
      "webhook",
      "webhook_url",
    ]) {
      expect(isSecretKey(key)).toBe(true);
    }
    expect(isSecretKey("name")).toBe(false);
    expect(isSecretKey("template_id")).toBe(false);
  });

  it("recursively replaces secret values without mutating the source", () => {
    const source = {
      type: "event",
      seq: 7,
      event: {
        kind: "mcp.tool.completed",
        api_key: "sk-live-123",
        nested: {
          Authorization: "Bearer abc",
          webhook_url: "https://example.test/hook",
          safe: "keep-me",
          list: [{ password: "p@ss", token: "t" }],
        },
      },
    };
    const redacted = redactSecrets(source) as typeof source;
    expect(redacted.event.api_key).toBe("[redacted]");
    expect(redacted.event.nested.Authorization).toBe("[redacted]");
    expect(redacted.event.nested.webhook_url).toBe("[redacted]");
    expect(redacted.event.nested.safe).toBe("keep-me");
    expect(redacted.event.nested.list[0]).toEqual({
      password: "[redacted]",
      token: "[redacted]",
    });
    // Source object remains untouched.
    expect(source.event.api_key).toBe("sk-live-123");
    expect(redacted).not.toBe(source);
  });
});

describe("reply runtime event sequence dedupe", () => {
  it("forwards only strictly increasing event sequences", () => {
    const first = routeRuntimeEvent(
      { type: "event", seq: 2, event: { kind: "a" } },
      0,
    );
    expect(first.forwarded).toBe(true);
    expect(first.lastSeq).toBe(2);
    expect(first.payload).toEqual({ type: "event", seq: 2, event: { kind: "a" } });

    const duplicate = routeRuntimeEvent(
      { type: "event", seq: 2, event: { kind: "a" } },
      first.lastSeq,
    );
    expect(duplicate.forwarded).toBe(false);
    expect(duplicate.lastSeq).toBe(2);

    const older = routeRuntimeEvent(
      { type: "event", seq: 1, event: { kind: "old" } },
      first.lastSeq,
    );
    expect(older.forwarded).toBe(false);

    const next = routeRuntimeEvent(
      { type: "event", seq: 3, event: { kind: "b" } },
      first.lastSeq,
    );
    expect(next).toMatchObject({ forwarded: true, lastSeq: 3 });
  });

  it("ignores responses and malformed events", () => {
    expect(routeRuntimeEvent({ id: "1", ok: true, data: {} }, 0)).toEqual({
      forwarded: false,
      lastSeq: 0,
      payload: null,
    });
    expect(routeRuntimeEvent({ type: "event", event: {} }, 0).forwarded).toBe(false);
    expect(routeRuntimeEvent({ type: "event", seq: 4 }, 4).forwarded).toBe(false);
  });

  it("redacts secrets before forwarding an event", () => {
    const routed = routeRuntimeEvent(
      {
        type: "event",
        seq: 9,
        event: { token: "secret-token", nested: { api_key: "k" } },
      },
      0,
    );
    expect(routed.payload).toEqual({
      type: "event",
      seq: 9,
      event: { token: "[redacted]", nested: { api_key: "[redacted]" } },
    });
  });
});
