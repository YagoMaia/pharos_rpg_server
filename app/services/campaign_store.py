"""Persistência local de campanhas; uma conexão SQLite por operação."""

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.database import open_connection, write_transaction
from app.services import campaign_records as records
from app.services import compendium_records

# Reexportado: `from app.services.campaign_store import CampaignError` já é o
# caminho usado por rotas, stores de preparação e testes.
from app.services.errors import CampaignError


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
    "Estevão",
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
                CREATE TABLE IF NOT EXISTS campaign_locations (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    parent_id TEXT REFERENCES campaign_locations(id) ON DELETE SET NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('region', 'location')),
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    gm_notes TEXT NOT NULL DEFAULT '',
                    image TEXT,
                    visibility TEXT NOT NULL CHECK (visibility IN ('party', 'gm', 'selected')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_locations_campaign
                    ON campaign_locations(campaign_id, kind, name ASC);
                CREATE TABLE IF NOT EXISTS campaign_location_visibility (
                    location_id TEXT NOT NULL REFERENCES campaign_locations(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    PRIMARY KEY (location_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS campaign_sessions (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    session_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    scheduled_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('planned', 'active', 'completed')),
                    location_id TEXT REFERENCES campaign_locations(id) ON DELETE SET NULL,
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
                    visibility TEXT NOT NULL CHECK (visibility IN ('party', 'gm', 'selected')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_npcs_campaign
                    ON campaign_npcs(campaign_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS campaign_npc_bestiary_links (
                    npc_id TEXT PRIMARY KEY REFERENCES campaign_npcs(id) ON DELETE CASCADE,
                    template_id TEXT NOT NULL
                );
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
                CREATE TABLE IF NOT EXISTS campaign_compendium_entries (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    slug TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL CHECK (kind IN ('fauna', 'flora')),
                    name TEXT NOT NULL,
                    subtitle TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT '',
                    subtype TEXT NOT NULL DEFAULT '',
                    danger_label TEXT NOT NULL DEFAULT '',
                    material_label TEXT NOT NULL DEFAULT '',
                    lore TEXT NOT NULL DEFAULT '',
                    image TEXT,
                    visibility TEXT NOT NULL DEFAULT 'gm' CHECK (visibility IN ('gm', 'party', 'selected')),
                    visible_to_member_ids_json TEXT NOT NULL DEFAULT '[]',
                    combat_template_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_compendium_campaign
                    ON campaign_compendium_entries(campaign_id, kind, updated_at DESC);
                CREATE TABLE IF NOT EXISTS campaign_compendium_notes (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    entry_id TEXT NOT NULL REFERENCES campaign_compendium_entries(id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    note TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (campaign_id, entry_id, user_id)
                );
            """)
            # Migration segura para character_data em bases SQLite já criadas
            cols = [row["name"] for row in conn.execute("PRAGMA table_info(members)").fetchall()]
            if "character_data" not in cols:
                conn.execute("ALTER TABLE members ADD COLUMN character_data TEXT")
        self._migrate_npc_visibility()
        self._migrate_locations()

    def _migrate_npc_visibility(self) -> None:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            # Mantém foreign_keys OFF durante eventual rebuild para não disparar
            # ON DELETE CASCADE sobre campaign_npc_bestiary_links.
            conn.execute("PRAGMA foreign_keys = OFF")
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'campaign_npcs'"
            ).fetchone()
            if ddl and ddl["sql"] and "'selected'" not in ddl["sql"]:
                with conn:
                    conn.executescript("""
                        CREATE TABLE campaign_npcs_new (
                            id TEXT PRIMARY KEY,
                            campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                            name TEXT NOT NULL,
                            role TEXT NOT NULL DEFAULT '',
                            attitude TEXT NOT NULL DEFAULT 'neutral' CHECK (attitude IN ('friendly', 'neutral', 'hostile', 'unknown')),
                            description TEXT NOT NULL DEFAULT '',
                            gm_notes TEXT NOT NULL DEFAULT '',
                            combat_data TEXT,
                            visibility TEXT NOT NULL CHECK (visibility IN ('party', 'gm', 'selected')),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        );
                        INSERT INTO campaign_npcs_new
                        SELECT id, campaign_id, name, role, attitude, description, gm_notes, combat_data, visibility, created_at, updated_at
                        FROM campaign_npcs;
                        DROP TABLE campaign_npcs;
                        ALTER TABLE campaign_npcs_new RENAME TO campaign_npcs;
                        CREATE INDEX IF NOT EXISTS idx_npcs_campaign
                            ON campaign_npcs(campaign_id, created_at DESC);
                    """)
            with conn:
                conn.executescript("""
                    CREATE TABLE IF NOT EXISTS campaign_npc_visibility (
                        npc_id TEXT NOT NULL REFERENCES campaign_npcs(id) ON DELETE CASCADE,
                        user_id TEXT NOT NULL REFERENCES users(id),
                        PRIMARY KEY (npc_id, user_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_npc_visibility_user
                        ON campaign_npc_visibility(user_id, npc_id);
                """)

    def _migrate_locations(self) -> None:
        with closing(self._connect()) as conn, conn:
            for table in (
                "campaign_sessions",
                "campaign_quests",
                "campaign_encounters",
                "campaign_npcs",
            ):
                cols = {
                    row["name"]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if cols and "location_id" not in cols:
                    conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN location_id TEXT REFERENCES campaign_locations(id) ON DELETE SET NULL"
                    )

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path)

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
        if not canonical or not secrets.compare_digest(
            pin.encode("utf-8"), canonical.lower().encode("utf-8")
        ):
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
                    expires_at, UTC
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
            now = datetime.now(UTC).isoformat()
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
            now = datetime.now(UTC).isoformat()
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
    def _session(
        row: sqlite3.Row,
        is_gm: bool = True,
        visible_location_ids: set[str] | None = None,
    ) -> dict:
        location_id = dict(row).get("location_id")
        if (
            not is_gm
            and location_id
            and visible_location_ids is not None
            and location_id not in visible_location_ids
        ):
            location_id = None
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "sessionNumber": row["session_number"],
            "title": row["title"],
            "summary": row["summary"],
            "scheduledAt": row["scheduled_at"],
            "status": row["status"],
            "locationId": location_id,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_sessions(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            is_gm = role == "gm"
            rows = conn.execute(
                """SELECT * FROM campaign_sessions
                WHERE campaign_id = ?
                ORDER BY session_number ASC, created_at ASC""",
                (campaign_id,),
            ).fetchall()
            visible_locs = (
                None if is_gm else self._visible_location_ids(conn, campaign_id, user_id)
            )
            return [
                self._session(row, is_gm=is_gm, visible_location_ids=visible_locs)
                for row in rows
            ]

    def create_session(
        self,
        user_id: str,
        campaign_id: str,
        session_number: int,
        title: str,
        summary: str = "",
        scheduled_at: str | None = None,
        status: str = "planned",
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode gerenciar sessões.")
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.insert_session(
                conn,
                campaign_id,
                {
                    "session_number": session_number,
                    "title": title.strip(),
                    "summary": summary.strip(),
                    "scheduled_at": scheduled_at,
                    "status": status,
                },
            )
            row = self._set_record_location(
                conn, "campaign_sessions", campaign_id, row["id"], resolved_location
            )
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
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar sessões.")
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.update_session(
                conn,
                campaign_id,
                session_id,
                {
                    "session_number": session_number,
                    "title": title.strip(),
                    "summary": summary.strip(),
                    "scheduled_at": scheduled_at,
                    "status": status,
                },
            )
            row = self._set_record_location(
                conn, "campaign_sessions", campaign_id, row["id"], resolved_location
            )
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
    def _quest(
        row: sqlite3.Row,
        is_gm: bool = True,
        visible_location_ids: set[str] | None = None,
    ) -> dict:
        location_id = dict(row).get("location_id")
        if (
            not is_gm
            and location_id
            and visible_location_ids is not None
            and location_id not in visible_location_ids
        ):
            location_id = None
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "title": row["title"],
            "description": row["description"],
            "reward": row["reward"],
            "status": row["status"],
            "visibility": row["visibility"],
            "locationId": location_id,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def list_quests(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            is_gm = role == "gm"
            rows = conn.execute(
                """SELECT * FROM campaign_quests
                WHERE campaign_id = ? AND (visibility = 'party' OR ? = 'gm')
                ORDER BY created_at DESC, id DESC""",
                (campaign_id, role),
            ).fetchall()
            visible_locs = (
                None if is_gm else self._visible_location_ids(conn, campaign_id, user_id)
            )
            return [
                self._quest(row, is_gm=is_gm, visible_location_ids=visible_locs)
                for row in rows
            ]

    def create_quest(
        self,
        user_id: str,
        campaign_id: str,
        title: str,
        description: str = "",
        reward: str = "",
        visibility: str = "party",
        status: str = "active",
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode gerenciar missões.")
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.insert_quest(
                conn,
                campaign_id,
                {
                    "title": title.strip(),
                    "description": description.strip(),
                    "reward": reward.strip(),
                    "status": status,
                    "visibility": visibility,
                },
            )
            row = self._set_record_location(
                conn, "campaign_quests", campaign_id, row["id"], resolved_location
            )
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
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar missões.")
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.update_quest(
                conn,
                campaign_id,
                quest_id,
                {
                    "title": title.strip(),
                    "description": description.strip(),
                    "reward": reward.strip(),
                    "visibility": visibility,
                    "status": status,
                },
            )
            row = self._set_record_location(
                conn, "campaign_quests", campaign_id, row["id"], resolved_location
            )
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
            "locationId": dict(row).get("location_id"),
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
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode criar encontros.")
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.insert_encounter(
                conn,
                campaign_id,
                {
                    "name": name.strip(),
                    "description": description.strip(),
                    "tactics": tactics.strip(),
                    "rewards": rewards.strip(),
                    "status": status,
                    "monsters_data": monsters_data,
                },
            )
            row = self._set_record_location(
                conn, "campaign_encounters", campaign_id, row["id"], resolved_location
            )
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
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar encontros.")
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.update_encounter(
                conn,
                campaign_id,
                encounter_id,
                {
                    "name": name.strip(),
                    "description": description.strip(),
                    "tactics": tactics.strip(),
                    "rewards": rewards.strip(),
                    "status": status,
                    "monsters_data": monsters_data,
                },
            )
            row = self._set_record_location(
                conn, "campaign_encounters", campaign_id, row["id"], resolved_location
            )
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
    def _npc(
        row: sqlite3.Row,
        is_gm: bool,
        visible_to_member_ids: list[str] | None = None,
        visible_location_ids: set[str] | None = None,
    ) -> dict:
        combat = None
        if row["combat_data"]:
            try:
                combat = json.loads(row["combat_data"])
            except (json.JSONDecodeError, TypeError):
                combat = None
        location_id = dict(row).get("location_id")
        if not is_gm and location_id and visible_location_ids is not None and location_id not in visible_location_ids:
            location_id = None
        data = {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "name": row["name"],
            "role": row["role"],
            "attitude": row["attitude"],
            "description": row["description"],
            "combatData": combat,
            "visibility": row["visibility"],
            "locationId": location_id,
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        if is_gm:
            data["gmNotes"] = row["gm_notes"]
            data["visibleToMemberIds"] = (
                list(visible_to_member_ids or [])
                if row["visibility"] == "selected"
                else []
            )
        return data

    @staticmethod
    def _resolve_npc_targets(
        conn: sqlite3.Connection,
        campaign_id: str,
        visibility: str,
        visible_to_member_ids: list[str] | None,
    ) -> list[str]:
        if visibility != "selected":
            return []
        unique_ids = list(
            dict.fromkeys(
                mid.strip()
                for mid in (visible_to_member_ids or [])
                if isinstance(mid, str) and mid.strip()
            )
        )
        if not unique_ids:
            raise CampaignError(
                422,
                "Selecione ao menos um jogador para visibilidade específica.",
            )
        player_rows = conn.execute(
            "SELECT user_id FROM members WHERE campaign_id = ? AND role = 'player'",
            (campaign_id,),
        ).fetchall()
        valid_players = {row["user_id"] for row in player_rows}
        if any(mid not in valid_players for mid in unique_ids):
            raise CampaignError(
                422,
                "Os destinatários do NPC devem ser jogadores membros da campanha.",
            )
        return unique_ids

    @staticmethod
    def _save_npc_visibility(
        conn: sqlite3.Connection,
        npc_id: str,
        member_ids: list[str],
    ) -> None:
        conn.execute("DELETE FROM campaign_npc_visibility WHERE npc_id = ?", (npc_id,))
        for member_id in member_ids:
            conn.execute(
                "INSERT INTO campaign_npc_visibility (npc_id, user_id) VALUES (?, ?)",
                (npc_id, member_id),
            )

    @staticmethod
    def _npc_visible_map(
        conn: sqlite3.Connection,
        npc_ids: list[str],
    ) -> dict[str, list[str]]:
        if not npc_ids:
            return {}
        placeholders = ", ".join("?" for _ in npc_ids)
        rows = conn.execute(
            f"SELECT npc_id, user_id FROM campaign_npc_visibility WHERE npc_id IN ({placeholders}) ORDER BY user_id",
            tuple(npc_ids),
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["npc_id"], []).append(row["user_id"])
        return result

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
                vis_map = self._npc_visible_map(conn, [row["id"] for row in rows])
                return [self._npc(row, True, vis_map.get(row["id"])) for row in rows]
            rows = conn.execute(
                """SELECT DISTINCT n.* FROM campaign_npcs n
                LEFT JOIN campaign_npc_visibility v
                  ON v.npc_id = n.id AND v.user_id = ?
                WHERE n.campaign_id = ?
                  AND (n.visibility = 'party' OR (n.visibility = 'selected' AND v.user_id IS NOT NULL))
                ORDER BY n.created_at DESC, n.id DESC""",
                (user_id, campaign_id),
            ).fetchall()
            visible_locs = self._visible_location_ids(conn, campaign_id, user_id)
            return [
                self._npc(row, False, visible_location_ids=visible_locs)
                for row in rows
            ]

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
        visible_to_member_ids: list[str] | None = None,
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode criar NPCs.")
            target_ids = self._resolve_npc_targets(
                conn, campaign_id, visibility, visible_to_member_ids
            )
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.insert_npc(
                conn,
                campaign_id,
                {
                    "name": name.strip(),
                    "role": role.strip(),
                    "attitude": attitude,
                    "description": description.strip(),
                    "gm_notes": gm_notes.strip(),
                    "combat_data": combat_data,
                    "visibility": visibility,
                },
            )
            row = self._set_record_location(
                conn, "campaign_npcs", campaign_id, row["id"], resolved_location
            )
            self._save_npc_visibility(conn, row["id"], target_ids)
            return self._npc(row, is_gm=True, visible_to_member_ids=target_ids)

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
        visible_to_member_ids: list[str] | None = None,
        location_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar NPCs.")
            target_ids = self._resolve_npc_targets(
                conn, campaign_id, visibility, visible_to_member_ids
            )
            resolved_location = self._resolve_location_id(conn, campaign_id, location_id)
            row = records.update_npc(
                conn,
                campaign_id,
                npc_id,
                {
                    "name": name.strip(),
                    "role": role.strip(),
                    "attitude": attitude,
                    "description": description.strip(),
                    "gm_notes": gm_notes.strip(),
                    "combat_data": combat_data,
                    "visibility": visibility,
                },
            )
            row = self._set_record_location(
                conn, "campaign_npcs", campaign_id, row["id"], resolved_location
            )
            self._save_npc_visibility(conn, npc_id, target_ids)
            return self._npc(row, is_gm=True, visible_to_member_ids=target_ids)

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

    def _assert_gm_npc(self, conn: sqlite3.Connection, campaign_id: str, user_id: str, npc_id: str) -> None:
        if self._member(conn, campaign_id, user_id)["role"] != "gm":
            raise CampaignError(403, "Apenas o mestre pode acessar o vínculo do bestiário.")
        if not conn.execute(
            "SELECT 1 FROM campaign_npcs WHERE id = ? AND campaign_id = ?", (npc_id, campaign_id)
        ).fetchone():
            raise CampaignError(404, "NPC não encontrado.")

    def npc_bestiary_link(self, user_id: str, campaign_id: str, npc_id: str) -> str | None:
        """Devolve a ficha do bestiário vinculada ao NPC, ou None.

        O vínculo é privado do mestre: nunca aparece na lista de NPCs e não
        altera `combat_data`. Exemplo:
        `store.npc_bestiary_link(gm_id, campaign_id, npc_id) -> "lobo"`.
        """
        with closing(self._connect()) as conn:
            self._assert_gm_npc(conn, campaign_id, user_id, npc_id)
            row = conn.execute(
                "SELECT template_id FROM campaign_npc_bestiary_links WHERE npc_id = ?", (npc_id,)
            ).fetchone()
            return row["template_id"] if row else None

    def set_npc_bestiary_link(
        self, user_id: str, campaign_id: str, npc_id: str, template_id: str | None
    ) -> None:
        """Vincula uma ficha ou remove o vínculo quando `template_id` é None.

        Usa `write_transaction` porque a checagem de papel e a escrita precisam
        da mesma transação; dois mestre-encontros concorrentes não podem
        sobrescrever o vínculo lido fora do lock. Exemplo:
        `store.set_npc_bestiary_link(gm_id, campaign_id, npc_id, None)`.
        """
        with write_transaction(self.path) as conn:
            self._assert_gm_npc(conn, campaign_id, user_id, npc_id)
            if template_id is None:
                conn.execute(
                    "DELETE FROM campaign_npc_bestiary_links WHERE npc_id = ?", (npc_id,)
                )
                return
            conn.execute(
                """INSERT INTO campaign_npc_bestiary_links (npc_id, template_id)
                VALUES (?, ?)
                ON CONFLICT(npc_id) DO UPDATE SET template_id = excluded.template_id""",
                (npc_id, template_id),
            )

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

    # --- COMPÊNDIO (BESTIÁRIO & HERBÁRIO) ---
    def list_compendium(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            return compendium_records.list_compendium_rows(
                conn, campaign_id, user_id, is_gm=role == "gm"
            )

    def upsert_compendium_entry(
        self,
        user_id: str,
        campaign_id: str,
        data: dict,
        entry_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar o compêndio.")
            if not (data.get("name") or "").strip():
                raise CampaignError(400, "Nome da entrada é obrigatório.")
            return compendium_records.upsert_compendium_entry(
                conn, campaign_id, data, entry_id=entry_id
            )

    def delete_compendium_entry(
        self, user_id: str, campaign_id: str, entry_id: str
    ) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir entradas.")
            deleted = conn.execute(
                "DELETE FROM campaign_compendium_entries WHERE id = ? AND campaign_id = ?",
                (entry_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Entrada do compêndio não encontrada.")

    def save_compendium_note(
        self, user_id: str, campaign_id: str, entry_id: str, note: str
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            return compendium_records.save_compendium_note(
                conn, campaign_id, entry_id, user_id, note, is_gm=role == "gm"
            )

    # --- LOCATIONS ---
    @staticmethod
    def _visible_location_ids(
        conn: sqlite3.Connection, campaign_id: str, user_id: str
    ) -> set[str]:
        rows = conn.execute(
            """SELECT DISTINCT l.id FROM campaign_locations l
            LEFT JOIN campaign_location_visibility v
              ON v.location_id = l.id AND v.user_id = ?
            WHERE l.campaign_id = ?
              AND (l.visibility = 'party' OR (l.visibility = 'selected' AND v.user_id IS NOT NULL))""",
            (user_id, campaign_id),
        ).fetchall()
        return {row["id"] for row in rows}

    @staticmethod
    def _resolve_location_id(
        conn: sqlite3.Connection, campaign_id: str, location_id: str | None
    ) -> str | None:
        if location_id is None:
            return None
        clean = location_id.strip()
        if not clean:
            return None
        row = conn.execute(
            "SELECT 1 FROM campaign_locations WHERE id = ? AND campaign_id = ?",
            (clean, campaign_id),
        ).fetchone()
        if not row:
            raise CampaignError(422, "Localidade não encontrada nesta campanha.")
        return clean

    @staticmethod
    def _resolve_parent_location_id(
        conn: sqlite3.Connection,
        campaign_id: str,
        parent_id: str | None,
        self_id: str | None = None,
    ) -> str | None:
        if parent_id is None:
            return None
        clean = parent_id.strip()
        if not clean:
            return None
        if self_id and clean == self_id:
            raise CampaignError(422, "Uma localidade não pode ser pai de si mesma.")
        row = conn.execute(
            "SELECT id, kind FROM campaign_locations WHERE id = ? AND campaign_id = ?",
            (clean, campaign_id),
        ).fetchone()
        if not row:
            raise CampaignError(422, "Região pai não encontrada nesta campanha.")
        if row["kind"] != "region":
            raise CampaignError(422, "Apenas regiões podem ser definidas como pai.")
        return clean

    @staticmethod
    def _set_record_location(
        conn: sqlite3.Connection,
        table: str,
        campaign_id: str,
        record_id: str,
        location_id: str | None,
    ) -> sqlite3.Row:
        if table not in {
            "campaign_sessions",
            "campaign_quests",
            "campaign_encounters",
            "campaign_npcs",
        }:
            raise ValueError(f"Tabela inválida para location_id: {table}")
        conn.execute(
            f"UPDATE {table} SET location_id = ? WHERE id = ? AND campaign_id = ?",
            (location_id, record_id, campaign_id),
        )
        return conn.execute(
            f"SELECT * FROM {table} WHERE id = ? AND campaign_id = ?",
            (record_id, campaign_id),
        ).fetchone()

    @staticmethod
    def _save_location_visibility(
        conn: sqlite3.Connection,
        location_id: str,
        member_ids: list[str],
    ) -> None:
        conn.execute(
            "DELETE FROM campaign_location_visibility WHERE location_id = ?",
            (location_id,),
        )
        for member_id in member_ids:
            conn.execute(
                "INSERT INTO campaign_location_visibility (location_id, user_id) VALUES (?, ?)",
                (location_id, member_id),
            )

    @staticmethod
    def _location_visible_map(
        conn: sqlite3.Connection, location_ids: list[str]
    ) -> dict[str, list[str]]:
        if not location_ids:
            return {}
        placeholders = ", ".join("?" for _ in location_ids)
        rows = conn.execute(
            f"SELECT location_id, user_id FROM campaign_location_visibility WHERE location_id IN ({placeholders}) ORDER BY user_id",
            tuple(location_ids),
        ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["location_id"], []).append(row["user_id"])
        return result

    @staticmethod
    def _location(
        row: sqlite3.Row,
        is_gm: bool,
        visible_to_member_ids: list[str] | None = None,
        visible_parent_ids: set[str] | None = None,
    ) -> dict:
        parent_id = row["parent_id"]
        if (
            not is_gm
            and parent_id
            and visible_parent_ids is not None
            and parent_id not in visible_parent_ids
        ):
            parent_id = None
        data = {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "parentId": parent_id,
            "kind": row["kind"],
            "name": row["name"],
            "description": row["description"],
            "image": row["image"],
            "visibility": row["visibility"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        if is_gm:
            data["gmNotes"] = row["gm_notes"]
            data["visibleToMemberIds"] = (
                list(visible_to_member_ids or [])
                if row["visibility"] == "selected"
                else []
            )
        return data

    def list_locations(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            is_gm = role == "gm"
            if is_gm:
                rows = conn.execute(
                    """SELECT * FROM campaign_locations
                    WHERE campaign_id = ?
                    ORDER BY CASE WHEN kind = 'region' THEN 0 ELSE 1 END, name ASC, created_at DESC""",
                    (campaign_id,),
                ).fetchall()
                vis_map = self._location_visible_map(conn, [row["id"] for row in rows])
                return [
                    self._location(row, True, vis_map.get(row["id"])) for row in rows
                ]
            rows = conn.execute(
                """SELECT DISTINCT l.* FROM campaign_locations l
                LEFT JOIN campaign_location_visibility v
                  ON v.location_id = l.id AND v.user_id = ?
                WHERE l.campaign_id = ?
                  AND (l.visibility = 'party' OR (l.visibility = 'selected' AND v.user_id IS NOT NULL))
                ORDER BY CASE WHEN l.kind = 'region' THEN 0 ELSE 1 END, l.name ASC, l.created_at DESC""",
                (user_id, campaign_id),
            ).fetchall()
            visible_ids = {row["id"] for row in rows}
            return [
                self._location(row, False, visible_parent_ids=visible_ids)
                for row in rows
            ]

    def create_location(
        self,
        user_id: str,
        campaign_id: str,
        name: str,
        kind: str = "region",
        parent_id: str | None = None,
        description: str = "",
        gm_notes: str = "",
        image: str | None = None,
        visibility: str = "gm",
        visible_to_member_ids: list[str] | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode criar localidades.")
            target_ids = self._resolve_npc_targets(
                conn, campaign_id, visibility, visible_to_member_ids
            )
            resolved_parent = self._resolve_parent_location_id(
                conn, campaign_id, parent_id
            )
            now = datetime.now(UTC).isoformat()
            location_id = str(uuid4())
            clean_image = image.strip() if isinstance(image, str) and image.strip() else None
            conn.execute(
                """INSERT INTO campaign_locations (
                    id, campaign_id, parent_id, kind, name, description,
                    gm_notes, image, visibility, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    location_id,
                    campaign_id,
                    resolved_parent,
                    kind,
                    name.strip(),
                    description.strip(),
                    gm_notes.strip(),
                    clean_image,
                    visibility,
                    now,
                    now,
                ),
            )
            self._save_location_visibility(conn, location_id, target_ids)
            row = conn.execute(
                "SELECT * FROM campaign_locations WHERE id = ?", (location_id,)
            ).fetchone()
            return self._location(row, is_gm=True, visible_to_member_ids=target_ids)

    def update_location(
        self,
        user_id: str,
        campaign_id: str,
        location_id: str,
        name: str,
        kind: str = "region",
        parent_id: str | None = None,
        description: str = "",
        gm_notes: str = "",
        image: str | None = None,
        visibility: str = "gm",
        visible_to_member_ids: list[str] | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar localidades.")
            existing = conn.execute(
                "SELECT id FROM campaign_locations WHERE id = ? AND campaign_id = ?",
                (location_id, campaign_id),
            ).fetchone()
            if not existing:
                raise CampaignError(404, "Localidade não encontrada.")
            target_ids = self._resolve_npc_targets(
                conn, campaign_id, visibility, visible_to_member_ids
            )
            resolved_parent = self._resolve_parent_location_id(
                conn, campaign_id, parent_id, self_id=location_id
            )
            now = datetime.now(UTC).isoformat()
            clean_image = image.strip() if isinstance(image, str) and image.strip() else None
            conn.execute(
                """UPDATE campaign_locations
                SET parent_id = ?, kind = ?, name = ?, description = ?,
                    gm_notes = ?, image = ?, visibility = ?, updated_at = ?
                WHERE id = ? AND campaign_id = ?""",
                (
                    resolved_parent,
                    kind,
                    name.strip(),
                    description.strip(),
                    gm_notes.strip(),
                    clean_image,
                    visibility,
                    now,
                    location_id,
                    campaign_id,
                ),
            )
            self._save_location_visibility(conn, location_id, target_ids)
            row = conn.execute(
                "SELECT * FROM campaign_locations WHERE id = ?", (location_id,)
            ).fetchone()
            return self._location(row, is_gm=True, visible_to_member_ids=target_ids)

    def delete_location(
        self, user_id: str, campaign_id: str, location_id: str
    ) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir localidades.")
            for table in (
                "campaign_sessions",
                "campaign_quests",
                "campaign_encounters",
                "campaign_npcs",
            ):
                conn.execute(
                    f"UPDATE {table} SET location_id = NULL WHERE location_id = ? AND campaign_id = ?",
                    (location_id, campaign_id),
                )
            conn.execute(
                "UPDATE campaign_locations SET parent_id = NULL WHERE parent_id = ? AND campaign_id = ?",
                (location_id, campaign_id),
            )
            conn.execute(
                "DELETE FROM campaign_location_visibility WHERE location_id = ?",
                (location_id,),
            )
            deleted = conn.execute(
                "DELETE FROM campaign_locations WHERE id = ? AND campaign_id = ?",
                (location_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Localidade não encontrada.")

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
        now = updated_at or datetime.now(UTC).isoformat()
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
        now = updated_at or datetime.now(UTC).isoformat()
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

            now = datetime.now(UTC).isoformat()

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

    # --- COMPÊNDIO (BESTIÁRIO & HERBÁRIO) ---
    def list_compendium(self, user_id: str, campaign_id: str) -> list[dict]:
        with closing(self._connect()) as conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            return compendium_records.list_compendium_rows(
                conn, campaign_id, user_id, is_gm=role == "gm"
            )

    def upsert_compendium_entry(
        self,
        user_id: str,
        campaign_id: str,
        data: dict,
        entry_id: str | None = None,
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode editar o compêndio.")
            if not (data.get("name") or "").strip():
                raise CampaignError(400, "Nome da entrada é obrigatório.")
            return compendium_records.upsert_compendium_entry(
                conn, campaign_id, data, entry_id=entry_id
            )

    def delete_compendium_entry(
        self, user_id: str, campaign_id: str, entry_id: str
    ) -> None:
        with closing(self._connect()) as conn, conn:
            if self._member(conn, campaign_id, user_id)["role"] != "gm":
                raise CampaignError(403, "Apenas o mestre pode excluir entradas.")
            deleted = conn.execute(
                "DELETE FROM campaign_compendium_entries WHERE id = ? AND campaign_id = ?",
                (entry_id, campaign_id),
            )
            if deleted.rowcount == 0:
                raise CampaignError(404, "Entrada do compêndio não encontrada.")

    def save_compendium_note(
        self, user_id: str, campaign_id: str, entry_id: str, note: str
    ) -> dict:
        with closing(self._connect()) as conn, conn:
            role = self._member(conn, campaign_id, user_id)["role"]
            return compendium_records.save_compendium_note(
                conn, campaign_id, entry_id, user_id, note, is_gm=role == "gm"
            )

