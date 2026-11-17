// Store/PR screenshots of the REAL extension on synthetic pages. Usage: PW_DIR=... node screenshots.mjs <outDir>
import { createRequire } from "node:module";
import path from "node:path"; import os from "node:os"; import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { startMockGateway } from "./mock-gateway.mjs";
const require = createRequire(path.join(process.env.PW_DIR || process.cwd(), "x.js"));
const { chromium } = require("playwright");
const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../services/extension");
const OUT = path.resolve(process.argv[2] || "screenshots"); fs.mkdirSync(OUT, { recursive: true });
const PORT = 18998, EXT = fs.mkdtempSync(path.join(os.tmpdir(), "k70-ext-")); fs.cpSync(SRC, EXT, { recursive: true });
for (const f of ["background.js", "manifest.json"]) { const p = path.join(EXT, f); fs.writeFileSync(p, fs.readFileSync(p, "utf8").replaceAll("http://localhost:8080", "http://127.0.0.1:" + PORT)); }
// Playwright cannot click the toolbar icon, so activeTab (granted by that click) is not available in the test.
// The TEST COPY gets host access to the fake test origin only; the shipped manifest is not changed.
{ const mp = path.join(EXT, "manifest.json"); const mm = JSON.parse(fs.readFileSync(mp, "utf8")); mm.host_permissions.push("https://example.test/*"); fs.writeFileSync(mp, JSON.stringify(mm)); }
const gw = await startMockGateway(PORT);
const ctx = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(os.tmpdir(), "k70-")), { channel: "chromium", headless: true, viewport: { width: 1280, height: 800 }, args: ["--disable-extensions-except=" + EXT, "--load-extension=" + EXT] });
const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker");
const PAGE = `<!doctype html><title>Any page</title><body style="font:16px system-ui;margin:40px;max-width:640px"><h2>Supplier inquiry</h2><p id="msg">Hi, can you send the sample photos and the MOQ for the linen shirts? We need a quote by Friday.</p>`;
const p = await ctx.newPage(); await p.route(/^https:\/\/example\.test\//, (r) => r.fulfill({ contentType: "text/html", body: PAGE }));
await p.goto("https://example.test/any");
await sw.evaluate(async () => { const [t] = await chrome.tabs.query({ url: "https://example.test/*" }); await openPopup(t); });
await p.waitForSelector("#k70-copilot");
await p.evaluate(() => { const r = document.createRange(); r.selectNodeContents(document.getElementById("msg")); const s = getSelection(); s.removeAllRanges(); s.addRange(r); });
await p.click("[data-action=capture]"); await p.click("[data-tone=friendly]"); await p.click("[data-action=generate]");
await p.waitForSelector("#k70-draft", { state: "visible" }); await p.waitForTimeout(300);
await p.screenshot({ path: path.join(OUT, "k70-popup-reply.png") });
await p.click("[data-mode=compose]"); await p.fill("#k70-instruction", "Customer ko politely bolo main Thursday available hoon. Keep it short.");
await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible" }); await p.waitForTimeout(300);
await p.screenshot({ path: path.join(OUT, "k70-popup-write.png") });
await ctx.close(); gw.close(); console.log("saved to", OUT);
