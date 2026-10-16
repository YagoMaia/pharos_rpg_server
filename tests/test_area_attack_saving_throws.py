import asyncio
import unittest
from unittest.mock import Mock, patch

from app.models.domain import Combatant, CombatSession
from app.services.combat_utils import (
    calculate_area_damage,
    calculate_save_dc,
    get_target_save_modifier,
    resolve_saving_throw,
)
from app.services.engine import GameLogic


def make_actor(identifier, **updates):
    data = dict(
        id=identifier,
        name=identifier,
        type="player",
        initiative=10,
        hp={"current": 30, "max": 30},
        focus={"current": 10, "max": 10},
        armorClass=15,
        turnActions={"standard": True, "bonus": True, "reaction": True},
        attributes={
            "Destreza": {"name": "Destreza", "value": 16, "modifier": 3},
            "Inteligência": {"name": "Inteligência", "value": 18, "modifier": 4},
            "Constituição": {"name": "Constituição", "value": 14, "modifier": 2},
            "Força": {"name": "Força", "value": 10, "modifier": 0},
            "Sabedoria": {"name": "Sabedoria", "value": 12, "modifier": 1},
            "Carisma": {"name": "Carisma", "value": 8, "modifier": -1},
        },
    )
    data.update(updates)
    return Combatant(**data)


def make_session(*actors):
    return CombatSession(
        session_id="room-test",
        combatants={a.id: a for a in actors},
        turn_order=[a.id for a in actors],
        combatantOwners={a.id: f"owner-{a.id}" for a in actors},
    )


class MemoryManagerMock:
    def __init__(self, state):
        self.state = state
        self.redis = Mock(set=self.save)
        self.events = []

    async def save(self, key, value, **kwargs):
        await asyncio.sleep(0)

    async def get_state(self, session_id):
        await asyncio.sleep(0)
        return self.state

    async def broadcast_event(self, session_id, event_type, payload):
        self.events.append((event_type, payload))
        await asyncio.sleep(0)

    async def broadcast_state(self, session_id, state):
        await asyncio.sleep(0)


class TestCombatUtils(unittest.TestCase):
    def test_calculate_save_dc(self):
        attacker = make_actor("mage")
        # 8 + prof(2) + INT mod(4) = 14
        self.assertEqual(calculate_save_dc(attacker, "Inteligência"), 14)
        # 8 + prof(2) + DES mod(3) = 13
        self.assertEqual(calculate_save_dc(attacker, "Destreza"), 13)
        # Default takes highest relevant attribute (INT mod 4) -> 14
        self.assertEqual(calculate_save_dc(attacker), 14)

    def test_get_target_save_modifier(self):
        target = make_actor("target")
        self.assertEqual(get_target_save_modifier(target, "Destreza"), 3)
        self.assertEqual(get_target_save_modifier(target, "Constituição"), 2)
        self.assertEqual(get_target_save_modifier(target, "Inexistente"), 0)

    def test_resolve_saving_throw(self):
        target = make_actor("target")  # Destreza modifier = 3
        # Normal pass: 11 + 3 = 14 >= 14
        res = resolve_saving_throw(target, "Destreza", save_dc=14, roll_result=11)
        self.assertTrue(res["passed"])
        self.assertEqual(res["total"], 14)
        self.assertFalse(res["is_crit_success"])
        self.assertFalse(res["is_crit_fail"])

        # Normal fail: 10 + 3 = 13 < 14
        res = resolve_saving_throw(target, "Destreza", save_dc=14, roll_result=10)
        self.assertFalse(res["passed"])
        self.assertEqual(res["total"], 13)

        # Critical success: natural 20 passes even if total < save_dc
        res = resolve_saving_throw(target, "Destreza", save_dc=30, roll_result=20)
        self.assertTrue(res["passed"])
        self.assertTrue(res["is_crit_success"])

        # Critical failure: natural 1 fails even if total >= save_dc
        res = resolve_saving_throw(target, "Destreza", save_dc=2, roll_result=1)
        self.assertFalse(res["passed"])
        self.assertTrue(res["is_crit_fail"])

    def test_calculate_area_damage(self):
        # Failed save takes full damage
        self.assertEqual(
            calculate_area_damage(base_damage=25, passed=False, half_on_save=True), 25
        )
        # Passed save with half_on_save=True takes half rounded down (25 // 2 = 12)
        self.assertEqual(
            calculate_area_damage(base_damage=25, passed=True, half_on_save=True), 12
        )
        # Passed save with half_on_save=False takes 0
        self.assertEqual(
            calculate_area_damage(base_damage=25, passed=True, half_on_save=False), 0
        )


