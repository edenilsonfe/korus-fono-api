"""Tests for transactional email HTML escaping."""

from app.services.email.templates import (
    password_reset_email,
    school_report_delivery_email,
    trial_expiration_email,
)


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


def test_school_report_delivery_email_is_minimized_and_escapes_user_data():
    rendered = school_report_delivery_email(
        professional_name='Dra. <Ana> & "Cia"',
        school_name='Escola "Vila" <img src=x onerror=alert(1)>',
        school_recipient_name="Coordenação <Pedagógica>",
        delivery_url="https://app.example.com/relatorio/token-abc123",
        expires_days=30,
    )

    # Generic subject: no patient name, no diagnosis.
    assert rendered.subject == "Documento escolar disponível"
    assert "<" not in rendered.subject

    # User-provided values are escaped in HTML...
    assert "<img" not in rendered.html
    assert "&lt;img" in rendered.html
    assert "&lt;Ana&gt;" in rendered.html
    assert "&quot;Vila&quot;" in rendered.html
    assert "&lt;Pedagógica&gt;" in rendered.html
    # ...while the plain-text variant keeps them readable.
    assert "Escola \"Vila\"" in rendered.text
    assert "Coordenação <Pedagógica>" in rendered.text

    assert 'href="https://app.example.com/relatorio/token-abc123"' in rendered.html
    assert "https://app.example.com/relatorio/token-abc123" in rendered.text
    assert "30 dias" in rendered.text
    assert "30 dias" in rendered.html
    # Minimized copy: nothing clinical in the message.
    assert "diagn" not in rendered.html.lower()
    assert "diagn" not in rendered.text.lower()
    assert "João" not in rendered.html
    assert "João" not in rendered.subject
