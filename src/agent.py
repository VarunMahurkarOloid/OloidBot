import re
from collections import defaultdict

from .config import get_default_model, settings
from .gmail_client import Email, GmailClient
from .llm import LLMFactory
from .llm.prompts import (
    build_chat_prompt_with_memory,
    build_email_prompt_with_memory,
)
from .user_store import user_store


def _format_email_for_llm(email: Email) -> str:
    body_preview = email.body[:500] if email.body else email.snippet
    return (
        f"From: {email.sender}\n"
        f"Subject: {email.subject}\n"
        f"Date: {email.date}\n"
        f"Body: {body_preview}\n"
        f"Link: {email.gmail_url}\n"
    )


def _format_email_list(emails: list[Email]) -> str:
    lines = []
    for i, e in enumerate(emails, 1):
        lines.append(
            f"*{i}. {e.subject}*\n"
            f"   From: {e.sender} | {e.date}\n"
            f"   <{e.gmail_url}|Open in Gmail>"
        )
    return "\n\n".join(lines)


def _get_llm_for_user(slack_user_id: str):
    user_cfg = user_store.get_llm_config(slack_user_id)

    provider = user_cfg["provider"]
    api_key = user_cfg["api_key"]

    if not provider or not api_key:
        raise RuntimeError(
            "You haven't set up your LLM yet.\n"
            "Run `/oloid-set-llm <provider> <api_key> [model]` to get started.\n\n"
            "Examples:\n"
            "• `/oloid-set-llm openai sk-... gpt-4o`\n"
            "• `/oloid-set-llm anthropic sk-ant-... claude-3-haiku-20240307`\n"
            "• `/oloid-set-llm gemini AIza...`"
        )

    model = user_cfg["model"] or get_default_model(provider)

    if not model:
        raise RuntimeError(f"No default model for provider '{provider}'. Set one with `/oloid-set-llm {provider} <key> <model>`.")

    return LLMFactory.create(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=settings.ollama_base_url,
    )


def _load_memories(user_id: str) -> list[str]:
    """Load user memories from the private Slack channel."""
    try:
        from . import memory
        return memory.get_memories(user_id, limit=15)
    except Exception:
        return []


def _remember(user_id: str, user_message: str, bot_response: str):
    """Background: analyze interaction and save memory if useful."""
    try:
        from . import memory
        memory.analyze_and_remember(user_id, user_message, bot_response)
    except Exception:
        pass


