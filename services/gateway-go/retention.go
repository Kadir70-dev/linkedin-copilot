package main

import (
	"context"
	"database/sql"
	"fmt"
	"log"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

// Data minimisation: chat text the user sent for drafting is deleted after RETENTION_DAYS
// (default 30; 0 disables). Covers the SQLite tables and the Redis streams that carried the text.

// purgeOlderThan deletes rows whose updated_at (RFC3339, UTC) is older than the cutoff.
func purgeOlderThan(d *sql.DB, cutoff time.Time) (int64, error) {
	c := cutoff.UTC().Format(time.RFC3339)
	var total int64
	for _, t := range []string{"threads", "drafts", "outreach"} {
		res, err := d.Exec(fmt.Sprintf("DELETE FROM %s WHERE updated_at IS NOT NULL AND updated_at < ?", t), c)
		if err != nil {
			return total, err
		}
		n, _ := res.RowsAffected()
		total += n
	}
	return total, nil
}

// trimStreams removes stream entries older than the cutoff (stream IDs start with a ms timestamp).
func trimStreams(ctx context.Context, r *redis.Client, cutoff time.Time, streams ...string) {
	min := strconv.FormatInt(cutoff.UnixMilli(), 10)
	for _, s := range streams {
		if err := r.XTrimMinID(ctx, s, min).Err(); err != nil {
			log.Printf("retention: trim %s: %v", s, err)
		}
	}
}

// startRetention purges once at start-up and then hourly. days<=0 disables it.
func startRetention(ctx context.Context, d *sql.DB, r *redis.Client, days int, streams ...string) {
	if days <= 0 {
		log.Printf("retention: disabled")
		return
	}
	run := func() {
		cutoff := time.Now().Add(-time.Duration(days) * 24 * time.Hour)
		n, err := purgeOlderThan(d, cutoff)
		if err != nil {
			log.Printf("retention: purge failed: %v", err)
			return
		}
		trimStreams(ctx, r, cutoff, streams...)
		if n > 0 {
			log.Printf("retention: removed %d rows older than %d days", n, days)
		}
	}
	go func() {
		run()
		for range time.Tick(time.Hour) {
			run()
		}
	}()
}
