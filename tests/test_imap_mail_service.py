import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.imap_mail import ImapMailService, imap_service_matches_account
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class FakeClock:
    def __init__(self, start=1_700_000_000):
        self.current = float(start)

    def time(self):
        self.current += 1.0
        return self.current


class FakeIMAPClient:
    def __init__(self, unseen_ids, all_ids, messages):
        self.unseen_ids = list(unseen_ids)
        self.all_ids = list(all_ids)
        self.messages = dict(messages)
        self.stored = []

    def select(self, mailbox):
        assert mailbox == "INBOX"
        return "OK", [b"1"]

    def search(self, _charset, criteria):
        ids = self.unseen_ids if criteria == "UNSEEN" else self.all_ids
        payload = b" ".join(ids)
        return "OK", [payload]

    def fetch(self, msg_id, _query):
        return "OK", [(b"RFC822", self.messages[msg_id])]

    def store(self, msg_id, op, flag):
        self.stored.append((msg_id, op, flag))
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


def _build_message(*, recipient, code, sender="noreply@openai.com", dt=None):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    if dt is None:
        dt = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
    msg["Date"] = dt.strftime("%a, %d %b %Y %H:%M:%S %z")
    msg["Subject"] = "Your OpenAI verification code"
    msg.set_content(f"Your OpenAI verification code is {code}")
    return msg.as_bytes()


def test_create_email_returns_generated_address_for_catchall():
    service = ImapMailService({
        "host": "mail.example.com",
        "port": 993,
        "use_ssl": True,
        "email": "admin@example.com",
        "password": "secret",
        "address_mode": "catchall",
        "domain": "example.com",
        "subdomain": "test",
    })

    email_info = service.create_email()

    assert email_info["email"].endswith("@test.example.com")
    assert email_info["inbox_email"] == "admin@example.com"
    assert email_info["service_id"] == email_info["email"]


def test_get_verification_code_filters_by_target_recipient(monkeypatch):
    service = ImapMailService({
        "host": "mail.example.com",
        "email": "admin@example.com",
        "password": "secret",
        "address_mode": "catchall",
        "domain": "example.com",
        "subdomain": "test",
    })
    fake_mail = FakeIMAPClient(
        unseen_ids=[b"1", b"2"],
        all_ids=[b"1", b"2"],
        messages={
            b"1": _build_message(recipient="other@test.example.com", code="111111"),
            b"2": _build_message(recipient="target@test.example.com", code="222222"),
        },
    )
    clock = FakeClock()

    monkeypatch.setattr(service, "_connect", lambda: fake_mail)
    monkeypatch.setattr("src.services.imap_mail.time.time", clock.time)
    monkeypatch.setattr("src.services.imap_mail.time.sleep", lambda _value: None)

    code = service.get_verification_code(
        email="target@test.example.com",
        timeout=3,
    )

    assert code == "222222"
    assert fake_mail.stored == [(b"2", "+FLAGS", "\\Seen")]


def test_get_verification_code_falls_back_to_recent_seen_mail(monkeypatch):
    service = ImapMailService({
        "host": "mail.example.com",
        "email": "admin@example.com",
        "password": "secret",
    })
    recent_dt = datetime(2026, 4, 5, 12, 0, 5, tzinfo=timezone.utc)
    fake_mail = FakeIMAPClient(
        unseen_ids=[],
        all_ids=[b"9"],
        messages={
            b"9": _build_message(recipient="admin@example.com", code="333333", dt=recent_dt),
        },
    )
    clock = FakeClock()

    monkeypatch.setattr(service, "_connect", lambda: fake_mail)
    monkeypatch.setattr("src.services.imap_mail.time.time", clock.time)
    monkeypatch.setattr("src.services.imap_mail.time.sleep", lambda _value: None)

    code = service.get_verification_code(
        email="admin@example.com",
        timeout=3,
        otp_sent_at=datetime(2026, 4, 5, 12, 0, 3, tzinfo=timezone.utc).timestamp(),
    )

    assert code == "333333"


