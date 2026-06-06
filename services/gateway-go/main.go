// Gateway = single system authority + state machine (MVP).
// It owns SQLite state, publishes message.received, consumes draft.generated,
// and gates everything behind a MANDATORY human approval. It links NO LinkedIn
// client and has NO send path: the only outbound-adjacent action is returning
// draft text for the extension to INJECT. The human is the only sender.
package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	_ "modernc.org/sqlite"
)

const (
	streamIngest = "stream:message_ingest"
	streamResult = "stream:cognition_result"
	groupGateway = "cg:gateway"
)

// DraftState spine (subset, faithful to safety/frozen/state-transitions.json).
const (
	stReceived    = "RECEIVED"
	stDrafting    = "DRAFTING"
	stPending     = "PENDING_APPROVAL"
	stApproved    = "APPROVED"
	stReadyInject = "READY_FOR_INJECTION"
	stInjected    = "INJECTED"
	stSent        = "SENT_BY_HUMAN"
	stRejected    = "REJECTED"
)

// Phase 1: AI Outreach spine. Mirrors the reply spine but for cold/new-thread
// connection requests. Same mandatory approval gate — the human is still the
// only sender, and injection targets the connection NOTE field only (no Connect
// click anywhere). See CLAUDE.md "Architecture Freeze".
const (
	stProfileCaptured  = "PROFILE_CAPTURED"
	stOutreachGen      = "OUTREACH_GENERATING"
	stOutreachReady    = "OUTREACH_READY"
	stOutreachApproved = "OUTREACH_APPROVED"
	stReadyConnInject  = "READY_FOR_CONNECTION_INJECTION"
	stConnInjected     = "CONNECTION_INJECTED"
	stConnSent         = "CONNECTION_SENT_BY_HUMAN"
	stOutreachRejected = "OUTREACH_REJECTED"
)

// Phase 2: Batch Outreach Queue spine. A batch item walks this richer status
// line. Same mandatory approval gate and inject-only boundary as everything
// else — the AI NEVER sends; the human clicks Connect. Batch items live in the
// SAME `outreach` table (tagged by batch_id); single-profile rows keep the
// Phase 1 vocabulary above. The two vocabularies coexist as plain strings.
const (
	stDiscovered        = "DISCOVERED"
	stOutreachGenerated = "OUTREACH_GENERATED"
	stBatchApproved     = "APPROVED"
	stReadyForInjection = "READY_FOR_INJECTION"
	stBatchInjected     = "INJECTED"
	stConnectedByHuman  = "CONNECTED_BY_HUMAN"
	stFollowupPending   = "FOLLOWUP_PENDING"
)

