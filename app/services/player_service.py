import random

from app.core.utils import static_method_log
from app.models.domain import Combatant, CombatSession
from app.models.events import ResolveActionPayload
from app.services.gm_service import GMService


class PlayerService:
    @staticmethod
    @static_method_log
    async def join_session(state: CombatSession, payload: dict):
        """Adiciona ou sincroniza um jogador na sessão. Se tiver pet, adiciona também."""
        combatant_data = payload.get("combatant")
        if not isinstance(combatant_data, dict):
            raise ValueError("combatant deve ser um dicionário")

        new_combatant = Combatant(**combatant_data)

        if new_combatant.id in state.combatants:
            existing = state.combatants[new_combatant.id]
            # RECONEXÃO: Apenas atualiza dados "estruturais" da ficha.
            # Preserva HP, Focus, DeathSaves, TurnActions, ActiveStance, ArmorClass
            # que são o estado VIVO do combate no servidor.
            existing.attributes = new_combatant.attributes
            existing.skills = new_combatant.skills
            existing.spells = new_combatant.spells
            existing.weapons = new_combatant.weapons
            existing.image = new_combatant.image
            state.logs.append(f"{new_combatant.name} reconectou.")
        else:
            state.combatants[new_combatant.id] = new_combatant
            if new_combatant.id not in state.turn_order:
                state.turn_order.append(new_combatant.id)
            state.logs.append(f"{new_combatant.name} entrou no combate.")

        # Processa o Pet (se enviado junto)
        pet_data = payload.get("pet")
        if isinstance(pet_data, dict):
            pet_combatant = Combatant(**pet_data)

            if pet_combatant.id in state.combatants:
                existing_pet = state.combatants[pet_combatant.id]
                # RECONEXÃO PET: Preserva estado de combate, atualiza estruturais
                existing_pet.attributes = pet_combatant.attributes
                existing_pet.skills = pet_combatant.skills
                existing_pet.spells = pet_combatant.spells
                existing_pet.weapons = pet_combatant.weapons
                existing_pet.image = pet_combatant.image
                state.logs.append(f"🐾 {pet_combatant.name} reconectou.")
            else:
                state.combatants[pet_combatant.id] = pet_combatant
                if pet_combatant.id not in state.turn_order:
                    state.turn_order.append(pet_combatant.id)
                state.logs.append(f"🐾 {pet_combatant.name} entrou no combate como companheiro.")

        # Reordena por iniciativa
        state.turn_order.sort(
            key=lambda x: state.combatants[x].initiative if x in state.combatants else 0,
            reverse=True,
        )

    @staticmethod
    @static_method_log
    async def update_hp(state: CombatSession, payload: dict):
        """Atualiza HP e dispara evento Delta."""
        target_id = payload["combatantId"]
        new_hp = payload["newHp"]
        combatant = state.combatants.get(target_id)

        if combatant:
            old_hp = combatant.hp.current
            if old_hp <= 0 and new_hp > 0:
                combatant.deathSaves.successes = 0
                combatant.deathSaves.failures = 0
                state.logs.append(f"{combatant.name} recuperou a consciência!")

            combatant.hp.current = new_hp
            diff = old_hp - new_hp
            action_text = "curado em" if diff < 0 else "recebeu dano de"
            log_msg = f"{combatant.name} {action_text} {abs(diff)} PV."
            state.logs.append(log_msg)

            if combatant.hp.current == 0:
                state.logs.append(f"{combatant.name} caiu inconsciente!")

            # DISPARO DELTA: Notifica apenas a mudança de vida
            from app.managers.connection import manager

            await manager.broadcast_event(
                state.session_id,
                "HP_UPDATE",
                {"combatantId": target_id, "newHp": new_hp, "log": log_msg},
            )

    @staticmethod
    @static_method_log
    async def change_stance(state: CombatSession, payload: dict):
        """Muda postura e dispara evento Delta."""
        c_id = payload["combatantId"]
        s_id = payload["stanceId"]
        new_ac = payload["newAC"]
        combatant = state.combatants.get(c_id)

        if combatant:
            if s_id is None:
                log_msg = f"{combatant.name} assumiu a postura Neutra (CA {new_ac})."
                combatant.activeStanceId = None
            else:
                stance_name = next(
                    (s.name for s in combatant.stances if s.id == s_id), "Desconhecida"
                )
                combatant.activeStanceId = s_id
                combatant.turnActions.bonus = False  # Consome ação bônus
                log_msg = (
                    f"{combatant.name} assumiu a postura {stance_name} (CA {new_ac})."
                )

            combatant.armorClass = new_ac
            state.logs.append(log_msg)

            from app.managers.connection import manager

            await manager.broadcast_event(
                state.session_id,
                "STANCE_UPDATE",
                {
                    "combatantId": c_id,
                    "activeStanceId": s_id,
                    "newAC": new_ac,
                    "log": state.logs[-1],
                    "turnActions": combatant.turnActions.model_dump(),
                },
            )

    @staticmethod
    @static_method_log
    async def next_turn(state: CombatSession):
        """Avança turno e dispara evento Delta."""
        if not state.turn_order:
            return
        state.turn_index = (state.turn_index + 1) % len(state.turn_order)
        next_id = state.turn_order[state.turn_index]
        next_actor = state.combatants.get(next_id)
        if not next_actor:
            return

        from app.managers.connection import manager

        # Lógica de Inconsciência
        if next_actor.hp.current <= 0:
            if next_actor.type == "npc":
                state.logs.append(f"💀 {next_actor.name} removido.")
                await GMService.remove_combatant(state, {"combatantId": next_actor.id})
                await PlayerService.next_turn(state)
                return

            # Pets mortos pulam o turno automaticamente
            if next_actor.type == "pet":
                state.logs.append(f"🐾 {next_actor.name} está inconsciente e pulou o turno.")
                await PlayerService.next_turn(state)
                return

            if (
                next_actor.deathSaves.failures >= 3
                or next_actor.deathSaves.successes >= 3
            ):
                status = (
                    "morto" if next_actor.deathSaves.failures >= 3 else "estabilizado"
                )
                state.logs.append(f"⚰️ {next_actor.name} está {status} e pulou o turno.")
                await PlayerService.next_turn(state)
                return

            next_actor.turnActions.standard, next_actor.turnActions.bonus = True, False
            log_msg = f"🚑 --- Turno de {next_actor.name} (TESTE DE MORTE) ---"
        else:
            next_actor.turnActions.standard = True
            next_actor.turnActions.bonus = True
            next_actor.turnActions.reaction = True

            log_msg = f"⚔️ --- Turno de {next_actor.name} ---"

        state.logs.append(log_msg)
        await manager.broadcast_event(
            state.session_id,
            "TURN_UPDATE",
            {
                "newActiveId": next_id,
                "turnIndex": state.turn_index,
                "log": log_msg,
                "turnActions": next_actor.turnActions.model_dump(),
            },
        )

    @staticmethod
    @static_method_log
    async def resolve_action(state: CombatSession, payload: dict):
        """Processa ataques/curas e dispara evento Delta."""
        data = ResolveActionPayload(**payload)
        attacker = state.combatants.get(data.attackerId)
        if not attacker:
            return

        # Validação de Economia de Ações
        cost_attr = (
            "standard"
            if data.costType == "standard"
            else "bonus"
            if data.costType == "bonus"
            else "reaction"
        )
        if not getattr(attacker.turnActions, cost_attr) and data.costType != "reaction":
            return

        if data.focusCost > attacker.focus.current:
            state.logs.append(f"{attacker.name} sem foco para {data.actionName}.")
            return

        # Execução
        setattr(attacker.turnActions, cost_attr, False)
        attacker.focus.current -= data.focusCost
        target = state.combatants.get(data.targetId) if data.targetId else None

        damage_done, healed, event_type = 0, 0, "info"
        log_detail = "(efeito sem alvo)"

        if target:
            if data.damageAmount > 0:
                event_type, old_hp = "damage", target.hp.current
                target.hp.current = max(0, target.hp.current - data.damageAmount)
                damage_done = old_hp - target.hp.current
                log_detail = f"causando {damage_done} de dano em {target.name}"
            elif data.healingAmount > 0:
                event_type, old_hp = "heal", target.hp.current
                target.hp.current = min(
                    target.hp.max, target.hp.current + data.healingAmount
                )
                healed = target.hp.current - old_hp
                log_detail = f"curando {healed} PV de {target.name}"

        final_log = f"{attacker.name} usou {data.actionName} {log_detail}."
        state.logs.append(final_log)

        from app.managers.connection import manager

        await manager.broadcast_event(
            state.session_id,
            "ACTION_RESOLVED",
            {
                "type": event_type,
                "attackerId": attacker.id,
                "targetId": target.id if target else None,
                "targetHp": target.hp.current if target else None,
                "actionName": data.actionName,
                "value": damage_done if event_type == "damage" else healed,
                "focusCost": data.focusCost,
                "log": final_log,
                "attackerActions": attacker.turnActions.model_dump(),
                "attackerFocus": attacker.focus.current,
            },
        )

    @staticmethod
    @static_method_log
    async def roll_death_save(state: CombatSession, payload: dict):
        """Processa teste de morte e dispara evento Delta."""
        c_id = payload["combatantId"]
        combatant = state.combatants.get(c_id)
        if not combatant:
            return

        d20 = payload.get("rollValue", random.randint(1, 20))

        if d20 == 20:
            combatant.hp.current = 1
            combatant.deathSaves.successes = 0
            combatant.deathSaves.failures = 0
            log_msg = f"{combatant.name} rolou 20 NATURAL! Renasceu com 1 PV!"
        elif d20 == 1:
            combatant.deathSaves.failures += 2
            log_msg = f"{combatant.name} rolou 1 CRÍTICO! 2 falhas no teste de morte."
        elif d20 >= 10:
            combatant.deathSaves.successes += 1
            log_msg = f"{combatant.name} rolou {d20} — Sucesso no teste de morte."
        else:
            combatant.deathSaves.failures += 1
            log_msg = f"{combatant.name} rolou {d20} — Falha no teste de morte."

        combatant.turnActions.standard = False
        state.logs.append(log_msg)

        from app.managers.connection import manager

        await manager.broadcast_event(
            state.session_id,
            "DEATH_SAVE_UPDATE",
            {
                "combatantId": c_id,
                "successes": combatant.deathSaves.successes,
                "failures": combatant.deathSaves.failures,
                "newHp": combatant.hp.current,
                "log": log_msg,
                "turnActions": combatant.turnActions.model_dump(),
            },
        )
