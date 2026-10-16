import random
import uuid

from app.core.utils import static_method_log
from app.models.domain import ActiveCondition, Combatant, CombatSession
from app.models.events import (
    ApplyConditionPayload,
    RemoveConditionPayload,
    RequestSavingThrowPayload,
    ResolveActionPayload,
    SubmitSavingThrowPayload,
)
from app.services.combat_utils import (
    calculate_area_damage,
    get_target_save_modifier,
    resolve_saving_throw,
)
from app.services.gm_service import GMService


class PlayerService:
    @staticmethod
    def _sync_sheet(existing: Combatant, incoming: Combatant):
        """Sincroniza a ficha sem restaurar recursos, ações ou testes de morte."""

        def stance_bonus(actor):
            stance = next(
                (s for s in actor.stances if s.id == actor.activeStanceId), None
            )
            if not stance:
                return 0
            attribute = actor.attributes.get(stance.acAttribute)
            return (stance.acBonus or 0) + (attribute.modifier if attribute else 0)

        old_bonus = stance_bonus(existing)
        old_base = existing.baseArmorClass
        if old_base is None:
            old_base = existing.armorClass - old_bonus
        # Preserva ajustes temporários de combate separados da ficha e da postura.
        temporary_ac = existing.armorClass - old_base - old_bonus
        new_base = incoming.baseArmorClass
        if new_base is None:
            new_base = incoming.armorClass - stance_bonus(incoming)

        for field in (
            "attributes",
            "stances",
            "skills",
            "spells",
            "weapons",
            "image",
            "feats",
            "factionFeat",
            "specialFeat",
        ):
            setattr(existing, field, getattr(incoming, field))
        if not any(s.id == existing.activeStanceId for s in existing.stances):
            existing.activeStanceId = None
        existing.baseArmorClass = new_base
        existing.armorClass = new_base + stance_bonus(existing) + temporary_ac
        existing.hp.max = max(1, incoming.hp.max)
        existing.hp.current = min(existing.hp.current, existing.hp.max)
        existing.focus.max = max(0, incoming.focus.max)
        existing.focus.current = min(existing.focus.current, existing.focus.max)

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
            PlayerService._sync_sheet(existing, new_combatant)
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
                PlayerService._sync_sheet(existing_pet, pet_combatant)
                state.logs.append(f"🐾 {pet_combatant.name} reconectou.")
            else:
                state.combatants[pet_combatant.id] = pet_combatant
                if pet_combatant.id not in state.turn_order:
                    state.turn_order.append(pet_combatant.id)
                state.logs.append(
                    f"🐾 {pet_combatant.name} entrou no combate como companheiro."
                )

        # Reordena por iniciativa
        state.turn_order.sort(
            key=lambda x: state.combatants[x].initiative
            if x in state.combatants
            else 0,
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
        combatant = state.combatants.get(c_id)

        if combatant:
            old_stance = next(
                (s for s in combatant.stances if s.id == combatant.activeStanceId), None
            )
            new_stance = next((s for s in combatant.stances if s.id == s_id), None)
            if s_id is not None and (
                new_stance is None or not combatant.turnActions.bonus
            ):
                return
            if any(
                c.name in {"Incapacitado", "Paralisado", "Atordoado"}
                for c in combatant.conditions
            ):
                return
            if s_id == combatant.activeStanceId:
                return

            def armor_bonus(stance):
                if not stance:
                    return 0
                attribute = combatant.attributes.get(stance.acAttribute)
                return (stance.acBonus or 0) + (attribute.modifier if attribute else 0)

            new_ac = (
                combatant.armorClass - armor_bonus(old_stance) + armor_bonus(new_stance)
            )
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

        from app.managers.connection import manager

        max_iterations = len(state.turn_order)
        for _ in range(max_iterations):
            if not state.turn_order:
                continue
            state.turn_index = (state.turn_index + 1) % len(state.turn_order)
            next_id = state.turn_order[state.turn_index]
            next_actor = state.combatants.get(next_id)

            if not next_actor:
                continue

            if next_actor.hp.current <= 0:
                if next_actor.type == "npc":
                    if next_actor.currentPhaseIndex < len(next_actor.phases):
                        state.logs.append(f"{next_actor.name} aguarda a próxima fase.")
                        continue
                    state.logs.append(f"💀 {next_actor.name} removido.")
                    await GMService.remove_combatant(
                        state, {"combatantId": next_actor.id}
                    )
                    # A remoção deslocou o próximo ator para o índice atual.
                    state.turn_index -= 1
                    continue
                if next_actor.type == "pet":
                    state.logs.append(
                        f"🐾 {next_actor.name} está inconsciente e pulou o turno."
                    )
                    continue
                if (
                    next_actor.deathSaves.failures >= 3
                    or next_actor.deathSaves.successes >= 3
                ):
                    status = (
                        "morto"
                        if next_actor.deathSaves.failures >= 3
                        else "estabilizado"
                    )
                    state.logs.append(
                        f"⚰️ {next_actor.name} está {status} e pulou o turno."
                    )
                    continue
            break  # Combatente valido encontrado
        else:
            state.logs.append("💀 Todos os combatentes estão incapacitados.")
            state.turn_index = -1
            await manager.broadcast_event(
                state.session_id,
                "TURN_UPDATE",
                {
                    "newActiveId": None,
                    "turnIndex": -1,
                    "log": state.logs[-1],
                },
            )
            return

        # Lógica de Inconsciência
        if next_actor.hp.current <= 0:
            next_actor.turnActions.standard, next_actor.turnActions.bonus = True, False
            next_actor.turnActions.reaction = False
            log_msg = f"🚑 --- Turno de {next_actor.name} (TESTE DE MORTE) ---"
        else:
            next_actor.turnActions.standard = True
            next_actor.turnActions.bonus = True
            next_actor.turnActions.reaction = True

            log_msg = f"⚔️ --- Turno de {next_actor.name} ---"

        # ═══════════════════════════════════════════════════════
        # CONDIÇÕES: Decremento de duração no início do turno
        # ═══════════════════════════════════════════════════════
        expired_conditions = []
        remaining_conditions = []
        for cond in next_actor.conditions:
            if cond.durationTurns is not None:
                cond.durationTurns -= 1
                if cond.durationTurns <= 0:
                    expired_conditions.append(cond)
                    continue
            remaining_conditions.append(cond)

        if any(
            c.name in {"Incapacitado", "Paralisado", "Atordoado"}
            for c in remaining_conditions
        ):
            next_actor.turnActions.standard = False
            next_actor.turnActions.bonus = False
            next_actor.turnActions.reaction = False

        if expired_conditions:
            next_actor.conditions = remaining_conditions
            expired_names = ", ".join(c.name for c in expired_conditions)
            cond_log = f"✨ {next_actor.name} se livrou de: {expired_names}."
            state.logs.append(cond_log)

            await manager.broadcast_event(
                state.session_id,
                "CONDITION_UPDATE",
                {
                    "combatantId": next_id,
                    "conditions": [c.model_dump() for c in next_actor.conditions],
                    "expired": [c.name for c in expired_conditions],
                    "log": cond_log,
                },
            )

        state.logs.append(log_msg)
        await manager.broadcast_event(
            state.session_id,
            "TURN_UPDATE",
            {
                "newActiveId": next_id,
                "turnIndex": state.turn_index,
                "log": log_msg,
                "turnActions": next_actor.turnActions.model_dump(),
                "conditions": [c.model_dump() for c in next_actor.conditions],
            },
        )

    @staticmethod
    @static_method_log
    async def resolve_action(state: CombatSession, payload: dict):
        """Processa ataques/curas (single ou multi-target) e dispara evento Delta."""
        data = ResolveActionPayload(**payload)
        attacker = state.combatants.get(data.attackerId)
        if not attacker:
            return
        if attacker.hp.current <= 0 or any(
            c.name in {"Incapacitado", "Paralisado", "Atordoado"}
            for c in attacker.conditions
        ):
            return

        # Validação de Economia de Ações
        cost_attr = (
            "standard"
            if data.costType == "standard"
            else "bonus"
            if data.costType == "bonus"
            else "reaction"
        )
        if not getattr(attacker.turnActions, cost_attr):
            return

        if data.focusCost > attacker.focus.current:
            state.logs.append(f"{attacker.name} sem foco para {data.actionName}.")
            return

        # Consome ação e foco
        setattr(attacker.turnActions, cost_attr, False)
        attacker.focus.current -= data.focusCost

        from app.managers.connection import manager

        # ═══════════════════════════════════════════════════════
        # MULTI-TARGET: Processa lista de alvos individuais
        # ═══════════════════════════════════════════════════════
        if data.targets and len(data.targets) > 0:
            results = []
            log_parts = []

            for t in data.targets:
                target = state.combatants.get(t.targetId)
                if not target:
                    continue

                damage_done, healed, event_type = 0, 0, "info"

                if t.damageAmount > 0:
                    event_type = "damage"
                    old_hp = target.hp.current
                    target.hp.current = max(0, target.hp.current - t.damageAmount)
                    damage_done = old_hp - target.hp.current
                    log_parts.append(f"{damage_done} dano em {target.name}")
                elif t.healingAmount > 0:
                    event_type = "heal"
                    old_hp = target.hp.current
                    target.hp.current = min(
                        target.hp.max, target.hp.current + t.healingAmount
                    )
                    healed = target.hp.current - old_hp
                    log_parts.append(f"{healed} PV curados em {target.name}")

                results.append(
                    {
                        "targetId": target.id,
                        "targetHp": target.hp.current,
                        "type": event_type,
                        "value": damage_done if event_type == "damage" else healed,
                    }
                )

            if log_parts:
                final_log = (
                    f"{attacker.name} usou {data.actionName}: {', '.join(log_parts)}."
                )
            else:
                final_log = (
                    f"{attacker.name} usou {data.actionName} (sem alvos válidos)."
                )

            # Aplica condições (multi-target)
            applied_cond_logs = PlayerService._apply_conditions_from_action(
                state, data, attacker
            )
            if applied_cond_logs:
                final_log += " " + " ".join(applied_cond_logs)

            state.logs.append(final_log)

            await manager.broadcast_event(
                state.session_id,
                "ACTION_RESOLVED",
                {
                    "type": "multi",
                    "attackerId": attacker.id,
                    "targetId": None,
                    "targetHp": None,
                    "actionName": data.actionName,
                    "value": 0,
                    "focusCost": data.focusCost,
                    "log": final_log,
                    "attackerActions": attacker.turnActions.model_dump(),
                    "attackerFocus": attacker.focus.current,
                    "results": results,
                },
            )
            return

        # ═══════════════════════════════════════════════════════
        # SINGLE-TARGET: Fluxo original (retrocompatibilidade)
        # ═══════════════════════════════════════════════════════
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

        # Aplica condições (single-target)
        applied_cond_logs = PlayerService._apply_conditions_from_action(
            state, data, attacker
        )

        final_log = f"{attacker.name} usou {data.actionName} {log_detail}."
        if applied_cond_logs:
            final_log += " " + " ".join(applied_cond_logs)

        state.logs.append(final_log)

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

    # ═══════════════════════════════════════════════════════════
    # HELPER: Aplica condições enviadas junto com uma ação
    # ═══════════════════════════════════════════════════════════
    @staticmethod
    def _apply_conditions_from_action(
        state: CombatSession,
        data: ResolveActionPayload,
        attacker: Combatant,
    ) -> list[str]:
        """Aplica condições embutidas em um RESOLVE_ACTION. Retorna logs."""
        if not data.appliedConditions:
            return []

        logs = []
        for cp in data.appliedConditions:
            target = state.combatants.get(cp.targetId)
            if not target:
                continue

            condition = ActiveCondition(
                id=str(uuid.uuid4()),
                name=cp.conditionName,
                appliedAtTurn=state.turn_index,
                durationTurns=cp.durationTurns,
                sourceId=attacker.id,
                description=cp.description,
            )
            target.conditions.append(condition)

            duration_text = (
                f" por {cp.durationTurns} turnos" if cp.durationTurns else ""
            )
            logs.append(f"⚡ {target.name} ficou {cp.conditionName}{duration_text}!")

        return logs

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

    # ═══════════════════════════════════════════════════════════
    # CONDIÇÕES: Aplicação e Remoção Manual (GM)
    # ═══════════════════════════════════════════════════════════

    @staticmethod
    @static_method_log
    async def apply_condition(state: CombatSession, payload: dict):
        """GM aplica manualmente uma condição a um combatente."""
        data = ApplyConditionPayload(**payload)
        combatant = state.combatants.get(data.combatantId)
        if not combatant:
            return

        if any(c.name == data.conditionName for c in combatant.conditions):
            return

        condition = ActiveCondition(
            id=str(uuid.uuid4()),
            name=data.conditionName,
            appliedAtTurn=state.turn_index,
            durationTurns=data.durationTurns,
            sourceId="gm",
            description=data.description,
        )
        combatant.conditions.append(condition)

        duration_text = (
            f" por {data.durationTurns} turnos" if data.durationTurns else ""
        )
        log_msg = f"🎯 {combatant.name} recebeu a condição {data.conditionName}{duration_text}."
        state.logs.append(log_msg)

        from app.managers.connection import manager

        await manager.broadcast_event(
            state.session_id,
            "CONDITION_UPDATE",
            {
                "combatantId": data.combatantId,
                "conditions": [c.model_dump() for c in combatant.conditions],
                "applied": condition.model_dump(),
                "log": log_msg,
            },
        )

    @staticmethod
    @static_method_log
    async def remove_condition(state: CombatSession, payload: dict):
        """GM ou sistema remove uma condição pelo ID."""
        data = RemoveConditionPayload(**payload)
        combatant = state.combatants.get(data.combatantId)
        if not combatant:
            return

        removed = None
        for cond in combatant.conditions:
            if cond.id == data.conditionId:
                removed = cond
                break

        if not removed:
            return

        combatant.conditions = [
            c for c in combatant.conditions if c.id != data.conditionId
        ]

        log_msg = f"✨ {combatant.name} não está mais {removed.name}."
        state.logs.append(log_msg)

        from app.managers.connection import manager

        await manager.broadcast_event(
            state.session_id,
            "CONDITION_UPDATE",
            {
                "combatantId": data.combatantId,
                "conditions": [c.model_dump() for c in combatant.conditions],
                "removed": removed.model_dump(),
                "log": log_msg,
            },
        )

    @staticmethod
    @static_method_log
    async def request_saving_throw(state: CombatSession, payload: dict):
        """Dispara solicitação de salvaguarda para múltiplos alvos (ataques em área)."""
        data = RequestSavingThrowPayload(**payload)
        attacker = state.combatants.get(data.attackerId)
        attacker_name = (
            attacker.name
            if attacker
            else (data.attackerName or data.attackerId or "Mestre")
        )

        if attacker:
            if attacker.hp.current <= 0 or any(
                c.name in {"Incapacitado", "Paralisado", "Atordoado"}
                for c in attacker.conditions
            ):
                return

            if data.costType != "none":
                cost_attr = (
                    "standard"
                    if data.costType == "standard"
                    else "bonus"
                    if data.costType == "bonus"
                    else "reaction"
                )
                if not getattr(attacker.turnActions, cost_attr, True):
                    return
                if data.focusCost > attacker.focus.current:
                    state.logs.append(
                        f"{attacker.name} sem foco para {data.actionName}."
                    )
                    return
        else:
            # Se não estiver no dicionário de combatentes, permite GM / narrador / perigo ambiental
            if not (data.attackerId in ("gm", "GM_ADMIN") or data.attackerName):
                return

        request_id = data.requestId or data.attackId or uuid.uuid4().hex
        valid_targets = [t_id for t_id in data.targetIds if t_id in state.combatants]
        if not valid_targets:
            return

        log_msg = (
            f"⚠️ {attacker_name} exigiu salvaguarda de {data.attribute} (CD {data.saveDC}) "
            f"para {data.actionName} contra {len(valid_targets)} alvo(s)!"
        )
        state.logs.append(log_msg)

        from app.managers.connection import manager

        broadcast_payload = {
            "requestId": request_id,
            "attackId": request_id,
            "attackerId": attacker.id if attacker else data.attackerId,
            "attackerName": attacker_name,
            "actionName": data.actionName,
            "attribute": data.attribute,
            "saveAttribute": data.attribute,
            "saveDC": data.saveDC,
            "baseDamage": data.baseDamage,
            "halfOnSave": data.halfOnSave,
            "targetIds": valid_targets,
            "costType": data.costType,
            "focusCost": data.focusCost,
            "log": log_msg,
        }

        await manager.broadcast_event(
            state.session_id,
            "REQUEST_SAVING_THROW",
            broadcast_payload,
        )
        await manager.broadcast_event(
            state.session_id,
            "SAVING_THROW_REQUESTED",
            broadcast_payload,
        )

    @staticmethod
    @static_method_log
    async def submit_saving_throw(state: CombatSession, payload: dict):
        """Processa a rolagem de salvaguarda de um alvo e dispara SAVING_THROW_ROLLED / SAVING_THROW_SUBMITTED."""
        data = SubmitSavingThrowPayload(**payload)
        target_id = data.combatantId or data.targetId
        if not target_id:
            return
        target = state.combatants.get(target_id)
        if not target:
            return

        roll_value = data.rollValue if data.rollValue is not None else (data.roll or 10)
        modifier = 0
        is_crit_success = roll_value == 20
        is_crit_fail = roll_value == 1

        if data.attribute and data.saveDC is not None:
            res = resolve_saving_throw(
                target, data.attribute, data.saveDC, roll_value
            )
            modifier = res["modifier"]
            total = res["total"]
            passed = res["passed"]
            is_crit_success = res["is_crit_success"]
            is_crit_fail = res["is_crit_fail"]
        else:
            if data.total is not None:
                total = data.total
                modifier = total - roll_value
            else:
                modifier = (
                    get_target_save_modifier(target, data.attribute)
                    if data.attribute
                    else 0
                )
                total = roll_value + modifier

            passed = (
                data.passed
                if data.passed is not None
                else (
                    is_crit_success
                    or (not is_crit_fail and total >= (data.saveDC or 10))
                )
            )

        damage_amount = data.damageAmount
        if damage_amount is None:
            base_damage = data.baseDamage or 0
            half_on_save = data.halfOnSave if data.halfOnSave is not None else True
            damage_amount = calculate_area_damage(base_damage, passed, half_on_save)

        if data.applyDamage and damage_amount > 0:
            target.hp.current = max(0, target.hp.current - damage_amount)

        res_str = "SUCESSO" if passed else "FALHA"
        attr_label = data.attribute or "Salvaguarda"
        log_msg = (
            f"🎲 {target.name} rolou salvaguarda de {attr_label}: "
            f"{roll_value} + {modifier} = {total} ({res_str})"
            f"{f' sofrerá {damage_amount} de dano' if damage_amount > 0 else ''}."
        )
        state.logs.append(log_msg)

        from app.managers.connection import manager

        broadcast_payload = {
            "requestId": data.requestId or data.attackId,
            "attackId": data.requestId or data.attackId,
            "combatantId": target.id,
            "targetId": target.id,
            "combatantName": target.name,
            "targetName": target.name,
            "rollValue": roll_value,
            "roll": roll_value,
            "modifier": modifier,
            "total": total,
            "passed": passed,
            "isCritSuccess": is_crit_success,
            "isCritFail": is_crit_fail,
            "damageAmount": damage_amount,
            "currentHp": target.hp.current,
            "log": log_msg,
        }

        await manager.broadcast_event(
            state.session_id,
            "SAVING_THROW_ROLLED",
            broadcast_payload,
        )
        await manager.broadcast_event(
            state.session_id,
            "SAVING_THROW_SUBMITTED",
            broadcast_payload,
        )
