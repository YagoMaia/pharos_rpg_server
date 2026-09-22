import asyncio
from collections import defaultdict

from app.managers.connection import ConnectionManager
from app.models.domain import CombatSession
from app.services.gm_service import GMService
from app.services.player_service import PlayerService


class GameLogic:
    def __init__(self, manager: ConnectionManager):
        self.manager = manager
        self._locks = defaultdict(asyncio.Lock)

    def _create_session(self, session_id: str) -> CombatSession:
        """Cria uma nova instância de sessão vazia"""
        return CombatSession(
            session_id=session_id,
            turn_order=[],
            combatants={},  # Dict vazio
            logs=[f"Sessão {session_id} iniciada."],
        )

    async def handle_message(self, session_id: str, data: dict):
        session_id = session_id.lower()
        event_type = data.get("type")

        # Heartbeat - responde imediatamente sem lock nem estado
        if event_type == "PING":
            return

        async with self._locks[session_id]:
            # 1. Recupera estado atual
            state = await self.manager.get_state(session_id)
            payload = data.get("payload")

            # 2. Inicialização da Sessão (Corrigido e sem duplicidade)
            if not state:
                if event_type in ["JOIN_SESSION", "GM_CONNECT"]:
                    state = self._create_session(session_id)
                else:
                    return  # Não processa comandos se a sala não existe

            # 3. Validação de Payload
            if not payload and event_type not in [
                "END_TURN",
                "END_COMBAT",
                "GM_CONNECT",
            ]:
                return

            # 4. Processamento da Lógica
            previous_combatants = set(state.combatants)
            match event_type:
                case "GM_CONNECT":
                    await GMService.gm_connect(state)
                case "GM_UPDATE_COMBATANT":
                    await GMService.update_combatant(state, payload)
                case "GM_ADD_NPC":
                    await GMService.add_npc(state, payload)
                case "GM_REMOVE_COMBATANT":
                    await GMService.remove_combatant(state, payload)
                case "GM_ADVANCE_PHASE":
                    await GMService.advance_phase(state, payload)
                case "END_COMBAT":
                    await GMService.end_combat(state)
                case "JOIN_SESSION":
                    await PlayerService.join_session(state, payload)
                case "MANUAL_UPDATE":
                    await PlayerService.update_hp(state, payload)
                case "CHANGE_STANCE":
                    await PlayerService.change_stance(state, payload)
                case "END_TURN":
                    await PlayerService.next_turn(state)
                case "RESOLVE_ACTION":
                    await PlayerService.resolve_action(state, payload)
                case "ROLL_DEATH_SAVE":
                    await PlayerService.roll_death_save(state, payload)
                case "APPLY_CONDITION":
                    await PlayerService.apply_condition(state, payload)
                case "REMOVE_CONDITION":
                    await PlayerService.remove_condition(state, payload)

            # 5. Persistência e Broadcast (Ainda dentro do Lock)
            if state:
                # Broadcast TOTAL apenas para mudanças de lista/estrutura
                structural_events = [
                    "JOIN_SESSION",
                    "GM_CONNECT",
                    "END_COMBAT",
                    "GM_ADD_NPC",
                    "GM_REMOVE_COMBATANT",
                    "GM_ADVANCE_PHASE",
                ]
                if event_type in structural_events or previous_combatants != set(state.combatants):
                    # broadcast_state já faz set + publish
                    await self.manager.broadcast_state(session_id, state)
                else:
                    # Apenas persiste (o evento delta já foi enviado pelo service)
                    await self.manager.redis.set(
                        f"state:{session_id}", state.model_dump_json(), ex=3600
                    )
