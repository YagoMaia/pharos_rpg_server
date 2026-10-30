"""Conexões SQLite do backend: modo de journal, chaves estrangeiras e concorrência.

Motivo do WAL: em `journal_mode=DELETE` (o padrão do SQLite) um escritor bloqueia
leitores e vice-versa. Com o diário de preparação, o painel do mestre e o
combate compartilhando o mesmo arquivo, leitura de campanha passa a coexistir com
importação de notas. O `timeout=5` do `sqlite3` apenas espera a liberação do
lock; ele não torna leitura e escrita simultâneas.

Atenção operacional: WAL é propriedade **persistente do arquivo**, então uma
conexão que o ativa deixa o banco nesse modo para todas as outras. Isso muda o
procedimento de backup — ver `docs/DEPLOYMENT_DOCKER.md` no frontend.
"""

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.database import open_connection
from app.services.campaign_store import CampaignStore


class DatabaseConnectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "pharos.sqlite3"

    def tearDown(self):
        self.directory.cleanup()

    def test_connection_enables_wal(self):
        with closing(open_connection(self.database)) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_connection_enforces_foreign_keys_and_named_rows(self):
        CampaignStore(self.database).register("gm@example.com", "Mestre", "safe-password-123")
        conn = open_connection(self.database)
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            row = conn.execute("SELECT email, display_name FROM users").fetchone()
            self.assertEqual(row["email"], "gm@example.com")
            self.assertEqual(row["display_name"], "Mestre")
        finally:
            conn.close()

    def test_existing_database_is_switched_to_wal_on_first_connection(self):
        CampaignStore(self.database)
        with closing(open_connection(self.database)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")

    def test_reader_is_not_blocked_while_writer_holds_the_database(self):
        """Este é o comportamento que o WAL existe para permitir.

        Em `journal_mode=DELETE` a leitura dentro deste cenário levantaria
        `database is locked`.
        """
        CampaignStore(self.database)
        writer = open_connection(self.database)
        reader = open_connection(self.database)
        writer.isolation_level = None
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO users (id, email, display_name, password_hash) "
                "VALUES ('u1', 'a@b.com', 'A', 'hash')"
            )
            count = reader.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            self.assertEqual(count, 0)  # o escritor ainda não comitou
            writer.execute("COMMIT")
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        finally:
            writer.close()
            reader.close()

    def test_wal_survives_reopening_the_store(self):
        CampaignStore(self.database).register("gm@example.com", "Mestre", "safe-password-123")
        reopened = CampaignStore(self.database)
        with closing(reopened._connect()) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
