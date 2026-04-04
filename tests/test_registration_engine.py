import base64
import json

from src.config.constants import EmailServiceType, OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.core.http_client import OpenAIHTTPClient
from src.core.openai.oauth import OAuthStart
from src.core.register import RegistrationEngine, RegistrationResult
from src.services.base import BaseEmailService


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, on_return=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.on_return = on_return

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class QueueSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.cookies = {}

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._request(method.upper(), url, **kwargs)

    def close(self):
        return None

    def _request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.steps:
            raise AssertionError(f"unexpected request: {method} {url}")
        expected_method, expected_url, response = self.steps.pop(0)
        assert method == expected_method
        assert url == expected_url
        if callable(response):
            response = response(self)
        if response.on_return:
            response.on_return(self)
        return response


class FakeEmailService(BaseEmailService):
    def __init__(self, codes):
        super().__init__(EmailServiceType.TEMPMAIL)
        self.codes = list(codes)
        self.otp_requests = []

    def create_email(self, config=None):
        return {
            "email": "tester@example.com",
            "service_id": "mailbox-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        self.otp_requests.append({
            "email": email,
            "email_id": email_id,
            "timeout": timeout,
            "otp_sent_at": otp_sent_at,
        })
        if not self.codes:
            raise AssertionError("no verification code queued")
        return self.codes.pop(0)

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


class FakeOutlookEmailService(FakeEmailService):
    def __init__(self, codes):
        super().__init__(codes)
        self.service_type = EmailServiceType.OUTLOOK


class FakeCatchallEmailService(FakeEmailService):
    def create_email(self, config=None):
        return {
            "email": "tester@test.example.com",
            "inbox_email": "admin@catchall.example.com",
            "service_id": "mailbox-1",
        }


class FakeOAuthManager:
    def __init__(self):
        self.start_calls = 0
        self.callback_calls = []

    def start_oauth(self):
        self.start_calls += 1
        return OAuthStart(
            auth_url=f"https://auth.example.test/flow/{self.start_calls}",
            state=f"state-{self.start_calls}",
            code_verifier=f"verifier-{self.start_calls}",
            redirect_uri="http://localhost:1455/auth/callback",
        )

    def handle_callback(self, callback_url, expected_state, code_verifier):
        self.callback_calls.append({
            "callback_url": callback_url,
            "expected_state": expected_state,
            "code_verifier": code_verifier,
        })
        return {
            "account_id": "acct-1",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "id_token": "id-1",
        }


class FakeOpenAIClient:
    def __init__(self, sessions, sentinel_tokens):
        self._sessions = list(sessions)
        self._session_index = 0
        self._session = self._sessions[0]
        self._sentinel_tokens = list(sentinel_tokens)

    @property
    def session(self):
        return self._session

    def check_ip_location(self):
        return True, "US"

    def check_sentinel(self, did):
        if not self._sentinel_tokens:
            raise AssertionError("no sentinel token queued")
        return self._sentinel_tokens.pop(0)

    def close(self):
        if self._session_index + 1 < len(self._sessions):
            self._session_index += 1
            self._session = self._sessions[self._session_index]


def _workspace_cookie(workspace_id):
    payload = base64.urlsafe_b64encode(
        json.dumps({"workspaces": [{"id": workspace_id}]}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload}.sig"


def _response_with_did(did):
    return DummyResponse(
        status_code=200,
        text="ok",
        on_return=lambda session: session.cookies.__setitem__("oai-did", did),
    )


def _response_with_login_cookies(workspace_id="ws-1", session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie(workspace_id)
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def test_check_sentinel_sends_non_empty_pow(monkeypatch):
    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["sentinel"], DummyResponse(payload={"token": "sentinel-token"})),
    ])
    client = OpenAIHTTPClient()
    client._session = session

    monkeypatch.setattr(
        "src.core.http_client.build_sentinel_pow_token",
        lambda user_agent: "gAAAAACpow-token",
    )

    token = client.check_sentinel("device-1")

    assert token == "sentinel-token"
    body = json.loads(session.calls[0]["kwargs"]["data"])
    assert body["id"] == "device-1"
    assert body["flow"] == "authorize_continue"
    assert body["p"] == "gAAAAACpow-token"


