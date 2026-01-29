from app.models.domain import Combatant, CombatSession
from app.models.events import RemoveCombatantPayload


class GMService:
    @staticmethod
    async def add_npc(state: CombatSession, payload: dict):
        new_npc = Combatant(**payload)
        state.combatants[new_npc.id] = new_npc
        state.turn_order.append(new_npc.id)
        state.turn_order.sort(
            key=lambda x: state.combatants[x].initiative, reverse=True
        )
        state.logs.append(f"(GM) Adicionou: {new_npc.name}")

    @staticmethod
    async def gm_connect(state: CombatSession):
        state.logs.append("O Mestre (GM) conectou na sala.")

    @staticmethod
    async def update_combatant(state: CombatSession, payload: dict):
        target_id = payload["combatantId"]
        value = payload["value"]
        field = payload["field"]

        combatant = state.combatants.get(target_id)

        if combatant:
            if field == "hp_current":
                diff = combatant.hp.current - value
                combatant.hp.current = value

                action_text = "curou" if diff < 0 else "deu dano"
                state.logs.append(
                    f"Mestre {action_text} {combatant.name} - {abs(diff)} PV."
                )

                if combatant.hp.current == 0:
                    state.logs.append(f"{combatant.name} caiu!")
            elif field == "current_focus":
                diff = combatant.currentFocus - value
                combatant.currentFocus = value

                action_text = "recuperou" if diff < 0 else "gastou"
                state.logs.append(
                    f"Mestre {action_text} {combatant.name} - {abs(diff)} Foco."
                )

                if combatant.currentFocus == 0:
                    state.logs.append(f"{combatant.name} sem foco!")

    @staticmethod
    async def remove_combatant(state: CombatSession, payload: dict):
        """
        Remove um combatente (morto ou deletado) e ajusta o índice de turno
        para a fila não pular ninguém incorretamente.
        """
        # Converte payload
        data = RemoveCombatantPayload(**payload)
        c_id = data.combatantId

        if c_id not in state.combatants:
            return

        # 1. Recupera nome para o log antes de deletar
        name = state.combatants[c_id].name

        # 2. Descobre o índice dele na ordem atual
        try:
            removed_index = state.turn_order.index(c_id)
        except ValueError:
            removed_index = -1

        # 3. Remove dos dados
        del state.combatants[c_id]
        if c_id in state.turn_order:
            state.turn_order.remove(c_id)

        if removed_index != -1 and removed_index < state.turn_index:
            state.turn_index = max(0, state.turn_index - 1)

        if len(state.turn_order) > 0:
            state.turn_index = state.turn_index % len(state.turn_order)
        else:
            state.turn_index = 0

        state.logs.append(f"{name} foi removido do combate.")

    @staticmethod
    async def end_combat(state: CombatSession):
        # 1. Identificar quem são os jogadores (para mantê-los)
        player_ids = [
            c_id for c_id, char in state.combatants.items() if char.type == "player"
        ]

        # 2. Resetar Ações dos Jogadores (Restaurar economia de ação)
        for p_id in player_ids:
            player = state.combatants[p_id]
            player.turnActions.standard = True
            player.turnActions.bonus = True
            player.turnActions.reaction = True
            # Opcional: Remover posturas de combate?
            # player.activeStanceId = None

        # 3. Limpar NPCs da memória (Combatants)
        # Cria um novo dict apenas com os players
        state.combatants = {p_id: state.combatants[p_id] for p_id in player_ids}

        # 4. Limpar a Ordem de Turno
        # Opção A: Zera tudo (Ninguém tem turno)
        state.turn_order = []
        state.turn_index = 0

        # Opção B (Recomendada): Mantém players na lista para eles verem seus HPs,
        # mas sem ordem de iniciativa específica.
        state.turn_order = player_ids

        # Log Final
        print("End Combat")
        state.logs.append("🏆 O Mestre encerrou o combate! (Modo Narrativo)")
