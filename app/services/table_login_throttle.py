"""Cotas atômicas e compartilhadas para o login público da mesa."""

import sqlite3
import time
from collections.abc import Callable
from hashlib import sha256

from app.core.database import write_transaction

WINDOW_SECONDS = 60
NAME_ATTEMPTS = 10
PEER_ATTEMPTS = 30


class TableLoginThrottled(Exception):
    """A API converte esta recusa em 429 e expõe `retry_after` em segundos."""

    def __init__(self, retry_after: int):
        super().__init__("Muitas tentativas de login. Aguarde e tente novamente.")
        self.retry_after = retry_after


class TableLoginThrottle:
    """Reserva uma tentativa antes do PBKDF2: `throttle.consume(name, peer)`."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock

    def consume(self, name: str, peer: str) -> None:
        now = int(self.clock())
        buckets = (
            (self._bucket("name", name.strip().lower()), NAME_ATTEMPTS),
            (self._bucket("peer", peer), PEER_ATTEMPTS),
        )
        # Sem criptografia de PIN nem I/O de rede sob este lock. A reserva das duas
        # cotas deve ser indivisível, inclusive entre processos distintos do app.
        with write_transaction(self.path) as conn:
            conn.execute(
                "DELETE FROM table_login_attempts WHERE expires_at <= ?", (now,)
            )
            for bucket, limit in buckets:
                self._reserve(conn, bucket, limit, now)

    @staticmethod
    def _bucket(scope: str, value: str) -> str:
        # O banco de cotas não precisa reter o nome digitado nem o endereço em claro.
        return f"{scope}:{sha256(value.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _reserve(conn: sqlite3.Connection, bucket: str, limit: int, now: int) -> None:
        current = conn.execute(
            "SELECT attempts, expires_at FROM table_login_attempts WHERE bucket = ?",
            (bucket,),
        ).fetchone()
        if current and current["attempts"] >= limit:
            raise TableLoginThrottled(max(1, current["expires_at"] - now))
        conn.execute(
            """INSERT INTO table_login_attempts (bucket, attempts, expires_at) VALUES (?, 1, ?)
               ON CONFLICT(bucket) DO UPDATE SET attempts = attempts + 1""",
            (bucket, now + WINDOW_SECONDS),
        )
