"""Integração: API e WebSockets ASGI contra um Redis real.

Redis de integração, por precedência:
  1. `PHAROS_TEST_REDIS_URL` — qualquer Redis acessível, inclusive container
     (ex.: `redis://127.0.0.1:6379/0`).
  2. `redis-server` efêmero iniciado por este módulo.

Sem nenhum dos dois o teste **falha** em vez de pular. Ele existe para
exercitar pub/sub real; `skipTest()` deixava a suíte verde sem nunca cobrir o
transporte, que é justamente o que mais quebra em produção.
"""

import os
import shutil
import socket
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient
from redis import Redis
from redis.exceptions import RedisError
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.managers.connection import ConnectionManager
from app.routers import websocket as ws_router
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore
from app.services.engine import GameLogic

REDIS_URL_ENV = "PHAROS_TEST_REDIS_URL"
MISSING_REDIS_HINT = (
    "Redis de integração indisponível. Defina "
    f"{REDIS_URL_ENV}=redis://127.0.0.1:6379/0 ou instale redis-server no PATH. "
    "Este teste cobre pub/sub real e não pode ser pulado em silêncio."
)


def responds_to_ping(url: str) -> bool:
    try:
        client = Redis.from_url(url, socket_connect_timeout=1)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except (RedisError, OSError):
        return False


def available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CampaignLiveRedisTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.server = None
        self.redis_url = os.environ.get(REDIS_URL_ENV, "").strip()

        if self.redis_url:
            if not responds_to_ping(self.redis_url):
                self.fail(f"{REDIS_URL_ENV}={self.redis_url} não responde a PING.")
        else:
            self.redis_url = self._start_ephemeral_redis()

        if not self.redis_url:
            self.fail(MISSING_REDIS_HINT)

        self.store = CampaignStore(Path(self.directory.name) / "campaign.sqlite3")
        self.manager = ConnectionManager()
        self.manager.redis = self.manager.redis.from_url(
            self.redis_url, decode_responses=True
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

    def _start_ephemeral_redis(self) -> str | None:
        """Sobe um Redis descartável local; retorna None se não for possível."""
        executable = shutil.which("redis-server")
        if not executable:
            return None
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
        if not self._wait_for_redis(port):
            self.stop_redis()
            return None
        return f"redis://127.0.0.1:{port}/0"

    def _wait_for_redis(self, port: int) -> bool:
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if self.server.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def stop_redis(self):
        if self.server is None:
            return
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)

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
