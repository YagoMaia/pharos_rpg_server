import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import WebSocketDisconnect

from app.managers.connection import ConnectionManager
from app.models.domain import Combatant, CombatSession
from app.services.engine import GameLogic
from app.services.gm_service import GMService
from app.services.player_service import PlayerService


def actor(identifier, **updates):
    data = dict(
        id=identifier, name=identifier, type="npc", initiative=10,
        hp={"current": 20, "max": 20}, focus={"current": 7, "max": 10},
        armorClass=18, turnActions={"standard": False, "bonus": False, "reaction": False},
        attributes={"Constituição": {"name": "Constituição", "value": 16, "modifier": 3}},
    )
    data.update(updates)
    return Combatant(**data)


def session(*actors):
    return CombatSession(session_id="room", combatants={a.id: a for a in actors}, turn_order=[a.id for a in actors])


class MemoryManager:
    def __init__(self, state):
        self.raw = state.model_dump_json()
        self.redis = Mock(set=self.save)
        self.events = []
        self.snapshots = []
        self.connect = AsyncMock()
        self.disconnect = Mock()

    async def save(self, key, value, **kwargs):
        await asyncio.sleep(0)
        self.raw = value

    async def get_state(self, session_id):
        state = CombatSession.model_validate_json(self.raw)
        await asyncio.sleep(0)
        return state

    async def broadcast_event(self, session_id, kind, payload):
        self.events.append((kind, payload))
        await asyncio.sleep(0)

    async def broadcast_state(self, session_id, state):
        await self.save(session_id, state.model_dump_json())
        self.snapshots.append(json.loads(self.raw))


class CombatRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_updates_maxima_and_armor_without_restoring_combat_resources(self):
        stance = dict(id="shot", name="Queima-Roupa", benefit="", restriction="", maneuver="", acAttribute="Constituição")
        old = actor("hero", type="player", baseArmorClass=18, armorClass=23, stances=[stance], activeStanceId="shot",
                    hp={"current": 8, "max": 20}, focus={"current": 2, "max": 10},
                    deathSaves={"successes": 1, "failures": 1},
                    conditions=[dict(id="poison", name="Envenenado", appliedAtTurn=0, sourceId="gm", description="")])
        incoming = actor("hero", type="player", baseArmorClass=19, armorClass=19, stances=[stance],
                         hp={"current": 27, "max": 27}, focus={"current": 12, "max": 12},
                         attributes={"Constituição": {"name": "Constituição", "value": 18, "modifier": 4}})
        state = session(old)
        manager = MemoryManager(state)
        with patch("app.managers.connection.manager", manager):
            for _ in range(3):
                await PlayerService.join_session(state, {"combatant": incoming.model_dump()})
                self.assertEqual((old.hp.current, old.hp.max), (8, 27))
                self.assertEqual((old.focus.current, old.focus.max), (2, 12))
                self.assertEqual(old.armorClass, 25)  # base 19 + CON 4 + ajuste temporário 2
                self.assertEqual(old.baseArmorClass, 19)
                self.assertFalse(old.turnActions.standard)
                self.assertFalse(old.turnActions.bonus)
                self.assertFalse(old.turnActions.reaction)
                self.assertEqual(old.deathSaves.failures, 1)
                self.assertEqual(old.conditions[0].id, "poison")
            # A postura continua removível sem subtrair o modificador antigo.
            await PlayerService.change_stance(state, {"combatantId": "hero", "stanceId": None})
            self.assertEqual(old.armorClass, 21)

    async def test_reconnect_handles_removed_stances_lower_maxima_and_legacy_armor(self):
        stance = dict(id="guard", name="Guarda", benefit="", restriction="", maneuver="", acBonus=2)
        old = actor("hero", type="player", armorClass=20, stances=[stance], activeStanceId="guard",
                    hp={"current": 27, "max": 27}, focus={"current": 8, "max": 10})
        incoming = actor("hero", type="player", armorClass=15, stances=[], hp={"current": 20, "max": 20}, focus={"current": 6, "max": 6})
        await PlayerService.join_session(session(old), {"combatant": incoming.model_dump()})
        self.assertEqual((old.hp.current, old.hp.max), (20, 20))
        self.assertEqual((old.focus.current, old.focus.max), (6, 6))
        self.assertEqual(old.armorClass, 15)
        self.assertEqual(old.baseArmorClass, 15)
        self.assertIsNone(old.activeStanceId)
        old.hp.current = 0
        incoming.hp.max = 30
        await PlayerService.join_session(session(old), {"combatant": incoming.model_dump()})
        self.assertEqual(old.hp.current, 0)

    async def test_reconnect_updates_pet_and_snapshot_preserves_damage_components(self):
        player = actor("hero", type="player")
        pet = actor("pet", type="pet", ownerId="hero", hp={"current": 3, "max": 20})
        incoming_pet = actor("pet", type="pet", ownerId="hero", armorClass=15, hp={"current": 30, "max": 30})
        player.weapons = actor("weapon", weapons={"melee": dict(name="Espada", damage="1d8", damageBonus=1,
            damageIncludesAttribute=False, attribute="Força", attackBonus=0, range="1.5m")}).weapons
        manager = MemoryManager(session(player, pet))
        await GameLogic(manager).handle_message("room", {"type": "JOIN_SESSION", "payload": {
            "combatant": player.model_dump(), "pet": incoming_pet.model_dump(),
        }})
        restored = await manager.get_state("room")
        self.assertEqual((restored.combatants["pet"].hp.current, restored.combatants["pet"].hp.max), (3, 30))
        self.assertEqual(restored.combatants["pet"].armorClass, 15)
        weapon = manager.snapshots[-1]["combatants"]["hero"]["weapons"]["melee"]
        self.assertEqual(weapon["damage"], "1d8")
        self.assertEqual(weapon["damageBonus"], 1)
        self.assertFalse(weapon["damageIncludesAttribute"])

    async def test_bonus_and_reaction_actions_work_with_standard_action_spent(self):
        for action in ("bonus", "reaction"):
            for healing in (False, True):
                attacker = actor("hero", type="player", turnActions={"standard": False, "bonus": True, "reaction": True})
                target = actor("target", hp={"current": 8, "max": 20})
                state = session(attacker, target)
                manager = MemoryManager(state)
                payload = dict(attackerId="hero", targetId="target", actionName="Teste", costType=action,
                               focusCost=2, damageAmount=0 if healing else 5, healingAmount=5 if healing else 0)
                with patch("app.managers.connection.manager", manager):
                    await PlayerService.resolve_action(state, payload)
                    self.assertEqual(target.hp.current, 13 if healing else 3)
                    self.assertEqual(attacker.focus.current, 5)
                    self.assertFalse(getattr(attacker.turnActions, action))
                    self.assertFalse(attacker.turnActions.standard)
                    self.assertTrue(getattr(attacker.turnActions, "bonus" if action == "reaction" else "reaction"))
                    await PlayerService.resolve_action(state, payload)
                    self.assertEqual(target.hp.current, 13 if healing else 3)
                    self.assertEqual(attacker.focus.current, 5)

    async def test_feats_survive_join_snapshot_and_reconnection_without_reapplying_bonuses(self):
        feat = dict(id="vigoroso", name="Vigoroso", category="Geral", benefit="+1 CON e PV por nível",
                    slotLevel=3, manualBenefits=False)
        custom = dict(id="faction", name="Facção", category="Social", benefit="Efeito manual")
        player = actor("hero", type="player", hp={"current": 20, "max": 27}, feats=[feat],
                       factionFeat=custom, specialFeat={**custom, "id": "special"})
        manager = MemoryManager(session())
        engine = GameLogic(manager)
        with patch("app.managers.connection.manager", manager):
            await engine.handle_message("room", {"type": "JOIN_SESSION", "payload": {"combatant": player.model_dump()}})
            restored = (await manager.get_state("room")).combatants["hero"]
            self.assertEqual(restored.feats[0].slotLevel, 3)
            self.assertFalse(restored.feats[0].manualBenefits)
            self.assertEqual(restored.factionFeat.id, "faction")
            self.assertEqual(restored.specialFeat.id, "special")
            self.assertEqual(restored.hp.max, 27)
            self.assertEqual(manager.snapshots[-1]["combatants"]["hero"]["feats"][0]["id"], "vigoroso")
            await engine.handle_message("room", {"type": "MANUAL_UPDATE", "payload": {"combatantId": "hero", "newHp": 12}})
            player.specialFeat = None
            await engine.handle_message("room", {"type": "JOIN_SESSION", "payload": {"combatant": player.model_dump()}})
            restored = (await manager.get_state("room")).combatants["hero"]
            self.assertIsNone(restored.specialFeat)
            self.assertEqual(restored.hp.current, 12)
            self.assertEqual(restored.hp.max, 27)

    async def test_two_campaign_sockets_do_not_lose_concurrent_updates(self):
        """Dois sockets atualizando a mesma sala em paralelo não podem perder escrita.

        Reescrito para a sala autenticada: a versão anterior chamava
        `websocket_endpoint(a, "ROOM")` com `clientId` dentro do payload, ou
        seja, dependia exatamente do ramo legado não autenticado e da
        normalização de caixa que foram removidos. A garantia testada
        (serialização por sala) é a mesma; apenas o caminho deixou de ser o
        legado.
        """
        # A sala começa com dois jogadores já presentes: o caminho autenticado
        # recusa que um JOIN tome o ID de um combatente que não é jogador.
        manager = MemoryManager(
            session(
                actor("a", type="player", sheetId="sheet-a"),
                actor("b", type="player", sheetId="sheet-b"),
            )
        )
        room = "campaign-6f1c0b0e-1a2b-4c3d-9e8f-7a6b5c4d3e2f"

        def socket(identifier, hp):
            combatant = actor(identifier, type="player", sheetId=f"sheet-{identifier}").model_dump()
            identity = {
                "authenticated": True,
                "role": "player",
                "client_id": f"user:{identifier}",
                "character_id": f"sheet-{identifier}",
                "player_id": None,
            }
            return identity, [
                {"type": "JOIN_SESSION", "payload": {"combatant": combatant}},
                {"type": "MANUAL_UPDATE", "payload": {"combatantId": identifier, "newHp": hp}},
            ]

        identity_a, messages_a = socket("a", 11)
        identity_b, messages_b = socket("b", 7)
        engine = GameLogic(manager)

        async def drive(identity, messages):
            for message in messages:
                await engine.handle_message(room, message, identity)

        with patch("app.managers.connection.manager", manager):
            await asyncio.gather(drive(identity_a, messages_a), drive(identity_b, messages_b))

        state = await manager.get_state(room)
        self.assertEqual({key: c.hp.current for key, c in state.combatants.items()}, {"a": 11, "b": 7})

    async def test_phase_survives_persistence_and_broadcast_and_duplicate_command(self):
        boss = actor("boss", hp={"current": 0, "max": 20}, armorClass=20,
                     activeStanceId="guard", stances=[dict(id="guard", name="Guarda", benefit="", restriction="", maneuver="", acBonus=2)],
                     conditions=[dict(id="poison", name="Envenenado", appliedAtTurn=0, sourceId="gm", description="", durationTurns=3)],
                     phases=[dict(name="Fase 2", maxHp=40, armorClass=18, stances=[]), dict(maxHp=60)])
        manager = MemoryManager(session(boss))
        engine = GameLogic(manager)
        command = {"type": "GM_ADVANCE_PHASE", "payload": {"combatantId": "boss", "expectedPhaseIndex": 0}}
        await engine.handle_message("room", command)
        await engine.handle_message("room", command)
        restored = (await manager.get_state("room")).combatants["boss"]
        self.assertEqual(restored.currentPhaseIndex, 1)
        self.assertEqual(restored.hp.current, 40)
        self.assertEqual(restored.armorClass, 18)
        self.assertIsNone(restored.activeStanceId)
        self.assertEqual(restored.stances, [])
        self.assertEqual(restored.focus.current, 7)
        self.assertFalse(restored.turnActions.standard)
        self.assertEqual(restored.conditions[0].durationTurns, 3)
        self.assertEqual(len(manager.snapshots[-1]["combatants"]["boss"]["phases"]), 2)
        await engine.handle_message("room", {**command, "payload": {"combatantId": "boss", "expectedPhaseIndex": 1}})
        await engine.handle_message("room", {**command, "payload": {"combatantId": "boss", "expectedPhaseIndex": 2}})
        self.assertEqual((await manager.get_state("room")).combatants["boss"].currentPhaseIndex, 2)

    async def test_authorization_locks_gm_and_combatant_ownership(self):
        hero = actor("hero", type="player", turnActions={"standard": True, "bonus": True, "reaction": True})
        other = actor("other", type="player")
        state = session(hero, other)
        manager = MemoryManager(state)
        engine = GameLogic(manager)

        gm_identity = {"client_id": None, "role": None, "player_id": None}
        await engine.handle_message("room", {"type": "GM_CONNECT", "payload": {"clientId": "gm-1"}}, gm_identity)
        self.assertEqual(gm_identity["role"], "gm")

        with self.assertRaisesRegex(ValueError, "Apenas o GM"):
            await engine.handle_message(
                "room",
                {"type": "GM_ADD_NPC", "payload": {"clientId": "gm-2", **actor("npc").model_dump()}},
                {"client_id": None, "role": None, "player_id": None},
            )

        player_identity = {"client_id": None, "role": None, "player_id": None}
        await engine.handle_message("room", {"type": "JOIN_SESSION", "payload": {
            "clientId": "player-1", "combatant": hero.model_dump(),
        }}, player_identity)
        self.assertEqual(player_identity["player_id"], "hero")

        with self.assertRaisesRegex(ValueError, "outro usuário"):
            await engine.handle_message("room", {"type": "JOIN_SESSION", "payload": {
                "clientId": "player-2", "combatant": hero.model_dump(),
            }}, {"client_id": None, "role": None, "player_id": None})

        await engine.handle_message("room", {"type": "CHANGE_STANCE", "payload": {
            "clientId": "player-1", "combatantId": "hero", "stanceId": None,
        }}, player_identity)

        with self.assertRaisesRegex(ValueError, "seu personagem"):
            await engine.handle_message("room", {"type": "MANUAL_UPDATE", "payload": {
                "clientId": "player-1", "combatantId": "other", "newHp": 1,
            }}, player_identity)

    async def test_authorization_allows_owned_pet_and_current_turn_only(self):
        hero = actor("hero", type="player")
        pet = actor("pet", type="pet", ownerId="hero")
        other = actor("other", type="player")
        state = session(hero, pet, other)
        state.turn_index = 0
        manager = MemoryManager(state)
        engine = GameLogic(manager)
        identity = {"client_id": None, "role": None, "player_id": None}

        await engine.handle_message("room", {"type": "JOIN_SESSION", "payload": {
            "clientId": "player-1", "combatant": hero.model_dump(), "pet": pet.model_dump(),
        }}, identity)
        await engine.handle_message("room", {"type": "MANUAL_UPDATE", "payload": {
            "clientId": "player-1", "combatantId": "pet", "newHp": 9,
        }}, identity)
        self.assertEqual((await manager.get_state("room")).combatants["pet"].hp.current, 9)

        state = await manager.get_state("room")
        state.turn_index = state.turn_order.index("other")
        manager.raw = state.model_dump_json()
        with self.assertRaisesRegex(ValueError, "turno atual"):
            await engine.handle_message("room", {"type": "END_TURN", "payload": {"clientId": "player-1"}}, identity)

    async def test_turn_skips_dead_npc_without_skipping_next_and_keeps_pending_boss(self):
        state = session(actor("a"), actor("dead", hp={"current": 0, "max": 20}),
                        actor("boss", hp={"current": 0, "max": 20}, phases=[{}]), actor("b"))
        manager = MemoryManager(state)
        with patch("app.managers.connection.manager", manager):
            await GameLogic(manager).handle_message("room", {"type": "END_TURN"})
        restored = await manager.get_state("room")
        self.assertNotIn("dead", restored.combatants)
        self.assertIn("boss", restored.combatants)
        self.assertEqual(restored.turn_order[restored.turn_index], "b")
        self.assertTrue(restored.combatants["b"].turnActions.standard)
        self.assertNotIn("dead", manager.snapshots[-1]["combatants"])

    async def test_phase_without_armor_override_removes_dynamic_stance_bonus(self):
        boss = actor("boss", armorClass=21, activeStanceId="shot", phases=[{}], stances=[dict(
            id="shot", name="Queima-Roupa", benefit="", restriction="", maneuver="", acAttribute="Constituição")])
        await GMService.advance_phase(session(boss), {"combatantId": "boss"})
        self.assertEqual(boss.armorClass, 18)

    async def test_server_computes_stance_armor_and_rejects_incapacitated_actions(self):
        fighter = actor("fighter", turnActions={"standard": True, "bonus": True, "reaction": True}, stances=[dict(
            id="shot", name="Queima-Roupa", benefit="", restriction="", maneuver="", acAttribute="Constituição")])
        state = session(fighter)
        with patch("app.managers.connection.manager", MemoryManager(state)):
            await PlayerService.change_stance(state, {"combatantId": "fighter", "stanceId": "shot", "newAC": 999})
            self.assertEqual(fighter.armorClass, 21)
            await PlayerService.change_stance(state, {"combatantId": "fighter", "stanceId": None, "newAC": 999})
            self.assertEqual(fighter.armorClass, 18)
            await PlayerService.apply_condition(state, {"combatantId": "fighter", "conditionName": "Incapacitado", "description": ""})
            await PlayerService.resolve_action(state, {"attackerId": "fighter", "actionName": "Ataque", "costType": "standard", "focusCost": 2})
            self.assertEqual(fighter.focus.current, 7)

    async def test_pubsub_delivers_events_in_order_and_cleans_failed_connections(self):
        manager = ConnectionManager()
        delivered = []

        async def send(data):
            if data == "first":
                await asyncio.sleep(0.01)
            delivered.append(data)

        socket = Mock(send_text=send)
        manager.active_connections["room"] = [socket]
        complete = asyncio.Event()

        async def listen():
            yield {"type": "message", "data": "first"}
            yield {"type": "message", "data": "second"}
            complete.set()
            await asyncio.Event().wait()

        pubsub = Mock(subscribe=AsyncMock(), unsubscribe=AsyncMock(), close=AsyncMock(), listen=listen)
        manager.redis = Mock(pubsub=Mock(return_value=pubsub))
        task = asyncio.create_task(manager.subscribe_channel("room"))
        await asyncio.wait_for(complete.wait(), 1)
        task.cancel()
        await task
        self.assertEqual(delivered, ["first", "second"])
        failed = Mock(send_text=AsyncMock(side_effect=RuntimeError("closed")))
        manager.active_connections["room"] = [failed]
        await manager._safe_send("room", failed, "data")
        self.assertNotIn("room", manager.active_connections)
        await manager._safe_send("room", failed, "data")

    async def test_connection_waits_for_redis_subscription_ack(self):
        manager = ConnectionManager()
        subscribed = asyncio.Event()
        acknowledge = asyncio.Event()

        async def listen():
            subscribed.set()
            await acknowledge.wait()
            yield {"type": "subscribe"}
            await asyncio.Event().wait()

        pubsub = Mock(subscribe=AsyncMock(), unsubscribe=AsyncMock(), close=AsyncMock(), listen=listen)
        manager.redis = Mock(pubsub=Mock(return_value=pubsub))
        socket = Mock(accept=AsyncMock(), close=AsyncMock())
        connecting = asyncio.create_task(manager.connect("ROOM", socket))
        await asyncio.wait_for(subscribed.wait(), 1)
        self.assertFalse(connecting.done())
        acknowledge.set()
        await asyncio.wait_for(connecting, 1)
        subscriber = manager.pubsub_tasks["room"]
        manager.disconnect("ROOM", socket)
        await subscriber
        self.assertEqual(manager.pubsub_tasks, {})
        self.assertEqual(manager.subscription_ready, {})


if __name__ == "__main__":
    unittest.main()
