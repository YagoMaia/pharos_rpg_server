import asyncio
from collections import defaultdict

from app.managers.connection import ConnectionManager
from app.models.domain import CombatSession
from app.services.gm_service import GMService
from app.services.player_service import PlayerService


GM_EVENTS = {
    "GM_CONNECT",
    "GM_UPDATE_COMBATANT",
    "GM_ADD_NPC",
    "GM_REMOVE_COMBATANT",
    "GM_ADVANCE_PHASE",
    "END_COMBAT",
    "APPLY_CONDITION",
    "REMOVE_CONDITION",
}


class AuthorizationError(ValueError):
    pass


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

    def _owned_by_client(self, state: CombatSession, combatant_id: str, client_id: str) -> bool:
        return state.combatantOwners.get(combatant_id) == client_id

    @staticmethod
    def _authorize_sheet(state: CombatSession, actor_id: str, identity: dict):
        actor = state.combatants.get(actor_id)
        if actor and actor.type == "pet":
            actor = state.combatants.get(actor.ownerId)
        if not actor or actor.sheetId != identity["character_id"]:
            raise AuthorizationError("A ficha vinculada não controla este personagem.")

    def _authorize_gm(self, state: CombatSession, client_id: str, identity: dict):
        if state.gmClientId is None:
            state.gmClientId = client_id
        if state.gmClientId != client_id:
            raise AuthorizationError("Apenas o GM da sessão pode executar este comando.")
        identity["role"] = "gm"

    def _authorize_join(self, state: CombatSession, payload: dict, client_id: str, identity: dict):
        combatant = payload.get("combatant") if isinstance(payload, dict) else None
        if not isinstance(combatant, dict) or not combatant.get("id"):
            raise AuthorizationError("JOIN_SESSION requer combatant.id válido.")

        combatant_id = combatant["id"]
        owner = state.combatantOwners.get(combatant_id)
        if owner is not None and owner != client_id:
            raise AuthorizationError("Este personagem já pertence a outro usuário conectado.")

        state.combatantOwners[combatant_id] = client_id
        identity["role"] = "player"
        identity["player_id"] = combatant_id

        pet = payload.get("pet")
        if isinstance(pet, dict) and pet.get("id"):
            if pet.get("ownerId") != combatant_id:
                raise AuthorizationError("Pet deve pertencer ao personagem enviado no JOIN_SESSION.")
            pet_owner = state.combatantOwners.get(pet["id"])
            if pet_owner is not None and pet_owner != client_id:
                raise AuthorizationError("Este pet já pertence a outro usuário conectado.")
            state.combatantOwners[pet["id"]] = client_id

    def _authorize_player_command(self, state: CombatSession, event_type: str, payload: dict, identity: dict):
        if identity.get("role") == "gm":
            return

        client_id = identity.get("client_id")
        if not client_id:
            raise AuthorizationError("Comando requer clientId.")

        if event_type == "END_TURN":
            if not state.turn_order:
                return
            active_id = state.turn_order[state.turn_index]
            if not self._owned_by_client(state, active_id, client_id):
                raise AuthorizationError("Apenas o dono do turno atual ou o GM pode encerrar o turno.")
            if identity.get("authenticated"):
                self._authorize_sheet(state, active_id, identity)
            return

        actor_id = payload.get("attackerId") or payload.get("combatantId")
        if not actor_id or not self._owned_by_client(state, actor_id, client_id):
            raise AuthorizationError("Você só pode comandar seu personagem ou pet.")
        if identity.get("authenticated"):
            self._authorize_sheet(state, actor_id, identity)

    def _authorize(self, state: CombatSession, event_type: str, payload: dict, identity: dict | None):
        if identity is None:
            return

        if identity.get("authenticated"):
            client_id = identity["client_id"]
            if event_type in GM_EVENTS:
                if identity["role"] != "gm":
                    raise AuthorizationError("Apenas o mestre da campanha pode executar este comando.")
                self._authorize_gm(state, client_id, identity)
                return
            if event_type == "JOIN_SESSION":
                combatant = payload.get("combatant") if isinstance(payload, dict) else None
                if identity["role"] != "player" or not isinstance(combatant, dict):
                    raise AuthorizationError("Apenas jogadores vinculados podem entrar no combate.")
                if combatant.get("type") != "player" or combatant.get("sheetId") != identity["character_id"]:
                    raise AuthorizationError("A ficha enviada não corresponde à ficha vinculada.")
                existing = state.combatants.get(combatant.get("id"))
                if existing and existing.type != "player":
                    raise AuthorizationError("Este ID de personagem já está em uso.")
                if existing and existing.sheetId != identity["character_id"]:
                    raise AuthorizationError("A ficha vinculada não corresponde ao personagem existente.")
                self._authorize_join(state, payload, client_id, identity)
                return
            self._authorize_player_command(state, event_type, payload or {}, identity)
            return

        client_id = None
        if isinstance(payload, dict):
            client_id = payload.get("clientId")
        client_id = client_id or identity.get("client_id")
        if client_id:
            identity["client_id"] = client_id

        if event_type == "GM_CONNECT":
            if not client_id:
                raise AuthorizationError("GM_CONNECT requer clientId.")
            self._authorize_gm(state, client_id, identity)
            return

        if event_type in GM_EVENTS:
            if not client_id:
                raise AuthorizationError("Comando de GM requer clientId.")
            self._authorize_gm(state, client_id, identity)
            return

        if event_type == "JOIN_SESSION":
            if not client_id:
                raise AuthorizationError("JOIN_SESSION requer clientId.")
            self._authorize_join(state, payload, client_id, identity)
            return

        self._authorize_player_command(state, event_type, payload or {}, identity)

    async def handle_message(self, session_id: str, data: dict, identity: dict | None = None):
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
            self._authorize(state, event_type, payload, identity)
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
