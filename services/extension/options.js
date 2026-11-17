// Settings page: stores the gateway access key in chrome.storage.local and
// tests it against the configured gateway. No LinkedIn access, no send capability.
const $ = (id) => document.getElementById(id);
const show = (msg) => { $("status").textContent = msg; };

chrome.runtime.sendMessage({ action: "gatewayInfo" }, (info) => {
  $("gateway").textContent = (info && info.gateway) || "?";
});
chrome.storage.local.get("gatewayToken", ({ gatewayToken }) => {
  $("token").value = gatewayToken || "";
});
$("save").addEventListener("click", () => {
  const gatewayToken = $("token").value.trim();
  chrome.storage.local.set({ gatewayToken }, () => {
    show("Saved. Testing connection…");
    chrome.runtime.sendMessage({ action: "config" }, (res) => {
      if (res && !res.error) show("Connected ✓");
      else if (res && res.error === "unauthorized") show("Access key rejected — check it with your administrator.");
      else show("Cannot reach the gateway: " + ((res && res.error) || "no response"));
    });
  });
});

// Per-platform opt-in for the WhatsApp Web and Instagram popups (default OFF).
chrome.storage.local.get("k70Platforms", ({ k70Platforms }) => {
  const f = k70Platforms || {};
  ["whatsapp", "instagram"].forEach((id) => {
    const box = $("p-" + id);
    box.checked = !!f[id];
    box.addEventListener("change", () => {
      chrome.storage.local.get("k70Platforms", ({ k70Platforms: cur }) => {
        chrome.storage.local.set({ k70Platforms: Object.assign({}, cur || {}, { [id]: box.checked }) });
      });
    });
  });
});
