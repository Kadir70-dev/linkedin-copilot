// Synthetic pages that mimic only the DOM hooks each adapter uses. All names/messages are fictional.
export const LINKEDIN = `<!doctype html><title>mock linkedin</title><body>
<div class="msg-s-message-list-content"><p class="msg-s-event-listitem__body">Hi Abdul, are you free for a call on Thursday?</p></div>
<div class="msg-form__contenteditable" contenteditable="true" role="textbox" id="composer"></div>
<button id="send" onclick="window.__sent=(window.__sent||0)+1">Send</button></body>`;
export const WHATSAPP = `<!doctype html><title>mock whatsapp</title><body>
<div id="side"><div class="selectable-text" id="other-chat">SECRET OTHER CHAT: bank pin 4821</div></div>
<div id="main"><header><span title="Saima">Saima</span></header>
 <div data-pre-plain-text="[5:10 PM, 11/17/2026] Saima Shaikh: "><span class="selectable-text">Can you share the sample photos?</span></div>
 <div data-pre-plain-text="[5:20 PM, 11/17/2026] Saima Shaikh: "><span class="selectable-text">Also what is the MOQ?</span></div>
 <footer><div id="composer" contenteditable="true" role="textbox" data-tab="10"></div><button id="send" onclick="window.__sent=(window.__sent||0)+1">Send</button></footer></div></body>`;
export const INSTAGRAM = `<!doctype html><title>mock instagram</title><body><main>
 <div role="row">Hey! Loved your latest post</div><div role="row">Do you do collabs?</div>
 <div id="composer" role="textbox" contenteditable="true" aria-label="Message"></div>
 <button id="send" onclick="window.__sent=(window.__sent||0)+1">Send</button></main></body>`;
