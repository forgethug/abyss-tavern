class TestAuthApi:
    def test_register_login_and_me(self, client):
        register = client.post(
            "/api/auth/register",
            json={
                "username": "alice",
                "password": "secret123",
                "display_name": "爱丽丝",
            },
        )
        assert register.status_code == 201
        assert register.json["data"]["user"]["display_name"] == "爱丽丝"
        assert register.json["data"]["user"]["player_id"] == 1001

        login = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "secret123"},
        )
        assert login.status_code == 200
        token = login.json["data"]["token"]

        me = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me.status_code == 200
        assert me.json["data"]["username"] == "alice"
        assert me.json["data"]["player_id"] == 1001
        assert "password_hash" not in me.json["data"]

    def test_duplicate_username_is_rejected(self, client):
        payload = {"username": "alice", "password": "secret123"}
        assert client.post("/api/auth/register", json=payload).status_code == 201
        duplicate = client.post("/api/auth/register", json=payload)
        assert duplicate.status_code == 409

    def test_invalid_login_is_rejected(self, client):
        response = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "wrong-password"},
        )
        assert response.status_code == 401
