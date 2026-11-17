// Service worker: performs the cross-origin fetches to the configured gateway only.
// Toolbar click injects the popup into the active tab (activeTab; no broad site access). No send capability.
// GATEWAY is set at package time by scripts/configure_extension.py and must match
// manifest host_permissions. The access key lives in chrome.storage.local.
function openPopup(tab) {
  if (!tab || !tab.id || !/^https?:/.test(tab.url || "")) return Promise.resolve();
  chrome.scripting.insertCSS({ target: { tabId: tab.id }, files: ["premium_panel.css"] })
    .then(() => chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["premium_panel.js"] }))
    .catch(() => {});
}
chrome.action.onClicked.addListener(openPopup);

const GATEWAY = "http://localhost:8080";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handle(msg).then(sendResponse).catch((e) => sendResponse({ error: String(e) }));
  return true; // async
});

async function handle({ action, payload }) {
  if (action === "gatewayInfo") {
    return { gateway: GATEWAY };
  }
  if (action === "config") {
    return j(await gw(`${GATEWAY}/config`));
  }
  if (action === "ingest") {
    return j(await gw(`${GATEWAY}/ingest`, post(payload)));
  }
  if (action === "getDraft") {
    return j(await gw(`${GATEWAY}/draft/${encodeURIComponent(payload.thread_id)}`));
  }
  if (action === "approve") {
    return j(await gw(`${GATEWAY}/approve`, post(payload)));
  }
  return { error: "unknown action" };
}

const post = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

async function gw(url, init = {}) {
  const { gatewayToken } = await chrome.storage.local.get("gatewayToken");
  const headers = { ...(init.headers || {}) };
  if (gatewayToken) headers.Authorization = `Bearer ${gatewayToken}`;
  return fetch(url, { ...init, headers });
}

const j = async (r) => {
  if (r.status === 401) return { error: "unauthorized" };
  if (r.status === 429) return { error: "rate limited — try again in a minute" };
  return r.json();
};
