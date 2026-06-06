/* Live-DOM logic verification for the extension content script — WITHOUT touching
 * real LinkedIn or any account. Loads the REAL services/extension/content.js into
 * jsdom (shipped file untouched; a test-export hook is injected at load time only)
 * and exercises every selector + guard against realistic LinkedIn-like fixtures.
 *
 * Covers the user's gate: selector failures, wrong-profile injection, empty note,
 * dedupe, daily cap, recipient verification, verify-after-write.
 *
 * NOTE: jsdom has no layout engine (offsetWidth/Height are 0), so we patch those
 * getters to honor display/visibility — that makes isVisible() faithful. What this
 * CANNOT prove is whether the selectors match LinkedIn's CURRENT obfuscated class
 * names on the live site; that last mile needs a human on a real page (runbook).
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("/tmp/lc-domtest/node_modules/jsdom");

const SRC = path.resolve(__dirname, "../services/extension/content.js");
let code = fs.readFileSync(SRC, "utf8");

// Inject a test-export hook right before the IIFE's closing `})();`.
const HOOK =
  "\n;window.__lc = { pageType, inviteModal, inviteModalRecipient, connectionNoteField," +
  " injectIntoConnectionNote, nameMatches, isVisible, scanProfiles, captureProfile," +
  " todayStr, dailyUsed, bumpDaily, markProcessed, processedSet };\n";
const lastClose = code.lastIndexOf("})();");
if (lastClose < 0) throw new Error("could not find IIFE close");
code = code.slice(0, lastClose) + HOOK + code.slice(lastClose);

let pass = 0, fail = 0;
const results = [];
function check(name, cond, detail) {
  (cond ? pass++ : fail++);
  results.push(`${cond ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

function freshDom(url, body) {
  const dom = new JSDOM(`<!DOCTYPE html><html><body>${body || ""}</body></html>`, {
    url, runScripts: "outside-only", pretendToBeVisual: true,
  });
  const { window } = dom;
  // chrome stub (in-memory storage + no-op messaging).
  const store = {};
  window.chrome = {
    runtime: { sendMessage: (_m, cb) => cb && cb({ ok: true }) },
    storage: { local: {
      get: (keys, cb) => { const o = {}; (Array.isArray(keys) ? keys : [keys]).forEach((k) => (o[k] = store[k])); cb(o); },
      set: (obj, cb) => { Object.assign(store, obj); cb && cb(); },
    } },
  };
  // Patch layout: offsetWidth/Height honor display:none / visibility:hidden.
  const proto = window.HTMLElement.prototype;
  Object.defineProperty(proto, "offsetWidth", { configurable: true, get() {
    const cs = window.getComputedStyle(this);
    return (cs.display === "none" || cs.visibility === "hidden") ? 0 : 120;
  }});
  Object.defineProperty(proto, "offsetHeight", { configurable: true, get() {
    const cs = window.getComputedStyle(this);
    return (cs.display === "none" || cs.visibility === "hidden") ? 0 : 40;
  }});
  // innerText shim (jsdom only has textContent).
  Object.defineProperty(proto, "innerText", {
    configurable: true,
    get() { return this.textContent; },
    set(v) { this.textContent = v; },
  });
  // Run the content script in the window.
  const script = new window.Function(code);
  // Execute in window scope: use the window's eval so globals/closures bind to it.
  window.eval(code);
  return window;
}

// ---- Fixtures -------------------------------------------------------------
const INVITE_MODAL = (recipient) => `
  <div role="dialog" class="artdeco-modal" aria-label="Invite">
    <h2 id="ember-h">Invite ${recipient} to connect</h2>
    <p>Add a note to personalize your invitation</p>
    <textarea id="custom-message" name="message"></textarea>
  </div>`;

const SEARCH_CARDS = `
 <ul>
  <li class="reusable-search__result-container">
    <a href="/in/gianluca-orsini/"><span aria-hidden="true">Gianluca Orsini</span></a>
    <div class="entity-result__primary-subtitle">CTO at MLG Group</div>
    <div class="entity-result__secondary-subtitle">Milan, Italy</div>
    <span>12 mutual connections</span>
  </li>
  <li class="reusable-search__result-container">
    <a href="/in/ayesha-khan/"><span aria-hidden="true">Ayesha Khan</span></a>
    <div class="entity-result__primary-subtitle">Founder &amp; CEO at Loom Textiles</div>
    <div class="entity-result__secondary-subtitle">Karachi</div>
  </li>
  <li class="reusable-search__result-container">
    <a href="/in/ayesha-khan/"><span aria-hidden="true">Ayesha Khan</span></a>  <!-- duplicate id in same scan -->
    <div class="entity-result__primary-subtitle">dupe card</div>
  </li>
  <li class="reusable-search__result-container">
    <a href="/in/daniel-park/"><span aria-hidden="true">Daniel Park</span></a>
    <button><a href="/in/daniel-park/"><span aria-hidden="true">Connect</span></a></button>
    <div class="entity-result__primary-subtitle">Co-founder at Northwind Robotics</div>
  </li>
  <li class="reusable-search__result-container">
    <a href="/in/anon/"><span aria-hidden="true">LinkedIn Member</span></a>
    <div class="entity-result__primary-subtitle">out of network</div>
  </li>
 </ul>`;

(async function main() {
  // ===== 1. scanProfiles: selectors, dedupe within scan, junk-anchor skip =====
  {
    const w = freshDom("https://www.linkedin.com/search/results/people/", SEARCH_CARDS);
    const found = w.__lc.scanProfiles();
    const ids = found.map((f) => f.profile_id);
    check("scan finds real profiles", ids.includes("gianluca-orsini") && ids.includes("ayesha-khan") && ids.includes("daniel-park"), ids.join(","));
    check("scan dedupes duplicate id within a scan", ids.filter((i) => i === "ayesha-khan").length === 1);
    check("scan skips 'Connect' button anchor as a name", !found.some((f) => /^connect$/i.test(f.name)));
    check("scan skips 'LinkedIn Member'", !ids.includes("anon"));
    const g = found.find((f) => f.profile_id === "gianluca-orsini");
    check("scan reads headline", g && g.headline === "CTO at MLG Group", g && g.headline);
    check("scan reads location", g && g.location === "Milan, Italy", g && g.location);
    check("scan reads mutual as COUNT only (no names)", g && g.mutual === "12 mutual connections", g && g.mutual);
  }

  // ===== 2. inviteModal / recipient / note-field scoping =====
  {
    // An UNRELATED hidden modal + the real visible invite modal: must pick the invite.
    const body = `
      <div role="dialog" class="artdeco-modal" style="display:none"><textarea id="other"></textarea>Some other modal</div>
      ${INVITE_MODAL("Gianluca Orsini")}`;
    const w = freshDom("https://www.linkedin.com/in/gianluca-orsini/", body);
    const modal = w.__lc.inviteModal();
    check("inviteModal finds the visible invite dialog", !!modal);
    check("inviteModal ignores a hidden unrelated modal", modal && /Invite Gianluca/.test(modal.textContent));
    check("inviteModalRecipient parses the header name", w.__lc.inviteModalRecipient(modal) === "Gianluca Orsini", w.__lc.inviteModalRecipient(modal));
    const field = w.__lc.connectionNoteField();
    check("connectionNoteField is scoped to the invite modal", field && field.id === "custom-message");
  }

  // ===== 3. injectIntoConnectionNote: happy path + verify-after-write =====
  {
    const w = freshDom("https://www.linkedin.com/in/gianluca-orsini/", INVITE_MODAL("Gianluca Orsini"));
    const note = "Hi Gianluca, CTO at MLG Group in Milan — what's the day-to-day focus of your role there?";
    const r = w.__lc.injectIntoConnectionNote(note, "Gianluca Orsini");
    check("inject ok for matching recipient", r.ok && r.reason === "ok", JSON.stringify(r));
    check("inject actually wrote the note (verify-after-write)", w.document.querySelector("#custom-message").value === note);
    check("written note is NOT empty", (w.document.querySelector("#custom-message").value || "").trim().length > 0);
  }

  // ===== 4. WRONG-PROFILE guard: right note must refuse the wrong person =====
  {
    const w = freshDom("https://www.linkedin.com/in/ayesha-khan/", INVITE_MODAL("Daniel Park"));
    const r = w.__lc.injectIntoConnectionNote("Hi Ayesha, Founder & CEO at Loom Textiles...", "Ayesha Khan");
    check("inject REFUSES on confident recipient mismatch", !r.ok && r.reason === "wrong-profile", JSON.stringify(r));
    check("wrong-profile leaves the textarea EMPTY (no leak)", (w.document.querySelector("#custom-message").value || "") === "");
    check("wrong-profile surfaces the actual recipient", r.recipient === "Daniel Park", r.recipient);
  }

  // ===== 5. Guards: no modal / no textarea =====
  {
    const w = freshDom("https://www.linkedin.com/in/x/", "<div>no modal here</div>");
    const r = w.__lc.injectIntoConnectionNote("note", "X");
    check("inject returns no-modal when invite not open", !r.ok && r.reason === "no-modal", JSON.stringify(r));
  }
  {
    const w = freshDom("https://www.linkedin.com/in/x/",
      `<div role="dialog" class="artdeco-modal">Invite X to connect<p>add a note</p></div>`);
    const r = w.__lc.injectIntoConnectionNote("note", "X");
    check("inject returns no-textarea when note field absent", !r.ok && r.reason === "no-textarea", JSON.stringify(r));
  }

  // ===== 6. nameMatches permissiveness (never strand the user) =====
  {
    const w = freshDom("https://x/", "");
    const nm = w.__lc.nameMatches;
    check("nameMatches: same first name passes", nm("Gianluca Orsini", "Gianluca") === true);
    check("nameMatches: different first name blocks", nm("Ayesha Khan", "Daniel Park") === false);
    check("nameMatches: unknown recipient does NOT block (permissive)", nm("Ayesha Khan", "") === true);
  }

  // ===== 7. Daily cap + dedupe persistence (chrome.storage) =====
  {
    const w = freshDom("https://www.linkedin.com/search/results/people/", "");
    check("dailyUsed starts at 0", (await w.__lc.dailyUsed()) === 0);
    await w.__lc.bumpDaily(7);
    check("bumpDaily accrues today's count", (await w.__lc.dailyUsed()) === 7);
    await w.__lc.bumpDaily(5);
    check("daily cap accounting accumulates (12 used)", (await w.__lc.dailyUsed()) === 12);
    await w.__lc.markProcessed(["gianluca-orsini", "ayesha-khan"]);
    const seen = await w.__lc.processedSet();
    check("dedupe set persists processed ids", seen.has("gianluca-orsini") && seen.has("ayesha-khan"));
    await w.__lc.markProcessed(["daniel-park"]);
    const seen2 = await w.__lc.processedSet();
    check("dedupe set unions across calls", seen2.size === 3 && seen2.has("daniel-park"));
  }

  // ===== 8. pageType routing =====
  {
    const mk = (u) => freshDom(u, "").__lc.pageType();
    check("pageType MESSAGING", mk("https://www.linkedin.com/messaging/thread/abc/") === "MESSAGING");
    check("pageType LIST_PAGE (search)", mk("https://www.linkedin.com/search/results/people/?k=founder") === "LIST_PAGE");
    check("pageType LIST_PAGE (mynetwork)", mk("https://www.linkedin.com/mynetwork/") === "LIST_PAGE");
    check("pageType PROFILE_PAGE", mk("https://www.linkedin.com/in/gianluca-orsini/") === "PROFILE_PAGE");
  }

  console.log(results.join("\n"));
  console.log(`\n==== DOM LOGIC VERIFY: ${pass} passed, ${fail} failed ====`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error("HARNESS ERROR:", e); process.exit(2); });
