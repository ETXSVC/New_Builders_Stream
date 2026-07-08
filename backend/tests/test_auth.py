async def test_register_creates_company_and_admin_user(client):
    response = await client.post(
        "/auth/register",
        json={
            "company_name": "Acme Construction",
            "admin_full_name": "Ada Lovelace",
            "admin_email": "ada@acme.test",
            "admin_password": "supersecret123",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "ada@acme.test"
    assert "company_id" in body
    assert "user_id" in body


async def test_register_rejects_duplicate_email(client):
    payload = {
        "company_name": "Acme Construction",
        "admin_full_name": "Ada Lovelace",
        "admin_email": "ada@acme.test",
        "admin_password": "supersecret123",
    }
    first = await client.post("/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/auth/register", json={**payload, "company_name": "Beta Builders"})
    assert second.status_code == 409


async def test_login_returns_token_for_valid_credentials(client):
    await client.post(
        "/auth/register",
        json={
            "company_name": "Acme Construction",
            "admin_full_name": "Ada Lovelace",
            "admin_email": "ada@acme.test",
            "admin_password": "supersecret123",
        },
    )

    response = await client.post("/auth/login", json={"email": "ada@acme.test", "password": "supersecret123"})
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20
    assert "default_company_id" in body


async def test_login_rejects_wrong_password(client):
    await client.post(
        "/auth/register",
        json={
            "company_name": "Acme Construction",
            "admin_full_name": "Ada Lovelace",
            "admin_email": "ada@acme.test",
            "admin_password": "supersecret123",
        },
    )

    response = await client.post("/auth/login", json={"email": "ada@acme.test", "password": "wrong"})
    assert response.status_code == 401


async def test_login_rejects_unknown_email(client):
    response = await client.post("/auth/login", json={"email": "nobody@nowhere.test", "password": "whatever123"})
    assert response.status_code == 401
