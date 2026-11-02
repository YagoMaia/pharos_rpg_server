"""Provisiona (ou gira) os PINs das identidades da mesa.

O `table_login` parou de aceitar `pin == nome.minúsculas`: esse valor estava na dica da
própria tela de login, então não autenticava ninguém — e a partir do painel de credenciais
de preparação isso significaria poder emitir a credencial do assistente de outra pessoa.

Quem roda é o administrador, no servidor. Sem rota HTTP de propósito: não há superfície
pública nova para atacar.

Uso no VPS (o container tem o código do app, o script vem pelo stdin):

    sudo docker exec -i pharos-backend python - --list < tools/set_table_pin.py
    sudo docker exec -i pharos-backend python - --name Victor --generate < tools/set_table_pin.py
    sudo docker exec -i pharos-backend python - --name Yago --pin-file /tmp/pin.txt < tools/set_table_pin.py

Fora do container: `uv run python tools/set_table_pin.py --db /caminho/pharos.sqlite3 ...`.

PIN gerado aparece uma única vez na saída; girar um PIN derruba as sessões daquela
identidade (é para isso que se gira).
"""

import argparse
import importlib.util
import secrets
import sys
from contextlib import closing
from pathlib import Path

if importlib.util.find_spec("app") is None:
    # `uv run python tools/set_table_pin.py`: só tools/ estaria no path. Ao rodar dentro
    # do container (stdin pipe) o `app` já é encontrado e este ramo não roda.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.core.database import open_connection
from app.services.campaign_store import TABLE_NAMES, CampaignStore
from app.services.errors import CampaignError

# Sem 0/O/1/I/l: PIN é ditado por mensagem, e caractere ambíguo vira suporte técnico.
PIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
PIN_CHARS = 10


def pin_status(db_path: str) -> list[str]:
    """Situação de cada identidade da mesa; leitura pura, sem abrir escrita.

    O arquivo precisa existir: `sqlite3` cria um banco vazio ao abrir um caminho
    inexistente, e ali o erro seria `no such table: table_users` sem dizer o caminho.
    """
    if not Path(db_path).exists():
        raise SystemExit(
            f"não existe banco em {db_path!r}; passe --db apontando para o SQLite do app"
            " (em produção: /data/pharos.sqlite3)."
        )
    with closing(open_connection(db_path)) as conn:
        rows = {
            row["name"]: row["pin_hash"]
            for row in conn.execute("SELECT name, pin_hash FROM table_users")
        }
    lines = []
    for name in TABLE_NAMES:
        if name not in rows:
            lines.append(f"  {name:<10} nunca usada")
        elif rows[name]:
            lines.append(f"  {name:<10} PIN definido")
        else:
            lines.append(f"  {name:<10} SEM PIN (não entra)")
    return lines


def read_pin_file(path: str) -> str:
    """PIN do arquivo, sem o fim de linha; `utf-8-sig` ignora BOM de editor Windows."""
    try:
        return Path(path).read_text(encoding="utf-8-sig").strip()
    except OSError as error:
        raise SystemExit(f"não consegui ler o PIN de {path!r}: {error}")


def resolve_pin(args: argparse.Namespace, name: str) -> str:
    if args.generate:
        return "".join(secrets.choice(PIN_ALPHABET) for _ in range(PIN_CHARS))
    return read_pin_file(args.pin_file)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=settings.CAMPAIGN_DB_PATH,
        help="caminho do SQLite (padrão: CAMPAIGN_DB_PATH do app)",
    )
    parser.add_argument("--name", action="append", default=[], help="identidade da mesa (repetível)")
    parser.add_argument("--generate", action="store_true", help="gera um PIN e mostra uma vez")
    parser.add_argument("--pin-file", help="arquivo com o PIN a definir")
    parser.add_argument("--list", action="store_true", help="mostra o status sem escrever")
    args = parser.parse_args(argv)

    if args.list:
        status = pin_status(args.db)  # antes de imprimir cabeçalho: senão o erro vem depois da promessa
        print(f"PINs da mesa em {args.db}")
        print("\n".join(status))
        return 0
    if not args.name:
        parser.error("nada a fazer: passe --name (ou --list)")
    if not args.generate and not args.pin_file:
        parser.error("escolha --generate ou --pin-file")

    store = CampaignStore(args.db)
    for name in args.name:
        try:
            pin = resolve_pin(args, name)
            record = store.set_table_pin(name, pin)
        except CampaignError as failure:
            print(f"{name}: {failure}")
            return 2
        if args.generate:
            print(f"{name}: {pin}  ({record['pinChars']} caracteres, mostrado uma única vez)")
        else:
            print(f"{name}: PIN definido ({record['pinChars']} caracteres)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
