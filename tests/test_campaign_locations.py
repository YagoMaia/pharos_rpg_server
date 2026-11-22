import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore


class CampaignLocationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_locations.sqlite3"
        self.store = CampaignStore(self.db_path)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

        self.gm_token = self.client.post(
            "/auth/table-login", json={"name": "Victor", "pin": "victor"}
        ).json()["accessToken"]
        self.p1_token = self.client.post(
            "/auth/table-login", json={"name": "Gabriel", "pin": "gabriel"}
        ).json()["accessToken"]
        self.p2_token = self.client.post(
            "/auth/table-login", json={"name": "Leandro", "pin": "leandro"}
        ).json()["accessToken"]

        camp = self.client.post(
            "/campaigns",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Terras de Pharos"},
        ).json()
        self.campaign_id = camp["id"]

        for tok in (self.p1_token, self.p2_token):
            inv = self.client.post(
                f"/campaigns/{self.campaign_id}/invites",
                headers={"Authorization": f"Bearer {self.gm_token}"},
            ).json()
            self.client.post(
                "/campaigns/join",
                headers={"Authorization": f"Bearer {tok}"},
                json={"inviteCode": inv["code"]},
            )

        details = self.client.get(
            f"/campaigns/{self.campaign_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        by_name = {m["name"]: m["id"] for m in details["members"]}
        self.p1_id = by_name["Gabriel"]
        self.p2_id = by_name["Leandro"]

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    def test_locations_crud_hierarchy_images_visibility_and_cross_links(self):
        # Jogador não pode criar localidade
        forbidden = self.client.post(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={"name": "Bosque", "kind": "region"},
        )
        self.assertEqual(forbidden.status_code, 403)

        # GM cria região secreta com imagem
        reg_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Costa de Terug",
                "kind": "region",
                "description": "Península brumosa ao sul.",
                "gmNotes": "Esconde a base dos corsários.",
                "image": "https://example.org/mapa-terug.png",
                "visibility": "gm",
            },
        )
        self.assertEqual(reg_resp.status_code, 201)
        region = reg_resp.json()
        self.assertEqual(region["kind"], "region")
        self.assertEqual(region["image"], "https://example.org/mapa-terug.png")
        region_id = region["id"]

        # GM cria localidade filha visível apenas para Gabriel (p1)
        loc_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Porto de Bruma",
                "kind": "location",
                "parentId": region_id,
                "description": "Doca clandestina.",
                "gmNotes": "Senha do vigia: albatroz.",
                "visibility": "selected",
                "visibleToMemberIds": [self.p1_id],
            },
        )
        self.assertEqual(loc_resp.status_code, 201)
        loc_id = loc_resp.json()["id"]

        # Validações 422: self-parent e parent de outra campanha
        self_parent = self.client.put(
            f"/campaigns/{self.campaign_id}/locations/{loc_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Porto de Bruma", "kind": "location", "parentId": loc_id, "visibility": "party"},
        )
        self.assertEqual(self_parent.status_code, 422)

        bad_selected = self.client.post(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Ilha", "kind": "location", "visibility": "selected", "visibleToMemberIds": []},
        )
        self.assertEqual(bad_selected.status_code, 422)

        # Gabriel vê apenas Porto de Bruma (sem gmNotes, sem visibleToMemberIds e sem vazar parentId secreto)
        p1_locs = self.client.get(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        ).json()
        self.assertEqual([l["id"] for l in p1_locs], [loc_id])
        self.assertNotIn("gmNotes", p1_locs[0])
        self.assertNotIn("visibleToMemberIds", p1_locs[0])
        self.assertIsNone(p1_locs[0]["parentId"])

        # Leandro não vê nenhuma
        p2_locs = self.client.get(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.p2_token}"},
        ).json()
        self.assertEqual(p2_locs, [])

        # GM vincula Sessão, Encontro, NPC e Missão à região secreta
        sess = self.client.post(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"sessionNumber": 1, "title": "Chegada ao Sul", "status": "planned", "locationId": region_id},
        ).json()
        self.assertEqual(sess["locationId"], region_id)

        enc = self.client.post(
            f"/campaigns/{self.campaign_id}/encounters",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Emboscada no Cais", "status": "planned", "monstersData": [], "locationId": region_id},
        ).json()
        self.assertEqual(enc["locationId"], region_id)

        npc = self.client.post(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Capitã Mara", "visibility": "party", "locationId": region_id},
        ).json()
        self.assertEqual(npc["locationId"], region_id)

        quest = self.client.post(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"title": "Resgatar o Diário", "visibility": "party", "locationId": region_id},
        ).json()
        self.assertEqual(quest["locationId"], region_id)

        # Enquanto região for 'gm', jogador não recebe locationId vazando o vínculo
        p1_sessions = self.client.get(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        ).json()
        self.assertIsNone(p1_sessions[0]["locationId"])

        p1_npcs = self.client.get(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        ).json()
        self.assertIsNone(p1_npcs[0]["locationId"])

        p1_quests = self.client.get(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        ).json()
        self.assertIsNone(p1_quests[0]["locationId"])

        # GM revela a região para a party -> agora todos veem a região e o parentId / locationId!
        rev = self.client.put(
            f"/campaigns/{self.campaign_id}/locations/{region_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Costa de Terug",
                "kind": "region",
                "description": "Península brumosa ao sul.",
                "image": "https://example.org/mapa-terug.png",
                "visibility": "party",
            },
        )
        self.assertEqual(rev.status_code, 200)

        p1_locs_after = {
            item["id"]: item
            for item in self.client.get(
                f"/campaigns/{self.campaign_id}/locations",
                headers={"Authorization": f"Bearer {self.p1_token}"},
            ).json()
        }
        self.assertIn(region_id, p1_locs_after)
        self.assertEqual(p1_locs_after[loc_id]["parentId"], region_id)

        p2_sessions = self.client.get(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.p2_token}"},
        ).json()
        self.assertEqual(p2_sessions[0]["locationId"], region_id)

        # Excluir localidade faz ON DELETE SET NULL sem quebrar sessões/encontros
        del_resp = self.client.delete(
            f"/campaigns/{self.campaign_id}/locations/{region_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(del_resp.status_code, 204)
        gm_sessions_after = self.client.get(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        self.assertIsNone(gm_sessions_after[0]["locationId"])

    def test_legacy_db_adds_location_id_columns_safely(self):
        legacy_path = Path(self.tmp.name) / "legacy_locations.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE campaigns (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, gm_id TEXT NOT NULL
                );
                CREATE TABLE members (
                    campaign_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    role TEXT NOT NULL, character_id TEXT, character_data TEXT,
                    PRIMARY KEY (campaign_id, user_id)
                );
                CREATE TABLE campaign_sessions (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                    session_number INTEGER NOT NULL, title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '', scheduled_at TEXT,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE campaign_quests (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    reward TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    visibility TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE campaign_encounters (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                    name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
                    tactics TEXT NOT NULL DEFAULT '', rewards TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL, monsters_data TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE campaign_npcs (
                    id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                    name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
                    attitude TEXT NOT NULL DEFAULT 'neutral',
                    description TEXT NOT NULL DEFAULT '', gm_notes TEXT NOT NULL DEFAULT '',
                    combat_data TEXT, visibility TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            conn.execute("INSERT INTO users VALUES ('u1', 'u1@x.test', 'Victor', 'h')")
            conn.execute("INSERT INTO campaigns VALUES ('c1', 'Mesa', 'u1')")
            conn.execute("INSERT INTO members VALUES ('c1', 'u1', 'gm', NULL, NULL)")
            conn.execute(
                "INSERT INTO campaign_sessions VALUES ('s1', 'c1', 1, 'Sessão 1', '', NULL, 'planned', '2026-01-01', '2026-01-01')"
            )

        migrated = CampaignStore(legacy_path)
        sessions = migrated.list_sessions("u1", "c1")
        self.assertEqual(len(sessions), 1)
        self.assertIsNone(sessions[0]["locationId"])
