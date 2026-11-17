# K70 Floating Copilot — LinkedIn Beta

The `services/extension` Chrome/Brave Manifest V3 extension already supports LinkedIn messaging. This beta adds a premium, draggable, minimizable popup and a second **Write a Message** mode for instructions in the user's own words.

## Current scope

- **LinkedIn Web only** (`https://www.linkedin.com/messaging/*`).
- `Reply Suggestions`: click **Capture current chat**, review or edit the captured message, choose a tone, click Generate Reply.
- `Write a Message`: type instructions (e.g. "Tell the recruiter I'm available Thursday, short and friendly"); optionally tick **Include my currently selected conversation**.
- Results are always editable. **Copy** or **Approve & Insert** inserts the approved text into the LinkedIn composer, but NEVER clicks Send.
- Chrome/Brave popup floats at the bottom right; can be dragged from its header and minimized to a small `✦` launcher.
- Does NOT scrape chats in the background, auto-reply, publish, or send messages. Capture is user-triggered. The local AI runs only on the selected/copied text.

The original panel is retained as fallback if `premium_panel.js` is missing.

## Run locally

```bash
git checkout feature/k70-floating-copilot-v1-20261117
cp .env.example .env  # only first time; protect your existing .env
make up
make pull-model       # only first time
curl http://localhost:8080/health
```

Open `chrome://extensions` (or `brave://extensions`), enable Developer mode, select **Load unpacked** and choose the repository's `services/extension` folder. Reload LinkedIn, open `https://www.linkedin.com/messaging/` and try both modes. When code changes, press **Reload** on the unpacked extension.

The extension calls the **local** gateway at `http://localhost:8080` through its service worker. There is NO secure public deployment / cloud authentication yet. Do not expose port 8080 to the public internet or deploy this local gateway as a public endpoint.

The Compose profile in this feature branch binds Redis, Ollama and the gateway to **127.0.0.1 only**, so browser extension and API must run on the same computer (or a separately authenticated secure tunnel, which is not configured here). This intentionally does **not** turn the cloud website into the extension backend.

## Developer checks

```bash
node --check services/extension/premium_panel.js
node --check services/extension/content.js
node --check services/extension/background.js
python3 -m py_compile services/ai-brain/worker.py
(cd services/gateway-go && go test ./...)
```

## Browser QA — required before marking production-ready

1. Confirm popup appears on LinkedIn messaging, not on arbitrary websites.
2. Capture a real open conversation only when requested, edit its text, and generate a professional draft.
3. Generate a custom message **without** incoming message text. Verify the model follows the user's instructions rather than treating them as inbound text.
4. Toggle "Include my currently selected conversation" and confirm no context is sent when unchecked.
5. Check Professional, Friendly, Casual and Short variants (each regenerated deliberately).
6. Edit draft, copy, and use Approve & Insert. Confirm actual LinkedIn composer updates, and NO Send is pressed by the extension.
7. Minimize to launcher, reopen, drag within viewport, check narrow screens and reduced motion.
8. Reject a draft and confirm no insertion. Disconnect gateway and verify an error is shown without sending/injection.
9. Switch LinkedIn SPA routes (/messaging → /in → /messaging) and check old popup nodes don't accumulate.
10. Verify a fresh generated request cannot display the previous draft after switching threads.

## Pending before public release

- Test against live LinkedIn DOM and browser permissions.
- Build consent-driven WhatsApp Web and Instagram Web adapters independently, with platform-specific selector tests; do not reuse LinkedIn selectors blindly.
- Authenticate + HTTPS-protect a remotely hosted backend if a cloud release is desired; minimize access scopes and add privacy policy, retention policy, account security, rate limits and explicit consent.
- Store listing/review and user-approved publishing. Do not claim Chrome Web Store publication.

**Safety contract:** AI drafts → human approves → extension inserts text only → human clicks Send.
