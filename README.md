# Pharos RPG Server

Backend server for a real-time RPG, built with FastAPI and WebSockets.

## Features

- Real-time communication via WebSockets.
- RPG game logic (combat, etc.).
- Uses Redis for data storage/caching.

## Technologies Used

- Python 3.12+
- FastAPI
- WebSockets
- Redis

## How to Run

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd pharos-rpg-server
   ```

2. **Install dependencies:**

   **Using `pip`:**
   ```bash
   pip install -r requirements.txt
   ```

   **Using `uv`:**
   ```bash
   uv sync
   ```

3. **Run the server:**
   ```bash
   uvicorn app.main:app --reload
   ```

## Banco de dados

Campanhas, sessões, NPCs, encontros e preparação vivem em **SQLite**
(`CAMPAIGN_DB_PATH`, padrão local; em produção `/data/pharos.sqlite3` dentro de um
volume Docker). Redis é usado apenas para transporte/broadcast do combate em
tempo real — não é a fonte de verdade dos dados da campanha.

Toda conexão passa por `app/core/database.py`, que aplica:

| Pragma | Por quê |
|---|---|
| `journal_mode = WAL` | leitores coexistem com escritores; sem ele, uma importação de notas bloqueia o painel do mestre |
| `foreign_keys = ON` | é por conexão, precisa ser reafirmado em cada conexão nova |
| `timeout=5` | espera o lock em vez de falhar imediatamente |

**Backup não é mais `cp` do arquivo.** Com WAL, dados cometidos podem estar em
`pharos.sqlite3-wal`; copiar só o `pharos.sqlite3` gera backup incompleto ou até
sem tabelas. Use `sqlite3.Connection.backup()`:

```bash
docker exec pharos-backend python -c "import sqlite3; s=sqlite3.connect('/data/pharos.sqlite3'); d=sqlite3.connect('/data/backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker cp pharos-backend:/data/backup.sqlite3 ./backup_pharos.sqlite3
docker exec pharos-backend rm /data/backup.sqlite3
```

O runbook versionado fica em `docs/DEPLOYMENT_DOCKER.md` e `docs/CHECKLIST_DEPLOY.md`
no repositório do frontend (`pharos_rp`).

## Preparação do mestre (fontes, rascunhos, credenciais)

Fluxo em três camadas, cada uma com sua rota sob `/campaigns/{campaign_id}/preparation`:

| Camada | Rotas | O que faz |
|---|---|---|
| fontes brutas | `POST /sources/import`, `GET /sources`, `GET /sources/{id}/revisions/{n}` | guarda o texto recebido byte a byte, com revisões imutáveis |
| rascunhos | `POST/GET /drafts`, `GET/PUT /drafts/{id}` | propostas tipadas (sessão/encontro/NPC/missão) vinculadas a `(sourceId, revision)` |
| aplicação | `POST /drafts/{id}/apply` | rascunho revisado vira registro oficial, numa transação só |

Nenhuma dessas rotas inicia combate nem escreve em `journal_entries`. Aplicar um
rascunho cujo conteúdo chega aos jogadores (resumo de sessão, NPC/missão com
`visibility=party`) exige `acknowledgePlayerVisibility: true` no pedido.

Importar é idempotente por `idempotencyKey`, e a chave representa **um** pedido
(coleção + caminho + conteúdo): repetir o mesmo pedido devolve exatamente o
resultado anterior; reutilizar a chave para outra fonte devolve 409. Para
acrescentar revisão com segurança, envie `expectedRevision` com o número que você
leu — se a fonte mudou no meio do caminho, a resposta é 409, não sobrescrita. A
leitura da chave, a leitura da fonte e a escrita acontecem numa transação só, então
duas importações concorrentes do mesmo arquivo não viram fonte duplicada nem 500.

### Credencial de preparação (para o agent do mestre)

O agent de preparação autentica com uma credencial emitida pelo próprio GM, em vez
da senha ou do token de sessão do app:

```bash
# o GM (logado no app) emite uma credencial por campanha:
curl -X POST "$PHAROS/campaigns/$CID/preparation/tokens" \
  -H "Authorization: Bearer $SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"label": "agent do Victor"}'
# -> { "id": "...", "token": "pharos_prep_..." }   (o valor aparece só aqui)

# o agent usa a credencial:
curl "$PHAROS/campaigns/$CID/preparation/sources" -H "Authorization: Bearer pharos_prep_..."
```

Regras (cobertas por testes em `tests/test_preparation_tokens.py`):

- no banco fica apenas o SHA-256 da credencial; `GET /preparation/tokens` lista
  rótulo, criação e último uso — nunca o segredo;
- `DELETE /preparation/tokens/{id}` revoga e vale imediatamente;
- a credencial autentica **somente** rotas de preparação da campanha dela, e só
  as de criação/edição de fontes e rascunhos;
- a credencial **não aplica rascunho**: publicar exige o GM logado no app, porque
  "`approved` enviado pelo modelo não comprova consentimento humano".

A tela do painel do mestre para emitir/revogar essas credenciais ainda não existe;
por enquanto a emissão é pela rota acima.
