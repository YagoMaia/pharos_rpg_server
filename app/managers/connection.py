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
        self.subscription_ready: dict[str, asyncio.Event] = {}

    async def connect(self, session_id: str, websocket: WebSocket, already_accepted: bool = False):
        session_id = session_id.lower()
        if not already_accepted:
            await websocket.accept()
        if session_id not in self.active_connections:
            self.active_connections[session_id] = []
        self.active_connections[session_id].append(websocket)

        existing_task = self.pubsub_tasks.get(session_id)
        if existing_task is None or existing_task.done():
            self.subscription_ready[session_id] = asyncio.Event()
            self.pubsub_tasks[session_id] = asyncio.create_task(
                self.subscribe_channel(session_id)
            )
        try:
            # JOIN/GM_CONNECT só pode publicar depois que o canal estiver inscrito.
            await asyncio.wait_for(self.subscription_ready[session_id].wait(), timeout=5.0)
        except BaseException:
            self.disconnect(session_id, websocket)
            await websocket.close()
            raise

    def disconnect(self, session_id: str, websocket: WebSocket):
        session_id = session_id.lower()
        if session_id in self.active_connections:
            try:
                self.active_connections[session_id].remove(websocket)
            except ValueError:
                pass
            if not self.active_connections[session_id]:
                if session_id in self.pubsub_tasks:
                    self.pubsub_tasks[session_id].cancel()
                    del self.pubsub_tasks[session_id]
                del self.active_connections[session_id]
                self.subscription_ready.pop(session_id, None)

    async def _safe_send(self, session_id: str, conn: WebSocket, data: str):
        """Limita a espera por cliente; o assinante preserva a ordem dos eventos."""
        try:
            await asyncio.wait_for(conn.send_text(data), timeout=5.0)
        except Exception:
            self.disconnect(session_id, conn)

    async def subscribe_channel(self, session_id: str):
        """Escuta unica resiliente a falhas do Redis com backoff exponencial."""
        retry_delay = 1.0
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.subscribe(f"channel:{session_id}")
                retry_delay = 1.0
                async for message in pubsub.listen():
                    if message["type"] == "subscribe":
                        ready = self.subscription_ready.get(session_id)
                        if ready:
                            ready.set()
                    if message["type"] == "message":
                        data = message["data"]
                        if session_id in self.active_connections:
                            targets = list(self.active_connections[session_id])
                            await asyncio.gather(*(
                                self._safe_send(session_id, conn, data) for conn in targets
                            ))
            except asyncio.CancelledError:
                await pubsub.unsubscribe(f"channel:{session_id}")
                return
            except Exception as e:
                ready = self.subscription_ready.get(session_id)
                if ready:
                    ready.clear()
                print(f"[PubSub] Erro sessao {session_id}: {e}. Reconectando em {retry_delay:.1f}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)
            finally:
                try:
                    await pubsub.close()
                except Exception:
                    pass
            if session_id not in self.active_connections:
                break

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
