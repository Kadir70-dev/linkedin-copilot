// Service worker: performs the cross-origin fetches to the LOCAL gateway only.
// It talks to nothing on linkedin.com. It has no send capability.
const GATEWAY = "http://localhost:8080";

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handle(msg).then(sendResponse).catch((e) => sendResponse({ error: String(e) }));
  return true; // async
});

async function handle({ action, payload }) {
  if (action === "config") {
    return j(await fetch(`${GATEWAY}/config`));
  }
  if (action === "ingest") {
    return j(await fetch(`${GATEWAY}/ingest`, post(payload)));
  }
  if (action === "getDraft") {
    return j(await fetch(`${GATEWAY}/draft/${encodeURIComponent(payload.thread_id)}`));
  }
  if (action === "approve") {
    return j(await fetch(`${GATEWAY}/approve`, post(payload)));
  }
  if (action === "injected") {
    return j(await fetch(`${GATEWAY}/injected`, post(payload)));
  }
  if (action === "observed") {
    return j(await fetch(`${GATEWAY}/observed`, post(payload)));
  }
  // ---- Phase 1: AI Outreach (cold connection requests, same gateway) ----
  if (action === "profileIngest") {
    return j(await fetch(`${GATEWAY}/profile-ingest`, post(payload)));
  }
  if (action === "getOutreach") {
    return j(await fetch(`${GATEWAY}/outreach/${encodeURIComponent(payload.profile_id)}`));
  }
  if (action === "approveOutreach") {
    return j(await fetch(`${GATEWAY}/approve-outreach`, post(payload)));
  }
  if (action === "connectionInjected") {
    return j(await fetch(`${GATEWAY}/connection-injected`, post(payload)));
  }
  if (action === "connectionObserved") {
    return j(await fetch(`${GATEWAY}/connection-observed`, post(payload)));
  }
  // ---- Phase 2: Batch Outreach Queue (scan -> bulk draft -> approval queue) ----
  if (action === "batchIngest") {
    return j(await fetch(`${GATEWAY}/batch-ingest`, post(payload)));
  }
  if (action === "getBatch") {
    return j(await fetch(`${GATEWAY}/batch/${encodeURIComponent(payload.batch_id)}`));
  }
  if (action === "batchApprove") {
    return j(await fetch(`${GATEWAY}/batch-approve`, post(payload)));
  }
  if (action === "batchInjected") {
    return j(await fetch(`${GATEWAY}/batch-injected`, post(payload)));
  }
  if (action === "batchObserved") {
    return j(await fetch(`${GATEWAY}/batch-observed`, post(payload)));
  }
  return { error: "unknown action" };
}

const post = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});
const j = (r) => r.json();
