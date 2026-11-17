/* Instagram Web DM adapter. Active only on /direct/ pages. Reads ONLY the open conversation, only when
 * the user presses Capture. Never browses the inbox or other threads. Missing selectors fail safe. */
(function () {
  "use strict";
  if (typeof K70Adapter === "undefined") return;
  const root = () => document.querySelector("main") || document.querySelector('[role="main"]') || document;
  const composer = () =>
    root().querySelector('div[role="textbox"][contenteditable="true"]') ||
    root().querySelector('[contenteditable="true"][aria-label*="essage"]');
  function capture() {
    const rows = [...root().querySelectorAll('div[role="row"]')].slice(-12);
    return rows.map((r) => (r.innerText || "").trim().replace(/\s*\n+\s*/g, " ")).filter(Boolean).join("\n");
  }
  K70Adapter.run({ id: "instagram", label: "Instagram", pathOk: (p) => p.indexOf("/direct/") === 0, capture, composer });
})();
