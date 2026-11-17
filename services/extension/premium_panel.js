(function install() {
  "use strict";
  // Injected into the active tab only when the user clicks the toolbar icon (activeTab). General
  // purpose: works on any page, reads nothing unless the user presses "Use selected text", and never
  // touches the page's message boxes. Clicking the icon again closes it.
  (function K70PremiumPanel() {
    const existing = document.getElementById("k70-copilot");
    if (existing) { existing.remove(); return; }
    const call = (action, payload) => new Promise((res) => chrome.runtime.sendMessage({ action, payload }, res));
    const threadId = () => "k70";
    const captureThread = () => String(window.getSelection ? window.getSelection() : "").trim();
    const host = document.createElement("section");
    host.id = "k70-copilot";
    host.setAttribute("data-lc-panel", "1");
    host.setAttribute("aria-label", "K70 AI Copilot");
    host.innerHTML = `
      <button type="button" class="k70-launcher" data-action="expand" title="Open K70 Copilot" aria-label="Open K70 Copilot" hidden>✦</button>
      <div class="k70-shell" role="complementary" aria-label="K70 AI writing assistant">
        <header class="k70-header" data-drag="1">
          <div class="k70-heading"><span class="k70-brand-icon">✦</span>
            <div><strong>K70 AI Copilot</strong><small>Draft only · You copy and send it yourself</small></div>
          </div>
          <div class="k70-window-actions">
            <button type="button" data-action="collapse" aria-label="Minimize Copilot" title="Minimize">−</button>
            <button type="button" data-action="collapse" aria-label="Close to floating icon" title="Close to icon">×</button>
          </div>
        </header>
        <div class="k70-body">
          <nav class="k70-tabs" role="tablist" aria-label="Writing mode">
            <button type="button" data-mode="reply" class="active" role="tab" aria-selected="true">✦ Reply Suggestions</button>
            <button type="button" data-mode="compose" role="tab" aria-selected="false">✎ Write a Message</button>
          </nav>
          <section class="k70-pane" data-pane="reply" role="tabpanel">
            <div class="k70-labelrow"><label for="k70-thread">Message you received</label>
              <button type="button" class="k70-textbutton" data-action="capture">Use selected text</button>
            </div>
            <textarea id="k70-thread" rows="3" maxlength="6000" placeholder="Paste a message, or select text on the page and press the button"></textarea>
            <span class="k70-hint">Only the text shown here is sent, and only when you press Generate.</span>
          </section>
          <section class="k70-pane" data-pane="compose" role="tabpanel" hidden>
            <label for="k70-instruction">Tell AI what you want to say</label>
            <textarea id="k70-instruction" rows="4" maxlength="1600" placeholder="e.g. Customer ko politely bolo I'm available Thursday. Keep it short."></textarea>
            <label class="k70-checkbox"><input id="k70-context" type="checkbox"> Include the text I have selected on the page for context</label>
            <span class="k70-hint">Your instructions are treated as your request, not as an incoming message.</span>
          </section>
          <div class="k70-tone-heading">Tone</div>
          <div class="k70-tones" role="group" aria-label="Message tone">
            <button type="button" class="active" data-tone="professional" aria-pressed="true">Professional</button>
            <button type="button" data-tone="friendly" aria-pressed="false">Friendly</button>
            <button type="button" data-tone="casual" aria-pressed="false">Casual</button>
            <button type="button" data-tone="short" aria-pressed="false">Short</button>
          </div>
          <button type="button" class="k70-primary" data-action="generate">✦ Generate Reply</button>
          <p class="k70-status" role="status" aria-live="polite">Choose a message or write your own instructions.</p>
          <section class="k70-result" data-result="1" hidden>
            <label for="k70-draft">Your draft — edit before copying</label>
            <textarea id="k70-draft" rows="4" maxlength="6000"></textarea>
            <div class="k70-actions">
              <button type="button" class="k70-primary" data-action="copy">Copy reply</button>
              <button type="button" class="k70-secondary k70-reject" data-action="reject">Discard</button>
            </div>
          </section>
          <p class="k70-safety">K70 never sends or types into any page. You paste it and press Send yourself.</p>
        </div>
      </div>`;
    document.body.appendChild(host);
    const $ = (q) => host.querySelector(q);
    const shell = $(".k70-shell");
    const launcher = $(".k70-launcher");
    const threadBox = $("#k70-thread");
    const instructions = $("#k70-instruction");
    const draftBox = $("#k70-draft");
    const result = $("[data-result]");
    const generateButton = $("[data-action=generate]");
    const setStatus = (message) => { $(".k70-status").textContent = message; };
    let mode = "reply";
    let tone = "professional";
    let busy = false;
    let activeId = "";
    let generation = 0;

    const toggle = (open) => {
      shell.hidden = !open;
      launcher.hidden = open;
      launcher.setAttribute("aria-expanded", String(open));
    };
    const clearDraft = () => {
      activeId = "";
      result.hidden = true;
      draftBox.value = "";
    };
    const setBusy = (value) => {
      busy = value;
      host.querySelectorAll("[data-mode], [data-tone], [data-action=generate], [data-action=copy], [data-action=reject]")
        .forEach((b) => { b.disabled = value; });
      generateButton.textContent = value ? "Generating with local AI…" :
        (mode === "compose" ? "✦ Write My Message" : "✦ Generate Reply");
    };
    const selectMode = (next) => {
      if (busy || !["reply", "compose"].includes(next)) return;
      mode = next;
      clearDraft();
      host.querySelectorAll("[data-mode]").forEach((button) => {
        const selected = button.dataset.mode === mode;
        button.classList.toggle("active", selected);
        button.setAttribute("aria-selected", String(selected));
      });
      host.querySelectorAll("[data-pane]").forEach((pane) => {
        pane.hidden = pane.dataset.pane !== mode;
      });
      generateButton.textContent = mode === "compose" ? "✦ Write My Message" : "✦ Generate Reply";
      setStatus(mode === "compose" ? "Write your instructions in your own words." : "Paste a message to get a suggestion.");
    };
    host.querySelectorAll("[data-mode]").forEach((button) =>
      button.addEventListener("click", () => selectMode(button.dataset.mode)));
    host.querySelectorAll("[data-tone]").forEach((button) =>
      button.addEventListener("click", () => {
        if (busy) return;
        tone = button.dataset.tone;
        host.querySelectorAll("[data-tone]").forEach((b) => {
          const selected = b.dataset.tone === tone;
          b.classList.toggle("active", selected);
          b.setAttribute("aria-pressed", String(selected));
        });
      }));
    host.querySelectorAll("[data-action=collapse]").forEach((button) =>
      button.addEventListener("click", () => toggle(false)));
    launcher.addEventListener("click", () => toggle(true));
    $("[data-action=capture]").addEventListener("click", () => {
      const captured = (captureThread() || "").slice(-6000);
      threadBox.value = captured;
      clearDraft();
      setStatus(captured ? "Selected text added. Review it before generating." : "Nothing is selected on the page. Paste the message instead.");
    });
    generateButton.addEventListener("click", async () => {
      if (busy) return;
      const custom = mode === "compose";
      const userInstruction = instructions.value.trim();
      const selectedText = custom ?
        ($("#k70-context").checked ? (captureThread() || "").slice(-6000) : "") :
        (threadBox.value.trim() || (captureThread() || "").slice(-6000));
      if (custom && !userInstruction) {
        setStatus("Tell me what message you want to write first.");
        return;
      }
      if (!custom && !selectedText) {
        setStatus("Paste the message you want to reply to.");
        return;
      }
      clearDraft();
      const myGeneration = ++generation;
      const requestId = (threadId().slice(0, 80) + "_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 8));
      setBusy(true);
      setStatus("Preparing a private draft with your local AI…");
      try {
        const ing = await call("ingest", {
          thread_id: requestId,
          text: selectedText,
          mode: custom ? "compose" : "reply",
          instructions: custom ? userInstruction : "",
          tone,
        });
        if (!ing || ing.error) throw new Error((ing && ing.error) || "Local gateway unavailable. Start Docker Compose on your computer.");
        for (let i = 0; i < 90; i++) {
          if (myGeneration !== generation || !host.isConnected) return;
          const d = await call("getDraft", { thread_id: requestId });
          if (!d || d.error) throw new Error((d && d.error) || "Could not retrieve a draft");
          if (d.state === "PENDING_APPROVAL") {
            if (!d.draft_text || !d.draft_text.trim()) throw new Error("AI returned an empty draft. Please retry.");
            activeId = requestId;
            draftBox.value = d.draft_text;
            result.hidden = false;
            setStatus("Draft ready. Edit it, then copy.");
            return;
          }
          await new Promise((resolve) => setTimeout(resolve, 2000));
        }
        throw new Error("AI draft timed out. Check the local Qwen service.");
      } catch (error) {
        setStatus(error.message || "Unable to generate a draft.");
      } finally {
        if (myGeneration === generation && host.isConnected) setBusy(false);
      }
    });
    $("[data-action=copy]").addEventListener("click", async () => {
      if (busy || !activeId || !draftBox.value.trim()) return;
      const text = draftBox.value.trim();
      const approved = await call("approve", { thread_id: activeId, decision: "approve_with_edit", edited_text: text });
      if (!approved || approved.error) { setStatus((approved && approved.error) || "Approval was not confirmed by the gateway."); return; }
      try {
        await navigator.clipboard.writeText(text);
        setStatus("Copied. Paste it where you need it and send it yourself.");
      } catch (_) {
        draftBox.focus();
        draftBox.select();
        setStatus("Copy was blocked by the page. Press Ctrl+C on the selected text.");
      }
    });
    $("[data-action=reject]").addEventListener("click", async () => {
      if (busy || !activeId) return;
      const requestId = activeId;
      const response = await call("approve", { thread_id: requestId, decision: "reject" });
      if (!response || response.error) {
        setStatus((response && response.error) || "Unable to discard the draft.");
        return;
      }
      clearDraft();
      setStatus("Draft discarded. Nothing was copied or sent.");
    });

    // Drag using the title bar. Never drag from the buttons.
    const header = $(".k70-header");
    let drag = null;
    header.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest("button")) return;
      const bounds = host.getBoundingClientRect();
      drag = { x: event.clientX, y: event.clientY, left: bounds.left, top: bounds.top };
      header.setPointerCapture(event.pointerId);
    });
    header.addEventListener("pointermove", (event) => {
      if (!drag) return;
      const bounds = host.getBoundingClientRect();
      const left = Math.min(Math.max(8, drag.left + event.clientX - drag.x), Math.max(8, window.innerWidth - bounds.width - 8));
      const top = Math.min(Math.max(8, drag.top + event.clientY - drag.y), Math.max(8, window.innerHeight - bounds.height - 8));
      host.style.left = left + "px";
      host.style.top = top + "px";
      host.style.right = "auto";
      host.style.bottom = "auto";
    });
    const stopDrag = () => { drag = null; };
    header.addEventListener("pointerup", stopDrag);
    header.addEventListener("pointercancel", stopDrag);
  })();
})();