var (
	db  *sql.DB
	rdb *redis.Client
	ctx = context.Background()
)

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	// --- SQLite ---
	var err error
	db, err = sql.Open("sqlite", "file:"+env("DB_PATH", "./app.db")+"?_pragma=busy_timeout(5000)")
	must(err)
	db.SetMaxOpenConns(1) // MVP: serialize writes, avoid SQLITE_BUSY
	must(db.Ping())
	mustExec(`CREATE TABLE IF NOT EXISTS threads(
		thread_id TEXT PRIMARY KEY, text TEXT, updated_at TEXT)`)
	mustExec(`CREATE TABLE IF NOT EXISTS drafts(
		thread_id TEXT PRIMARY KEY, draft_text TEXT, state TEXT, updated_at TEXT)`)
	// Phase 1: AI Outreach. profile_text holds the captured profile context;
	// outreach_text holds the AI-drafted connection note; style is the
	// auto-detected outreach style (networking/recruiter/founder/...).
	mustExec(`CREATE TABLE IF NOT EXISTS outreach(
		profile_id TEXT PRIMARY KEY, profile_text TEXT, outreach_text TEXT,
		style TEXT, state TEXT, updated_at TEXT)`)
	// Phase 2: Batch Outreach Queue. The `outreach` table is reused; these
	// columns are added defensively (the DB lives on a persistent volume, so
	// CREATE TABLE IF NOT EXISTS won't add them to an existing table).
	addColumn("outreach", "batch_id", "TEXT")
	addColumn("outreach", "name", "TEXT")
	addColumn("outreach", "headline", "TEXT")
	addColumn("outreach", "company", "TEXT")
	addColumn("outreach", "location", "TEXT")
	addColumn("outreach", "mutual", "TEXT")
	// Debug-preview grounding signal from the ai-brain (key phrases actually drawn
	// from the profile + a 0..1 grounding confidence). Display-only; never gates.
	addColumn("outreach", "key_phrases", "TEXT")
	addColumn("outreach", "grounding", "TEXT")
	addColumn("outreach", "personalization", "TEXT")
	// Routing transparency: the detected-style confidence (high/medium/low) from
	// the ai-brain. Display-only; shown in the approval UI, never gates approval.
	addColumn("outreach", "style_confidence", "TEXT")
	// Hermes structured understanding (single-person path) as a JSON blob: role,
	// company, niche, geo, seniority, keywords, angle, tone, forbidden, grounded_facts.
	// Display-only; the brain already used it to build the grounded prompt.
	addColumn("outreach", "hermes_json", "TEXT")
	// Batch metadata (limit + filters) for tracking/auditing a scan run.
	mustExec(`CREATE TABLE IF NOT EXISTS batches(
		batch_id TEXT PRIMARY KEY, limit_n INTEGER, filters TEXT,
		count INTEGER, created_at TEXT)`)

	// --- Redis (retry until up) ---
	rdb = redis.NewClient(&redis.Options{Addr: env("REDIS_ADDR", "localhost:6379")})
	for i := 0; i < 30; i++ {
		if err = rdb.Ping(ctx).Err(); err == nil {
			break
		}
		log.Printf("waiting for redis... (%v)", err)
		time.Sleep(time.Second)
	}
	must(err)
	// Consumer group on the result stream (ignore BUSYGROUP).
	rdb.XGroupCreateMkStream(ctx, streamResult, groupGateway, "0")

	go consumeDraftResults()

	// --- HTTP ---
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, map[string]any{"ok": true}) })
	// Read-only config the extension uses to pick mode-aware UI defaults (e.g. the
	// scan-limit default and low-CPU helper text). No state, no send capability.
	mux.HandleFunc("GET /config", func(w http.ResponseWriter, r *http.Request) {
		fast := strings.EqualFold(env("FAST_MODE", "false"), "true")
		// SEQUENTIAL_MODE defaults on, and FAST_MODE forces it (CPU-inference proxy)
		// — mirror the ai-brain so the extension can cap the recommended scan limit.
		sequential := strings.EqualFold(env("SEQUENTIAL_MODE", "true"), "true") || fast
		writeJSON(w, 200, map[string]any{
			"mode":            strings.ToLower(env("MODE", "general")),
			"fast_mode":       fast,
			"sequential_mode": sequential,
		})
	})
	// Read-only observability for `make doctor` and the extension: model strategy
	// (base/premium/routing) + live queue counts by state. No state change, no send.
	mux.HandleFunc("GET /stats", handleStats)
	mux.HandleFunc("POST /ingest", handleIngest)
	mux.HandleFunc("GET /draft/{thread_id}", handleGetDraft)
	mux.HandleFunc("POST /approve", handleApprove)
	mux.HandleFunc("POST /injected", handleInjected)
	mux.HandleFunc("POST /observed", handleObserved)
	// Phase 1: AI Outreach endpoints (cold connection requests, same gate).
	mux.HandleFunc("POST /profile-ingest", handleProfileIngest)
	mux.HandleFunc("GET /outreach/{profile_id}", handleGetOutreach)
	mux.HandleFunc("POST /approve-outreach", handleApproveOutreach)
	mux.HandleFunc("POST /connection-injected", handleConnInjected)
	mux.HandleFunc("POST /connection-observed", handleConnObserved)
	// Phase 2: Batch Outreach Queue endpoints (scan -> bulk draft -> approval queue).
	mux.HandleFunc("POST /batch-ingest", handleBatchIngest)
	mux.HandleFunc("GET /batch/{batch_id}", handleGetBatch)
	mux.HandleFunc("POST /batch-approve", handleBatchApprove)
	mux.HandleFunc("POST /batch-injected", handleBatchInjected)
	mux.HandleFunc("POST /batch-observed", handleBatchObserved)

	port := env("GATEWAY_PORT", "8080")
	log.Printf("gateway listening on :%s", port)
	must(http.ListenAndServe(":"+port, cors(mux)))
}

// ---------- handlers ----------

// GET /stats -> read-only health/observability for `make doctor` and the UI:
// the active model strategy (from env, kept in sync with ai-brain) plus live
// outreach + draft queue counts by state. It mints no send capability.
func handleStats(w http.ResponseWriter, r *http.Request) {
	fast := strings.EqualFold(env("FAST_MODE", "false"), "true")
	writeJSON(w, 200, map[string]any{
		"mode":            strings.ToLower(env("MODE", "general")),
		"fast_mode":       fast,
		"sequential_mode": strings.EqualFold(env("SEQUENTIAL_MODE", "true"), "true") || fast,
		"base_model":      env("MODEL", "qwen2.5:3b"),
		"premium_model":   env("PREMIUM_MODEL", "qwen3:4b-instruct-2507-q4_K_M"),
		"premium_routing": strings.EqualFold(env("PREMIUM_ROUTING", "true"), "true"),
		"outreach":        countByState("outreach"),
		"drafts":          countByState("drafts"),
	})
}