class EmailAgent:
    def __init__(self):
        self._history: dict[str, list[dict]] = defaultdict(list)

    async def handle_message(self, user_id: str, text: str) -> str:
        """Route DM messages — email intents need Gmail, rest goes to general chat."""
        text_lower = text.lower().strip()

        if user_store.is_gmail_connected(user_id):
            if re.search(r"\bsummar", text_lower):
                n = self._extract_number(text_lower, default=10)
                return await self.summarize_emails(user_id, n)

            if re.search(r"\b(list|show|get)\b.*\bemail", text_lower):
                n = self._extract_number(text_lower, default=10)
                gmail = GmailClient(user_id)
                emails = gmail.fetch_emails(max_results=n)
                if not emails:
                    return "No emails found."
                return _format_email_list(emails)

            if re.search(r"\b(from|about|regarding)\b", text_lower):
                return await self._search_and_respond(user_id, text)

            if re.search(r"\b(today|recent|latest|new)\b.*\b(email|mail)\b", text_lower):
                return await self._recent_emails_chat(user_id, text)

        return await self.general_chat(user_id, text)

    async def general_chat(self, user_id: str, text: str) -> str:
        """General AI chat — works without Gmail. Personalized with memory."""
        memories = _load_memories(user_id)
        system = build_chat_prompt_with_memory(memories)

        self._history[user_id].append({"role": "user", "content": text})
        messages = self._history[user_id][-10:]

        llm = _get_llm_for_user(user_id)
        reply = await llm.chat(messages, system=system)
        self._history[user_id].append({"role": "assistant", "content": reply})

        _remember(user_id, text, reply)
        return reply

    async def summarize_emails(self, user_id: str, n: int = 10) -> str:
        gmail = GmailClient(user_id)
        emails = gmail.fetch_emails(max_results=n)
        if not emails:
            return "No emails found to summarize."

        emails_text = "\n---\n".join(_format_email_for_llm(e) for e in emails)
        llm = _get_llm_for_user(user_id)
        summary = await llm.summarize(emails_text)

        links = "\n".join(f"• <{e.gmail_url}|{e.subject}>" for e in emails[:5])
        result = f"{summary}\n\n*Quick links:*\n{links}"

        _remember(user_id, f"summarize {n} emails", result)
        return result

    async def _search_and_respond(self, user_id: str, text: str) -> str:
        query = text
        match = re.search(r"from\s+(\S+)", text, re.IGNORECASE)
        if match:
            query = f"from:{match.group(1)}"

        gmail = GmailClient(user_id)
        emails = gmail.search_emails(query, max_results=10)
        if not emails:
            return "No emails found matching your query."

        memories = _load_memories(user_id)
        system = build_email_prompt_with_memory(memories)

        emails_text = "\n---\n".join(_format_email_for_llm(e) for e in emails)
        llm = _get_llm_for_user(user_id)
        reply = await llm.chat(
            [{"role": "user", "content": f"Based on these emails:\n\n{emails_text}\n\nAnswer: {text}"}],
            system=system,
        )

        _remember(user_id, text, reply)
        return reply

    async def _recent_emails_chat(self, user_id: str, text: str) -> str:
        gmail = GmailClient(user_id)
        emails = gmail.fetch_recent(hours=24)
        if not emails:
            return "No new emails in the last 24 hours."

        memories = _load_memories(user_id)
        system = build_email_prompt_with_memory(memories)

        emails_text = "\n---\n".join(_format_email_for_llm(e) for e in emails)
        llm = _get_llm_for_user(user_id)
        reply = await llm.chat(
            [{"role": "user", "content": f"Here are the recent emails:\n\n{emails_text}\n\nUser asked: {text}"}],
            system=system,
        )

        _remember(user_id, text, reply)
        return reply

    async def ease_my_life(self, user_id: str, days: int, slack_data: dict) -> str:
        """Generate a priority-ranked briefing from Slack, Gmail, and reminders."""
        sections = []

        # ── Slack data ──
        mentions = slack_data.get("mentions", [])
        messages = slack_data.get("messages", [])
        if mentions:
            lines = [f"- [{m['channel']}] {m['user']}: {m['text']}" for m in mentions[:100]]
            sections.append("HIGH-PRIORITY SLACK (mentions & DMs):\n" + "\n".join(lines))
        if messages:
            lines = [f"- [Slack #{m['channel']}] {m['user']}: {m['text']}" for m in messages[:200]]
            sections.append("OTHER SLACK MESSAGES:\n" + "\n".join(lines))

        # ── Emails ──
        if user_store.is_gmail_connected(user_id):
            try:
                gmail = GmailClient(user_id)
                emails = gmail.fetch_recent(hours=days * 24)
                if emails:
                    lines = [
                        f"- [Email from {e.sender}] {e.subject}: {(e.body or e.snippet)[:200]}"
                        for e in emails[:50]
                    ]
                    sections.append("EMAILS:\n" + "\n".join(lines))
            except Exception:
                pass

        # ── Reminders ──
        reminders = user_store.get_user_reminders(user_id)
        if reminders:
            from datetime import datetime as dt, timezone as _tz
            from zoneinfo import ZoneInfo
            # Get user timezone from Slack
            try:
                from slack_sdk import WebClient
                _bot = WebClient(token=settings.slack_bot_token)
                _resp = _bot.users_info(user=user_id)
                _tz_str = _resp.data.get("user", {}).get("tz", "UTC")
                _user_tz = ZoneInfo(_tz_str)
            except Exception:
                _user_tz = ZoneInfo("UTC")
            lines = []
            for r in reminders:
                fire_str = dt.fromtimestamp(r["fire_at"], tz=_user_tz).strftime("%b %d, %I:%M %p")
                lines.append(f"- [Reminder] {r['text']} — due {fire_str}")
            sections.append("PENDING REMINDERS:\n" + "\n".join(lines))

        if not sections:
            return f"Nothing found in the last {days} day(s). Your inbox is clean!"

        combined = "\n\n".join(sections)

        # Truncate if too large
        if len(combined) > 15000:
            combined = combined[:15000] + "\n...(truncated)"

        system_prompt = (
            "You are OloidBot, a productivity assistant generating a priority-ranked daily briefing from Slack, email, and reminders from the past {days} day(s)."

            "Organize output into exactly three sections in this order:"
            "🔴 HIGH PRIORITY"
            "🟡 MEDIUM PRIORITY"
            "🟢 LOW PRIORITY"

            "Formatting rules:"
            "- Do NOT use ** or any markdown formatting."
            "- Use alphanumerical counting only."
            "- Each item must be one concise line."
            "- Leave one blank line between sections."
            "- No explanations or extra commentary."

            "For each item:"
            "- Start with numbers and alphanumeric for nested one or related items"
            "- Write a one-line action-oriented summary."
            "- End with a source tag in brackets: [Slack #channel], [Slack DM from Name], [Email from Name], or [Reminder]."

            "If a section has no items, write '- No items'."

            "End with:"
            "Quick Actions:"
            "1. Most urgent response"
            "2. Second most urgent"
            "3. Optional follow-up"

            "Keep everything clean, scannable, and Slack-friendly."
        )

        llm = _get_llm_for_user(user_id)
        reply = await llm.chat(
            [{"role": "user", "content": f"Here is my data from the past {days} day(s):\n\n{combined}"}],
            system=system_prompt,
        )
        return reply

    def _extract_number(self, text: str, default: int = 10) -> int:
        match = re.search(r"\b(\d+)\b", text)
        if match:
            return min(int(match.group(1)), 50)
        return default
