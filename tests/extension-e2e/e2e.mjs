// End-to-end test of the REAL unpacked extension in Chromium against synthetic sites + a mock gateway.
// Usage: PW_DIR=<dir containing node_modules/playwright> node tests/extension-e2e/e2e.mjs
import { createRequire } from "node:module";
import path from "node:path"; import os from "node:os"; import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { startMockGateway } from "./mock-gateway.mjs";
import { LINKEDIN, WHATSAPP, INSTAGRAM } from "./sites.mjs";
const require = createRequire(path.join(process.env.PW_DIR || process.cwd(), "x.js"));
const { chromium } = require("playwright");
const SRC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../services/extension");
const PORT = 18999;
// Test copy of the extension pointing at the mock gateway port (the shipped files are not modified).
const EXT = fs.mkdtempSync(path.join(os.tmpdir(), "k70-ext-"));
fs.cpSync(SRC, EXT, { recursive: true });
for (const f of ["background.js", "manifest.json"]) {
  const p = path.join(EXT, f); fs.writeFileSync(p, fs.readFileSync(p, "utf8").replaceAll("http://localhost:8080", "http://127.0.0.1:" + PORT));
}
let fails = 0; const ok = (n, c, x = "") => { console.log((c ? "PASS " : "FAIL ") + n + (c ? "" : "  -> " + x)); if (!c) fails++; };
const gw = await startMockGateway(PORT);
const ctx = await chromium.launchPersistentContext(fs.mkdtempSync(path.join(os.tmpdir(), "k70-")), {
  channel: "chromium", headless: true, viewport: { width: 1200, height: 800 },
  args: ["--disable-extensions-except=" + EXT, "--load-extension=" + EXT] });