def test_get_verification_code_skips_old_mail_from_all_search(monkeypatch):
    service = ImapMailService({
        "host": "mail.example.com",
        "email": "admin@example.com",
        "password": "secret",
    })
    old_dt = datetime(2026, 4, 5, 11, 55, 0, tzinfo=timezone.utc)
    fake_mail = FakeIMAPClient(
        unseen_ids=[],
        all_ids=[b"5"],
        messages={
            b"5": _build_message(recipient="admin@example.com", code="444444", dt=old_dt),
        },
    )
    clock = FakeClock()

    monkeypatch.setattr(service, "_connect", lambda: fake_mail)
    monkeypatch.setattr("src.services.imap_mail.time.time", clock.time)
    monkeypatch.setattr("src.services.imap_mail.time.sleep", lambda _value: None)

    code = service.get_verification_code(
        email="admin@example.com",
        timeout=2,
        otp_sent_at=datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc).timestamp(),
    )

    assert code is None


def test_get_verification_code_uses_settings_poll_interval(monkeypatch):
    service = ImapMailService({
        "host": "mail.example.com",
        "email": "admin@example.com",
        "password": "secret",
    })
    fake_mail = FakeIMAPClient(unseen_ids=[], all_ids=[], messages={})
    clock = FakeClock()
    sleep_calls = []

    class DummySettings:
        email_code_timeout = 60
        email_code_poll_interval = 7

    monkeypatch.setattr(service, "_connect", lambda: fake_mail)
    monkeypatch.setattr("src.services.imap_mail.get_settings", lambda: DummySettings())
    monkeypatch.setattr("src.services.imap_mail.time.time", clock.time)
    monkeypatch.setattr("src.services.imap_mail.time.sleep", lambda value: sleep_calls.append(value))

    code = service.get_verification_code(
        email="admin@example.com",
        timeout=2,
    )

    assert code is None
    assert sleep_calls == [7]


def test_imap_service_matches_account_handles_catchall_domain():
    config = {
        "email": "admin@example.com",
        "address_mode": "catchall",
        "domain": "example.com",
        "subdomain": "test",
    }

    assert imap_service_matches_account(config, "foo@test.example.com") is True
    assert imap_service_matches_account(config, "foo@example.com") is False


def test_email_service_types_include_imap_catchall_fields():
    result = asyncio.run(email_routes.get_service_types())
    imap_type = next(item for item in result["types"] if item["value"] == "imap_mail")

    field_names = [field["name"] for field in imap_type["config_fields"]]
    assert "address_mode" in field_names
    assert "domain" in field_names
    assert "subdomain" in field_names


def test_update_email_service_preserves_false_and_clears_old_catchall(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "imap_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        service = EmailService(
            service_type="imap_mail",
            name="Catchall IMAP",
            config={
                "host": "mail.example.com",
                "port": 993,
                "use_ssl": True,
                "email": "admin@example.com",
                "password": "secret",
                "address_mode": "catchall",
                "domain": "example.com",
                "subdomain": "test",
            },
            enabled=True,
            priority=0,
        )
        session.add(service)
        session.flush()
        service_id = service.id

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(email_routes, "get_db", fake_get_db)

    request = email_routes.EmailServiceUpdate(
        config={
            "use_ssl": False,
            "address_mode": "single",
            "domain": "",
            "subdomain": "",
        }
    )

    asyncio.run(email_routes.update_email_service(service_id, request))

    with manager.session_scope() as session:
        saved = session.query(EmailService).filter(EmailService.id == service_id).first()
        assert saved.config["use_ssl"] is False
        assert saved.config["address_mode"] == "single"
        assert "domain" not in saved.config
        assert "subdomain" not in saved.config


def test_registration_available_services_include_imap_catchall_metadata(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "imap_available_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="imap_mail",
                name="Catchall IMAP",
                config={
                    "host": "mail.example.com",
                    "port": 993,
                    "use_ssl": True,
                    "email": "admin@example.com",
                    "password": "secret",
                    "address_mode": "catchall",
                    "domain": "example.com",
                    "subdomain": "test",
                },
                enabled=True,
                priority=0,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class DummySettings:
        yyds_mail_enabled = False
        yyds_mail_api_key = None
        custom_domain_base_url = ""
        custom_domain_api_key = None

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)
    monkeypatch.setattr(registration_routes, "get_settings", lambda: DummySettings())

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["imap_mail"]["available"] is True
    service = result["imap_mail"]["services"][0]
    assert service["address_mode"] == "catchall"
    assert service["generated_domain"] == "test.example.com"
    assert service["email"] == "admin@example.com"
