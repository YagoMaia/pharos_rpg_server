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
