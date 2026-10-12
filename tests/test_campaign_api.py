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

    def test_registration_accepts_eight_characters_but_rejects_seven(self):
        def register_with(password, email):
            return self.client.post(
                "/auth/register",
                json={"email": email, "displayName": "Jogador", "password": password},
            )

        self.assertEqual(register_with("1234567", "short@example.com").status_code, 422)
        registered = register_with("12345678", "valid@example.com")
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(
            self.client.post(
                "/auth/login",
                json={"email": "valid@example.com", "password": "12345678"},
            ).status_code,
            200,
        )

    def test_table_login_uses_fixed_names_and_keeps_new_accounts_separate(self):
        roster = (
            "Gabriel",
            "Leandro",
            "Yago",
            "Luiz",
            "Amom",
            "Victor",
            "Wayne",
            "Visitante",
        )
        for name in roster:
            response = self.client.post(
                "/auth/table-login", json={"name": name, "pin": name.lower()}
            )
            self.assertEqual(response.status_code, 200, name)
            token = response.json()["accessToken"]
            self.assertEqual(self.store.user_for_token(token)["display_name"], name)
            self.assertEqual(
                self.client.get(
                    "/campaigns", headers={"Authorization": f"Bearer {token}"}
                ).json(),
                [],
            )
        for name, pin in (
            ("Victor", "wrong"),
            ("Outro", "outro"),
            ("Visitante", "VISITANTE"),
        ):
            self.assertEqual(
                self.client.post(
                    "/auth/table-login", json={"name": name, "pin": pin}
                ).status_code,
                401,
            )

        victor = self.client.post(
            "/auth/table-login", json={"name": "Victor", "pin": "victor"}
        ).json()["accessToken"]
        campaign_id = self.campaign({"Authorization": f"Bearer {victor}"})["id"]
        returning = self.client.post(
            "/auth/table-login", json={"name": "Victor", "pin": "victor"}
        ).json()["accessToken"]
        self.assertEqual(
            self.client.get(
                "/campaigns", headers={"Authorization": f"Bearer {returning}"}
            ).json()[0]["id"],
            campaign_id,
        )

    def test_table_accounts_do_not_reuse_existing_email_accounts(self):
        old_account = self.register("old@example.com", "Victor")
        table_token = self.client.post(
            "/auth/table-login", json={"name": "Victor", "pin": "victor"}
        ).json()["accessToken"]
        self.assertNotEqual(
            self.store.user_for_token(old_account["Authorization"].split()[1])["id"],
            self.store.user_for_token(table_token)["id"],
        )

    def test_only_victor_can_create_campaign_in_table_mode(self):
        player_token = self.client.post(
            "/auth/table-login", json={"name": "Gabriel", "pin": "gabriel"}
        ).json()["accessToken"]
        response = self.client.post(
            "/campaigns",
            json={"name": "Mesa"},
            headers={"Authorization": f"Bearer {player_token}"},
        )
        self.assertEqual(response.status_code, 403)

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

    def test_journal_visibility_and_access_are_enforced_by_server(self):
        gm = self.register("gm@example.com", "Mestre")
        player = self.register("player@example.com", "Jogador")
        outsider = self.register("outsider@example.com", "Estranho")
        campaign_id = self.campaign(gm)["id"]
        code = self.invite(gm, campaign_id)["code"]
        self.client.post("/campaigns/join", headers=player, json={"inviteCode": code})
        url = f"/campaigns/{campaign_id}/entries"

        secret = self.client.post(
            url,
            headers=gm,
            json={
                "title": "Segredo",
                "body": "Revelação escondida",
                "visibility": "gm",
            },
        )
        self.assertEqual(secret.status_code, 201, secret.text)
        public = self.client.post(
            url,
            headers=gm,
            json={
                "title": "Recapitulação",
                "body": "O grupo chegou à cidade",
                "visibility": "party",
            },
        )
        self.assertEqual(public.status_code, 201, public.text)
        self.assertEqual(secret.json()["visibility"], "gm")
        self.assertIn("createdAt", public.json())
        self.assertEqual(len(self.client.get(url, headers=gm).json()), 2)
        player_entries = self.client.get(url, headers=player).json()
        self.assertEqual(
            [entry["title"] for entry in player_entries], ["Recapitulação"]
        )
        self.assertNotIn("Revelação escondida", str(player_entries))
        self.assertEqual(self.client.get(url, headers=outsider).status_code, 403)
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(
            self.client.post(
                url,
                headers=player,
                json={
                    "title": "Outro",
                    "body": "Tentativa",
                    "visibility": "gm",
                },
            ).status_code,
            403,
        )

    def test_journal_edit_restricts_role_and_visibility_changes(self):
        gm = self.register("gm@example.com")
        player = self.register("player@example.com")
        campaign_id = self.campaign(gm)["id"]
        code = self.invite(gm, campaign_id)["code"]
        self.client.post("/campaigns/join", headers=player, json={"inviteCode": code})
        url = f"/campaigns/{campaign_id}/entries"
        entry = self.client.post(
            url,
            headers=gm,
            json={
                "title": "Nota",
                "body": "Segredo",
                "visibility": "gm",
            },
        ).json()
        edit_url = f"{url}/{entry['id']}"
        changes = {"title": "Resumo", "body": "Revelado", "visibility": "party"}
        self.assertEqual(
            self.client.put(edit_url, headers=player, json=changes).status_code, 403
        )
        self.assertEqual(
            self.client.put(
                edit_url,
                headers=gm,
                json={"title": "  ", "body": "x", "visibility": "party"},
            ).status_code,
            422,
        )
        edited = self.client.put(edit_url, headers=gm, json=changes)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(
            self.client.get(url, headers=player).json()[0]["body"], "Revelado"
        )
        self.assertEqual(
            self.client.put(f"{url}/missing", headers=gm, json=changes).status_code, 404
        )
        other_campaign_id = self.campaign(gm)["id"]
        self.assertEqual(
            self.client.put(
                f"/campaigns/{other_campaign_id}/entries/{entry['id']}",
                headers=gm,
                json=changes,
            ).status_code,
            404,
        )

    def test_logout_revokes_only_the_current_token(self):
        first = self.register("gm@example.com")
        second_response = self.client.post(
            "/auth/login",
            json={
                "email": "gm@example.com",
                "password": "safe-password-123",
            },
        )
        second = {"Authorization": f"Bearer {second_response.json()['accessToken']}"}
        self.assertEqual(self.client.post("/auth/logout").status_code, 401)
        logged_out = self.client.post("/auth/logout", headers=first)
        self.assertEqual(logged_out.status_code, 204, logged_out.text)
        self.assertEqual(self.client.get("/campaigns", headers=first).status_code, 401)
        self.assertEqual(self.client.get("/campaigns", headers=second).status_code, 200)
        self.assertEqual(
            self.client.post("/auth/logout", headers=first).status_code, 401
        )

    def test_journal_delete_restricts_role_and_removes_entry(self):
        gm = self.register("gm@example.com")
        player = self.register("player@example.com")
        campaign_id = self.campaign(gm)["id"]
        code = self.invite(gm, campaign_id)["code"]
        self.client.post("/campaigns/join", headers=player, json={"inviteCode": code})
        url = f"/campaigns/{campaign_id}/entries"
        entry = self.client.post(
            url,
            headers=gm,
            json={
                "title": "Aviso",
                "body": "Texto a ser removido",
                "visibility": "party",
            },
        ).json()
        entry_url = f"{url}/{entry['id']}"

        self.assertEqual(
            self.client.delete(entry_url, headers=player).status_code, 403
        )
        self.assertEqual(
            self.client.delete(f"{url}/missing", headers=gm).status_code, 404
        )

        deleted = self.client.delete(entry_url, headers=gm)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(url, headers=gm).json(), [])
        self.assertEqual(self.client.get(url, headers=player).json(), [])

        self.assertEqual(
            self.client.delete(entry_url, headers=gm).status_code, 404
        )

    def test_unlink_and_change_character_for_player(self):
        gm = self.register("gm@example.com")
        player = self.register("player@example.com")
        campaign_id = self.campaign(gm)["id"]
        code = self.invite(gm, campaign_id)["code"]
        self.client.post("/campaigns/join", headers=player, json={"inviteCode": code})

        char_url = f"/campaigns/{campaign_id}/character"
        link1 = self.client.put(char_url, headers=player, json={"characterId": "hero-1"})
        self.assertEqual(link1.status_code, 200)
        self.assertEqual(link1.json()["characterId"], "hero-1")

        link2 = self.client.put(char_url, headers=player, json={"characterId": "hero-2"})
        self.assertEqual(link2.status_code, 200)
        self.assertEqual(link2.json()["characterId"], "hero-2")

        self.assertEqual(
            self.client.delete(char_url, headers=gm).status_code, 403
        )

        unlinked = self.client.delete(char_url, headers=player)
        self.assertEqual(unlinked.status_code, 200)
        self.assertIsNone(unlinked.json()["characterId"])

        campaign = self.client.get(f"/campaigns/{campaign_id}", headers=player).json()
        self.assertIsNone(campaign["characterId"])

