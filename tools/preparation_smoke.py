"""Smoke de ponta a ponta do MCP de preparação contra a instalação pública.

O smoke de `curl` só prova que o endpoint responde. Este script exercita o
contrato inteiro pelo cliente MCP **oficial**, pela internet, com uma credencial
emitida de verdade, e revoga essa credencial no fim.

A campanha de ensaio é separada de propósito: as fontes brutas são imutáveis e
não existe ferramenta de exclusão, então qualquer nota escrita na campanha real
da mesa fica lá para sempre.

Uso:

    uv run python tools/preparation_smoke.py
    uv run python tools/preparation_smoke.py --base http://localhost:8000 --keep

E-mail e senha são pedidos no terminal — nunca por argumento, nunca gravados em
disco ou impressos na saída. A credencial de preparação aparece mascarada.
"""

import argparse
import asyncio
import getpass
import json
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

DEFAULT_BASE = "https://pharos-server.yagomaia.com.br"
ENSAIO = "Ensaio MCP"
NOTE = "Sessão 9\tde ensaio\r\nFerrarin mentiu sobre o porto — çãé 🎲\r\nfim."
EXPECTED_TOOLS = {
    "connection_status",
    "import_source",
    "list_sources",
    "search_sources",
    "read_source",
    "read_campaign_context",
    "save_preparation_draft",
    "read_preparation_draft",
}
FORBIDDEN = ("apply", "delete", "remove", "launch", "combat", "publish")


class Reporter:
    """Acumula PASS/FAIL; a saída é texto plano porque é saída de CLI."""

    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  ok   {name}")
            return
        self.failures.append(name)
        print(f"  FAIL {name} :: {detail}")

    def summary(self) -> int:
        print(f"\n{self.passed} verificações ok, {len(self.failures)} falhas")
        for name in self.failures:
            print(f"  - {name}")
        return 1 if self.failures else 0


@dataclass
class Rest:
    """REST da campanha pelo token de sessão do mestre (o mesmo do app)."""

    base: str
    token: str

    def call(self, method: str, path: str, body: dict[str, Any] | None = None):
        with httpx2.Client(base_url=self.base, timeout=30.0) as client:
            return client.request(
                method,
                path,
                json=body,
                headers={"Authorization": f"Bearer {self.token}"},
            )


def login(base: str, email: str, password: str) -> str:
    with httpx2.Client(base_url=base, timeout=30.0) as client:
        response = client.post("/auth/login", json={"email": email, "password": password})
    response.raise_for_status()
    return response.json()["accessToken"]


def ensure_campaign(rest: Rest, name: str) -> str:
    existing = rest.call("GET", "/campaigns").json()
    found = next((c for c in existing if c["name"] == name), None)
    if found:
        return found["id"]
    return rest.call("POST", "/campaigns", {"name": name}).json()["id"]


def issue_prep_token(rest: Rest, campaign_id: str) -> dict[str, Any]:
    response = rest.call(
        "POST", f"/campaigns/{campaign_id}/preparation/tokens", {"label": "smoke automatizado"}
    )
    response.raise_for_status()
    return response.json()


def mask(secret: str) -> str:
    return f"{secret[:12]}…({len(secret)} chars)"


async def call_tool(session: ClientSession, name: str, arguments: dict[str, Any]):
    return await session.call_tool(name, arguments)


def as_json(result) -> dict[str, Any]:
    return json.loads(result.content[0].text)


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content)


async def check_surface(session: ClientSession, report: Reporter) -> None:
    tools = (await session.list_tools()).tools
    names = {tool.name for tool in tools}
    report.check("inventário é exatamente o do plano", names == EXPECTED_TOOLS, str(sorted(names)))
    forbidden = [name for name in names if any(word in name.lower() for word in FORBIDDEN)]
    report.check("nenhuma ferramenta de aplicar/excluir/combate", not forbidden, str(forbidden))


