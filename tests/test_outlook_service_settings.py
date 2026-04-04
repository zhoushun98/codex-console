from src.services.outlook.service import OutlookService


class DummyOutlookSettings:
    outlook_provider_priority = ["graph_api", "imap_old", "imap_new"]
    outlook_health_failure_threshold = 8
    outlook_health_disable_duration = 150
    outlook_default_client_id = "client-default"


def test_outlook_service_uses_global_settings_defaults(monkeypatch):
    monkeypatch.setattr("src.services.outlook.service.get_settings", lambda: DummyOutlookSettings())

    service = OutlookService({
        "email": "tester@hotmail.com",
        "password": "secret",
    })

    assert [item.value for item in service.provider_priority] == [
        "graph_api",
        "imap_old",
        "imap_new",
    ]
    assert service.provider_config.health_failure_threshold == 8
    assert service.provider_config.health_disable_duration == 150
    assert service.accounts[0].client_id == "client-default"


def test_outlook_service_keeps_explicit_provider_config(monkeypatch):
    monkeypatch.setattr("src.services.outlook.service.get_settings", lambda: DummyOutlookSettings())

    service = OutlookService({
        "email": "tester@hotmail.com",
        "password": "secret",
        "provider_priority": ["imap_new"],
        "health_failure_threshold": 3,
        "health_disable_duration": 45,
        "client_id": "explicit-client",
    })

    assert [item.value for item in service.provider_priority] == ["imap_new"]
    assert service.provider_config.health_failure_threshold == 3
    assert service.provider_config.health_disable_duration == 45
    assert service.accounts[0].client_id == "explicit-client"