def test_run_registers_then_relogs_to_fetch_token():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "register"
    assert result.workspace_id == "ws-1"
    assert result.session_token == "session-1"
    assert fake_oauth.start_calls == 2
    assert len(email_service.otp_requests) == 2
    assert all(item["otp_sent_at"] is not None for item in email_service.otp_requests)
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 1
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 0
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 1
    relogin_start_body = json.loads(session_two.calls[1]["kwargs"]["data"])
    assert relogin_start_body["screen_hint"] == "login"
    assert relogin_start_body["username"]["value"] == "tester@example.com"
    password_verify_body = json.loads(session_two.calls[2]["kwargs"]["data"])
    assert password_verify_body == {"password": result.password}
    assert result.metadata["token_acquired_via_relogin"] is True


def test_existing_account_login_uses_auto_sent_otp_without_manual_send():
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-existing", "session-existing")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-existing"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-existing",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["246810"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert fake_oauth.start_calls == 1
    assert sum(1 for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert len(email_service.otp_requests) == 1
    assert email_service.otp_requests[0]["otp_sent_at"] is not None
    assert result.metadata["token_acquired_via_relogin"] is False


def test_get_verification_code_uses_registration_email_for_catchall_inbox():
    email_service = FakeCatchallEmailService(["135790"])
    engine = RegistrationEngine(email_service)
    engine._otp_sent_at = 1234567890

    assert engine._create_email() is True
    code = engine._get_verification_code(timeout=12)

    assert code == "135790"
    assert engine.email == "tester@test.example.com"
    assert engine.inbox_email == "admin@catchall.example.com"
    assert email_service.otp_requests[0]["email"] == "tester@test.example.com"
    assert email_service.otp_requests[0]["email_id"] == "mailbox-1"


def test_get_verification_code_uses_settings_timeout_by_default(monkeypatch):
    email_service = FakeEmailService(["246810"])
    engine = RegistrationEngine(email_service)
    engine._otp_sent_at = 1234567890

    assert engine._create_email() is True

    monkeypatch.setattr(
        "src.core.register.get_settings",
        lambda: type("Settings", (), {"email_code_timeout": 77})(),
    )

    code = engine._get_verification_code()

    assert code == "246810"
    assert email_service.otp_requests[0]["timeout"] == 77


def test_verify_email_otp_with_retry_resends_once_when_no_code(monkeypatch):
    engine = RegistrationEngine(FakeEmailService([]))
    codes = iter([None, "246810"])
    resend_calls = []
    validated_codes = []

    monkeypatch.setattr(engine, "_get_verification_code", lambda timeout=None: next(codes))
    monkeypatch.setattr(
        engine,
        "_send_verification_code",
        lambda referer=None: resend_calls.append(referer) or True,
    )
    monkeypatch.setattr(
        engine,
        "_validate_verification_code",
        lambda code: validated_codes.append(code) or True,
    )

    ok = engine._verify_email_otp_with_retry(
        stage_label="注册验证码",
        max_attempts=3,
        resend_on_empty=True,
    )

    assert ok is True
    assert resend_calls == ["https://auth.openai.com/email-verification"]
    assert validated_codes == ["246810"]


def test_should_try_anyauto_fallback_covers_redirect_failure(monkeypatch):
    engine = RegistrationEngine(FakeEmailService([]))
    monkeypatch.setattr(
        "src.core.register.get_settings",
        lambda: type("Settings", (), {"registration_enable_anyauto_fallback": True})(),
    )

    result = RegistrationResult(success=False)
    result.error_message = "跟随重定向链失败"

    assert engine._should_try_anyauto_fallback(result) is True


def test_complete_token_exchange_outlook_uses_auth_session_fallback():
    engine = RegistrationEngine(FakeEmailService([]))
    engine.password = "Passw0rd!"
    engine.device_id = "did-1"
    engine._is_existing_account = False
    engine._last_validate_otp_workspace_id = ""
    engine._last_validate_otp_continue_url = "https://auth.openai.com/add-phone"
    engine._create_account_continue_url = "https://auth.openai.com/add-phone"
    engine._create_account_account_id = "acct-1"
    engine._create_account_workspace_id = "ws-1"
    engine._create_account_refresh_token = "refresh-1"
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.example.test/authorize",
        state="state-1",
        code_verifier="verifier-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    engine.session = type("Session", (), {"cookies": {"__Secure-next-auth.session-token": "session-1"}})()

    engine._verify_email_otp_with_retry = lambda **kwargs: True
    engine._get_workspace_id = lambda: ""
    engine._follow_redirects = lambda _url: ("", "https://chatgpt.com/")

    def fake_capture(result, access_hint=None):
        result.access_token = "access-1"
        result.session_token = "session-1"
        return True

    engine._capture_auth_session_tokens = fake_capture

    result = RegistrationResult(success=False)
    ok = engine._complete_token_exchange_outlook(result)

    assert ok is True
    assert result.access_token == "access-1"
    assert result.account_id == "acct-1"
    assert result.workspace_id == "ws-1"
    assert result.metadata["completion_path"] == "auth_session_fallback"
    assert result.metadata.get("token_pending") is not True


def test_complete_token_exchange_outlook_backfills_session_token_after_callback(monkeypatch):
    engine = RegistrationEngine(FakeOutlookEmailService([]))
    engine.oauth_manager = FakeOAuthManager()
    engine.password = "Passw0rd!"
    engine.device_id = "did-1"
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.example.test/authorize",
        state="state-1",
        code_verifier="verifier-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    engine.session = type("Session", (), {"cookies": {}})()

    monkeypatch.setattr(engine, "_verify_email_otp_with_retry", lambda **kwargs: True)
    monkeypatch.setattr(engine, "_get_workspace_id", lambda: "ws-1")
    monkeypatch.setattr(engine, "_select_workspace", lambda workspace_id: "https://auth.example.test/continue")
    monkeypatch.setattr(
        engine,
        "_follow_redirects",
        lambda _url: ("http://localhost:1455/auth/callback?code=code-1&state=state-1", "https://chatgpt.com/"),
    )
    capture_calls = []

    def fake_capture(result, access_hint=None):
        capture_calls.append(access_hint)
        result.session_token = "session-outlook"
        return True

    monkeypatch.setattr(engine, "_capture_auth_session_tokens", fake_capture)
    monkeypatch.setattr(engine, "_bootstrap_chatgpt_signin_for_session", lambda result: False)

    result = RegistrationResult(success=False)
    ok = engine._complete_token_exchange_outlook(result)

    assert ok is True
    assert result.access_token == "access-1"
    assert result.session_token == "session-outlook"
    assert capture_calls == ["access-1"]


def test_run_outlook_registration_marks_token_pending_when_account_already_created(monkeypatch):
    email_service = FakeOutlookEmailService(["111111", "222222"])
    engine = RegistrationEngine(email_service)
    engine.oauth_manager = FakeOAuthManager()

    monkeypatch.setattr(engine, "_check_ip_location", lambda: (True, "SG"))

    def fake_create_email():
        engine.email = "outlook@example.com"
        engine.inbox_email = "outlook@example.com"
        engine.email_info = {"service_id": "mailbox-1"}
        return True

    monkeypatch.setattr(engine, "_create_email", fake_create_email)

    def fake_prepare_authorize_flow(_label):
        engine.oauth_start = OAuthStart(
            auth_url="https://auth.example.test/authorize",
            state="state-1",
            code_verifier="verifier-1",
            redirect_uri="http://localhost:1455/auth/callback",
        )
        engine.session = type("Session", (), {"cookies": {}})()
        engine.device_id = "did-1"
        return "did-1", "sen-1"

    monkeypatch.setattr(engine, "_prepare_authorize_flow", fake_prepare_authorize_flow)
    monkeypatch.setattr(
        engine,
        "_submit_signup_form",
        lambda _did, _sen: type("SignupResult", (), {"success": True, "error_message": "", "page_type": "create_account_password"})(),
    )
    monkeypatch.setattr(engine, "_register_password_with_retry", lambda _did, _sen: (True, "Passw0rd!"))
    monkeypatch.setattr(engine, "_send_verification_code", lambda referer=None: True)
    monkeypatch.setattr(engine, "_verify_email_otp_with_retry", lambda **kwargs: True)

    def fake_create_user_account():
        engine._create_account_account_id = "acct-pending"
        engine._create_account_workspace_id = "ws-pending"
        engine._create_account_refresh_token = "refresh-pending"
        engine._create_account_continue_url = "https://auth.openai.com/add-phone"
        return True

    monkeypatch.setattr(engine, "_create_user_account", fake_create_user_account)
    monkeypatch.setattr(engine, "_restart_login_flow", lambda: (True, ""))
    monkeypatch.setattr(engine, "_get_workspace_id", lambda: "")
    monkeypatch.setattr(engine, "_follow_redirects", lambda _url: ("", "https://auth.example.test/authorize"))
    monkeypatch.setattr(engine, "_capture_auth_session_tokens", lambda result, access_hint=None: False)

    result = engine.run()

    assert result.success is True
    assert result.account_id == "acct-pending"
    assert result.workspace_id == "ws-pending"
    assert result.access_token == ""
    assert result.metadata["registration_entry_flow_effective"] == "outlook"
    assert result.metadata["completion_path"] == "account_created_pending"
    assert result.metadata["token_pending"] is True


def test_complete_token_exchange_native_backup_backfills_session_token_after_callback(monkeypatch):
    engine = RegistrationEngine(FakeEmailService([]))
    engine.oauth_manager = FakeOAuthManager()
    engine.password = "Passw0rd!"
    engine.device_id = "did-1"
    engine.oauth_start = OAuthStart(
        auth_url="https://auth.example.test/authorize",
        state="state-1",
        code_verifier="verifier-1",
        redirect_uri="http://localhost:1455/auth/callback",
    )
    engine.session = type("Session", (), {"cookies": {}})()

    monkeypatch.setattr(engine, "_verify_email_otp_with_retry", lambda **kwargs: True)
    monkeypatch.setattr(engine, "_get_workspace_id", lambda: "ws-1")
    monkeypatch.setattr(engine, "_select_workspace", lambda workspace_id: "https://auth.example.test/continue")
    monkeypatch.setattr(
        engine,
        "_follow_redirects",
        lambda _url: ("http://localhost:1455/auth/callback?code=code-1&state=state-1", "https://chatgpt.com/"),
    )
    capture_calls = []

    def fake_capture(result, access_hint=None):
        capture_calls.append(access_hint)
        result.session_token = "session-native"
        return True

    monkeypatch.setattr(engine, "_capture_auth_session_tokens", fake_capture)
    monkeypatch.setattr(engine, "_bootstrap_chatgpt_signin_for_session", lambda result: False)

    result = RegistrationResult(success=False)
    ok = engine._complete_token_exchange_native_backup(result)

    assert ok is True
    assert result.access_token == "access-1"
    assert result.session_token == "session-native"
    assert capture_calls == ["access-1"]


def test_bridge_login_for_session_token_prioritizes_chatgpt_callback_continue_url(monkeypatch):
    engine = RegistrationEngine(FakeOutlookEmailService([]))
    engine.email = "bridge@example.com"
    engine.password = "Passw0rd!"
    engine.device_id = "did-1"
    engine._last_validate_otp_continue_url = "https://chatgpt.com/api/auth/callback/openai?code=bridge-code"
    engine.session = type(
        "Session",
        (),
        {"cookies": type("Cookies", (dict,), {"set": lambda self, key, value, domain=None, path=None: self.__setitem__(key, value)})()},
    )()

    monkeypatch.setattr(engine, "_check_sentinel", lambda did: "sen-1")
    monkeypatch.setattr(
        engine,
        "_submit_login_start",
        lambda did, sen: type(
            "SignupResult",
            (),
            {
                "success": True,
                "page_type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"],
                "error_message": "",
            },
        )(),
    )
    monkeypatch.setattr(engine, "_verify_email_otp_with_retry", lambda **kwargs: True)
    monkeypatch.setattr(engine, "_warmup_chatgpt_session", lambda: None)
    monkeypatch.setattr(engine, "_select_workspace", lambda workspace_id: (_ for _ in ()).throw(AssertionError("workspace fallback should not run")))
    follow_calls = []
    capture_calls = []

    def fake_follow(url):
        follow_calls.append(url)
        return (url, "https://chatgpt.com/")

    def fake_capture(result, access_hint=None):
        capture_calls.append(access_hint)
        result.session_token = "session-bridge"
        return True

    monkeypatch.setattr(engine, "_follow_chatgpt_auth_redirects", fake_follow)
    monkeypatch.setattr(engine, "_capture_auth_session_tokens", fake_capture)

    result = RegistrationResult(success=False, access_token="access-1")
    ok = engine._bridge_login_for_session_token(result)

    assert ok is True
    assert result.session_token == "session-bridge"
    assert follow_calls == ["https://chatgpt.com/api/auth/callback/openai?code=bridge-code"]
    assert capture_calls == ["access-1"]
