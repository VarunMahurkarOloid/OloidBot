import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from .config import get_default_model, settings
from .user_store import user_store

logger = logging.getLogger(__name__)

app: App = None
agent = None

# Cache: slack_user_id → ZoneInfo
_tz_cache: dict[str, ZoneInfo] = {}


def _get_user_tz(user_id: str) -> ZoneInfo:
    """Get a user's timezone from Slack profile. Cached per session."""
    if user_id in _tz_cache:
        return _tz_cache[user_id]
    try:
        bot = _get_bot_client()
        resp = bot.users_info(user=user_id)
        tz_str = resp.data.get("user", {}).get("tz", "UTC")
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = ZoneInfo("UTC")
    _tz_cache[user_id] = tz
    return tz


def _user_now(user_id: str) -> datetime:
    """Get current time in the user's timezone."""
    tz = _get_user_tz(user_id)
    return datetime.now(tz)


def _to_user_time(user_id: str, ts: float) -> datetime:
    """Convert a Unix timestamp to a datetime in the user's timezone."""
    tz = _get_user_tz(user_id)
    return datetime.fromtimestamp(ts, tz=tz)


def _get_app() -> App:
    global app
    if app is None:
        app = App(token=settings.slack_bot_token)
        _register_handlers(app)
    return app


def _get_agent():
    global agent
    if agent is None:
        from .agent import EmailAgent
        agent = EmailAgent()
    return agent


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _parse_reminder(text: str) -> tuple[str, timedelta] | None:
    """Parse reminder text into (message, timedelta).

    Supports:
      "Buy groceries in 30m"
      "Team standup in 2h30m"
      "Submit report in 1d"
      "Check deployment in 5 minutes"
      "Do laundry in 1 hour"
    """
    if not text:
        return None

    # Try to split on " in " from the right
    parts = text.rsplit(" in ", 1)
    if len(parts) != 2:
        return None

    message = parts[0].strip()
    time_str = parts[1].strip().lower()

    if not message or not time_str:
        return None

    total = timedelta()
    matched = False

    # Compact format: 2h30m, 30m, 1d, 5m, 1h
    compact = re.findall(r"(\d+)\s*(d|h|hr|m|min)", time_str)
    if compact:
        for val, unit in compact:
            n = int(val)
            if unit == "d":
                total += timedelta(days=n)
            elif unit in ("h", "hr"):
                total += timedelta(hours=n)
            elif unit in ("m", "min"):
                total += timedelta(minutes=n)
        matched = True

    # Natural format: "5 minutes", "2 hours", "1 day"
    if not matched:
        natural = re.findall(r"(\d+)\s*(minutes?|hours?|days?)", time_str)
        if natural:
            for val, unit in natural:
                n = int(val)
                if unit.startswith("day"):
                    total += timedelta(days=n)
                elif unit.startswith("hour"):
                    total += timedelta(hours=n)
                elif unit.startswith("min"):
                    total += timedelta(minutes=n)
            matched = True

    if not matched or total.total_seconds() <= 0:
        return None

    return (message, total)


def _open_focus_window(url: str, minutes: int, user_id: str, reminder_id: str):
    """Open a 500x500 browser window pinned to the top-right corner.

    Uses a temporary --user-data-dir so Chrome/Edge spawns a dedicated process
    that stays alive until the window is closed. Monitors it for early close.
    """
    import subprocess
    import ctypes
    import tempfile
    import shutil

    size = 500

    try:
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
    except Exception:
        screen_w = 1920

    x = screen_w - size - 20
    y = 20

    # Create a temp profile dir so the browser starts a fresh isolated process
    profile_dir = tempfile.mkdtemp(prefix="oloid_focus_")

    browser_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]

    proc = None
    start_time = time.time()
    expected_end = start_time + minutes * 60

    for browser in browser_paths:
        try:
            proc = subprocess.Popen([
                browser,
                f"--app={url}",
                f"--window-size={size},{size}",
                f"--window-position={x},{y}",
                f"--user-data-dir={profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
            ])
            logger.info("Focus window opened with %s (pid=%s)", browser, proc.pid)
            break
        except FileNotFoundError:
            continue

    if proc is None:
        import webbrowser
        webbrowser.open(url)
        shutil.rmtree(profile_dir, ignore_errors=True)
        return

    # Wait for the browser process to exit (this now blocks correctly)
    proc.wait()
    closed_at = time.time()
    elapsed = closed_at - start_time
    remaining_secs = expected_end - closed_at

    # Clean up temp profile
    try:
        shutil.rmtree(profile_dir, ignore_errors=True)
    except Exception:
        pass

    bot = _get_bot_client()

    if remaining_secs > 5:
        # User closed the browser early
        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)
        logger.info("Focus window closed early by user after %dm %ds", elapsed_min, elapsed_sec)

        from .scheduler import cancel_reminder
        cancel_reminder(user_id, reminder_id)

        try:
            bot.chat_postMessage(
                channel=user_id,
                text=(
                    f":pause_button: *Focus session ended early* after {elapsed_min}m {elapsed_sec}s "
                    f"(out of {minutes}m).\n\n"
                    f"Break reminder cancelled. Use `/oloid-focus` to start a new session when ready."
                ),
            )
        except Exception:
            logger.exception("Failed to send focus-closed DM")
    else:
        logger.info("Focus session completed: %d minutes", minutes)


def _get_bot_client() -> WebClient:
    """Return a WebClient using the bot token (has users:read by default)."""
    return WebClient(token=settings.slack_bot_token)


