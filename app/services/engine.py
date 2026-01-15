from app.models.domain import Combatant, CombatSession
from app.models.events import RemoveCombatantPayload, ResolveActionPayload


class GameLogic:
    def __init__(self, manager):
        self.manager = manager  # Instância do ConnectionManager criada anteriormente

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

        # Se não existe estado e o evento não é JOIN ou CREATE, erro
        if not state:
            if event_type == "JOIN_SESSION":
                # A sessão não existe, mas é um pedido de entrada? CRIAMOS AGORA.
                state = self._create_session(session_id)
            else:
                # Sessão não existe e tentaram atacar/mover? Ignora ou erro.
                # (Opcional: enviar msg de erro via socket informando que a sessão expirou)
                return

        if not payload:
            return

        # --- Roteamento de Eventos ---

        if event_type == "JOIN_SESSION":
            await self._handle_join_session(state, payload)

        if event_type == "MANUAL_UPDATE":
            await self._handle_update_hp(state, payload)

        elif event_type == "CHANGE_STANCE":
            await self._handle_change_stance(state, payload)

        elif event_type == "NEXT_TURN":
            await self._handle_next_turn(state)

        elif event_type == "RESOLVE_ACTION":
            await self._handle_use_skill(state, payload)

        elif event_type == "REMOVE_COMBATANT":
            await self._handle_remove_combatant(state, payload)

        # Salva e notifica a todos
        if state:
            await self.manager.broadcast_state(session_id, state)

    # --- Implementação das Regras ---

    async def _handle_join_session(self, state: CombatSession, payload: dict):
        # Payload esperado: { combatant: { ...dados... } }
        combatant_data = payload.get("combatant")

        # Validação e Conversão Pydantic
        # Isso garante que o dict vire um objeto Combatant com métodos e tipos certos
        new_combatant = Combatant(**combatant_data)

        # Lógica de Idempotência: Se já existe, atualiza ou ignora?
        # Aqui vou assumir que substitui (reconexão)
        state.combatants[new_combatant.id] = new_combatant

        # Adiciona à ordem de turnos se não estiver lá
        if new_combatant.id not in state.turn_order:
            state.turn_order.append(new_combatant.id)
            # Reordena por iniciativa (maior para menor)
            state.turn_order.sort(
                key=lambda x: state.combatants[x].initiative, reverse=True
            )

        state.logs.append(f"{new_combatant.name} entrou no combate.")

    async def _handle_remove_combatant(self, state: CombatSession, payload: dict):
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

        # 4. Ajuste Matemático do Turno (A parte crítica!)
        # Se removemos alguém que estava ANTES do jogador atual na fila,
        # o índice do jogador atual diminui em 1 (a fila andou para trás).
        if removed_index != -1 and removed_index < state.turn_index:
            state.turn_index = max(0, state.turn_index - 1)

        # Se removemos O jogador atual, o índice agora aponta para o próximo automaticamente,
        # mas precisamos garantir que não estourou o tamanho da lista.
        if len(state.turn_order) > 0:
            state.turn_index = state.turn_index % len(state.turn_order)
        else:
            state.turn_index = 0

        state.logs.append(f"{name} foi removido do combate.")

    async def _handle_resolve_action(self, state: CombatSession, payload: dict):
        """
        Centraliza a lógica de executar uma ação:
        1. Verifica economia de ações (tem Ação Padrão?)
        2. Verifica recursos (tem Foco?)
        3. Consome os custos
        4. Aplica o efeito (Dano/Cura)
        """
        data = ResolveActionPayload(**payload)

        attacker = state.combatants.get(data.attackerId)
        if not attacker:
            return  # Ou logar erro

        # --- PASSO 1: Validação e Consumo de Ações (Action Economy) ---

        # Só validamos se não for custo livre/none
        if data.costType == "standard":
            if not attacker.turnActions.standard:
                # Opcional: Enviar erro específico para o usuário
                return
            attacker.turnActions.standard = False  # Gasta a ação

        elif data.costType == "bonus":
            if not attacker.turnActions.bonus:
                return
            attacker.turnActions.bonus = False

        elif data.costType == "reaction":
            if not attacker.turnActions.reaction:
                return
            attacker.turnActions.reaction = False

        # --- PASSO 2: Validação e Consumo de Foco ---

        if data.focusCost > 0:
            if attacker.currentFocus < data.focusCost:
                # Sem foco suficiente, cancela ação (e devolve o slot de ação gasto acima?)
                # Para simplificar, assumimos que o Front bloqueia, mas aqui devolvemos a ação:
                if data.costType == "standard":
                    attacker.turnActions.standard = True
                if data.costType == "bonus":
                    attacker.turnActions.bonus = True
                state.logs.append(
                    f"{attacker.name} tentou usar {data.actionName} mas não tem foco suficiente."
                )
                return

            attacker.currentFocus -= data.focusCost

        # --- PASSO 3: Aplicação de Efeitos (Dano/Cura) ---

        target = state.combatants.get(data.targetId) if data.targetId else None
        effect_msg = ""

        if target:
            # Aplica Dano
            if data.damageAmount > 0:
                old_hp = target.hp.current
                target.hp.current = max(0, target.hp.current - data.damageAmount)
                real_dmg = old_hp - target.hp.current
                effect_msg = f"causando {real_dmg} de dano em {target.name}"

                if target.hp.current == 0:
                    effect_msg += " (CAIU!)"

            # Aplica Cura
            elif data.healingAmount > 0:
                old_hp = target.hp.current
                target.hp.current = min(
                    target.hp.max, target.hp.current + data.healingAmount
                )
                real_heal = target.hp.current - old_hp
                effect_msg = f"curando {real_heal} PV de {target.name}"
        else:
            # Se não tem alvo (ex: Buff em si mesmo ou Errou ataque), mensagem genérica
            effect_msg = ""

        # --- PASSO 4: Log Final ---
        cost_txt = f"[{data.costType}]" if data.costType != "none" else ""
        state.logs.append(
            f"{attacker.name} usou {data.actionName} {cost_txt} {effect_msg}."
        )

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
