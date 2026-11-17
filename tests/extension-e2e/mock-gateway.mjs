// Minimal stand-in for the Go gateway (same endpoints/state names) so the extension can be tested end to end.
import http from "node:http";
export function startMockGateway(port = 8080) {
  const log = [], drafts = new Map(); let failing = false;
  const server = http.createServer((req, res) => {
    let body = ""; req.on("data", (c) => (body += c));
    req.on("end", () => {
      const json = (code, o) => { res.writeHead(code, { "content-type": "application/json", "access-control-allow-origin": "*" }); res.end(JSON.stringify(o)); };
      const b = body ? JSON.parse(body) : {}; const u = new URL(req.url, "http://x");
      log.push({ method: req.method, path: u.pathname, body: b });
      if (u.pathname === "/__fail") { failing = !!b.on; return json(200, { ok: true }); }
      if (failing) return json(503, { error: "gateway down" });
      if (u.pathname === "/config") return json(200, { ok: true });
      if (u.pathname === "/ingest") {
        const mode = b.mode || "reply";
        drafts.set(b.thread_id, `DRAFT[${mode}/${b.tone || "-"}] ${mode === "compose" ? b.instructions : "Thanks for the message, happy to help."}`);
        return json(200, { ok: true, thread_id: b.thread_id, state: "DRAFTING" });
      }
      if (u.pathname.startsWith("/draft/")) {
        const id = decodeURIComponent(u.pathname.slice(7));
        return drafts.has(id) ? json(200, { thread_id: id, state: "PENDING_APPROVAL", draft_text: drafts.get(id) }) : json(404, { error: "unknown thread" });
      }
      if (u.pathname === "/approve") {
        return b.decision === "reject" ? json(200, { state: "REJECTED" }) : json(200, { state: "READY_FOR_INJECTION", inject_text: b.edited_text });
      }
      json(404, { error: "not found" });
    });
  });
  return new Promise((r) => server.listen(port, "127.0.0.1", () => r({ log, close: () => server.close() })));
}