def _resolve_user_name(uid: str, cache: dict) -> str:
    """Resolve a Slack user ID to a real display name using the bot token."""
    if uid in cache:
        return cache[uid]
    bot = _get_bot_client()
    name = ""
    try:
        resp = bot.users_info(user=uid)
        data = resp.data if hasattr(resp, "data") else resp
        user_obj = data.get("user") or {}
        profile = user_obj.get("profile") or {}
        logger.debug(
            "Resolving %s: real_name=%r, display_name=%r, name=%r",
            uid,
            profile.get("real_name"),
            profile.get("display_name"),
            user_obj.get("name"),
        )
        for candidate in (
            profile.get("real_name_normalized"),
            profile.get("real_name"),
            profile.get("display_name_normalized"),
            profile.get("display_name"),
            user_obj.get("real_name"),
            user_obj.get("name"),
        ):
            if candidate and isinstance(candidate, str) and candidate.strip():
                name = candidate.strip()
                break
    except Exception as e:
        logger.warning("Could not resolve user %s: %s", uid, e)

    name = name or uid
    cache[uid] = name
    logger.debug("Resolved %s → %s", uid, name)
    return name


def _resolve_mentions_in_text(text: str, cache: dict) -> str:
    """Replace <@USERID> patterns in text with real names."""
    def replacer(m):
        uid = m.group(1)
        return _resolve_user_name(uid, cache)
    return re.sub(r"<@([A-Z0-9]+)>", replacer, text)


def _fetch_slack_summary(client: WebClient, user_id: str, days: int) -> dict:
    """Fetch Slack messages from the user's channels for the past N days.

    Uses SLACK_USER_TOKEN (xoxp-) if available to read the user's actual channels
    and DMs. Falls back to bot token if no user token is configured.

    Returns dict with 'mentions' (high priority) and 'messages' (normal) lists.
    """
    # Use user token if available, otherwise fall back to bot token
    if settings.slack_user_token:
        api_client = WebClient(token=settings.slack_user_token)
        logger.debug("ease-my-life: using user token")
    else:
        api_client = client
        logger.debug("ease-my-life: no user token, using bot token (limited access)")

    oldest_ts = int(time.time() - days * 86400)
    mentions = []
    messages = []
    name_cache: dict = {}  # user_id → display name

    # Fetch each channel type separately so one missing scope doesn't block others
    all_channels = []
    for ch_type in ["public_channel", "private_channel", "mpim", "im"]:
        try:
            cursor = None
            while True:
                kwargs = {"types": ch_type, "limit": 200, "exclude_archived": True}
                if cursor:
                    kwargs["cursor"] = cursor
                result = api_client.users_conversations(**kwargs)
                batch = result.get("channels", [])
                # tag each channel with its type for later use
                for ch in batch:
                    ch["_type"] = ch_type
                all_channels.extend(batch)
                cursor = result.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except Exception as e:
            logger.warning("Failed to list channels (type=%s): %s", ch_type, e)

    logger.debug("ease-my-life: found %d channels total", len(all_channels))

    for ch in all_channels[:50]:
        ch_id = ch["id"]
        ch_name = ch.get("name") or ch.get("name_normalized", "")
        ch_type = ch.get("_type", "")
        is_im = ch_type == "im" or ch.get("is_im", False)
        is_mpim = ch_type == "mpim" or ch.get("is_mpim", False)

        if is_im:
            im_user = ch.get("user", "")
            ch_name = f"DM with {_resolve_user_name(im_user, name_cache)}" if im_user else "DM"
        if is_mpim and not ch_name:
            ch_name = ch.get("name") or "Group DM"

        try:
            history = api_client.conversations_history(
                channel=ch_id, oldest=str(oldest_ts), limit=200,
            )
        except Exception as e:
            logger.warning("ease-my-life: history error %s (%s): %s", ch_name, ch_id, e)
            continue

        if not history.get("ok", True):
            logger.warning("ease-my-life: API error %s (%s): %s", ch_name, ch_id, history.get("error"))
            continue

        msg_list = history.get("messages", [])
        logger.debug("ease-my-life: [%s] %s → %d messages", ch_type, ch_name, len(msg_list))

        for msg in msg_list[:200]:
            raw_text = msg.get("text", "")
            if not raw_text:
                continue
            # Resolve <@USERID> mentions to real names
            resolved_text = _resolve_mentions_in_text(raw_text, name_cache)
            truncated = resolved_text[:200]
            sender_id = msg.get("user", "")
            sender = _resolve_user_name(sender_id, name_cache) if sender_id else "unknown"
            entry = {
                "channel": ch_name,
                "channel_id": ch_id,
                "text": truncated,
                "user": sender,
                "ts": msg.get("ts", ""),
            }

            if is_im or is_mpim or f"<@{user_id}>" in raw_text:
                mentions.append(entry)
            else:
                messages.append(entry)

    logger.debug(
        "ease-my-life: collected %d high-priority, %d normal messages",
        len(mentions), len(messages),
    )
    return {"mentions": mentions, "messages": messages}


def _build_slack_search_query(filters: dict) -> str:
    """Turn parsed LLM filters into a Slack search query string."""
    parts = []
    kw = filters.get("keywords", "").strip()
    if kw:
        parts.append(kw)
    sender = filters.get("sender", "").strip()
    if sender:
        parts.append(f"from:{sender}")
    channel = filters.get("channel", "").strip()
    if channel:
        parts.append(f"in:{channel}")
    date_from = filters.get("date_from", "").strip()
    if date_from:
        parts.append(f"after:{date_from}")
    date_to = filters.get("date_to", "").strip()
    if date_to:
        parts.append(f"before:{date_to}")
    return " ".join(parts) if parts else ""


def _execute_slack_search(query: str, filters: dict, client) -> tuple[list, list]:
    """Run search.messages and optionally search.files via the user token.

    Returns (messages_list, files_list).
    """
    token = settings.slack_user_token
    if not token:
        raise RuntimeError(
            "Slack search requires a *user token* (`SLACK_USER_TOKEN` / `xoxp-`).\n"
            "Ask your workspace admin to set the `SLACK_USER_TOKEN` environment variable."
        )

    search_client = WebClient(token=token)
    messages: list = []
    files: list = []
    file_type = filters.get("file_type", "").strip()

    try:
        if file_type:
            # primary: file search
            resp = search_client.search_files(query=query, count=10, sort="timestamp")
            files = (resp.data.get("files") or {}).get("matches", [])
        else:
            resp = search_client.search_messages(query=query, count=10, sort="timestamp")
            messages = (resp.data.get("messages") or {}).get("matches", [])
    except Exception:
        logger.exception("Slack search API error")
        raise

    return messages, files


