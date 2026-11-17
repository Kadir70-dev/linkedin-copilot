# K70 AI Copilot — General-purpose floating popup (beta)

One Chrome/Brave Manifest V3 extension (`services/extension`). It has **no site-specific code**: no LinkedIn, WhatsApp or Instagram adapters. Use it on any web page.

## How it works
1. Click the **K70 AI Copilot** toolbar icon on any `http(s)` page. The popup is injected into that tab only (`activeTab`). Click the icon again to close it.
2. **Reply Suggestions:** paste a message (or select text on the page and press **Use selected text**), pick a tone, press **Generate Reply**.
3. **Write a Message:** type what you want to say in your own words (English, Hinglish, Hindi, Urdu, ...). Instructions are treated as your request and as facts you provide, not as an incoming message.
4. Edit the draft, then **Copy reply**. Paste it wherever you need and send it yourself.

## Boundaries
- Nothing is read from a page unless you press **Use selected text** (only your selection) or tick "Include the text I have selected".
- The extension never types into a page, never clicks Send, never submits forms, never dispatches key or mouse events, never uses `execCommand`.
- No always-on content scripts and no host permissions other than the gateway (`http://localhost:8080`). Permissions: `storage`, `activeTab`, `scripting`.
- The text you generate from goes only to your own gateway. Drafts are purged by the gateway after `RETENTION_DAYS` (default 30).
- Copy goes through the gateway approval step (`approve_with_edit`), so the state machine records what you actually used.

## Install for testing
Open `chrome://extensions` (or `brave://extensions`), enable Developer mode, **Load unpacked**, choose `services/extension`, open **Options** and paste the gateway access key. Start the gateway + AI brain (see README). Or build the ZIP: `python3 scripts/package_k70.py` (`dist/k70-ai-copilot-<version>.zip`).

## Tests
- `tests/extension-e2e/e2e.mjs` loads the REAL unpacked extension in Chromium against a generic page and a mock gateway: manifest least-privilege, nothing before the icon click, selection capture, tone, Hinglish compose, copy via approval, discard, gateway down + recovery, drag/minimize/close, no typing into the page, non-web pages skipped.
- CI: `.github/workflows/k70-popup-checks.yml` (static checks, forbidden-API guard, E2E, AI tests, Go tests).
- Limitation: Playwright cannot click the browser toolbar, so the test calls the same open function and grants the fake test origin in a test-only copy of the manifest. The real `activeTab` grant on an icon click must be checked by hand once.

## Manual QA
1. On a normal site, click the icon: popup opens; click again: it closes.
2. On `chrome://` or the Web Store: nothing happens (browsers block extensions there).
3. Select text, **Use selected text**, Generate, edit, **Copy reply**, paste into any message box. Confirm the page was not changed by the extension and Send was not pressed.
4. Write mode with a Hinglish instruction; confirm the draft keeps your stated facts and invents none.
5. Stop the gateway: a clear error appears, no draft. Start it: Generate works again.
