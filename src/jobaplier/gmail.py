"""Gmail-klient: sökning av aviseringsmejl, skickande av ansökningsmejl och OAuth-inloggning."""

from __future__ import annotations

import base64
import email.message
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any, Protocol

import httplib2
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

LOGIN_HINT = "Kör `jobaplier gmail-login` för att logga in igen."

# Nätverksfel och tidsgränser (socket-timeouts är `OSError`). Vid skick kan Gmail ha
# tagit emot mejlet trots felet.
_TRANSPORT_ERRORS = (TransportError, httplib2.HttpLib2Error, OSError)
_GOOGLE_ERRORS = (HttpError, RefreshError, *_TRANSPORT_ERRORS)


class GmailError(Exception):
    """Gmail-anrop misslyckades. Meddelandet är på svenska.

    `status` är HTTP-statusen när Gmails API svarade med fel, annars `None`
    (nätverksfel, tidsgräns)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GmailAuthError(GmailError):
    """Inloggningen mot Gmail saknas eller har gått ut. Meddelandet är på svenska."""


@dataclass
class Mail:
    id: str
    sender: str
    subject: str
    received_at: datetime  # naiv UTC
    html: str
    text: str


class Gmail(Protocol):
    def search(self, query: str) -> list[Mail]: ...

    def send(self, msg: email.message.EmailMessage) -> str:
        """Skickar mejlet och returnerar Gmails message-id."""
        ...


def _header(payload: dict[str, Any], name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_header_value(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def _part_charset(part: dict[str, Any]) -> str:
    content_type = _header(part, "Content-Type")
    for piece in content_type.split(";")[1:]:
        key, _, value = piece.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip().strip('"')
    return "utf-8"


def _part_text(part: dict[str, Any]) -> str:
    data = part.get("body", {}).get("data", "")
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(_part_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _find_part(part: dict[str, Any], mime_type: str) -> dict[str, Any] | None:
    if part.get("mimeType") == mime_type and part.get("body", {}).get("data"):
        return part
    for child in part.get("parts", []):
        found = _find_part(child, mime_type)
        if found is not None:
            return found
    return None


def parse_message(payload: dict[str, Any]) -> Mail:
    """Tolkar ett svar från `users.messages.get(format="full")`."""
    root = payload.get("payload", {})
    received = datetime.fromtimestamp(int(payload.get("internalDate", 0)) / 1000, UTC)
    html_part = _find_part(root, "text/html")
    text_part = _find_part(root, "text/plain")
    return Mail(
        id=payload["id"],
        sender=_header(root, "From"),
        subject=_decode_header_value(_header(root, "Subject")),
        received_at=received.replace(tzinfo=None),
        html=_part_text(html_part) if html_part else "",
        text=_part_text(text_part) if text_part else "",
    )


class GoogleGmail(Gmail):
    def __init__(self, service: Any) -> None:
        self._service = service

    @classmethod
    def from_files(cls, credentials: Path, token: Path) -> GoogleGmail:
        """Laddar sparad token och förnyar den vid behov. Öppnar aldrig en webbläsare."""
        if not token.exists():
            raise GmailAuthError(f"Gmail är inte inloggat (token saknas). {LOGIN_HINT}")
        try:
            creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        except (ValueError, OSError) as exc:
            raise GmailAuthError(f"Gmail-token kunde inte läsas. {LOGIN_HINT}") from exc
        if not creds.valid:
            if not (creds.expired and creds.refresh_token):
                raise GmailAuthError(f"Gmail-token är ogiltig och kan inte förnyas. {LOGIN_HINT}")
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise GmailAuthError(f"Gmail-token kunde inte förnyas. {LOGIN_HINT}") from exc
            except _TRANSPORT_ERRORS as exc:
                raise _wrap_error(exc, "Gmail-token kunde inte förnyas") from exc
            _write_token(token, creds.to_json())
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return cls(service)

    def search(self, query: str) -> list[Mail]:
        users = self._service.users()
        try:
            ids: list[str] = []
            page_token: str | None = None
            while True:
                resp = users.messages().list(userId="me", q=query, pageToken=page_token).execute()
                ids.extend(m["id"] for m in resp.get("messages", []))
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
            return [
                parse_message(users.messages().get(userId="me", id=i, format="full").execute())
                for i in ids
            ]
        except _GOOGLE_ERRORS as exc:
            raise _wrap_error(exc, "Gmail-sökningen misslyckades") from exc

    def send(self, msg: email.message.EmailMessage) -> str:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        try:
            sent = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except _GOOGLE_ERRORS as exc:
            raise _wrap_error(exc, "Mejlet kunde inte skickas via Gmail") from exc
        return sent["id"]


def _wrap_error(exc: Exception, message: str) -> GmailError:
    """Googles fel (engelska) blir `GmailError`/`GmailAuthError` på svenska."""
    if isinstance(exc, HttpError):
        status = exc.resp.status
        if status in (401, 403):
            return GmailAuthError(
                f"{message}: behörighet saknas (HTTP {status}). {LOGIN_HINT}", status=status
            )
        return GmailError(f"{message} (HTTP {status}).", status=status)
    if isinstance(exc, RefreshError):
        # Token som gått ut eller återkallats (testlägets token gäller i 7 dagar).
        return GmailAuthError(f"{message}: Gmail-inloggningen har gått ut. {LOGIN_HINT}")
    return GmailError(f"{message}: nätverksfel eller tidsgräns ({type(exc).__name__}).")


def _write_token(path: Path, content: str) -> None:
    """Skriver token-filen atomiskt med läsrättighet bara för ägaren (den innehåller en refresh-token)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def run_oauth_flow(credentials: Path, token: Path) -> None:
    """Interaktiv inloggning (öppnar webbläsaren) och sparar token."""
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials), SCOPES)
    creds = flow.run_local_server(port=0)
    _write_token(token, creds.to_json())
