"""PIN da mesa: provisão, hash e a regressão do `pin == nome.minúsculas`.

O buraco que estes testes fecham: `table_login` comparava o PIN com o próprio nome
em minúsculas, e a tela de login do app mostrava a dica (`dica: victor`). Qualquer
pessoa com a URL entrava como qualquer membro da mesa — e a partir da Etapa 5 isso
significaria também emitir credencial do assistente de preparação de outra pessoa.
"""

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.core.database import open_connection
from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services.campaign_store import CampaignStore
from app.services.errors import CampaignError

GM_PIN = "mestre-de-pharos"
PLAYER_PIN = "jogador-de-pharos"


class TablePinTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.db = Path(self.directory.name) / "table.sqlite3"
        self.store = CampaignStore(self.db)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.directory.cleanup()

    def login(self, name, pin):
        return self.client.post("/auth/table-login", json={"name": name, "pin": pin})

    def row(self, name):
        with closing(open_connection(self.db)) as conn:
            return conn.execute(
                "SELECT user_id, pin_hash FROM table_users WHERE name = ?", (name,)
            ).fetchone()

    def test_nome_e_nome_em_minusculas_nao_autenticam(self):
        for pin in ("victor", "Victor", "VICTOR"):
            response = self.login("Victor", pin)
            self.assertEqual(response.status_code, 401, f"pin={pin} passou: {response.text}")

    def test_pin_provisionado_autentica_e_mantem_as_contas_separadas(self):
        self.store.set_table_pin("Victor", GM_PIN)
        self.store.set_table_pin("Gabriel", PLAYER_PIN)

        gm = self.login("Victor", GM_PIN)
        player = self.login("Gabriel", PLAYER_PIN)
        self.assertEqual(gm.status_code, 200, gm.text)
        self.assertEqual(player.status_code, 200, player.text)
        self.assertNotEqual(gm.json()["accessToken"], player.json()["accessToken"])

        # mesmo nome, PIN de outra pessoa
        self.assertEqual(self.login("Victor", PLAYER_PIN).status_code, 401)
        # caixa do nome continua tolerante, a do PIN não é ignorada
        self.assertEqual(self.login("victor", GM_PIN).status_code, 200)

    def test_pin_guardado_com_hash_e_nao_em_texto_puro(self):
        self.store.set_table_pin("Victor", GM_PIN)
        stored = self.row("Victor")["pin_hash"]

        self.assertNotEqual(stored, GM_PIN)
        salt, digest = stored.split(":")
        self.assertEqual(len(bytes.fromhex(salt)), 16)
        self.assertEqual(len(bytes.fromhex(digest)), 32)
        with closing(open_connection(self.db)) as conn:
            found = conn.execute(
                "SELECT 1 FROM users WHERE password_hash LIKE ?", (f"%{GM_PIN}%",)
            ).fetchone()
        self.assertIsNone(found, "o PIN não pode aparecer em nenhuma coluna")

    def test_identidade_provisionada_sem_pin_nao_entra_e_diz_o_motivo(self):
        # estado de uma base migrada: linha de mesa existe, coluna pin_hash está NULL
        self.store.set_table_pin("Wayne", "wayne-primeiro-pin")
        with closing(open_connection(self.db)) as conn, conn:
            conn.execute("UPDATE table_users SET pin_hash = NULL WHERE name = 'Wayne'")

        response = self.login("Wayne", "wayne-primeiro-pin")
        self.assertEqual(response.status_code, 401)
        self.assertIn("ainda não definido", response.json()["detail"])

    def test_girar_o_pin_derruba_as_sessoes_daquele_usuario(self):
        self.store.set_table_pin("Victor", GM_PIN)
        token = self.store.table_login("Victor", GM_PIN)
        self.assertIsNotNone(self.store.user_for_token(token))

        self.store.set_table_pin("Victor", "novo-pin-do-victor")
        self.assertIsNone(self.store.user_for_token(token), "PIN girado tem de derrubar a sessão")
        self.assertEqual(self.login("Victor", GM_PIN).status_code, 401)
        self.assertEqual(self.login("Victor", "novo-pin-do-victor").status_code, 200)

    def test_pin_nao_pode_ser_o_proprio_nome(self):
        with self.assertRaises(CampaignError) as caught:
            self.store.set_table_pin("Victor", "victor")
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("victor", str(caught.exception))
        self.assertEqual(self.login("Victor", "victor").status_code, 401)

    def test_pin_curto_ou_nome_fora_da_lista_sao_recusados_com_formato_esperado(self):
        with self.assertRaises(CampaignError) as curto:
            self.store.set_table_pin("Victor", "123")
        self.assertIn("6", str(curto.exception))

        with self.assertRaises(CampaignError) as desconhecido:
            self.store.set_table_pin("Mordor", "pin-valido-123")
        self.assertEqual(desconhecido.exception.status, 422)
        self.assertIn("Mordor", str(desconhecido.exception))
        self.assertIn("Victor", str(desconhecido.exception))

    def test_migracao_acrescenta_pin_hash_a_base_antiga(self):
        old = Path(self.directory.name) / "legado.sqlite3"
        with closing(sqlite3.connect(old)) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE table_users (
                    name TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE REFERENCES users(id)
                );
                INSERT INTO users VALUES ('u1', 'u1@table.invalid', 'Victor', 'x');
                INSERT INTO table_users VALUES ('Victor', 'u1');
                """
            )

        migrated_store = CampaignStore(old)  # a abertura aplica a migration

        with closing(open_connection(old)) as conn:
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(table_users)")]
            kept = conn.execute("SELECT user_id FROM table_users WHERE name = 'Victor'").fetchone()
        self.assertIn("pin_hash", columns)
        self.assertEqual(kept["user_id"], "u1", "a mesa existente não pode desaparecer")
        # O cliente precisa usar a base legada migrada, não a base nova do setUp.
        app.dependency_overrides[get_campaign_store] = lambda: migrated_store
        self.assertEqual(self.login("Victor", "victor").status_code, 401)
        migrated_store.set_table_pin("Victor", GM_PIN)
        response = self.login("Victor", GM_PIN)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            migrated_store.user_for_token(response.json()["accessToken"])["id"], "u1"
        )


if __name__ == "__main__":
    unittest.main()