// countByState returns {state: count} for a table. `table` is an internal
// constant (never user input), so the string build is safe.
func countByState(table string) map[string]int {
	out := map[string]int{}
	rows, err := db.Query("SELECT state, COUNT(*) FROM " + table + " GROUP BY state")
	if err != nil {
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var s sql.NullString
		var n int
		if rows.Scan(&s, &n) == nil && s.Valid {
			out[s.String] = n
		}
	}
	return out
}

// POST /ingest {thread_id, text} -> persist + publish message.received
func handleIngest(w http.ResponseWriter, r *http.Request) {
	var in struct {
		ThreadID string `json:"thread_id"`
		Text     string `json:"text"`
	}
	if json.NewDecoder(r.Body).Decode(&in) != nil || in.ThreadID == "" {
		writeJSON(w, 400, map[string]any{"error": "thread_id and text required"})
		return
	}
	now := time.Now().UTC().Format(time.RFC3339)
	mustExec(`INSERT INTO threads(thread_id,text,updated_at) VALUES(?,?,?)
		ON CONFLICT(thread_id) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at`,
		in.ThreadID, in.Text, now)
	// RECEIVED -> DRAFTING (one draft per thread in the MVP)
	mustExec(`INSERT INTO drafts(thread_id,draft_text,state,updated_at) VALUES(?,?,?,?)
		ON CONFLICT(thread_id) DO UPDATE SET draft_text='', state=?, updated_at=?`,
		in.ThreadID, "", stDrafting, now, stDrafting, now)

	// publish message.received
	must(rdb.XAdd(ctx, &redis.XAddArgs{Stream: streamIngest, Values: map[string]any{
		"type":      "message.received",
		"thread_id": in.ThreadID,
		"text":      in.Text,
	}}).Err())
	log.Printf("ingested thread=%s -> DRAFTING, published message.received", in.ThreadID)
	writeJSON(w, 200, map[string]any{"ok": true, "thread_id": in.ThreadID, "state": stDrafting})
}

// GET /draft/{thread_id} -> current state + draft (extension polls this)
func handleGetDraft(w http.ResponseWriter, r *http.Request) {
	tid := r.PathValue("thread_id")
	text, state, ok := loadDraft(tid)
	if !ok {
		writeJSON(w, 404, map[string]any{"error": "unknown thread"})
		return
	}
	writeJSON(w, 200, map[string]any{"thread_id": tid, "state": state, "draft_text": text})
}

// POST /approve {thread_id, decision, edited_text?}
// The ONLY path past PENDING_APPROVAL. Returns inject_text for the extension to
// inject. It returns NO send instruction — send does not exist here.
func handleApprove(w http.ResponseWriter, r *http.Request) {
	var in struct {
		ThreadID   string `json:"thread_id"`
		Decision   string `json:"decision"` // approve | approve_with_edit | reject
		EditedText string `json:"edited_text"`
	}
	if json.NewDecoder(r.Body).Decode(&in) != nil || in.ThreadID == "" {
		writeJSON(w, 400, map[string]any{"error": "thread_id required"})
		return
	}
	text, state, ok := loadDraft(in.ThreadID)
	if !ok {
		writeJSON(w, 404, map[string]any{"error": "unknown thread"})
		return
	}
	if state != stPending {
		// Mandatory-approval guard: cannot advance unless awaiting a decision.
		writeJSON(w, 409, map[string]any{"error": "not awaiting approval", "state": state})
		return
	}
	if in.Decision == "reject" {
		setState(in.ThreadID, text, stRejected)
		writeJSON(w, 200, map[string]any{"state": stRejected})
		return
	}
	// approve / approve_with_edit
	if in.EditedText != "" {
		text = in.EditedText
	}
	// PENDING_APPROVAL -> APPROVED -> READY_FOR_INJECTION (token mint/consume
	// is stubbed for the MVP; see safety/ frozen specs for the real flow).
	setState(in.ThreadID, text, stApproved)
	setState(in.ThreadID, text, stReadyInject)
	log.Printf("approved thread=%s -> READY_FOR_INJECTION (inject only, no send)", in.ThreadID)
	writeJSON(w, 200, map[string]any{"state": stReadyInject, "inject_text": text})
}

