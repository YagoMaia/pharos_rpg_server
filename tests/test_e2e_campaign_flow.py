"""Teste de ponta a ponta (E2E) cobrindo todo o fluxo da mesa de 6 jogadores:
Autenticação de mesa -> Campanha -> Convite -> Vínculo/Troca/Desvínculo de Ficha ->
Diário Privado/Compartilhado (Criar, Editar, Excluir) -> Combate WebSocket com Pet -> Logout.
"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.models.domain import CombatSession
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore
from app.services.engine import GameLogic


class MemoryManager:
    def __init__(self):
        self.raw = None
        self.redis = Mock(set=self.save)
        self.connect = AsyncMock()
        self.disconnect = Mock()

    async def save(self, key, value, **kwargs):
        self.raw = value

    async def get_state(self, session_id):
        return CombatSession.model_validate_json(self.raw) if self.raw else None

    async def broadcast_state(self, session_id, state):
        await self.save(session_id, state.model_dump_json())

    async def broadcast_event(self, session_id, event_type, payload):
        pass


class EndToEndCampaignFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.db_path = Path(self.directory.name) / "e2e_pharos.sqlite3"
        self.store = CampaignStore(self.db_path)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.directory.cleanup()

    async def test_full_table_campaign_and_combat_lifecycle(self):
        # 1. Login da mesa: Mestre Victor e Jogador Gabriel
        victor_auth = self.client.post(
            "/auth/table-login", json={"name": "Victor", "pin": "victor"}
        )
        self.assertEqual(victor_auth.status_code, 200)
        victor_token = victor_auth.json()["accessToken"]
        victor_headers = {"Authorization": f"Bearer {victor_token}"}

        gabriel_auth = self.client.post(
            "/auth/table-login", json={"name": "Gabriel", "pin": "gabriel"}
        )
        self.assertEqual(gabriel_auth.status_code, 200)
        gabriel_token = gabriel_auth.json()["accessToken"]
        gabriel_headers = {"Authorization": f"Bearer {gabriel_token}"}

        # 2. Victor cria a campanha da mesa
        camp_res = self.client.post(
            "/campaigns", headers=victor_headers, json={"name": "Crônicas de Pharos"}
        )
        self.assertEqual(camp_res.status_code, 201)
        campaign = camp_res.json()
        campaign_id = campaign["id"]
        self.assertEqual(campaign["role"], "gm")

        # 3. Victor gera convite de uso único para Gabriel
        inv_res = self.client.post(
            f"/campaigns/{campaign_id}/invites", headers=victor_headers
        )
        self.assertEqual(inv_res.status_code, 201)
        invite_code = inv_res.json()["code"]

        # 4. Gabriel entra na campanha via convite
        join_res = self.client.post(
            "/campaigns/join", headers=gabriel_headers, json={"inviteCode": invite_code}
        )
        self.assertEqual(join_res.status_code, 200)
        self.assertEqual(join_res.json()["role"], "player")

        # 5. Vínculo, troca e desvínculo de ficha
        char_url = f"/campaigns/{campaign_id}/character"

        # 5.1 Gabriel vincula primeira ficha
        link1 = self.client.put(char_url, headers=gabriel_headers, json={"characterId": "sheet-guerreiro"})
        self.assertEqual(link1.status_code, 200)
        self.assertEqual(link1.json()["characterId"], "sheet-guerreiro")

        # 5.2 Gabriel troca para outra ficha (ex: subiu personagem novo ou alternativo)
        link2 = self.client.put(char_url, headers=gabriel_headers, json={"characterId": "sheet-ranger-lobo"})
        self.assertEqual(link2.status_code, 200)
        self.assertEqual(link2.json()["characterId"], "sheet-ranger-lobo")

        # 5.3 Gabriel desvincula temporariamente
        unlink_res = self.client.delete(char_url, headers=gabriel_headers)
        self.assertEqual(unlink_res.status_code, 200)
        self.assertIsNone(unlink_res.json()["characterId"])

        # 5.4 Gabriel re-vincula a ficha final para o combate
        link3 = self.client.put(char_url, headers=gabriel_headers, json={"characterId": "sheet-ranger-lobo"})
        self.assertEqual(link3.status_code, 200)
        self.assertEqual(link3.json()["characterId"], "sheet-ranger-lobo")

        # 6. Diário: Criar, Visibilidade, Editar e Excluir
        journal_url = f"/campaigns/{campaign_id}/entries"

        # 6.1 Mestre cria nota do grupo e nota secreta do mestre
        pistas = self.client.post(
            journal_url,
            headers=victor_headers,
            json={"title": "Pistas do Porto", "body": "Navio misterioso atraca à meia-noite", "visibility": "party"},
        )
        self.assertEqual(pistas.status_code, 201)
        pistas_id = pistas.json()["id"]

        segredo = self.client.post(
            journal_url,
            headers=victor_headers,
            json={"title": "Segredo dos Cultistas", "body": "O capitão é um traidor", "visibility": "gm"},
        )
        self.assertEqual(segredo.status_code, 201)

        # 6.2 Jogador consulta diário: vê apenas notas 'party'
        gabriel_view = self.client.get(journal_url, headers=gabriel_headers).json()
        self.assertEqual(len(gabriel_view), 1)
        self.assertEqual(gabriel_view[0]["title"], "Pistas do Porto")
        self.assertNotIn("capitão", json.dumps(gabriel_view))

        # 6.3 Mestre edita nota
        edit_res = self.client.put(
            f"{journal_url}/{pistas_id}",
            headers=victor_headers,
            json={"title": "Pistas do Porto", "body": "Navio atraca às 23h na doca 4", "visibility": "party"},
        )
        self.assertEqual(edit_res.status_code, 200)
        self.assertEqual(edit_res.json()["body"], "Navio atraca às 23h na doca 4")

        # 6.4 Mestre exclui nota
        del_res = self.client.delete(f"{journal_url}/{pistas_id}", headers=victor_headers)
        self.assertEqual(del_res.status_code, 204)
        self.assertEqual(self.client.get(journal_url, headers=gabriel_headers).json(), [])

        # 7. Combate Sincronizado via WebSocket com Pet
        manager = MemoryManager()
        engine = GameLogic(manager)
        room = f"campaign-{campaign_id}"

        victor_id = self.store.user_for_token(victor_token)["id"]
        gabriel_id = self.store.user_for_token(gabriel_token)["id"]

        victor_identity = {
            "authenticated": True, "role": "gm", "client_id": f"user:{victor_id}",
            "player_id": None, "character_id": None,
        }
        gabriel_identity = {
            "authenticated": True, "role": "player", "client_id": f"user:{gabriel_id}",
            "player_id": None, "character_id": "sheet-ranger-lobo",
        }

        with patch("app.managers.connection.manager", manager):
            # 7.1 Mestre conecta
            await engine.handle_message(
                room, {"type": "GM_CONNECT", "payload": {}}, victor_identity
            )

            # 7.2 Gabriel conecta enviando seu personagem e seu pet (Lobo Fenrir)
            gabriel_combatant = {
                "id": "gabriel",
                "name": "Gabriel",
                "type": "player",
                "sheetId": "sheet-ranger-lobo",
                "initiative": 14,
                "hp": {"current": 24, "max": 24},
                "focus": {"current": 6, "max": 6},
                "armorClass": 15,
                "turnActions": {"standard": True, "bonus": True, "reaction": True},
            }
            fenrir_pet = {
                "id": "gabriel_pet_fenrir",
                "name": "🐾 Fenrir",
                "baseName": "Fenrir",
                "type": "pet",
                "ownerId": "gabriel",
                "initiative": 18,
                "hp": {"current": 16, "max": 16},
                "focus": {"current": 2, "max": 2},
                "armorClass": 13,
                "turnActions": {"standard": True, "bonus": True, "reaction": True},
            }

            await engine.handle_message(
                room,
                {
                    "type": "JOIN_SESSION",
                    "payload": {
                        "clientId": "client-gabriel",
                        "combatant": gabriel_combatant,
                        "pet": fenrir_pet,
                    },
                },
                gabriel_identity,
            )

            # 7.3 Valida estado do combate: Pet e Jogador cadastrados e ordenados por iniciativa
            state = await manager.get_state(room)
            self.assertIn("gabriel", state.combatants)
            self.assertIn("gabriel_pet_fenrir", state.combatants)
            self.assertEqual(state.combatants["gabriel_pet_fenrir"].name, "🐾 Fenrir")
            self.assertEqual(state.combatants["gabriel_pet_fenrir"].ownerId, "gabriel")

            # Fenrir teve iniciativa 18 e Gabriel 14 -> Fenrir age antes
            self.assertEqual(state.turn_order[0], "gabriel_pet_fenrir")
            self.assertEqual(state.turn_order[1], "gabriel")

            # 7.4 Gabriel atualiza HP do seu pet
            await engine.handle_message(
                room,
                {
                    "type": "MANUAL_UPDATE",
                    "payload": {
                        "clientId": "client-gabriel",
                        "combatantId": "gabriel_pet_fenrir",
                        "newHp": 11,
                    },
                },
                gabriel_identity,
            )
            updated_state = await manager.get_state(room)
            self.assertEqual(updated_state.combatants["gabriel_pet_fenrir"].hp.current, 11)

        # 8. Logout revoga os tokens
        self.assertEqual(self.client.post("/auth/logout", headers=victor_headers).status_code, 204)
        self.assertEqual(self.client.post("/auth/logout", headers=gabriel_headers).status_code, 204)
        self.assertEqual(self.client.get("/campaigns", headers=victor_headers).status_code, 401)
        self.assertEqual(self.client.get("/campaigns", headers=gabriel_headers).status_code, 401)