async def check_sources(session: ClientSession, report: Reporter) -> str:
    imported = await call_tool(session, "import_source", {
        "collectionId": "ensaio-smoke", "relativePath": "notas/sessao-09.md",
        "title": "Sessão 9 — ensaio", "content": NOTE, "idempotencyKey": "smoke-1",
    })
    report.check("import_source aceito", not imported.is_error, text_of(imported))
    body = as_json(imported)
    report.check(
        "resposta não devolve o documento", NOTE not in text_of(imported), text_of(imported)[:120]
    )
    source_id = body["source"]["id"]

    again = await call_tool(session, "import_source", {
        "collectionId": "ensaio-smoke", "relativePath": "notas/sessao-09.md",
        "title": "Sessão 9 — ensaio", "content": NOTE, "idempotencyKey": "smoke-2",
    })
    report.check(
        "reimportar texto igual não cria revisão",
        as_json(again)["source"]["currentRevision"] == body["source"]["currentRevision"],
        text_of(again)[:160],
    )

    whole = as_json(await call_tool(session, "read_source", {"sourceId": source_id}))
    report.check("leitura devolve o texto byte a byte", whole["content"] == NOTE, repr(whole)[:160])
    report.check("sem truncamento quando coube", whole["truncated"] is False, str(whole)[:160])
    return source_id


async def read_all_windows(session: ClientSession, source_id: str, first: dict) -> str:
    """Percorre as janelas fixando a revisão lida — é assim que o agent deve reler."""
    pieces = [first["content"]]
    window, offset, revision = first, first["nextOffset"], first["revision"]
    while window["truncated"]:
        window = as_json(await call_tool(session, "read_source", {
            "sourceId": source_id, "offset": offset, "maxChars": 17, "revision": revision,
        }))
        pieces.append(window["content"])
        offset = window["nextOffset"]
    return "".join(pieces)


async def check_windows(session: ClientSession, source_id: str, report: Reporter) -> None:
    first = as_json(await call_tool(
        session, "read_source", {"sourceId": source_id, "maxChars": 12}
    ))
    joined = await read_all_windows(session, source_id, first)
    report.check("continuação com revision fixado reconstrói o original", joined == NOTE, repr(joined))

    loose = await call_tool(
        session, "read_source", {"sourceId": source_id, "offset": first["nextOffset"]}
    )
    report.check(
        "continuar sem revision é recusado",
        loose.is_error and "revision" in text_of(loose),
        text_of(loose)[:160],
    )

    past = as_json(await call_tool(
        session, "read_source", {"sourceId": source_id, "offset": 10**6, "revision": first["revision"]}
    ))
    report.check(
        "offset além do fim aparece como fim do documento",
        past["offset"] == past["contentChars"] and past["content"] == "",
        str({key: past[key] for key in ("offset", "contentChars", "truncated")}),
    )


async def check_drafts(session: ClientSession, source_id: str, report: Reporter) -> str:
    created = await call_tool(session, "save_preparation_draft", {
        "payload": {"kind": "npc", "name": "Ferrarin", "role": "contrabandista"},
        "sourceRefs": [{"sourceId": source_id, "revision": 1}],
        "uncertainties": ["grafias conferidas na sessão seguinte"],
    })
    report.check("rascunho criado ligado à revisão", not created.is_error, text_of(created)[:160])
    draft = as_json(created)

    careless = await call_tool(session, "save_preparation_draft", {
        "draftId": draft["id"], "expectedRevision": draft["revision"],
        "payload": {"kind": "npc", "name": "Ferrarin", "role": "informante"},
    })
    report.check(
        "update sem sourceRefs é recusado",
        careless.is_error and "sourceRefs" in text_of(careless),
        text_of(careless)[:160],
    )
    still = as_json(await call_tool(session, "read_preparation_draft", {"draftId": draft["id"]}))
    report.check(
        "a recusa não apagou a procedência",
        still["sourceRefs"] == [{"sourceId": source_id, "revision": 1}]
        and still["revision"] == draft["revision"],
        str(still)[:200],
    )

    fixed = await call_tool(session, "save_preparation_draft", {
        "draftId": draft["id"], "expectedRevision": draft["revision"],
        "payload": {"kind": "npc", "name": "Ferrarin", "role": "informante"},
        "sourceRefs": [{"sourceId": source_id, "revision": 1}],
    })
    report.check("update com sourceRefs passa", not fixed.is_error, text_of(fixed)[:160])

    ghost = await call_tool(session, "apply_preparation_draft", {"draftId": draft["id"]})
    report.check(
        "não existe caminho MCP para aplicar",
        ghost.is_error and "unknown tool" in text_of(ghost).lower(),
        text_of(ghost)[:120],
    )
    return draft["id"]