// POST /injected {thread_id} -> READY_FOR_INJECTION -> INJECTED
func handleInjected(w http.ResponseWriter, r *http.Request) {
	tid := body(r)["thread_id"]
	text, state, ok := loadDraft(tid)
	if !ok || state != stReadyInject {
		writeJSON(w, 409, map[string]any{"error": "not ready for injection", "state": state})
		return
	}
	setState(tid, text, stInjected)
	writeJSON(w, 200, map[string]any{"state": stInjected})
}

// POST /observed {thread_id} -> INJECTED -> SENT_BY_HUMAN (observation only).
func handleObserved(w http.ResponseWriter, r *http.Request) {
	tid := body(r)["thread_id"]
	text, state, ok := loadDraft(tid)
	if !ok {
		writeJSON(w, 404, map[string]any{"error": "unknown thread"})
		return
	}
	if state != stInjected {
		// Per INV-021: SENT_BY_HUMAN only from INJECTED. Otherwise just record nothing.
		writeJSON(w, 200, map[string]any{"state": state, "note": "observed but not from INJECTED"})
		return
	}
	setState(tid, text, stSent)
	log.Printf("observed human send thread=%s -> SENT_BY_HUMAN", tid)
	writeJSON(w, 200, map[string]any{"state": stSent})
}

// ---------- Phase 1: AI Outreach handlers ----------

// POST /profile-ingest {profile_id, profile_text, headline?, about?, context?}
// Persists the captured profile and kicks off outreach generation. This is the
// outreach analogue of /ingest. It mints no send capability — it only requests
// a DRAFT (data) from the AI brain.
func handleProfileIngest(w http.ResponseWriter, r *http.Request) {
	var in struct {
		ProfileID   string `json:"profile_id"`
		ProfileText string `json:"profile_text"`
		Name        string `json:"name"`
		Headline    string `json:"headline"`
		About       string `json:"about"`
		Context     string `json:"context"`
	}
	if json.NewDecoder(r.Body).Decode(&in) != nil || in.ProfileID == "" {
		writeJSON(w, 400, map[string]any{"error": "profile_id and profile_text required"})
		return
	}
	// Fold the structured fields into one profile_text blob the brain can read.
	blob := in.ProfileText
	if in.Headline != "" {
		blob = "Headline: " + in.Headline + "\n" + blob
	}
	if in.About != "" {
		blob = blob + "\nAbout: " + in.About
	}
	if in.Context != "" {
		blob = blob + "\nContext: " + in.Context
	}
	now := time.Now().UTC().Format(time.RFC3339)
	// PROFILE_CAPTURED -> OUTREACH_GENERATING (one outreach per profile in MVP).
	mustExec(`INSERT INTO outreach(profile_id,profile_text,outreach_text,style,state,updated_at)
		VALUES(?,?,?,?,?,?)
		ON CONFLICT(profile_id) DO UPDATE SET profile_text=excluded.profile_text,
		outreach_text='', style='', state=?, updated_at=?`,
		in.ProfileID, blob, "", "", stOutreachGen, now, stOutreachGen, now)

	// publish outreach.requested (reuses the ingest stream; type disambiguates).
	// target_name is carried EXPLICITLY so the brain greets the right person.
	must(rdb.XAdd(ctx, &redis.XAddArgs{Stream: streamIngest, Values: map[string]any{
		"type":         "outreach.requested",
		"profile_id":   in.ProfileID,
		"profile_text": blob,
		"target_name":  in.Name,
		// single=1 -> the brain runs the HERMES per-person pipeline (structured
		// profile spec + grounded prompt). Batch-ingest does NOT set this.
		"single": "1",
	}}).Err())
	log.Printf("profile-ingest profile=%s -> OUTREACH_GENERATING, published outreach.requested", in.ProfileID)
	writeJSON(w, 200, map[string]any{"ok": true, "profile_id": in.ProfileID, "state": stOutreachGen})
}

// GET /outreach/{profile_id} -> current state + drafted note + detected style.
func handleGetOutreach(w http.ResponseWriter, r *http.Request) {
	pid := r.PathValue("profile_id")
	text, style, state, ok := loadOutreach(pid)
	if !ok {
		writeJSON(w, 404, map[string]any{"error": "unknown profile"})
		return
	}
	var keyPhrases, grounding, personalization, styleConf, hermesJSON sql.NullString
	_ = db.QueryRow(`SELECT key_phrases, grounding, personalization, style_confidence, hermes_json FROM outreach WHERE profile_id=?`, pid).
		Scan(&keyPhrases, &grounding, &personalization, &styleConf, &hermesJSON)
	writeJSON(w, 200, map[string]any{
		"profile_id": pid, "state": state, "outreach_text": text, "style": style,
		"style_confidence": styleConf.String,
		"hermes":           hermesJSON.String,
		"key_phrases": keyPhrases.String, "grounding": grounding.String,
		"personalization": personalization.String,
	})
}

