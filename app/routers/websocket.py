import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.managers.connection import manager
from app.services.engine import GameLogic

router = APIRouter()
game_logic = GameLogic(manager)


@router.websocket("/ws/{session_id}/{character_id}")
async def combat_endpoint(websocket: WebSocket, session_id: str):
    await manager.connect(session_id, websocket)
    try:
        while True:
            data_text = await websocket.receive_text()
            data_json = json.loads(data_text)

            # Delega tudo para a classe de lógica
            await game_logic.handle_message(session_id, data_json)

    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)


# @router.post("/create-battle/{session_id}")
# async def create_battle(session_id: str):
#     """Inicializa uma batalha fake para testes"""

#     # Criando dados mockados
#     initial_state = CombatSession(
#         session_id=session_id,
#         turn_order=["player1", "npc1", "player2"],  # Ordem de iniciativa
#         entities={
#             "player1": Character(
#                 id="player1",
#                 name="Aragorn",
#                 type=EntityType.PLAYER,
#                 hp=50,
#                 max_hp=50,
#                 initiative=10,
#             ),
#             "player2": Character(
#                 id="player2",
#                 name="Legolas",
#                 type=EntityType.PLAYER,
#                 hp=40,
#                 max_hp=40,
#                 initiative=8,
#             ),
#             "npc1": Character(
#                 id="npc1",
#                 name="Orc Berserker",
#                 type=EntityType.NPC,
#                 hp=60,
#                 max_hp=60,
#                 initiative=9,
#             ),
#         },
#         logs=["Batalha iniciada!"],
#     )

#     # Salva no Redis
#     await manager.broadcast_state(session_id, initial_state)
#     return {"message": "Batalha criada!", "state": initial_state}
