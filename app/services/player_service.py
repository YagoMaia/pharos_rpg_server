import random
import time

from app.core.utils import static_method_log
from app.models.domain import Combatant, CombatSession
from app.models.events import ResolveActionPayload
from app.services.gm_service import GMService


class PlayerService:
    @staticmethod
    @static_method_log
    async def join_session(state: CombatSession, payload: dict):
        combatant_data = payload.get("combatant")

        if not isinstance(combatant_data, dict):
            raise ValueError("combatant deve ser um dicionário")

        new_combatant = Combatant(**combatant_data)

        if new_combatant.id in state.combatants:
            existing = state.combatants[new_combatant.id]

            existing.hp = new_combatant.hp
            existing.focus.current = new_combatant.focus.current
            existing.focus.max = new_combatant.focus.max

            existing.attributes = new_combatant.attributes
            existing.equipment = new_combatant.equipment
            existing.skills = new_combatant.skills
            existing.armorClass = new_combatant.armorClass
            existing.spells = new_combatant.spells

            state.logs.append(f"{new_combatant.name} retornou (Status Sincronizados).")
            return

        state.combatants[new_combatant.id] = new_combatant

        if new_combatant.id not in state.turn_order:
            state.turn_order.append(new_combatant.id)
            state.turn_order.sort(
                key=lambda x: state.combatants[x].initiative, reverse=True
            )

        state.logs.append(f"{new_combatant.name} entrou no combate.")

    @staticmethod
    @static_method_log
    async def update_hp(state: CombatSession, payload: dict):
        target_id = payload["combatantId"]
        new_hp = payload["newHp"]

        combatant = state.combatants.get(target_id)
        if combatant:
            old_hp = combatant.hp.current

            # --- REGRA DE OURO: CURA RESETTA MORTE ---
            # Se estava com 0 e recebeu cura (new_hp > 0), reseta os testes
            if old_hp <= 0 and new_hp > 0:
                combatant.deathSaves.successes = 0
                combatant.deathSaves.failures = 0
                state.logs.append(f"{combatant.name} recuperou a consciência!")

            combatant.hp.current = new_hp

            diff = old_hp - new_hp
            action_text = "curado em" if diff < 0 else "recebeu dano de"
            state.logs.append(f"{combatant.name} {action_text} {abs(diff)} PV.")

            if combatant.hp.current == 0:
                state.logs.append(f"{combatant.name} caiu inconsciente!")

    @staticmethod
    @static_method_log
    async def change_stance(state: CombatSession, payload: dict):
        c_id = payload["combatantId"]
        s_id = payload["stanceId"]
        new_ac = payload["newAC"]

        combatant = state.combatants.get(c_id)
        if combatant:
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
    @static_method_log
    async def next_turn(state: CombatSession):
        # 1. Finaliza o turno do atual (sem alterações)
        # current_id = state.turn_order[state.turn_index]
        # (Opcional: lógica de fim de turno, ex: remover buffs temporários)

        # 2. Avança o índice
        state.turn_index = (state.turn_index + 1) % len(state.turn_order)

        # 3. Identifica o próximo
        next_id = state.turn_order[state.turn_index]
        next_actor = state.combatants.get(next_id)

        if not next_actor:
            return

        # --- LÓGICA DE MORTE E INCONSCIÊNCIA ---

        if next_actor.hp.current <= 0:
            # CASO A: NPC com 0 PV -> Morre e sai do jogo
            if next_actor.type == "npc":
                state.logs.append(f"💀 {next_actor.name} foi removido.")
                # Remove o NPC
                await GMService.remove_combatant(state, {"combatantId": next_actor.id})
                # IMPORTANTE: Chama next_turn de novo para achar o próximo válido (Recursão)
                await PlayerService.next_turn(state)
                return

            # CASO B: Jogador MORTO (3 Falhas) -> Pula o turno
            if next_actor.deathSaves.failures >= 3:
                state.logs.append(f"⚰️ {next_actor.name} está morto e pulou o turno.")
                await PlayerService.next_turn(state)
                return

            # CASO C: Jogador ESTÁVEL (3 Sucessos) -> Pula o turno (Dorme)
            if next_actor.deathSaves.successes >= 3:
                state.logs.append(
                    f"💤 {next_actor.name} está estabilizado (inconsciente)."
                )
                await PlayerService.next_turn(state)
                return

            # (Já que o botão consome a Ação Padrão)
            next_actor.turnActions.standard = True
            next_actor.turnActions.bonus = False
            next_actor.turnActions.reaction = False

            state.logs.append(f"🚑 --- Turno de {next_actor.name} (TESTE DE MORTE) ---")
            return

        # --- CASO E: VIDA NORMAL ---
        # Reseta economia de ações completa
        next_actor.turnActions.standard = True
        next_actor.turnActions.bonus = True
        next_actor.turnActions.reaction = True

        state.logs.append(f"⚔️ --- Turno de {next_actor.name} ---")

    @staticmethod
    @static_method_log
    async def resolve_action(state: CombatSession, payload: dict):
        data = ResolveActionPayload(**payload)
        damage_done = 0
        healed = 0
        event_type = "info"  # default

        attacker = state.combatants.get(data.attackerId)
        if not attacker:
            return

        current_turn_id = state.turn_order[state.turn_index]

        if attacker.id != current_turn_id and data.costType != "reaction":
            return

        if data.costType == "standard":
            if not attacker.turnActions.standard:
                return
            attacker.turnActions.standard = False

        elif data.costType == "bonus":
            if not attacker.turnActions.bonus:
                return
            attacker.turnActions.bonus = False

        elif data.costType == "reaction":
            if not attacker.turnActions.reaction:
                return
            attacker.turnActions.reaction = False

        if data.focusCost > 0:
            if attacker.focus.current < data.focusCost:
                state.logs.append(
                    f"{attacker.name} tentou usar {data.actionName} sem Foco suficiente."
                )
                if data.costType == "standard":
                    attacker.turnActions.standard = True
                return

            attacker.focus.current -= data.focusCost

        target = state.combatants.get(data.targetId) if data.targetId else None
        log_detail = ""

        if target:
            if data.damageAmount > 0:
                event_type = "damage"
                old_hp = target.hp.current
                target.hp.current = max(0, target.hp.current - data.damageAmount)
                damage_done = old_hp - target.hp.current
                log_detail = f"causando {damage_done} de dano em {target.name}"

                if target.hp.current == 0:
                    log_detail += " (CAIU!)"

            elif data.healingAmount > 0:
                event_type = "heal"
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

        state.logs.append(f"{attacker.name} usou {data.actionName} {log_detail}.")
        state.last_event = {
            "id": int(time.time() * 1000),
            "type": event_type,
            "target_id": target.id if target else None,
            "attacker_name": attacker.name,
            "skill_name": data.actionName,
            "value": damage_done if event_type == "damage" else healed,
        }

    @staticmethod
    @static_method_log
    async def roll_death_save(state: CombatSession, payload: dict):
        c_id = payload["combatantId"]
        combatant = state.combatants.get(c_id)

        if not combatant:
            return

        d20 = payload.get("rollValue", random.randint(1, 20))

        log_msg = f"Teste de Morte: Rolou {d20} -> "

        # 2. Regra do 20 Natural (Crítico Sucesso)
        if d20 == 20:
            combatant.hp.current = 1
            combatant.deathSaves.successes = 0
            combatant.deathSaves.failures = 0
            combatant.turnActions.standard = False  # Consome ação
            state.logs.append(
                f"{combatant.name} {log_msg} 20 NATURAL! Renasceu com 1 PV!"
            )
            return

        # 3. Regra do 1 Natural (Crítica Falha)
        if d20 == 1:
            combatant.deathSaves.failures += 2
            log_msg += "Falha Crítica (2 falhas)!"

        # 4. Sucesso (>= 10)
        elif d20 >= 10:
            combatant.deathSaves.successes += 1
            log_msg += "Sucesso."

        # 5. Falha (< 10)
        else:
            combatant.deathSaves.failures += 1
            log_msg += "Falha."

        # 6. Consome a ação do turno
        combatant.turnActions.standard = False

        # --- VERIFICAÇÃO DE FIM DE LINHA ---

        # Se estabilizou (3 Sucessos)
        if combatant.deathSaves.successes >= 3:
            combatant.deathSaves.successes = 0
            combatant.deathSaves.failures = 0
            combatant.hp.current = 1
            log_msg += " ESTABILIZOU! (Inconsciente, mas salvo)."

        # Se Morreu (3 Falhas)
        elif combatant.deathSaves.failures >= 3:
            log_msg += " MORREU."
            # Aqui você pode mudar o ícone para caveira ou remover do combate
            await GMService.remove_combatant(state, {"combatantId": c_id})

        state.logs.append(
            f"{combatant.name}: {log_msg} [S:{combatant.deathSaves.successes}/F:{combatant.deathSaves.failures}]"
        )
