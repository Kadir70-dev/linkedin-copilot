# Privacy policy draft — not approved for publication

Product: LinkedIn Personal Copilot (human-reviewed draft assistant).
Single purpose: draft and insert LinkedIn profile connection notes or open-thread replies for human review; user clicks Send/Connect.

## Data that the extension may process
- Visible name, headline, About, experience, location and connection-count facts from the profile the user elects to analyze.
- Open message/thread text that the user elects to use in a reply.
- Draft text, user edits, approval and limited workflow state.

## Data handling known from older source
- Extension sends selected data to the user's locally installed Go gateway at http://localhost:8080.
- The legacy GitHub version persists workflow state in local SQLite and uses locally configured AI components.
- The newer un-synced Phase-0 implementation reportedly uses PostgreSQL and local Ollama via FastAPI.
- The final released code must be inspected before any assertion of no cloud processing or limited retention.

## Owner must confirm and complete
- Legal/developer identity and actual support email.
- Accurate privacy policy URL accessible to all Chrome Web Store users.
- Explicit affirmative user consent before capturing/transmitting LinkedIn data.
- Exact data processors, storage and backups, retention limits, model data handling, deletion/export and security.
- Whether telemetry exists, whether any data is shared, and whether uninstall deletes saved local records.
- Disclose all changes to collection and processing clearly.

This is a review draft, not a final legal declaration. Do not submit as a final policy without owner verification.