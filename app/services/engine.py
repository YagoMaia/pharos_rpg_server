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
            if not state:
                # Se for Jogador entrando OU Mestre conectando, criamos a sessão
                if event_type in ["JOIN_SESSION", "GM_CONNECT"]:
                    state = self._create_session(session_id)
                    print(f"Sessão {session_id} criada por {event_type}")
                else:
                    return

        if not payload:
            return

        # --- Roteamento de Eventos ---

        if event_type == "GM_CONNECT":
            await self._handle_gm_connect(state)

        elif event_type == "GM_UPDATE_COMBATANT":
            await self._handle_gm_update_combatant(state, payload)

        elif event_type == "GM_ADD_NPC":
            await self._handle_add_npc(state, payload)

        elif event_type == "GM_REMOVE_COMBATANT":
            await self._handle_remove_combatant(state, payload)

        elif event_type == "JOIN_SESSION":
            await self._handle_join_session(state, payload)

        elif event_type == "MANUAL_UPDATE":
            await self._handle_update_hp(state, payload)

        elif event_type == "CHANGE_STANCE":
            await self._handle_change_stance(state, payload)

        elif event_type == "END_TURN":
            await self._handle_next_turn(state)

        elif event_type == "RESOLVE_ACTION":
            await self._handle_resolve_action(state, payload)

        # Salva e notifica a todos
        if state and state.session_id:
            await self.manager.broadcast_state(session_id, state)

    # --- Implementação das Regras ---

    async def _handle_gm_connect(self, state: CombatSession):
        # Não adiciona na lista de combatentes, apenas loga
        state.logs.append("O Mestre (GM) conectou na sala.")

    async def _handle_join_session(self, state: CombatSession, payload: dict):
        combatant_data = payload.get("combatant")
        new_combatant = Combatant(**combatant_data)

        # 1. VERIFICAÇÃO SEGURA: Checa se o ID já está nas chaves do dicionário
        if new_combatant.id in state.combatants:
            # Recupera a versão que está salva no Redis (O estado da batalha)
            existing = state.combatants[new_combatant.id]

            # --- LÓGICA DE MERGE (ATUALIZAÇÃO SEGURA) ---

            # Atualiza STATUS (Permite que ele volte curado/descansado)
            existing.hp = new_combatant.hp
            existing.currentFocus = new_combatant.currentFocus
            existing.maxFocus = new_combatant.maxFocus

            # Atualiza FICHA (Caso tenha trocado de arma ou upado de nível)
            existing.attributes = new_combatant.attributes
            existing.equipment = new_combatant.equipment
            existing.skills = new_combatant.skills
            existing.armorClass = new_combatant.armorClass
            existing.spells = new_combatant.spells

            # --- O QUE NÃO ATUALIZAMOS (PROTEÇÃO) ---
            # existing.initiative      -> Mantém o lugar na fila
            # existing.turnActions     -> Impede "roubar" ações saindo e voltando
            # existing.activeStanceId  -> Mantém a postura ativa

            state.logs.append(f"{new_combatant.name} retornou (Status Sincronizados).")
            return

        # --- CENÁRIO: NOVO JOGADOR ---
        # Adiciona ao dicionário
        state.combatants[new_combatant.id] = new_combatant

        # Adiciona à ordem de turnos
        if new_combatant.id not in state.turn_order:
            state.turn_order.append(new_combatant.id)
            # Reordena por iniciativa
            state.turn_order.sort(
                key=lambda x: state.combatants[x].initiative, reverse=True
            )

        state.logs.append(f"{new_combatant.name} entrou no combate.")

    async def _handle_add_npc(self, state: CombatSession, payload: dict):
        """
        Adiciona um NPC ao combate.
        Diferente do Player, o NPC entra direto via comando do Mestre.
        """
        # O payload já deve vir com o objeto Combatant pronto (montado no Front)
        combatant_data = payload
        new_npc = Combatant(**combatant_data)

        # Adiciona ao mapa de combatentes
        state.combatants[new_npc.id] = new_npc

        # Adiciona à ordem de iniciativa
        state.turn_order.append(new_npc.id)

        # Reordena (Iniciativa maior -> menor)
        state.turn_order.sort(
            key=lambda x: state.combatants[x].initiative, reverse=True
        )

        state.logs.append(f"(GM) Adicionou inimigo: {new_npc.name}")

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
        data = ResolveActionPayload(**payload)

        attacker = state.combatants.get(data.attackerId)
        if not attacker:
            return

        # --- 0. VALIDAÇÃO DE TURNO (A MUDANÇA ESTÁ AQUI) ---

        # Quem é o dono do turno atual?
        current_turn_id = state.turn_order[state.turn_index]

        # Se NÃO é o turno do atacante E NÃO é uma reação, bloqueia.
        if attacker.id != current_turn_id and data.costType != "reaction":
            # (Opcional) Enviar erro para o usuário via socket individual
            print(f"Bloqueado: {attacker.name} tentou agir fora do turno.")
            return

        # --- 1. VERIFICAÇÃO DE ECONOMIA DE AÇÕES (Action Economy) ---
        # Se for um ataque básico ou skill, consome a ação padrão/bônus
        if data.costType == "standard":
            if not attacker.turnActions.standard:
                # Opcional: Logar tentativa falha
                return
            attacker.turnActions.standard = False  # GASTOU A AÇÃO

        elif data.costType == "bonus":
            if not attacker.turnActions.bonus:
                return
            attacker.turnActions.bonus = False

        elif data.costType == "reaction":
            if not attacker.turnActions.reaction:
                return
            attacker.turnActions.reaction = False

        # --- 2. VERIFICAÇÃO DE RECURSOS (FOCO) ---
        # Se for Ataque Básico, focusCost será 0, então passa direto
        if data.focusCost > 0:
            if attacker.currentFocus < data.focusCost:
                state.logs.append(
                    f"{attacker.name} tentou usar {data.actionName} sem Foco suficiente."
                )
                # Devolve a ação que foi gasta no passo 1 (Rollback simples)
                if data.costType == "standard":
                    attacker.turnActions.standard = True
                return

            attacker.currentFocus -= data.focusCost

        # --- 3. APLICAÇÃO DE EFEITOS (DANO/CURA) ---
        target = state.combatants.get(data.targetId) if data.targetId else None
        log_detail = ""

        if target:
            # Lógica de Dano
            if data.damageAmount > 0:
                old_hp = target.hp.current
                target.hp.current = max(0, target.hp.current - data.damageAmount)
                damage_done = old_hp - target.hp.current
                log_detail = f"causando {damage_done} de dano em {target.name}"

                if target.hp.current == 0:
                    log_detail += " (CAIU!)"

            # Lógica de Cura
            elif data.healingAmount > 0:
                old_hp = target.hp.current
                target.hp.current = min(
                    target.hp.max, target.hp.current + data.healingAmount
                )
                healed = target.hp.current - old_hp
                log_detail = f"curando {healed} PV de {target.name}"

            else:
                log_detail = f"em {target.name} (sem dano)"
        else:
            log_detail = "(efeito sem alvo)"

        # --- 4. LOG FINAL ---
        state.logs.append(f"{attacker.name} usou {data.actionName} {log_detail}.")

    async def _handle_gm_update_combatant(self, state: CombatSession, payload: dict):
        target_id = payload["combatantId"]
        value = payload["value"]
        field = payload["field"]

        combatant = state.combatants.get(target_id)

        if combatant:
            if field == "hp_current":
                diff = combatant.hp.current - value
                combatant.hp.current = value

                # Log simples
                action_text = "curou" if diff < 0 else "deu dano"
                state.logs.append(
                    f"Mestre {action_text} {combatant.name} - {abs(diff)} PV."
                )

                if combatant.hp.current == 0:
                    state.logs.append(f"{combatant.name} caiu!")
            elif field == "current_focus":
                diff = combatant.currentFocus - value
                combatant.currentFocus = value

                # Log simples
                action_text = "recuperou" if diff < 0 else "gastou"
                state.logs.append(
                    f"Mestre {action_text} {combatant.name} - {abs(diff)} Foco."
                )

                if combatant.currentFocus == 0:
                    state.logs.append(f"{combatant.name} sem foco!")

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

            if s_id is None:
                state.logs.append(
                    f"{combatant.name} assumiu a postura Neutra (CA {new_ac})."
                )
                combatant.activeStanceId = None
                combatant.armorClass = new_ac
                return

            stance_name = next(
                (s.name for s in combatant.stances if s.id == s_id), "Desconhecida"
            )

            combatant.activeStanceId = s_id
            combatant.armorClass = new_ac
            combatant.turnActions.bonus = False
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
            if next_actor.hp.current <= 0 and next_actor.type == "npc":
                state.logs.append(f"{next_actor.name} Morto")
                await self._handle_remove_combatant(
                    state, {"combatantId": next_actor.id}
                )
                return
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
            elif cost_type == "Reação" and combatant.turnActions.reaction:
                combatant.turnActions.reaction = False
                can_act = True

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
