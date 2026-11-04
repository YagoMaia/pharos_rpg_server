"""Sessões revogadas não devem continuar inscritas em canais privados."""

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from starlette.websockets import WebSocketState

from app.routers import websocket as ws_router
from app.services.campaign_store import CampaignStore


class SilentSocket:
    def __init__(self, token: str):
        self.token = token
        self.authenticated = asyncio.Event()
        self.waiting = asyncio.Event()
        self.handshake_sent = False
        self.close_code: int | None = None
        self.application_state = WebSocketState.CONNECTING

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def receive_text(self) -> str:
        if not self.handshake_sent:
            self.handshake_sent = True
            return json.dumps({"type": "AUTH_CAMPAIGN", "payload": {"token": self.token}})
        await self.waiting.wait()
        raise AssertionError("o cliente silencioso não envia comandos")

    async def send_json(self, payload: dict[str, str]) -> None:
        if payload["type"] == "AUTH_OK":
            self.authenticated.set()

    async def close(self, code: int = 1000) -> None:
        self.close_code = code
        self.application_state = WebSocketState.DISCONNECTED


class IdleConnectionPool:
    async def connect(self, *args: object, **kwargs: object) -> None:
        pass

    def disconnect(self, *args: object) -> None:
        pass


class SilentSocketRevocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CampaignStore(Path(self.directory.name) / "socket.sqlite3")
        self.token = self.store.table_login("Victor", "victor")
        self.user_id = self.store.user_for_token(self.token)["id"]
        self.campaign = self.store.create_campaign(self.user_id, "Mesa")["id"]

    async def test_logout_closes_silent_connection(self) -> None:
        socket = SilentSocket(self.token)
        with (
            patch.object(ws_router, "manager", IdleConnectionPool()),
            patch.object(ws_router, "get_campaign_store", return_value=self.store),
            patch.object(ws_router, "_AUTH_RECHECK_SECONDS", 0.01, create=True),
        ):
            task = asyncio.create_task(
                ws_router.websocket_endpoint(socket, f"campaign-{self.campaign}")
            )
            try:
                await asyncio.wait_for(socket.authenticated.wait(), timeout=2)
                self.store.revoke_token(self.user_id, self.token)
                await asyncio.wait_for(task, timeout=2)
                self.assertEqual(socket.close_code, 1008)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
