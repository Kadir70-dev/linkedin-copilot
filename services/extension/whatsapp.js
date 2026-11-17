/* WhatsApp Web adapter. Reads ONLY the chat that is currently open (#main) and only when the user
 * presses Capture. Never opens, scrolls or scans other chats. Missing selectors fail safe (empty result). */
(function () {
  "use strict";
  if (typeof K70Adapter === "undefined") return;
  const composer = () =>
    document.querySelector('#main footer div[contenteditable="true"][role="textbox"]') ||
    document.querySelector('#main footer [contenteditable="true"]');
  function capture() {
    const main = document.querySelector("#main");
    if (!main) return "";
    const lines = [];
    main.querySelectorAll("[data-pre-plain-text]").forEach((el) => {
      const m = (el.getAttribute("data-pre-plain-text") || "").match(/\]\s*(.*?):\s*$/);
      const parts = [...el.querySelectorAll("span.selectable-text")].map((n) => n.innerText.trim()).filter(Boolean);
      const text = parts.join(" ").trim();
      if (text) lines.push((m && m[1] ? m[1] + ": " : "") + text);
    });
    return lines.slice(-12).join("\n");
  }
  K70Adapter.run({ id: "whatsapp", label: "WhatsApp", pathOk: () => true, capture, composer });
})();
