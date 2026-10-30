"""Persistência local de campanhas; uma conexão SQLite por operação."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
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


TABLE_NAMES = (
    "Gabriel",
    "Leandro",
    "Yago",
    "Luiz",
    "Amom",
    "Victor",
    "Wayne",
    "Visitante",
)


class CampaignStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        with closing(self._connect()) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL, password_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS table_users (
                    name TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE REFERENCES users(id)
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
                    character_id TEXT,
                    character_data TEXT,
                    PRIMARY KEY (campaign_id, user_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS member_character ON members(campaign_id, character_id)
                    WHERE character_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS invites (
                    hash TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    expires_at INTEGER NOT NULL, used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS journal_entries (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    title TEXT NOT NULL, body TEXT NOT NULL,
                    visibility TEXT NOT NULL CHECK (visibility IN ('gm', 'party')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS journal_campaign_recent
                    ON journal_entries(campaign_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS campaign_sessions (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    session_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    scheduled_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('planned', 'active', 'completed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_campaign
                    ON campaign_sessions(campaign_id, session_number ASC);
                CREATE TABLE IF NOT EXISTS campaign_quests (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    reward TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'failed')),
                    visibility TEXT NOT NULL CHECK (visibility IN ('party', 'gm')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quests_campaign
                    ON campaign_quests(campaign_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS campaign_encounters (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    tactics TEXT NOT NULL DEFAULT '',
                    rewards TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('planned', 'ready', 'completed')),
                    monsters_data TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_encounters_campaign
                    ON campaign_encounters(campaign_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS campaign_npcs (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    name TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT '',
                    attitude TEXT NOT NULL DEFAULT 'neutral' CHECK (attitude IN ('friendly', 'neutral', 'hostile', 'unknown')),
                    description TEXT NOT NULL DEFAULT '',
                    gm_notes TEXT NOT NULL DEFAULT '',
                    combat_data TEXT,
                    visibility TEXT NOT NULL CHECK (visibility IN ('party', 'gm')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_npcs_campaign
                    ON campaign_npcs(campaign_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS npc_templates (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    level INTEGER NOT NULL DEFAULT 1,
                    template_data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_npc_templates_user
                    ON npc_templates(user_id, updated_at DESC);
            """)
            # Migration segura para character_data em bases SQLite já criadas
            cols = [row["name"] for row in conn.execute("PRAGMA table_info(members)").fetchall()]
            if "character_data" not in cols:
                conn.execute("ALTER TABLE members ADD COLUMN character_data TEXT")

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

    def table_login(self, name: str, pin: str) -> str:
        """Emite token para a lista fixa da mesa; o PIN público evita apenas erros casuais."""
        canonical = next(
            (entry for entry in TABLE_NAMES if entry.lower() == name.strip().lower()),
            None,
        )
        if not canonical or not secrets.compare_digest(pin, canonical.lower()):
            raise CampaignError(401, "Nome ou PIN inválidos.")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT user_id FROM table_users WHERE name = ?", (canonical,)
            ).fetchone()
            if row:
                return self._issue_token(conn, row["user_id"])
            user_id = str(uuid4())
            salt = secrets.token_bytes(16)
            conn.execute(
                "INSERT INTO users VALUES (?, ?, ?, ?)",
                (
                    user_id,
                    f"{user_id}@table.invalid",
                    canonical,
                    _password_hash(secrets.token_urlsafe(32), salt),
                ),
            )
            conn.execute("INSERT INTO table_users VALUES (?, ?)", (canonical, user_id))
            return self._issue_token(conn, user_id)

    def user_for_token(self, token: str) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT users.id, users.display_name FROM tokens
                JOIN users ON users.id = tokens.user_id
                WHERE tokens.hash = ? AND tokens.expires_at > ?""",
                (_digest(token), int(time.time())),
            ).fetchone()
            return dict(row) if row else None

    def revoke_token(self, user_id: str, token: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM tokens WHERE hash = ? AND user_id = ?",
                (_digest(token), user_id),
            )

    def combat_member(self, token: str, campaign_id: str) -> dict:
        user = self.user_for_token(token)
        if not user:
            raise CampaignError(401, "Sessão inválida.")
        with closing(self._connect()) as conn:
            member = self._member(conn, campaign_id, user["id"])
            if member["role"] == "player" and not member["character_id"]:
                raise CampaignError(
                    403, "Vincule sua ficha antes de entrar no combate."
                )
            return {
                "user_id": user["id"],
                "role": member["role"],
                "character_id": member["character_id"],
            }

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
            table_user = conn.execute(
                "SELECT name FROM table_users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if table_user and table_user["name"] != "Victor":
                raise CampaignError(403, "Apenas Victor pode criar campanhas da mesa.")
            conn.execute(
                "INSERT INTO campaigns VALUES (?, ?, ?)",
                (campaign_id, name.strip(), user_id),
            )
            conn.execute(
                "INSERT INTO members (campaign_id, user_id, role, character_id, character_data) VALUES (?, ?, 'gm', NULL, NULL)",
                (campaign_id, user_id),
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

    def assert_gm(self, user_id: str, campaign_id: str) -> dict:
        """Confirma que o usuário é o mestre da campanha; 403/404 como `_member`.

        Reutilizado pelas rotas de preparação para que a autorização não dependa
        de cada chamador lembrar de checar o papel.
        """
        with closing(self._connect()) as conn:
            member = self._member(conn, campaign_id, user_id)
            if member["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode acessar a preparação.")
            return dict(member)

    def get_campaign(self, user_id: str, campaign_id: str) -> dict:
        with closing(self._connect()) as conn:
            campaign = self._summary(self._member(conn, campaign_id, user_id))
            rows = conn.execute(
                """SELECT users.id, users.display_name, members.role, members.character_id, members.character_data
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
                    "hasSheet": bool(row["character_data"]) if row["character_data"] else False,
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
                "INSERT INTO members (campaign_id, user_id, role, character_id, character_data) VALUES (?, ?, 'player', NULL, NULL)",
                (invite["campaign_id"], user_id),
            )
            conn.execute(
                "UPDATE invites SET used_at = ? WHERE hash = ?",
                (int(time.time()), invite["hash"]),
            )
            return self._summary(self._member(conn, invite["campaign_id"], user_id))

    def link_character(
        self,
        user_id: str,
        campaign_id: str,
        character_id: str,
        character_data: dict | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            member = self._member(conn, campaign_id, user_id)
            if member["role"] != "player":
                raise CampaignError(403, "Somente jogadores vinculam personagens.")
            serialized = json.dumps(character_data) if character_data is not None else None
            try:
                conn.execute(
                    """UPDATE members
                    SET character_id = ?, character_data = COALESCE(?, character_data)
                    WHERE campaign_id = ? AND user_id = ?""",
                    (character_id.strip(), serialized, campaign_id, user_id),
                )
            except sqlite3.IntegrityError as exc:
                raise CampaignError(
                    409, "Personagem já vinculado nesta campanha."
                ) from exc
            return self._summary(self._member(conn, campaign_id, user_id))

    def unlink_character(self, user_id: str, campaign_id: str) -> dict:
        with closing(self._connect()) as conn, conn:
            member = self._member(conn, campaign_id, user_id)
            if member["role"] != "player":
                raise CampaignError(403, "Somente jogadores desvinculam personagens.")
            conn.execute(
                "UPDATE members SET character_id = NULL, character_data = NULL WHERE campaign_id = ? AND user_id = ?",
                (campaign_id, user_id),
            )
            return self._summary(self._member(conn, campaign_id, user_id))

    def sync_character(self, user_id: str, campaign_id: str, character_data: dict) -> dict:
        with closing(self._connect()) as conn, conn:
            member = self._member(conn, campaign_id, user_id)
            if member["role"] != "player":
                raise CampaignError(403, "Somente jogadores sincronizam fichas.")
            conn.execute(
                "UPDATE members SET character_data = ? WHERE campaign_id = ? AND user_id = ?",
                (json.dumps(character_data), campaign_id, user_id),
            )
            return self._summary(self._member(conn, campaign_id, user_id))

    def get_member_sheet(self, user_id: str, campaign_id: str, target_user_id: str) -> dict:
        with closing(self._connect()) as conn:
            member = self._member(conn, campaign_id, user_id)
            if user_id != target_user_id and member["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre ou o próprio jogador podem inspecionar a ficha.")
            target = conn.execute(
                "SELECT character_id, character_data FROM members WHERE campaign_id = ? AND user_id = ?",
                (campaign_id, target_user_id),
            ).fetchone()
            if not target:
                raise CampaignError(404, "Membro não encontrado.")
            raw = target["character_data"]
            parsed = json.loads(raw) if raw else None
            return {
                "characterId": target["character_id"],
                "characterData": parsed,
            }

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "title": row["title"],
            "body": row["body"],
            "visibility": row["visibility"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def create_entry(
        self, user_id: str, campaign_id: str, title: str, body: str, visibility: str
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode criar notas.")
            entry_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO journal_entries VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (entry_id, campaign_id, title, body, visibility, now, now),
            )
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            return self._entry(row)

    def list_entries(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            rows = conn.execute(
                """SELECT * FROM journal_entries
                WHERE campaign_id = ? AND (visibility = 'party' OR ? = 'gm')
                ORDER BY created_at DESC, id DESC LIMIT 100""",
                (campaign_id, role),
            ).fetchall()
            return [self._entry(row) for row in rows]

    def update_entry(
        self,
        user_id: str,
        campaign_id: str,
        entry_id: str,
        title: str,
        body: str,
        visibility: str,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar notas.")
            now = datetime.now(timezone.utc).isoformat()
            updated = conn.execute(
                """UPDATE journal_entries
                SET title = ?, body = ?, visibility = ?, updated_at = ?
                WHERE id = ? AND campaign_id = ?""",
                (title, body, visibility, now, entry_id, campaign_id),
            )
            if updated.rowcount == 0:
                raise CampaignError(404, "Nota não encontrada.")
            row = conn.execute(
                "SELECT * FROM journal_entries WHERE id = ?", (entry_id,)
            ).fetchone()
            return self._entry(row)

    def delete_entry(self, user_id: str, campaign_id: str, entry_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir notas.")
            deleted = conn.execute(
                "DELETE FROM journal_entries WHERE id = ? AND campaign_id = ?",
                (entry_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Nota não encontrada.")

    @staticmethod
    def _session(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "sessionNumber": row["session_number"],
            "title": row["title"],
            "summary": row["summary"],
            "scheduledAt": row["scheduled_at"],
            "status": row["status"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_sessions(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            self._member(conn, campaign_id, user_id)
            rows = conn.execute(
                """SELECT * FROM campaign_sessions
                WHERE campaign_id = ?
                ORDER BY session_number ASC, created_at ASC""",
                (campaign_id,),
            ).fetchall()
            return [self._session(row) for row in rows]

    def create_session(
        self,
        user_id: str,
        campaign_id: str,
        session_number: int,
        title: str,
        summary: str = "",
        scheduled_at: str | None = None,
        status: str = "planned",
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode gerenciar sessões.")
            session_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO campaign_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    campaign_id,
                    session_number,
                    title.strip(),
                    summary.strip(),
                    scheduled_at,
                    status,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM campaign_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._session(row)

    def update_session(
        self,
        user_id: str,
        campaign_id: str,
        session_id: str,
        session_number: int,
        title: str,
        summary: str = "",
        scheduled_at: str | None = None,
        status: str = "planned",
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar sessões.")
            now = datetime.now(timezone.utc).isoformat()
            updated = conn.execute(
                """UPDATE campaign_sessions
                SET session_number = ?, title = ?, summary = ?, scheduled_at = ?, status = ?, updated_at = ?
                WHERE id = ? AND campaign_id = ?""",
                (
                    session_number,
                    title.strip(),
                    summary.strip(),
                    scheduled_at,
                    status,
                    now,
                    session_id,
                    campaign_id,
                ),
            )
            if updated.rowcount == 0:
                raise CampaignError(404, "Sessão não encontrada.")
            row = conn.execute(
                "SELECT * FROM campaign_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return self._session(row)

    def delete_session(self, user_id: str, campaign_id: str, session_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir sessões.")
            deleted = conn.execute(
                "DELETE FROM campaign_sessions WHERE id = ? AND campaign_id = ?",
                (session_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Sessão não encontrada.")

    @staticmethod
    def _quest(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "title": row["title"],
            "description": row["description"],
            "reward": row["reward"],
            "status": row["status"],
            "visibility": row["visibility"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_quests(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            rows = conn.execute(
                """SELECT * FROM campaign_quests
                WHERE campaign_id = ? AND (visibility = 'party' OR ? = 'gm')
                ORDER BY created_at DESC, id DESC""",
                (campaign_id, role),
            ).fetchall()
            return [self._quest(row) for row in rows]

    def create_quest(
        self,
        user_id: str,
        campaign_id: str,
        title: str,
        description: str = "",
        reward: str = "",
        visibility: str = "party",
        status: str = "active",
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode gerenciar missões.")
            quest_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO campaign_quests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    quest_id,
                    campaign_id,
                    title.strip(),
                    description.strip(),
                    reward.strip(),
                    status,
                    visibility,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM campaign_quests WHERE id = ?", (quest_id,)
            ).fetchone()
            return self._quest(row)

    def update_quest(
        self,
        user_id: str,
        campaign_id: str,
        quest_id: str,
        title: str,
        description: str = "",
        reward: str = "",
        visibility: str = "party",
        status: str = "active",
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar missões.")
            now = datetime.now(timezone.utc).isoformat()
            updated = conn.execute(
                """UPDATE campaign_quests
                SET title = ?, description = ?, reward = ?, visibility = ?, status = ?, updated_at = ?
                WHERE id = ? AND campaign_id = ?""",
                (
                    title.strip(),
                    description.strip(),
                    reward.strip(),
                    visibility,
                    status,
                    now,
                    quest_id,
                    campaign_id,
                ),
            )
            if updated.rowcount == 0:
                raise CampaignError(404, "Missão não encontrada.")
            row = conn.execute(
                "SELECT * FROM campaign_quests WHERE id = ?", (quest_id,)
            ).fetchone()
            return self._quest(row)

    def delete_quest(self, user_id: str, campaign_id: str, quest_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir missões.")
            deleted = conn.execute(
                "DELETE FROM campaign_quests WHERE id = ? AND campaign_id = ?",
                (quest_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Missão não encontrada.")

    # --- ENCOUNTERS ---
    @staticmethod
    def _encounter(row: sqlite3.Row) -> dict:
        monsters = []
        if row["monsters_data"]:
            try:
                monsters = json.loads(row["monsters_data"])
            except Exception:
                monsters = []
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "name": row["name"],
            "description": row["description"],
            "tactics": row["tactics"],
            "rewards": row["rewards"],
            "status": row["status"],
            "monstersData": monsters,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_encounters(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode acessar encontros.")
            rows = conn.execute(
                """SELECT * FROM campaign_encounters
                WHERE campaign_id = ?
                ORDER BY created_at DESC, id DESC""",
                (campaign_id,),
            ).fetchall()
            return [self._encounter(row) for row in rows]

    def get_encounter(self, user_id: str, campaign_id: str, encounter_id: str) -> dict:
        with closing(self._connect()) as conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode acessar encontros.")
            row = conn.execute(
                "SELECT * FROM campaign_encounters WHERE id = ? AND campaign_id = ?",
                (encounter_id, campaign_id),
            ).fetchone()
            if not row:
                raise CampaignError(404, "Encontro não encontrado.")
            return self._encounter(row)

    def create_encounter(
        self,
        user_id: str,
        campaign_id: str,
        name: str,
        description: str = "",
        tactics: str = "",
        rewards: str = "",
        status: str = "planned",
        monsters_data: list | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode criar encontros.")
            encounter_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            monsters_json = json.dumps(monsters_data or [])
            conn.execute(
                """INSERT INTO campaign_encounters VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    encounter_id,
                    campaign_id,
                    name.strip(),
                    description.strip(),
                    tactics.strip(),
                    rewards.strip(),
                    status,
                    monsters_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM campaign_encounters WHERE id = ?", (encounter_id,)
            ).fetchone()
            return self._encounter(row)

    def update_encounter(
        self,
        user_id: str,
        campaign_id: str,
        encounter_id: str,
        name: str,
        description: str = "",
        tactics: str = "",
        rewards: str = "",
        status: str = "planned",
        monsters_data: list | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar encontros.")
            now = datetime.now(timezone.utc).isoformat()
            monsters_json = json.dumps(monsters_data or [])
            updated = conn.execute(
                """UPDATE campaign_encounters
                SET name = ?, description = ?, tactics = ?, rewards = ?, status = ?, monsters_data = ?, updated_at = ?
                WHERE id = ? AND campaign_id = ?""",
                (
                    name.strip(),
                    description.strip(),
                    tactics.strip(),
                    rewards.strip(),
                    status,
                    monsters_json,
                    now,
                    encounter_id,
                    campaign_id,
                ),
            )
            if updated.rowcount == 0:
                raise CampaignError(404, "Encontro não encontrado.")
            row = conn.execute(
                "SELECT * FROM campaign_encounters WHERE id = ?", (encounter_id,)
            ).fetchone()
            return self._encounter(row)

    def delete_encounter(self, user_id: str, campaign_id: str, encounter_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir encontros.")
            deleted = conn.execute(
                "DELETE FROM campaign_encounters WHERE id = ? AND campaign_id = ?",
                (encounter_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Encontro não encontrado.")

    # --- NPCS ---
    @staticmethod
    def _npc(row: sqlite3.Row, is_gm: bool) -> dict:
        combat = None
        if row["combat_data"]:
            try:
                combat = json.loads(row["combat_data"])
            except Exception:
                combat = None
        data = {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "name": row["name"],
            "role": row["role"],
            "attitude": row["attitude"],
            "description": row["description"],
            "combatData": combat,
            "visibility": row["visibility"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        if is_gm:
            data["gmNotes"] = row["gm_notes"]
        return data

    def list_npcs(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            is_gm = role == "gm"
            if is_gm:
                rows = conn.execute(
                    """SELECT * FROM campaign_npcs
                    WHERE campaign_id = ?
                    ORDER BY created_at DESC, id DESC""",
                    (campaign_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM campaign_npcs
                    WHERE campaign_id = ? AND visibility = 'party'
                    ORDER BY created_at DESC, id DESC""",
                    (campaign_id,),
                ).fetchall()
            return [self._npc(row, is_gm) for row in rows]

    def create_npc(
        self,
        user_id: str,
        campaign_id: str,
        name: str,
        role: str = "",
        attitude: str = "neutral",
        description: str = "",
        gm_notes: str = "",
        combat_data: dict | None = None,
        visibility: str = "party",
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode criar NPCs.")
            npc_id = str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            combat_json = json.dumps(combat_data) if combat_data else None
            conn.execute(
                """INSERT INTO campaign_npcs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    npc_id,
                    campaign_id,
                    name.strip(),
                    role.strip(),
                    attitude,
                    description.strip(),
                    gm_notes.strip(),
                    combat_json,
                    visibility,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM campaign_npcs WHERE id = ?", (npc_id,)
            ).fetchone()
            return self._npc(row, is_gm=True)

    def update_npc(
        self,
        user_id: str,
        campaign_id: str,
        npc_id: str,
        name: str,
        role: str = "",
        attitude: str = "neutral",
        description: str = "",
        gm_notes: str = "",
        combat_data: dict | None = None,
        visibility: str = "party",
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar NPCs.")
            now = datetime.now(timezone.utc).isoformat()
            combat_json = json.dumps(combat_data) if combat_data else None
            updated = conn.execute(
                """UPDATE campaign_npcs
                SET name = ?, role = ?, attitude = ?, description = ?, gm_notes = ?,
                    combat_data = ?, visibility = ?, updated_at = ?
                WHERE id = ? AND campaign_id = ?""",
                (
                    name.strip(),
                    role.strip(),
                    attitude,
                    description.strip(),
                    gm_notes.strip(),
                    combat_json,
                    visibility,
                    now,
                    npc_id,
                    campaign_id,
                ),
            )
            if updated.rowcount == 0:
                raise CampaignError(404, "NPC não encontrado.")
            row = conn.execute(
                "SELECT * FROM campaign_npcs WHERE id = ?", (npc_id,)
            ).fetchone()
            return self._npc(row, is_gm=True)

    def delete_npc(self, user_id: str, campaign_id: str, npc_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir NPCs.")
            deleted = conn.execute(
                "DELETE FROM campaign_npcs WHERE id = ? AND campaign_id = ?",
                (npc_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "NPC não encontrado.")

    @staticmethod
    def _npc_template(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "userId": row["user_id"],
            "name": row["name"],
            "level": row["level"],
            "templateData": json.loads(row["template_data"]) if row["template_data"] else {},
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_npc_templates(self, user_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM npc_templates WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            return [self._npc_template(row) for row in rows]

    def create_npc_template(
        self,
        user_id: str,
        name: str,
        level: int = 1,
        template_data: dict | None = None,
        template_id: str | None = None,
        updated_at: str | None = None,
    ) -> dict:
        tid = template_id or str(uuid4())
        now = updated_at or datetime.now(timezone.utc).isoformat()
        clean_name = name.strip()
        if not clean_name:
            raise CampaignError(400, "Nome do template é obrigatório.")
        tdata_json = json.dumps(template_data or {})
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO npc_templates (id, user_id, name, level, template_data, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (tid, user_id, clean_name, int(level), tdata_json, now, now),
            )
            row = conn.execute("SELECT * FROM npc_templates WHERE id = ?", (tid,)).fetchone()
            return self._npc_template(row)

    def update_npc_template(
        self,
        user_id: str,
        template_id: str,
        name: str,
        level: int = 1,
        template_data: dict | None = None,
        updated_at: str | None = None,
    ) -> dict:
        now = updated_at or datetime.now(timezone.utc).isoformat()
        clean_name = name.strip()
        if not clean_name:
            raise CampaignError(400, "Nome do template é obrigatório.")
        tdata_json = json.dumps(template_data or {})
        with closing(self._connect()) as conn, conn:
            updated = conn.execute(
                """UPDATE npc_templates
                SET name = ?, level = ?, template_data = ?, updated_at = ?
                WHERE id = ? AND user_id = ?""",
                (clean_name, int(level), tdata_json, now, template_id, user_id),
            )
            if updated.rowcount == 0:
                raise CampaignError(404, "Template não encontrado.")
            row = conn.execute("SELECT * FROM npc_templates WHERE id = ?", (template_id,)).fetchone()
            return self._npc_template(row)

    def delete_npc_template(self, user_id: str, template_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            deleted = conn.execute(
                "DELETE FROM npc_templates WHERE id = ? AND user_id = ?",
                (template_id, user_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Template não encontrado.")

    def sync_npc_templates(self, user_id: str, local_templates: list[dict]) -> list[dict]:
        with closing(self._connect()) as conn, conn:
            existing = {
                row["id"]: row
                for row in conn.execute(
                    "SELECT * FROM npc_templates WHERE user_id = ?", (user_id,)
                ).fetchall()
            }

            now = datetime.now(timezone.utc).isoformat()

            for item in local_templates:
                tid = item.get("id") or str(uuid4())
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                level = int(item.get("level") or 1)
                tdata = item.get("templateData") or item.get("template_data") or item
                tdata_json = json.dumps(tdata)
                local_updated = item.get("updatedAt") or item.get("updated_at") or now

                if tid in existing:
                    server_updated = existing[tid]["updated_at"]
                    if local_updated > server_updated:
                        conn.execute(
                            """UPDATE npc_templates
                            SET name = ?, level = ?, template_data = ?, updated_at = ?
                            WHERE id = ? AND user_id = ?""",
                            (name, level, tdata_json, local_updated, tid, user_id),
                        )
                else:
                    conn.execute(
                        """INSERT INTO npc_templates (id, user_id, name, level, template_data, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (tid, user_id, name, level, tdata_json, local_updated, local_updated),
                    )

            rows = conn.execute(
                "SELECT * FROM npc_templates WHERE user_id = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            return [self._npc_template(row) for row in rows]


