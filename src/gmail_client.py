import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .user_store import user_store

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class Email:
    message_id: str
    subject: str
    sender: str
    date: str
    snippet: str
    body: str = ""
    labels: list[str] = field(default_factory=list)

    @property
    def gmail_url(self) -> str:
        return f"https://mail.google.com/mail/u/0/#inbox/{self.message_id}"


class GmailClient:
    """Per-user Gmail client. Each instance is bound to one Slack user."""

    def __init__(self, slack_user_id: str):
        self._slack_user_id = slack_user_id
        self._service = None

    def _get_service(self):
        if self._service:
            return self._service

        token_data = user_store.get_gmail_token(self._slack_user_id)
        if not token_data:
            raise RuntimeError(
                "Gmail not connected. Use `/oloid-connect-gmail` to link your account."
            )

        creds = Credentials.from_authorized_user_info(token_data, SCOPES)

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                user_store.update_gmail_token(
                    self._slack_user_id, json.loads(creds.to_json())
                )
            else:
                user_store.disconnect_gmail(self._slack_user_id)
                raise RuntimeError(
                    "Gmail token expired and can't be refreshed. "
                    "Use `/oloid-connect-gmail` to reconnect."
                )

        self._service = build("gmail", "v1", credentials=creds)
        return self._service

    def fetch_emails(self, max_results: int = 10, query: str = "") -> list[Email]:
        service = self._get_service()
        q = query or "in:inbox"
        results = (
            service.users()
            .messages()
            .list(userId="me", q=q, maxResults=max_results)
            .execute()
        )
        messages = results.get("messages", [])
        emails = []
        for msg_ref in messages:
            email = self._parse_message(service, msg_ref["id"])
            if email:
                emails.append(email)
        return emails

    def search_emails(self, query: str, max_results: int = 10) -> list[Email]:
        return self.fetch_emails(max_results=max_results, query=query)

    def fetch_recent(self, hours: int = 24, max_results: int = 50) -> list[Email]:
        ts = int(datetime.now(timezone.utc).timestamp() - (hours * 3600))
        query = f"in:inbox after:{ts}"
        return self.fetch_emails(max_results=max_results, query=query)

    def fetch_new_since(self, since_ts: float, max_results: int = 20) -> list[Email]:
        """Fetch emails received after a Unix timestamp. Used by the poller."""
        ts = int(since_ts)
        query = f"in:inbox after:{ts}"
        return self.fetch_emails(max_results=max_results, query=query)

    def _parse_message(self, service, msg_id: str) -> Optional[Email]:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
            headers = {
                h["name"].lower(): h["value"] for h in msg["payload"]["headers"]
            }
            body = self._extract_body(msg["payload"])

            return Email(
                message_id=msg_id,
                subject=headers.get("subject", "(no subject)"),
                sender=headers.get("from", "unknown"),
                date=headers.get("date", ""),
                snippet=msg.get("snippet", ""),
                body=body,
                labels=msg.get("labelIds", []),
            )
        except Exception:
            logger.exception("Failed to parse message %s", msg_id)
            return None

    def _extract_body(self, payload: dict) -> str:
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )

        for part in payload.get("parts", []):
            if part["mimeType"] == "text/plain" and part.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(part["body"]["data"]).decode(
                    "utf-8", errors="replace"
                )
            if part.get("parts"):
                result = self._extract_body(part)
                if result:
                    return result

        return ""
