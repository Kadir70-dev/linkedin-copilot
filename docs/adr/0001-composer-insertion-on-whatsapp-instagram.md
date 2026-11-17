# ADR 0001 — Composer text insertion on WhatsApp Web and Instagram

**Status:** PROPOSED. Needs the owner's explicit approval (security sign-off) before this PR is merged.

## Context
The frozen boundary (`safety/frozen/extension-forbidden-api.txt`) allows the extension to set the composer
text VALUE and nothing else. LinkedIn's composer accepts a plain DOM text change. WhatsApp Web and Instagram
use rich editors that ignore plain DOM changes, so the draft would never appear in the message box.

## Decision
The WhatsApp and Instagram adapters insert approved text with `document.execCommand("insertText", false, text)`
on the focused composer (`adapter_common.js: insertIntoComposer`). Nothing else is added: no clicks, no submit,
no keyboard or mouse events, no `dispatchEvent`.

## Why this stays inside the safety contract
- It is `composer_text_injection` (set composer text), not a send path. There is no API in it that triggers Send.
- It runs only after the gateway returned `READY_FOR_INJECTION` for text the user edited and approved.
- CI fails the build if any shipped script contains `.click(`, `.submit(`, `requestSubmit`, `dispatchEvent`,
  keyboard/mouse/pointer events, `fetch(`, `XMLHttpRequest`, `WebSocket` or `sendBeacon`.
- The end-to-end suite asserts the site's Send handler is never called.

## Required change-control step (not done in this PR)
Add this row to `ALLOWED-APIS` in the frozen file and recompute `extension-forbidden-api.sha256`:
`document.execCommand('insertText') on the composer | insert approved draft text only | composer_text_injection`
The frozen file was deliberately NOT edited here; that is the owner's sign-off.

## Consequences
Insertion on WhatsApp/Instagram depends on those sites' editors. If a site changes, insertion fails safely
(the popup tells the user and offers Copy). Verified on synthetic pages only; real-site verification is a release blocker.
