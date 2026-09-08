"""HTTP tests for AI conversation CRUD (title update + delete) and the
slim list / detail split (plan 009): list never loads messages, detail
does, and cross-tenant access to another professional's conversation 404s.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import utcnow
from app.core.security import hash_password
from app.models.ai import ChatMessage, Conversation
from app.models.professional import Professional


@pytest.fixture
async def conversation(db_session: AsyncSession, professional: Professional):
    conv = Conversation(
        professional_id=professional.id,
        title="Conversa original",
    )
    db_session.add(conv)
    await db_session.commit()
    await db_session.refresh(conv)
    return conv


@pytest.fixture
async def other_professional(db_session: AsyncSession):
    pro = Professional(
        email_verified_at=utcnow(),
        email="outra-profissional@example.com",
        password_hash=hash_password("testpass123"),
        name="Dra. Outra",
        specialty_key="fono",
        specialty="Fonoaudiologia",
        council="CREFITO",
        phone="11988880000",
    )
    db_session.add(pro)
    await db_session.commit()
    await db_session.refresh(pro)
    return pro


async def test_create_conversation(api_client: AsyncClient, auth_headers: dict):
    resp = await api_client.post(
        "/api/v1/ai/conversations",
        headers=auth_headers,
        json={"title": "Nova conversa"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Nova conversa"
    assert data["messages"] == []
    assert data["id"]


async def test_update_conversation_title(
    api_client: AsyncClient,
    auth_headers: dict,
    conversation: Conversation,
):
    resp = await api_client.patch(
        f"/api/v1/ai/conversations/{conversation.id}",
        headers=auth_headers,
        json={"title": "Novo título"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "Novo título"
    assert data["id"] == str(conversation.id)


async def test_update_conversation_rejects_empty_title(
    api_client: AsyncClient,
    auth_headers: dict,
    conversation: Conversation,
):
    resp = await api_client.patch(
        f"/api/v1/ai/conversations/{conversation.id}",
        headers=auth_headers,
        json={"title": "   "},
    )
    assert resp.status_code == 400


async def test_delete_conversation(
    api_client: AsyncClient,
    auth_headers: dict,
    conversation: Conversation,
):
    resp = await api_client.delete(
        f"/api/v1/ai/conversations/{conversation.id}",
        headers=auth_headers,
    )
    assert resp.status_code == 204

    list_resp = await api_client.get("/api/v1/ai/conversations", headers=auth_headers)
    assert list_resp.status_code == 200
    ids = [c["id"] for c in list_resp.json()]
    assert str(conversation.id) not in ids


async def test_delete_conversation_not_found(api_client: AsyncClient, auth_headers: dict):
    resp = await api_client.delete(
        "/api/v1/ai/conversations/00000000-0000-0000-0000-000000000099",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_list_conversations_returns_empty_messages(
    api_client: AsyncClient,
    auth_headers: dict,
    conversation: Conversation,
    db_session: AsyncSession,
):
    db_session.add(
        ChatMessage(conversation_id=conversation.id, role="user", content="Olá")
    )
    await db_session.commit()

    resp = await api_client.get("/api/v1/ai/conversations", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    listed = next(c for c in data if c["id"] == str(conversation.id))
    assert listed["messages"] == []


async def test_get_conversation_returns_message_history(
    api_client: AsyncClient,
    auth_headers: dict,
    conversation: Conversation,
    db_session: AsyncSession,
):
    db_session.add(
        ChatMessage(conversation_id=conversation.id, role="user", content="Olá")
    )
    await db_session.commit()

    resp = await api_client.get(
        f"/api/v1/ai/conversations/{conversation.id}", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(conversation.id)
    assert len(data["messages"]) == 1
    assert data["messages"][0]["content"] == "Olá"


async def test_get_conversation_not_found(api_client: AsyncClient, auth_headers: dict):
    resp = await api_client.get(
        "/api/v1/ai/conversations/00000000-0000-0000-0000-000000000099",
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_get_conversation_cross_tenant_returns_404(
    api_client: AsyncClient,
    conversation: Conversation,
    other_professional: Professional,
):
    from app.core.security import create_access_token

    other_headers = {
        "Authorization": f"Bearer {create_access_token(other_professional.id)}"
    }
    resp = await api_client.get(
        f"/api/v1/ai/conversations/{conversation.id}", headers=other_headers
    )
    assert resp.status_code == 404
