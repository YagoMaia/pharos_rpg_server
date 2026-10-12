from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore


class CampaignSessionsAndQuestsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_sessions_quests.sqlite3"
        self.store = CampaignStore(self.db_path)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

        # Autentica mestre Victor e jogadores Gabriel e Leandro
        gm_resp = self.client.post("/auth/table-login", json={"name": "Victor", "pin": "victor"})
        self.assertEqual(gm_resp.status_code, 200)
        self.gm_token = gm_resp.json()["accessToken"]

        p1_resp = self.client.post("/auth/table-login", json={"name": "Gabriel", "pin": "gabriel"})
        self.assertEqual(p1_resp.status_code, 200)
        self.p1_token = p1_resp.json()["accessToken"]

        p2_resp = self.client.post("/auth/table-login", json={"name": "Leandro", "pin": "leandro"})
        self.assertEqual(p2_resp.status_code, 200)
        self.p2_token = p2_resp.json()["accessToken"]

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

        # Leandro entra na campanha
        inv_resp2 = self.client.post(
            f"/campaigns/{self.campaign_id}/invites",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.client.post(
            "/campaigns/join",
            headers={"Authorization": f"Bearer {self.p2_token}"},
            json={"inviteCode": inv_resp2.json()["code"]},
        )

        # Recupera user IDs
        campaign_data = self.client.get(
            f"/campaigns/{self.campaign_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        self.members_by_name = {m["name"]: m for m in campaign_data["members"]}

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    def test_session_lifecycle_and_permissions(self):
        # Jogador não pode criar sessão
        forbidden = self.client.post(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={
                "sessionNumber": 1,
                "title": "A Chegada",
                "summary": "Resumo inicial",
                "scheduledAt": "2026-10-15T19:00:00Z",
                "status": "planned",
            },
        )
        self.assertEqual(forbidden.status_code, 403)

        # GM cria sessão 1
        create_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "sessionNumber": 1,
                "title": "A Chegada",
                "summary": "Resumo inicial",
                "scheduledAt": "2026-10-15T19:00:00Z",
                "status": "planned",
            },
        )
        self.assertEqual(create_resp.status_code, 201)
        session_id = create_resp.json()["id"]
        self.assertEqual(create_resp.json()["sessionNumber"], 1)
        self.assertEqual(create_resp.json()["title"], "A Chegada")

        # GM cria sessão 2
        s2_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "sessionNumber": 2,
                "title": "A Cripta",
                "summary": "Explorando a tumba",
                "status": "planned",
            },
        )
        self.assertEqual(s2_resp.status_code, 201)
        s2_id = s2_resp.json()["id"]

        # GM e Jogador listam sessões
        gm_list = self.client.get(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(gm_list.status_code, 200)
        self.assertEqual(len(gm_list.json()), 2)
        self.assertEqual(gm_list.json()[0]["sessionNumber"], 1)
        self.assertEqual(gm_list.json()[1]["sessionNumber"], 2)

        player_list = self.client.get(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        )
        self.assertEqual(player_list.status_code, 200)
        self.assertEqual(len(player_list.json()), 2)

        # GM atualiza sessão 1 (status: completed, novo resumo)
        update_resp = self.client.put(
            f"/campaigns/{self.campaign_id}/sessions/{session_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "sessionNumber": 1,
                "title": "A Chegada (Concluída)",
                "summary": "Encontraram o mapa secreto e ganharam 200 XP",
                "scheduledAt": "2026-10-15T19:00:00Z",
                "status": "completed",
            },
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(update_resp.json()["status"], "completed")
        self.assertEqual(update_resp.json()["title"], "A Chegada (Concluída)")

        # Jogador não pode atualizar nem deletar
        p_update = self.client.put(
            f"/campaigns/{self.campaign_id}/sessions/{session_id}",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={
                "sessionNumber": 1,
                "title": "Hackeado",
                "summary": "Erro",
                "status": "completed",
            },
        )
        self.assertEqual(p_update.status_code, 403)

        p_delete = self.client.delete(
            f"/campaigns/{self.campaign_id}/sessions/{session_id}",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        )
        self.assertEqual(p_delete.status_code, 403)

        # GM exclui sessão 2
        del_resp = self.client.delete(
            f"/campaigns/{self.campaign_id}/sessions/{s2_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(del_resp.status_code, 204)

        # Lista final tem apenas 1 sessão
        final_list = self.client.get(
            f"/campaigns/{self.campaign_id}/sessions",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        self.assertEqual(len(final_list), 1)
        self.assertEqual(final_list[0]["id"], session_id)

    def test_quests_visibility_and_lifecycle(self):
        # Jogador não pode criar quest
        forbidden = self.client.post(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={
                "title": "Limpar Porão",
                "description": "Matar ratos",
                "reward": "10 pratas",
                "visibility": "party",
                "status": "active",
            },
        )
        self.assertEqual(forbidden.status_code, 403)

        # GM cria quest pública para a party
        q1_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Limpar Porão da Taverna",
                "description": "Derrotar 4 ratos gigantes no subsolo",
                "reward": "25 moedas de prata e cerveja grátis",
                "visibility": "party",
                "status": "active",
            },
        )
        self.assertEqual(q1_resp.status_code, 201)
        q1_id = q1_resp.json()["id"]

        # GM cria quest secreta (visibility: 'gm')
        q2_resp = self.client.post(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Identidade do Espião Imperial",
                "description": "O taverneiro é um informante disfarçado",
                "reward": "Pista principal",
                "visibility": "gm",
                "status": "active",
            },
        )
        self.assertEqual(q2_resp.status_code, 201)
        q2_id = q2_resp.json()["id"]

        # GM vê ambas as missões
        gm_quests = self.client.get(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        self.assertEqual(len(gm_quests), 2)
        titles = [q["title"] for q in gm_quests]
        self.assertIn("Limpar Porão da Taverna", titles)
        self.assertIn("Identidade do Espião Imperial", titles)

        # Jogador Gabriel só vê a missão pública 'party'
        player_quests = self.client.get(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        ).json()
        self.assertEqual(len(player_quests), 1)
        self.assertEqual(player_quests[0]["id"], q1_id)
        self.assertEqual(player_quests[0]["title"], "Limpar Porão da Taverna")

        # GM atualiza quest para 'completed'
        update_resp = self.client.put(
            f"/campaigns/{self.campaign_id}/quests/{q1_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Limpar Porão da Taverna",
                "description": "Ratos derrotados com sucesso",
                "reward": "25 moedas de prata",
                "visibility": "party",
                "status": "completed",
            },
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertEqual(update_resp.json()["status"], "completed")

        # GM exclui quest secreta
        del_resp = self.client.delete(
            f"/campaigns/{self.campaign_id}/quests/{q2_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(del_resp.status_code, 204)

        gm_quests_after = self.client.get(
            f"/campaigns/{self.campaign_id}/quests",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        self.assertEqual(len(gm_quests_after), 1)

    def test_character_sheet_sync_and_inspection(self):
        gabriel_id = self.members_by_name["Gabriel"]["id"]
        character_data = {
            "name": "Thorgar Machado de Ferro",
            "class": "Guerreiro",
            "level": 3,
            "stats": {"hp": {"current": 28, "max": 28}, "focus": {"current": 5, "max": 5}},
            "attributes": {
                "Força": {"name": "Força", "value": 16, "modifier": 3},
                "Destreza": {"name": "Destreza", "value": 12, "modifier": 1},
                "Constituição": {"name": "Constituição", "value": 14, "modifier": 2},
                "Inteligência": {"name": "Inteligência", "value": 10, "modifier": 0},
                "Sabedoria": {"name": "Sabedoria", "value": 10, "modifier": 0},
                "Carisma": {"name": "Carisma", "value": 8, "modifier": -1},
            },
            "equipment": {
                "meleeWeapon": {"id": "w1", "name": "Machado de Guerra", "damage": "1d8+3"},
                "armor": {"id": "a1", "name": "Cota de Malha", "acBonus": 5},
            },
        }

        # Gabriel vincula ficha enviando characterData
        link_resp = self.client.put(
            f"/campaigns/{self.campaign_id}/character",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={
                "characterId": "sheet-thorgar-001",
                "characterData": character_data,
            },
        )
        self.assertEqual(link_resp.status_code, 200)
        self.assertEqual(link_resp.json()["characterId"], "sheet-thorgar-001")

        # GM inspeciona ficha de Gabriel
        inspect_resp = self.client.get(
            f"/campaigns/{self.campaign_id}/members/{gabriel_id}/sheet",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(inspect_resp.status_code, 200)
        sheet = inspect_resp.json()["characterData"]
        self.assertEqual(sheet["name"], "Thorgar Machado de Ferro")
        self.assertEqual(sheet["class"], "Guerreiro")
        self.assertEqual(sheet["stats"]["hp"]["current"], 28)

        # Gabriel inspeciona a própria ficha
        own_sheet = self.client.get(
            f"/campaigns/{self.campaign_id}/members/{gabriel_id}/sheet",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        )
        self.assertEqual(own_sheet.status_code, 200)

        # Leandro (outro jogador) tenta inspecionar a ficha de Gabriel -> 403 Forbidden
        leandro_inspect = self.client.get(
            f"/campaigns/{self.campaign_id}/members/{gabriel_id}/sheet",
            headers={"Authorization": f"Bearer {self.p2_token}"},
        )
        self.assertEqual(leandro_inspect.status_code, 403)

        # Gabriel atualiza os dados da ficha (sofreu dano: hp atual = 19)
        character_data["stats"]["hp"]["current"] = 19
        sync_resp = self.client.put(
            f"/campaigns/{self.campaign_id}/character/sheet",
            headers={"Authorization": f"Bearer {self.p1_token}"},
            json={"characterData": character_data},
        )
        self.assertEqual(sync_resp.status_code, 200)

        # GM inspeciona novamente e vê HP atualizado
        inspect_updated = self.client.get(
            f"/campaigns/{self.campaign_id}/members/{gabriel_id}/sheet",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(inspect_updated.status_code, 200)
        self.assertEqual(inspect_updated.json()["characterData"]["stats"]["hp"]["current"], 19)
