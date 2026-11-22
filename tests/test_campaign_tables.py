import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore


class CampaignTablesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_tables.sqlite3"
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
            json={"name": "Expedição Pharos"},
        ).json()
        self.campaign_id = camp["id"]

        for tok in (self.p1_token, self.p2_token):
            inv = self.client.post(
                f"/campaigns/{self.campaign_id}/invites",
                headers={"Authorization": f"Bearer {self.gm_token}"},
            ).json()
            self.client.post(
                "/campaigns/join",
                headers={"Authorization": f"Bearer {tok}"},
                json={"inviteCode": inv["code"]},
            )

        details = self.client.get(
            f"/campaigns/{self.campaign_id}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        by_name = {m["name"]: m["id"] for m in details["members"]}
        self.p1_id = by_name["Gabriel"]
        self.p2_id = by_name["Leandro"]

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.tmp.cleanup()

    def test_tables_crud_visibility_privacy_and_location_masking(self):
        secret_loc = self.client.post(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Manora", "kind": "region", "visibility": "gm"},
        ).json()
        public_loc = self.client.post(
            f"/campaigns/{self.campaign_id}/locations",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={"name": "Pedra Branca", "kind": "region", "visibility": "party"},
        ).json()

        # 1. Tabela de rolagem d100 (Caos Arcano) visível a todos
        chaos = self.client.post(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Tabela de Caos Arcano",
                "kind": "roll",
                "category": "Caos Arcano",
                "diceFormula": "d100",
                "description": "Efeitos ao rolar na tabela de Caos.",
                "gmNotes": "Ajustar CD se houver lei quebrada.",
                "columns": ["Faixa", "Efeito"],
                "rows": [
                    {"id": "r1", "min": 1, "max": 50, "cells": ["01-50", "Faíscas inofensivas"]},
                    {"id": "r2", "min": 51, "max": 100, "cells": ["51-100", "Explosão arcana 2d6"]},
                ],
                "locationId": secret_loc["id"],
                "visibility": "party",
            },
        )
        self.assertEqual(chaos.status_code, 201)
        chaos_data = chaos.json()
        self.assertEqual(chaos_data["diceFormula"], "d100")
        self.assertEqual(chaos_data["gmNotes"], "Ajustar CD se houver lei quebrada.")
        self.assertEqual(chaos_data["locationId"], secret_loc["id"])

        # 2. Tabela de referência (Rotas) visível apenas para Gabriel (p1)
        routes = self.client.post(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Segunda Fase — Rotas",
                "kind": "reference",
                "category": "Expedição",
                "description": "Orçamento: 3.500 Pratas",
                "columns": ["Destino", "Tempo", "Transporte", "Guia (Pratas)", "Total"],
                "rows": [
                    {
                        "id": "rot1",
                        "cells": ["Pedra Branca (Rota Curta)", "40 dias", "300", "Taktuq Aullaqti — 700", "1.000"],
                    }
                ],
                "locationId": public_loc["id"],
                "visibility": "selected",
                "visibleToMemberIds": [self.p1_id],
            },
        )
        self.assertEqual(routes.status_code, 201)

        # 3. Tabela privada do Mestre
        gm_only = self.client.post(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Preços do Mercado Negro",
                "kind": "reference",
                "columns": ["Item", "Custo"],
                "rows": [{"id": "m1", "cells": ["Ópio", "150"]}],
                "visibility": "gm",
            },
        )
        self.assertEqual(gm_only.status_code, 201)

        # GM vê todas as 3
        gm_list = self.client.get(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        self.assertEqual(len(gm_list), 3)

        # p1 vê chaos + routes, sem gmNotes/visibleToMemberIds e com secret_loc mascarado
        p1_list = self.client.get(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        ).json()
        self.assertEqual({t["title"] for t in p1_list}, {"Tabela de Caos Arcano", "Segunda Fase — Rotas"})
        for item in p1_list:
            self.assertNotIn("gmNotes", item)
            self.assertNotIn("visibleToMemberIds", item)
            if item["title"] == "Tabela de Caos Arcano":
                self.assertIsNone(item["locationId"])
            else:
                self.assertEqual(item["locationId"], public_loc["id"])

        # p2 vê apenas chaos
        p2_list = self.client.get(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.p2_token}"},
        ).json()
        self.assertEqual([t["title"] for t in p2_list], ["Tabela de Caos Arcano"])

        # Jogador não pode criar/editar/excluir
        for method, url in (
            ("post", f"/campaigns/{self.campaign_id}/tables"),
            ("put", f"/campaigns/{self.campaign_id}/tables/{chaos_data['id']}"),
        ):
            res = getattr(self.client, method)(
                url,
                headers={"Authorization": f"Bearer {self.p1_token}"},
                json={"title": "Hack", "kind": "reference", "columns": ["A"], "rows": []},
            )
            self.assertEqual(res.status_code, 403)
        del_p1 = self.client.delete(
            f"/campaigns/{self.campaign_id}/tables/{chaos_data['id']}",
            headers={"Authorization": f"Bearer {self.p1_token}"},
        )
        self.assertEqual(del_p1.status_code, 403)

        # Validação 422 para selected sem membros e locationId inexistente
        bad_sel = self.client.post(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Inválida",
                "kind": "reference",
                "columns": ["A"],
                "rows": [],
                "visibility": "selected",
                "visibleToMemberIds": [],
            },
        )
        self.assertEqual(bad_sel.status_code, 422)

        bad_loc = self.client.post(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Inválida",
                "kind": "reference",
                "columns": ["A"],
                "rows": [],
                "locationId": "nao-existe",
            },
        )
        self.assertEqual(bad_loc.status_code, 422)

        # Excluir localidade limpa locationId na tabela (SET NULL)
        self.client.delete(
            f"/campaigns/{self.campaign_id}/locations/{public_loc['id']}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        after_loc_del = self.client.get(
            f"/campaigns/{self.campaign_id}/tables",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        ).json()
        routes_after = next(t for t in after_loc_del if t["id"] == routes.json()["id"])
        self.assertIsNone(routes_after["locationId"])

        # Atualizar e excluir tabela
        upd = self.client.put(
            f"/campaigns/{self.campaign_id}/tables/{routes_after['id']}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
            json={
                "title": "Segunda Fase — Rotas (Atualizada)",
                "kind": "reference",
                "category": "Expedição",
                "columns": ["Destino", "Total"],
                "rows": [{"id": "rot1", "cells": ["Talras", "2.100"]}],
                "visibility": "party",
            },
        )
        self.assertEqual(upd.status_code, 200)
        self.assertEqual(upd.json()["title"], "Segunda Fase — Rotas (Atualizada)")

        rem = self.client.delete(
            f"/campaigns/{self.campaign_id}/tables/{gm_only.json()['id']}",
            headers={"Authorization": f"Bearer {self.gm_token}"},
        )
        self.assertEqual(rem.status_code, 204)