def _format_search_results(messages: list, files: list, user_id: str) -> str:
    """Format Slack search results into a readable Slack message."""
    sections: list[str] = []

    if messages:
        lines = [f"*Messages ({len(messages)} result{'s' if len(messages) != 1 else ''}):*\n"]
        for i, m in enumerate(messages[:10], 1):
            ch = m.get("channel", {}).get("name", "")
            user = m.get("username", "unknown")
            ts = m.get("ts", "")
            text = m.get("text", "")[:200]
            permalink = m.get("permalink", "")
            try:
                dt_str = _to_user_time(user_id, float(ts)).strftime("%b %d, %I:%M %p")
            except (ValueError, OSError, TypeError):
                dt_str = ""
            link = f"<{permalink}|View>" if permalink else ""
            lines.append(f"{i}. *#{ch}* — {user} ({dt_str})\n   {text}\n   {link}")
        sections.append("\n".join(lines))

    if files:
        lines = [f"*Files ({len(files)} result{'s' if len(files) != 1 else ''}):*\n"]
        for i, f in enumerate(files[:10], 1):
            name = f.get("name", "unknown")
            ftype = f.get("filetype", "")
            user = f.get("username", "unknown")
            permalink = f.get("permalink", "")
            channels = ", ".join(f"#{c}" for c in (f.get("channels", []) or []))
            link = f"<{permalink}|Open>" if permalink else ""
            lines.append(f"{i}. `{name}` ({ftype}) — shared by {user} in {channels or 'DM'}\n   {link}")
        sections.append("\n".join(lines))

    if not sections:
        return "No results found. Try different keywords or broader date ranges."

    return "\n\n".join(sections)


