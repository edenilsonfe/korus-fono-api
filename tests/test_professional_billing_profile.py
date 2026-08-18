import pytest


@pytest.mark.asyncio
async def test_me_exposes_and_updates_billing_profile(
    api_client,
    professional,
    auth_headers,
):
    initial = await api_client.get("/api/v1/me", headers=auth_headers)

    assert initial.status_code == 200
    assert initial.json()["billingProfileComplete"] is False
    assert initial.json()["billingAddress"] == ""
    assert initial.json()["billingPostalCode"] == ""

    updated = await api_client.patch(
        "/api/v1/me",
        headers=auth_headers,
        json={
            "billingAddress": "Rua das Flores",
            "billingAddressNumber": "123",
            "billingAddressComplement": "Sala 4",
            "billingProvince": "Centro",
            "billingPostalCode": "01310-100",
        },
    )

    assert updated.status_code == 200
    assert updated.json()["billingAddress"] == "Rua das Flores"
    assert updated.json()["billingAddressNumber"] == "123"
    assert updated.json()["billingAddressComplement"] == "Sala 4"
    assert updated.json()["billingProvince"] == "Centro"
    assert updated.json()["billingPostalCode"] == "01310100"
    assert updated.json()["billingProfileComplete"] is True


@pytest.mark.asyncio
async def test_me_keeps_billing_profile_incomplete_with_invalid_phone_or_postal_code(
    api_client,
    professional,
    auth_headers,
):
    professional.phone = "1199"

    updated = await api_client.patch(
        "/api/v1/me",
        headers=auth_headers,
        json={
            "billingAddress": "Rua das Flores",
            "billingAddressNumber": "123",
            "billingProvince": "Centro",
            "billingPostalCode": "01310",
        },
    )

    assert updated.status_code == 200
    assert updated.json()["billingProfileComplete"] is False
