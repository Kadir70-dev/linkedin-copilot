package main

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"log"
	"net/http"
	"strings"
)

// Caller authentication. Every route except GET /health and CORS preflights
// requires "Authorization: Bearer <token>", where <token> is one of the
// comma-separated GATEWAY_API_TOKENS. The gateway refuses to start without a
// token unless ALLOW_NO_AUTH=true (local laptop use only, port bound to loopback).
const minTokenLen = 32

func loadAPITokens() [][32]byte {
	var hashes [][32]byte
	for _, t := range strings.Split(env("GATEWAY_API_TOKENS", ""), ",") {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		if len(t) < minTokenLen {
			log.Fatalf("GATEWAY_API_TOKENS: every token must be at least %d characters", minTokenLen)
		}
		hashes = append(hashes, sha256.Sum256([]byte(t)))
	}
	if len(hashes) == 0 && !strings.EqualFold(env("ALLOW_NO_AUTH", "false"), "true") {
		log.Fatal("no GATEWAY_API_TOKENS configured; set one (openssl rand -hex 32) or ALLOW_NO_AUTH=true for local-only use")
	}
	if len(hashes) == 0 {
		log.Print("WARNING: ALLOW_NO_AUTH=true — gateway accepts unauthenticated requests; keep it on localhost")
	}
	return hashes
}

func tokenAllowed(hashes [][32]byte, header string) bool {
	tok, ok := strings.CutPrefix(header, "Bearer ")
	if !ok || tok == "" {
		return false
	}
	got := sha256.Sum256([]byte(strings.TrimSpace(tok)))
	match := 0
	for _, h := range hashes {
		match |= subtle.ConstantTimeCompare(got[:], h[:])
	}
	return match == 1
}

// Owner keys: the subset of access keys (OWNER_API_TOKENS, each also listed in
// GATEWAY_API_TOKENS) that belong to the account owner. Requests made with one
// are tagged owner=1 so the brain may use the owner's personal Claude plan.
type ctxKey string

const ownerCtx ctxKey = "owner"

var ownerHashes [][32]byte

func loadOwnerTokens(all [][32]byte) {
	for _, t := range strings.Split(env("OWNER_API_TOKENS", ""), ",") {
		if t = strings.TrimSpace(t); t == "" {
			continue
		}
		h := sha256.Sum256([]byte(t))
		if !tokenAllowed(all, "Bearer "+t) {
			log.Fatal("OWNER_API_TOKENS: every owner key must also be in GATEWAY_API_TOKENS")
		}
		ownerHashes = append(ownerHashes, h)
	}
}

// ownerFlag returns "1" for requests authenticated with an owner key.
func ownerFlag(r *http.Request) string {
	if v, _ := r.Context().Value(ownerCtx).(bool); v {
		return "1"
	}
	return ""
}

func requireAuth(hashes [][32]byte, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if len(hashes) == 0 || r.Method == http.MethodOptions || (r.Method == http.MethodGet && r.URL.Path == "/health") {
			next.ServeHTTP(w, r)
			return
		}
		if !tokenAllowed(hashes, r.Header.Get("Authorization")) {
			w.Header().Set("WWW-Authenticate", `Bearer realm="linkedin-copilot"`)
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		if len(ownerHashes) > 0 && tokenAllowed(ownerHashes, r.Header.Get("Authorization")) {
			r = r.WithContext(context.WithValue(r.Context(), ownerCtx, true))
		}
		next.ServeHTTP(w, r)
	})
}
