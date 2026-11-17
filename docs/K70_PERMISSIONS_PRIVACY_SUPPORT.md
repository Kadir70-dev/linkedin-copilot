# K70 AI Copilot — permissions, privacy and support

## Browser permissions (manifest v0.2.0)
| Permission | Why |
|---|---|
| `storage` | Keeps your gateway access key and the per-platform on/off switches in this browser profile only. |
| Sites: `linkedin.com`, `web.whatsapp.com`, `instagram.com` | Show the popup and (only when you press Capture) read the open chat. WhatsApp and Instagram stay **off until you switch them on** in the extension Options. |
| Gateway host (`localhost:8080` by default; your HTTPS gateway when configured with `scripts/configure_extension.py`) | Sends the text you chose to your own gateway to get a draft. Nothing else is contacted. |

No `tabs`, `cookies`, `history`, `webRequest`, `scripting` or `<all_urls>` access.

## What the extension does and never does
- Reads a chat **only** when you press *Capture current chat*, and only the chat open at that moment. It never opens, scrolls or scans other chats and never reads in the background.
- Inserts text **only** after you approve the draft. It never presses Send, Connect or Submit and sends no keyboard or mouse events.
- Sends your text only to your configured gateway (HTTPS and an access key for any remote gateway).

## Privacy policy (draft for a store listing — have it reviewed before publishing)
- **What is processed:** the message or chat text you capture or paste, and the instructions you type. 
- **Where it goes:** your own K70 gateway and AI service. For a local install that is your computer. A remote gateway must use HTTPS and an access key.
- **Retention:** the gateway deletes drafted-chat text from its database and queues after `RETENTION_DAYS` (default 30; set 0 to keep). You can also delete the database volume at any time.
- **Third parties:** none by default. If the owner's optional Claude engine is enabled on the server, text for that owner's requests is sent to that provider.
- **Other people's messages:** captured chats contain other people's words. Use the tool only for conversations you are entitled to process.
- **Not collected:** no analytics, no advertising identifiers, no browsing history, no contacts lists.
- **Contact:** [support email to be added by the owner].

## Support page (draft)
1. Popup missing? Reload the site; for WhatsApp/Instagram turn the platform on in the extension Options.
2. "Gateway unavailable": check the gateway is running (local: `curl http://localhost:8080/health`), and the access key in Options.
3. "Could not read this chat": the site changed its layout. Paste the message into the box manually and report the site and date.
4. Draft not inserted: use **Copy** and paste it yourself.
5. Revoke a user: remove their key from `GATEWAY_API_TOKENS` and restart the gateway.
