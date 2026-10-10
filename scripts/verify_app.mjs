/**
 * Load the running dashboard while it has no market-data credentials and assert
 * it fabricates nothing: no price, no candles, no "Live", no page errors.
 *
 *   node scripts/verify_app.mjs [http://localhost:5173]
 *
 * This is the state a fresh `docker compose up` without FX_PROVIDER_TOKEN is in,
 * and the one place a fallback to invented data would show.
 */

import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");

// Resolved from apps/web so the browser toolchain lives with the rest of the
// front-end's dev dependencies instead of needing a second manifest at the root.
const { chromium } = createRequire(join(ROOT, "apps/web/package.json"))("playwright");
const url = process.argv[2] ?? "http://localhost:5173";

const pinned = process.env.CHROMIUM_PATH;
const browser = await chromium.launch(pinned && existsSync(pinned) ? { executablePath: pinned } : {});
const page = await browser.newPage();
const failures = [];
page.on("pageerror", (err) => failures.push(`page error: ${err.message}`));

await page.goto(url);
await page.getByRole("tab", { name: /EURUSD/ }).waitFor();
// Long enough for the socket's snapshot and a status heartbeat to have landed.
await page.waitForTimeout(8_000);

const banner = (await page.locator(".status").first().innerText()).replace(/\s+/g, " ");
if (/\bLive\b/.test(banner)) failures.push(`banner claims a live feed: ${banner}`);
for (const tab of await page.getByRole("tab").all()) {
  const text = (await tab.innerText()).replace(/\s+/g, " ");
  if (/\d\.\d{3}/.test(text)) failures.push(`price shown with no feed: ${text}`);
}
if (!(await page.getByText("Waiting for EURUSD history…").count())) {
  failures.push("the chart does not say it has no history");
}

await browser.close();
if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}
console.log(`no fabricated market data · banner: ${banner}`);
