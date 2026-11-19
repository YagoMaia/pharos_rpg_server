import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.main import app
from app.managers import connection as conn_module
from app.models.domain import Combatant, CombatSession, StatsPair, TurnActions
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore
from app.services.player_service import PlayerService


class CompendiumAndLegendaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_compendium.sqlite3"
        self.store = CampaignStore(self.db_path)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

        self.gm_token = self.client.post(
            "/auth/table-login", json={"name": "Victor", "pin": "victor"}
        ).json()["accessToken"]
        self.p1_token = self.client.post(
            "/auth/table-login", json={"name": "Gabriel", "pin": "gabriel"}
        ).json()["accessToken"]
        self.p2_token = self.client.post(
            "/auth/table-login", json={"name": "Leandro", "pin": "leandro"}
        ).json()["accessToken"]

        camp = self.client.post(
            "/campaigns",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Mesa Pharos"},
        ).json()
        self.campaign_id = camp["id"]

        for token in (self.p1_token, self.p2_token):
            inv = self.client.post(
                f"/campaigns/{self.campaign_id}/invites",
                headers={"Authorization": f"Bearer {self.gm_token}"},
            ).json()
            self.client.post(
                "/campaigns/join",
                headers={"Authorization": f"Bearer {token}"},
                json={"inviteCode": inv["code"]},
            )

        self.p1_id = self.store.user_for_token(self.p1_token)["id"]
        self.p2_id = self.store.user_for_token(self.p2_token)["id"]

    def tearDown(self):
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    def test_visibility_filtering_and_combat_template_redaction(self):
        gm_headers = {"Authorization": f"Bearer {self.gm_token}"}
        p1_headers = {"Authorization": f"Bearer {self.p1_token}"}
        p2_headers = {"Authorization": f"Bearer {self.p2_token}"}

        hidden = self.client.post(
            f"/campaigns/{self.campaign_id}/compendium",
            headers=gm_headers,
            json={
                "slug": "puraavanu",
                "kind": "fauna",
                "name": "Puraavanu",
                "region": "Assarif",
                "dangerLabel": "Extremo",
                "lore": "Devorador de ossos.",
                "visibility": "gm",
                "combatTemplateId": "tpl-puraavanu",
            },
        )
        self.assertEqual(hidden.status_code, 201)

        party_flora = self.client.post(
            f"/campaigns/{self.campaign_id}/compendium",
            headers=gm_headers,
            json={
                "slug": "yva-kans",
                "kind": "flora",
                "name": "Yva-Kans",
                "region": "Terug",
                "materialLabel": "Fibras / Isolamento",
                "lore": "Capim-ferro das estepes.",
                "visibility": "party",
            },
        ).json()

        selected_fauna = self.client.post(
            f"/campaigns/{self.campaign_id}/compendium",
            headers=gm_headers,
            json={
                "slug": "moris",
                "kind": "fauna",
                "name": "Moris",
                "region": "Terug",
                "dangerLabel": "Moderado",
                "lore": "Montaria ágil do norte.",
                "visibility": "selected",
                "visibleToMemberIds": [self.p1_id],
                "combatTemplateId": "tpl-moris",
            },
        ).json()

        # GM vê todas (3) e vê combatTemplateId / visibleToMemberIds
        gm_list = self.client.get(
            f"/campaigns/{self.campaign_id}/compendium", headers=gm_headers
        ).json()
        self.assertEqual(len(gm_list), 3)
        moris_gm = next(e for e in gm_list if e["id"] == selected_fauna["id"])
        self.assertEqual(moris_gm["combatTemplateId"], "tpl-moris")
        self.assertEqual(moris_gm["visibleToMemberIds"], [self.p1_id])

        # Player 1 vê party + selected[p1], mas NUNCA combatTemplateId nem visibleToMemberIds
        p1_list = self.client.get(
            f"/campaigns/{self.campaign_id}/compendium", headers=p1_headers
        ).json()
        self.assertEqual({e["slug"] for e in p1_list}, {"yva-kans", "moris"})
        for item in p1_list:
            self.assertNotIn("combatTemplateId", item)
            self.assertNotIn("visibleToMemberIds", item)

        # Player 2 vê apenas party
        p2_list = self.client.get(
            f"/campaigns/{self.campaign_id}/compendium", headers=p2_headers
        ).json()
        self.assertEqual([e["slug"] for e in p2_list], ["yva-kans"])

        # Notas privadas são isoladas por jogador
        note_res = self.client.put(
            f"/campaigns/{self.campaign_id}/compendium/{party_flora['id']}/notes",
            headers=p1_headers,
            json={"note": "Colher com luvas grossas."},
        )
        self.assertEqual(note_res.status_code, 200)

        # Player 2 não pode anotar entrada oculta dele
        denied_note = self.client.put(
            f"/campaigns/{self.campaign_id}/compendium/{selected_fauna['id']}/notes",
            headers=p2_headers,
            json={"note": "Tentativa"},
        )
        self.assertEqual(denied_note.status_code, 404)

        p1_after = self.client.get(
            f"/campaigns/{self.campaign_id}/compendium", headers=p1_headers
        ).json()
        p2_after = self.client.get(
            f"/campaigns/{self.campaign_id}/compendium", headers=p2_headers
        ).json()
        self.assertEqual(
            next(e for e in p1_after if e["id"] == party_flora["id"])["playerNote"],
            "Colher com luvas grossas.",
        )
        self.assertEqual(
            next(e for e in p2_after if e["id"] == party_flora["id"])["playerNote"],
            "",
        )

    def test_legendary_actions_consume_and_reset_on_turn(self):
        boss = Combatant(
            id="boss-1",
            name="Puraavanu #1",
            initiative=18,
            hp=StatsPair(current=140, max=140),
            focus=StatsPair(current=10, max=10),
            type="npc",
            armorClass=17,
            turnActions=TurnActions(standard=True, bonus=True, reaction=True),
            legendaryActionsMax=3,
            legendaryActionsRemaining=3,
        )
        hero = Combatant(
            id="hero-1",
            name="Valen",
            initiative=12,
            hp=StatsPair(current=40, max=40),
            focus=StatsPair(current=6, max=6),
            type="player",
            armorClass=15,
            turnActions=TurnActions(standard=True, bonus=True, reaction=True),
        )
        session = CombatSession(
            session_id="s-leg",
            turn_index=0,
            turn_order=["boss-1", "hero-1"],
            combatants={"boss-1": boss, "hero-1": hero},
        )
        class FakeRedis:
            async def publish(self, *_args):
                return 1

        orig_redis = conn_module.manager.redis
        conn_module.manager.redis = FakeRedis()
        try:
            async def _run_sequence():
                await PlayerService.resolve_action(
                    session,
                    {
                        "attackerId": "boss-1",
                        "targetId": "hero-1",
                        "actionName": "Bote Ossívoro",
                        "costType": "legendary",
                        "focusCost": 2,
                        "legendaryCost": 1,
                        "damageAmount": 12,
                    },
                )
                self.assertEqual(boss.legendaryActionsRemaining, 2)
                self.assertEqual(boss.focus.current, 8)
                self.assertEqual(hero.hp.current, 28)

                # Avança para hero-1 e volta para boss-1 -> restaura 3 ações lendárias
                await PlayerService.next_turn(session)
                self.assertEqual(session.turn_index, 1)
                await PlayerService.next_turn(session)
                self.assertEqual(session.turn_index, 0)
                self.assertEqual(boss.legendaryActionsRemaining, 3)

            asyncio.run(_run_sequence())
        finally:
            conn_module.manager.redis = orig_redis
