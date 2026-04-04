from src.core.anyauto.chatgpt_client import ChatGPTClient


class DummySkyMailClient:
    def __init__(self, codes):
        self.codes = list(codes)
        self.calls = []

    def wait_for_verification_code(self, email, timeout=60, otp_sent_at=None, exclude_codes=None):
        self.calls.append({
            "email": email,
            "timeout": timeout,
            "otp_sent_at": otp_sent_at,
            "exclude_codes": exclude_codes,
        })
        if not self.codes:
            return None
        return self.codes.pop(0)


def test_wait_for_email_otp_with_single_resend(monkeypatch):
    client = ChatGPTClient.__new__(ChatGPTClient)
    client.verbose = False
    client._log = lambda _msg: None

    send_calls = []
    skymail_client = DummySkyMailClient([None, "135790"])

    monkeypatch.setattr(client, "send_email_otp", lambda: send_calls.append(True) or True)
    monkeypatch.setattr("src.core.anyauto.chatgpt_client.time.time", lambda: 222.0)

    otp_code = client._wait_for_email_otp_with_single_resend(
        "tester@example.com",
        skymail_client,
        timeout=60,
        otp_sent_at=111.0,
    )

    assert otp_code == "135790"
    assert len(send_calls) == 1
    assert skymail_client.calls == [
        {
            "email": "tester@example.com",
            "timeout": 60,
            "otp_sent_at": 111.0,
            "exclude_codes": None,
        },
        {
            "email": "tester@example.com",
            "timeout": 60,
            "otp_sent_at": 222.0,
            "exclude_codes": None,
        },
    ]
