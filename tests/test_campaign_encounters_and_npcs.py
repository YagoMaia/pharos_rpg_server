from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore


class CampaignEncountersAndNpcsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_encounters_npcs.sqlite3"
        self.store = CampaignStore(self.db_path)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

        # Autentica mestre Victor e jogador Gabriel
        gm_resp = self.client.post("/auth/table-login", json={"name": "Victor", "pin": "victor"})
        self.assertEqual(gm_resp.status_code, 200)
        self.gm_token = gm_resp.json()["accessToken"]

        p1_resp = self.client.post("/auth/table-login", json={"name": "Gabriel", "pin": "gabriel"})
        self.assertEqual(p1_resp.status_code, 200)
        self.p1_token = p1_resp.json()["accessToken"]

        # Cria campanha
        camp_resp = self.client.post(
            "/campaigns",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Crônicas de Eldoria"},
        )
        self.assertEqual(camp_resp.status_code, 201)
        self.campaign_id = camp_resp.json()["id"]

        # Gabriel entra na campanha
        inv_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/invites",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.client.post(
            "/campaigns/join",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={"inviteCode": inv_resp.json()["code"]},
        )

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    def test_encounter_permissions_and_crud(self):
        # 1. Jogador tenta listar ou criar encontros -> 403 Forbidden
        p_list = self.client.get(
            f"/campaigns/{self.campaign_id}/encounters",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        )
        self.assertEqual(p_list.status_code, 403)

        p_create = self.client.post(
            f"/campaigns/{self.campaign_id}/encounters",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={"name": "Emboscada Não Autorizada"},
        )
        self.assertEqual(p_create.status_code, 403)

        # 2. GM cria encontro
        monsters = [
            {"name": "Goblin Arqueiro", "count": 2, "hp": 12, "ac": 13, "initiativeBonus": 2},
            {"name": "Chefe Hobgoblin", "count": 1, "hp": 35, "ac": 16, "initiativeBonus": 1},
        ]
        create_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/encounters",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Emboscada no Passo da Montanha",
                "description": "Goblins atacam de cima das rochas.",
                "tactics": "Arqueiros focam nos conjuradores enquanto o chefe avança.",
                "rewards": "50 PO e um arco curto refinado.",
                "status": "planned",
                "monstersData": monsters,
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        encounter = create_resp.json()
        self.assertEqual(encounter["name"], "Emboscada no Passo da Montanha")
        self.assertEqual(len(encounter["monstersData"]), 2)
        encounter_id = encounter["id"]

        # 3. GM lista encontros
        list_resp = self.client.get(
            f"/campaigns/{self.campaign_id}/encounters",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.json()), 1)
        self.assertEqual(list_resp.json()[0]["id"], encounter_id)

        # 4. GM atualiza encontro para pronto
        update_resp = self.client.put(
            f"/campaigns/{self.campaign_id}/encounters/{encounter_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Emboscada no Passo da Montanha (Preparada)",
                "status": "ready",
                "monstersData": monsters,
            },
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(update_resp.json()["status"], "ready")

        # 5. GM exclui encontro
        del_resp = self.client.delete(
            f"/campaigns/{self.campaign_id}/encounters/{encounter_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(del_resp.status_code, 204)

        # 6. Listagem agora vazia
        list_after = self.client.get(
            f"/campaigns/{self.campaign_id}/encounters",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        self.assertEqual(len(list_after), 0)

    def test_launch_encounter_injects_into_combat_session(self):
        # 1. GM cria encontro pronto para combate
        monsters = [
            {"name": "Lobo Selvagem", "count": 2, "hp": 18, "ac": 12, "initiativeBonus": 3},
            {"name": "Alfa da Alcateia", "count": 1, "hp": 40, "ac": 14, "initiativeBonus": 4},
        ]
        create_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/encounters",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Ataque da Alcateia",
                "status": "ready",
                "monstersData": monsters,
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        encounter_id = create_resp.json()["id"]

        # 2. Jogador não pode lançar encontro
        p_launch = self.client.post(
            f"/campaigns/{self.campaign_id}/encounters/{encounter_id}/launch",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        )
        self.assertEqual(p_launch.status_code, 403)

        # 3. GM lança o encontro com manager simulado para verificar injeção de estado
        from unittest.mock import patch

        combat_state = None

        async def fake_get_state(sess_id):
            return combat_state

        async def fake_broadcast(sess_id, st):
            nonlocal combat_state
            combat_state = st

        with patch("app.routers.campaigns.manager") as mock_mgr:
            mock_mgr.get_state.side_effect = fake_get_state
            mock_mgr.broadcast_state.side_effect = fake_broadcast

            launch_resp = self.client.post(
                f"/campaigns/{self.campaign_id}/encounters/{encounter_id}/launch",
                headers={"Authorization": f"Bearer {self.gm_token}"},
            )
            self.assertEqual(launch_resp.status_code, 200)
            launch_data = launch_resp.json()
            self.assertEqual(launch_data["status"], "launched")
            # 2 lobos selvagens + 1 alfa = 3 combatentes adicionados
            self.assertEqual(launch_data["combatantsAdded"], 3)

            # 4. Verifica estado do combate simulado
            self.assertIsNotNone(combat_state)
            self.assertEqual(len(combat_state.combatants), 3)
            self.assertEqual(len(combat_state.turn_order), 3)

            # Checa nomes numerados
            names = [c.name for c in combat_state.combatants.values()]
            self.assertIn("Lobo Selvagem #1", names)
            self.assertIn("Lobo Selvagem #2", names)
            self.assertIn("Alfa da Alcateia", names)

            # Checa ordenação de iniciativa decrescente
            initiatives = [combat_state.combatants[cid].initiative for cid in combat_state.turn_order]
            self.assertEqual(initiatives, sorted(initiatives, reverse=True))

    def test_npc_dossier_permissions_and_visibility(self):
        # 1. Jogador não pode criar NPC
        p_create = self.client.post(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={"name": "Tentativa Jogador"},
        )
        self.assertEqual(p_create.status_code, 403)

        # 2. GM cria NPC Secreto (visibility: gm)
        secret_npc = self.client.post(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Valerius, o Escriba",
                "role": "Informante Secreto",
                "attitude": "neutral",
                "description": "Um homem quieto de manto cinza.",
                "gmNotes": "Trabalha secretamente para a guilda dos assassinos.",
                "visibility": "gm",
            },
        )
        self.assertEqual(secret_npc.status_code, 201)
        secret_id = secret_npc.json()["id"]
        self.assertEqual(secret_npc.json()["gmNotes"], "Trabalha secretamente para a guilda dos assassinos.")

        # 3. GM cria NPC Público (visibility: party)
        public_npc = self.client.post(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Brumo, o Taberneiro",
                "role": "Dono da Taverna do Javali",
                "attitude": "friendly",
                "description": "Um anão robusto e risonho.",
                "gmNotes": "Esconde uma reserva de hidromel élfico premiado.",
                "visibility": "party",
            },
        )
        self.assertEqual(public_npc.status_code, 201)
        public_id = public_npc.json()["id"]

        # 4. Jogador lista NPCs:
        # - Deve ver APENAS Brumo (party)
        # - Valerius (gm) NÃO deve aparecer
        # - gmNotes de Brumo NÃO deve ser exposto!
        p_list = self.client.get(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        )
        self.assertEqual(p_list.status_code, 200)
        p_npcs = p_list.json()
        self.assertEqual(len(p_npcs), 1)
        self.assertEqual(p_npcs[0]["id"], public_id)
        self.assertEqual(p_npcs[0]["name"], "Brumo, o Taberneiro")
        self.assertNotIn("gmNotes", p_npcs[0])

        # 5. GM lista NPCs:
        # - Vê ambos os NPCs
        # - Possui o campo gmNotes em ambos
        gm_list = self.client.get(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(gm_list.status_code, 200)
        gm_npcs = gm_list.json()
        self.assertEqual(len(gm_npcs), 2)
        gm_by_id = {n["id"]: n for n in gm_npcs}
        self.assertIn("gmNotes", gm_by_id[secret_id])
        self.assertIn("gmNotes", gm_by_id[public_id])

        # 6. GM revela Valerius para a party (visibility: party)
        reveal_resp = self.client.put(
            f"/campaigns/{self.campaign_id}/npcs/{secret_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "name": "Valerius, o Escriba",
                "visibility": "party",
            },
        )
        self.assertEqual(reveal_resp.status_code, 200)
        self.assertEqual(reveal_resp.json()["visibility"], "party")

        # 7. Jogador agora vê 2 NPCs, ambos sem gmNotes
        p_list2 = self.client.get(
            f"/campaigns/{self.campaign_id}/npcs",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        ).json()
        self.assertEqual(len(p_list2), 2)
        for npc in p_list2:
            self.assertNotIn("gmNotes", npc)

        # 8. GM exclui NPC
        del_resp = self.client.delete(
            f"/campaigns/{self.campaign_id}/npcs/{secret_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(del_resp.status_code, 204)
