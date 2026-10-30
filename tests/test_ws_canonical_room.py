"""Contrato de canonicidade das salas WebSocket.

Reproduz o bypass confirmado em produção: o gate de autenticação comparava o nome
da sala com um prefixo sensível a caixa, enquanto a inscrição no canal Redis
normalizava o mesmo nome para minúsculas. `CAMPAIGN-<uuid>` caía no ramo legado
(sem handshake) e desembocava no canal da campanha autenticada.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect

from app.core.rooms import canonical_campaign_room
from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore


class CanonicalRoomTests(unittest.TestCase):
    def test_campaign_room_is_preserved(self):
        self.assertEqual(
            canonical_campaign_room("campaign-6f1c0b0e-1a2b-4c3d-9e8f-7a6b5c4d3e2f"),
            "campaign-6f1c0b0e-1a2b-4c3d-9e8f-7a6b5c4d3e2f",
        )

    def test_prefixed_uuid_is_normalized(self):
        self.assertEqual(
            canonical_campaign_room("CAMPAIGN-6F1C0B0E-1A2B-4C3D-9E8F-7A6B5C4D3E2F"),
            "campaign-6f1c0b0e-1a2b-4c3d-9e8f-7a6b5c4d3e2f",
        )

    def test_non_campaign_room_is_rejected(self):
        for room in ("", "MESA_01", "campaign-", "campaign-nao-e-uuid"):
            with self.subTest(room=room):
                self.assertIsNone(canonical_campaign_room(room))

    def test_uuid_with_extra_segments_is_rejected(self):
        self.assertIsNone(
            canonical_campaign_room("campaign-6f1c0b0e-1a2b-4c3d-9e8f-7a6b5c4d3e2f-extra")
        )


class RoomGateTests(unittest.TestCase):
    """A decisão do ramo e a inscrição devem usar exatamente o mesmo valor."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.store = CampaignStore(Path(self.directory.name) / "campaign.sqlite3")
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.directory.cleanup()

    def _reject_code_for(self, room: str):
        """Retorna o código de rejeição, ou None se a sala foi aceita e entregue.

        Fechamento antes do `accept()` chega como HTTP 403/WSDisconnect;
        fechamento depois do `accept()` chega como frame de close. Ambas as
        formas contam como rejeição — o que não pode acontecer é a sala ser
        entregue sem autenticação.
        """
        try:
            with self.client.websocket_connect(f"/ws/{room}") as socket:
                socket.send_json({"type": "GM_CONNECT", "payload": {"clientId": "forged"}})
                frame = socket.receive()
                if isinstance(frame, dict) and frame.get("type") == "websocket.close":
                    return frame.get("code", "CLOSED")
                return None
        except WebSocketDisconnect:
            return 1008
        except httpx.HTTPStatusError:
            return "HTTP-REJECT"

    def test_lowercase_campaign_room_is_closed_as_unauthenticated(self):
        """Comportamento que já funcionava: gate autenticado presente."""
        room = "campaign-6f1c0b0e-1a2b-4c3d-9e8f-7a6b5c4d3e2f"
        self.assertIn(self._reject_code_for(room), (1008, "HTTP-REJECT"))

    def test_uppercase_campaign_room_is_closed(self):
        """Bypass confirmado em produção: precisa fechar, não abrir sem auth."""
        room = "CAMPAIGN-6F1C0B0E-1A2B-4C3D-9E8F-7A6B5C4D3E2F"
        self.assertIn(self._reject_code_for(room), (1008, "HTTP-REJECT"))

    def test_mixed_case_campaign_room_is_closed(self):
        room = "Campaign-6F1C0B0E-1A2B-4C3D-9E8F-7A6B5C4D3E2F"
        self.assertIn(self._reject_code_for(room), (1008, "HTTP-REJECT"))

    def test_non_canonical_room_is_rejected_before_join(self):
        """Sala que não é de campanha não pode mais ser inscrita no canal."""
        self.assertIn(self._reject_code_for("MESA_01"), (1008, "HTTP-REJECT"))


if __name__ == "__main__":
    unittest.main()
