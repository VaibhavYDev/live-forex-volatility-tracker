/**
 * Where the browser talks to the API.
 *
 * SAME ORIGIN, ALWAYS. Both deployment paths already proxy `/api` and `/ws` to
 * the API service - nginx in the built image, Vite's dev server locally - so
 * dialing the API's own port directly bypasses working infrastructure and buys
 * two failure modes for nothing:
 *
 *   1. Port 8000 has to be publicly reachable, which on a cloud VM means
 *      punching a second hole through both the provider firewall and the
 *      instance's own iptables.
 *   2. A page served over HTTPS has its `ws://` and `http://` requests killed by
 *      the browser as mixed content. The dashboard renders, the socket never
 *      opens, and nothing on screen says why - the failure is silent and looks
 *      exactly like a broken project.
 *
 * (2) is the reason this file exists. The scheme is derived from the page's own
 * protocol rather than hardcoded, so the same build works on http://localhost
 * and on an HTTPS origin with no rebuild and no environment variable.
 */

/** Deliberately explicit rather than `startsWith("https")`: a page on `file:`
 *  or `blob:` has no meaningful socket origin, and defaulting those to `ws:`
 *  fails loudly at connect time instead of silently half-working. */
const WS_SCHEME: Record<string, string> = {
  "https:": "wss:",
  "http:": "ws:",
};

interface Origin {
  readonly protocol: string;
  /** host, not hostname - it carries the port, which is the whole point. */
  readonly host: string;
}

export function wsUrl(loc: Origin = window.location): string {
  return `${WS_SCHEME[loc.protocol] ?? "ws:"}//${loc.host}/ws/stream`;
}

/** Empty string, so callers build `/api/...` and the request stays on this
 *  origin. A base is still overridable for the split-origin case (frontend on a
 *  CDN, API elsewhere), which is the only time CORS is worth paying for. */
export function apiBase(): string {
  return "";
}