// POST /approve-outreach {profile_id, decision, edited_text?}
// The ONLY path past OUTREACH_READY. Returns inject_text for the connection NOTE
// field. Returns NO send instruction — send does not exist here.
func handleApproveOutreach(w http.ResponseWriter, r *http.Request) {
	var in struct {
		ProfileID  string `json:"profile_id"`
		Decision   string `json:"decision"` // approve | approve_with_edit | reject
		EditedText string `json:"edited_text"`
	}
	if json.NewDecoder(r.Body).Decode(&in) != nil || in.ProfileID == "" {
		writeJSON(w, 400, map[string]any{"error": "profile_id required"})
		return
	}
	text, style, state, ok := loadOutreach(in.ProfileID)
	if !ok {
		writeJSON(w, 404, map[string]any{"error": "unknown profile"})
		return
	}
	if state != stOutreachReady {
		writeJSON(w, 409, map[string]any{"error": "not awaiting approval", "state": state})
		return
	}
	if in.Decision == "reject" {
		setOutreach(in.ProfileID, text, style, stOutreachRejected)
		writeJSON(w, 200, map[string]any{"state": stOutreachRejected})
		return
	}
	if in.EditedText != "" {
		text = in.EditedText
	}
	// OUTREACH_READY -> OUTREACH_APPROVED -> READY_FOR_CONNECTION_INJECTION.
	setOutreach(in.ProfileID, text, style, stOutreachApproved)
	setOutreach(in.ProfileID, text, style, stReadyConnInject)
	log.Printf("approve-outreach profile=%s -> READY_FOR_CONNECTION_INJECTION (note inject only, no send)", in.ProfileID)
	writeJSON(w, 200, map[string]any{"state": stReadyConnInject, "inject_text": text})
}

// POST /connection-injected {profile_id} -> READY_FOR_CONNECTION_INJECTION -> CONNECTION_INJECTED.
func handleConnInjected(w http.ResponseWriter, r *http.Request) {
	pid := body(r)["profile_id"]
	text, style, state, ok := loadOutreach(pid)
	if !ok || state != stReadyConnInject {
		writeJSON(w, 409, map[string]any{"error": "not ready for connection injection", "state": state})
		return
	}
	setOutreach(pid, text, style, stConnInjected)
	// publish connection.inject.requested (audit trail; not a send)
	_ = rdb.XAdd(ctx, &redis.XAddArgs{Stream: streamResult, Values: map[string]any{
		"type": "connection.inject.requested", "profile_id": pid,
	}}).Err()
	writeJSON(w, 200, map[string]any{"state": stConnInjected})
}

// POST /connection-observed {profile_id} -> CONNECTION_INJECTED -> CONNECTION_SENT_BY_HUMAN.
func handleConnObserved(w http.ResponseWriter, r *http.Request) {
	pid := body(r)["profile_id"]
	text, style, state, ok := loadOutreach(pid)
	if !ok {
		writeJSON(w, 404, map[string]any{"error": "unknown profile"})
		return
	}
	if state != stConnInjected {
		writeJSON(w, 200, map[string]any{"state": state, "note": "observed but not from CONNECTION_INJECTED"})
		return
	}
	setOutreach(pid, text, style, stConnSent)
	_ = rdb.XAdd(ctx, &redis.XAddArgs{Stream: streamResult, Values: map[string]any{
		"type": "human.connection.sent.observed", "profile_id": pid,
	}}).Err()
	log.Printf("observed human connection profile=%s -> CONNECTION_SENT_BY_HUMAN", pid)
	writeJSON(w, 200, map[string]any{"state": stConnSent})
}

// ---------- Phase 2: Batch Outreach Queue handlers ----------

type batchProfile struct {
	ProfileID string `json:"profile_id"`
	Name      string `json:"name"`
	Headline  string `json:"headline"`
	About     string `json:"about"`
	Company   string `json:"company"`
	Location  string `json:"location"`
	Mutual    string `json:"mutual"`
}

