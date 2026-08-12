"""Tests for transactional email HTML escaping."""

from app.services.email.templates import password_reset_email, trial_expiration_email


def test_password_reset_email_plain_name_ok():
    rendered = password_reset_email(
        user_name="Ana",
        reset_url="https://app.example.com/reset?token=abc",
        expires_minutes=30,
    )
    assert "Olá Ana," in rendered.html
    assert "Olá Ana," in rendered.text
    assert 'href="https://app.example.com/reset?token=abc"' in rendered.html
    assert "https://app.example.com/reset?token=abc" in rendered.text


def test_password_reset_email_escapes_html_injection_in_name():
    malicious = 'Ana<img src=x onerror=alert(1)>'
    reset_url = "https://app.example.com/reset?token=abc"
    rendered = password_reset_email(
        user_name=malicious,
        reset_url=reset_url,
        expires_minutes=30,
    )

    assert "<img" not in rendered.html
    assert "&lt;img" in rendered.html
    assert "&gt;" in rendered.html

    # Plain text keeps the raw name readable
    assert malicious in rendered.text
    assert reset_url in rendered.text


def test_trial_expiration_email_has_audience_specific_copy_and_escapes_name():
    expiring = trial_expiration_email(
        user_name="<Dra. Ana>",
        audience="expiring_soon",
        trial_ends_at="15/08/2026",
        plans_url="https://korusfono.com.br/planos",
    )
    expired = trial_expiration_email(
        user_name="Dra. Ana",
        audience="expired",
        trial_ends_at="10/08/2026",
        plans_url="https://korusfono.com.br/planos",
    )

    assert "está terminando" in expiring.subject.lower()
    assert "15/08/2026" in expiring.text
    assert "&lt;Dra. Ana&gt;" in expiring.html
    assert "terminou" in expired.subject.lower()
    assert "10/08/2026" in expired.text
