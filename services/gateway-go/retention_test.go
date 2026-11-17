package main

import (
	"database/sql"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

func TestPurgeOlderThan(t *testing.T) {
	d, err := sql.Open("sqlite", "file:retention_test?mode=memory&cache=shared")
	if err != nil {
		t.Fatal(err)
	}
	defer d.Close()
	d.SetMaxOpenConns(1)
	for _, q := range []string{
		`CREATE TABLE threads(thread_id TEXT PRIMARY KEY, text TEXT, updated_at TEXT)`,
		`CREATE TABLE drafts(thread_id TEXT PRIMARY KEY, draft_text TEXT, state TEXT, updated_at TEXT)`,
		`CREATE TABLE outreach(profile_id TEXT PRIMARY KEY, profile_text TEXT, updated_at TEXT)`,
	} {
		if _, err := d.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	now := time.Now().UTC()
	old := now.Add(-40 * 24 * time.Hour).Format(time.RFC3339)
	fresh := now.Add(-2 * 24 * time.Hour).Format(time.RFC3339)
	d.Exec(`INSERT INTO threads VALUES('old','private chat',?),('new','recent chat',?)`, old, fresh)
	d.Exec(`INSERT INTO drafts VALUES('old','d','READY',?),('new','d','READY',?)`, old, fresh)
	d.Exec(`INSERT INTO outreach VALUES('o1','profile',?),('o2','profile',?)`, old, fresh)
	n, err := purgeOlderThan(d, now.Add(-30*24*time.Hour))
	if err != nil || n != 3 {
		t.Fatalf("removed %d rows, err=%v; want 3", n, err)
	}
	var left int
	d.QueryRow(`SELECT COUNT(*) FROM threads WHERE thread_id='new'`).Scan(&left)
	if left != 1 {
		t.Fatal("fresh thread must be kept")
	}
	d.QueryRow(`SELECT COUNT(*) FROM threads`).Scan(&left)
	if left != 1 {
		t.Fatalf("old thread must be gone, %d rows left", left)
	}
}
