import asyncio
import json

from fastapi import WebSocket
from redis import asyncio as aioredis

from app.core.config import settings
from app.models.domain import CombatSession


class ConnectionManager:
    def __init__(self):
        self.redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        self.active_connections: dict[str, list[WebSocket]] = {}
        self.pubsub_tasks: dict[str, asyncio.Task] = {}

    async def connect(self, session_id: str, websocket: WebSocket):
        session_id = session_id.lower()
        await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)

        if session_id not in self.pubsub_tasks:
            self.pubsub_tasks[session_id] = asyncio.create_task(
                self.subscribe_channel(session_id)
            )

    def disconnect(self, session_id: str, websocket: WebSocket):
        session_id = session_id.lower()
        if session_id in self.active_connections:
            self.active_connections[session_id].remove(websocket)
            if not self.active_connections[session_id]:
                if session_id in self.pubsub_tasks:
                    self.pubsub_tasks[session_id].cancel()
                    del self.pubsub_tasks[session_id]
                del self.active_connections[session_id]

    async def subscribe_channel(self, session_id: str):
        """Escuta única que distribui para todos da sala"""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(f"channel:{session_id}")
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    data = message["data"]
                    if session_id in self.active_connections:
                        targets = list(self.active_connections[session_id])
                        results = await asyncio.gather(
                            *[conn.send_text(data) for conn in targets],
                            return_exceptions=True,
                        )
                        # Remove conexões mortas que falharam no envio
                        for conn, result in zip(targets, results):
                            if isinstance(result, Exception):
                                try:
                                    self.active_connections[session_id].remove(conn)
                                except ValueError:
                                    pass
        except asyncio.CancelledError:
            await pubsub.unsubscribe(f"channel:{session_id}")
        except Exception as e:
            print(f"Erro central no PubSub da sessão {session_id}: {e}")

    async def broadcast_state(self, session_id: str, state: CombatSession):
        state_json = state.model_dump_json()

        await self.redis.set(f"state:{session_id}", state_json, ex=3600)
        await self.redis.publish(f"channel:{session_id}", state_json)

    async def get_state(self, session_id: str) -> CombatSession | None:
        # Use EXATAMENTE o mesmo prefixo que o engine.py usa para salvar
        raw_state = await self.redis.get(f"state:{session_id}")
        if not raw_state:
            return None
        return CombatSession.model_validate_json(raw_state)

    async def broadcast_event(self, session_id: str, event_type: str, payload: dict):
        """Envia apenas um evento específico (ex: HP_UPDATE) em vez do estado todo"""
        event_data = {"type": event_type, "payload": payload}
        await self.redis.publish(f"channel:{session_id}", json.dumps(event_data))


manager = ConnectionManager()
