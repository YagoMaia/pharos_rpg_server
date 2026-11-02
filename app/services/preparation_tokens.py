"""Credenciais de preparação: o agent do mestre entra, mas não publica.

Etapa 1 do plano `plans/mcp-preparacao-campanha.md`. O agent do Victor roda na
máquina dele e fala com o MCP remoto no VPS; ele precisa de uma identidade sem
receber a senha nem o token de sessão do app. Regras que este módulo garante:

- **uma credencial por campanha**, emitida pelo GM daquela campanha;
- o valor em claro existe só na resposta de emissão: no banco fica SHA-256, então
  dump do SQLite não entrega credencial utilizável;
- token com 256 bits de entropia: não há PIN a adivinhar (diferente do
  `table_login`, que hoje aceita `pin == nome.lower()`);
- revogar corta imediatamente — a checagem lê `revoked_at` a cada uso;
- o token **não** autoriza nada sozinho: ele resolve para o usuário do GM, e as
  stores continuam chamando `CampaignStore.assert_gm`. A restrição de escopo
  (só rotas de preparação) é do roteador, em `app/routers/preparation.py`.

Publicar rascunho continua exigindo o GM logado no app: `preparation_apply`
recusa identidade que veio por credencial de preparação (§5 do plano:
"approved enviado pelo modelo não comprova consentimento humano").
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from typing import Any
from uuid import uuid4

from app.core.database import open_connection, write_transaction
from app.services.campaign_store import CampaignError, CampaignStore

TOKEN_PREFIX = "pharos_prep_"
TOKEN_BYTES = 32
MAX_LABEL_CHARS = 80

TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS preparation_tokens (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    created_by TEXT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS preparation_token_campaign
    ON preparation_tokens(campaign_id, created_at DESC, id DESC);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(token: str) -> str:
    return sha256(token.encode("utf-8")).hexdigest()


class PreparationTokenStore:
    """Emissão, listagem, revogação e validação de credenciais de preparação."""

    def __init__(self, path: str | Path, campaigns: CampaignStore):
        self.path = str(path)
        self.campaigns = campaigns
        with closing(open_connection(self.path)) as conn, conn:
            conn.executescript(TOKENS_SCHEMA)

    def issue(self, user_id: str, campaign_id: str, label: str = "") -> dict[str, Any]:
        """Gera uma credencial; o valor em claro só existe neste retorno."""
        self.campaigns.assert_gm(user_id, campaign_id)
        clean = (label or "").strip()
        if len(clean) > MAX_LABEL_CHARS:
            raise CampaignError(
                422,
                f"label com {len(clean)} caracteres excede o limite de "
                f"{MAX_LABEL_CHARS}.",
            )
        token, token_id, now = TOKEN_PREFIX + token_urlsafe(TOKEN_BYTES), str(uuid4()), utc_now()
        with write_transaction(self.path) as conn:
            conn.execute(
                """INSERT INTO preparation_tokens
                (id, campaign_id, created_by, token_hash, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (token_id, campaign_id, user_id, _digest(token), clean, now),
            )
        return {
            "id": token_id,
            "campaignId": campaign_id,
            "label": clean,
            "token": token,
            "createdAt": now,
            "lastUsedAt": None,
            "revokedAt": None,
        }

    def list_tokens(self, user_id: str, campaign_id: str) -> dict[str, Any]:
        """Inventário sem segredo: `token_hash` nunca sai daqui."""
        self.campaigns.assert_gm(user_id, campaign_id)
        with closing(open_connection(self.path)) as conn:
            rows = conn.execute(
                """SELECT id, label, created_at, last_used_at, revoked_at
                FROM preparation_tokens WHERE campaign_id = ?
                ORDER BY created_at DESC, id DESC""",
                (campaign_id,),
            ).fetchall()
        return {"items": [self._summary(row) for row in rows]}

    def revoke(self, user_id: str, campaign_id: str, token_id: str) -> dict[str, Any]:
        """Marca `revoked_at`; o próximo uso já é recusado."""
        self.campaigns.assert_gm(user_id, campaign_id)
        with write_transaction(self.path) as conn:
            revoked = conn.execute(
                """UPDATE preparation_tokens SET revoked_at = ?
                WHERE id = ? AND campaign_id = ? AND revoked_at IS NULL""",
                (utc_now(), token_id, campaign_id),
            )
            if revoked.rowcount == 0:
                raise CampaignError(
                    404,
                    f"credencial de preparação {token_id} não existe nesta campanha "
                    "ou já está revogada.",
                )
            row = conn.execute(
                """SELECT id, label, created_at, last_used_at, revoked_at
                FROM preparation_tokens WHERE id = ?""",
                (token_id,),
            ).fetchone()
        return self._summary(row)

    def resolve(self, token: str) -> dict[str, str] | None:
        """De quem é esta credencial de preparação (não autoriza nada).

        O prefixo é checagem rápida para não gastar hash em token de sessão. A
        leitura de `revoked_at` acontece a cada uso de propósito: revogar tem que
        valer na hora, não só no próximo login.
        """
        if not token or not token.startswith(TOKEN_PREFIX):
            return None
        # A procura é leitura: só uma credencial válida escreve `last_used_at`. Se a
        # resolução abrisse `BEGIN IMMEDIATE` primeiro, qualquer `pharos_prep_...`
        # inventado pela internet reservaria o escritor único do SQLite e disputaria
        # com as importações do mestre.
        with closing(open_connection(self.path)) as conn:
            row = conn.execute(
                """SELECT id, campaign_id, created_by, revoked_at
                FROM preparation_tokens WHERE token_hash = ?""",
                (_digest(token),),
            ).fetchone()
        if not row or row["revoked_at"]:
            return None
        with write_transaction(self.path) as conn:
            conn.execute(
                "UPDATE preparation_tokens SET last_used_at = ? WHERE id = ?",
                (utc_now(), row["id"]),
            )
        return {
            "userId": row["created_by"],
            "campaignId": row["campaign_id"],
            "tokenId": row["id"],
        }

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "label": row["label"],
            "createdAt": row["created_at"],
            "lastUsedAt": row["last_used_at"],
            "revokedAt": row["revoked_at"],
        }
