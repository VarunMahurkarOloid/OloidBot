"""
Email poller — checks each connected user's Gmail for new emails
at a configurable interval and DMs them a summary of new messages.
"""

import asyncio
import logging
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from slack_sdk import WebClient

from .config import settings
from .user_store import user_store

logger = logging.getLogger(__name__)


def _poll_new_emails():
    """Check all connected users for new emails and DM summaries."""
    from .agent import _format_email_for_llm, _get_llm_for_user
    from .gmail_client import GmailClient

    connected_users = user_store.all_connected_users()
    if not connected_users:
        return

    client = WebClient(token=settings.slack_bot_token)
    now = time.time()

    for user_id in connected_users:
        # Skip users who disabled notifications
        if not user_store.get_notifications(user_id):
            continue

        last_poll = user_store.get_last_poll_ts(user_id)
        if last_poll == 0:
            # First poll — set to now, don't flood
            user_store.set_last_poll_ts(user_id, now)
            continue

        try:
            gmail = GmailClient(user_id)
            new_emails = gmail.fetch_new_since(last_poll, max_results=20)

            user_store.set_last_poll_ts(user_id, now)

            if not new_emails:
                continue

            logger.info("Found %d new emails for user %s", len(new_emails), user_id)

            # Summarize with LLM
            try:
                llm = _get_llm_for_user(user_id)
                emails_text = "\n---\n".join(_format_email_for_llm(e) for e in new_emails)
                loop = asyncio.new_event_loop()
                summary = loop.run_until_complete(llm.summarize(emails_text))
                loop.close()

                # Build quick links
                links = "\n".join(
                    f"• <{e.gmail_url}|{e.subject}>" for e in new_emails[:5]
                )
                message = (
                    f"*New emails ({len(new_emails)}):*\n\n"
                    f"{summary}\n\n"
                    f"*Quick links:*\n{links}"
                )
            except Exception:
                # LLM not configured — send plain list
                logger.debug("LLM not available for user %s, sending plain list", user_id)
                lines = []
                for e in new_emails:
                    lines.append(f"• *{e.subject}* — {e.sender}\n  <{e.gmail_url}|Open in Gmail>")
                message = f"*New emails ({len(new_emails)}):*\n\n" + "\n".join(lines)

            client.chat_postMessage(channel=user_id, text=message)

        except Exception:
            logger.exception("Poll failed for user %s", user_id)


_scheduler: BackgroundScheduler = None


def _fire_reminder(user_id: str, text: str, reminder_id: str):
    """DM the user their reminder, then remove it from the store."""
    try:
        client = WebClient(token=settings.slack_bot_token)
        dm = client.conversations_open(users=[user_id])
        dm_channel = dm["channel"]["id"]
        client.chat_postMessage(
            channel=dm_channel,
            text=f":bell: *Reminder:* {text}",
        )
    except Exception:
        logger.exception("Failed to send reminder to %s", user_id)
    finally:
        user_store.remove_reminder(user_id, reminder_id)


def schedule_reminder(user_id: str, text: str, fire_at_ts: float) -> str:
    """Schedule a one-shot reminder job. Returns the reminder ID."""
    reminder_id = user_store.add_reminder(user_id, text, fire_at_ts)
    run_date = datetime.fromtimestamp(fire_at_ts)

    if _scheduler is not None:
        _scheduler.add_job(
            _fire_reminder,
            "date",
            run_date=run_date,
            args=[user_id, text, reminder_id],
            id=reminder_id,
        )

    return reminder_id


def cancel_reminder(user_id: str, reminder_id: str):
    """Cancel a pending reminder."""
    user_store.remove_reminder(user_id, reminder_id)
    if _scheduler is not None:
        try:
            _scheduler.remove_job(reminder_id)
        except Exception:
            pass


def _restore_reminders():
    """Re-schedule all pending reminders from the store (after restart)."""
    now = time.time()
    reminders = user_store.get_all_reminders()
    restored = 0

    for r in reminders:
        if r["fire_at"] <= now:
            # Already past due — fire immediately
            _fire_reminder(r["user_id"], r["text"], r["id"])
        else:
            run_date = datetime.fromtimestamp(r["fire_at"])
            _scheduler.add_job(
                _fire_reminder,
                "date",
                run_date=run_date,
                args=[r["user_id"], r["text"], r["id"]],
                id=r["id"],
            )
            restored += 1

    if restored:
        logger.info("Restored %d pending reminders.", restored)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    _scheduler = BackgroundScheduler()

    interval = settings.poll_interval_minutes
    _scheduler.add_job(
        _poll_new_emails,
        "interval",
        minutes=interval,
        id="email_poller",
    )

    _scheduler.start()
    logger.info("Email poller started — checking every %d minutes.", interval)

    _restore_reminders()

    return _scheduler
