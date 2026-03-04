"""
User memory system — stores behavioral insights in a private Slack channel.
The bot creates a hidden private channel 'oloid-memory' that only it can see.
After each interaction, the LLM analyzes and decides what to remember.
"""

import logging
import threading

from slack_sdk import WebClient

from .config import settings
from .user_store import user_store

logger = logging.getLogger(__name__)

MEMORY_CHANNEL_NAME = "oloid-memory"

_client: WebClient = None
_channel_id: str = None
_lock = threading.Lock()


def init(client: WebClient):
    global _client
    _client = client


def _ensure_channel() -> str:
    """Create or find the private memory channel. Cache the ID."""
    global _channel_id
    if _channel_id:
        return _channel_id

    with _lock:
        if _channel_id:
            return _channel_id

        # Check if channel_id is stored from a previous run
        stored_id = user_store.get_memory_channel_id()
        if stored_id:
            _channel_id = stored_id
            return _channel_id

        # Try to create the private channel
        try:
            resp = _client.conversations_create(
                name=MEMORY_CHANNEL_NAME, is_private=True
            )
            _channel_id = resp["channel"]["id"]
            user_store.set_memory_channel_id(_channel_id)
            # Set channel topic for clarity
            _client.conversations_setTopic(
                channel=_channel_id,
                topic="Oloid user behavior memory — do not delete",
            )
            logger.info("Created memory channel: %s", _channel_id)
            return _channel_id
        except Exception as e:
            # Channel might already exist (name_taken error)
            if "name_taken" in str(e):
                return _find_existing_channel()
            logger.exception("Failed to create memory channel")
            raise


def _find_existing_channel() -> str:
    """Find the existing oloid-memory channel by listing private channels."""
    global _channel_id
    try:
        resp = _client.conversations_list(types="private_channel", limit=200)
        for ch in resp.get("channels", []):
            if ch["name"] == MEMORY_CHANNEL_NAME:
                _channel_id = ch["id"]
                user_store.set_memory_channel_id(_channel_id)
                return _channel_id
    except Exception:
        logger.exception("Failed to find memory channel")
    raise RuntimeError("Memory channel exists but bot can't find it")


def save_memory(user_id: str, note: str):
    """Post a memory note to the private channel, tagged with user_id."""
    if not _client or not note.strip():
        return
    try:
        channel = _ensure_channel()
        _client.chat_postMessage(
            channel=channel,
            text=f"[{user_id}] {note}",
        )
    except Exception:
        logger.exception("Failed to save memory for %s", user_id)


def get_memories(user_id: str, limit: int = 15) -> list[str]:
    """Read recent memory notes for a specific user from the channel."""
    if not _client:
        return []
    try:
        channel = _ensure_channel()
        resp = _client.conversations_history(channel=channel, limit=200)
        memories = []
        tag = f"[{user_id}]"
        for msg in resp.get("messages", []):
            text = msg.get("text", "")
            if text.startswith(tag):
                memories.append(text[len(tag):].strip())
                if len(memories) >= limit:
                    break
        return memories
    except Exception:
        logger.exception("Failed to read memories for %s", user_id)
        return []


def analyze_and_remember(user_id: str, user_message: str, bot_response: str):
    """Background task: ask LLM what to remember, then save it."""
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
