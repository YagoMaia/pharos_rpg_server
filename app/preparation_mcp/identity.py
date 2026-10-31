"""Quem está do outro lado do transporte MCP de preparação.

A identidade não vem de um argumento da ferramenta — viria do modelo, que pode
errar (ou ser induzido a errar) sobre qual mesa está escrevendo. Ela vem da
credencial de preparação, resolvida uma vez por requisição aqui, no transporte, e
entregue às ferramentas por dentro do ASGI `scope`.

A mensagem de recusa é deliberadamente genérica: existir, expirar ou estar
revogada é informação que quem chutou um token não precisa receber, e o próprio
token nunca é devolvido em erro ou log.
"""

from dataclasses import dataclass
from typing import Protocol

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

# chave do scope onde a identidade viaja; só este pacote lê e escreve
ACTOR_SCOPE_KEY = "pharos_prep_actor"

REFUSAL = "Credencial de preparação inválida, revogada ou ausente."
_CHALLENGE = 'Bearer realm="pharos-preparation"'


@dataclass(frozen=True)
class PreparationActor:
    """A credencial resolve exatamente uma campanha; a store ainda confere o GM."""

    user_id: str
    campaign_id: str
    token_id: str


class ResolvesPreparationTokens(Protocol):
    """O mínimo que a camada de transporte espera do `PreparationTokenStore`."""

    def resolve(self, token: str) -> dict[str, str] | None: ...


def bearer_of(scope: Scope) -> str | None:
    """O token do header `Authorization`, sem gravá-lo em lugar algum."""
    for name, value in scope.get("headers") or []:
        if name.decode("latin-1").lower() != "authorization":
            continue
        scheme, _, credential = value.decode("latin-1").partition(" ")
        if scheme.lower() == "bearer" and credential.strip():
            return credential.strip()
    return None


def actor_from_scope(scope: Scope) -> PreparationActor:
    """Identidade da requisição atual; falha alto se alguém montar sem autenticação."""
    actor = scope.get(ACTOR_SCOPE_KEY)
    if actor is None:
        raise RuntimeError(
            "requisição MCP sem credencial no scope: o middleware de autenticação "
            "precisa envolver o app do MCP"
        )
    return actor


class PreparationAuthMiddleware:
    """Camada ASGI pura: recusa antes de qualquer método do protocolo MCP.

    Resolver a credencial é SQLite bloqueante, então sai do event loop em
    `anyio.to_thread`. O `resolve` lê `revoked_at` a cada uso de propósito, e é
    isso que faz a revogação valer na hora também para uma sessão MCP aberta.
    """

    def __init__(self, app: ASGIApp, tokens: ResolvesPreparationTokens):
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        actor = await self._actor(bearer_of(scope))
        if actor is None:
            await self._refuse(scope, receive, send)
            return
        scope[ACTOR_SCOPE_KEY] = actor
        await self.app(scope, receive, send)

    async def _actor(self, token: str | None) -> PreparationActor | None:
        if token is None:
            return None
        resolved = await anyio.to_thread.run_sync(self.tokens.resolve, token)
        if not resolved:
            return None
        return PreparationActor(
            user_id=resolved["userId"],
            campaign_id=resolved["campaignId"],
            token_id=resolved["tokenId"],
        )

    @staticmethod
    async def _refuse(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": REFUSAL},
            status_code=401,
            headers={"WWW-Authenticate": _CHALLENGE},
        )
        await response(scope, receive, send)
