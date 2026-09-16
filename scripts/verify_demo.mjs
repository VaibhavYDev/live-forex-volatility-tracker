/**
 * Load docs/index.html the way each candidate host would serve it and assert the
 * app actually came up.
 *
 * The published demo has failed silently four times now, every time for a
 * different reason and every time looking identical from outside: a page that
 * loads, renders nothing, and logs one 404 to a console nobody opens. Eyeballing
 * one URL cannot catch that class of bug, because the bug is path-dependent -
 * the same file is correct under one prefix and broken under another.
 *
 * So: serve it under every prefix a real host might use, plus straight off disk,
 * and fail on any console error or any request that does not return 200.
 */

import { createServer } from "node:http";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Resolved from apps/web so the browser toolchain lives with the rest of the
// front-end's dev dependencies instead of needing a second manifest at the root.
const { chromium } = createRequire(join(ROOT, "apps/web/package.json"))("playwright");
const PAGE = join(ROOT, "docs/index.html");

/** Prefix -> how that host addresses the file. Each is a real deployment shape. */
const MOUNTS = [
  ["", "/", "custom domain / user site at the root"],
  ["/live-forex-volatility-tracker", "/", "Pages project repo"],
  ["/VaibhavYDev/live-forex-volatility-tracker/main/docs", "/index.html", "raw CDN deep path"],
  ["/live-forex-volatility-tracker", "/deliberately/missing/page", "Pages 404 fallback"],
];

const html = readFileSync(PAGE);
const notFound = readFileSync(join(ROOT, "docs/404.html"));

const server = createServer((req, res) => {
  const path = decodeURI(req.url.split("?")[0]);
  const hit = MOUNTS.some(([prefix]) => path === `${prefix}/` || path === `${prefix}/index.html`);
  const body = hit ? html : notFound;
  res.writeHead(hit ? 200 : 404, { "content-type": "text/html; charset=utf-8" });
  res.end(body);
});

await new Promise((ok) => server.listen(0, "127.0.0.1", ok));
const origin = `http://127.0.0.1:${server.address().port}`;

// Prefer a Chromium the image already ships, because some sandboxes cannot reach
// the browser CDN that `playwright install` downloads from. Fall back to
// Playwright's own, which is what CI has.
const pinned = process.env.CHROMIUM_PATH ?? "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
const browser = await chromium.launch(existsSync(pinned) ? { executablePath: pinned } : {});
let failures = 0;

/** Wait for the app to paint, then report what a visitor would actually see. */
async function check(label, url, { documentIs404 = false } = {}) {
  const page = await browser.newPage();
  const errors = [];
  const bad = [];

  // Chromium logs the document's own 404 to the console, and the 404-fallback
  // case navigates to a missing path deliberately. Everything else counts.
  const expected = documentIs404 ? /failed to load resource.*404/i : /(?!)/;
  page.on("console", (m) => {
    if (m.type() === "error" && !expected.test(m.text())) errors.push(m.text());
  });
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("requestfailed", (r) => bad.push(`${r.url()} ${r.failure()?.errorText}`));
  page.on("response", (r) => {
    if (r.status() < 400) return;
    // The 404-fallback case navigates to a missing path on purpose: the status
    // IS the scenario. Anything else 404ing is a real defect.
    if (documentIs404 && r.url() === url) return;
    bad.push(`${r.url()} -> ${r.status()}`);
  });

  let banner = "(never rendered)";
  let timeframes = 0;
  let rows = 0;

  try {
    await page.goto(url, { waitUntil: "load", timeout: 30_000 });
    await page.waitForSelector("[data-testid='status-banner'], .status-banner, header", {
      timeout: 20_000,
    });
    // The demo heartbeats every 5s; give the first one room to land so a "Stale"
    // reading here means genuinely stale, not merely early.
    await page.waitForTimeout(2_500);

    banner = (await page.locator("body").innerText()).split("\n").find((l) => l.trim()) ?? "";
    timeframes = await page.getByRole("button", { name: /minute|hour|day|week|month/i }).count();
    rows = await page.locator("table tbody tr, [role='row']").count();
  } catch (err) {
    errors.push(`navigation: ${err.message}`);
  }

  const text = await page.locator("body").innerText().catch(() => "");
  const live = /live|connect/i.test(text) && !/stale/i.test(text.slice(0, 400));
  const ok = errors.length === 0 && bad.length === 0 && timeframes >= 9 && live;

  if (!ok) failures++;
  console.log(
    `${ok ? "PASS" : "FAIL"}  ${label.padEnd(30)} ` +
      `timeframes=${timeframes} rows=${rows} errors=${errors.length} badRequests=${bad.length}`,
  );
  for (const e of errors.slice(0, 3)) console.log(`        error: ${e.slice(0, 160)}`);
  for (const b of bad.slice(0, 3)) console.log(`        request: ${b.slice(0, 160)}`);

  await page.close();
}

for (const [prefix, path, label] of MOUNTS) {
  await check(label, `${origin}${prefix}${path}`, { documentIs404: label.includes("404") });
}
// No server, no origin, no base URL. If this one passes, nothing about hosting
// can break the page.
await check("opened straight off disk", `file://${PAGE}`);

await browser.close();
server.close();
process.exit(failures === 0 ? 0 : 1);
