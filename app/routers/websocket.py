import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.managers.connection import manager
from app.services.engine import GameLogic

router = APIRouter()
game_logic = GameLogic(manager)


@router.websocket("/ws/{session_id}")
async def combat_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(session_id, websocket)
    try:
        while True:
            data_text = await websocket.receive_text()
            try:
                data_json = json.loads(data_text)
                await game_logic.handle_message(session_id, data_json)
            except json.JSONDecodeError:
                await websocket.send_json({"error": "Formato JSON inválido"})
            except Exception as e:
                print(f"Erro ao processar lógica: {e}")
                await websocket.send_json(
                    {"error": "Erro interno ao processar comando"}
                )
    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)
