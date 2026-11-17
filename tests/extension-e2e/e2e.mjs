// End-to-end test of the REAL unpacked extension in Chromium against a generic web page + a mock gateway.
// Usage: PW_DIR=<dir containing node_modules/playwright> node tests/extension-e2e/e2e.mjs
import { createRequire } from "node:module";
import path from "node:path"; import os from "node:os"; import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { startMockGateway } from "./mock-gateway.mjs";
const require = createRequire(path.join(process.env.PW_DIR || process.cwd(), "x.js"));
const { chromium } = require("playwright");
const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../services/extension");
const PORT = 18999;
const EXT = fs.mkdtempSync(path.join(os.tmpdir(), "k70-ext-"));
fs.cpSync(SRC, EXT, { recursive: true });
for (const f of ["background.js", "manifest.json"]) {
  const p = path.join(EXT, f); fs.writeFileSync(p, fs.readFileSync(p, "utf8").replaceAll("http://localhost:8080", "http://127.0.0.1:" + PORT));
}
let fails = 0; const ok = (n, c, x = "") => { console.log((c ? "PASS " : "FAIL ") + n + (c ? "" : "  -> " + x)); if (!c) fails++; };
// Playwright cannot click the toolbar icon, so activeTab (granted by that click) is not available in the test.
// The TEST COPY gets host access to the fake test origin only; the shipped manifest is not changed.
{ const mp = path.join(EXT, "manifest.json"); const mm = JSON.parse(fs.readFileSync(mp, "utf8")); mm.host_permissions.push("https://example.test/*"); fs.writeFileSync(mp, JSON.stringify(mm)); }
const gw = await startMockGateway(PORT);
const ctx = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(os.tmpdir(), "k70-")), {
  channel: "chromium", headless: true, viewport: { width: 1200, height: 800 },
  args: ["--disable-extensions-except=" + EXT, "--load-extension=" + EXT] });
const sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker");
const manifest = JSON.parse(fs.readFileSync(path.join(SRC, "manifest.json"), "utf8"));

