"""Fontes brutas de preparação da campanha.

Etapa 2 do plano `plans/mcp-preparacao-campanha.md` (ai-memory: workspace
`pharos`, projeto `intregração`).

Aqui vive apenas o **conteúdo bruto** que o mestre importa das notas dele, mais
suas revisões imutáveis. Este módulo é deliberadamente separado de
`campaign_store.py`, que já concentra doze tabelas e não deveria absorver mais
essa responsabilidade.

Regras que o contrato depende:

- O texto é armazenado **exatamente como recebido**: nada de `strip`,
  normalização de espaços ou reformatar. A fidelidade é a razão de existir da
  camada de fontes.
- O `checksum` é SHA-256 do conteúdo recebido (representação UTF-8). Ele prova a
  integridade do que chegou e foi guardado — **não** prova que o cliente
  encaminhou todos os bytes de um arquivo local que o servidor nunca abriu.
- Caminho relativo é **metadado de origem**. Ele nunca é usado para abrir
  arquivo algum, nem para montar caminho no disco do servidor.
- Revisões são imutáveis e a importação é idempotente por `idempotencyKey`. A
  consulta da chave, a leitura da fonte e a escrita acontecem na MESMA transação
  (`BEGIN IMMEDIATE`): fora dela, duas importações juntas liam o mesmo estado e a
  perdedora explodia em `IntegrityError` (500) em vez de receber o resultado do
  vencedor. A chave vale para um pedido (coleção + caminho + conteúdo); usá-la para
  outro pedido é conflito, não sobrescrita.
"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.database import open_connection, write_transaction
from app.services.campaign_store import CampaignError, CampaignStore

MAX_CONTENT_CHARS = 200_000
MAX_PATH_CHARS = 512
MAX_TITLE_CHARS = 200
MAX_COLLECTION_CHARS = 120
MAX_IDEMPOTENCY_KEY_CHARS = 120
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
MAX_DUPLICATE_HINTS = 5

# colunas acrescentadas depois da primeira versão de `preparation_imports`
IMPORT_OPTIONAL_COLUMNS = (("request_fingerprint", "TEXT"),)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def checksum_of(content: str) -> str:
    """SHA-256 do conteúdo na representação UTF-8 definida pelo contrato."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def request_fingerprint(collection_id: str, relative_path: str, content: str) -> str:
    """Identidade do pedido de importação: a chave não pode virar curinga.

    Uma `idempotencyKey` representa um pedido, não a campanha inteira. Guardar esta
    impressão digital junto do resultado permite devolver o replay para o retry e
    recusar (409) quem reutiliza a mesma chave para outro arquivo.

    >>> request_fingerprint("c", "a.md", "x") == request_fingerprint("c", "a.md", "x")
    True
    >>> request_fingerprint("c", "a.md", "x") == request_fingerprint("c", "b.md", "x")
    False
    """
    identity = f"{collection_id}\n{relative_path}\n{checksum_of(content)}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def normalize_relative_path(raw: str) -> str:
    """Valida o caminho de origem e o devolve canônico, sem tocar no disco.

    O caminho identifica a fonte; não é uma instrução de filesystem. Por isso
    caminhos absolutos, traversal e caracteres de controle são recusados aqui, e
    nenhum valor retornado desta função é entregue a `open()`.

    >>> normalize_relative_path("sessoes\\\\sub\\\\a.md")
    'sessoes/sub/a.md'
    >>> normalize_relative_path("../segredo.md")
    Traceback (most recent call last):
        ...
    app.services.campaign_store.CampaignError: Caminho inválido: '..' não é permitido ...
    """
    candidate = raw or ""
    # caracteres de controle são olhados no valor recebido: dar `strip()` antes
    # aceitaria "\nnotas.md\n" como "notas.md", escondendo o dado corrompido
    if any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise CampaignError(
            422, f"Caminho não pode conter caracteres de controle: {raw!r}."
        )
    candidate = candidate.strip().replace("\\", "/")
    if not candidate:
        raise CampaignError(422, "Caminho obrigatório (ex.: sessoes/sessao-08.md).")
    if len(candidate) > MAX_PATH_CHARS:
        raise CampaignError(
            422, f"Caminho deve ter no máximo {MAX_PATH_CHARS} caracteres."
        )
    if candidate.startswith("/") or candidate[1:2] == ":":
        raise CampaignError(
            422,
            f"Caminho deve ser relativo à pasta da campanha, não absoluto: {raw!r}.",
        )
    segments = candidate.split("/")
    for segment in segments:
        if segment in ("", ".", ".."):
            raise CampaignError(
                422, f"Caminho inválido: {segment!r} não é permitido em {raw!r}."
            )
    return "/".join(segments)


