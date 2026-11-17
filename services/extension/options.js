// Settings page: stores the gateway access key in chrome.storage.local and
// tests it against the configured gateway. No send capability.
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

