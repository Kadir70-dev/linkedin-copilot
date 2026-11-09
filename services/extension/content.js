/* LinkedIn Copilot content script — OBSERVE + INJECT ONLY.
 *
 * HARD BOUNDARY (safety/frozen/extension-forbidden-api.txt): this file contains
 * NO .click() on Send/Connect, NO form.submit()/requestSubmit(), NO synthetic
 * Enter/keydown, NO fetch to LinkedIn. The ONLY DOM writes are setting the
 * messaging composer's text and the connection-note field's text (+ an 'input'
 * event so LinkedIn registers them). The human clicks Send / Connect.
 *
 * Two modes, auto-detected from the URL:
 *   - MESSAGING (/messaging/...) -> reply flow (Phase 0 MVP, unchanged).
 *   - PROFILE_PAGE (/in/...)     -> AI Outreach flow (Phase 1).
 */
(function () {
  "use strict";

  const call = (action, payload) =>
    new Promise((res) => chrome.runtime.sendMessage({ action, payload }, res));

  // ---- page-type detection ----
  function pageType() {
    if (/\/messaging\//.test(location.pathname)) return "MESSAGING";
    // Phase 2: search-results / connections / people-list pages -> batch queue.
    if (/\/search\/results\/(people|all)/.test(location.pathname) ||
        /\/mynetwork\//.test(location.pathname)) return "LIST_PAGE";
    if (/\/in\//.test(location.pathname)) return "PROFILE_PAGE";
    return "OTHER";
  }

  // ---- tiny async/util helpers shared by the batch queue ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const jitter = (base) => base + Math.floor(Math.random() * base); // base..2*base
  const storeGet = (k, def) =>
    new Promise((res) => chrome.storage.local.get([k], (o) => res(o[k] === undefined ? def : o[k])));
  const storeSet = (k, v) => new Promise((res) => chrome.storage.local.set({ [k]: v }, res));

  // ---- shared UI utilities (toast / visibility / name match / error copy) ----

  // A transient, non-blocking status toast. Pure DOM; touches nothing on
  // LinkedIn. kind: "ok" (green) | "err" (red) | "info" (blue).
  function toast(msg, kind) {
    const colors = { ok: "#057642", err: "#b00020", info: "#0a66c2" };
    document.querySelectorAll("[data-lc-toast]").forEach((n) => n.remove());
    const t = document.createElement("div");
    t.setAttribute("data-lc-toast", "1");
    t.textContent = msg;
    t.style.cssText =
      "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:1000001;" +
      "max-width:440px;padding:10px 16px;border-radius:8px;color:#fff;text-align:center;" +
      "font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;box-shadow:0 6px 24px rgba(0,0,0,.25);" +
      "opacity:0;transition:opacity .2s;background:" + (colors[kind] || colors.info);
    document.body.appendChild(t);
    requestAnimationFrame(() => (t.style.opacity = "1"));
    setTimeout(() => {
      t.style.opacity = "0";
      setTimeout(() => t.remove(), 300);
    }, 3400);
  }

  // Render the HERMES structured understanding (single-person path) into `el`.
  // `raw` is the JSON string the gateway returns in `hermes`. Read-only display
  // of what the deterministic orchestration layer extracted before the writer ran.
  function renderHermes(el, raw) {
    if (!el) return;
    let h;
    try { h = raw ? JSON.parse(raw) : null; } catch (_) { h = null; }
    if (!h) { el.style.display = "none"; return; }
    const esc = (s) => String(s == null ? "" : s).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
    const row = (k, v) => (v && String(v).length ? `<div><b>${k}:</b> ${esc(v)}</div>` : "");
    const kw = (h.keywords || []).join(", ");
    const facts = (h.grounded_facts || []).join(" · ");
    const forb = (h.forbidden || []).slice(0, 6).join(", ");
    el.innerHTML =
      `<div style="font-weight:700;color:#0a66c2;margin-bottom:2px">🧭 Hermes understanding</div>` +
      row("role", h.role) + row("company", h.company) + row("niche", h.niche) +
      row("geo", h.geo) + row("seniority", h.seniority) +
      (h.mutuals ? `<div><b>mutuals:</b> ${esc(h.mutuals)}</div>` : "") +
      row("angle", h.angle) + row("tone", h.tone) +
      (kw ? `<div><b>keywords:</b> ${esc(kw)}</div>` : "") +
      (facts ? `<div><b>grounded facts:</b> ${esc(facts)}</div>` : "") +
      (forb ? `<div style="color:#b26a00"><b>forbidden:</b> ${esc(forb)}…</div>` : "");
    el.style.display = "block";
  }

  // Real visibility check. NB: a `position:fixed` modal has a null offsetParent
  // even when visible, so we must test box dimensions + computed style instead.
  const isVisible = (el) =>
    !!el && (el.offsetWidth > 0 || el.offsetHeight > 0) &&
    getComputedStyle(el).visibility !== "hidden" &&
    getComputedStyle(el).display !== "none";

  // First-name match — the correctness guard for "right note → right person".
  // Permissive by design: if either side is unknown we DON'T block (return true),
  // so a modal whose recipient we can't parse never strands the user. It only
  // returns false on a confident first-name mismatch (e.g. Daniel vs Ayesha).
  function nameMatches(expected, actual) {
    const first = (s) => (s || "").trim().toLowerCase().split(/\s+/)[0] || "";
    const a = first(expected), b = first(actual);
    if (!a || !b) return true;
    return a === b || a.startsWith(b) || b.startsWith(a);
  }

  // Map an injection reason code -> human-readable, actionable guidance.
  function injectMsg(r) {
    switch (r && r.reason) {
      case "no-modal":
        return "No invite open. Click LinkedIn's Connect → Add a note first, then Inject.";
      case "no-textarea":
        return "Invite is open but no note field — click 'Add a note' inside it.";
      case "wrong-profile":
        return `That open invite is for ${r.recipient || "someone else"}, not this person. Open the right profile's Add-a-note.`;
      case "verify-failed":
        return "Filled the note but it didn't stick. Click inside the note field and Inject again.";
      default:
        return "Couldn't fill the connection note.";
    }
  }

  // =====================================================================
  //  MESSAGING MODE (Phase 0 — reply flow, preserved as-is)
  // =====================================================================

  // ---- thread identity (best-effort from the URL) ----
  function threadId() {
    const m = location.pathname.match(/messaging\/thread\/([^/]+)/);
    return (m && m[1]) || location.pathname.replace(/\W+/g, "_").slice(-40) || "demo-thread";
  }

  // ---- capture the open thread text (best-effort; user can edit/paste) ----
  function captureThread() {
    const sel = [
      ".msg-s-event-listitem__body",
      ".msg-s-message-group__meta",
      "p.msg-s-event-listitem__body",
    ].join(",");
    const nodes = document.querySelectorAll(sel);
    const txt = [...nodes].map((n) => n.innerText.trim()).filter(Boolean).join("\n");
    return txt;
  }

  // ---- find the composer (contenteditable) ----
  function composer() {
    return (
      document.querySelector('div.msg-form__contenteditable[contenteditable="true"]') ||
      document.querySelector('[role="textbox"][contenteditable="true"]') ||
      document.querySelector('div[contenteditable="true"]')
    );
  }

  // ---- INJECT ONLY: set composer text + dispatch 'input'. No submit, no Enter. ----
  function injectIntoComposer(text) {
    const box = composer();
    if (!box) return false;
    box.focus();
    box.innerHTML = "";
    const p = document.createElement("p");
    p.textContent = text;
    box.appendChild(p);
    // Minimal events so LinkedIn registers typed text. NOT submit, NOT keydown-Enter.
    box.dispatchEvent(new InputEvent("input", { bubbles: true }));
    box.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  // ---- observe a HUMAN send (passive): a new outbound bubble appears ----
  function observeHumanSend(tid) {
    const list =
      document.querySelector(".msg-s-message-list-content") ||
      document.querySelector(".msg-s-message-list") ||
      document.body;
    let fired = false;
    const obs = new MutationObserver((muts) => {
      if (fired) return;
      for (const m of muts) {
        if (m.addedNodes && m.addedNodes.length) {
          fired = true;
          obs.disconnect();
          call("observed", { thread_id: tid });
          setStatus("✅ Observed your send — recorded as SENT_BY_HUMAN.");
          return;
        }
      }
    });
    obs.observe(list, { childList: true, subtree: true });
    // safety auto-stop after 5 min
    setTimeout(() => obs.disconnect(), 300000);
  }

  // ---------------- messaging UI panel ----------------
  let elStatus, elDraft;

  function setStatus(s) {
    if (elStatus) elStatus.textContent = s;
  }

  function panel() {
    const wrap = document.createElement("div");
    wrap.style.cssText =
      "position:fixed;right:16px;bottom:16px;width:320px;z-index:999999;background:#fff;" +
      "border:1px solid #0a66c2;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.2);" +
      "font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;overflow:hidden";
    wrap.setAttribute("data-lc-panel", "1");
    wrap.innerHTML = `
      <div style="background:#0a66c2;color:#fff;padding:8px 12px;font-weight:600">
        LinkedIn Copilot <span style="font-weight:400;opacity:.8">· inject only</span>
      </div>
      <div style="padding:10px 12px">
        <textarea id="lc-thread" placeholder="Captured thread text (editable / paste here)"
          style="width:100%;height:64px;box-sizing:border-box;resize:vertical;margin-bottom:6px"></textarea>
        <div style="display:flex;gap:6px;margin-bottom:8px">
          <button id="lc-capture" style="flex:1">Capture</button>
          <button id="lc-suggest" style="flex:2;background:#0a66c2;color:#fff;border:none;border-radius:4px;padding:6px;cursor:pointer">Get Suggestion</button>
        </div>
        <div id="lc-status" style="color:#0a66c2;min-height:18px;margin-bottom:6px"></div>
        <textarea id="lc-draft" placeholder="Suggested reply will appear here"
          style="width:100%;height:80px;box-sizing:border-box;resize:vertical;display:none;margin-bottom:6px"></textarea>
        <div id="lc-actions" style="display:none;gap:6px">
          <button id="lc-approve" style="flex:2;background:#057642;color:#fff;border:none;border-radius:4px;padding:6px;cursor:pointer">Approve → Inject</button>
          <button id="lc-reject" style="flex:1">Reject</button>
        </div>
        <div style="font-size:11px;color:#666;margin-top:8px">
          After injecting, <b>you</b> click LinkedIn's Send. This extension never sends.
        </div>
      </div>`;
    document.body.appendChild(wrap);

    elStatus = wrap.querySelector("#lc-status");
    elDraft = wrap.querySelector("#lc-draft");
    const elThread = wrap.querySelector("#lc-thread");
    const elActions = wrap.querySelector("#lc-actions");

    wrap.querySelector("#lc-capture").onclick = () => {
      elThread.value = captureThread() || "";
      setStatus(elThread.value ? "Captured thread." : "Nothing captured — paste text.");
    };

    wrap.querySelector("#lc-suggest").onclick = async () => {
      const text = elThread.value.trim() || captureThread();
      if (!text) return setStatus("No thread text. Capture or paste first.");
      const tid = threadId();
      setStatus("Sending to gateway…");
      const ing = await call("ingest", { thread_id: tid, text });
      if (ing && ing.error) {
        const m = "Gateway unreachable — is the local gateway running on localhost:8080?";
        setStatus(m); toast(m, "err"); return;
      }
      setStatus("AI is drafting (local Qwen)… this can take a bit.");
      // poll until PENDING_APPROVAL
      for (let i = 0; i < 90; i++) {
        const d = await call("getDraft", { thread_id: tid });
        if (d && d.error) {
          const m = "Gateway unreachable — is the local gateway running on localhost:8080?";
          setStatus(m); toast(m, "err"); return;
        }
        if (d && d.state === "PENDING_APPROVAL") {
          elDraft.style.display = "block";
          elActions.style.display = "flex";
          elDraft.value = d.draft_text || "";
          setStatus("Review the suggested reply.");
          return;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      setStatus("Timed out waiting for a draft. Is the model pulled?");
    };

    wrap.querySelector("#lc-approve").onclick = async (e) => {
      const btnEl = e.currentTarget;
      const tid = threadId();
      btnEl.disabled = true;
      const old = btnEl.textContent;
      btnEl.textContent = "Approving…";
      try {
        const res = await call("approve", {
          thread_id: tid,
          decision: "approve_with_edit",
          edited_text: elDraft.value,
        });
        const text = (res && res.inject_text) || elDraft.value;
        const ok = injectIntoComposer(text);
        if (ok) {
          await call("injected", { thread_id: tid });
          setStatus("📝 Injected into composer. Now click LinkedIn's Send yourself.");
          toast("Injected into composer — you click Send", "ok");
          observeHumanSend(tid);
        } else {
          const m = "No composer found. Open a messaging thread and click in the message box.";
          setStatus(m);
          toast(m, "err");
        }
      } finally {
        btnEl.disabled = false;
        btnEl.textContent = old;
      }
    };

    wrap.querySelector("#lc-reject").onclick = async () => {
      await call("approve", { thread_id: threadId(), decision: "reject" });
      elDraft.style.display = "none";
      elActions.style.display = "none";
      setStatus("Rejected. No draft injected.");
    };
  }

  // =====================================================================
  //  PROFILE MODE (Phase 1 — AI Outreach flow)
  // =====================================================================

  // ---- profile identity (the /in/<slug> handle) ----
  function profileId() {
    const m = location.pathname.match(/\/in\/([^/]+)/);
    return (m && m[1]) || location.pathname.replace(/\W+/g, "_").slice(-40) || "profile";
  }

  const firstText = (sels) => {
    for (const s of sels) {
      const n = document.querySelector(s);
      if (n && n.innerText && n.innerText.trim()) return n.innerText.trim();
    }
    return "";
  };

  // ---- the profile OWNER's name, robustly. The page <title> ("Saima Shaikh |
  //      LinkedIn") is the single most reliable source and is immune to the
  //      mutual-connections list, so it wins. The top-card <h1> is the fallback.
  //      We deliberately DO NOT fall back to a bare `h1` (it can match a section
  //      heading or a mutual-connection name). ----
  function profileName() {
    const title = (document.title || "")
      .replace(/^\(\d+\+?\)\s*/, "")        // drop "(20+) " unread badge
      .replace(/\s*[|–\-·].*$/, "") // drop " | LinkedIn" and the rest
      .trim();
    if (title && !/linkedin/i.test(title)) return title;
    const h1 = firstText([
      ".pv-text-details__left-panel h1",
      "main section h1",
      "h1.text-heading-xlarge",
      "main h1",
    ]);
    // Guard: never accept a "name" that is really a mutual/connection label.
    if (h1 && !/mutual|connection|follower|following/i.test(h1)) return h1;
    return h1 || "";
  }

  // ---- capture the visible profile (best-effort; user can edit before sending) ----
  function captureProfile() {
    const name = profileName();
    const headline = firstText([
      "div.text-body-medium.break-words",
      ".pv-text-details__left-panel .text-body-medium",
      ".text-body-medium",
    ]);
    const location = firstText([
      "span.text-body-small.inline.t-black--light.break-words",
      ".pv-text-details__left-panel .text-body-small.inline",
    ]);

    // About section: find the section anchored by #about, take its visible text.
    let about = "";
    const aboutAnchor = document.querySelector("#about");
    if (aboutAnchor) {
      const sec = aboutAnchor.closest("section") || aboutAnchor.parentElement;
      if (sec) {
        const span = sec.querySelector('div.display-flex span[aria-hidden="true"]') ||
          sec.querySelector('span[aria-hidden="true"]');
        about = (span && span.innerText.trim()) || sec.innerText.trim();
      }
    }

    // Experience section: summarize the visible text of the experience block.
    let experience = "";
    const expAnchor = document.querySelector("#experience");
    if (expAnchor) {
      const sec = expAnchor.closest("section") || expAnchor.parentElement;
      if (sec) {
        const items = [...sec.querySelectorAll('span[aria-hidden="true"]')]
          .map((n) => n.innerText.trim()).filter(Boolean);
        experience = [...new Set(items)].slice(0, 6).join(" · ");
      }
    }

    // Mutual connections: capture ONLY the COUNT, never the names. Mutual-
    // connection labels read like "Yessica Bianchi and 12 other mutual
    // connections" — feeding those names to the model is exactly what made it
    // greet the wrong person. We strip names and keep a count/flag only.
    let mutual = "";
    const mutualNode = [...document.querySelectorAll("a, span")]
      .find((n) => /mutual connection/i.test(n.innerText || ""));
    if (mutualNode) {
      const t = mutualNode.innerText || "";
      const m = t.match(/(\d[\d,]*)\s+(?:other\s+)?mutual connection/i);
      mutual = m ? `${m[1]} mutual connections` : "Has mutual connections";
    }

    return { name, headline, location, about, experience, mutual };
  }

  function profileToText(p) {
    const lines = [];
    if (p.name) lines.push("Name: " + p.name);
    if (p.headline) lines.push("Headline: " + p.headline);
    if (p.location) lines.push("Location: " + p.location);
    if (p.about) lines.push("About: " + p.about);
    if (p.experience) lines.push("Experience: " + p.experience);
    if (p.mutual) lines.push("Mutual: " + p.mutual);
    return lines.join("\n");
  }

  // ---- locate the OPEN, VISIBLE "Add a note" invite dialog. Scoping every
  //      lookup to this dialog is what keeps us from ever writing into some
  //      unrelated modal's textarea. Returns the dialog element or null. ----
  function inviteModal() {
    const dialogs = [
      ...document.querySelectorAll('div[role="dialog"], .artdeco-modal'),
    ];
    for (const d of dialogs) {
      if (!isVisible(d)) continue;
      const t = d.innerText || "";
      const looksLikeInvite =
        d.querySelector("#custom-message") ||
        d.querySelector('textarea[name="message"]') ||
        /add a note|personalize.*invitation|invite .* to connect/i.test(t);
      if (looksLikeInvite) return d;
    }
    return null;
  }

  // ---- the invite's intended recipient, read from the modal header
  //      ("Invite <Name> to connect"). Empty if LinkedIn's copy omits it. ----
  function inviteModalRecipient(modal) {
    const t = (modal && modal.innerText) || "";
    const m = t.match(/invite\s+(.+?)\s+to connect/i);
    return m ? m[1].trim() : "";
  }

  // ---- find the connection-note field, scoped to the open invite modal first
  //      (only exists once "Add a note" is open). We write text ONLY. ----
  function connectionNoteField() {
    const modal = inviteModal();
    if (modal) {
      const f =
        modal.querySelector("#custom-message") ||
        modal.querySelector('textarea[name="message"]') ||
        modal.querySelector("textarea.connect-button-send-invite__custom-message") ||
        modal.querySelector("textarea");
      if (f) return f;
    }
    // Last-resort global lookups for older DOM layouts (still text-only).
    return (
      document.querySelector("#custom-message") ||
      document.querySelector('textarea[name="message"]')
    );
  }

  // ---- INJECT ONLY into the connection note. No Connect/Send click anywhere.
  //      Returns {ok, reason, recipient?} so callers can give precise guidance.
  //      reason ∈ ok | no-modal | no-textarea | wrong-profile | verify-failed.
  //      When `expectedName` is given, refuses to fill if the open invite is
  //      confidently for a different person (right note → right person). ----
  function injectIntoConnectionNote(text, expectedName) {
    const modal = inviteModal();
    if (!modal) return { ok: false, reason: "no-modal" };
    const box = connectionNoteField();
    if (!box) return { ok: false, reason: "no-textarea" };
    const recipient = inviteModalRecipient(modal);
    if (expectedName && recipient && !nameMatches(expectedName, recipient)) {
      return { ok: false, reason: "wrong-profile", recipient };
    }
    box.focus();
    // React-controlled textarea: set value via the native setter, then 'input'.
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, "value"
    ).set;
    setter.call(box, text);
    box.dispatchEvent(new InputEvent("input", { bubbles: true }));
    box.dispatchEvent(new Event("change", { bubbles: true }));
    // Verify the value actually took (some React forms reject programmatic sets).
    if (box.value !== text) return { ok: false, reason: "verify-failed", recipient };
    return { ok: true, reason: "ok", recipient };
  }

  // ---- observe a HUMAN-sent connection (passive): "Pending"/"invitation sent". ----
  function observeHumanConnection(pid) {
    let fired = false;
    const obs = new MutationObserver((muts) => {
      if (fired) return;
      for (const m of muts) {
        for (const node of m.addedNodes || []) {
          const t = (node.innerText || node.textContent || "");
          if (/invitation sent|pending|withdraw/i.test(t)) {
            fired = true;
            obs.disconnect();
            call("connectionObserved", { profile_id: pid });
            setStatusP("✅ Observed your connection request — CONNECTION_SENT_BY_HUMAN.");
            return;
          }
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => obs.disconnect(), 300000);
  }

  // ---------------- outreach UI panel ----------------
  let elStatusP, elOut;

  function setStatusP(s) {
    if (elStatusP) elStatusP.textContent = s;
  }

  function outreachPanel() {
    const wrap = document.createElement("div");
    wrap.style.cssText =
      "position:fixed;right:16px;bottom:16px;width:340px;z-index:999999;background:#fff;" +
      "border:1px solid #0a66c2;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.2);" +
      "font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;overflow:hidden";
    wrap.setAttribute("data-lc-panel", "1");
    wrap.innerHTML = `
      <div style="background:#0a66c2;color:#fff;padding:8px 12px;font-weight:600">
        LinkedIn Copilot · Outreach <span style="font-weight:400;opacity:.8">· inject only</span>
      </div>
      <div style="padding:10px 12px">
        <textarea id="lc-profile" placeholder="Captured profile context (editable / paste here)"
          style="width:100%;height:84px;box-sizing:border-box;resize:vertical;margin-bottom:6px"></textarea>
        <div style="display:flex;gap:6px;margin-bottom:8px">
          <button id="lc-capture-profile" style="flex:1">Capture Profile</button>
          <button id="lc-generate" style="flex:2;background:#0a66c2;color:#fff;border:none;border-radius:4px;padding:6px;cursor:pointer">Generate Outreach</button>
        </div>
        <div id="lc-status-p" style="color:#0a66c2;min-height:18px;margin-bottom:6px"></div>
        <div id="lc-style" style="font-size:11px;color:#666;margin-bottom:4px"></div>
        <div id="lc-hermes" style="display:none;font-size:11px;color:#33475b;background:#f3f6f8;border:1px solid #e1e6ea;border-radius:4px;padding:6px 8px;margin-bottom:6px;line-height:1.5"></div>
        <textarea id="lc-outreach" placeholder="Suggested connection note will appear here"
          style="width:100%;height:84px;box-sizing:border-box;resize:vertical;display:none;margin-bottom:6px"></textarea>
        <div id="lc-actions-p" style="display:none;gap:6px">
          <button id="lc-approve-p" style="flex:2;background:#057642;color:#fff;border:none;border-radius:4px;padding:6px;cursor:pointer">Approve &amp; Inject</button>
          <button id="lc-reject-p" style="flex:1">Reject</button>
        </div>
        <div style="font-size:11px;color:#666;margin-top:8px">
          Open LinkedIn's <b>Connect → Add a note</b>, then Approve. We fill the note only —
          <b>you</b> click Connect. This extension never sends.
        </div>
      </div>`;
    document.body.appendChild(wrap);

    elStatusP = wrap.querySelector("#lc-status-p");
    elOut = wrap.querySelector("#lc-outreach");
    const elProfile = wrap.querySelector("#lc-profile");
    const elStyle = wrap.querySelector("#lc-style");
    const elHermes = wrap.querySelector("#lc-hermes");
    const elActions = wrap.querySelector("#lc-actions-p");

    wrap.querySelector("#lc-capture-profile").onclick = () => {
      const p = captureProfile();
      const text = profileToText(p);
      elProfile.value = text;
      setStatusP(text ? `Captured profile${p.name ? " — " + p.name : ""}.` : "Nothing captured — paste profile text.");
    };

    wrap.querySelector("#lc-generate").onclick = async () => {
      const text = elProfile.value.trim() || profileToText(captureProfile());
      if (!text) return setStatusP("No profile text. Capture or paste first.");
      const pid = profileId();
      // Pass the target's name EXPLICITLY (parsed from the editable text so user
      // corrections win) so the model addresses the right person — never a mutual.
      const nm = text.match(/^\s*Name:\s*(.+)$/im);
      const name = (nm && nm[1].trim()) || "";
      setStatusP("Sending profile to gateway…");
      const ing = await call("profileIngest", { profile_id: pid, profile_text: text, name });
      if (ing && ing.error) {
        const m = "Gateway unreachable — is the local gateway running on localhost:8080?";
        setStatusP(m); toast(m, "err"); return;
      }
      setStatusP("AI is writing your outreach (local Qwen)… this can take a bit.");
      for (let i = 0; i < 90; i++) {
        const d = await call("getOutreach", { profile_id: pid });
        if (d && d.error) {
          const m = "Gateway unreachable — is the local gateway running on localhost:8080?";
          setStatusP(m); toast(m, "err"); return;
        }
        if (d && d.state === "OUTREACH_READY") {
          elOut.style.display = "block";
          elActions.style.display = "flex";
          elOut.value = d.outreach_text || "";
          const gc = parseFloat(d.grounding);
          const pc = parseFloat(d.personalization);
          const gtxt = isNaN(gc) ? "" : ` · grounding ${Math.round(gc * 100)}%${gc < 0.5 ? " ⚠" : ""}`;
          const ptxt = isNaN(pc) ? "" : ` · personalization ${Math.round(pc * 100)}%${pc < 0.5 ? " ⚠" : ""}`;
          const kp = d.key_phrases ? ` · grounded on: ${d.key_phrases}` : "";
          const conf = d.style_confidence ? ` (confidence: ${d.style_confidence})` : "";
          elStyle.textContent = d.style
            ? `Detected style: ${d.style}${conf} · ${(d.outreach_text || "").length} chars${ptxt}${gtxt}${kp}`
            : "";
          // Amber when routing confidence is low OR grounding/personalization is weak.
          const lowConf = (d.style_confidence || "").toLowerCase() === "low";
          elStyle.style.color = lowConf || (!isNaN(gc) && gc < 0.5) || (!isNaN(pc) && pc < 0.5) ? "#b26a00" : "#666";
          // Hermes structured understanding (single-person path) — the deterministic
          // profile spec the writer grounded on. Read-only transparency for the human.
          renderHermes(elHermes, d.hermes);
          setStatusP("Review the suggested connection note.");
          return;
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
      setStatusP("Timed out waiting for outreach. Is the model pulled?");
    };

    wrap.querySelector("#lc-approve-p").onclick = async (e) => {
      const btnEl = e.currentTarget;
      const pid = profileId();
      btnEl.disabled = true;
      const old = btnEl.textContent;
      btnEl.textContent = "Approving…";
      try {
        const res = await call("approveOutreach", {
          profile_id: pid,
          decision: "approve_with_edit",
          edited_text: elOut.value,
        });
        const text = (res && res.inject_text) || elOut.value;
        const r = injectIntoConnectionNote(text, profileName());
        if (r.ok) {
          await call("connectionInjected", { profile_id: pid });
          setStatusP("📝 Filled the note" + (r.recipient ? ` for ${r.recipient}` : "") +
            ". Now click LinkedIn's Connect yourself.");
          toast("Note injected — you click Connect", "ok");
          observeHumanConnection(pid);
        } else {
          setStatusP(injectMsg(r));
          toast(injectMsg(r), "err");
        }
      } finally {
        btnEl.disabled = false;
        btnEl.textContent = old;
      }
    };

    wrap.querySelector("#lc-reject-p").onclick = async () => {
      await call("approveOutreach", { profile_id: profileId(), decision: "reject" });
      elOut.style.display = "none";
      elActions.style.display = "none";
      setStatusP("Rejected. No note injected.");
    };
  }

  // =====================================================================
  //  LIST MODE (Phase 2 — Batch Outreach Queue)
  //  Scan visible profile cards -> bulk-draft -> inbox-style approval queue.
  //  READ-ONLY on LinkedIn (DOM scan only). No navigation, no clicks on LinkedIn
  //  controls, no LinkedIn network calls. The AI NEVER sends; the human clicks
  //  Connect. Injection (note text only) happens per-profile when the human has
  //  the "Add a note" modal open.
  // =====================================================================

  // Map filter chips -> keyword sets (matched case-insensitively with includes()).
  // Substrings are intentional: "found" matches founder/co-founder/founding/
  // founding partner; "ceo & founder" and "startup founder" both contain "found".
  const FILTER_KEYWORDS = {
    recruiter: ["recruit", "talent", "sourcer", "ta partner", "hiring"],
    founder: ["found", "ceo", "owner", "entrepreneur", "co-found", "cofound", "founding partner"],
    "hiring manager": ["hiring", "head of", "people ops", "vp ", "director of", "talent"],
    sales: ["sales", "account executive", " ae ", "business development", "bdm", "revenue", "gtm"],
    // Fashion / apparel sourcing vertical (MODE=fashion). Broad substrings so the
    // many persona titles (buyer, merchandiser, head/director of sourcing, etc.)
    // all match against the headline (falls back to full card text).
    fashion: [
      "fashion", "apparel", "garment", "textile", "clothing", "buyer", "buying",
      "merchandis", "sourcing", "procurement", "purchasing", "supply chain",
      "head of sourcing", "director sourcing", "fashion brand", "brand owner",
      "retail buying", "retail buyer", "private label", "wholesale",
    ],
  };

  // Returns {ok, hay, reason}. Matches the headline if present, else falls back
  // to the card's full text (so a missed headline selector never silently drops
  // a profile). Always lowercase includes() matching.
  function matchesFilters(headline, cardText, selected, customKw) {
    const kws = [];
    selected.forEach((s) => (FILTER_KEYWORDS[s] || []).forEach((k) => kws.push(k)));
    (customKw || "")
      .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean)
      .forEach((k) => kws.push(k));
    if (!kws.length) return { ok: true, hay: "", reason: "no-filter" };
    const usedHeadline = !!(headline && headline.trim());
    const hay = (usedHeadline ? headline : cardText || "").toLowerCase();
    const hit = kws.find((k) => hay.includes(k));
    return {
      ok: !!hit,
      hay,
      reason: hit
        ? `matched "${hit}" in ${usedHeadline ? "headline" : "card-text"}`
        : `no keyword [${kws.join("|")}] in ${usedHeadline ? "headline" : "card-text"}`,
    };
  }

  // ---- scan the visible profile cards (READ-ONLY). Best-effort selectors across
  //      search-results and connections/people-list layouts. ----
  function scanProfiles() {
    const anchors = [...document.querySelectorAll('a[href*="/in/"]')];
    const seen = new Set();
    const out = [];
    for (const a of anchors) {
      const m = (a.getAttribute("href") || "").match(/\/in\/([^/?#]+)/);
      if (!m) continue;
      const id = m[1];
      if (seen.has(id)) continue;

      const span = a.querySelector('span[aria-hidden="true"]');
      let name = ((span && span.innerText) || a.innerText || "").split("\n")[0].trim();
      // Skip non-name anchors (avatars, "Message"/"Connect"/"Follow" buttons, etc.)
      if (!name || name.length < 2 || /^(message|connect|follow|view|invite)/i.test(name)) continue;
      if (/linkedin member/i.test(name)) continue; // out-of-network, unusable

      const card =
        a.closest("li.reusable-search__result-container") ||
        a.closest("div.entity-result") ||
        a.closest(".mn-connection-card") ||
        a.closest("li") ||
        a.parentElement;
      const txt = (sel) => {
        const n = card && card.querySelector(sel);
        return n ? n.innerText.trim() : "";
      };
      const headline =
        txt(".entity-result__primary-subtitle") ||
        txt(".mn-connection-card__occupation") ||
        txt('[class*="primary-subtitle"]') ||
        txt('[class*="occupation"]');
      const location =
        txt(".entity-result__secondary-subtitle") ||
        txt('[class*="secondary-subtitle"]');

      // Mutual: COUNT only — never names (same lesson as the single-profile fix).
      let mutual = "";
      if (card) {
        const mt = [...card.querySelectorAll("span, a")]
          .map((n) => n.innerText || "")
          .find((t) => /mutual connection/i.test(t));
        if (mt) {
          const mm = mt.match(/(\d[\d,]*)\s+(?:other\s+)?mutual connection/i);
          mutual = mm ? `${mm[1]} mutual connections` : "Has mutual connections";
        }
      }

      // Full card text (collapsed) — used ONLY as a filtering fallback when the
      // headline selector misses; not sent to the brain.
      const cardText = card ? (card.innerText || "").replace(/\s+/g, " ").trim().slice(0, 400) : "";

      seen.add(id);
      out.push({ profile_id: id, name, headline, location, mutual, cardText });
    }
    return out;
  }

  // ---- daily cap tracking (anti-ban guardrail), stored locally ----
  function todayStr() {
    return new Date().toISOString().slice(0, 10);
  }
  async function dailyUsed() {
    const d = await storeGet("lc_daily", { date: todayStr(), count: 0 });
    return d.date === todayStr() ? d.count : 0;
  }
  async function bumpDaily(n) {
    const used = await dailyUsed();
    await storeSet("lc_daily", { date: todayStr(), count: used + n });
  }
  async function markProcessed(ids) {
    const s = new Set(await storeGet("lc_processed", []));
    ids.forEach((i) => s.add(i));
    await storeSet("lc_processed", [...s].slice(-3000)); // cap memory
  }
  async function processedSet() {
    return new Set(await storeGet("lc_processed", []));
  }

  // ---------------- queue UI panel ----------------
  let elQStatus;
  let qCandidates = []; // last scan result (post-filter, post-cap)
  let qBatchId = "";

  function setQStatus(s) {
    if (elQStatus) elQStatus.textContent = s;
  }

  function queuePanel() {
    const wrap = document.createElement("div");
    wrap.style.cssText =
      "position:fixed;right:16px;bottom:16px;width:380px;max-height:88vh;z-index:999999;background:#fff;" +
      "border:1px solid #0a66c2;border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.2);" +
      "font:13px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#111;display:flex;flex-direction:column;overflow:hidden";
    wrap.setAttribute("data-lc-panel", "1");
    wrap.innerHTML = `
      <div style="background:#0a66c2;color:#fff;padding:8px 12px;font-weight:600">
        LinkedIn Copilot · Outreach Queue <span style="font-weight:400;opacity:.8">· inject only</span>
      </div>
      <div style="padding:10px 12px;overflow:auto">
        <div style="margin-bottom:2px">
          <label style="font-weight:600">Limit</label>
          <select id="lc-limit" style="margin-left:6px">
            <option>1</option><option>2</option><option>3</option><option selected>5</option><option>10</option><option>15</option><option>20</option>
          </select>
          <label style="margin-left:10px">custom</label>
          <input id="lc-limit-custom" type="number" min="1" max="50" placeholder="#"
            style="width:48px;margin-left:4px">
          <span style="margin-left:10px">Daily cap</span>
          <select id="lc-daily-cap" style="margin-left:4px">
            <option>10</option><option selected>20</option><option>50</option>
          </select>
        </div>
        <div id="lc-limit-hint" style="font-size:11px;color:#666;margin-bottom:6px"></div>
        <div style="margin-bottom:6px">
          <div style="font-weight:600;margin-bottom:2px">Fashion search presets (opens a LinkedIn people search)</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
            <button class="lc-preset" data-q="fashion founder OR apparel founder OR fashion brand owner"
              style="flex:1;min-width:48%;font-size:11px;padding:4px">👗 Fashion Founders UAE</button>
            <button class="lc-preset" data-q="fashion buyer OR apparel buyer OR garment buyer OR purchasing manager"
              style="flex:1;min-width:48%;font-size:11px;padding:4px">🛍️ Fashion Buyers UAE</button>
            <button class="lc-preset" data-q="head of sourcing OR director sourcing OR sourcing manager OR procurement apparel"
              style="flex:1;min-width:100%;font-size:11px;padding:4px">🧵 Sourcing Decision Makers</button>
          </div>
          <div style="font-weight:600;margin-bottom:2px">Filters (role — headline, falls back to card text)</div>
          <label><input type="checkbox" class="lc-filter" value="fashion" checked>fashion/apparel</label>
          <label style="margin-left:6px"><input type="checkbox" class="lc-filter" value="founder">founder</label>
          <label style="margin-left:6px"><input type="checkbox" class="lc-filter" value="recruiter">recruiter</label>
          <label style="margin-left:6px"><input type="checkbox" class="lc-filter" value="sales">sales</label>
          <input id="lc-filter-custom" placeholder="custom keywords, comma-separated"
            style="width:100%;box-sizing:border-box;margin-top:4px">
        </div>
        <div style="display:flex;gap:6px;margin-bottom:8px">
          <button id="lc-scan" style="flex:1">Scan Profiles</button>
          <button id="lc-gen-queue" style="flex:2;background:#0a66c2;color:#fff;border:none;border-radius:4px;padding:6px;cursor:pointer">Generate Outreach Queue</button>
        </div>
        <div id="lc-q-status" style="color:#0a66c2;min-height:18px;margin-bottom:6px"></div>
        <div id="lc-q-bulk" style="display:none;gap:6px;margin-bottom:6px">
          <button id="lc-approve-all" style="flex:1;background:#057642;color:#fff;border:none;border-radius:4px;padding:5px;cursor:pointer">Approve All</button>
          <button id="lc-reject-all" style="flex:1">Reject All</button>
          <button id="lc-q-refresh" style="flex:1">Refresh</button>
        </div>
        <div id="lc-q-items"></div>
        <div style="font-size:11px;color:#666;margin-top:8px">
          AI drafts only. After approving, open a profile's <b>Connect → Add a note</b> and click
          <b>Inject</b>; <b>you</b> click Connect. This extension never sends, never clicks Connect.
        </div>
      </div>`;
    document.body.appendChild(wrap);
    elQStatus = wrap.querySelector("#lc-q-status");
    const elItems = wrap.querySelector("#lc-q-items");
    const elBulk = wrap.querySelector("#lc-q-bulk");

    function selectedLimit() {
      const custom = parseInt(wrap.querySelector("#lc-limit-custom").value, 10);
      if (custom > 0) return Math.min(custom, 50);
      return parseInt(wrap.querySelector("#lc-limit").value, 10) || 10;
    }
    function selectedFilters() {
      return [...wrap.querySelectorAll(".lc-filter:checked")].map((c) => c.value);
    }

    // ---- Fashion search presets: open LinkedIn's people-search for the preset
    //      query. This is a plain NAVIGATION (location change) on a user click —
    //      NOT a fetch to LinkedIn and NOT an automated send. On the results page
    //      the LIST_PAGE panel re-mounts; we persist a flag so the fashion filter
    //      is pre-checked there. The user still clicks Scan, reviews, and Connects.
    wrap.querySelectorAll(".lc-preset").forEach((btn) => {
      btn.onclick = () => {
        const q = btn.getAttribute("data-q") || "";
        chrome.storage.local.set({ lc_preset_fashion: true }, () => {
          const url = "https://www.linkedin.com/search/results/people/?keywords=" +
            encodeURIComponent(q) + "&origin=GLOBAL_SEARCH_HEADER";
          location.href = url; // user-initiated navigation; not a send
        });
      };
    });
    // If we arrived here via a preset, pre-check the fashion filter once.
    storeGet("lc_preset_fashion", false).then((flag) => {
      if (flag) {
        const fb = wrap.querySelector('.lc-filter[value="fashion"]');
        if (fb) fb.checked = true;
        setQStatus("Fashion preset active — click Scan Profiles to build the queue.");
        chrome.storage.local.set({ lc_preset_fashion: false });
      }
    });

    // ---- mode-aware scan-limit default + low-CPU helper text (from gateway /config) ----
    //      Default limit: 2 in fashion mode, 5 in general. When FAST_MODE is on
    //      (the low-CPU / CPU-inference profile, e.g. HP EliteBook 840 G1), nudge
    //      the user to 1-2 drafts/scan for speed and safer manual review. We never
    //      override a choice the user has already made this session.
    const limitSel = wrap.querySelector("#lc-limit");
    const limitHint = wrap.querySelector("#lc-limit-hint");
    if (limitSel) limitSel.addEventListener("change", () => (limitSel.dataset.userset = "1"));
    (async () => {
      let cfg = {};
      try { cfg = (await call("config", {})) || {}; } catch (_) { /* offline: keep static default */ }
      const fashion = (cfg.mode || "").toLowerCase() === "fashion";
      // Sequential mode (low-CPU, one profile drafted at a time): default the
      // scan to 1 and recommend 1–2. Otherwise the usual mode-aware default.
      const def = cfg.sequential_mode ? "1" : (fashion ? "2" : "5");
      if (limitSel && !limitSel.dataset.userset &&
          [...limitSel.options].some((o) => o.value === def)) {
        limitSel.value = def;
      }
      if (limitHint) {
        if (cfg.sequential_mode) {
          limitHint.textContent =
            "🐢 Sequential mode (low-CPU) — profiles are drafted ONE at a time; keep scans to 1–2 for the fastest review loop.";
          limitHint.style.color = "#b26a00";
        } else if (cfg.fast_mode) {
          limitHint.textContent =
            "⚙️ FAST_MODE (CPU inference) — keep to 1–2 drafts per scan for speed and safer manual review.";
          limitHint.style.color = "#b26a00";
        } else {
          limitHint.textContent =
            `Default ${def} for ${fashion ? "fashion" : "general"} mode · min 1 (lower = safer manual review).`;
          limitHint.style.color = "#666";
        }
      }
    })();

    // ---- Scan: read cards, filter, dedupe vs processed, apply limit + daily cap ----
    //      Emits debug counters + a per-profile reason for every rejection so a
    //      "queuing 0" is always explainable (open DevTools console).
    wrap.querySelector("#lc-scan").onclick = async () => {
      setQStatus("Scanning visible profiles…");
      const filters = selectedFilters();
      const custom = wrap.querySelector("#lc-filter-custom").value;
      const raw = scanProfiles();
      const processed = await processedSet();
      const cap = parseInt(wrap.querySelector("#lc-daily-cap").value, 10) || 20;
      const used = await dailyUsed();
      const remaining = Math.max(0, cap - used);
      const limit = Math.min(selectedLimit(), remaining);

      const counters = { scanned: raw.length, matched_filter: 0, queued: 0, rejected: 0 };
      qCandidates = [];
      console.groupCollapsed(
        `[LC] scan: ${raw.length} profiles · filters=[${filters.join(",") || "none"}]` +
        (custom ? ` custom="${custom}"` : "")
      );
      for (const p of raw) {
        const mf = matchesFilters(p.headline, p.cardText, filters, custom);
        if (!mf.ok) {
          counters.rejected++;
          console.log(`[LC] ✗ ${p.name} — filtered out (${mf.reason}) | headline="${p.headline || "(empty)"}"`);
          continue;
        }
        counters.matched_filter++;
        if (processed.has(p.profile_id)) {
          counters.rejected++;
          console.log(`[LC] ✗ ${p.name} — already processed (dedupe) | ${p.profile_id}`);
          continue;
        }
        if (qCandidates.length >= limit) {
          counters.rejected++;
          console.log(`[LC] ✗ ${p.name} — over limit/daily-cap (limit=${limit}, daily ${used}/${cap})`);
          continue;
        }
        qCandidates.push(p);
        counters.queued++;
        console.log(`[LC] ✓ ${p.name} — queued (${mf.reason}) | headline="${p.headline || "(empty)"}"`);
      }
      console.log("[LC] counters:", counters);
      console.groupEnd();

      setQStatus(
        `scanned ${counters.scanned} · matched_filter ${counters.matched_filter} · ` +
        `queued ${counters.queued} · rejected ${counters.rejected} (daily ${used}/${cap}).` +
        (counters.matched_filter === 0 && filters.length
          ? " No titles matched the filter — see console for per-profile reasons."
          : remaining === 0 ? " Daily cap reached." : "")
      );
    };

    // ---- Generate: send the candidate batch, then render the approval queue ----
    wrap.querySelector("#lc-gen-queue").onclick = async () => {
      if (!qCandidates.length) return setQStatus("Nothing to queue. Click Scan Profiles first.");
      qBatchId = "batch_" + Date.now();
      // Gentle, jittered pacing (anti-ban posture). We only READ the DOM and call
      // our LOCAL gateway here — no LinkedIn traffic — but keep it unhurried.
      setQStatus(`Submitting ${qCandidates.length} profiles…`);
      await sleep(jitter(150));
      const res = await call("batchIngest", {
        batch_id: qBatchId,
        limit: qCandidates.length,
        filters: selectedFilters().join(",") || "(none)",
        // drop the filtering-only cardText; send just the real profile fields
        profiles: qCandidates.map(({ cardText, ...p }) => p),
      });
      const accepted = (res && res.accepted) || 0;
      await markProcessed(qCandidates.map((p) => p.profile_id));
      await bumpDaily(accepted);
      setQStatus(`AI is drafting ${accepted} personalized notes (local Qwen)… this can take a while.`);
      elBulk.style.display = "flex";
      await pollQueue(qBatchId, elItems);
    };

    wrap.querySelector("#lc-q-refresh").onclick = () => pollQueue(qBatchId, elItems);
    wrap.querySelector("#lc-approve-all").onclick = async () => {
      await call("batchApprove", { batch_id: qBatchId, decision: "approve_all" });
      setQStatus("Approved all generated drafts. Each is READY_FOR_INJECTION.");
      await pollQueue(qBatchId, elItems);
    };
    wrap.querySelector("#lc-reject-all").onclick = async () => {
      await call("batchApprove", { batch_id: qBatchId, decision: "reject_all" });
      setQStatus("Rejected all generated drafts.");
      await pollQueue(qBatchId, elItems);
    };
  }

  // ---- poll the batch and (re)render the inbox-style queue ----
  async function pollQueue(batchId, elItems) {
    if (!batchId) return;
    for (let i = 0; i < 120; i++) {
      const b = await call("getBatch", { batch_id: batchId });
      const items = (b && b.items) || [];
      renderQueue(items, batchId, elItems);
      const stillGenerating = items.some(
        (it) => it.state === "OUTREACH_GENERATING" || it.state === "DISCOVERED"
      );
      if (!stillGenerating) return;
      await sleep(2000);
    }
  }

  function renderQueue(items, batchId, elItems) {
    elItems.innerHTML = "";
    // REPETITION ENGINE (queue-wide): count how many drafts share each opener so
    // we can score per-item uniqueness. The brain diversifies openers per profile;
    // this catches any that still collide across the whole queue.
    const openerCount = {};
    for (const it of items) {
      if (!it.outreach_text) continue;
      const k = openerKey(it.outreach_text);
      openerCount[k] = (openerCount[k] || 0) + 1;
    }
    for (const it of items) {
      const row = document.createElement("div");
      row.style.cssText = "border-top:1px solid #eee;padding:8px 0";
      const generating = it.state === "OUTREACH_GENERATING" || it.state === "DISCOVERED";
      const ready = it.state === "OUTREACH_GENERATED";
      const approved = it.state === "READY_FOR_INJECTION" || it.state === "APPROVED";
      const injected = it.state === "INJECTED";
      const done = it.state === "CONNECTED_BY_HUMAN";
      const rejected = it.state === "OUTREACH_REJECTED";

      const head = document.createElement("div");
      head.style.cssText = "display:flex;justify-content:space-between;gap:6px";
      head.innerHTML =
        `<div style="font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(it.name || it.profile_id)}</div>` +
        `<div style="font-size:11px;font-weight:600;color:${stateColor(it.state)};white-space:nowrap">${esc(stateBadge(it.state))}</div>`;
      row.appendChild(head);
      if (it.headline) {
        const hl = document.createElement("div");
        hl.style.cssText = "font-size:11px;color:#666;overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
        hl.textContent = it.headline;
        row.appendChild(hl);
      }
      // Routing transparency: the detected persona/style + confidence. A LOW
      // confidence (amber) means detection fell back to a generic default — worth
      // a glance before approving, since wrong routing = wrong tone.
      if (it.style) {
        const conf = (it.style_confidence || "").toLowerCase();
        const sl = document.createElement("div");
        sl.style.cssText =
          "font-size:11px;margin-top:1px;font-weight:600;color:" + (conf === "low" ? "#b26a00" : "#0a66c2");
        sl.textContent =
          `Detected style: ${it.style}` + (conf ? ` · confidence: ${conf}${conf === "low" ? " ⚠" : ""}` : "");
        row.appendChild(sl);
      }
      // Queue quality preview: which real facts the draft used (grounded_on), a
      // personalization score (did it reference real specifics?), a grounding
      // confidence (no invented claims?), and a repetition score (is its opener
      // unique across the queue?). Each ⚠ flags a draft worth a second look.
      if (it.outreach_text) {
        const gconf = parseFloat(it.grounding);
        const pscore = parseFloat(it.personalization);
        const rep = openerCount[openerKey(it.outreach_text)] || 1;
        const repScore = rep <= 1 ? 1 : 1 / rep; // 100% unique, 50% if 2 share, …
        const lowP = !isNaN(pscore) && pscore < 0.5;
        const lowG = !isNaN(gconf) && gconf < 0.5;
        const lowR = repScore < 1;

        const dbg = document.createElement("div");
        dbg.style.cssText = "font-size:11px;margin-top:2px;color:#5a6b7b";
        dbg.textContent = "grounded on: " + (it.key_phrases || "(no profile specifics)");
        row.appendChild(dbg);

        const scoreLine = document.createElement("div");
        scoreLine.style.cssText =
          "font-size:11px;margin-top:1px;color:" + (lowP || lowR || lowG ? "#b26a00" : "#5a6b7b");
        const parts = [];
        if (!isNaN(pscore)) parts.push(`personalization ${Math.round(pscore * 100)}%${lowP ? " ⚠" : ""}`);
        if (!isNaN(gconf)) parts.push(`grounding ${Math.round(gconf * 100)}%${lowG ? " ⚠" : ""}`);
        parts.push(`repetition ${Math.round(repScore * 100)}%${lowR ? ` ⚠ ×${rep} alike` : ""}`);
        scoreLine.textContent = parts.join(" · ");
        row.appendChild(scoreLine);
      }

      if (generating) {
        const g = document.createElement("div");
        g.style.cssText = "font-size:12px;color:#0a66c2;margin-top:4px";
        g.textContent = "✍️ drafting…";
        row.appendChild(g);
        elItems.appendChild(row);
        continue;
      }
      if (rejected || done) {
        elItems.appendChild(row);
        continue;
      }

      const ta = document.createElement("textarea");
      ta.value = it.outreach_text || "";
      ta.style.cssText = "width:100%;box-sizing:border-box;height:52px;resize:vertical;margin-top:4px";
      ta.disabled = injected;
      row.appendChild(ta);

      const actions = document.createElement("div");
      actions.style.cssText = "display:flex;gap:6px;margin-top:4px";

      if (ready) {
        // Per-item EDIT-before-approve: the textarea above is freely editable and
        // its current value is what Approve sends. This live counter makes the
        // 120–180 target visible while editing.
        const counter = document.createElement("div");
        counter.style.cssText = "font-size:11px;margin-top:2px";
        const updCount = () => {
          const n = ta.value.length;
          const inband = n >= 120 && n <= 180;
          counter.textContent = `${n} chars` + (inband ? " ✓ in target" : " · target 120–180");
          counter.style.color = inband ? "#057642" : n > 200 ? "#b00020" : "#666";
        };
        ta.addEventListener("input", updCount);
        updCount();
        row.appendChild(counter);

        const ap = btn("Approve", "#057642", "#fff");
        ap.onclick = async () => {
          await call("batchApprove", {
            batch_id: batchId, profile_id: it.profile_id,
            decision: "approve", edited_text: ta.value,
          });
          pollQueue(batchId, elItems);
        };
        const rj = btn("Reject");
        rj.onclick = async () => {
          await call("batchApprove", {
            batch_id: batchId, profile_id: it.profile_id, decision: "reject",
          });
          pollQueue(batchId, elItems);
        };
        actions.append(ap, rj);
      } else if (approved) {
        const inj = btn("Inject", "#0a66c2", "#fff");
        inj.onclick = async () => {
          inj.disabled = true;
          inj.textContent = "Injecting…";
          // Pass the row's name so we refuse to fill if the OPEN invite modal
          // belongs to a different person (right note → right person).
          const r = injectIntoConnectionNote(ta.value, it.name);
          if (r.ok) {
            await call("batchInjected", { profile_id: it.profile_id });
            observeBatchConnection(it.profile_id, batchId, elItems);
            setQStatus(`📝 Filled ${it.name}'s connection note. Click Connect yourself.`);
            toast(`Injected ${it.name}'s note — you click Connect`, "ok");
          } else {
            inj.disabled = false;
            inj.textContent = "Inject";
            setQStatus(injectMsg(r));
            toast(injectMsg(r), "err");
          }
        };
        actions.appendChild(inj);
        const hint = document.createElement("div");
        hint.style.cssText = "font-size:11px;color:#666;align-self:center";
        hint.textContent = "open Add-a-note on their profile";
        actions.appendChild(hint);
      } else if (injected) {
        const note = document.createElement("div");
        note.style.cssText = "font-size:11px;color:#666";
        note.textContent = "Injected — click LinkedIn Connect yourself.";
        actions.appendChild(note);
      }
      row.appendChild(actions);
      elItems.appendChild(row);
    }
  }

  function observeBatchConnection(pid, batchId, elItems) {
    let fired = false;
    const obs = new MutationObserver((muts) => {
      if (fired) return;
      for (const m of muts) {
        for (const node of m.addedNodes || []) {
          const t = node.innerText || node.textContent || "";
          if (/invitation sent|pending|withdraw/i.test(t)) {
            fired = true;
            obs.disconnect();
            call("batchObserved", { profile_id: pid });
            pollQueue(batchId, elItems);
            return;
          }
        }
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => obs.disconnect(), 300000);
  }

  // small DOM helpers for the queue
  function btn(label, bg, fg) {
    const b = document.createElement("button");
    b.textContent = label;
    b.style.cssText = "flex:1;cursor:pointer" +
      (bg ? `;background:${bg};color:${fg};border:none;border-radius:4px;padding:5px` : "");
    return b;
  }
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  // Normalized opener (greeting + name stripped, first 6 words) — the key the
  // repetition engine groups on to detect drafts that open the same way.
  function openerKey(text) {
    const body = String(text || "").replace(/^(hi|hello|hey)\b[^,]*,/i, "").trim().toLowerCase();
    return body.split(/\s+/).slice(0, 6).join(" ");
  }
  function stateColor(s) {
    if (s === "CONNECTED_BY_HUMAN") return "#057642";
    if (s === "OUTREACH_REJECTED") return "#b00";
    if (s === "READY_FOR_INJECTION" || s === "APPROVED" || s === "INJECTED") return "#0a66c2";
    if (s === "OUTREACH_GENERATED") return "#b26a00"; // amber: needs your review
    return "#888";
  }
  // Friendly, emoji-prefixed badge for each queue state (clear status at a glance).
  function stateBadge(s) {
    const map = {
      DISCOVERED: "⏳ queued",
      OUTREACH_GENERATING: "✍️ drafting…",
      OUTREACH_GENERATED: "📝 review",
      APPROVED: "✅ approved",
      READY_FOR_INJECTION: "✅ approved",
      INJECTED: "📥 injected",
      CONNECTED_BY_HUMAN: "🤝 connected",
      OUTREACH_REJECTED: "✕ rejected",
    };
    return map[s] || s;
  }

  // =====================================================================
  //  MOUNT — route by page type. LinkedIn is an SPA, so re-check on nav.
  // =====================================================================
  let mounted = "";
  function mount() {
    const type = pageType();
    if (type === mounted) return;
    // Remove any previously-mounted panel before switching modes.
    document.querySelectorAll("[data-lc-panel]").forEach((n) => n.remove());
    mounted = type;
    if (type === "MESSAGING") panel();
    else if (type === "PROFILE_PAGE") outreachPanel();
    else if (type === "LIST_PAGE") queuePanel();
  }

  function start() {
    mount();
    // SPA navigation watcher: re-mount when the path changes.
    let last = location.pathname;
    setInterval(() => {
      if (location.pathname !== last) {
        last = location.pathname;
        mounted = ""; // force re-evaluation
        mount();
      }
    }, 1500);
  }

  if (document.body) start();
  else window.addEventListener("DOMContentLoaded", start);
})();
