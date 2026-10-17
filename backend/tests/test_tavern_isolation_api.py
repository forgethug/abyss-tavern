class TestTavernIsolationApi:
    def test_admin_creates_custom_named_tavern_without_public_zone_number(
        self,
        client,
        make_user,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        response = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "灰烬与星火", "slug": "ash-and-ember"},
        )
        assert response.status_code == 201
        assert response.json["data"]["display_name"] == "灰烬与星火"
        assert "1区" not in str(response.json["data"])
        assert response.json["data"]["runtime_namespace"]
        assert response.json["data"]["is_public"] is False
        assert response.json["data"]["allow_self_join"] is False

    def test_two_taverns_keep_members_isolated(self, client, make_user):
        admin_id, admin_headers = make_user("admin", is_admin=True)
        alice_id, alice_headers = make_user("alice")
        bob_id, bob_headers = make_user("bob")

        first = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "月下旧港", "slug": "moon-harbor"},
        ).json["data"]
        second = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "无光回廊", "slug": "lightless-hall"},
        ).json["data"]

        assert client.post(
            f"/api/admin/taverns/{first['id']}/members",
            headers=admin_headers,
            json={"user_id": alice_id, "role": "member"},
        ).status_code == 200
        assert client.post(
            f"/api/admin/taverns/{second['id']}/members",
            headers=admin_headers,
            json={"user_id": bob_id, "role": "member"},
        ).status_code == 200

        alice_list = client.get("/api/taverns", headers=alice_headers).json["data"]
        bob_list = client.get("/api/taverns", headers=bob_headers).json["data"]
        assert [row["slug"] for row in alice_list] == ["moon-harbor"]
        assert [row["slug"] for row in bob_list] == ["lightless-hall"]

        assert client.get(
            "/api/taverns/moon-harbor/context",
            headers=alice_headers,
        ).status_code == 200
        assert client.get(
            "/api/taverns/lightless-hall/context",
            headers=alice_headers,
        ).status_code == 403
        assert client.get(
            "/api/taverns/moon-harbor/context",
            headers=bob_headers,
        ).status_code == 403

        assert admin_id != alice_id != bob_id

    def test_disabled_tavern_cannot_be_entered(self, client, make_user):
        _, admin_headers = make_user("admin", is_admin=True)
        alice_id, alice_headers = make_user("alice")
        tavern = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={"display_name": "暂停营业", "slug": "closed"},
        ).json["data"]
        client.post(
            f"/api/admin/taverns/{tavern['id']}/members",
            headers=admin_headers,
            json={"user_id": alice_id},
        )
        client.put(
            f"/api/admin/taverns/{tavern['id']}",
            headers=admin_headers,
            json={"status": "disabled"},
        )

        response = client.get(
            "/api/taverns/closed/context",
            headers=alice_headers,
        )
        assert response.status_code == 404

    def test_public_tavern_is_discoverable_but_not_enterable_without_membership(
        self,
        client,
        make_user,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        _, player_headers = make_user("observer")
        created = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={
                "display_name": "公开观察站",
                "slug": "public-observation",
                "is_public": True,
                "allow_self_join": False,
            },
        )
        assert created.status_code == 201

        listed = client.get("/api/taverns", headers=player_headers)
        assert listed.status_code == 200
        assert listed.json["data"][0]["slug"] == "public-observation"
        assert listed.json["data"][0]["membership_role"] is None
        assert listed.json["data"][0]["can_self_join"] is False
        assert client.get(
            "/api/taverns/public-observation/context",
            headers=player_headers,
        ).status_code == 403
        assert client.post(
            "/api/taverns/public-observation/join",
            headers=player_headers,
        ).status_code == 403

    def test_player_can_self_join_public_tavern_idempotently(
        self,
        client,
        make_user,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        _, player_headers = make_user("joiner")
        created = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={
                "display_name": "自由报名站",
                "slug": "open-enrollment",
                "is_public": True,
                "allow_self_join": True,
            },
        )
        assert created.status_code == 201

        before = client.get("/api/taverns", headers=player_headers).json["data"][0]
        assert before["can_self_join"] is True
        assert before["membership_role"] is None

        first = client.post(
            "/api/taverns/open-enrollment/join",
            headers=player_headers,
        )
        second = client.post(
            "/api/taverns/open-enrollment/join",
            headers=player_headers,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json["data"]["membership_role"] == "member"
        assert client.get(
            "/api/taverns/open-enrollment/context",
            headers=player_headers,
        ).status_code == 200

        after = client.get("/api/taverns", headers=player_headers).json["data"]
        assert len(after) == 1
        assert after[0]["membership_role"] == "member"
        assert after[0]["can_self_join"] is False

    def test_self_join_requires_public_visibility_and_private_update_disables_it(
        self,
        client,
        make_user,
    ):
        _, admin_headers = make_user("admin", is_admin=True)
        invalid = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={
                "display_name": "错误设置",
                "slug": "invalid-access",
                "is_public": False,
                "allow_self_join": True,
            },
        )
        assert invalid.status_code == 400

        created = client.post(
            "/api/admin/taverns",
            headers=admin_headers,
            json={
                "display_name": "临时公开站",
                "slug": "temporary-public",
                "is_public": True,
                "allow_self_join": True,
            },
        ).json["data"]
        updated = client.put(
            f"/api/admin/taverns/{created['id']}",
            headers=admin_headers,
            json={"is_public": False},
        )
        assert updated.status_code == 200
        assert updated.json["data"]["is_public"] is False
        assert updated.json["data"]["allow_self_join"] is False
