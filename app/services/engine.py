from app.models.domain import CombatSession


class GameLogic:
    def __init__(self, manager):
        self.manager = manager  # Instância do ConnectionManager criada anteriormente

    async def handle_message(self, session_id: str, data: dict):
        # 1. Recupera estado atual do Redis
        state = await self.manager.get_state(session_id)

        # Se não existe estado e o evento não é JOIN ou CREATE, erro
        if not state and data["type"] != "JOIN_SESSION":
            return

        event_type = data.get("type")
        payload = data.get("payload")

        if not payload:
            return

        # --- Roteamento de Eventos ---

        if event_type == "UPDATE_HP":
            await self._handle_update_hp(state, payload)

        elif event_type == "CHANGE_STANCE":
            await self._handle_change_stance(state, payload)

        elif event_type == "NEXT_TURN":
            await self._handle_next_turn(state)

        elif event_type == "USE_SKILL":
            await self._handle_use_skill(state, payload)

        # Salva e notifica a todos
        if state:
            await self.manager.broadcast_state(session_id, state)

    # --- Implementação das Regras ---

    async def _handle_update_hp(self, state: CombatSession, payload: dict):
        target_id = payload["combatantId"]
        new_hp = payload["newHp"]

        combatant = state.combatants.get(target_id)
        if combatant:
            diff = combatant.hp.current - new_hp
            combatant.hp.current = new_hp

            # Log simples
            action_text = "curado em" if diff < 0 else "recebeu dano de"
            state.logs.append(f"{combatant.name} {action_text} {abs(diff)} PV.")

            if combatant.hp.current == 0:
                state.logs.append(f"{combatant.name} caiu!")

    async def _handle_change_stance(self, state: CombatSession, payload: dict):
        c_id = payload["combatantId"]
        s_id = payload["stanceId"]
        new_ac = payload["newAC"]  # Front já calculou (Base + Bonus da Stance)

        combatant = state.combatants.get(c_id)
        if combatant:
            # Busca o nome da postura para o log
            stance_name = next(
                (s.name for s in combatant.stances if s.id == s_id), "Desconhecida"
            )

            combatant.activeStanceId = s_id
            combatant.armorClass = new_ac
            state.logs.append(
                f"{combatant.name} assumiu a postura {stance_name} (CA {new_ac})."
            )

    async def _handle_next_turn(self, state: CombatSession):
        # 1. Finaliza turno do atual
        current_id = state.turn_order[state.turn_index]
        current_actor = state.combatants.get(current_id)

        if not current_actor:
            return

        # Opcional: Resetar reações do personagem que acabou de agir?
        current_actor.turnActions.bonus = True
        current_actor.turnActions.standard = True
        current_actor.turnActions.reaction = True
        # Depende da regra do seu sistema (Geralmente reseta no INÍCIO do turno dele)

        # 2. Calcula próximo índice
        state.turn_index = (state.turn_index + 1) % len(state.turn_order)

        # 3. Prepara o próximo
        next_id = state.turn_order[state.turn_index]
        next_actor = state.combatants.get(next_id)

        if next_actor:
            # REGRA DE OURO: Resetar economia de ações no INÍCIO do turno
            next_actor.turnActions.standard = True
            next_actor.turnActions.bonus = True
            next_actor.turnActions.reaction = True
            # (Reação às vezes reseta no início da RODADA, ajuste conforme seu sistema)

            state.logs.append(f"--- Turno de {next_actor.name} ---")

    async def _handle_use_skill(self, state: CombatSession, payload: dict):
        # Payload esperado: { combatantId, skillId }
        c_id = payload["combatantId"]
        s_id = payload["skillId"]
        combatant = state.combatants.get(c_id)

        if not combatant:
            return

        skill = next((s for s in combatant.skills if s.id == s_id), None)

        if combatant and skill:
            # Verifica custos
            cost_type = skill.actionType  # "Padrão", "Ação Bônus", etc.

            can_act = False
            if cost_type == "Padrão" and combatant.turnActions.standard:
                combatant.turnActions.standard = False
                can_act = True
            elif cost_type == "Ação Bônus" and combatant.turnActions.bonus:
                combatant.turnActions.bonus = False
                can_act = True
            # ... outros tipos

            if can_act:
                # Gasta Foco
                if combatant.currentFocus >= skill.cost:
                    combatant.currentFocus -= skill.cost
                    state.logs.append(
                        f"{combatant.name} usou {skill.name} (Custo: {skill.cost} Foco)."
                    )
                else:
                    # Se falhar validação, poderíamos enviar uma msg de erro específica apenas para o usuário
                    pass
