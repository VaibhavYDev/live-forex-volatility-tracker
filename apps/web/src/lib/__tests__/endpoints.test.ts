import { describe, expect, it } from "vitest";
import { apiBase, wsUrl } from "../endpoints";

/**
 * These assertions exist because the failure they prevent is SILENT.
 *
 * A page served over HTTPS that opens a `ws://` socket gets it killed by the
 * browser as mixed content. Nothing throws in a way the UI notices: the
 * dashboard renders, the banner sits on "Connecting", and the only evidence is
 * a console warning nobody reads. It is indistinguishable from a broken build,
 * which for a public demo is the worst way to fail.
 *
 * The previous default hardcoded `ws://` and port 8000, so it was guaranteed to
 * fail the moment the project was served from anywhere but localhost.
 */

const at = (protocol: string, host: string) => ({ protocol, host });

describe("socket URL", () => {
  it("uses wss on an HTTPS page", () => {
    // The regression, stated exactly. On the public demo this is the only
    // scheme a browser will allow.
    expect(wsUrl(at("https:", "fx.example.com"))).toBe("wss://fx.example.com/ws/stream");
  });

  it("uses ws on a plain HTTP page", () => {
    expect(wsUrl(at("http:", "localhost:5173"))).toBe("ws://localhost:5173/ws/stream");
  });

  it("keeps the port, because dev does not run on 80", () => {
    // `host` rather than `hostname` is load-bearing: drop the port and every
    // local dev session silently dials port 80.
    expect(wsUrl(at("http:", "127.0.0.1:5173"))).toContain(":5173/");
  });

  it("stays on the page's own origin", () => {
    // Same origin is what lets nginx and the Vite dev proxy do their job, and
    // is why no CORS policy and no second public port are needed.
    expect(wsUrl(at("https:", "demo.example.org:8443"))).toBe(
      "wss://demo.example.org:8443/ws/stream",
    );
  });

  it("falls back to ws for an origin with no sensible scheme", () => {
    // file:// and blob:// have no socket origin. Failing at connect time is
    // better than inventing a URL that half-works.
    expect(wsUrl(at("file:", ""))).toBe("ws:///ws/stream");
  });
});

describe("REST base", () => {
  it("is relative, so requests stay on this origin", () => {
    expect(apiBase()).toBe("");
  });

  it("composes into a same-origin path", () => {
    // Mirrors how VolatilityPanel builds its request.
    expect(`${apiBase()}/api/volatility/EURUSD/compare?limit=120`).toBe(
      "/api/volatility/EURUSD/compare?limit=120",
    );
  });
});
