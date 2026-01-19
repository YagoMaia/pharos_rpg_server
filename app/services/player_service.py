from app.models.domain import Combatant, CombatSession
from app.models.events import ResolveActionPayload
from app.services.gm_service import GMService


class PlayerService:
    @staticmethod
    async def join_session(state: CombatSession, payload: dict):
        combatant_data = payload.get("combatant")

        if not isinstance(combatant_data, dict):
            raise ValueError("combatant deve ser um dicionário")

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

    @staticmethod
    async def update_hp(state: CombatSession, payload: dict):
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

    @staticmethod
    async def change_stance(state: CombatSession, payload: dict):
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

    @staticmethod
    async def next_turn(state: CombatSession):
        # 1. Finaliza turno do atual
        current_id = state.turn_order[state.turn_index]
        current_actor = state.combatants.get(current_id)

        if not current_actor:
            return

        # Opcional: Resetar reações do personagem que acabou de agir?
        # current_actor.turnActions.bonus = True
        # current_actor.turnActions.standard = True
        # current_actor.turnActions.reaction = True
        # Depende da regra do seu sistema (Geralmente reseta no INÍCIO do turno dele)

        # 2. Calcula próximo índice
        state.turn_index = (state.turn_index + 1) % len(state.turn_order)

        # 3. Prepara o próximo
        next_id = state.turn_order[state.turn_index]
        next_actor = state.combatants.get(next_id)

        if next_actor:
            if next_actor.hp.current <= 0 and next_actor.type == "npc":
                state.logs.append(f"{next_actor.name} Morto")
                await GMService.remove_combatant(state, {"combatantId": next_actor.id})
                return
            # REGRA DE OURO: Resetar economia de ações no INÍCIO do turno
            next_actor.turnActions.standard = True
            next_actor.turnActions.bonus = True
            next_actor.turnActions.reaction = True
            # (Reação às vezes reseta no início da RODADA, ajuste conforme seu sistema)

            state.logs.append(f"--- Turno de {next_actor.name} ---")

    @staticmethod
    async def resolve_action(state: CombatSession, payload: dict):
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
