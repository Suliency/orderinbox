"""Inbox monitoring: the unattended part of the product.

IMAP mode (default): connects to any IMAP server (Gmail, Office 365/Exchange
IMAP, cPanel) on a poll interval, downloads new messages with attachments as
.eml files into the spool, marks them for processing, and never re-processes
a message (idempotent by UID).

Gmail API mode: uses google-api-python-client (extra: orderinbox[gmail]) for
the same flow with push-friendly polling.

The monitor only *collects*. Processing is done by the Pipeline.
"""
from __future__ import annotations

import email as email_lib
import email.utils
import hashlib
import imaplib
import logging
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from pathlib import Path
from typing import Optional

from .config import Settings
from .models import ProcessedOrder
from .pipeline import Pipeline

log = logging.getLogger("orderinbox.mail")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    return " ".join(
        p.decode(c or "utf-8", "ignore") if isinstance(p, bytes) else p
        for p, c in parts).strip()


class MailMonitor:
    def __init__(self, settings: Settings, pipeline: Pipeline):
        self.settings = settings
        self.pipeline = pipeline

    def poll_once(self) -> list[ProcessedOrder]:
        provider = self.settings.mail_provider
        if provider == "imap":
            return self._poll_imap()
        if provider == "gmail":
            return self._poll_gmail()
        return []

    # ------------------------------------------------------------------
    def _poll_imap(self) -> list[ProcessedOrder]:
        s = self.settings
        if not (s.imap_username and s.imap_password):
            log.info("IMAP not configured (IMAP_USERNAME / IMAP_PASSWORD)")
            return []
        results: list[ProcessedOrder] = []
        conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port)
        try:
            conn.login(s.imap_username, s.imap_password)
            conn.select(s.imap_folder)
            since = (datetime.now(timezone.utc) - timedelta(days=s.inbox_lookback_days)).strftime("%d-%b-%Y")
            status, data = conn.search(None, f"(SINCE {since})")
            if status != "OK":
                return []
            uids = data[0].split()
            cutoff = datetime.now(timezone.utc) - timedelta(days=s.inbox_lookback_days)
            for uid in uids:
                status, msg_data = conn.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                msg = email_lib.message_from_bytes(raw)
                date_hdr = msg.get("Date")
                try:
                    msg_date = email.utils.parsedate_to_datetime(date_hdr)
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                    if msg_date < cutoff:
                        continue
                except Exception:
                    pass
                subject = _decode(msg.get("Subject"))
                sender = _decode(msg.get("From"))
                # skip obvious non-orders before storing anything
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            payload = part.get_payload(decode=True)
                            body += (payload or b"").decode(part.get_content_charset() or "utf-8", "ignore")
                else:
                    payload = msg.get_payload(decode=True)
                    body = (payload or b"").decode("utf-8", "ignore") if payload else (msg.get_payload() or "")
                # cheap filter: only store messages that look like orders,
                # freight (RFQ/quote), or have attachments
                has_att = any(p.get_content_disposition() == "attachment" for p in msg.walk()) if msg.is_multipart() else False
                from .extract.documents import order_like_text
                from .freight.extract import classify_freight_text
                if not has_att and not order_like_text(body) and classify_freight_text(body) is None:
                    continue
                dest = self._store(raw, subject, sender)
                order = self.pipeline.process_message(
                    dest, subject=subject, sender=sender,
                    received_at=msg_date.astimezone(timezone.utc).replace(tzinfo=None) if msg_date else None)
                results.append(order)
                log.info("processed IMAP uid %s: %s -> %s", uid.decode(), subject, order.status.value)
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        return results

    def _store(self, raw: bytes, subject: str, sender: str) -> Path:
        digest = hashlib.sha256(raw).hexdigest()[:16]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", subject or "message")[:40]
        self.settings.spool_dir.mkdir(parents=True, exist_ok=True)
        path = self.settings.spool_dir / f"{digest}_{safe}.eml"
        if not path.exists():
            path.write_bytes(raw)
        return path

    # ------------------------------------------------------------------
    def _poll_gmail(self) -> list[ProcessedOrder]:
        s = self.settings
        if not s.imap_username:
            log.info("Gmail mode needs GMAIL_USER (IMAP_USERNAME)")
            return []
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib import flow as gaflow
            from googleapiclient.discovery import build
        except ImportError:
            log.error("gmail extra not installed: pip install 'orderinbox[gmail]'")
            return []
        creds_file = s.home / "data" / "gmail_credentials.json"
        tokens_file = s.home / "data" / "gmail_token.json"
        SCOPES = ["https://mail.google.com/", "https://www.googleapis.com/auth/userinfo.email"]
        creds = None
        if tokens_file.exists():
            creds = Credentials.from_authorized_user_file(str(tokens_file), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                creds.token_uri = None
                creds.to_json_file(tokens_file) if hasattr(creds, "to_json_file") else None
            else:
                log.error("No Gmail token. Run `orderinbox gmail-auth` once to authorize.")
                return []
        svc = build("gmail", "v1", credentials=creds)
        results: list[ProcessedOrder] = []
        q = f"newer_than:{s.inbox_lookback_days}d"
        resp = svc.users().messages().list(userId="me", q=q, maxResults=100).execute()
        for item in resp.get("messages", []):
            msg = svc.users().messages().get(userId="me", id=item["id"],
                                             format="raw").execute()
            import base64
            raw = base64.urlsafe_b64decode(msg["raw"].encode())
            parsed = email_lib.message_from_bytes(raw)
            subject = _decode(parsed.get("Subject"))
            sender = _decode(parsed.get("From"))
            date_hdr = parsed.get("Date")
            try:
                msg_date = email.utils.parsedate_to_datetime(date_hdr)
                received = msg_date.astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                received = None
            dest = self._store(raw, subject, sender)
            order = self.pipeline.process_message(dest, subject=subject, sender=sender, received_at=received)
            results.append(order)
        return results


def gmail_auth_command(settings: Settings) -> None:
    """Interactive one-time OAuth authorization for Gmail mode."""
    try:
        from google_auth_oauthlib import flow
    except ImportError:
        raise SystemExit("pip install 'orderinbox[gmail]' first")
    import os
    client_id = os.environ.get("GMAIL_CLIENT_ID")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET")
    redirect = os.environ.get("GMAIL_REDIRECT_URI", "http://localhost:8090/callback")
    if not (client_id and client_secret):
        raise SystemExit("Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET from the Google Cloud console.")
    creds_file = settings.home / "data" / "gmail_credentials.json"
    creds_file.write_text(f'{{"installed": {{"client_id": "{client_id}", "client_secret": "{client_secret}", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["{redirect}"]}}}}')
    tokens_file = settings.home / "data" / "gmail_token.json"
    flow_obj = flow.InstalledAppFlow.from_client_secrets_file(str(creds_file), scopes=["https://mail.google.com/"])
    creds = flow_obj.run_console()
    import json as _json
    tokens_file.write_text(_json.dumps({
        "token": creds.token, "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri, "client_id": creds.client_id,
        "client_secret": creds.client_secret, "scopes": creds.scopes,
    }))
    print(f"Authorized. Token stored at {tokens_file}")
