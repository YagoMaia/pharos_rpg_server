import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from connection import manager
from models import ActionType, Character, CombatState, EntityType, PlayerAction

app = FastAPI()


@app.websocket("/ws/{session_id}/{character_id}")
async def combat_endpoint(websocket: WebSocket, session_id: str, character_id: str):
    await manager.connect(session_id, websocket)

    try:
        # Envia estado inicial ao conectar
        current_state = await manager.get_state(session_id)
        if current_state:
            await websocket.send_text(current_state.model_dump_json())

        while True:
            data = await websocket.receive_text()
            action_data = json.loads(data)
            action = PlayerAction(**action_data)

            # Recuperar estado atual do Redis (Single Source of Truth)
            state = await manager.get_state(session_id)
            if not state:
                continue

            # --- Validação de Regras de Negócio ---

            # 1. É a vez de quem enviou a mensagem?
            current_actor_id = state.turn_order[state.turn_index]
            if current_actor_id != character_id:
                await websocket.send_json({"error": "Não é seu turno!"})
                continue

            # --- Processamento da Ação ---

            if action.action == ActionType.ATTACK:
                if not action.target_id or not action.damage_amount:
                    return
                target = state.entities.get(action.target_id)
                if target:
                    target.hp -= action.damage_amount
                    state.logs.append(
                        f"{state.entities[character_id].name} atacou {target.name} causando {action.damage_amount} de dano."
                    )

                    # Verifica Morte
                    if target.hp <= 0:
                        target.hp = 0
                        state.logs.append(f"{target.name} caiu inconsciente!")

            elif action.action == ActionType.END_TURN:
                # Avança o turno
                state.turn_index = (state.turn_index + 1) % len(state.turn_order)
                next_actor = state.entities[state.turn_order[state.turn_index]]
                state.logs.append(f"Fim de turno. Vez de {next_actor.name}.")

            # --- Publicação ---
            # Salva no Redis e avisa todos (Players e Mestre)
            await manager.broadcast_state(session_id, state)

    except WebSocketDisconnect:
        manager.disconnect(session_id, websocket)


@app.post("/create-battle/{session_id}")
async def create_battle(session_id: str):
    """Inicializa uma batalha fake para testes"""

    # Criando dados mockados
    initial_state = CombatState(
        session_id=session_id,
        turn_order=["player1", "npc1", "player2"],  # Ordem de iniciativa
        entities={
            "player1": Character(
                id="player1",
                name="Aragorn",
                type=EntityType.PLAYER,
                hp=50,
                max_hp=50,
                initiative=10,
            ),
            "player2": Character(
                id="player2",
                name="Legolas",
                type=EntityType.PLAYER,
                hp=40,
                max_hp=40,
                initiative=8,
            ),
            "npc1": Character(
                id="npc1",
                name="Orc Berserker",
                type=EntityType.NPC,
                hp=60,
                max_hp=60,
                initiative=9,
            ),
        },
        logs=["Batalha iniciada!"],
    )

    # Salva no Redis
    await manager.broadcast_state(session_id, initial_state)
    return {"message": "Batalha criada!", "state": initial_state}
