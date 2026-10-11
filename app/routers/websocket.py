import asyncio
import json
import time
from uuid import UUID
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.managers.connection import manager
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignError
from app.services.engine import GameLogic

router = APIRouter()
game_logic = GameLogic(manager)

_MAX_MESSAGES_PER_SECOND = 10


async def authenticate_campaign(websocket: WebSocket, session_id: str):
    """Valida o primeiro frame antes de inscrever o socket no canal Redis."""
    campaign_id = session_id.removeprefix("campaign-")
    try:
        if str(UUID(campaign_id)) != campaign_id:
            raise ValueError("ID inválido")
        await websocket.accept()
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        data = json.loads(raw) if len(raw) <= 2048 else {}
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(data, dict) or data.get("type") != "AUTH_CAMPAIGN" or not isinstance(payload, dict):
            raise ValueError("Autenticação necessária")
        token = payload.get("token")
        if not isinstance(token, str) or not 0 < len(token) <= 128:
            raise ValueError("Token inválido")
        store = get_campaign_store()
        member = store.combat_member(token, campaign_id)
    except (ValueError, json.JSONDecodeError, CampaignError, asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close(code=1008)
        return None
    identity = {"authenticated": True, "role": member["role"],
                "client_id": f"user:{member['user_id']}", "player_id": None,
                "character_id": member["character_id"]}
    await manager.connect(session_id, websocket, already_accepted=True)
    await websocket.send_json({"type": "AUTH_OK", "role": member["role"]})
    return identity, token, store

@router.websocket('/ws/{session_id}')
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    is_campaign = session_id.startswith("campaign-")
    if is_campaign:
        handshake = await authenticate_campaign(websocket, session_id)
        if handshake is None:
            return
        identity, token, store = handshake
    else:
        await manager.connect(session_id, websocket)
        identity = {'role': None, 'client_id': None, 'player_id': None}

    # Estado de rate limiting por conexao (sliding window)
    msg_count = 0
    window_start = time.monotonic()

    try:
        while True:
            data_text = await websocket.receive_text()
            if is_campaign:
                try:
                    member = store.combat_member(token, session_id.removeprefix("campaign-"))
                    if member["role"] != identity["role"] or member["character_id"] != identity["character_id"]:
                        raise CampaignError(403, "Vínculo alterado.")
                except CampaignError:
                    await websocket.close(code=1008)
                    return

            # Rate limiting: reinicia janela a cada 1 segundo
            now = time.monotonic()
            if now - window_start >= 1.0:
                msg_count = 0
                window_start = now
            msg_count += 1
            if msg_count > _MAX_MESSAGES_PER_SECOND:
                await websocket.send_json({'error': 'Muitas mensagens. Aguarde um momento.'})
                continue

            try:
                data_json = json.loads(data_text)

                # PING/PONG: tratado aqui onde temos acesso ao WebSocket
                # (handle_message nao tem acesso ao objeto WebSocket)
                if data_json.get('type') == 'PING':
                    await websocket.send_text('{"type": "PONG"}')
                    continue

                try:
                    await game_logic.handle_message(session_id, data_json, identity)
                except ValueError as e:
                    await websocket.send_json({'error': str(e)})
            except json.JSONDecodeError:
                await websocket.send_json({'error': 'Formato JSON invalido'})
            except Exception as e:
                print(f'Erro ao processar logica: {e}')
                await websocket.send_json(
                    {'error': 'Erro interno ao processar comando'}
                )
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(session_id, websocket)
