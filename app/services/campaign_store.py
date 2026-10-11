"""Persistência local de campanhas; uma conexão SQLite por operação."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import uuid4


class CampaignError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_hash(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260_000)
    return f"{salt.hex()}:{digest.hex()}"


class CampaignStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, gm_id TEXT NOT NULL REFERENCES users(id)
                );
                CREATE TABLE IF NOT EXISTS members (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    user_id TEXT NOT NULL REFERENCES users(id),
                    role TEXT NOT NULL CHECK (role IN ('gm', 'player')),
                    character_id TEXT, PRIMARY KEY (campaign_id, user_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS member_character ON members(campaign_id, character_id)
                    WHERE character_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS invites (
                    hash TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    expires_at INTEGER NOT NULL, used_at INTEGER
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _issue_token(self, conn: sqlite3.Connection, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO tokens VALUES (?, ?, ?)",
            (_digest(token), user_id, int(time.time()) + 7 * 86400),
        )
        return token

    def register(self, email: str, name: str, password: str) -> str:
        salt = secrets.token_bytes(16)
        with closing(self._connect()) as conn, conn:
            try:
                user_id = str(uuid4())
                conn.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?)",
                    (
                        user_id,
                        email.lower().strip(),
                        name.strip(),
                        _password_hash(password, salt),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CampaignError(409, "Conta já cadastrada.") from exc
            return self._issue_token(conn, user_id)

    def login(self, email: str, password: str) -> str:
        with closing(self._connect()) as conn, conn:
            user = conn.execute(
                "SELECT id, password_hash FROM users WHERE email = ?",
                (email.lower().strip(),),
            ).fetchone()
            # Mesmo custo aproximado para email inexistente; não revela a existência da conta.
            saved = (
                user["password_hash"] if user else _password_hash("invalid", bytes(16))
            )
            salt, _ = saved.split(":")
            if not user or not secrets.compare_digest(
                saved, _password_hash(password, bytes.fromhex(salt))
            ):
                raise CampaignError(401, "Credenciais inválidas.")
            return self._issue_token(conn, user["id"])

    def user_for_token(self, token: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT users.id, users.display_name FROM tokens
                JOIN users ON users.id = tokens.user_id
                WHERE tokens.hash = ? AND tokens.expires_at > ?""",
                (_digest(token), int(time.time())),
            ).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "role": row["role"],
            "characterId": row["character_id"],
        }

    def _member(
        self, conn: sqlite3.Connection, campaign_id: str, user_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """SELECT campaigns.id, campaigns.name, members.role, members.character_id
            FROM campaigns JOIN members ON campaigns.id = members.campaign_id
            WHERE campaigns.id = ? AND members.user_id = ?""",
            (campaign_id, user_id),
        ).fetchone()
        if not row:
            exists = conn.execute(
                "SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,)
            ).fetchone()
            raise CampaignError(403 if exists else 404, "Campanha indisponível.")
        return row

    def create_campaign(self, user_id: str, name: str) -> dict:
        campaign_id = str(uuid4())
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO campaigns VALUES (?, ?, ?)",
                (campaign_id, name.strip(), user_id),
            )
            conn.execute(
                "INSERT INTO members VALUES (?, ?, 'gm', NULL)", (campaign_id, user_id)
            )
        return {
            "id": campaign_id,
            "name": name.strip(),
            "role": "gm",
            "characterId": None,
        }

    def list_campaigns(self, user_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT campaigns.id, campaigns.name, members.role, members.character_id
                FROM campaigns JOIN members ON campaigns.id = members.campaign_id
                WHERE members.user_id = ? ORDER BY campaigns.name, campaigns.id""",
                (user_id,),
            ).fetchall()
            return [self._summary(row) for row in rows]

    def get_campaign(self, user_id: str, campaign_id: str) -> dict:
        with closing(self._connect()) as conn:
            campaign = self._summary(self._member(conn, campaign_id, user_id))
            rows = conn.execute(
                """SELECT users.id, users.display_name, members.role, members.character_id
                FROM members JOIN users ON users.id = members.user_id
                WHERE members.campaign_id = ? ORDER BY members.role, users.display_name""",
                (campaign_id,),
            ).fetchall()
            campaign["members"] = [
                {
                    "id": row["id"],
                    "name": row["display_name"],
                    "role": row["role"],
                    "characterId": row["character_id"],
                }
                for row in rows
            ]
            return campaign

    def create_invite(self, user_id: str, campaign_id: str) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode gerar convites.")
            code = secrets.token_urlsafe(24)
            expires_at = int(time.time()) + 86400
            conn.execute(
                "INSERT INTO invites VALUES (?, ?, ?, NULL)",
                (_digest(code), campaign_id, expires_at),
            )
            return {
                "code": code,
                "expiresAt": datetime.fromtimestamp(
                    expires_at, timezone.utc
                ).isoformat(),
            }

    def join_campaign(self, user_id: str, code: str) -> dict:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            invite = conn.execute(
                "SELECT * FROM invites WHERE hash = ?", (_digest(code),)
            ).fetchone()
            if not invite:
                raise CampaignError(404, "Convite inválido.")
            if (
                invite["expires_at"] <= int(time.time())
                or invite["used_at"] is not None
            ):
                raise CampaignError(410, "Convite expirado ou utilizado.")
            exists = conn.execute(
                "SELECT 1 FROM members WHERE campaign_id = ? AND user_id = ?",
                (invite["campaign_id"], user_id),
            ).fetchone()
            if exists:
                raise CampaignError(409, "Você já participa da campanha.")
            conn.execute(
                "INSERT INTO members VALUES (?, ?, 'player', NULL)",
                (invite["campaign_id"], user_id),
            )
            conn.execute(
                "UPDATE invites SET used_at = ? WHERE hash = ?",
                (int(time.time()), invite["hash"]),
            )
            return self._summary(self._member(conn, invite["campaign_id"], user_id))

    def link_character(self, user_id: str, campaign_id: str, character_id: str) -> dict:
        with closing(self._connect()) as conn, conn:
            member = self._member(conn, campaign_id, user_id)
            if member["role"] != "player":
                raise CampaignError(403, "Somente jogadores vinculam personagens.")
            try:
                conn.execute(
                    "UPDATE members SET character_id = ? WHERE campaign_id = ? AND user_id = ?",
                    (character_id.strip(), campaign_id, user_id),
                )
            except sqlite3.IntegrityError as exc:
                raise CampaignError(
                    409, "Personagem já vinculado nesta campanha."
                ) from exc
            return self._summary(self._member(conn, campaign_id, user_id))
