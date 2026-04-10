"""
User memory system — per-user private Slack channels.
Each user gets a private channel (oloid-mem-{user_id_lower}) shared only
with the bot and that user. After each interaction the LLM auto-saves
insights; users can also add/delete memories manually via /oloid-my-memory.
"""

import logging
import threading

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from .user_store import user_store

logger = logging.getLogger(__name__)

_client: WebClient = None
_user_channels: dict[str, str] = {}   # user_id -> channel_id (in-memory cache)
_lock = threading.Lock()

MANUAL_PREFIX = "[manual] "


def init(client: WebClient):
    global _client
    _client = client


# ── channel management ─────────────────────────────────────────────────────

def _channel_name_for(user_id: str) -> str:
    return f"oloid-mem-{user_id.lower()}"


def _get_or_create_user_channel(user_id: str) -> str:
    """Return the private memory channel ID for this user, creating it if needed."""
    if user_id in _user_channels:
        return _user_channels[user_id]

    with _lock:
        if user_id in _user_channels:
            return _user_channels[user_id]

        stored_id = user_store.get_user_memory_channel_id(user_id)
        if stored_id:
            _user_channels[user_id] = stored_id
            return stored_id

        channel_name = _channel_name_for(user_id)

        try:
            resp = _client.conversations_create(name=channel_name, is_private=True)
            channel_id = resp["channel"]["id"]
        except SlackApiError as e:
            if "name_taken" in str(e):
                channel_id = _find_channel_by_name(channel_name)
            else:
                raise

        # Invite the user so they can see their own memories
        try:
            _client.conversations_invite(channel=channel_id, users=user_id)
        except SlackApiError as e:
            if "already_in_channel" not in str(e):
                logger.warning("Could not invite %s to memory channel: %s", user_id, e)

        try:
            _client.conversations_setTopic(
                channel=channel_id,
                topic="Your Oloid memory — the bot reads this to personalize responses for you.",
            )
        except Exception:
            pass

        user_store.set_user_memory_channel_id(user_id, channel_id)
        _user_channels[user_id] = channel_id
        logger.info("Created memory channel %s for user %s", channel_id, user_id)
        return channel_id


def _find_channel_by_name(name: str) -> str:
    resp = _client.conversations_list(types="private_channel", limit=200)
    for ch in resp.get("channels", []):
        if ch["name"] == name:
            return ch["id"]
    raise RuntimeError(f"Memory channel '{name}' exists but bot can't find it")


def get_user_channel_id(user_id: str) -> str | None:
    """Return the channel ID for a user's memory channel, or None on failure."""
    try:
        return _get_or_create_user_channel(user_id)
    except Exception:
        return None


# ── read ───────────────────────────────────────────────────────────────────

def get_memories_with_ts(user_id: str, limit: int = 30) -> list[tuple[str, str]]:
    """Return list of (ts, text) tuples, newest-first. Skips system messages."""
    if not _client:
        return []
    try:
        channel = _get_or_create_user_channel(user_id)
        resp = _client.conversations_history(channel=channel, limit=limit)
        return [
            (msg["ts"], msg.get("text", ""))
            for msg in resp.get("messages", [])
            if not msg.get("subtype")   # skip channel_join, topic_change, etc.
        ]
    except Exception:
        logger.exception("Failed to read memories for %s", user_id)
        return []


def get_memories(user_id: str, limit: int = 15) -> list[str]:
    """Return memory texts for LLM injection (newest first)."""
    return [text for _, text in get_memories_with_ts(user_id, limit=limit)]


def get_split_memories(user_id: str, limit: int = 20) -> tuple[list[str], list[str]]:
    """Return (manual_memories, auto_memories) for differentiated LLM injection."""
    manual, auto = [], []
    for _, text in get_memories_with_ts(user_id, limit=limit):
        if text.startswith(MANUAL_PREFIX):
            manual.append(text[len(MANUAL_PREFIX):].strip())
        else:
            auto.append(text)
    return manual, auto


# ── write ──────────────────────────────────────────────────────────────────

def save_memory(user_id: str, note: str):
    """Post an auto-generated memory to the user's private channel."""
    if not _client or not note.strip():
        return
    try:
        channel = _get_or_create_user_channel(user_id)
        _client.chat_postMessage(channel=channel, text=note.strip())
    except Exception:
        logger.exception("Failed to save memory for %s", user_id)


def save_manual_memory(user_id: str, note: str):
    """Post a user-written memory prefixed with [manual]."""
    if not _client or not note.strip():
        return
    try:
        channel = _get_or_create_user_channel(user_id)
        _client.chat_postMessage(channel=channel, text=f"{MANUAL_PREFIX}{note.strip()}")
    except Exception:
        logger.exception("Failed to save manual memory for %s", user_id)


# ── delete ─────────────────────────────────────────────────────────────────

def delete_memory(user_id: str, index: int) -> bool:
    """Delete the Nth memory (1-based, newest-first order). Returns True on success."""
    if not _client:
        return False
    try:
        channel = _get_or_create_user_channel(user_id)
        entries = get_memories_with_ts(user_id, limit=50)
        if index < 1 or index > len(entries):
            return False
        ts = entries[index - 1][0]
        _client.chat_delete(channel=channel, ts=ts)
        return True
    except Exception:
        logger.exception("Failed to delete memory %d for %s", index, user_id)
        return False


def clear_memories(user_id: str) -> int:
    """Delete all memories for a user. Returns count deleted."""
    if not _client:
        return 0
    deleted = 0
    try:
        channel = _get_or_create_user_channel(user_id)
        entries = get_memories_with_ts(user_id, limit=100)
        for ts, _ in entries:
            try:
                _client.chat_delete(channel=channel, ts=ts)
                deleted += 1
            except Exception:
                pass
    except Exception:
        logger.exception("Failed to clear memories for %s", user_id)
    return deleted


# ── auto-memory (background LLM analysis) ─────────────────────────────────

def analyze_and_remember(user_id: str, user_message: str, bot_response: str):
    """Background task: ask LLM what to remember, then auto-save it."""
    import asyncio
    from .agent import _get_llm_for_user
    from .llm.prompts import MEMORY_ANALYSIS_PROMPT

    def _run():
        try:
            llm = _get_llm_for_user(user_id)
            existing = get_memories(user_id, limit=10)
            existing_text = "\n".join(f"- {m}" for m in existing) if existing else "None yet."

            prompt = (
                f"Existing memories about this user:\n{existing_text}\n\n"
                f"User said: {user_message}\n\n"
                f"Bot replied: {bot_response[:500]}\n\n"
                "Based on this interaction, what (if anything) is worth remembering "
                "about this user's preferences, behavior, or interests? "
                "Reply with a single short sentence, or reply NOTHING if there's "
                "nothing new worth remembering."
            )

            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(
                llm.chat(
                    [{"role": "user", "content": prompt}],
                    system=MEMORY_ANALYSIS_PROMPT,
                )
            )
            loop.close()

            result = result.strip()
            if result and "NOTHING" not in result.upper():
                save_memory(user_id, result)

        except Exception:
            logger.debug("Memory analysis skipped for %s", user_id)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