// POST /batch-ingest {batch_id, limit, filters, profiles:[...]}
// Bulk analogue of /profile-ingest. Persists each scanned profile (DISCOVERED ->
// PROFILE_CAPTURED) and requests a DRAFT for each (data, never an action). It
// mints NO send capability. The extension is responsible for limit/daily-cap
// enforcement and de-dupe; the gateway records the batch and backstops the count.
func handleBatchIngest(w http.ResponseWriter, r *http.Request) {
	var in struct {
		BatchID  string         `json:"batch_id"`
		Limit    int            `json:"limit"`
		Filters  string         `json:"filters"`
		Profiles []batchProfile `json:"profiles"`
	}
	if json.NewDecoder(r.Body).Decode(&in) != nil || in.BatchID == "" || len(in.Profiles) == 0 {
		writeJSON(w, 400, map[string]any{"error": "batch_id and non-empty profiles required"})
		return
	}
	// Server-side backstop on batch size (anti-ban guardrail; client also caps).
	const hardCap = 50
	profiles := in.Profiles
	if len(profiles) > hardCap {
		profiles = profiles[:hardCap]
	}
	now := time.Now().UTC().Format(time.RFC3339)
	mustExec(`INSERT INTO batches(batch_id,limit_n,filters,count,created_at) VALUES(?,?,?,?,?)
		ON CONFLICT(batch_id) DO UPDATE SET limit_n=excluded.limit_n, filters=excluded.filters,
		count=excluded.count, created_at=excluded.created_at`,
		in.BatchID, in.Limit, in.Filters, len(profiles), now)

	accepted := 0
	for _, p := range profiles {
		if p.ProfileID == "" {
			continue
		}
		blob := buildProfileBlob(p)
		// DISCOVERED -> PROFILE_CAPTURED -> OUTREACH_GENERATING (we already hold
		// the captured context, so we go straight to requesting a draft).
		mustExec(`INSERT INTO outreach(profile_id,profile_text,outreach_text,style,state,updated_at,
			batch_id,name,headline,company,location,mutual)
			VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
			ON CONFLICT(profile_id) DO UPDATE SET profile_text=excluded.profile_text,
			outreach_text='', style='', state=excluded.state, updated_at=excluded.updated_at,
			batch_id=excluded.batch_id, name=excluded.name, headline=excluded.headline,
			company=excluded.company, location=excluded.location, mutual=excluded.mutual`,
			p.ProfileID, blob, "", "", stOutreachGen, now,
			in.BatchID, p.Name, p.Headline, p.Company, p.Location, p.Mutual)

		must(rdb.XAdd(ctx, &redis.XAddArgs{Stream: streamIngest, Values: map[string]any{
			"type":         "outreach.requested",
			"profile_id":   p.ProfileID,
			"profile_text": blob,
			"target_name":  p.Name,
		}}).Err())
		accepted++
	}
	log.Printf("batch-ingest batch=%s accepted=%d -> OUTREACH_GENERATING (bulk drafts requested)", in.BatchID, accepted)
	writeJSON(w, 200, map[string]any{"ok": true, "batch_id": in.BatchID, "accepted": accepted})
}

// GET /batch/{batch_id} -> the whole approval queue: every item + state + draft.
func handleGetBatch(w http.ResponseWriter, r *http.Request) {
	bid := r.PathValue("batch_id")
	rows, err := db.Query(`SELECT profile_id,name,headline,state,outreach_text,style,key_phrases,grounding,personalization,style_confidence
		FROM outreach WHERE batch_id=? ORDER BY updated_at`, bid)
	if err != nil {
		writeJSON(w, 500, map[string]any{"error": err.Error()})
		return
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var pid, name, headline, state, text, style, keyPhrases, grounding, personalization, styleConf sql.NullString
		_ = rows.Scan(&pid, &name, &headline, &state, &text, &style, &keyPhrases, &grounding, &personalization, &styleConf)
		items = append(items, map[string]any{
			"profile_id":       pid.String,
			"name":             name.String,
			"headline":         headline.String,
			"state":            state.String,
			"outreach_text":    text.String,
			"style":            style.String,
			"style_confidence": styleConf.String,
			"key_phrases":      keyPhrases.String,
			"grounding":        grounding.String,
			"personalization":  personalization.String,
		})
	}
	writeJSON(w, 200, map[string]any{"batch_id": bid, "count": len(items), "items": items})
}

