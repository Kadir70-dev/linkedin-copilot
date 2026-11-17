// Store/PR screenshots of the REAL extension on synthetic pages. Usage: PW_DIR=... node screenshots.mjs <outDir>
import { createRequire } from "node:module";
import path from "node:path"; import os from "node:os"; import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { startMockGateway } from "./mock-gateway.mjs";
import { LINKEDIN, WHATSAPP, INSTAGRAM } from "./sites.mjs";
const require = createRequire(path.join(process.env.PW_DIR || process.cwd(), "x.js"));
const { chromium } = require("playwright");
const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../services/extension");
const OUT = path.resolve(process.argv[2] || "screenshots"); fs.mkdirSync(OUT, { recursive: true });
const PORT = 18998, EXT = fs.mkdtempSync(path.join(os.tmpdir(), "k70-ext-")); fs.cpSync(SRC, EXT, { recursive: true });
for (const f of ["background.js", "manifest.json"]) { const p = path.join(EXT, f); fs.writeFileSync(p, fs.readFileSync(p, "utf8").replaceAll("http://localhost:8080", "http://127.0.0.1:" + PORT)); }
const gw = await startMockGateway(PORT);
const ctx = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(os.tmpdir(), "k70-")), { channel: "chromium", headless: true, viewport: { width: 1280, height: 800 }, args: ["--disable-extensions-except=" + EXT, "--load-extension=" + EXT] });
const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker");
await sw.evaluate(() => chrome.storage.local.set({ k70Platforms: { whatsapp: true, instagram: true } }));
const pages = { linkedin: ["https://www.linkedin.com/messaging/thread/abc/", LINKEDIN], whatsapp: ["https://web.whatsapp.com/", WHATSAPP], instagram: ["https://www.instagram.com/direct/t/1/", INSTAGRAM] };
for (const [n, [url, html]] of Object.entries(pages)) {
  const p = await ctx.newPage(); await p.route(/^https:\/\/(www\.linkedin\.com|web\.whatsapp\.com|www\.instagram\.com)\//, (r) => r.fulfill({ contentType: "text/html", body: html }));
  await p.goto(url); await p.waitForSelector("#k70-copilot"); await p.evaluate(() => document.getElementById("side")?.remove()); await p.waitForTimeout(400);
  await p.click("[data-action=capture]"); await p.evaluate(() => document.getElementById("side")?.remove()); await p.click("[data-tone=friendly]"); await p.click("[data-action=generate]");
  await p.waitForSelector("#k70-draft", { state: "visible" }); await p.waitForTimeout(300);
  await p.screenshot({ path: path.join(OUT, `k70-${n}-popup.png`) }); await p.close();
}
await ctx.close(); gw.close(); console.log("saved to", OUT);