let sw = ctx.serviceWorkers()[0] || await ctx.waitForEvent("serviceworker");
const setFlags = (f) => sw.evaluate((f) => chrome.storage.local.set({ k70Platforms: f }), f);
const sites = { linkedin: ["https://www.linkedin.com/messaging/thread/abc/", LINKEDIN], whatsapp: ["https://web.whatsapp.com/", WHATSAPP], instagram: ["https://www.instagram.com/direct/t/1/", INSTAGRAM] };
async function open(name, url = sites[name][0]) {
  const p = await ctx.newPage(); const errs = []; p.on("pageerror", (e) => errs.push(String(e)));
  await p.route(/^https:\/\/(www\.linkedin\.com|web\.whatsapp\.com|www\.instagram\.com)\//, (r) => r.fulfill({ contentType: "text/html", body: sites[name][1] }));
  await p.goto(url); p.errs = errs; return p;
}
const panel = (p) => p.locator("#k70-copilot");
const launcher = (p) => p.locator("#k70-copilot .k70-launcher");
const expand = async (p) => { if (await launcher(p).isVisible()) await launcher(p).click(); await p.waitForTimeout(200); };

// ---- consent: WhatsApp/Instagram are OFF by default ----
await setFlags({});
for (const n of ["whatsapp", "instagram"]) { const p = await open(n); await p.waitForTimeout(2200); ok(n + ": no popup before the user enables it", await panel(p).count() === 0); await p.close(); }
{ const p = await open("linkedin"); await p.waitForTimeout(1500); ok("linkedin: popup appears (default on)", await panel(p).count() === 1); await p.close(); }
await setFlags({ whatsapp: true, instagram: true });

// ---- WhatsApp ----
{
  const p = await open("whatsapp"); await p.waitForSelector("#k70-copilot", { timeout: 8000 }); await expand(p);
  ok("whatsapp: popup says WhatsApp", (await p.locator(".k70-safety").innerText()).includes("WhatsApp"));
  ok("whatsapp: nothing captured before the user asks", (await p.inputValue("#k70-thread")) === "");
  await p.click("[data-action=capture]");
  const cap = await p.inputValue("#k70-thread");
  ok("whatsapp: captures only the open chat", cap.includes("sample photos") && cap.includes("MOQ") && !cap.includes("bank pin") && !cap.includes("SECRET"), cap);
  await p.click("[data-tone=friendly]"); await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 });
  const ing = gw.log.filter((l) => l.path === "/ingest").pop().body;
  ok("whatsapp: ingest carries mode=reply, tone=friendly, captured text", ing.mode === "reply" && ing.tone === "friendly" && ing.text.includes("MOQ"), JSON.stringify(ing));
  await p.fill("#k70-draft", "Sure Saima, sharing the photos now."); await p.click("[data-action=approve]"); await p.waitForTimeout(500);
  ok("whatsapp: approved text inserted into composer", (await p.locator("#composer").innerText()).includes("sharing the photos now"));
  ok("whatsapp: Send was NOT pressed", !(await p.evaluate(() => window.__sent)));
  ok("whatsapp: composer inserted only after gateway approval", gw.log.map((l) => l.path).join(",").includes("/approve,/injected"));
  ok("whatsapp: no JS errors", p.errs.length === 0, p.errs.join("|")); await p.close();
}
// ---- Instagram ----
{
  const p = await open("instagram"); await p.waitForSelector("#k70-copilot", { timeout: 8000 }); await expand(p);
  await p.click("[data-mode=compose]"); await p.fill("#k70-instruction", "Is message ko professional English mein likho: collab ke liye shukriya, main Friday free hoon.");
  await p.click("[data-tone=professional]"); await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 });
  const ing = gw.log.filter((l) => l.path === "/ingest").pop().body;
  ok("instagram: compose sends mode=compose + Hinglish instruction, no chat text unless ticked", ing.mode === "compose" && ing.instructions.includes("professional English") && ing.text === "", JSON.stringify(ing));
  await p.click("[data-action=approve]"); await p.waitForTimeout(500);
  ok("instagram: draft inserted into the DM box", (await p.locator("#composer").innerText()).includes("DRAFT[compose/professional]"));
  ok("instagram: Send was NOT pressed", !(await p.evaluate(() => window.__sent)));
  ok("instagram: no JS errors", p.errs.length === 0, p.errs.join("|")); await p.close();
  const q = await open("instagram", "https://www.instagram.com/explore/"); await q.waitForTimeout(2200); ok("instagram: no popup outside /direct/", await panel(q).count() === 0); await q.close();
}
// ---- LinkedIn (existing flow must keep working) ----
{
  const p = await open("linkedin"); await p.waitForSelector("#k70-copilot"); await expand(p);
  await p.click("[data-action=capture]"); ok("linkedin: capture reads the thread", (await p.inputValue("#k70-thread")).includes("Thursday"));
  await p.click("[data-tone=short]"); await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 });
  await p.click("[data-action=approve]"); await p.waitForTimeout(500);
  ok("linkedin: approve inserts into composer", (await p.locator("#composer").innerText()).includes("DRAFT[reply/short]"));
  ok("linkedin: Send was NOT pressed", !(await p.evaluate(() => window.__sent)));
  // discard never inserts
  await p.evaluate(() => { document.getElementById("composer").innerHTML = ""; });
  await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 }); await p.click("[data-action=reject]"); await p.waitForTimeout(400);
  ok("linkedin: discard inserts nothing", (await p.locator("#composer").innerText()).trim() === "");
  // gateway failure then reconnection
  await p.request.post("http://127.0.0.1:" + PORT + "/__fail", { data: { on: true } });
  await p.click("[data-action=generate]"); await p.waitForTimeout(1500);
  const st = await p.locator(".k70-status").innerText(); ok("linkedin: gateway down shows an error and no draft", /unavailable|error|gateway/i.test(st) && await p.locator("[data-result]").isHidden(), st);
  await p.request.post("http://127.0.0.1:" + PORT + "/__fail", { data: { on: false } });
  await p.click("[data-action=generate]"); await p.waitForSelector("#k70-draft", { state: "visible", timeout: 15000 }); ok("linkedin: recovers after the gateway comes back", true);
  // minimize / reopen / drag
  await p.click("[data-action=collapse] >> nth=0"); ok("minimize shows the launcher", await launcher(p).isVisible());
  await expand(p); const before = await panel(p).boundingBox(); const h = await p.locator(".k70-header").boundingBox();
  await p.mouse.move(h.x + 40, h.y + 12); await p.mouse.down(); await p.mouse.move(h.x - 160, h.y + 90, { steps: 6 }); await p.mouse.up();
  const after = await panel(p).boundingBox(); ok("drag moves the panel and keeps it on screen", Math.abs(after.x - before.x) > 80 && after.x >= 0 && after.y >= 0, JSON.stringify([before, after]));
  ok("linkedin: no JS errors", p.errs.length === 0, p.errs.join("|")); await p.close();
}
// ---- no unapproved destinations ----
const paths = new Set(gw.log.map((l) => l.path)); ok("only gateway endpoints were called", [...paths].every((x) => ["/ingest", "/approve", "/injected", "/observed", "/config", "/__fail"].includes(x) || x.startsWith("/draft/")), [...paths].join(","));
await ctx.close(); gw.close(); console.log(fails ? "FAILED " + fails : "ALL PASS"); process.exit(fails ? 1 : 0);
