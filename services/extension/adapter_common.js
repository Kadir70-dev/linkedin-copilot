/* Shared helpers for the WhatsApp Web and Instagram adapters.
 *
 * Boundary (same as LinkedIn): OBSERVE + INSERT ONLY. These adapters never click Send, never submit,
 * never dispatch keyboard/mouse events, never open or scan other chats. A chat is read only when the
 * user presses "Capture current chat" in the popup, and only the chat that is open at that moment.
 * The popup is shown only after the user switched the platform ON in the extension Options.
 */
(function () {
  "use strict";
  const call = (action, payload) => new Promise((res) => chrome.runtime.sendMessage({ action, payload }, res));

  function toast(msg, kind) {
    const colors = { ok: "#057642", err: "#b00020", info: "#3b2a6b" };
    document.querySelectorAll("[data-k70-toast]").forEach((n) => n.remove());
    const t = document.createElement("div");
    t.setAttribute("data-k70-toast", "1");
    t.textContent = msg;
    t.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483600;max-width:440px;" +
      "padding:10px 16px;border-radius:10px;color:#fff;text-align:center;font:13px/1.4 system-ui,sans-serif;" +
      "box-shadow:0 6px 24px rgba(0,0,0,.3);background:" + (colors[kind] || colors.info);
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3400);
  }

  const readFlags = () => chrome.storage.local.get("k70Platforms").then((r) => r.k70Platforms || {});

  // Put text into the composer at the caret. Only the composer's text changes; nothing is submitted.
  function insertIntoComposer(box, text) {
    if (!box || !text) return false;
    box.focus();
    let ok = false;
    try { ok = document.execCommand("insertText", false, text); } catch (_) { ok = false; }
    const probe = text.slice(0, 20);
    return !!ok && (box.innerText || box.textContent || "").indexOf(probe) >= 0;
  }

  // Mount the popup when (a) the user enabled this platform, (b) we are on a chat page.
  function run(cfg) {
    let enabled = false;
    const bridge = {
      platform: { id: cfg.id, label: cfg.label },
      call,
      threadId: () => cfg.id,
      captureThread: () => { try { return cfg.capture() || ""; } catch (_) { return ""; } },
      injectIntoComposer: (text) => { try { return insertIntoComposer(cfg.composer(), text); } catch (_) { return false; } },
      observeHumanSend: () => {},
      toast,
    };
    const tick = () => {
      const want = enabled && cfg.pathOk(location.pathname);
      const present = document.getElementById("k70-copilot");
      if (want && !present && typeof globalThis.K70PremiumPanel === "function") globalThis.K70PremiumPanel(bridge);
      if (!want && present) present.remove();
    };
    readFlags().then((f) => { enabled = !!f[cfg.id]; tick(); });
    chrome.storage.onChanged.addListener((ch, area) => {
      if (area === "local" && ch.k70Platforms) { enabled = !!(ch.k70Platforms.newValue || {})[cfg.id]; tick(); }
    });
    setInterval(tick, 1500); // SPA route changes
  }

  globalThis.K70Adapter = { call, toast, insertIntoComposer, run };
})();
