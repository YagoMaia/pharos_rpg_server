import json
import time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.managers.connection import manager
from app.services.engine import GameLogic

router = APIRouter()
game_logic = GameLogic(manager)

_MAX_MESSAGES_PER_SECOND = 10

@router.websocket('/ws/{session_id}')
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(session_id, websocket)

    # Estado de rate limiting por conexao (sliding window)
    msg_count = 0
    window_start = time.monotonic()

    try:
        while True:
            data_text = await websocket.receive_text()

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

                await game_logic.handle_message(session_id, data_json)
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
