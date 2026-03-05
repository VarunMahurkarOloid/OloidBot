"""
Lightweight HTTP server for Google OAuth callbacks.
Reads Google credentials from the encrypted user store (set via /setup).
"""

import json
import logging
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from google_auth_oauthlib.flow import Flow

from .config import settings
from .user_store import user_store

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_slack_client = None


def set_slack_client(client):
    global _slack_client
    _slack_client = client


def build_oauth_url(slack_user_id: str) -> str:
    """Generate a Google OAuth URL for a Slack user."""
    state = secrets.token_urlsafe(32)
    user_store.save_oauth_state(state, slack_user_id)

    flow = _create_flow()
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )
    return auth_url


def _create_flow() -> Flow:
    google = user_store.get_admin_google()
    if not google["client_id"] or not google["client_secret"]:
        raise RuntimeError("Google OAuth not configured. Ask an admin to run `/oloid-setup google ...`")

    client_config = {
        "web": {
            "client_id": google["client_id"],
            "client_secret": google["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.effective_oauth_redirect_uri],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=SCOPES,
        redirect_uri=settings.effective_oauth_redirect_uri,
    )


FOCUS_TIMER_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Oloid Focus</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0f0f1a;
    color: #e0e0e0;
    display: flex; justify-content: center; align-items: center;
    height: 100vh;
    overflow: hidden;
    user-select: none;
  }
  .container {
    text-align: center;
    padding: 16px;
    width: 100%; height: 100%;
    display: flex; flex-direction: column;
    justify-content: center; align-items: center;
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
  }
  h1 { font-size: 0.85em; color: #a78bfa; margin-bottom: 4px; letter-spacing: 2px; text-transform: uppercase; }
  .subtitle { color: #6b7280; font-size: 0.7em; margin-bottom: 8px; }
  .timer {
    font-size: 3em; font-weight: 200; color: #f0f0f0;
    font-variant-numeric: tabular-nums;
    margin: 2px 0;
    text-shadow: 0 0 20px rgba(167, 139, 250, 0.3);
  }
  .progress-ring { margin: 4px auto; }
  .progress-bg { stroke: #2a2a3e; }
  .progress-bar { stroke: #a78bfa; transition: stroke-dashoffset 1s linear; stroke-linecap: round; }
  .progress-bar.warn { stroke: #f59e0b; }
  .progress-bar.urgent { stroke: #ef4444; }
  .controls { margin-top: 8px; }
  .btn {
    padding: 6px 18px; border: none; border-radius: 8px;
    font-size: 0.8em; cursor: pointer; margin: 0 4px;
    transition: all 0.2s; font-weight: 500;
  }
  .btn-primary { background: #6366f1; color: white; }
  .btn-primary:hover { background: #818cf8; }
  .btn-secondary { background: #2a2a3e; color: #a78bfa; }
  .btn-secondary:hover { background: #3a3a4e; }
  .status { margin-top: 6px; font-size: 0.7em; color: #6b7280; }
  .status.active { color: #a78bfa; }
  .done { display: none; }
  .done h2 { font-size: 1.4em; color: #10b981; margin-bottom: 8px; }
  .done p { color: #9ca3af; font-size: 0.85em; }
</style>
</head>
<body>
<div class="container">
  <div id="timerView">
    <h1>Oloid Focus</h1>
    <p class="subtitle">DURATION_LABEL</p>
    <svg class="progress-ring" width="150" height="150" viewBox="0 0 150 150">
      <circle class="progress-bg" cx="75" cy="75" r="65" fill="none" stroke-width="5"/>
      <circle class="progress-bar" id="progressBar" cx="75" cy="75" r="65" fill="none"
        stroke-width="5" stroke-dasharray="408.41" stroke-dashoffset="0"
        transform="rotate(-90 75 75)"/>
    </svg>
    <div class="timer" id="display">TIMER_DISPLAY</div>
    <div class="controls">
      <button class="btn btn-primary" id="startBtn" onclick="toggle()">Start</button>
      <button class="btn btn-secondary" onclick="resetTimer()">Reset</button>
    </div>
    <p class="status" id="status">Ready to focus</p>
  </div>
  <div class="done" id="doneView">
    <h2>Done!</h2>
    <p>Great work! Take a break.</p>
    <p style="margin-top:10px;font-size:1.8em;">&#127881;</p>
    <button class="btn btn-primary" style="margin-top:12px;" onclick="resetTimer()">Again</button>
  </div>
</div>
<script>
const TOTAL = TOTAL_SECONDS;
const AUTOSTART = AUTOSTART_FLAG;
let remaining = TOTAL;
let running = false;
let interval = null;
const circumference = 2 * Math.PI * 65;

function fmt(s) {
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return String(m).padStart(2, '0') + ':' + String(sec).padStart(2, '0');
}
function updateUI() {
  document.getElementById("display").textContent = fmt(remaining);
  const pct = 1 - remaining / TOTAL;
  document.getElementById("progressBar").style.strokeDashoffset = circumference * (1 - pct);
  const bar = document.getElementById("progressBar");
  bar.classList.remove("warn", "urgent");
  if (remaining <= 60) bar.classList.add("urgent");
  else if (remaining <= TOTAL * 0.2) bar.classList.add("warn");
  document.title = fmt(remaining) + " - Oloid Focus";
}
function toggle() {
  if (running) { pause(); } else { start(); }
}
function start() {
  running = true;
  document.getElementById("startBtn").textContent = "Pause";
  document.getElementById("status").textContent = "Focusing...";
  document.getElementById("status").className = "status active";
  interval = setInterval(() => {
    remaining--;
    updateUI();
    if (remaining <= 0) { finish(); }
  }, 1000);
}
function pause() {
  running = false;
  clearInterval(interval);
  document.getElementById("startBtn").textContent = "Resume";
  document.getElementById("status").textContent = "Paused";
  document.getElementById("status").className = "status";
}
function finish() {
  clearInterval(interval);
  running = false;
  document.getElementById("timerView").style.display = "none";
  document.getElementById("doneView").style.display = "flex";
  document.getElementById("doneView").style.flexDirection = "column";
  document.getElementById("doneView").style.alignItems = "center";
  document.getElementById("doneView").style.justifyContent = "center";
  document.getElementById("doneView").style.height = "100%";
  document.title = "Break time!";
  if (Notification.permission === "granted") {
    new Notification("Oloid Focus", { body: "Session complete! Time for a break." });
  }
  try { new Audio("data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdH2Pn5yYjHxre3eFkZiWkIR4b3R/ipOXlpCFem92gIuUl5WPhHlvdYGLlJeVj4R5b3WBi5SXlY+EeW92gYuUl5WPhHlvdYGLlJeVj4R5b3aBi5SXlY+EeW91gYuUl5WPhHlvdoGLlJeVj4R5b3WBi5SXlY+EeW91gYuUl5WPhHlvdoGLlJeVkIR5b3WBi5SXlY+EeW91gYuUl5WPhHlvdoGLlJeUj4R5b3WBi5SXlY+EeW91gYuUl5WPhHlvdYGLlJeVj4R5b3aBi5SXlY+EeW91gYuUl5WPhHlvdoGLlJeVj4R5b3WBi5SXlY+FeW91gYuUl5WPhHlvdoGLlJeVj4R5b3WBi5SXlY+EeW91gYuUl5WPhHlvdoGLlJeVj4R5b3WBi5SXlY+EeW91gQ==").play(); } catch(e) {}
  // Auto-close the window after a short delay so the user sees the notification
  setTimeout(() => { window.close(); }, 3000);
}
function resetTimer() {
  clearInterval(interval);
  running = false;
  remaining = TOTAL;
  document.getElementById("timerView").style.display = "flex";
  document.getElementById("timerView").style.flexDirection = "column";
  document.getElementById("timerView").style.alignItems = "center";
  document.getElementById("timerView").style.justifyContent = "center";
  document.getElementById("timerView").style.height = "100%";
  document.getElementById("doneView").style.display = "none";
  document.getElementById("startBtn").textContent = "Start";
  document.getElementById("status").textContent = "Ready to focus";
  document.getElementById("status").className = "status";
  updateUI();
}
if ("Notification" in window && Notification.permission === "default") { Notification.requestPermission(); }
updateUI();
if (AUTOSTART) { setTimeout(start, 500); }
</script>
</body></html>"""


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # ── Health check (Render uses this to verify service is alive) ──
        if parsed.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","service":"oloidbot"}')
            return

        # ── Focus timer page ──
        if parsed.path == "/focus":
            params = urllib.parse.parse_qs(parsed.query)
            minutes = int(params.get("m", [25])[0])
            autostart = params.get("autostart", ["0"])[0] == "1"
            total_seconds = minutes * 60
            mins_display = f"{minutes:02d}"
            duration_label = f"{minutes} min deep work"

            html = (
                FOCUS_TIMER_HTML
                .replace("TOTAL_SECONDS", str(total_seconds))
                .replace("TIMER_DISPLAY", f"{mins_display}:00")
                .replace("DURATION_LABEL", duration_label)
                .replace("AUTOSTART_FLAG", "true" if autostart else "false")
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
            return

        if parsed.path != "/oauth/callback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        params = urllib.parse.parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        error = params.get("error", [None])[0]

        if error:
            self._respond(400, f"Authorization denied: {error}")
            return

        if not code or not state:
            self._respond(400, "Missing code or state parameter.")
            return

        slack_user_id = user_store.pop_oauth_state(state)
        if not slack_user_id:
            self._respond(400, "Invalid or expired state. Please try /oloid-connect-gmail again.")
            return

        try:
            flow = _create_flow()
            flow.fetch_token(code=code)
            creds = flow.credentials
            token_data = json.loads(creds.to_json())
            user_store.save_gmail_token(slack_user_id, token_data)

            # Set initial poll timestamp to now so we don't flood with old emails
            import time
            user_store.set_last_poll_ts(slack_user_id, time.time())

            self._respond(200, "Gmail connected successfully! You can close this tab and return to Slack.")

            if _slack_client:
                try:
                    _slack_client.chat_postMessage(
                        channel=slack_user_id,
                        text=(
                            "Your Gmail is now connected!\n\n"
                            "*Try these commands:*\n"
                            "• `/oloid-summarize` — AI summary of your recent emails\n"
                            "• `/oloid-emails` — list emails with Gmail links\n"
                            "• `/oloid-ask` — ask me anything\n"
                            "• Just DM me a question about your emails!\n\n"
                            "You'll also get automatic notifications when new emails arrive."
                        ),
                    )
                except Exception:
                    logger.exception("Failed to send Slack confirmation")

            logger.info("Gmail connected for user %s", slack_user_id)
        except Exception as e:
            logger.exception("OAuth token exchange failed")
            self._respond(500, f"Token exchange failed: {e}")

    def _respond(self, status: int, message: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        color = "#2ecc71" if status == 200 else "#e74c3c"
        html = f"""<!DOCTYPE html>
<html><head><title>OloidBot</title></head>
<body style="font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#1a1a2e;color:white;">
<div style="text-align:center;padding:40px;border-radius:12px;background:#16213e;box-shadow:0 4px 20px rgba(0,0,0,0.3);">
<h1 style="color:{color};">{"Connected!" if status == 200 else "Error"}</h1>
<p>{message}</p>
</div></body></html>"""
        self.wfile.write(html.encode())

    def log_message(self, format, *args):
        logger.debug("OAuth server: %s", format % args)


def start_oauth_server():
    port = settings.effective_port
    server = HTTPServer(("0.0.0.0", port), OAuthCallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("OAuth callback server running on port %d", port)
    return server