// POST /batch-approve {batch_id, profile_id?, decision, edited_text?}
// decision: approve | reject | approve_all | reject_all.
// The ONLY path past OUTREACH_GENERATED. Approve -> READY_FOR_INJECTION and
// returns inject_text per item. Returns NO send instruction — send does not exist.
func handleBatchApprove(w http.ResponseWriter, r *http.Request) {
	var in struct {
		BatchID    string `json:"batch_id"`
		ProfileID  string `json:"profile_id"`
		Decision   string `json:"decision"`
		EditedText string `json:"edited_text"`
	}
	if json.NewDecoder(r.Body).Decode(&in) != nil {
		writeJSON(w, 400, map[string]any{"error": "bad request"})
		return
	}
	approve := in.Decision == "approve" || in.Decision == "approve_all"
	all := in.Decision == "approve_all" || in.Decision == "reject_all"

	// Build the worklist: one profile, or every still-pending item in the batch.
	var ids []string
	if all {
		if in.BatchID == "" {
			writeJSON(w, 400, map[string]any{"error": "batch_id required for *_all"})
			return
		}
		rows, _ := db.Query(`SELECT profile_id FROM outreach WHERE batch_id=? AND state=?`,
			in.BatchID, stOutreachGenerated)
		for rows.Next() {
			var pid string
			_ = rows.Scan(&pid)
			ids = append(ids, pid)
		}
		rows.Close()
	} else {
		if in.ProfileID == "" {
			writeJSON(w, 400, map[string]any{"error": "profile_id required"})
			return
		}
		ids = []string{in.ProfileID}
	}

	results := []map[string]any{}
	for _, pid := range ids {
		text, style, state, ok := loadOutreach(pid)
		if !ok || state != stOutreachGenerated {
			results = append(results, map[string]any{"profile_id": pid, "state": state, "skipped": true})
			continue
		}
		if !approve {
			setOutreach(pid, text, style, stOutreachRejected)
			results = append(results, map[string]any{"profile_id": pid, "state": stOutreachRejected})
			continue
		}
		if in.EditedText != "" && !all {
			text = in.EditedText
		}
		// OUTREACH_GENERATED -> APPROVED -> READY_FOR_INJECTION.
		setOutreach(pid, text, style, stBatchApproved)
		setOutreach(pid, text, style, stReadyForInjection)
		results = append(results, map[string]any{
			"profile_id": pid, "state": stReadyForInjection, "inject_text": text,
		})
	}
	log.Printf("batch-approve batch=%s decision=%s items=%d", in.BatchID, in.Decision, len(results))
	writeJSON(w, 200, map[string]any{"decision": in.Decision, "results": results})
}

// POST /batch-injected {profile_id} -> READY_FOR_INJECTION -> INJECTED.
func handleBatchInjected(w http.ResponseWriter, r *http.Request) {
	pid := body(r)["profile_id"]
	text, style, state, ok := loadOutreach(pid)
	if !ok || state != stReadyForInjection {
		writeJSON(w, 409, map[string]any{"error": "not ready for injection", "state": state})
		return
	}
	setOutreach(pid, text, style, stBatchInjected)
	_ = rdb.XAdd(ctx, &redis.XAddArgs{Stream: streamResult, Values: map[string]any{
		"type": "connection.inject.requested", "profile_id": pid,
	}}).Err()
	writeJSON(w, 200, map[string]any{"state": stBatchInjected})
}

// POST /batch-observed {profile_id} -> INJECTED -> CONNECTED_BY_HUMAN.
func handleBatchObserved(w http.ResponseWriter, r *http.Request) {
	pid := body(r)["profile_id"]
	text, style, state, ok := loadOutreach(pid)
	if !ok {
		writeJSON(w, 404, map[string]any{"error": "unknown profile"})
		return
	}
	if state != stBatchInjected {
		writeJSON(w, 200, map[string]any{"state": state, "note": "observed but not from INJECTED"})
		return
	}
	setOutreach(pid, text, style, stConnectedByHuman)
	_ = rdb.XAdd(ctx, &redis.XAddArgs{Stream: streamResult, Values: map[string]any{
		"type": "human.connection.sent.observed", "profile_id": pid,
	}}).Err()
	log.Printf("batch-observed human connection profile=%s -> CONNECTED_BY_HUMAN", pid)
	writeJSON(w, 200, map[string]any{"state": stConnectedByHuman})
}

// Fold a scanned profile's structured fields into one blob the brain can read.
func buildProfileBlob(p batchProfile) string {
	blob := "Name: " + p.Name
	if p.Headline != "" {
		blob += "\nHeadline: " + p.Headline
	}
	if p.Company != "" {
		blob += "\nCompany: " + p.Company
	}
	if p.Location != "" {
		blob += "\nLocation: " + p.Location
	}
	if p.About != "" {
		blob += "\nAbout: " + p.About
	}
	if p.Mutual != "" {
		blob += "\nMutual: " + p.Mutual
	}
	return blob
}

// ---------- redis consumer ----------

