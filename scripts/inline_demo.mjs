/**
 * Collapse the built demo into one self-contained HTML file.
 *
 * A multi-file build is only as reachable as its asset URLs, and every host
 * resolves those differently: Pages serves a project repo under /<repo>/, a raw
 * CDN serves it under /<user>/<repo>/<ref>/docs/, and a file:// open has no
 * origin at all. Each of those is a separate way for the page to come up blank
 * with a 404 in a console nobody opens - which is exactly the failure this is
 * here to end.
 *
 * With nothing external to fetch there is no URL left to get wrong. The file
 * is the site.
 */

import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DIST = join(ROOT, "apps/web/dist");
const DOCS = join(ROOT, "docs");

const SCRIPT = /<script\b[^>]*\bsrc="([^"]+)"[^>]*><\/script>/g;
const STYLE = /<link\b[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"[^>]*>/g;
const PRELOAD = /<link\b[^>]*\brel="modulepreload"[^>]*>\s*/g;

/** An asset path in the built HTML, resolved against dist. */
const asset = (href) => readFileSync(join(DIST, href.replace(/^\.?\//, "")), "utf8");

/**
 * "</script" inside the bundle would close the tag early. The backslash form is
 * an identity transform in JS source - "<\/script>" and "</script>" are the same
 * string, the same regex - so this is safe to apply blindly.
 */
const escapeJs = (js) => js.replace(/<\/script/gi, "<\\/script");
const escapeCss = (css) => css.replace(/<\/style/gi, "<\\/style");

let html = readFileSync(join(DIST, "index.html"), "utf8");

html = html
  .replace(PRELOAD, "")
  .replace(STYLE, (_m, href) => `<style>${escapeCss(asset(href))}</style>`)
  .replace(SCRIPT, (_m, src) => `<script type="module">${escapeJs(asset(src))}</script>`);

// The whole guarantee rests on there being nothing left to fetch, so assert it
// rather than trust it. data: and in-page anchors are self-contained; anything
// else is a URL that some host will resolve differently.
const external = [...html.matchAll(/\s(?:src|href)="([^"]*)"/g)]
  .map(([, url]) => url)
  .filter((url) => !url.startsWith("data:") && !url.startsWith("#"));

if (external.length) {
  throw new Error(`external references survived inlining: ${external.join(", ")}`);
}

rmSync(join(DOCS, "assets"), { recursive: true, force: true });
writeFileSync(join(DOCS, "index.html"), html);

// Pages serves 404.html for any unknown path under the site. Serving the app
// itself there means a mistyped or stale URL still lands on a working page
// instead of a dead end.
writeFileSync(join(DOCS, "404.html"), html);

const kb = (s) => `${(s.length / 1024).toFixed(0)} kB`;
console.log(`docs/index.html  ${kb(html)}  (single file, 0 external requests)`);
