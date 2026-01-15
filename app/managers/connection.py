import asyncio
from typing import Optional

from core.config import settings
from fastapi import WebSocket
from redis import asyncio as aioredis

from models.domain import CombatSession


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
        """Escuta atualizações do Redis e repassa via WebSocket"""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"channel:{session_id}")

        async for message in pubsub.listen():
            if message["type"] == "message":
                # Envia a mensagem para TODOS os sockets conectados nesta instância
                data = message["data"]
                if session_id in self.active_connections:
                    for connection in self.active_connections[session_id]:
                        await connection.send_text(data)

    async def broadcast_state(self, session_id: str, state: CombatSession):
        """Salva o estado e publica no Redis"""
        # 1. Persistir o estado (TTL de 1 hora para não lixar memória)
        await self.redis.set(f"state:{session_id}", state.model_dump_json(), ex=3600)

        # 2. Publicar evento
        await self.redis.publish(f"channel:{session_id}", state.model_dump_json())

    async def get_state(self, session_id: str) -> Optional[CombatSession]:
        data = await self.redis.get(f"state:{session_id}")
        if data:
            return CombatSession.model_validate_json(data)
        return None


manager = ConnectionManager()
