import base64
import json
import stat
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import httplib2
import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from httplib2 import Response

from fakes import FakeGmail
from jobaplier import gmail as gmail_module
from jobaplier.gmail import (
    SCOPES,
    GmailAuthError,
    GmailError,
    GoogleGmail,
    Mail,
    parse_message,
)

FIXTURE = Path(__file__).parent / "fixtures" / "gmail" / "message_multipart.json"


def test_scopes_are_exact():
    assert SCOPES == [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]


def test_parse_multipart_message():
    mail = parse_message(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert mail.id == "18f3a2b4c5d6e7f8"
    assert mail.sender == "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
    assert mail.subject == "Backendutvecklare på Åkeri AB – nytt jobb åt dig"
    assert mail.received_at == datetime(2025, 10, 19, 8, 0, 0)
    assert mail.received_at.tzinfo is None
    assert "Backendutvecklare på Åkeri AB i Göteborg" in mail.html
    assert mail.html.startswith("<html>")
    assert "linkedin.com/comm/jobs/view/4012345678" in mail.html
    assert mail.text.startswith("Nytt jobb för dig")
    assert "<html>" not in mail.text


def test_parse_single_part_without_html():
    payload = {
        "id": "x1",
        "internalDate": "0",
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "From", "value": "a@b.se"}],
            "body": {"data": "aGVq"},  # "hej" utan padding-problem
        },
    }
    mail = parse_message(payload)
    assert mail.text == "hej"
    assert mail.html == ""
    assert mail.subject == ""
    assert mail.received_at == datetime(1970, 1, 1)


def test_from_files_without_token_raises(tmp_path: Path):
    with pytest.raises(GmailAuthError, match="jobaplier gmail-login"):
        GoogleGmail.from_files(tmp_path / "credentials.json", tmp_path / "gmail_token.json")


def _token_json(**overrides) -> str:
    data = {
        "token": "x",
        "refresh_token": None,
        "client_id": "a",
        "client_secret": "b",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": SCOPES,
        "expiry": "2000-01-01T00:00:00Z",
    }
    data.update(overrides)
    return json.dumps(data)


def test_from_files_expired_token_without_refresh_token_raises(tmp_path: Path):
    token = tmp_path / "gmail_token.json"
    token.write_text(_token_json())
    with pytest.raises(GmailAuthError, match="ogiltig och kan inte förnyas") as info:
        GoogleGmail.from_files(tmp_path / "credentials.json", token)
    assert "jobaplier gmail-login" in str(info.value)


def test_from_files_corrupt_token_raises(tmp_path: Path):
    token = tmp_path / "gmail_token.json"
    token.write_text("{inte json")
    with pytest.raises(GmailAuthError, match="kunde inte läsas"):
        GoogleGmail.from_files(tmp_path / "credentials.json", token)


def test_from_files_refresh_error_raises_and_keeps_token(tmp_path: Path, monkeypatch):
    token = tmp_path / "gmail_token.json"
    original = _token_json(refresh_token="r")
    token.write_text(original)

    def boom(self, request):
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(GmailAuthError, match="kunde inte förnyas"):
        GoogleGmail.from_files(tmp_path / "credentials.json", token)
    assert token.read_text() == original


def test_from_files_network_error_during_refresh_is_gmail_error(tmp_path: Path, monkeypatch):
    token = tmp_path / "gmail_token.json"
    token.write_text(_token_json(refresh_token="r"))

    def boom(self, request):
        raise TransportError("Failed to establish a new connection")

    monkeypatch.setattr(Credentials, "refresh", boom)
    with pytest.raises(GmailError, match="nätverksfel") as info:
        GoogleGmail.from_files(tmp_path / "credentials.json", token)
    assert not isinstance(info.value, GmailAuthError)  # inloggningen kan vara giltig


def test_from_files_refresh_success_rewrites_token_securely(tmp_path: Path, monkeypatch):
    token = tmp_path / "gmail_token.json"
    token.write_text(_token_json(refresh_token="r"))
    token.chmod(0o644)

    def fake_refresh(self, request):
        self.token = "fresh"
        self.expiry = datetime(2999, 1, 1)

    built: dict = {}

    def fake_build(*args, **kwargs):
        built["args"], built["kwargs"] = args, kwargs
        return "service"

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    monkeypatch.setattr(gmail_module, "build", fake_build)
    client = GoogleGmail.from_files(tmp_path / "credentials.json", token)

    assert client._service == "service"
    assert built["args"] == ("gmail", "v1")
    assert built["kwargs"]["cache_discovery"] is False
    saved = json.loads(token.read_text())
    assert saved["token"] == "fresh"
    assert saved["refresh_token"] == "r"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["gmail_token.json"]


