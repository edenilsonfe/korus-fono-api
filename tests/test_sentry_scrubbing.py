"""before_send must strip secrets and clinical PII before events leave the API."""

from app.services.sentry_scrubbing import scrub_sentry_event


def test_scrub_removes_authorization_header():
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret-token",
                "Content-Type": "application/json",
            }
        }
    }
    scrubbed = scrub_sentry_event(event, {})
    assert scrubbed is not None
    assert scrubbed["request"]["headers"]["Authorization"] == "[Filtered]"
    assert scrubbed["request"]["headers"]["Content-Type"] == "application/json"


def test_scrub_removes_cookie_and_set_cookie():
    event = {
        "request": {
            "headers": {
                "Cookie": "session=abc",
                "Set-Cookie": "session=abc",
            }
        }
    }
    scrubbed = scrub_sentry_event(event, {})
    assert scrubbed["request"]["headers"]["Cookie"] == "[Filtered]"
    assert scrubbed["request"]["headers"]["Set-Cookie"] == "[Filtered]"


def test_scrub_drops_request_body():
    event = {"request": {"data": {"cpf": "123", "notes": "prontuario"}}}
    scrubbed = scrub_sentry_event(event, {})
    assert "data" not in scrubbed["request"]


def test_scrub_strips_email_from_user():
    event = {"user": {"id": "prof-1", "email": "camila@example.com", "username": "camila"}}
    scrubbed = scrub_sentry_event(event, {})
    assert scrubbed["user"] == {"id": "prof-1"}


def test_scrub_redacts_sensitive_extra_keys():
    event = {
        "extra": {
            "password": "x",
            "jwt_secret": "y",
            "asaas_api_key": "z",
            "billing_document": "11222333000181",
            "billingDocument": "52998224725",
            "billing_cnpj": "11222333000181",
            "billingCnpj": "11222333000181",
            "safe": "ok",
        }
    }
    scrubbed = scrub_sentry_event(event, {})
    assert scrubbed["extra"]["password"] == "[Filtered]"
    assert scrubbed["extra"]["jwt_secret"] == "[Filtered]"
    assert scrubbed["extra"]["asaas_api_key"] == "[Filtered]"
    assert scrubbed["extra"]["billing_document"] == "[Filtered]"
    assert scrubbed["extra"]["billingDocument"] == "[Filtered]"
    assert scrubbed["extra"]["billing_cnpj"] == "[Filtered]"
    assert scrubbed["extra"]["billingCnpj"] == "[Filtered]"
    assert scrubbed["extra"]["safe"] == "ok"


def test_scrub_returns_event_when_request_missing():
    event = {"message": "boom"}
    assert scrub_sentry_event(event, {}) == {"message": "boom"}
