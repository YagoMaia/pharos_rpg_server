import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import WebSocketDisconnect

from app.models.domain import CombatSession
from app.routers import websocket as ws_router
from app.services.campaign_store import CampaignStore
from app.services.engine import GameLogic


def actor(identifier, sheet_id=None):
    return {
        "id": identifier,
        "name": identifier,
        "type": "player",
        "sheetId": sheet_id,
        "initiative": 12,
        "hp": {"current": 10, "max": 10},
        "focus": {"current": 5, "max": 5},
        "armorClass": 10,
        "turnActions": {"standard": True, "bonus": True, "reaction": True},
    }


class MemoryManager:
    def __init__(self):
        self.raw = None
        self.redis = Mock(set=AsyncMock())
        self.connect = AsyncMock()
        self.disconnect = Mock()

    async def get_state(self, session_id):
        return CombatSession.model_validate_json(self.raw) if self.raw else None

    async def broadcast_state(self, session_id, state):
        self.raw = state.model_dump_json()

    async def broadcast_event(self, session_id, event_type, payload):
        pass


class CampaignSocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = CampaignStore(Path(self.directory.name) / "campaign.sqlite3")
        self.gm_token = self.store.register(
            "gm@example.com", "Mestre", "safe-password-123"
        )
        self.gm_id = self.store.user_for_token(self.gm_token)["id"]
        self.campaign_id = self.store.create_campaign(self.gm_id, "Mesa")["id"]

    def tearDown(self):
        self.directory.cleanup()

    async def test_player_cannot_claim_gm_or_another_sheet(self):
        manager = MemoryManager()
        engine = GameLogic(manager)
        room = f"campaign-{self.campaign_id}"
        player = self.store.register(
            "player@example.com", "Jogador", "safe-password-123"
        )
        player_id = self.store.user_for_token(player)["id"]
        invite = self.store.create_invite(self.gm_id, self.campaign_id)
        self.store.join_campaign(player_id, invite["code"])
        self.store.link_character(player_id, self.campaign_id, "sheet-1")
        identity = {
            "authenticated": True,
            "role": "player",
            "client_id": f"user:{player_id}",
            "character_id": "sheet-1",
            "player_id": None,
        }

        with self.assertRaisesRegex(ValueError, "mestre"):
            await engine.handle_message(
                room, {"type": "GM_CONNECT", "payload": {"clientId": "gm"}}, identity
            )
        with self.assertRaisesRegex(ValueError, "ficha"):
            await engine.handle_message(
                room,
                {
                    "type": "JOIN_SESSION",
                    "payload": {
                        "clientId": "gm",
                        "combatant": actor("hero", "stolen-sheet"),
                    },
                },
                identity,
            )
        await engine.handle_message(
            room,
            {
                "type": "JOIN_SESSION",
                "payload": {
                    "clientId": "fake-gm",
                    "combatant": actor("hero", "sheet-1"),
                },
            },
            identity,
        )
        self.assertEqual(
            (await manager.get_state(room)).combatantOwners["hero"], f"user:{player_id}"
        )
        with self.assertRaisesRegex(ValueError, "personagem"):
            await engine.handle_message(
                room,
                {
                    "type": "MANUAL_UPDATE",
                    "payload": {
                        "clientId": "fake-gm",
                        "combatantId": "other",
                        "newHp": 0,
                    },
                },
                identity,
            )
        identity["character_id"] = "sheet-2"
        with self.assertRaisesRegex(ValueError, "ficha"):
            await engine.handle_message(
                room,
                {
                    "type": "MANUAL_UPDATE",
                    "payload": {
                        "combatantId": "hero",
                        "newHp": 0,
                    },
                },
                identity,
            )

    async def test_campaign_room_requires_handshake_before_subscription(self):
        manager = MemoryManager()
        socket = Mock(
            accept=AsyncMock(),
            close=AsyncMock(),
            send_json=AsyncMock(),
            receive_text=AsyncMock(
                side_effect=[json.dumps({"type": "GM_CONNECT"}), WebSocketDisconnect()]
            ),
        )
        with (
            patch.object(ws_router, "manager", manager),
            patch.object(ws_router, "game_logic", GameLogic(manager)),
            patch.object(ws_router, "get_campaign_store", return_value=self.store),
        ):
            await ws_router.websocket_endpoint(socket, f"campaign-{self.campaign_id}")
        manager.connect.assert_not_awaited()
        self.assertEqual(socket.close.await_args.kwargs["code"], 1008)

    async def test_valid_token_binds_role_and_revoked_token_is_rejected(self):
        manager = MemoryManager()
        socket = Mock(
            accept=AsyncMock(),
            close=AsyncMock(),
            send_json=AsyncMock(),
            receive_text=AsyncMock(
                side_effect=[
                    json.dumps(
                        {"type": "AUTH_CAMPAIGN", "payload": {"token": self.gm_token}}
                    ),
                    json.dumps({"type": "GM_CONNECT", "payload": {"clientId": "fake"}}),
                    WebSocketDisconnect(),
                ]
            ),
        )
        with (
            patch.object(ws_router, "manager", manager),
            patch.object(ws_router, "game_logic", GameLogic(manager)),
            patch.object(ws_router, "get_campaign_store", return_value=self.store),
        ):
            await ws_router.websocket_endpoint(socket, f"campaign-{self.campaign_id}")
        self.assertIsNotNone(
            (await manager.get_state(f"campaign-{self.campaign_id}")).gmClientId
        )
        self.assertEqual(socket.send_json.await_args_list[0].args[0]["type"], "AUTH_OK")

        self.store.revoke_token(self.gm_id, self.gm_token)
        invalid = Mock(
            accept=AsyncMock(),
            close=AsyncMock(),
            send_json=AsyncMock(),
            receive_text=AsyncMock(
                side_effect=[
                    json.dumps(
                        {
                            "type": "AUTH_CAMPAIGN",
                            "payload": {"token": self.gm_token},
                        }
                    )
                ]
            ),
        )
        with (
            patch.object(ws_router, "manager", MemoryManager()),
            patch.object(ws_router, "get_campaign_store", return_value=self.store),
        ):
            await ws_router.websocket_endpoint(invalid, f"campaign-{self.campaign_id}")
        self.assertEqual(invalid.close.await_args.kwargs["code"], 1008)

    async def test_player_without_linked_sheet_cannot_subscribe(self):
        player_token = self.store.register(
            "player@example.com", "Jogador", "safe-password-123"
        )
        player_id = self.store.user_for_token(player_token)["id"]
        invite = self.store.create_invite(self.gm_id, self.campaign_id)
        self.store.join_campaign(player_id, invite["code"])
        manager = MemoryManager()
        socket = Mock(
            accept=AsyncMock(),
            close=AsyncMock(),
            send_json=AsyncMock(),
            receive_text=AsyncMock(
                return_value=json.dumps(
                    {
                        "type": "AUTH_CAMPAIGN",
                        "payload": {"token": player_token},
                    }
                )
            ),
        )
        with (
            patch.object(ws_router, "manager", manager),
            patch.object(ws_router, "get_campaign_store", return_value=self.store),
        ):
            await ws_router.websocket_endpoint(socket, f"campaign-{self.campaign_id}")
        manager.connect.assert_not_awaited()
        self.assertEqual(socket.close.await_args.kwargs["code"], 1008)

    async def test_revoked_token_stops_commands_on_existing_socket(self):
        manager = MemoryManager()
        messages = iter(
            [
                {"type": "AUTH_CAMPAIGN", "payload": {"token": self.gm_token}},
                {"type": "GM_CONNECT", "payload": {}},
                {"type": "GM_ADD_NPC", "payload": actor("npc")},
            ]
        )

        async def next_message():
            message = next(messages)
            if message["type"] == "GM_ADD_NPC":
                self.store.revoke_token(self.gm_id, self.gm_token)
            return json.dumps(message)

        socket = Mock(
            accept=AsyncMock(),
            close=AsyncMock(),
            send_json=AsyncMock(),
            receive_text=AsyncMock(side_effect=next_message),
        )
        with (
            patch.object(ws_router, "manager", manager),
            patch.object(ws_router, "game_logic", GameLogic(manager)),
            patch.object(ws_router, "get_campaign_store", return_value=self.store),
        ):
            await ws_router.websocket_endpoint(socket, f"campaign-{self.campaign_id}")
        self.assertEqual(socket.close.await_args.kwargs["code"], 1008)
        self.assertEqual(
            (await manager.get_state(f"campaign-{self.campaign_id}")).combatants, {}
        )
