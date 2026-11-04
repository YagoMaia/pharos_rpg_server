"""Mesmo sem mensagens do cliente, uma sessão revogada sai do canal."""

import asyncio
import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from starlette.websockets import WebSocketState

from app.core.database import open_connection
from app.routers import websocket as ws_router
from app.services.campaign_store import CampaignStore
from app.services.socket_authorization import CampaignSocketAuthorization


class SilentSocket:
    def __init__(self, token: str):
        self.token = token
        self.authenticated = asyncio.Event()
        self.waiting = asyncio.Event()
        self.close_code: int | None = None
        self.handshake_sent = False
        self.application_state = WebSocketState.CONNECTING

    async def accept(self) -> None:
        self.application_state = WebSocketState.CONNECTED

    async def receive_text(self) -> str:
        if not self.handshake_sent:
            self.handshake_sent = True
            return json.dumps(
                {"type": "AUTH_CAMPAIGN", "payload": {"token": self.token}}
            )
        await self.waiting.wait()
        raise AssertionError("o cliente silencioso nunca envia comandos")

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
        self.store.set_table_pin("Victor", "pin-de-teste-victor")
        self.token = self.store.table_login("Victor", "pin-de-teste-victor")
        self.user_id = self.store.user_for_token(self.token)["id"]
        self.campaign = self.store.create_campaign(self.user_id, "Mesa")["id"]

    async def assert_idle_socket_closed(self, action: str) -> None:
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
                self.invalidate_session(action)
                await asyncio.wait_for(task, timeout=2)
                self.assertEqual(socket.close_code, 1008)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def invalidate_session(self, action: str) -> None:
        if action == "rotate":
            self.store.set_table_pin("Victor", "pin-substituto-victor")
        elif action == "logout":
            self.store.revoke_token(self.user_id, self.token)
        else:
            with closing(open_connection(self.store.path)) as conn, conn:
                conn.execute("UPDATE tokens SET expires_at = 0")

    async def test_pin_rotation_closes_a_silent_socket(self) -> None:
        await self.assert_idle_socket_closed("rotate")

    async def test_logout_closes_a_silent_socket(self) -> None:
        await self.assert_idle_socket_closed("logout")

    async def test_expiration_closes_a_silent_socket(self) -> None:
        await self.assert_idle_socket_closed("expire")

    async def test_changed_role_invalidates_the_connection_snapshot(self) -> None:
        authorization = CampaignSocketAuthorization(
            self.store, self.token, self.campaign, "gm", None
        )
        self.assertTrue(await asyncio.to_thread(authorization))
        with closing(open_connection(self.store.path)) as conn, conn:
            conn.execute("UPDATE members SET role = 'player', character_id = 'sheet-1'")
        self.assertFalse(await asyncio.to_thread(authorization))

    async def test_changed_sheet_invalidates_the_connection_snapshot(self) -> None:
        authorization = CampaignSocketAuthorization(
            self.store, self.token, self.campaign, "gm", None
        )
        with closing(open_connection(self.store.path)) as conn, conn:
            conn.execute("UPDATE members SET character_id = 'sheet-2'")
        self.assertFalse(await asyncio.to_thread(authorization))

    async def test_connection_description_does_not_expose_the_token(self) -> None:
        authorization = CampaignSocketAuthorization(
            self.store, self.token, self.campaign, "gm", None
        )
        self.assertNotIn(self.token, repr(authorization))
