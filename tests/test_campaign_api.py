from contextlib import closing
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore


class CampaignApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = CampaignStore(Path(self.directory.name) / "test.sqlite3")
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.directory.cleanup()

    def register(self, email, name="Jogador"):
        response = self.client.post(
            "/auth/register",
            json={
                "email": email,
                "displayName": name,
                "password": "safe-password-123",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return {"Authorization": f"Bearer {response.json()['accessToken']}"}

    def campaign(self, owner):
        response = self.client.post(
            "/campaigns", headers=owner, json={"name": "Pharos"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def invite(self, owner, campaign_id):
        response = self.client.post(f"/campaigns/{campaign_id}/invites", headers=owner)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_create_list_and_view_are_persistent_and_isolated(self):
        gm = self.register("gm@example.com", "Mestre")
        player = self.register("player@example.com")
        created = self.campaign(gm)
        self.assertEqual(created["role"], "gm")
        self.assertEqual(created["name"], "Pharos")
        self.assertEqual(self.client.get("/campaigns", headers=player).json(), [])
        self.assertEqual(self.client.get("/campaigns", headers=gm).json(), [created])
        self.assertEqual(
            self.client.get(f"/campaigns/{created['id']}", headers=player).status_code,
            403,
        )
        details = self.client.get(f"/campaigns/{created['id']}", headers=gm).json()
        self.assertEqual(details["members"][0]["name"], "Mestre")
        self.assertEqual(details["members"][0]["characterId"], None)
        self.assertEqual(
            CampaignStore(Path(self.directory.name) / "test.sqlite3").list_campaigns(
                self.store.user_for_token(gm["Authorization"].split()[1])["id"]
            )[0]["id"],
            created["id"],
        )

    def test_invite_join_and_link_character_have_role_boundaries(self):
        gm = self.register("gm@example.com", "Mestre")
        player = self.register("player@example.com", "Jogador")
        other = self.register("other@example.com", "Outro")
        campaign_id = self.campaign(gm)["id"]
        self.assertEqual(
            self.client.post(
                f"/campaigns/{campaign_id}/invites", headers=player
            ).status_code,
            403,
        )
        invite = self.invite(gm, campaign_id)
        self.assertIn("expiresAt", invite)
        joined = self.client.post(
            "/campaigns/join", headers=player, json={"inviteCode": invite["code"]}
        )
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertEqual(joined.json()["role"], "player")
        self.assertEqual(
            self.client.post(
                "/campaigns/join", headers=other, json={"inviteCode": invite["code"]}
            ).status_code,
            410,
        )
        linked = self.client.put(
            f"/campaigns/{campaign_id}/character",
            headers=player,
            json={"characterId": "stable-character-id"},
        )
        self.assertEqual(linked.status_code, 200, linked.text)
        self.assertEqual(linked.json()["characterId"], "stable-character-id")
        self.assertEqual(
            self.client.put(
                f"/campaigns/{campaign_id}/character",
                headers=gm,
                json={"characterId": "other"},
            ).status_code,
            403,
        )
        members = self.client.get(f"/campaigns/{campaign_id}", headers=gm).json()[
            "members"
        ]
        self.assertEqual({member["name"] for member in members}, {"Mestre", "Jogador"})

    def test_rejects_expired_invalid_and_duplicate_invites(self):
        gm = self.register("gm@example.com")
        player = self.register("player@example.com")
        campaign_id = self.campaign(gm)["id"]
        invite = self.invite(gm, campaign_id)
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE invites SET expires_at = 0")
        self.assertEqual(
            self.client.post(
                "/campaigns/join", headers=player, json={"inviteCode": invite["code"]}
            ).status_code,
            410,
        )
        self.assertEqual(
            self.client.post(
                "/campaigns/join", headers=player, json={"inviteCode": "unknown"}
            ).status_code,
            404,
        )
        active = self.invite(gm, campaign_id)
        self.assertEqual(
            self.client.post(
                "/campaigns/join", headers=player, json={"inviteCode": active["code"]}
            ).status_code,
            200,
        )
        second = self.invite(gm, campaign_id)
        self.assertEqual(
            self.client.post(
                "/campaigns/join", headers=player, json={"inviteCode": second["code"]}
            ).status_code,
            409,
        )

    def test_authentication_required_and_credentials_not_exposed(self):
        self.assertEqual(self.client.get("/campaigns").status_code, 401)
        self.assertEqual(
            self.client.post("/campaigns", json={"name": "X"}).status_code, 401
        )
        gm = self.register("gm@example.com")
        self.assertEqual(
            self.client.post(
                "/auth/register",
                json={
                    "email": "gm@example.com",
                    "displayName": "New",
                    "password": "safe-password-123",
                },
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                "/auth/login",
                json={
                    "email": "gm@example.com",
                    "password": "wrong-password",
                },
            ).status_code,
            401,
        )
        logged_in = self.client.post(
            "/auth/login",
            json={
                "email": "GM@EXAMPLE.COM",
                "password": "safe-password-123",
            },
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertEqual(
            self.client.get(
                "/campaigns",
                headers={"Authorization": f"Bearer {logged_in.json()['accessToken']}"},
            ).status_code,
            200,
        )
        self.assertNotEqual(
            logged_in.json()["accessToken"], gm["Authorization"].split()[1]
        )
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            self.assertNotIn(
                "safe-password-123",
                conn.execute("SELECT password_hash FROM users").fetchone()[0],
            )
            conn.execute("UPDATE tokens SET expires_at = 0")
        self.assertEqual(self.client.get("/campaigns", headers=gm).status_code, 401)

    def test_invalid_data_and_missing_campaign(self):
        gm = self.register("gm@example.com")
        self.assertEqual(
            self.client.post(
                "/campaigns", headers=gm, json={"name": "   "}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/campaigns/not-found", headers=gm).status_code, 404
        )

    def test_character_id_cannot_be_claimed_by_another_member(self):
        gm = self.register("gm@example.com")
        first = self.register("first@example.com")
        second = self.register("second@example.com")
        campaign_id = self.campaign(gm)["id"]
        for player in (first, second):
            code = self.invite(gm, campaign_id)["code"]
            self.assertEqual(
                self.client.post(
                    "/campaigns/join", headers=player, json={"inviteCode": code}
                ).status_code,
                200,
            )
        url = f"/campaigns/{campaign_id}/character"
        self.assertEqual(
            self.client.put(
                url, headers=first, json={"characterId": "hero-1"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                url, headers=second, json={"characterId": "hero-1"}
            ).status_code,
            409,
        )

    def test_password_spaces_are_not_silently_changed(self):
        registered = self.client.post(
            "/auth/register",
            json={
                "email": " player@example.com ",
                "displayName": " Player ",
                "password": "  safe-password-123  ",
            },
        )
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(
            self.client.post(
                "/auth/login",
                json={
                    "email": "player@example.com",
                    "password": "safe-password-123",
                },
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/auth/login",
                json={
                    "email": "player@example.com",
                    "password": "  safe-password-123  ",
                },
            ).status_code,
            200,
        )