class TestAreaAttackSavingThrowsWS(unittest.IsolatedAsyncioTestCase):
    async def test_request_saving_throw_broadcasts_event(self):
        attacker = make_actor("caster")
        target1 = make_actor("t1")
        target2 = make_actor("t2")
        state = make_session(attacker, target1, target2)
        mock_manager = MemoryManagerMock(state)

        engine = GameLogic(mock_manager)
        payload = {
            "attackerId": "caster",
            "actionName": "Bola de Fogo",
            "attribute": "Destreza",
            "saveDC": 14,
            "baseDamage": 28,
            "halfOnSave": True,
            "targetIds": ["t1", "t2"],
        }

        # Attacker player sends REQUEST_SAVING_THROW
        identity = {
            "role": "player",
            "client_id": "owner-caster",
        }
        with patch("app.managers.connection.manager", mock_manager):
            await engine.handle_message(
                state.session_id,
                {"type": "REQUEST_SAVING_THROW", "payload": payload},
                identity,
            )

        self.assertEqual(len(mock_manager.events), 1)
        event_type, event_payload = mock_manager.events[0]
        self.assertEqual(event_type, "REQUEST_SAVING_THROW")
        self.assertEqual(event_payload["attackerId"], "caster")
        self.assertEqual(event_payload["actionName"], "Bola de Fogo")
        self.assertEqual(event_payload["attribute"], "Destreza")
        self.assertEqual(event_payload["saveDC"], 14)
        self.assertEqual(event_payload["baseDamage"], 28)
        self.assertTrue(event_payload["halfOnSave"])
        self.assertEqual(event_payload["targetIds"], ["t1", "t2"])
        self.assertIn("requestId", event_payload)

    async def test_request_saving_throw_fails_if_unauthorized(self):
        attacker = make_actor("caster")
        target1 = make_actor("t1")
        state = make_session(attacker, target1)
        mock_manager = MemoryManagerMock(state)

        engine = GameLogic(mock_manager)
        payload = {
            "attackerId": "caster",
            "actionName": "Bola de Fogo",
            "attribute": "Destreza",
            "saveDC": 14,
            "targetIds": ["t1"],
        }

        # Another player tries to request saving throw for caster
        identity = {
            "role": "player",
            "client_id": "other-user",
        }
        with patch("app.managers.connection.manager", mock_manager):
            with self.assertRaises(ValueError):
                await engine.handle_message(
                    state.session_id,
                    {"type": "REQUEST_SAVING_THROW", "payload": payload},
                    identity,
                )

    async def test_submit_saving_throw_resolves_and_broadcasts(self):
        attacker = make_actor("caster")
        target1 = make_actor("t1")  # Destreza mod = 3
        state = make_session(attacker, target1)
        mock_manager = MemoryManagerMock(state)

        engine = GameLogic(mock_manager)
        payload = {
            "requestId": "req-123",
            "combatantId": "t1",
            "rollValue": 10,  # 10 + 3 = 13 < 15 -> Fails
            "attribute": "Destreza",
            "saveDC": 15,
            "baseDamage": 20,
            "halfOnSave": True,
        }

        identity = {
            "role": "player",
            "client_id": "owner-t1",
        }
        with patch("app.managers.connection.manager", mock_manager):
            await engine.handle_message(
                state.session_id,
                {"type": "SUBMIT_SAVING_THROW", "payload": payload},
                identity,
            )

        self.assertEqual(len(mock_manager.events), 1)
        event_type, event_payload = mock_manager.events[0]
        self.assertEqual(event_type, "SAVING_THROW_ROLLED")
        self.assertEqual(event_payload["requestId"], "req-123")
        self.assertEqual(event_payload["combatantId"], "t1")
        self.assertEqual(event_payload["rollValue"], 10)
        self.assertEqual(event_payload["modifier"], 3)
        self.assertEqual(event_payload["total"], 13)
        self.assertFalse(event_payload["passed"])
        self.assertEqual(event_payload["damageAmount"], 20)

    async def test_submit_saving_throw_with_success_and_half_damage(self):
        attacker = make_actor("caster")
        target1 = make_actor("t1")  # Destreza mod = 3
        state = make_session(attacker, target1)
        mock_manager = MemoryManagerMock(state)

        engine = GameLogic(mock_manager)
        payload = {
            "requestId": "req-456",
            "combatantId": "t1",
            "rollValue": 12,  # 12 + 3 = 15 >= 15 -> Passes
            "attribute": "Destreza",
            "saveDC": 15,
            "baseDamage": 20,
            "halfOnSave": True,
        }

        identity = {
            "role": "player",
            "client_id": "owner-t1",
        }
        with patch("app.managers.connection.manager", mock_manager):
            await engine.handle_message(
                state.session_id,
                {"type": "SUBMIT_SAVING_THROW", "payload": payload},
                identity,
            )

        self.assertEqual(len(mock_manager.events), 1)
        _, event_payload = mock_manager.events[0]
        self.assertTrue(event_payload["passed"])
        self.assertEqual(event_payload["damageAmount"], 10)  # 20 // 2 = 10

    async def test_gm_can_submit_saving_throw_for_any_target(self):
        attacker = make_actor("caster")
        target1 = make_actor("t1")
        state = make_session(attacker, target1)
        mock_manager = MemoryManagerMock(state)

        engine = GameLogic(mock_manager)
        payload = {
            "requestId": "req-789",
            "combatantId": "t1",
            "rollValue": 20,  # Natural 20
            "attribute": "Destreza",
            "saveDC": 25,
            "baseDamage": 30,
            "halfOnSave": True,
        }

        gm_identity = {
            "role": "gm",
            "client_id": "gm-client",
        }
        with patch("app.managers.connection.manager", mock_manager):
            await engine.handle_message(
                state.session_id,
                {"type": "SUBMIT_SAVING_THROW", "payload": payload},
                gm_identity,
            )

        self.assertEqual(len(mock_manager.events), 1)
        _, event_payload = mock_manager.events[0]
        self.assertTrue(event_payload["passed"])
        self.assertTrue(event_payload["isCritSuccess"])
        self.assertEqual(event_payload["damageAmount"], 15)  # 30 // 2


if __name__ == "__main__":
    unittest.main()