def _register_handlers(app: App):

    # ── /oloid-setup — admin configures Google OAuth and LLM ──

    @app.command("/oloid-setup")
    def handle_setup(ack, command, respond):
        ack()
        text = command.get("text", "").strip()

        if not text:
            google_ok = user_store.is_google_configured()
            g = user_store.get_admin_google()

            respond(
                f"*OloidBot Setup*\n\n"
                f"*1. Google OAuth:* {'`Connected` (' + g['client_id'][:20] + '...)' if google_ok else '`Not configured`'}\n"
                f"   `/oloid-setup google <client_id> <client_secret>`\n\n"
                f"*2. Each user sets their own LLM:*\n"
                f"   `/oloid-set-llm <provider> <api_key> [model]`\n\n"
                f"*3. Each user connects Gmail:*\n"
                f"   `/oloid-connect-gmail` — link Gmail account\n\n"
                f"_After setup, users can `/oloid-summarize`, `/oloid-emails`, `/oloid-ask`, or DM the bot._"
            )
            return

        parts = text.split()
        subcommand = parts[0].lower()

        if subcommand == "google" and len(parts) >= 3:
            client_id = parts[1]
            client_secret = parts[2]
            user_store.set_admin_google(client_id, client_secret)
            respond(
                f"Google OAuth configured! (Client ID: `{client_id[:20]}...`)\n"
                f"Team members can now use `/oloid-connect-gmail` to link their Gmail.\n\n"
                f"_Make sure `{settings.effective_oauth_redirect_uri}` is added as an authorized redirect URI in Google Cloud Console._"
            )

        else:
            respond(
                "*Usage:*\n"
                "• `/oloid-setup` — show current setup status\n"
                "• `/oloid-setup google <client_id> <client_secret>` — configure Google OAuth\n\n"
                "_Each user configures their own LLM with `/oloid-set-llm <provider> <api_key> [model]`_"
            )

    # ── /oloid-connect-gmail ──

    @app.command("/oloid-connect-gmail")
    def handle_connect_gmail(ack, command, respond):
        ack()
        user_id = command["user_id"]

        if user_store.is_gmail_connected(user_id):
            respond(
                "Your Gmail is already connected!\n"
                "To reconnect: `/oloid-disconnect-gmail` then `/oloid-connect-gmail` again."
            )
            return

        if not user_store.is_google_configured():
            respond(
                "Google OAuth hasn't been set up yet.\n"
                "An admin needs to run: `/oloid-setup google <client_id> <client_secret>`"
            )
            return

        try:
            from .oauth_server import build_oauth_url
            url = build_oauth_url(user_id)
            respond(
                f"*Connect your Gmail account:*\n\n"
                f"1. <{url}|Click here to authorize Gmail access>\n"
                f"2. Sign in with your Google account\n"
                f"3. Allow OloidBot to read your emails\n"
                f"4. You'll see a success page — return to Slack!\n\n"
                f"_Your credentials are encrypted and only you can access your emails._"
            )
        except Exception as e:
            logger.exception("Error generating OAuth URL")
            respond(f"Error: {e}")

    # ── /oloid-disconnect-gmail ──

    @app.command("/oloid-disconnect-gmail")
    def handle_disconnect_gmail(ack, command, respond):
        ack()
        user_store.disconnect_gmail(command["user_id"])
        respond("Gmail disconnected. Use `/oloid-connect-gmail` to reconnect anytime.")

    # ── /oloid-set-llm — per-user LLM override ──

    @app.command("/oloid-set-llm")
    def handle_set_llm(ack, command, respond):
        ack()
        user_id = command["user_id"]
        text = command.get("text", "").strip()

        if not text:
            from .llm import LLMFactory
            providers = ", ".join(LLMFactory.available_providers())
            respond(
                f"*Configure your LLM provider:*\n\n"
                f"Usage: `/oloid-set-llm <provider> <api_key> [model]`\n\n"
                f"Providers: `{providers}`\n\n"
                f"Examples:\n"
                f"• `/oloid-set-llm openai sk-abc123 gpt-4o`\n"
                f"• `/oloid-set-llm anthropic sk-ant-abc123`\n"
                f"• `/oloid-set-llm gemini AIza... gemini-1.5-flash`\n"
                f"• `/oloid-set-llm ollama none llama3.1`\n\n"
                f"_Only you can see this message. Your API key is stored encrypted._"
            )
            return

        parts = text.split()
        provider = parts[0].lower()
        api_key = parts[1] if len(parts) > 1 else ""
        model = parts[2] if len(parts) > 2 else ""

        from .llm import LLMFactory
        available = LLMFactory.available_providers()
        if provider not in available:
            respond(f"Unknown provider `{provider}`. Available: {', '.join(available)}")
            return

        user_store.set_llm_config(user_id, provider=provider, api_key=api_key, model=model)
        display_model = model or get_default_model(provider)
        respond(f"Your LLM set to `{provider}` / `{display_model}`")

    # ── /oloid-my-memory ──

    @app.command("/oloid-my-memory")
    def handle_my_memory(ack, command, respond):
        ack()
        from . import memory as mem

        user_id = command["user_id"]
        text = command.get("text", "").strip()

        # ── list (no args) ──
        if not text or text.lower() in ("list", "show"):
            entries = mem.get_memories_with_ts(user_id, limit=30)
            channel_id = mem.get_user_channel_id(user_id)
            channel_link = f"<#{channel_id}>" if channel_id else "_unavailable_"

            if not entries:
                respond(
                    f"*Your Oloid Memory*\n\n"
                    f"No memories yet. The bot learns from your interactions automatically.\n"
                    f"You can also add your own: `/oloid-my-memory add I prefer bullet points`\n\n"
                    f"_Your memory channel: {channel_link}_"
                )
                return

            lines = []
            for i, (_, text_) in enumerate(entries, 1):
                tag = " _(manual)_" if text_.startswith(mem.MANUAL_PREFIX) else ""
                clean = text_[len(mem.MANUAL_PREFIX):].strip() if text_.startswith(mem.MANUAL_PREFIX) else text_
                lines.append(f"{i}. {clean}{tag}")

            respond(
                f"*Your Oloid Memory* ({len(entries)} entries)\n\n"
                + "\n".join(lines)
                + f"\n\n_To delete: `/oloid-my-memory delete <number>`_\n"
                f"_To add: `/oloid-my-memory add <text>`_\n"
                f"_To clear all: `/oloid-my-memory clear`_\n"
                f"_Your memory channel: {channel_link}_"
            )
            return

        parts = text.split(None, 1)
        sub = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        # ── add ──
        if sub == "add":
            if not arg:
                respond("Usage: `/oloid-my-memory add <text>`\nExample: `/oloid-my-memory add I prefer concise bullet points`")
                return
            mem.save_manual_memory(user_id, arg)
            respond(f"Memory saved: _{arg}_\n\nThe bot will use this to personalize future responses.")
            return

        # ── delete ──
        if sub == "delete":
            if not arg.isdigit():
                respond("Usage: `/oloid-my-memory delete <number>`\nRun `/oloid-my-memory` to see the numbered list.")
                return
            idx = int(arg)
            entries = mem.get_memories_with_ts(user_id, limit=50)
            if idx < 1 or idx > len(entries):
                respond(f"No memory #{idx}. You have {len(entries)} memories. Run `/oloid-my-memory` to see them.")
                return
            _, deleted_text = entries[idx - 1]
            clean = deleted_text[len(mem.MANUAL_PREFIX):].strip() if deleted_text.startswith(mem.MANUAL_PREFIX) else deleted_text
            if mem.delete_memory(user_id, idx):
                respond(f"Deleted memory #{idx}: _{clean}_")
            else:
                respond(f"Could not delete memory #{idx}. Please try again.")
            return

        # ── clear ──
        if sub == "clear":
            count = mem.clear_memories(user_id)
            respond(f"Cleared {count} memories. Starting fresh.")
            return

        respond(
            "*Usage:*\n"
            "• `/oloid-my-memory` — view all memories\n"
            "• `/oloid-my-memory add <text>` — add a manual memory\n"
            "• `/oloid-my-memory delete <number>` — delete by number\n"
            "• `/oloid-my-memory clear` — clear all memories"
        )

    # ── /oloid-summarize ──

    @app.command("/oloid-summarize")
    def handle_summarize(ack, command, respond):
        ack()
        user_id = command["user_id"]

        if not user_store.is_gmail_connected(user_id):
            respond("Connect your Gmail first with `/oloid-connect-gmail`")
            return

        text = re.sub(r"[^0-9]", "", command.get("text", "").strip())
        n = int(text) if text else 10
        respond(f"Summarizing your last {n} emails...")
        try:
            result = _run_async(_get_agent().summarize_emails(user_id, n))
            respond(result)
        except Exception as e:
            logger.exception("Error in /oloid-summarize")
            respond(f"Error: {e}")

    # ── /oloid-emails ──

    @app.command("/oloid-emails")
    def handle_emails(ack, command, respond):
        ack()
        user_id = command["user_id"]

        if not user_store.is_gmail_connected(user_id):
            respond("Connect your Gmail first with `/oloid-connect-gmail`")
            return

        text = re.sub(r"[^0-9]", "", command.get("text", "").strip())
        n = int(text) if text else 10
        try:
            from .gmail_client import GmailClient
            from .agent import _format_email_list
            gmail = GmailClient(user_id)
            emails = gmail.fetch_emails(max_results=n)
            if not emails:
                respond("No emails found.")
                return
            respond(_format_email_list(emails))
        except Exception as e:
            logger.exception("Error in /oloid-emails")
            respond(f"Error: {e}")

    # ── /oloid-ask — general AI chat (no Gmail needed) ──

    @app.command("/oloid-ask")
    def handle_ask(ack, command, respond):
        ack()
        user_id = command["user_id"]
        text = command.get("text", "").strip()

        if not text:
            respond(
                "*Ask Oloid anything!*\n\n"
                "Usage: `/oloid-ask <your question>`\n\n"
                "Examples:\n"
                "• `/oloid-ask What is quantum computing?`\n"
                "• `/oloid-ask Write a Python function to sort a list`\n"
                "• `/oloid-ask Explain REST APIs in simple terms`\n\n"
                "_This uses your configured LLM provider. No Gmail needed._"
            )
            return

        try:
            result = _run_async(_get_agent().general_chat(user_id, text))
            respond(result)
        except Exception as e:
            logger.exception("Error in /oloid-ask")
            respond(f"Error: {e}")

    # ── /oloid-notifications — toggle real-time email alerts ──

    @app.command("/oloid-notifications")
    def handle_notifications(ack, command, respond):
        ack()
        user_id = command["user_id"]
        text = command.get("text", "").strip().lower()

        if text == "on":
            if not user_store.is_gmail_connected(user_id):
                respond("Connect your Gmail first with `/oloid-connect-gmail`")
                return
            user_store.set_notifications(user_id, True)
            respond(f"Email notifications *enabled*. You'll get DMs when new emails arrive (checking every {settings.poll_interval_minutes} min).")
        elif text == "off":
            user_store.set_notifications(user_id, False)
            respond("Email notifications *disabled*. Use `/oloid-notifications on` to re-enable.")
        else:
            current = user_store.get_notifications(user_id)
            respond(
                f"*Email Notifications:* `{'ON' if current else 'OFF'}`\n\n"
                f"• `/oloid-notifications on` — get DMs when new emails arrive\n"
                f"• `/oloid-notifications off` — disable automatic alerts\n\n"
                f"_Checking every {settings.poll_interval_minutes} minutes._"
            )

    # ── /oloid-mysettings ──

    @app.command("/oloid-mysettings")
    def handle_settings(ack, command, respond):
        ack()
        user_id = command["user_id"]
        gmail_status = "Connected" if user_store.is_gmail_connected(user_id) else "Not connected"
        notif_status = "ON" if user_store.get_notifications(user_id) else "OFF"
        user_llm = user_store.get_llm_config(user_id)

        if user_llm["provider"]:
            llm_display = f"`{user_llm['provider']}` / `{user_llm['model'] or 'default'}`"
        else:
            llm_display = "`Not configured` — run `/oloid-set-llm <provider> <api_key>`"

        respond(
            f"*Your Settings*\n\n"
            f"• Gmail: `{gmail_status}`\n"
            f"• LLM: {llm_display}\n"
            f"• Notifications: `{notif_status}` (every {settings.poll_interval_minutes} min)\n\n"
            f"*Commands:*\n"
            f"`/oloid-connect-gmail` · `/oloid-disconnect-gmail` · `/oloid-set-llm`\n"
            f"`/oloid-summarize` · `/oloid-emails` · `/oloid-ask` · `/oloid-notifications`"
        )

    # ── /oloid-remind — set a reminder ──

    @app.command("/oloid-remind")
    def handle_remind(ack, command, respond):
        ack()
        user_id = command["user_id"]
        text = command.get("text", "").strip()

        if not text:
            respond(
                "*Set a reminder:*\n\n"
                "Usage: `/oloid-remind [message] in [time]`\n\n"
                "Examples:\n"
                "• `/oloid-remind Buy groceries in 30m`\n"
                "• `/oloid-remind Team standup in 2h`\n"
                "• `/oloid-remind Submit report in 1d`\n"
                "• `/oloid-remind Check deployment in 2h30m`\n\n"
                "_Supported: `Nm/min`, `Nh/hr`, `Nd`, or `N minutes/hours/days`_"
            )
            return

        parsed = _parse_reminder(text)
        if parsed is None:
            respond(
                "Couldn't parse that reminder.\n"
                "Use: `/oloid-remind [message] in [time]`\n"
                "e.g. `/oloid-remind Buy groceries in 30m`"
            )
            return

        message, delta = parsed
        fire_at = time.time() + delta.total_seconds()
        fire_dt = _to_user_time(user_id, fire_at)

        from .scheduler import schedule_reminder
        schedule_reminder(user_id, message, fire_at)

        time_display = fire_dt.strftime("%I:%M %p").lstrip("0")
        respond(f"I'll remind you: *{message}* at {time_display}")

    # ── /oloid-reminders — list/cancel pending reminders ──

    @app.command("/oloid-reminders")
    def handle_reminders(ack, command, respond):
        ack()
        user_id = command["user_id"]
        text = command.get("text", "").strip()

        # Cancel a reminder: /oloid-reminders cancel r_123456
        if text.startswith("cancel "):
            reminder_id = text.split(" ", 1)[1].strip()
            from .scheduler import cancel_reminder
            cancel_reminder(user_id, reminder_id)
            respond(f"Reminder `{reminder_id}` cancelled.")
            return

        reminders = user_store.get_user_reminders(user_id)
        if not reminders:
            respond("You have no pending reminders.\nUse `/oloid-remind [message] in [time]` to set one.")
            return

        lines = ["*Your pending reminders:*\n"]
        for r in sorted(reminders, key=lambda x: x["fire_at"]):
            fire_dt = _to_user_time(user_id, r["fire_at"])
            time_str = fire_dt.strftime("%b %d, %I:%M %p").replace(" 0", " ")
            lines.append(f"• *{r['text']}* — {time_str}  (`{r['id']}`)")

        lines.append("\n_To cancel: `/oloid-reminders cancel <id>`_")
        respond("\n".join(lines))

    # ── /oloid-focus — deep work timer with break reminder ──

    @app.command("/oloid-focus")
    def handle_focus(ack, command, respond):
        ack()
        user_id = command["user_id"]
        text = command.get("text", "").strip().lower()

        # Parse time: "25m", "90m", "25", "1h", "1.5h". Default 25m.
        minutes = 25
        if text:
            m = re.match(r"^(\d+(?:\.\d+)?)\s*(m|min|h|hr)?$", text)
            if m:
                val = float(m.group(1))
                unit = (m.group(2) or "m")[0]
                if unit == "h":
                    minutes = int(val * 60)
                else:
                    minutes = int(val)

        if minutes < 1:
            minutes = 1
        if minutes > 480:
            minutes = 480

        # Schedule a break reminder DM when the focus session ends
        break_at = time.time() + minutes * 60
        from .scheduler import schedule_reminder
        reminder_id = schedule_reminder(
            user_id,
            f"Your {minutes}-minute focus session is done! Time to take a break. Stand up, stretch, grab some water.",
            break_at,
        )

        # Build the timer URL using the configured base URL
        timer_url = f"{settings.effective_base_url}/focus?m={minutes}&autostart=1"

        # Calculate break reminder time display in user's timezone
        break_dt = _to_user_time(user_id, break_at)
        break_time = break_dt.strftime("%I:%M %p").lstrip("0")

        # On production, just send the URL; locally, also open a browser window
        import os as _os
        if _os.environ.get("RENDER"):
            respond(
                f"*Oloid Focus — {minutes} min deep work session*\n\n"
                f":dart: <{timer_url}|Open your focus timer>\n\n"
                f"I'll DM you a break reminder at *{break_time}*.\n\n"
                f"_Tips: Close distracting tabs, silence notifications, and focus on one task._"
            )
        else:
            import threading as _threading
            _threading.Thread(
                target=_open_focus_window,
                args=(timer_url, minutes, user_id, reminder_id),
                daemon=True,
            ).start()

            respond(
                f"*Oloid Focus — {minutes} min deep work session started!*\n\n"
                f":dart: Timer opened in a new window.\n\n"
                f"I'll DM you a break reminder at *{break_time}*.\n\n"
                f"_Tips: Close distracting tabs, silence notifications, and focus on one task._"
            )

    # ── /oloid-format-this — summarize current channel/DM conversation ──

    @app.command("/oloid-format-this")
    def handle_format_this(ack, command, respond, client):
        ack()
        user_id = command["user_id"]
        channel_id = command["channel_id"]
        text = command.get("text", "").strip().lower()

        # Parse time: "7d", "3d", "24h", "2h", or just "7" (days). Default 7d.
        days = 7
        hours = 0
        if text:
            m = re.match(r"^(\d+)\s*(d|h)$", text)
            if m:
                val = int(m.group(1))
                unit = m.group(2)
                if unit == "d":
                    days = val
                elif unit == "h":
                    hours = val
                    days = 0
            elif text.isdigit():
                days = int(text)

        total_hours = days * 24 + hours
        if total_hours <= 0:
            total_hours = 168  # default 7 days
        time_label = f"{days}d" if days and not hours else f"{hours}h" if hours else f"{days}d"

        respond(f"Formatting the last {time_label} of this conversation... One moment.")

        try:
            oldest_ts = str(int(time.time() - total_hours * 3600))
            name_cache: dict = {}

            # Build list of clients to try: user token first (broader access), then bot token
            clients_to_try = []
            if settings.slack_user_token:
                clients_to_try.append(("user_token", WebClient(token=settings.slack_user_token)))
            clients_to_try.append(("bot_token", client))

            # Resolve channel name
            ch_name = channel_id
            for label, try_client in clients_to_try:
                try:
                    ch_resp = try_client.conversations_info(channel=channel_id)
                    ch_data = ch_resp.data.get("channel", {}) if hasattr(ch_resp, "data") else ch_resp.get("channel", {})
                    if ch_data:
                        raw_name = ch_data.get("name") or ch_data.get("name_normalized") or channel_id
                        if ch_data.get("is_im"):
                            other_user = ch_data.get("user", "")
                            ch_name = f"DM with {_resolve_user_name(other_user, name_cache)}" if other_user else "DM"
                        elif ch_data.get("is_mpim"):
                            ch_name = ch_data.get("name") or "Group DM"
                        else:
                            ch_name = f"#{raw_name}"
                        break
                except Exception as e:
                    logger.debug("format-this: conversations_info failed with %s: %s", label, e)

            logger.debug("format-this: channel=%s (%s), oldest=%s, time=%s", ch_name, channel_id, oldest_ts, time_label)

            # Fetch messages — try each client until one works
            all_messages = []
            for label, try_client in clients_to_try:
                try:
                    history = try_client.conversations_history(
                        channel=channel_id, oldest=oldest_ts, limit=200,
                    )
                    raw_data = history.data if hasattr(history, "data") else history
                    logger.info(
                        "format-this: [%s] ok=%s, msg_count=%s, has_more=%s",
                        label,
                        raw_data.get("ok"),
                        len(raw_data.get("messages", [])),
                        raw_data.get("has_more"),
                    )
                    batch = raw_data.get("messages", [])
                    if batch:
                        logger.debug("format-this: first msg preview: %s", str(batch[0])[:200])
                    all_messages.extend(batch)

                    # Paginate if needed
                    while True:
                        next_cursor = (raw_data.get("response_metadata") or {}).get("next_cursor")
                        if not next_cursor or len(all_messages) >= 1000:
                            break
                        history = try_client.conversations_history(
                            channel=channel_id, oldest=oldest_ts, limit=200, cursor=next_cursor,
                        )
                        raw_data = history.data if hasattr(history, "data") else history
                        all_messages.extend(raw_data.get("messages", []))

                    if all_messages:
                        logger.debug("format-this: fetched %d total messages via %s", len(all_messages), label)
                        break
                    else:
                        logger.debug("format-this: 0 messages via %s, trying next token", label)
                except Exception as e:
                    logger.warning("format-this: history failed with %s for %s: %s", label, channel_id, e)
                    all_messages = []

            if not all_messages:
                respond(
                    f"No messages found in the last {time_label}.\n\n"
                    "_Make sure OloidBot is added to this channel, or check that `SLACK_USER_TOKEN` has the right scopes._"
                )
                return

            # Format messages with resolved names, oldest first
            all_messages.reverse()
            lines = []
            for msg in all_messages[:500]:
                raw_text = msg.get("text", "")
                if not raw_text:
                    continue
                sender_id = msg.get("user", "")
                sender = _resolve_user_name(sender_id, name_cache) if sender_id else "unknown"
                resolved_text = _resolve_mentions_in_text(raw_text, name_cache)
                ts = msg.get("ts", "")
                try:
                    dt_str = _to_user_time(user_id, float(ts)).strftime("%b %d, %I:%M %p")
                except (ValueError, OSError):
                    dt_str = ""
                lines.append(f"[{dt_str}] {sender}: {resolved_text[:300]}")

            conversation_text = "\n".join(lines)
            if len(conversation_text) > 15000:
                conversation_text = conversation_text[:15000] + "\n...(truncated)"

            system_prompt = (
                "You are a conversation summarizer. The user wants a structured summary "
                f"of a Slack conversation from {ch_name} over the last {time_label}.\n\n"
                "Create a well-organized summary with:\n"
                "**📋 Key Topics Discussed** — Group messages by topic/thread\n"
                "**✅ Decisions Made** — Any conclusions or agreements reached\n"
                "**📌 Action Items** — Tasks or follow-ups mentioned (tag who is responsible)\n"
                "**💡 Key Highlights** — Important links, files, or announcements shared\n\n"
                "Use real names, keep it scannable. Attribute statements to the people who said them."
            )

            from .agent import _get_llm_for_user
            llm = _get_llm_for_user(user_id)
            result = _run_async(
                llm.chat(
                    [{"role": "user", "content": f"Here is the conversation:\n\n{conversation_text}"}],
                    system=system_prompt,
                )
            )
            respond(result)
        except Exception as e:
            logger.exception("Error in /oloid-format-this")
            respond(f"Error: {e}")

    # ── /oloid-ease-my-life — priority-ranked briefing ──

    @app.command("/oloid-ease-my-life")
    def handle_ease_my_life(ack, command, respond, client):
        ack()
        user_id = command["user_id"]
        text = re.sub(r"[^0-9]", "", command.get("text", "").strip())
        days = int(text) if text else 7

        respond(f"Processing your {days}-day briefing... This may take a moment.")
        try:
            slack_data = _fetch_slack_summary(client, user_id, days)
            result = _run_async(
                _get_agent().ease_my_life(user_id, days, slack_data)
            )
            respond(result)
        except Exception as e:
            logger.exception("Error in /oloid-ease-my-life")
            respond(f"Error: {e}")

    # ── /oloid-find — natural language Slack search ──

    @app.command("/oloid-find")
    def handle_find(ack, command, respond, client):
        ack()
        user_id = command["user_id"]
        text = command.get("text", "").strip()

        if not text:
            respond(
                "*Search Slack with natural language:*\n\n"
                "Usage: `/oloid-find <scope> <query>`\n\n"
                "*Scopes:*\n"
                "• `@username` — search in your DMs with that person\n"
                "• `#channel` — search in a specific channel/group\n"
                "• `all` — search across all of Slack\n\n"
                "*Examples:*\n"
                "• `/oloid-find @anshul pg admin server credentials`\n"
                "• `/oloid-find #engineering kafka memory leak`\n"
                "• `/oloid-find all pdf shared last week`\n"
            )
            return

        # ── Parse scope: @user, #channel, or all ──
        scope = ""
        scope_label = "everywhere"
        query_text = text

        # Slack encodes @user as <@U12345> and #channel as <#C12345|name>
        user_match = re.match(r"<@([A-Z0-9]+)(?:\|[^>]*)?>\s*(.*)", text)
        channel_match = re.match(r"<#([A-Z0-9]+)\|([^>]*)>\s*(.*)", text)

        if user_match:
            target_uid = user_match.group(1)
            query_text = user_match.group(2).strip()
            name_cache: dict = {}
            target_name = _resolve_user_name(target_uid, name_cache)
            # Open/find the DM channel with this user to get the real channel ID
            try:
                dm_resp = client.conversations_open(users=[target_uid])
                dm_channel_id = dm_resp.data["channel"]["id"]
                scope = f"in:{dm_channel_id}"
            except Exception as e:
                logger.warning("Could not open DM with %s: %s", target_uid, e)
                scope = f"from:{target_name}"
            scope_label = f"DMs with {target_name}"
        elif channel_match:
            channel_id_match = channel_match.group(1)
            channel_name = channel_match.group(2)
            query_text = channel_match.group(3).strip()
            scope = f"in:{channel_id_match}"
            scope_label = f"#{channel_name}"
        elif text.lower().startswith("all "):
            query_text = text[4:].strip()

        if not query_text:
            respond("Please provide a search query after the scope.\nExample: `/oloid-find @anshul server credentials`")
            return

        respond(f":mag: Searching {scope_label}...")

        try:
            filters = _run_async(_get_agent().parse_search_query(user_id, query_text))

            # Scope already handles user/channel filtering, clear LLM duplicates
            if user_match:
                filters["sender"] = ""
                filters["channel"] = ""
            elif channel_match:
                filters["channel"] = ""

            query_string = _build_slack_search_query(filters)
            if scope:
                query_string = f"{scope} {query_string}".strip() if query_string else scope

            if not query_string:
                respond("Couldn't build a search query from your input. Try rephrasing.")
                return

            file_type = filters.get("file_type", "").strip()
            search_type = "files" if file_type else "messages"

            messages, files = _execute_slack_search(query_string, filters, client)
            result = _format_search_results(messages, files, user_id)

            header = f"_Searched {search_type} in {scope_label}:_ `{query_string}`\n\n"
            respond(header + result)

        except RuntimeError as e:
            respond(str(e))
        except Exception as e:
            logger.exception("Error in /oloid-find")
            respond(f"Search failed: {e}")

    # ── /oloid-commands — list all available commands ──

    @app.command("/oloid-commands")
    def handle_commands(ack, command, respond):
        ack()

        respond(
            "*Oloid – Available Commands*\n\n"

            "*Admin Setup*\n"
            "• `/oloid-setup` — View setup status\n"
            "• `/oloid-setup google <client_id> <client_secret>` — Configure Google OAuth\n\n"

            "*User Setup*\n"
            "• `/oloid-set-llm <provider> <api_key> [model]` — Set your LLM (required)\n"
            "• `/oloid-connect-gmail` — Connect your Gmail account\n"
            "• `/oloid-disconnect-gmail` — Disconnect Gmail\n\n"

            "*Email Features*\n"
            "• `/oloid-summarize [n]` — Summarize last n emails (default 10)\n"
            "• `/oloid-emails [n]` — List last n emails\n"
            "• `/oloid-notifications on|off` — Toggle email alerts\n\n"

            "*AI Chat*\n"
            "• `/oloid-ask <question>` — Ask anything (no Gmail needed)\n"
            "• `/oloid-find <@user/#channel/all> <query>` — Search Slack with natural language\n"
            "• DM the bot — Chat directly\n"
            "• @Mention the bot — Ask in a channel\n\n"

            "*Briefing & Summaries*\n"
            "• `/oloid-ease-my-life [days]` — Priority-ranked briefing of Slack, emails & reminders (default 7 days)\n"
            "• `/oloid-format-this [time]` — Summarize this channel/DM conversation (e.g. `7d`, `24h`, default 7d)\n\n"

            "*Focus & Productivity*\n"
            "• `/oloid-focus [time]` — Start a focus timer with break reminder (e.g. `25m`, `90m`, `1h`, default 25m)\n\n"

            "*Reminders*\n"
            "• `/oloid-remind [message] in [time]` — Set a reminder (DM at specified time)\n"
            "• `/oloid-reminders` — View pending reminders\n"
            "• `/oloid-reminders cancel <id>` — Cancel a reminder\n\n"

            "*Memory*\n"
            "• `/oloid-my-memory` — View your personalized memory\n"
            "• `/oloid-my-memory add <text>` — Add a manual preference (e.g. _I prefer bullet points_)\n"
            "• `/oloid-my-memory delete <number>` — Delete a memory by number\n"
            "• `/oloid-my-memory clear` — Clear all memories\n\n"

            "*Settings*\n"
            "• `/oloid-mysettings` — View your current configuration\n\n"

            "_Tip: Most commands work in DMs or channels. Gmail must be connected for email features._"
        )

    # ── DM messages ──

    @app.event("message")
    def handle_dm(event, say):
        if event.get("channel_type") != "im" or event.get("bot_id"):
            return

        user_id = event.get("user", "")
        text = event.get("text", "")
        if not text:
            return

        # If Gmail is connected, route through the email agent
        if user_store.is_gmail_connected(user_id):
            try:
                result = _run_async(_get_agent().handle_message(user_id, text))
                say(result)
            except Exception as e:
                logger.exception("Error handling DM")
                say(f"Sorry, something went wrong: {e}")
            return

        # No Gmail — try general AI chat if LLM is configured
        try:
            from .agent import _get_llm_for_user
            _get_llm_for_user(user_id)  # check if LLM is available
            result = _run_async(_get_agent().general_chat(user_id, text))
            say(result)
        except RuntimeError:
            # No LLM configured either — show onboarding
            say(
                "Hi! I'm *Oloid*, your AI assistant.\n\n"
                "*Get started:*\n"
                "1. `/oloid-set-llm <provider> <api_key>` — set your LLM (required)\n"
                "2. `/oloid-connect-gmail` — link your Gmail\n"
                "3. `/oloid-ask` — ask me anything\n\n"
                "_DM me or use slash commands anytime!_"
            )
        except Exception as e:
            logger.exception("Error handling DM")
            say(f"Sorry, something went wrong: {e}")

    # ── @mentions ──

    @app.event("app_mention")
    def handle_mention(event, say):
        user_id = event.get("user", "")
        text = event.get("text", "")
        text = text.split(">", 1)[-1].strip() if ">" in text else text

        if not text:
            say("Hi! DM me or try `/oloid-commands` to list all the available commands for OloidBot. Than you can use any `/oloid-[what_you_want]` to use privately and publicly.")
            return

        # If Gmail connected, use email agent; otherwise general chat
        if user_store.is_gmail_connected(user_id):
            try:
                result = _run_async(_get_agent().handle_message(user_id, text))
                say(result)
            except Exception as e:
                logger.exception("Error handling mention")
                say(f"Sorry, something went wrong: {e}")
        else:
            try:
                result = _run_async(_get_agent().general_chat(user_id, text))
                say(result)
            except Exception as e:
                logger.exception("Error handling mention")
                say(f"Sorry, something went wrong: {e}")


def start_bot():
    from .oauth_server import set_slack_client
    from . import memory
    from slack_sdk import WebClient

    bolt_app = _get_app()
    client = WebClient(token=settings.slack_bot_token)
    set_slack_client(client)
    memory.init(client)

    handler = SocketModeHandler(bolt_app, settings.slack_app_token)
    logger.info("Slack bot starting in Socket Mode...")
    handler.start()
