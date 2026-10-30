"""Ponto único de configuração de conexões SQLite.

Existe para que nenhum store escolha pragmas por conta própria. Duas decisões
importam e ambas valem para o arquivo inteiro, não só para a conexão:

- `journal_mode = WAL`: leitores passam a coexistir com escritores. Em
  `journal_mode = DELETE` (padrão), qualquer escrita bloqueia leituras. O
  `timeout` do `sqlite3` apenas **espera** o lock; não torna as operações
  concorrentes.
- `foreign_keys = ON`: ao contrário do WAL, isto é por conexão, então precisa ser
  aplicado em toda conexão nova — é exatamente o que este helper garante.

WAL é persistente no arquivo: a primeira conexão que o ativa deixa o banco nesse
modo. Consequência operacional: o backup por cópia simples do
`pharos.sqlite3` passa a ignorar transações que estão em `pharos.sqlite3-wal`.
O procedimento correto está em `docs/DEPLOYMENT_DOCKER.md` (repo do frontend).
"""

import sqlite3
from pathlib import Path

LOCK_WAIT_SECONDS = 5


def open_connection(path: str | Path) -> sqlite3.Connection:
    """Abre uma conexão pronta para uso: WAL, chaves estrangeiras e linhas nomeadas.

    >>> conn = open_connection(":memory:")
    >>> conn.execute("PRAGMA foreign_keys").fetchone()[0]
    1
    >>> conn.close()
    """
    connection = sqlite3.connect(path, timeout=LOCK_WAIT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
