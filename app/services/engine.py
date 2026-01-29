from app.managers.connection import ConnectionManager
from app.models.domain import CombatSession
from app.services.gm_service import GMService
from app.services.player_service import PlayerService


class GameLogic:
    def __init__(self, manager: ConnectionManager):
        self.manager = manager

    def _create_session(self, session_id: str) -> CombatSession:
        """Cria uma nova instância de sessão vazia"""
        return CombatSession(
            session_id=session_id,
            turn_order=[],
            combatants={},  # Dict vazio
            logs=[f"Sessão {session_id} iniciada."],
        )

    async def handle_message(self, session_id: str, data: dict):
        # 1. Recupera estado atual do Redis
        state = await self.manager.get_state(session_id)

        event_type = data.get("type")
        payload = data.get("payload")

        if not state:
            if event_type in ["JOIN_SESSION", "GM_CONNECT"]:
                state = self._create_session(session_id)
                print(f"Sessão {session_id} criada por {event_type}")
            else:
                return

        if not payload:
            return

        # --- Roteamento de Eventos ---

        match event_type:
            case "GM_CONNECT":
                await GMService.gm_connect(state)

            case "GM_UPDATE_COMBATANT":
                await GMService.update_combatant(state, payload)

            case "GM_ADD_NPC":
                await GMService.add_npc(state, payload)

            case "GM_REMOVE_COMBATANT":
                await GMService.remove_combatant(state, payload)

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

        # Salva e notifica a todos
        if state and state.session_id:
            await self.manager.broadcast_state(session_id, state)

        print(state.turn_order)
        print(state.turn_index)
        # print(state.activeTurnId)