async def check_connection(session: ClientSession, campaign_id: str, token: str, report: Reporter):
    status = await call_tool(session, "connection_status", {})
    body = as_json(status)
    report.check(
        "a credencial resolve a campanha de ensaio",
        body["campaign"]["id"] == campaign_id,
        str(body.get("campaign"))[:160],
    )
    report.check(
        "o MCP se declara sem poder de aplicar",
        body["permissions"]["applyDrafts"] is False and body["permissions"]["startCombat"] is False,
        str(body.get("permissions"))[:160],
    )
    report.check("a credencial nunca aparece na resposta", token not in text_of(status))
    return body


async def mcp_flow(base: str, endpoint: str, token: str, campaign_id: str, report: Reporter) -> str:
    async with httpx2.AsyncClient(
        base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=60.0
    ) as http, streamable_http_client(endpoint, http_client=http) as streams, \
            ClientSession(streams[0], streams[1]) as session:
        await session.initialize()
        await check_connection(session, campaign_id, token, report)
        await check_surface(session, report)
        source_id = await check_sources(session, report)
        await check_windows(session, source_id, report)
        return await check_drafts(session, source_id, report)


def check_apply_needs_a_human(rest: Rest, prep: dict[str, Any], campaign_id: str,
                              draft_id: str, report: Reporter) -> None:
    path = f"/campaigns/{campaign_id}/preparation/drafts/{draft_id}/apply"
    body = {"idempotencyKey": "smoke-apply", "acknowledgePlayerVisibility": True}
    with httpx2.Client(base_url=rest.base, timeout=30.0) as client:
        as_prep = client.post(path, json=body, headers={"Authorization": f"Bearer {prep['token']}"})
        as_gm = client.post(path, json=body, headers={"Authorization": f"Bearer {rest.token}"})
        npcs = client.get(f"/campaigns/{campaign_id}/npcs",
                          headers={"Authorization": f"Bearer {rest.token}"}).json()
    report.check(
        "a credencial de preparação não aplica",
        as_prep.status_code == 403 and "humana" in as_prep.text,
        f"{as_prep.status_code} {as_prep.text[:120]}",
    )
    report.check("o mestre aplica", as_gm.status_code == 201, f"{as_gm.status_code} {as_gm.text[:160]}")
    report.check("o NPC do ensaio virou registro oficial", any(n["name"] == "Ferrarin" for n in npcs),
                 str([n["name"] for n in npcs])[:160])


def check_revocation_is_immediate(base: str, endpoint: str, token: str, report: Reporter) -> None:
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "smoke", "version": "1"}},
    }
    with httpx2.Client(base_url=base, timeout=30.0) as client:
        response = client.post(endpoint, json=body, headers={
            "content-type": "application/json", "accept": "application/json",
            "Authorization": f"Bearer {token}",
        })
    report.check("credencial revogada para na hora", response.status_code == 401,
                 f"{response.status_code} {response.text[:120]}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=DEFAULT_BASE, help="URL do servidor")
    parser.add_argument("--campaign", default=ENSAIO, help="campanha de ENSAIO a usar")
    parser.add_argument("--keep", action="store_true", help="não revogar a credencial no fim")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    report = Reporter()
    print(f"smoke do MCP de preparação em {args.base}")

    email = input("e-mail do mestre: ").strip()
    password = getpass.getpass("senha (não aparece): ")
    session_token = login(args.base, email, password)
    report.check("login do mestre", bool(session_token))

    rest = Rest(args.base, session_token)
    campaign_id = ensure_campaign(rest, args.campaign)
    print(f"campanha de ensaio: {args.campaign} ({campaign_id})")

    prep = issue_prep_token(rest, campaign_id)
    print(f"credencial de preparação: {mask(prep['token'])}")
    endpoint = f"{args.base}/mcp"

    try:
        draft_id = await mcp_flow(args.base, endpoint, prep["token"], campaign_id, report)
        check_apply_needs_a_human(rest, prep, campaign_id, draft_id, report)
    finally:
        if args.keep:
            print("\ncrédencial mantida (--keep); revogue pelo app do mestre depois")
        else:
            rest.call("DELETE", f"/campaigns/{campaign_id}/preparation/tokens/{prep['id']}")
            check_revocation_is_immediate(args.base, endpoint, prep["token"], report)

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
