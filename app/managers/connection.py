import asyncio
from typing import Optional

from fastapi import WebSocket
from redis import asyncio as aioredis

from app.core.config import settings
from app.models.domain import CombatSession


class ConnectionManager:
    def __init__(self):
        self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, session_id: str, websocket: WebSocket):
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)

        # Inscrever-se no canal do Redis para essa sessão
        # Nota: Em produção, o subscriber deve rodar em uma task separada para não bloquear
        asyncio.create_task(self.subscribe_channel(session_id))

    def disconnect(self, session_id: str, websocket: WebSocket):
        if session_id in self.active_connections:
            self.active_connections[session_id].remove(websocket)

    async def subscribe_channel(self, session_id: str):
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"channel:{session_id}")

        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    # Envia para TODOS os sockets conectados nesta sessão
                    if session_id in self.active_connections:
                        for connection in self.active_connections[session_id]:
                            try:
                                await connection.send_text(data)
                            except Exception as e:
                                print(f"Erro ao enviar socket: {e}")
        except Exception as e:
            print(f"Erro no PubSub: {e}")

    async def broadcast_state(self, session_id: str, state: CombatSession):
        # 1. Serializa o Objeto Pydantic para String JSON
        # ATENÇÃO: Se usar Pydantic V2, use state.model_dump_json()
        state_json = state.model_dump_json()

        print(f"📡 Enviando broadcast para {session_id}...")  # Debug

        # 2. Salva no Redis (Persistência)
        await self.redis.set(f"state:{session_id}", state_json, ex=3600)

        # 3. Publica no canal (Para o subscribe_channel pegar)
        await self.redis.publish(f"channel:{session_id}", state_json)

    async def get_state(self, session_id: str) -> Optional[CombatSession]:
        data = await self.redis.get(f"state:{session_id}")
        if data:
            return CombatSession.model_validate_json(data)
        return None


manager = ConnectionManager()
