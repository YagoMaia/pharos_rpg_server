import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import WebSocketDisconnect

from app.managers.connection import ConnectionManager
from app.models.domain import Combatant, CombatSession
from app.routers import websocket as websocket_router
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
    async def test_two_websocket_connections_do_not_lose_concurrent_updates(self):
        manager = MemoryManager(session(actor("a"), actor("b")))

        def socket(identifier, hp):
            return Mock(
                receive_text=AsyncMock(side_effect=[json.dumps({"type": "MANUAL_UPDATE", "payload": {
                    "combatantId": identifier, "newHp": hp,
                }}), WebSocketDisconnect()]),
                send_json=AsyncMock(),
            )

        a, b = socket("a", 11), socket("b", 7)
        with patch.object(websocket_router, "manager", manager), \
             patch.object(websocket_router.game_logic, "manager", manager), \
             patch("app.managers.connection.manager", manager):
            await asyncio.gather(
                websocket_router.websocket_endpoint(a, "ROOM"),
                websocket_router.websocket_endpoint(b, "room"),
            )
        state = await manager.get_state("room")
        self.assertEqual({key: c.hp.current for key, c in state.combatants.items()}, {"a": 11, "b": 7})
        self.assertEqual(manager.disconnect.call_count, 2)
        a.send_json.assert_not_called()
        b.send_json.assert_not_called()

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