const PAGE = `<!doctype html><title>Any page</title><body style="font:16px sans-serif">
<p id="msg">Hi, can you send the sample photos and the MOQ for the linen shirts?</p>
<textarea id="box" aria-label="Page message box"></textarea><button id="send" onclick="window.__sent=true">Send</button>
<script>window.__sent=false;document.getElementById("box").addEventListener("input",()=>window.__typed=true);</script>`;
async function open(url = "https://example.test/any") {
  const p = await ctx.newPage(); const errs = []; p.on("pageerror", (e) => errs.push(String(e)));
  await p.route(/^https:\/\/example\.test\//, (r) => r.fulfill({ contentType: "text/html", body: PAGE }));
  await p.goto(url); p.errs = errs; return p;
}
// Same code path as clicking the toolbar icon (Playwright cannot click the browser toolbar).
const clickIcon = async (url) => { await sw.evaluate(async (u) => { const [t] = await chrome.tabs.query({ url: u }); await openPopup(t); }, url); };
const panel = (p) => p.locator("#k70-copilot");

// ---- manifest: general use, least privilege ----
ok("manifest has no content scripts and no site access", !manifest.content_scripts && !manifest.host_permissions.some((h) => !/localhost|127\.0\.0\.1/.test(h)), JSON.stringify(manifest.host_permissions));
ok("manifest permissions are storage, activeTab, scripting only", JSON.stringify([...manifest.permissions].sort()) === JSON.stringify(["activeTab", "scripting", "storage"]), manifest.permissions.join());
{
  const p = await open(); await p.waitForTimeout(1500);
  ok("nothing appears on a page until the toolbar icon is clicked", await panel(p).count() === 0);
  await clickIcon("https://example.test/*"); await p.waitForSelector("#k70-copilot", { timeout: 8000 });
  ok("popup opens on a generic site", await panel(p).count() === 1);
  ok("popup does not mention a specific platform", !/linkedin|whatsapp|instagram/i.test(await panel(p).innerText()));
  ok("nothing read or sent before the user acts", (await p.inputValue("#k70-thread")) === "" && !gw.log.some((l) => l.path === "/ingest"));
  // selected text -> "Use selected text"
  await p.evaluate(() => { const r = document.createRange(); r.selectNodeContents(document.getElementById("msg")); const s = getSelection(); s.removeAllRanges(); s.addRange(r); });
  await p.click("[data-action=capture]");
  const cap = await p.inputValue("#k70-thread"); ok("Use selected text copies only the selection", cap.includes("MOQ") && cap.length < 200, cap);
  await p.click("[data-tone=friendly]"); await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 });
  const ing = gw.log.filter((l) => l.path === "/ingest").pop().body;
  ok("reply request carries mode=reply, tone=friendly and the text", ing.mode === "reply" && ing.tone === "friendly" && ing.text.includes("MOQ"), JSON.stringify(ing));
  // copy
  await ctx.grantPermissions(["clipboard-read", "clipboard-write"], { origin: "https://example.test" });
  await p.fill("#k70-draft", "Sure, sharing the photos now."); await p.click("[data-action=copy]"); await p.waitForTimeout(500);
  const clip = await p.evaluate(() => navigator.clipboard.readText()); ok("Copy puts the edited draft on the clipboard", clip === "Sure, sharing the photos now.", clip);
  ok("copy goes through gateway approval", gw.log.some((l) => l.path === "/approve" && l.body.decision === "approve_with_edit"));
  ok("page was never typed into and Send never pressed", !(await p.evaluate(() => window.__typed || window.__sent)) && (await p.inputValue("#box")) === "");
  // compose mode with Hinglish instruction
  await p.click("[data-mode=compose]"); await p.fill("#k70-instruction", "Customer ko politely bolo main Thursday available hoon.");
  await p.click("[data-tone=professional]"); await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 });
  const c2 = gw.log.filter((l) => l.path === "/ingest").pop().body;
  ok("compose sends the instruction as an instruction, with no page text", c2.mode === "compose" && c2.instructions.includes("Thursday") && c2.text === "", JSON.stringify(c2));
  // discard
  await p.click("[data-action=reject]"); await p.waitForTimeout(300); ok("discard hides the draft", await p.locator("[data-result]").isHidden());
  // gateway failure then recovery
  await p.request.post("http://127.0.0.1:" + PORT + "/__fail", { data: { on: true } });
  await p.click("[data-action=generate]"); await p.waitForTimeout(1500);
  const st = await p.locator(".k70-status").innerText(); ok("gateway down shows an error and no draft", /unavailable|error|gateway/i.test(st) && await p.locator("[data-result]").isHidden(), st);
  await p.request.post("http://127.0.0.1:" + PORT + "/__fail", { data: { on: false } });
  await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 }); ok("recovers after the gateway comes back", true);
  // drag
  const before = await panel(p).boundingBox(); const h = await p.locator(".k70-header").boundingBox();
  await p.mouse.move(h.x + 40, h.y + 12); await p.mouse.down(); await p.mouse.move(h.x - 160, h.y + 90, { steps: 6 }); await p.mouse.up();
  const after = await panel(p).boundingBox(); ok("drag moves the panel and keeps it on screen", Math.abs(after.x - before.x) > 80 && after.x >= 0 && after.y >= 0, JSON.stringify([before, after]));
  // minimize, then icon click closes it
  await p.click("[data-action=collapse] >> nth=0"); ok("minimize shows the launcher", await p.locator(".k70-launcher").isVisible());
  await clickIcon("https://example.test/*"); await p.waitForTimeout(300); ok("clicking the icon again closes the popup", await panel(p).count() === 0);
  ok("no JS errors", p.errs.length === 0, p.errs.join("|")); await p.close();
}
// ---- browser-internal pages are skipped safely ----
{ const p = await ctx.newPage(); await p.goto("about:blank"); await sw.evaluate(async () => { const t = (await chrome.tabs.query({ active: true }))[0]; await openPopup(t); }); ok("no popup injected into non-web pages", await panel(p).count() === 0); await p.close(); }
const paths = new Set(gw.log.map((l) => l.path)); ok("only gateway endpoints were called", [...paths].every((x) => ["/ingest", "/approve", "/config", "/__fail"].includes(x) || x.startsWith("/draft/")), [...paths].join(","));
await ctx.close(); gw.close(); console.log(fails ? "FAILED " + fails : "ALL PASS"); process.exit(fails ? 1 : 0);
