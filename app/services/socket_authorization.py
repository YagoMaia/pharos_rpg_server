"""Revalidação compartilhada entre comandos e broadcasts de uma conexão."""

from dataclasses import dataclass, field

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.services.campaign_store import CampaignStore
from app.services.errors import CampaignError


@dataclass(frozen=True)
class CampaignSocketAuthorization:
    """Snapshot do vínculo autenticado; `authorization()` verifica o estado atual."""

    store: CampaignStore
    token: str = field(repr=False)
    campaign_id: str
    role: str
    character_id: str | None

    def __call__(self) -> bool:
        try:
            current = self.store.combat_member(self.token, self.campaign_id)
        except CampaignError:
            return False
        return (
            current["role"] == self.role
            and current["character_id"] == self.character_id
        )


async def close_revoked_socket(websocket: WebSocket) -> None:
    """Fecha com 1008 sem reenviar close se outro caminho já fechou o socket."""
    if websocket.application_state != WebSocketState.DISCONNECTED:
        await websocket.close(code=1008)