def test_fake_gmail_records_queries_and_sends():
    mail = Mail("1", "a@b.se", "Hej", datetime(2025, 1, 1), "", "x")
    fake = FakeGmail([mail])
    assert fake.search("from:linkedin") == [mail]
    assert fake.queries == ["from:linkedin"]
    msg = EmailMessage()
    msg["To"] = "c@d.se"
    assert fake.send(msg) == "fake-1"
    assert fake.send(msg) == "fake-2"
    assert fake.sent == [msg, msg]


def test_fake_gmail_fail_send():
    fake = FakeGmail(fail_send=RuntimeError("nere"))
    with pytest.raises(RuntimeError):
        fake.send(EmailMessage())
    assert fake.sent == []


class _Call:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class _StubService:
    """Minimal users().messages()-kedja som svarar med färdiga sidor."""

    def __init__(self, pages, full, send_result=None, error=None):
        self.pages, self.full, self.send_result, self.error = pages, full, send_result, error
        self.list_calls: list[dict] = []
        self.send_bodies: list[dict] = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.error:
            return _Call(error=self.error)
        return _Call(self.pages[kwargs["pageToken"] or ""])

    def get(self, *, userId, id, format):
        assert (userId, format) == ("me", "full")
        return _Call(self.full[id])

    def send(self, *, userId, body):
        self.send_bodies.append(body)
        if self.error:
            return _Call(error=self.error)
        return _Call(self.send_result)


def _http_error(status: int) -> HttpError:
    return HttpError(Response({"status": status}), b"{}")


def test_search_follows_all_pages():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    second = {**payload, "id": "second"}
    service = _StubService(
        pages={
            "": {"messages": [{"id": payload["id"]}], "nextPageToken": "p2"},
            "p2": {"messages": [{"id": "second"}]},
        },
        full={payload["id"]: payload, "second": second},
    )
    mails = GoogleGmail(service).search("from:linkedin")
    assert [m.id for m in mails] == [payload["id"], "second"]
    assert [c["q"] for c in service.list_calls] == ["from:linkedin", "from:linkedin"]


def test_send_posts_base64url_raw_and_returns_id():
    msg = EmailMessage()
    msg["To"] = "c@d.se"
    msg["Subject"] = "Ansökan"
    msg.set_content("Hej")
    service = _StubService(pages={}, full={}, send_result={"id": "gmail-123"})
    assert GoogleGmail(service).send(msg) == "gmail-123"
    raw = service.send_bodies[0]["raw"]
    assert base64.urlsafe_b64decode(raw) == msg.as_bytes()


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_errors_become_gmail_auth_error(status):
    service = _StubService(pages={}, full={}, error=_http_error(status))
    with pytest.raises(GmailAuthError):
        GoogleGmail(service).send(EmailMessage())
    with pytest.raises(GmailAuthError):
        GoogleGmail(service).search("x")


def test_other_http_errors_become_gmail_error():
    service = _StubService(pages={}, full={}, error=_http_error(500))
    with pytest.raises(GmailError) as info:
        GoogleGmail(service).send(EmailMessage())
    assert not isinstance(info.value, GmailAuthError)


def test_http_error_keeps_status():
    service = _StubService(pages={}, full={}, error=_http_error(400))
    with pytest.raises(GmailError) as info:
        GoogleGmail(service).send(EmailMessage())
    assert info.value.status == 400


def test_refresh_error_during_execute_becomes_auth_error_in_swedish():
    """Testläges-token går ut efter 7 dagar; felet kommer först i `.execute()`."""
    error = RefreshError("invalid_grant: Token has been expired or revoked.")
    service = _StubService(pages={}, full={}, error=error)
    for call in (lambda g: g.send(EmailMessage()), lambda g: g.search("x")):
        with pytest.raises(GmailAuthError) as info:
            call(GoogleGmail(service))
        assert "jobaplier gmail-login" in str(info.value)
        assert "invalid_grant" not in str(info.value)
        assert "RefreshError" not in str(info.value)


@pytest.mark.parametrize(
    "error",
    [
        TransportError("connection reset"),
        httplib2.ServerNotFoundError("Unable to find the server at gmail.googleapis.com"),
        TimeoutError("timed out"),
        ConnectionResetError(54, "Connection reset by peer"),
    ],
)
def test_transport_errors_become_gmail_error_in_swedish(error):
    service = _StubService(pages={}, full={}, error=error)
    for call in (lambda g: g.send(EmailMessage()), lambda g: g.search("x")):
        with pytest.raises(GmailError) as info:
            call(GoogleGmail(service))
        assert not isinstance(info.value, GmailAuthError)
        assert info.value.status is None
        assert "nätverksfel" in str(info.value)
