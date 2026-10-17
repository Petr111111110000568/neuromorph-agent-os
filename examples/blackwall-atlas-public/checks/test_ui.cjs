"use strict";
// Optional test-only dependencies must already be installed. No automatic setup.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { chromium } = require("playwright");
const root = path.resolve(__dirname, "..");
const allowed = new Set(fs.readFileSync(path.join(root, "allowlist.txt"), "utf8").trim().split("\n"));
const mime = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".json": "application/json", ".md": "text/plain" };

function findBrowser() {
  if (process.env.ATLAS_TEST_BROWSER) return process.env.ATLAS_TEST_BROWSER;
  for (const candidate of ["chromium", "chromium-browser", "google-chrome"]) {
    const result = spawnSync("which", [candidate], { encoding: "utf8" });
    if (result.status === 0) return result.stdout.trim();
  }
  throw new Error("No existing Chromium found. Set ATLAS_TEST_BROWSER; no software was installed.");
}

(async () => {
  let browser;
  const server = http.createServer((request, response) => {
    const relative = decodeURIComponent((request.url || "/").split("?")[0]).slice(1) || "index.html";
    if (!allowed.has(relative)) { response.writeHead(404).end(); return; }
    response.writeHead(200, { "Content-Type": `${mime[path.extname(relative)] || "text/plain"}; charset=utf-8`, "Cache-Control": "no-store" });
    response.end(fs.readFileSync(path.join(root, relative)));
  });
  try {
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
    const origin = ["http:", "", `127.0.0.1:${server.address().port}`].join("/");
    browser = await chromium.launch({ executablePath: findBrowser(), headless: true });
    const context = await browser.newContext({ viewport: { width: 1366, height: 1000 } });
    const offOrigin = [];
    await context.route("**/*", (route) => {
      if (new URL(route.request().url()).origin !== origin) { offOrigin.push(route.request().url()); return route.abort(); }
      return route.continue();
    });
    const page = await context.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await page.goto(origin);
    await page.waitForSelector(".card");
    assert.equal(await page.locator(".card").count(), 6);
    assert.equal(await page.locator("#result-count").textContent(), "6 of 6 synthetic examples");
    assert.equal(await page.locator(".demo-pill").isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    console.log("PASS: six synthetic cards, visible demo notice, desktop layout");

    await page.locator("#status").selectOption("review");
    assert.equal(await page.locator(".card").count(), 2);
    await page.locator("#search").fill("KEYBOARD");
    assert.equal(await page.locator(".card").count(), 1);
    await page.locator("#search").fill("no-match-fixture");
    assert.equal(await page.locator(".card").count(), 0);
    assert.equal(await page.locator("#empty-state").isVisible(), true);
    await page.locator("#reset").click();
    await page.locator("#reset").click();
    assert.equal(await page.locator(".card").count(), 6);
    assert.equal(await page.locator("#search").evaluate((node) => node === document.activeElement), true);
    console.log("PASS: combined and case-insensitive filters, empty state, repeated reset");

    const open = page.locator(".card-button").first();
    await open.click();
    assert.equal(await page.locator("#task-dialog").evaluate((node) => node.open), true);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#task-dialog").evaluate((node) => node.open), false);
    assert.equal(await open.evaluate((node) => node === document.activeElement), true);
    await open.dblclick();
    await page.locator("#close-dialog").click();
    assert.equal(await page.locator("#task-dialog").evaluate((node) => node.open), false);
    await open.click();
    await page.mouse.click(3, 3);
    assert.equal(await page.locator("#task-dialog").evaluate((node) => node.open), false);
    await open.focus();
    await page.keyboard.press("Enter");
    await page.keyboard.press("Escape");
    assert.equal(await open.evaluate((node) => node === document.activeElement), true);
    console.log("PASS: repeated open, close button, Escape, backdrop, keyboard and focus restoration");

    await page.locator('a[href="CONTRIBUTING.md"]').first().click();
    await page.waitForURL("**/CONTRIBUTING.md");
    await page.goBack();
    await page.waitForSelector(".card");
    assert.equal(await page.locator(".card").count(), 6);
    await page.goForward();
    await page.waitForURL("**/CONTRIBUTING.md");
    await page.goBack();
    await page.waitForSelector(".card");
    console.log("PASS: documentation navigation and browser Back/Forward");

    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator("#search").isVisible(), true);
    await page.locator(".card-button").first().click();
    assert.equal(await page.locator("#close-dialog").isVisible(), true);
    await page.keyboard.press("Escape");
    console.log("PASS: narrow layout and dialog controls");
    if (process.env.ATLAS_TEST_SCREENSHOT) await page.screenshot({ path: process.env.ATLAS_TEST_SCREENSHOT, fullPage: true });

    const invalid = await context.newPage();
    await invalid.route("**/demo-data.json", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ synthetic: false }) }));
    await invalid.goto(origin);
    await invalid.waitForSelector("#load-error", { state: "visible" });
    assert.equal(await invalid.locator(".card").count(), 0);
    assert.equal(await invalid.locator("#search").isDisabled(), true);
    await invalid.close();
    const missing = await context.newPage();
    await missing.route("**/demo-data.json", (route) => route.fulfill({ status: 404, body: "" }));
    await missing.goto(origin);
    await missing.waitForSelector("#load-error", { state: "visible" });
    assert.equal(await missing.locator("#reset").isDisabled(), true);
    await missing.close();
    console.log("PASS: malformed/non-synthetic and missing dataset fail closed");

    const injection = await context.newPage();
    const fixture = JSON.parse(fs.readFileSync(path.join(root, "demo-data.json"), "utf8"));
    fixture.tasks[0].title = '<img src="invalid" onerror="globalThis.injected=true">';
    await injection.route("**/demo-data.json", (route) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fixture) }));
    await injection.goto(origin);
    await injection.waitForSelector(".card");
    assert.equal(await injection.locator(".card img").count(), 0);
    assert.equal(await injection.evaluate(() => globalThis.injected), undefined);
    assert.equal(await injection.locator(".card h3").first().textContent(), fixture.tasks[0].title);
    await injection.close();
    console.log("PASS: contributed strings render as text, not active markup");
    assert.deepEqual(pageErrors, []);
    assert.deepEqual(offOrigin, []);
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    console.log("PASS: zero page errors, zero non-local requests, zero browser storage entries");
    console.log(`Browser: ${browser.version()}`);
  } finally {
    if (browser) await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
})().catch((error) => { console.error(error.message); process.exitCode = 1; });
