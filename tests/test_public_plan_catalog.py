from app.models.billing import Plan


async def test_public_plan_catalog_is_available_without_authentication(api_client, db_session):
    db_session.add(
        Plan(
            slug="korusfono_pro_monthly",
            name="Plano Público",
            description="Descrição pública",
            limits={},
            price_cents=12300,
            currency="BRL",
            billing_interval="monthly",
            features=["Recurso real"],
            highlighted=True,
            display_order=1,
            is_active=True,
        )
    )
    await db_session.commit()

    response = await api_client.get("/api/v1/billing/plans")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": response.json()[0]["id"],
            "slug": "korusfono_pro_monthly",
            "name": "Plano Público",
            "description": "Descrição pública",
            "limits": {},
            "priceCents": 12300,
            "currency": "BRL",
            "billingInterval": "monthly",
            "features": ["Recurso real"],
            "badge": None,
            "highlighted": True,
            "displayOrder": 1,
            "isActive": True,
        }
    ]
