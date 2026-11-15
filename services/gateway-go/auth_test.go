package main

import (
	"crypto/sha256"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestRequireAuth(t *testing.T) {
	tok := "0123456789abcdef0123456789abcdef"
	h := requireAuth([][32]byte{sha256.Sum256([]byte(tok))}, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	cases := []struct {
		method, path, auth string
		want               int
	}{
		{"GET", "/health", "", 200},
		{"OPTIONS", "/ingest", "", 200},
		{"GET", "/stats", "", 401},
		{"POST", "/ingest", "Bearer wrong-token", 401},
		{"POST", "/ingest", tok, 401},
		{"POST", "/ingest", "Bearer " + tok, 200},
		{"GET", "/config", "Bearer " + tok, 200},
	}
	for _, c := range cases {
		req := httptest.NewRequest(c.method, c.path, nil)
		if c.auth != "" {
			req.Header.Set("Authorization", c.auth)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != c.want {
			t.Errorf("%s %s auth=%q: got %d want %d", c.method, c.path, c.auth, rec.Code, c.want)
		}
	}
}
