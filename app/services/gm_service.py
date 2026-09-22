from app.core.utils import static_method_log
from app.models.domain import Combatant, CombatSession, DeathSaves
from app.models.events import RemoveCombatantPayload


class GMService:
    @staticmethod
    @static_method_log
    async def advance_phase(state: CombatSession, payload: dict):
        actor = state.combatants.get(payload.get("combatantId"))
        if not actor or actor.type != "npc":
            return
        index = actor.currentPhaseIndex
        # O índice esperado torna cliques repetidos idempotentes.
        if payload.get("expectedPhaseIndex", index) != index or index >= len(actor.phases):
            return
        phase = actor.phases[index]
        stance = next((s for s in actor.stances if s.id == actor.activeStanceId), None)
        bonus = (stance.acBonus or 0) if stance else 0
        if stance and stance.acAttribute:
            attribute = actor.attributes.get(stance.acAttribute)
            bonus += attribute.modifier if attribute else 0
        actor.armorClass = phase.armorClass if phase.armorClass is not None else actor.armorClass - bonus
        actor.activeStanceId = None
        for field in ("name", "image", "stances", "skills", "weapons"):
            value = getattr(phase, field)
            if value is not None:
                setattr(actor, field, value)
        if phase.maxHp is not None:
            actor.hp.max = phase.maxHp
        actor.hp.current = actor.hp.max
        actor.deathSaves = DeathSaves()
        # Condições, foco e ações permanecem: mudar de fase não concede outro turno.
        actor.currentPhaseIndex += 1
        state.logs.append(f"{actor.name} avançou para a fase {actor.currentPhaseIndex + 1}.")

    @staticmethod
    @static_method_log
    async def add_npc(state: CombatSession, payload: dict):
        new_npc = Combatant(**payload)
        state.combatants[new_npc.id] = new_npc
        state.turn_order.append(new_npc.id)
        state.turn_order.sort(
            key=lambda x: state.combatants[x].initiative, reverse=True
        )
        state.logs.append(f"(GM) Adicionou: {new_npc.name}")

    @staticmethod
    @static_method_log
    async def gm_connect(state: CombatSession):
        if not state.logs or "Mestre (GM) conectou" not in state.logs[-1]:
            state.logs.append("O Mestre (GM) conectou na sala.")

    @staticmethod
    @static_method_log
    async def update_combatant(state: CombatSession, payload: dict):
        target_id = payload["combatantId"]
        value = payload["value"]
        field = payload["field"]

        combatant = state.combatants.get(target_id)

        if combatant:
            from app.managers.connection import manager

            # 1. Calcula a diferença e gera o texto do log conforme o campo alterado
            if field == "hp_current":
                diff = combatant.hp.current - value
                action_text = "curou" if diff < 0 else "deu dano em"
                log_msg = f"Mestre {action_text} {combatant.name}: {abs(diff)} PV."
                combatant.hp.current = value
            else:  # field == "current_focus"
                diff = combatant.focus.current - value
                action_text = "recuperou" if diff < 0 else "gastou"
                log_msg = f"Mestre {action_text} foco de {combatant.name}: {abs(diff)}."
                combatant.focus.current = value

            # 2. IMPORTANTE: Adiciona o log ao estado da sessão para ser persistido
            state.logs.append(log_msg)

            # 3. Determina as chaves do evento para o broadcast
            event = "HP_UPDATE" if field == "hp_current" else "FOCUS_UPDATE"
            payload_key = "newHp" if field == "hp_current" else "newFocus"

            # 4. Envia o evento com o log recém-criado
            await manager.broadcast_event(
                state.session_id,
                event,
                {
                    "combatantId": target_id,
                    payload_key: value,
                    "log": state.logs[-1],  # Agora state.logs[-1] é o log_msg acima
                },
            )

    @staticmethod
    @static_method_log
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
    @static_method_log
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
        state.logs.append("🏆 O Mestre encerrou o combate! (Modo Narrativo)")