// Consumes draft.generated from the AI brain: DRAFTING -> PENDING_APPROVAL.
func consumeDraftResults() {
	for {
		res, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group:    groupGateway,
			Consumer: "gw-1",
			Streams:  []string{streamResult, ">"},
			Count:    10,
			Block:    5 * time.Second,
		}).Result()
		if err != nil {
			if err != redis.Nil {
				time.Sleep(time.Second)
			}
			continue
		}
		for _, st := range res {
			for _, msg := range st.Messages {
				switch msg.Values["type"] {
				case "draft.generated":
					tid, _ := msg.Values["thread_id"].(string)
					draft, _ := msg.Values["draft_text"].(string)
					if _, state, ok := loadDraft(tid); ok && state == stDrafting {
						setState(tid, draft, stPending)
						log.Printf("draft.generated thread=%s -> PENDING_APPROVAL", tid)
					}
				case "outreach.generated":
					pid, _ := msg.Values["profile_id"].(string)
					note, _ := msg.Values["outreach_text"].(string)
					style, _ := msg.Values["style"].(string)
					keyPhrases, _ := msg.Values["key_phrases"].(string)
					grounding, _ := msg.Values["grounding"].(string)
					personalization, _ := msg.Values["personalization"].(string)
					styleConf, _ := msg.Values["style_confidence"].(string)
					setOutreachMeta(pid, keyPhrases, grounding, personalization, styleConf)
					// Hermes structured understanding (single-person path only).
					if hermes, _ := msg.Values["hermes"].(string); hermes != "" {
						mustExec(`UPDATE outreach SET hermes_json=? WHERE profile_id=?`, hermes, pid)
					}
					if _, _, state, ok := loadOutreach(pid); ok && state == stOutreachGen {
						// Batch items (tagged with a batch_id) use the Phase 2
						// vocabulary; single-profile rows use the Phase 1 one.
						if outreachBatchID(pid) != "" {
							setOutreach(pid, note, style, stOutreachGenerated)
							log.Printf("outreach.generated profile=%s style=%s -> OUTREACH_GENERATED (batch)", pid, style)
						} else {
							setOutreach(pid, note, style, stOutreachReady)
							log.Printf("outreach.generated profile=%s style=%s -> OUTREACH_READY", pid, style)
						}
					}
				}
				rdb.XAck(ctx, streamResult, groupGateway, msg.ID)
			}
		}
	}
}

// ---------- db helpers ----------

func loadDraft(tid string) (text, state string, ok bool) {
	row := db.QueryRow(`SELECT draft_text, state FROM drafts WHERE thread_id=?`, tid)
	if err := row.Scan(&text, &state); err != nil {
		return "", "", false
	}
	return text, state, true
}

func setState(tid, text, state string) {
	mustExec(`UPDATE drafts SET draft_text=?, state=?, updated_at=? WHERE thread_id=?`,
		text, state, time.Now().UTC().Format(time.RFC3339), tid)
}

func loadOutreach(pid string) (text, style, state string, ok bool) {
	row := db.QueryRow(`SELECT outreach_text, style, state FROM outreach WHERE profile_id=?`, pid)
	if err := row.Scan(&text, &style, &state); err != nil {
		return "", "", "", false
	}
	return text, style, state, true
}

func setOutreach(pid, text, style, state string) {
	mustExec(`UPDATE outreach SET outreach_text=?, style=?, state=?, updated_at=? WHERE profile_id=?`,
		text, style, state, time.Now().UTC().Format(time.RFC3339), pid)
}

// setOutreachMeta records the display-only quality signal (key phrases used +
// grounding + personalization + detected-style confidence). It never changes
// state and never gates approval.
func setOutreachMeta(pid, keyPhrases, grounding, personalization, styleConfidence string) {
	mustExec(`UPDATE outreach SET key_phrases=?, grounding=?, personalization=?, style_confidence=? WHERE profile_id=?`,
		keyPhrases, grounding, personalization, styleConfidence, pid)
}

// outreachBatchID returns the batch_id a profile belongs to ("" if single-profile).
func outreachBatchID(pid string) string {
	var b sql.NullString
	_ = db.QueryRow(`SELECT batch_id FROM outreach WHERE profile_id=?`, pid).Scan(&b)
	return b.String
}

// addColumn adds a column if it does not exist. table/col/typ are internal
// constants (never user input), so the string build is safe. Duplicate-column
// errors from re-runs are intentionally ignored.
func addColumn(table, col, typ string) {
	_, _ = db.Exec("ALTER TABLE " + table + " ADD COLUMN " + col + " " + typ)
}

// ---------- tiny utils ----------

func body(r *http.Request) map[string]string {
	m := map[string]string{}
	_ = json.NewDecoder(r.Body).Decode(&m)
	return m
}

func cors(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
		if r.Method == http.MethodOptions {
			w.WriteHeader(204)
			return
		}
		h.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func mustExec(q string, args ...any) {
	if _, err := db.Exec(q, args...); err != nil {
		log.Fatalf("db exec: %v\nquery: %s", err, q)
	}
}
func must(err error) {
	if err != nil {
		log.Fatal(err)
	}
}
