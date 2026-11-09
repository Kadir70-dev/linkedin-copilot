# LinkedIn Personal Copilot — Chrome Web Store release readiness

Status: PREPARATION ONLY; NOT READY TO PUBLISH (9 November 2026).

## Branch feat/launch-ready — automated prep completed (9 November 2026)

| Item | Status |
|---|---|
| Go gateway builds cleanly (`go build`, `go vet`) | DONE |
| Go tests pass (`go test ./... -v -count=1`, no test files = `[no test files]`) | DONE |
| worker.py FAST_MODE/SEQUENTIAL order bug fixed (NameError on startup) | DONE |
| VP/founder premium-model boundary fixed (VP removed from GENERAL_PREMIUM_KW) | DONE |
| Company extraction fixed (`hermes_role_company`: only split on 'at'/'@', not 'of') | DONE |
| Classification tests (bench/test_classification.py): 32/32 PASS | DONE |
| Forbidden-API grep: NO `.click()`, NO `form.submit()`, NO `keydown/Enter` in content.js | DONE |
| Extension offline error handling: gateway unreachable shows toast+status, no silent failure | DONE |
| Icons created: icons/icon-16.png, icons/icon-48.png, icons/icon-128.png | DONE |
| CWS ZIP: artifacts/linkedin-copilot-v0.1.0.zip (MV3, 6 files, 19 KB) | DONE |
| ZIP validated: manifest version=0.1.0, manifest_version=3, all icons present | DONE |
| .gitignore: .env, *.pyc, gateway binary, artifacts all excluded | DONE |
| No secrets/API keys in source files | DONE |

## Requires owner action before publish

1. **Chrome Web Store developer account** — one-time $5 registration fee at https://chrome.google.com/webstore/devconsole
2. **Privacy policy URL** — public URL required for CWS submission; draft at docs/CHROME_WEB_STORE_PRIVACY_DRAFT.md
3. **Real screenshots** — 1280×800 screenshots of a running extension on a real LinkedIn page (1-5 required)
4. **128×128 promotional icon** — the auto-generated icon-128.png is a placeholder; a polished branded icon is recommended
5. **Live LinkedIn DOM testing** — the DOM selectors in content.js (captureProfile, inviteModal, connectionNoteField, scanProfiles) must be verified against the current live LinkedIn DOM on desktop Chrome; LinkedIn obfuscates class names frequently
6. **Ollama + Docker deployment** — the local gateway + Ollama must be running on the user's machine for the extension to function; not required for CWS submission but required for it to work
7. **Legal/business identity** — developer name and support contact email for the CWS listing page

## Critical source-of-truth discrepancy

- GitHub main still holds the 6 June 2026 legacy source at cb6c638.
- User's actual local ~/linkedin-copilot was refactored afterward (Phase-0, 10 June).
- GitHub: Redis Streams + SQLite; local known Phase-0: Go/Asynq + PostgreSQL/pgvector + FastAPI brain.
- GitHub: MV3 manifest v0.1.0 with batch/list mode; local newer extension v0.2.0 has two modes: messaging + profile.
- DO NOT merge the legacy extension over the newer local source or publish this legacy preflight ZIP.

## Prepared on this branch

- Removed accidentally tracked .env, compiled gateway binary and Python bytecode from current branch tip.
- Added .gitignore for local configuration, databases, logs and release artifacts.
- Minimized extension host permissions, leaving localhost gateway, and made reproducible icon generation.
- Added JS syntax / MV3 / no-auto-send / package safety CI that attaches a test ZIP, not a published product.
- Added Chrome Web Store listing and privacy-disclosure drafts.

## Must verify against the newer original local source before publish

1. Safely sync reviewed Phase-0 source to a new Git branch, preserving its Go/Asynq/Postgres/FastAPI/Ollama architecture. Never force-push stale GitHub code onto the laptop.
2. Run actual Docker/Go/FastAPI/Ollama/Postgres/Asynq stack and Chrome MV3 integration tests; do not call static checks live testing.
3. Fix the July audit defects: wrong company extraction for 'Head of Sourcing at X', founder/VP classification, weak grounding word-prefix matching and fallback quality.
4. Ensure user intentionally opts in BEFORE profile or message content is transmitted to the local gateway. Confirm actual retention, delete/export and any other processors.
5. Test LinkedIn profile and messaging DOM, SPA navigation, insertion, approval and no Send/Connect activation on desktop Chrome.
6. Keep optional Playwright/Voyager worker and autonomous sending disabled; human must always click Send/Connect.
7. Final privacy policy URL, verified support contact, legal/business identity and 2026 developer disclosures required.
8. Real screenshots (1280x800), 128x128 icon and promotional image, developer account, Chrome Web Store submission and reviewer issues.

## Safe source handoff

On the original laptop, inspect git status, git remote -v, and git log -1, then review a sanitized archive of services/extension, services/gateway-go, services/ai-brain, Docker Compose, docs and tests.
Exclude .env, credentials, databases, binary executables, browser session cookies, cache, model files and volumes.
Alternatively, push reviewed source to a separate sync/phase0-local branch; never force-push or overwrite main.

Security note: removing files from a branch does not remove them from Git history. Rotate any real secrets if they ever entered a published commit.

Reference: docs/CHROME_WEB_STORE_PRIVACY_DRAFT.md and docs/CHROME_WEB_STORE_LISTING_DRAFT.md.