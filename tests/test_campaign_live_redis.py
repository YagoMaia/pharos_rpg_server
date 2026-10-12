"""Integração local: API e WebSockets ASGI usando Redis descartável."""

import shutil
import socket
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.managers.connection import ConnectionManager
from app.routers import websocket as ws_router
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore
from app.services.engine import GameLogic


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CampaignLiveRedisTests(unittest.TestCase):
    def setUp(self):
        executable = shutil.which("redis-server")
        if not executable:
            self.skipTest("redis-server não está disponível no PATH")
        self.directory = TemporaryDirectory()
        port = available_port()
        self.server = subprocess.Popen(
            [
                executable,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            cwd=self.directory.name,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.wait_for_redis(port)
        except Exception:
            self.stop_redis()
            self.directory.cleanup()
            raise
        self.store = CampaignStore(Path(self.directory.name) / "campaign.sqlite3")
        self.manager = ConnectionManager()
        self.manager.redis = self.manager.redis.from_url(
            f"redis://127.0.0.1:{port}/0", decode_responses=True
        )
        self.patches = [
            patch.object(ws_router, "manager", self.manager),
            patch.object(ws_router, "game_logic", GameLogic(self.manager)),
            patch.object(ws_router, "get_campaign_store", return_value=self.store),
        ]
        for active_patch in self.patches:
            active_patch.start()
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.portal.call(self.manager.redis.aclose)
        self.client.__exit__(None, None, None)
        app.dependency_overrides.clear()
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.stop_redis()
        self.directory.cleanup()

    def stop_redis(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)

    def wait_for_redis(self, port: int):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if self.server.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.05)
        self.fail("Redis temporário não iniciou")

    def post(
        self, path: str, body: dict | None = None, token: str | None = None
    ) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.client.post(path, json=body, headers=headers)
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()

    def test_gm_player_reconnect_and_logout(self):
        gm = self.post(
            "/auth/table-login",
            {"name": "Victor", "pin": "victor"},
        )["accessToken"]
        player = self.post(
            "/auth/table-login",
            {"name": "Gabriel", "pin": "gabriel"},
        )["accessToken"]
        campaign = self.post("/campaigns", {"name": "Mesa"}, gm)["id"]
        invite = self.post(f"/campaigns/{campaign}/invites", token=gm)["code"]
        self.post("/campaigns/join", {"inviteCode": invite}, player)
        linked = self.client.put(
            f"/campaigns/{campaign}/character",
            json={"characterId": "sheet-1"},
            headers={"Authorization": f"Bearer {player}"},
        )
        self.assertEqual(linked.status_code, 200)
        room = f"campaign-{campaign}"
        with self.client.websocket_connect(f"/ws/{room}") as unauthenticated:
            unauthenticated.send_json({"type": "GM_CONNECT", "payload": {}})
            with self.assertRaises(WebSocketDisconnect) as denied:
                unauthenticated.receive_json()
            self.assertEqual(denied.exception.code, 1008)
        with self.client.websocket_connect(f"/ws/{room}") as unlinked:
            outsider = self.post(
                "/auth/table-login",
                {"name": "Wayne", "pin": "wayne"},
            )["accessToken"]
            unlinked.send_json(
                {"type": "AUTH_CAMPAIGN", "payload": {"token": outsider}}
            )
            with self.assertRaises(WebSocketDisconnect) as denied:
                unlinked.receive_json()
            self.assertEqual(denied.exception.code, 1008)
        with self.client.websocket_connect(f"/ws/{room}") as gm_socket:
            gm_socket.send_json({"type": "AUTH_CAMPAIGN", "payload": {"token": gm}})
            self.assertEqual(gm_socket.receive_json()["type"], "AUTH_OK")
            gm_socket.send_json({"type": "GM_CONNECT", "payload": {}})
            self.assertEqual(gm_socket.receive_json()["session_id"], room)
            with self.client.websocket_connect(f"/ws/{room}") as player_socket:
                player_socket.send_json(
                    {"type": "AUTH_CAMPAIGN", "payload": {"token": player}}
                )
                self.assertEqual(player_socket.receive_json()["type"], "AUTH_OK")
                player_socket.send_json(
                    {
                        "type": "JOIN_SESSION",
                        "payload": {
                            "clientId": "forged-gm",
                            "combatant": {
                                "id": "hero",
                                "name": "Hero",
                                "type": "player",
                                "sheetId": "sheet-1",
                                "initiative": 12,
                                "hp": {"current": 10, "max": 10},
                                "focus": {"current": 5, "max": 5},
                                "armorClass": 10,
                                "turnActions": {
                                    "standard": True,
                                    "bonus": True,
                                    "reaction": True,
                                },
                            },
                        },
                    }
                )
                state = player_socket.receive_json()
                self.assertEqual(
                    state["combatantOwners"]["hero"],
                    f"user:{self.store.user_for_token(player)['id']}",
                )
                self.assertIn("hero", gm_socket.receive_json()["combatants"])
                player_socket.send_json(
                    {"type": "GM_CONNECT", "payload": {"clientId": "forged-gm"}}
                )
                self.assertIn("mestre", player_socket.receive_json()["error"])
                response = self.client.post(
                    "/auth/logout", headers={"Authorization": f"Bearer {player}"}
                )
                self.assertEqual(response.status_code, 204)
                player_socket.send_json({"type": "PING"})
                with self.assertRaises(WebSocketDisconnect) as closed:
                    player_socket.receive_json()
                self.assertEqual(closed.exception.code, 1008)
        with self.client.websocket_connect(f"/ws/{room}") as reconnected:
            reconnected.send_json({"type": "AUTH_CAMPAIGN", "payload": {"token": gm}})
            self.assertEqual(reconnected.receive_json()["type"], "AUTH_OK")
            reconnected.send_json({"type": "GM_CONNECT", "payload": {}})
            self.assertIn("hero", reconnected.receive_json()["combatants"])
