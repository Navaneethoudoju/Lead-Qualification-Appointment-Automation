/**
 * AI LeadFlow embeddable chat widget.
 *
 * NOTE ON STACK CHOICE: the documentation (Section 3) specifies a React
 * embeddable widget. This build uses plain vanilla JS instead so it can be
 * embedded via a single <script> tag with ZERO build step (no npm, no
 * bundler) and runs correctly on any static host or even file:// during
 * local testing. It is a drop-in replacement for the same job ("standard,
 * easy to embed via script tag") and talks to the exact same /api/message
 * contract a React widget would. See README for the React upgrade path if
 * you want it later (e.g. to reuse design-system components).
 *
 * Session 6: visual refresh only (professional/elegant pass) — brand color,
 * typing indicator, quick-reply starter chips, smoother open/close
 * animation, avatar + status dot in the header. No change to the wire
 * protocol (/api/message) or the session-id logic below.
 *
 * Usage: <script src="widget.js" data-api-base="http://localhost:8000" data-brand="Sunrise Family Clinic"></script>
 */
(function () {
  const scriptTag = document.currentScript;
  const API_BASE = (scriptTag && scriptTag.getAttribute("data-api-base")) || "http://localhost:8000";
  const BRAND = (scriptTag && scriptTag.getAttribute("data-brand")) || "AI LeadFlow";
  const CHANNEL = "web";
  const SESSION_KEY = "ai_leadflow_session_id";
  const STARTERS = ["What are your working hours?", "What services do you offer?", "I'd like to book an appointment"];

  function getSessionId() {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id) {
      id = "web-" + Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(SESSION_KEY, id);
    }
    return id;
  }

  function injectStyles() {
    const style = document.createElement("style");
    style.textContent = `
      #ai-leadflow-launcher {
        position: fixed; bottom: 22px; right: 22px; width: 58px; height: 58px;
        border-radius: 50%; border: none; cursor: pointer; z-index: 999998;
        background: linear-gradient(135deg, #6a63f6, #4338ca);
        color: #fff; font-size: 22px; display: flex; align-items: center; justify-content: center;
        box-shadow: 0 10px 28px -6px rgba(67,56,202,0.55);
        transition: transform 0.15s ease, box-shadow 0.15s ease;
      }
      #ai-leadflow-launcher:hover { transform: translateY(-2px); box-shadow: 0 14px 32px -6px rgba(67,56,202,0.6); }
      #ai-leadflow-launcher .df-icon-chat { display: inline-block; }
      #ai-leadflow-launcher.open .df-icon-chat { display: none; }
      #ai-leadflow-launcher .df-icon-close { display: none; font-weight: 300; }
      #ai-leadflow-launcher.open .df-icon-close { display: inline-block; }
      #ai-leadflow-launcher .df-dot {
        position: absolute; top: 3px; right: 3px; width: 12px; height: 12px; border-radius: 50%;
        background: #22c55e; border: 2px solid #fff;
      }
      #ai-leadflow-launcher.open .df-dot { display: none; }

      #ai-leadflow-panel {
        position: fixed; bottom: 92px; right: 22px; width: 366px; max-width: calc(100vw - 32px);
        height: 540px; max-height: calc(100vh - 140px); background: #fff; border-radius: 18px;
        box-shadow: 0 24px 60px -12px rgba(20,22,31,0.35), 0 2px 8px rgba(20,22,31,0.08);
        display: flex; flex-direction: column; overflow: hidden;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        z-index: 999999; opacity: 0; transform: translateY(14px) scale(0.98); pointer-events: none;
        transition: opacity 0.18s ease, transform 0.18s ease;
      }
      #ai-leadflow-panel.open { opacity: 1; transform: translateY(0) scale(1); pointer-events: auto; }

      #ai-leadflow-header {
        background: linear-gradient(135deg, #6a63f6, #4338ca); color: #fff; padding: 16px 18px;
        display: flex; align-items: center; gap: 11px; flex-shrink: 0;
      }
      #ai-leadflow-avatar {
        width: 36px; height: 36px; border-radius: 50%; background: rgba(255,255,255,0.22);
        display: flex; align-items: center; justify-content: center; font-size: 17px; flex-shrink: 0;
      }
      #ai-leadflow-header-text { flex: 1; min-width: 0; }
      #ai-leadflow-header-text .name { font-weight: 650; font-size: 14.5px; line-height: 1.25; }
      #ai-leadflow-header-text .status { font-size: 11.5px; opacity: 0.88; display: flex; align-items: center; gap: 5px; margin-top: 1px; }
      #ai-leadflow-header-text .status .dot { width: 6px; height: 6px; border-radius: 50%; background: #4ade80; display: inline-block; }
      #ai-leadflow-close {
        background: rgba(255,255,255,0.14); border: none; color: white; width: 26px; height: 26px; border-radius: 50%;
        font-size: 15px; cursor: pointer; flex-shrink: 0; line-height: 1;
      }
      #ai-leadflow-close:hover { background: rgba(255,255,255,0.24); }

      #ai-leadflow-messages { flex: 1; overflow-y: auto; padding: 16px 14px; background: #f7f7fb; display: flex; flex-direction: column; }
      #ai-leadflow-messages::-webkit-scrollbar { width: 6px; }
      #ai-leadflow-messages::-webkit-scrollbar-thumb { background: #d8d9e6; border-radius: 3px; }

      .ai-leadflow-msg {
        margin: 5px 0; max-width: 82%; padding: 9px 13px; border-radius: 15px; font-size: 13.5px;
        line-height: 1.45; white-space: pre-wrap; animation: ai-leadflow-in 0.16s ease;
      }
      @keyframes ai-leadflow-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
      .ai-leadflow-msg.user {
        margin-left: auto; background: linear-gradient(135deg, #6a63f6, #4338ca); color: white;
        border-bottom-right-radius: 4px;
      }
      .ai-leadflow-msg.bot { margin-right: auto; background: #fff; color: #1a1a1a; border: 1px solid #eceef4; border-bottom-left-radius: 4px; }
      .ai-leadflow-msg.escalated { border-left: 3px solid #d97706; }

      .ai-leadflow-typing { margin-right: auto; background: #fff; border: 1px solid #eceef4; border-radius: 15px; border-bottom-left-radius: 4px; padding: 11px 15px; display: flex; gap: 4px; align-items: center; }
      .ai-leadflow-typing span { width: 6px; height: 6px; border-radius: 50%; background: #a8abc0; display: inline-block; animation: ai-leadflow-bounce 1.1s infinite ease-in-out; }
      .ai-leadflow-typing span:nth-child(2) { animation-delay: 0.15s; }
      .ai-leadflow-typing span:nth-child(3) { animation-delay: 0.3s; }
      @keyframes ai-leadflow-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.5; } 30% { transform: translateY(-4px); opacity: 1; } }

      #ai-leadflow-starters { display: flex; flex-direction: column; gap: 7px; margin-top: 10px; }
      .ai-leadflow-chip {
        align-self: flex-start; background: #fff; border: 1px solid #dcdef0; color: #4338ca;
        border-radius: 999px; padding: 7px 13px; font-size: 12.5px; cursor: pointer; text-align: left;
        font-weight: 500; transition: background 0.12s, border-color 0.12s;
      }
      .ai-leadflow-chip:hover { background: #eef0ff; border-color: #c7cbf5; }

      #ai-leadflow-inputrow { display: flex; align-items: center; border-top: 1px solid #eceef4; padding: 10px; gap: 8px; background: #fff; flex-shrink: 0; }
      #ai-leadflow-input {
        flex: 1; border: 1px solid #e2e4ef; border-radius: 999px; padding: 10px 15px; font-size: 13.5px;
        outline: none; font-family: inherit;
      }
      #ai-leadflow-input:focus { border-color: #6a63f6; box-shadow: 0 0 0 3px rgba(106,99,246,0.14); }
      #ai-leadflow-send {
        background: linear-gradient(135deg, #6a63f6, #4338ca); color: white; border: none; border-radius: 50%;
        width: 38px; height: 38px; cursor: pointer; flex-shrink: 0; font-size: 15px; display: flex;
        align-items: center; justify-content: center;
      }
      #ai-leadflow-send:disabled { opacity: 0.45; cursor: default; }
      #ai-leadflow-footer { text-align: center; font-size: 10.5px; color: #a8abc0; padding: 4px 0 8px; flex-shrink: 0; background: #fff; }

      @media (max-width: 480px) {
        #ai-leadflow-panel { right: 12px; bottom: 84px; width: calc(100vw - 24px); height: calc(100vh - 120px); }
        #ai-leadflow-launcher { right: 16px; bottom: 16px; }
      }
    `;
    document.head.appendChild(style);
  }

  function buildDOM() {
    const launcher = document.createElement("button");
    launcher.id = "ai-leadflow-launcher";
    launcher.setAttribute("aria-label", "Open chat");
    launcher.innerHTML =
      '<span class="df-icon-chat">&#128172;</span><span class="df-icon-close">&times;</span><span class="df-dot"></span>';

    const panel = document.createElement("div");
    panel.id = "ai-leadflow-panel";
    panel.innerHTML = `
      <div id="ai-leadflow-header">
        <div id="ai-leadflow-avatar">&#10024;</div>
        <div id="ai-leadflow-header-text">
          <div class="name">${escapeHtml(BRAND)}</div>
          <div class="status"><span class="dot"></span> Typically replies instantly</div>
        </div>
        <button id="ai-leadflow-close" aria-label="Close chat">&times;</button>
      </div>
      <div id="ai-leadflow-messages"></div>
      <div id="ai-leadflow-inputrow">
        <input id="ai-leadflow-input" type="text" placeholder="Type your message…" autocomplete="off" />
        <button id="ai-leadflow-send" aria-label="Send">&#10148;</button>
      </div>
      <div id="ai-leadflow-footer">Powered by AI LeadFlow</div>
    `;

    document.body.appendChild(launcher);
    document.body.appendChild(panel);

    launcher.addEventListener("click", () => {
      const opening = !panel.classList.contains("open");
      panel.classList.toggle("open", opening);
      launcher.classList.toggle("open", opening);
      if (opening) {
        document.getElementById("ai-leadflow-input").focus();
        if (!panel.dataset.greeted) {
          appendMessage("bot", "Hi there! Ask me about services, pricing, or book an appointment — I'm happy to help.");
          appendStarters();
          panel.dataset.greeted = "1";
        }
      }
    });
    document.getElementById("ai-leadflow-close").addEventListener("click", () => {
      panel.classList.remove("open");
      launcher.classList.remove("open");
    });

    const input = document.getElementById("ai-leadflow-input");
    const sendBtn = document.getElementById("ai-leadflow-send");

    function send(text) {
      text = (text || input.value).trim();
      if (!text) return;
      input.value = "";
      removeStarters();
      appendMessage("user", text);
      sendBtn.disabled = true;
      showTyping();
      sendMessage(text)
        .then((res) => {
          hideTyping();
          appendMessage("bot", res.reply_text, res.escalated);
        })
        .catch(() => {
          hideTyping();
          appendMessage("bot", "Sorry, something went wrong reaching our server. Please try again in a moment.");
        })
        .finally(() => {
          sendBtn.disabled = false;
          input.focus();
        });
    }

    sendBtn.addEventListener("click", () => send());
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") send();
    });

    window.__aiLeadflowSend = send; // used by starter chips
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function appendStarters() {
    const container = document.getElementById("ai-leadflow-messages");
    const wrap = document.createElement("div");
    wrap.id = "ai-leadflow-starters";
    wrap.innerHTML = STARTERS.map((s) => `<button class="ai-leadflow-chip">${escapeHtml(s)}</button>`).join("");
    container.appendChild(wrap);
    wrap.querySelectorAll(".ai-leadflow-chip").forEach((chip) => {
      chip.addEventListener("click", () => window.__aiLeadflowSend(chip.textContent));
    });
    container.scrollTop = container.scrollHeight;
  }
  function removeStarters() {
    const el = document.getElementById("ai-leadflow-starters");
    if (el) el.remove();
  }

  function appendMessage(role, text, escalated) {
    const container = document.getElementById("ai-leadflow-messages");
    const el = document.createElement("div");
    el.className = "ai-leadflow-msg " + role + (escalated ? " escalated" : "");
    el.textContent = text;
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }

  function showTyping() {
    const container = document.getElementById("ai-leadflow-messages");
    const el = document.createElement("div");
    el.className = "ai-leadflow-typing";
    el.id = "ai-leadflow-typing-indicator";
    el.innerHTML = "<span></span><span></span><span></span>";
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
  }
  function hideTyping() {
    const el = document.getElementById("ai-leadflow-typing-indicator");
    if (el) el.remove();
  }

  async function sendMessage(text) {
    const resp = await fetch(API_BASE + "/api/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: getSessionId(), channel: CHANNEL, text }),
    });
    if (!resp.ok) throw new Error("Request failed: " + resp.status);
    return resp.json();
  }

  injectStyles();
  buildDOM();
})();
