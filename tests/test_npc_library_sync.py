from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore


class NpcLibrarySyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_npc_library.sqlite3"
        self.store = CampaignStore(self.db_path)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

        # Autentica mestre Victor
        gm_resp = self.client.post("/auth/table-login", json={"name": "Victor", "pin": "victor"})
        self.assertEqual(gm_resp.status_code, 200)
        self.gm_token = gm_resp.json()["accessToken"]

        # Autentica jogador Gabriel
        p1_resp = self.client.post("/auth/table-login", json={"name": "Gabriel", "pin": "gabriel"})
        self.assertEqual(p1_resp.status_code, 200)
        self.p1_token = p1_resp.json()["accessToken"]

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    def test_auth_required(self):
        resp = self.client.get("/npc-library")
        self.assertEqual(resp.status_code, 401)

    def test_npc_library_crud(self):
        # 1. Lista inicial vazia
        list_resp = self.client.get(
            "/npc-library",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(list_resp.json(), [])

        # 2. Criação de template
        template_payload = {
            "name": "Goblin Ladino",
            "level": 2,
            "templateData": {
                "name": "Goblin Ladino",
                "level": 2,
                "class": "Ladino",
                "ancestry": "Goblin",
                "maxHp": 18,
                "maxFocus": 2,
                "armorClass": 14,
                "attributes": {"Destreza": {"value": 16, "modifier": 3}},
                "weapons": {"adaga": {"name": "Adaga Envenenada", "attackBonus": 5}},
            },
        }
        create_resp = self.client.post(
            "/npc-library",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json=template_payload,
        )
        self.assertEqual(create_resp.status_code, 201)
        created = create_resp.json()
        self.assertEqual(created["name"], "Goblin Ladino")
        self.assertEqual(created["level"], 2)
        self.assertEqual(created["templateData"]["class"], "Ladino")
        template_id = created["id"]

        # 3. Lista contém 1 template
        list_resp2 = self.client.get(
            "/npc-library",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(list_resp2.status_code, 200)
        self.assertEqual(len(list_resp2.json()), 1)
        self.assertEqual(list_resp2.json()[0]["id"], template_id)

        # 4. Atualização
        update_payload = {
            "name": "Goblin Assassino",
            "level": 3,
            "templateData": {
                **template_payload["templateData"],
                "name": "Goblin Assassino",
                "level": 3,
                "maxHp": 25,
            },
        }
        update_resp = self.client.put(
            f"/npc-library/{template_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json=update_payload,
        )
        self.assertEqual(update_resp.status_code, 200)
        updated = update_resp.json()
        self.assertEqual(updated["name"], "Goblin Assassino")
        self.assertEqual(updated["level"], 3)
        self.assertEqual(updated["templateData"]["maxHp"], 25)

        # 5. Exclusão
        del_resp = self.client.delete(
            f"/npc-library/{template_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(del_resp.status_code, 204)

        # 6. Lista vazia novamente
        list_resp3 = self.client.get(
            "/npc-library",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(list_resp3.json(), [])

    def test_npc_library_sync_batch(self):
        # Cria um template já existente no servidor
        self.client.post(
            "/npc-library",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "id": "tpl-server-1",
                "name": "Esqueleto Soldado",
                "level": 1,
                "templateData": {"maxHp": 15, "armorClass": 12},
                "updatedAt": "2026-10-12T10:00:00Z",
            },
        )

        # O cliente local (mobile) tem:
        # - Um novo template que ainda não existe no servidor (ex: criado offline)
        # - Uma versão atualizada do template existente com timestamp mais recente
        sync_payload = {
            "templates": [
                {
                    "id": "tpl-mobile-new",
                    "name": "Aranha Gigante",
                    "level": 2,
                    "updatedAt": "2026-10-12T11:00:00Z",
                    "templateData": {"maxHp": 22, "speed": "12m"},
                },
                {
                    "id": "tpl-server-1",
                    "name": "Esqueleto Guarda",
                    "level": 2,
                    "updatedAt": "2026-10-12T12:00:00Z",  # Mais recente que 10:00:00Z
                    "templateData": {"maxHp": 20, "armorClass": 14},
                },
            ]
        }

        sync_resp = self.client.post(
            "/npc-library/sync",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json=sync_payload,
        )
        self.assertEqual(sync_resp.status_code, 200)
        synced_list = sync_resp.json()
        self.assertEqual(len(synced_list), 2)

        by_id = {item["id"]: item for item in synced_list}
        self.assertIn("tpl-mobile-new", by_id)
        self.assertEqual(by_id["tpl-mobile-new"]["name"], "Aranha Gigante")

        self.assertIn("tpl-server-1", by_id)
        self.assertEqual(by_id["tpl-server-1"]["name"], "Esqueleto Guarda")
        self.assertEqual(by_id["tpl-server-1"]["level"], 2)


if __name__ == "__main__":
    unittest.main()
