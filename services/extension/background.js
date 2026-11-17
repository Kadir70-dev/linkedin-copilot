// Service worker: performs the cross-origin fetches to the configured gateway only.
// It talks to nothing on linkedin.com. It has no send capability.
// GATEWAY is set at package time by scripts/configure_extension.py and must match
// manifest host_permissions. The access key lives in chrome.storage.local.
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
  if (action === "injected") {
    return j(await gw(`${GATEWAY}/injected`, post(payload)));
  }
  if (action === "observed") {
    return j(await gw(`${GATEWAY}/observed`, post(payload)));
  }
  // ---- Phase 1: AI Outreach (cold connection requests, same gateway) ----
  if (action === "profileIngest") {
    return j(await gw(`${GATEWAY}/profile-ingest`, post(payload)));
  }
  if (action === "getOutreach") {
    return j(await gw(`${GATEWAY}/outreach/${encodeURIComponent(payload.profile_id)}`));
  }
  if (action === "approveOutreach") {
    return j(await gw(`${GATEWAY}/approve-outreach`, post(payload)));
  }
  if (action === "connectionInjected") {
    return j(await gw(`${GATEWAY}/connection-injected`, post(payload)));
  }
  if (action === "connectionObserved") {
    return j(await gw(`${GATEWAY}/connection-observed`, post(payload)));
  }
  // ---- Phase 2: Batch Outreach Queue (scan -> bulk draft -> approval queue) ----
  if (action === "batchIngest") {
    return j(await gw(`${GATEWAY}/batch-ingest`, post(payload)));
  }
  if (action === "getBatch") {
    return j(await gw(`${GATEWAY}/batch/${encodeURIComponent(payload.batch_id)}`));
  }
  if (action === "batchApprove") {
    return j(await gw(`${GATEWAY}/batch-approve`, post(payload)));
  }
  if (action === "batchInjected") {
    return j(await gw(`${GATEWAY}/batch-injected`, post(payload)));
  }
  if (action === "batchObserved") {
    return j(await gw(`${GATEWAY}/batch-observed`, post(payload)));
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
