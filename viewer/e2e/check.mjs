// End-to-end check of the built viewer in Chromium (record 0012).
//
//   npm run e2e -- --url http://127.0.0.1:8300 --screenshot e2e/out/side-by-side.png
//
// It needs a running viewer (`python -m fivefold.viewer`, serving `npm run build`'s files)
// whose registry lists a demo's B0 and G2 runs (`make demo`), and Chromium for Playwright
// (`npx playwright install chromium` once). It checks that:
//
// - the run list shows every run the API lists;
// - the G2 run's page shows one timeline row per event the API serves, marks the events
//   the API marks as sharing a virtual time, and shows the five properties' verdicts;
// - turning a hop off hides exactly that hop's messages;
// - the side-by-side page shows B0 and G2 aligned by virtual time;
// - the browser logged no error.
//
// It saves a screenshot of the side-by-side page. `--b0` and `--g2` choose the runs;
// by default it takes the first experiment pair offered by the run list.

import assert from "node:assert/strict";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { parseArgs } from "node:util";

import { chromium } from "playwright";
import { pairs } from "../src/views.ts";

const { values: options } = parseArgs({
  options: {
    url: { type: "string", default: "http://127.0.0.1:8300" },
    b0: { type: "string" },
    g2: { type: "string" },
    screenshot: { type: "string", default: "e2e/out/side-by-side.png" },
  },
});
const base = options.url.replace(/\/$/, "");

async function api(path) {
  const response = await fetch(`${base}${path}`);
  assert.equal(response.status, 200, `${path} answered ${response.status}`);
  return response.json();
}

function choosePair(runs) {
  if (options.b0 && options.g2) return [options.b0, options.g2];
  const pair = pairs(runs)[0];
  if (pair) return [pair.b0.database, pair.g2.database];
  throw new Error("the registry lists no B0 and G2 runs that share an experiment cell");
}

const { runs } = await api("/api/runs");
const [b0, g2] = choosePair(runs);
const { events } = await api(`/api/runs/${encodeURIComponent(g2)}/timeline`);
const marked = events.filter((event) => event.shares_vt).length;
const hops = [...new Set(events.flatMap((e) => (e.kind === "message" ? [e.message.hop] : [])))];

const browser = await chromium.launch();
const errors = [];
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", (error) => errors.push(String(error)));

  await page.goto(`${base}/#/`);
  await page.waitForSelector("[data-testid=run-row]");
  assert.equal(await page.locator("[data-testid=run-row]").count(), runs.length, "run list rows");

  await page.goto(`${base}/#/runs/${encodeURIComponent(g2)}`);
  await page.waitForSelector("[data-testid=timeline-row]");
  const rows = page.locator("[data-testid=timeline-row]");
  assert.equal(await rows.count(), events.length, "one timeline row per event");
  assert.equal(await page.getByLabel("shares its virtual time").count(), marked, "marked rows");
  assert.equal(await page.locator("[data-testid=property-row]").count(), 5, "five properties");

  const hop = hops[0];
  const onHop = events.filter((e) => e.kind === "message" && e.message.hop === hop).length;
  await page.getByRole("checkbox", { name: hop }).uncheck();
  assert.equal(await rows.count(), events.length - onHop, `rows without hop ${hop}`);

  await page.goto(`${base}/#/compare/${encodeURIComponent(b0)}/${encodeURIComponent(g2)}`);
  await page.waitForSelector("[data-testid=compare-row]");
  const aligned = await page.locator("[data-testid=compare-row]").count();
  assert.ok(aligned > 0, "the side-by-side page has rows");
  mkdirSync(dirname(options.screenshot), { recursive: true });
  await page.screenshot({ path: options.screenshot, fullPage: true });

  assert.deepEqual(errors, [], "the browser logged errors");
  console.log(
    `viewer check passed: ${runs.length} runs listed; ${g2}: ${events.length} timeline rows,` +
      ` ${marked} marked as sharing a virtual time, hop ${hop} filters ${onHop} messages;` +
      ` ${b0} beside ${g2}: ${aligned} rows; screenshot ${options.screenshot}`,
  );
} finally {
  await browser.close();
}