class PreparationStore:
    """Persistência de fontes brutas e revisões, autorizada pelo mestre da campanha."""

    def __init__(self, path: str | Path, campaigns: CampaignStore):
        self.path = str(path)
        self.campaigns = campaigns
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS preparation_sources (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    collection_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    current_revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS preparation_source_identity
                    ON preparation_sources(campaign_id, collection_id, relative_path);
                CREATE INDEX IF NOT EXISTS preparation_source_recent
                    ON preparation_sources(campaign_id, updated_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS preparation_source_revisions (
                    source_id TEXT NOT NULL REFERENCES preparation_sources(id),
                    revision INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    content_chars INTEGER NOT NULL,
                    content_bytes INTEGER NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY (source_id, revision)
                );
                CREATE INDEX IF NOT EXISTS preparation_revision_checksum
                    ON preparation_source_revisions(source_id, checksum);
                CREATE TABLE IF NOT EXISTS preparation_imports (
                    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
                    idempotency_key TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    request_fingerprint TEXT,
                    PRIMARY KEY (campaign_id, idempotency_key)
                );
                """
            )
            self._ensure_optional_columns(conn)

    def _ensure_optional_columns(self, conn: sqlite3.Connection) -> None:
        """Adiciona colunas criadas depois da primeira versão das tabelas.

        `ALTER TABLE ADD COLUMN` basta aqui: `request_fingerprint` é metadado de
        detecção de reuso de chave, e o único leitor (`_replayed_result`) trata
        linha sem impressão digital como o comportamento antigo (devolver o replay).
        """
        existing = {
            row["name"] for row in conn.execute("PRAGMA table_info(preparation_imports)")
        }
        for column, declaration in IMPORT_OPTIONAL_COLUMNS:
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE preparation_imports ADD COLUMN {column} {declaration}"
                )
    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path)

    # ── escrita ────────────────────────────────────────────────────────────

    def import_source(
        self,
        user_id: str,
        campaign_id: str,
        *,
        collection_id: str,
        relative_path: str,
        title: str,
        content: str,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Importa conteúdo bruto, criando revisão apenas quando o texto mudou.

        Decidir "esta chave já rodou?", "esta fonte já existe?", "qual é a próxima
        revisão?" e depois escrever formam uma seção crítica só: tudo corre dentro
        de `write_transaction` (`BEGIN IMMEDIATE`). Lembre que o `sqlite3` abre
        transação apenas no primeiro DML, então ler antes de escrever sem isso vê um
        estado que já mudou na hora do `INSERT`.
        """
        collection = self._clean_text(collection_id, MAX_COLLECTION_CHARS, "coleção")
        clean_title = self._clean_text(title, MAX_TITLE_CHARS, "título")
        path = normalize_relative_path(relative_path)
        self._check_content(content)
        key = self._idempotency_key(idempotency_key)
        self.campaigns.assert_gm(user_id, campaign_id)

        with write_transaction(self.path) as conn:
            replay = self._replayed_result(conn, campaign_id, key, collection, path, content)
            if replay is not None:
                return replay

            existing = conn.execute(
                """SELECT * FROM preparation_sources
                WHERE campaign_id = ? AND collection_id = ? AND relative_path = ?""",
                (campaign_id, collection, path),
            ).fetchone()
            conflict = self._resolve_revision_precondition(existing, expected_revision)
            if conflict is not None:
                raise conflict

            try:
                result = (
                    self._append_revision(conn, existing, content, campaign_id)
                    if existing
                    else self._create_source(
                        conn, campaign_id, collection, path, clean_title, content
                    )
                )
            except sqlite3.IntegrityError as error:
                # dentro da transação de escrita isto não deveria acontecer; se
                # acontecer, o contrato é conflito lido, não o 500 do driver
                raise self._identity_conflict(collection, path) from error
            self._remember_import(conn, campaign_id, key, result, collection, path, content)
            return result

    @staticmethod
    def _identity_conflict(collection: str, path: str) -> CampaignError:
        return CampaignError(
            409,
            f"a fonte {collection}/{path} foi escrita por outro pedido neste "
            "meio-tempo. Reler a fonte antes de importar de novo.",
        )

    def _create_source(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        collection: str,
        path: str,
        title: str,
        content: str,
    ) -> dict[str, Any]:
        source_id = str(uuid4())
        now = utc_now()
        conn.execute(
            """INSERT INTO preparation_sources
            (id, campaign_id, collection_id, relative_path, title, current_revision,
             created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (source_id, campaign_id, collection, path, title, now, now),
        )
        self._insert_revision(conn, source_id, 1, content, now)
        return self._result(conn, source_id, "imported", 1)

    def _append_revision(
        self, conn: sqlite3.Connection, source: sqlite3.Row, content: str, campaign_id: str
    ) -> dict[str, Any]:
        checksum = checksum_of(content)
        current = self._revision(conn, source["id"], source["current_revision"])
        if current["checksum"] == checksum:
            return self._result(conn, source["id"], "unchanged", source["current_revision"])
        revision = source["current_revision"] + 1
        self._insert_revision(conn, source["id"], revision, content, utc_now())
        conn.execute(
            "UPDATE preparation_sources SET current_revision = ?, updated_at = ? WHERE id = ?",
            (revision, utc_now(), source["id"]),
        )
        return self._result(conn, source["id"], "new_revision", revision)

    def _insert_revision(
        self, conn: sqlite3.Connection, source_id: str, revision: int, content: str, when: str
    ) -> None:
        conn.execute(
            """INSERT INTO preparation_source_revisions
            (source_id, revision, content, checksum, content_chars, content_bytes, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                revision,
                content,
                checksum_of(content),
                len(content),
                len(content.encode("utf-8")),
                when,
            ),
        )

    # ── leitura ────────────────────────────────────────────────────────────

    def list_sources(
        self,
        user_id: str,
        campaign_id: str,
        *,
        search: str | None = None,
        collection_id: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Lista fontes com busca textual e paginação por cursor estável."""
        self.campaigns.assert_gm(user_id, campaign_id)
        page_size = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        where = ["s.campaign_id = ?"]
        params: list[Any] = [campaign_id]
        if collection_id:
            where.append("s.collection_id = ?")
            params.append(collection_id.strip())
        if search and search.strip():
            where.append(
                """(s.title LIKE ? ESCAPE '\\' OR s.relative_path LIKE ? ESCAPE '\\'
                    OR r.content LIKE ? ESCAPE '\\')"""
            )
            pattern = f"%{escape_like(search.strip())}%"
            params.extend([pattern, pattern, pattern])
        after = decode_cursor(cursor)
        if after:
            where.append("(s.updated_at, s.id) < (?, ?)")
            params.extend([after[0], after[1]])

        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT s.*, r.checksum, r.content_chars, r.content_bytes
                FROM preparation_sources s
                JOIN preparation_source_revisions r
                  ON r.source_id = s.id AND r.revision = s.current_revision
                WHERE {' AND '.join(where)}
                ORDER BY s.updated_at DESC, s.id DESC
                LIMIT ?""",
                (*params, page_size + 1),
            ).fetchall()

        items = [self._summary(row) for row in rows[:page_size]]
        has_more = len(rows) > page_size
        return {
            "items": items,
            "nextCursor": encode_cursor(items[-1]) if has_more and items else None,
        }

    def read_revision(
        self,
        user_id: str,
        campaign_id: str,
        source_id: str,
        revision: int | None = None,
    ) -> dict[str, Any]:
        """Uma revisão da fonte; sem número, a atual.

        `revision=None` é o que o MCP precisa: o agent aponta para a fonte e o
        servidor decide qual é a revisão corrente, em vez de o modelo adivinhar.
        """
        self.campaigns.assert_gm(user_id, campaign_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT r.*, s.relative_path, s.title FROM preparation_source_revisions r
                JOIN preparation_sources s ON s.id = r.source_id
                WHERE r.source_id = ? AND s.campaign_id = ?
                  AND r.revision = COALESCE(?, s.current_revision)""",
                (source_id, campaign_id, None if revision is None else int(revision)),
            ).fetchone()
            if not row:
                wanted = "atual" if revision is None else str(int(revision))
                raise CampaignError(
                    404,
                    f"Revisão {wanted} da fonte {source_id} não existe nesta "
                    "campanha. Listar as fontes para ver o número da revisão.",
                )
            return {
                "sourceId": row["source_id"],
                "revision": row["revision"],
                "title": row["title"],
                "relativePath": row["relative_path"],
                "content": row["content"],
                "checksum": row["checksum"],
                "contentChars": row["content_chars"],
                "contentBytes": row["content_bytes"],
                "importedAt": row["imported_at"],
            }

    def has_revision(self, campaign_id: str, source_id: str, revision: int) -> bool:
        """A revisão existe nessa campanha? Chamado após `assert_gm` do chamador.

        Existe para o `PreparationDraftStore` validar `(sourceId, revision)` sem
        copiar SQL das fontes. Não autoriza: quem chama já autorizou.
        """
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT 1 FROM preparation_source_revisions r
                JOIN preparation_sources s ON s.id = r.source_id
                WHERE r.source_id = ? AND r.revision = ? AND s.campaign_id = ?""",
                (source_id, int(revision), campaign_id),
            ).fetchone()
            return row is not None

    # ── auxiliares ─────────────────────────────────────────────────────────

    def _revision(self, conn: sqlite3.Connection, source_id: str, revision: int) -> dict:
        row = conn.execute(
            "SELECT * FROM preparation_source_revisions WHERE source_id = ? AND revision = ?",
            (source_id, revision),
        ).fetchone()
        return dict(row)

    def _result(
        self, conn: sqlite3.Connection, source_id: str, status: str, revision: int
    ) -> dict[str, Any]:
        source = conn.execute(
            """SELECT s.*, r.checksum, r.content_chars, r.content_bytes
            FROM preparation_sources s
            JOIN preparation_source_revisions r
              ON r.source_id = s.id AND r.revision = s.current_revision
            WHERE s.id = ?""",
            (source_id,),
        ).fetchone()
        return {
            "status": status,
            "revision": revision,
            "source": self._summary(source),
            "possibleDuplicates": self._duplicates(conn, source),
        }

    def _duplicates(self, conn: sqlite3.Connection, source: sqlite3.Row) -> list[dict]:
        """Mesmo conteúdo em outro caminho — sinaliza, nunca funde silenciosamente."""
        rows = conn.execute(
            """SELECT s2.id, s2.relative_path FROM preparation_sources s2
            JOIN preparation_source_revisions r2
              ON r2.source_id = s2.id AND r2.revision = s2.current_revision
            WHERE s2.campaign_id = ? AND s2.id <> ? AND r2.checksum = ?
            LIMIT ?""",
            (source["campaign_id"], source["id"], source["checksum"], MAX_DUPLICATE_HINTS),
        ).fetchall()
        return [{"sourceId": row["id"], "relativePath": row["relative_path"]} for row in rows]

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "collectionId": row["collection_id"],
            "relativePath": row["relative_path"],
            "title": row["title"],
            "currentRevision": row["current_revision"],
            "checksum": row["checksum"],
            "contentChars": row["content_chars"],
            "contentBytes": row["content_bytes"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    @staticmethod
    def _resolve_revision_precondition(
        existing: sqlite3.Row | None, expected_revision: int | None
    ) -> CampaignError | None:
        if existing is None or expected_revision is None:
            return None
        if expected_revision != existing["current_revision"]:
            return CampaignError(
                409,
                "A fonte mudou desde a leitura: revisão esperada "
                f"{expected_revision}, atual {existing['current_revision']}. "
                "Reler antes de importar.",
            )
        return None

    @staticmethod
    def _idempotency_key(value: str | None) -> str | None:
        """Identidade única da chave: o mesmo valor vale para a consulta e a gravação.

        Normalizar só na leitura fazia `"chave "` e `"chave"` serem registros
        diferentes, então o retry do mesmo pedido não encontrava o resultado antigo.
        """
        if value is None:
            return None
        return PreparationStore._clean_text(value, MAX_IDEMPOTENCY_KEY_CHARS, "idempotencyKey")

    @staticmethod
    def _replayed_result(
        conn: sqlite3.Connection,
        campaign_id: str,
        key: str | None,
        collection: str,
        path: str,
        content: str,
    ) -> dict[str, Any] | None:
        """Resultado já produzido por esta chave, se o pedido for o mesmo."""
        if key is None:
            return None
        row = conn.execute(
            """SELECT result, request_fingerprint FROM preparation_imports
            WHERE campaign_id = ? AND idempotency_key = ?""",
            (campaign_id, key),
        ).fetchone()
        if row is None:
            return None
        fingerprint = request_fingerprint(collection, path, content)
        if row["request_fingerprint"] not in (None, fingerprint):
            raise CampaignError(
                409,
                f"a idempotencyKey {key} já registrou outra importação nesta "
                "campanha (coleção, caminho ou conteúdo diferentes). Repetir o mesmo "
                "pedido devolve o resultado anterior; importar outra fonte exige "
                "outra chave.",
            )
        # `request_fingerprint` é vazio em linhas anteriores à coluna: sem como
        # comparar, mantém-se o contrato antigo de devolver o resultado gravado
        return json.loads(row["result"])

    @staticmethod
    def _remember_import(
        conn: sqlite3.Connection,
        campaign_id: str,
        key: str | None,
        result: dict,
        collection: str,
        path: str,
        content: str,
    ) -> None:
        if key is None:
            return
        conn.execute(
            """INSERT INTO preparation_imports
            (campaign_id, idempotency_key, result, request_fingerprint, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (
                campaign_id,
                key,
                json.dumps(result),
                request_fingerprint(collection, path, content),
                utc_now(),
            ),
        )

    @staticmethod
    def _clean_text(value: str, limit: int, label: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise CampaignError(422, f"{label} obrigatório.")
        if len(cleaned) > limit:
            raise CampaignError(422, f"{label} deve ter no máximo {limit} caracteres.")
        return cleaned

    @staticmethod
    def _check_content(content: str) -> None:
        if content is None or content == "":
            raise CampaignError(422, "Conteúdo da fonte obrigatório.")
        if len(content) > MAX_CONTENT_CHARS:
            raise CampaignError(
                422,
                f"Conteúdo com {len(content)} caracteres excede o limite de "
                f"{MAX_CONTENT_CHARS}. Importe em arquivos menores; nunca truncar.",
            )
        if "\x00" in content:
            raise CampaignError(422, "Conteúdo não pode conter caractere nulo.")


def escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def encode_cursor(item: dict[str, Any]) -> str:
    return f"{item['updatedAt']}|{item['id']}"


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    updated_at, separator, source_id = cursor.partition("|")
    if not separator or not updated_at or not source_id:
        raise CampaignError(422, "Cursor de paginação inválido.")
    return updated_at, source_id
