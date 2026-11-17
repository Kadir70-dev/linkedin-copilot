# K70 AI Copilot — permissions, privacy and support (draft for owner review)

| Permission | Why |
|---|---|
| `activeTab` | Lets the popup open on the tab where you click the toolbar icon. Grants nothing on other tabs and nothing in the background. |
| `scripting` | Injects the popup (`premium_panel.js/css`) into that one tab after your click. |
| `storage` | Keeps your gateway access key in this browser profile only. |
| `http://localhost:8080/*` | Talks to your own gateway. No other host is contacted. |

No content scripts run on any site by default, and there is no access to browsing history, cookies, tabs list or other pages.

## What is sent where
Only the text in the popup (pasted, or your selection) plus your tone/instructions is sent to your gateway when you press Generate. It is stored there until you act on it and purged after `RETENTION_DAYS` (default 30). No analytics, no ads, no sale of data.

## Support checklist
1. Popup does not open? Reload the page after installing; it cannot open on browser-internal pages (`chrome://`, Web Store).
2. "Gateway unavailable": start the gateway and AI brain, and check the access key in Options.
3. Copy blocked: the page blocked the clipboard; press Ctrl+C on the highlighted draft.

*Draft text for the owner; not legal advice.*
