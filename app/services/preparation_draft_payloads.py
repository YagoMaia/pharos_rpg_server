"""Contrato dos payloads de rascunho de preparação.

Etapa 4 do plano `plans/mcp-preparacao-campanha.md`. Existe em módulo próprio por
dois motivos: o arquivo do store ficaria acima do limite de 500 linhas do
`AGENTS.md`, e este é o contrato que o futuro adaptador MCP precisa importar para
validar exatamente as mesmas regras sem passar pelo HTTP.

Por que modelos explícitos e não `dict`: rascunho alimenta a aplicação
transacional da etapa 5. Contrato solto aqui vira registro oficial inventado ali.
Campos desconhecidos são recusados, e os limites de tamanho seguem os das rotas
existentes (`app/routers/campaigns.py`) para que aprovar um rascunho não falhe por
um campo que a entidade real não aceita.

Diferença deliberada em relação às rotas existentes: `NpcBody`/`QuestBody`
defaulteiam `visibility="party"`, enquanto rascunho defaulteia `"gm"`. Rascunho é
material do mestre até a revisão humana.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.campaign_store import CampaignError

MAX_SOURCE_REFS = 20
MAX_UNCERTAINTY_ITEMS = 20
MAX_UNCERTAINTY_CHARS = 500
MAX_MONSTER_ENTRIES = 50
# SQLite só vincula inteiro assinado de 64 bits: acima disso o binding estoura e o
# pedido viraria 500. O limite pertence à fronteira, não ao banco.
MAX_SQLITE_INTEGER = 2**63 - 1


class SessionPayload(BaseModel):
    """Sessão proposta. `summary` é visível aos jogadores ao aplicar (etapa 5)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["session"]
    sessionNumber: int = Field(ge=1, le=1000)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=20_000)
    scheduledAt: str | None = Field(default=None, max_length=64)
    status: Literal["planned", "active", "completed"] = "planned"


class EncounterPayload(BaseModel):
    """Encontro do mestre. Salvar rascunho não inicia combate algum."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["encounter"]
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=20_000)
    tactics: str = Field(default="", max_length=20_000)
    rewards: str = Field(default="", max_length=1_000)
    status: Literal["planned", "ready", "completed"] = "planned"
    monstersData: list[dict] = Field(default_factory=list, max_length=MAX_MONSTER_ENTRIES)


class NpcPayload(BaseModel):
    """NPC. Diferente de `NpcBody`, a visibilidade padrão é privada."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["npc"]
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=120)
    attitude: Literal["friendly", "neutral", "hostile", "unknown"] = "neutral"
    description: str = Field(default="", max_length=20_000)
    gmNotes: str = Field(default="", max_length=20_000)
    combatData: dict | None = None
    visibility: Literal["party", "gm"] = "gm"


class QuestPayload(BaseModel):
    """Missão. Diferente de `QuestBody`, a visibilidade padrão é privada."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["quest"]
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=20_000)
    reward: str = Field(default="", max_length=1_000)
    status: Literal["active", "completed", "failed"] = "active"
    visibility: Literal["party", "gm"] = "gm"


PAYLOAD_BY_KIND = {
    "session": SessionPayload,
    "encounter": EncounterPayload,
    "npc": NpcPayload,
    "quest": QuestPayload,
}


class SourceRef(BaseModel):
    """Vínculo a uma revisão imutável específica, não à fonte em geral."""

    model_config = ConfigDict(extra="forbid")

    sourceId: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1, le=MAX_SQLITE_INTEGER)


def parse_payload(raw: Any) -> BaseModel:
    """Valida o payload contra o modelo do seu tipo; nunca aceita dict solto."""
    if not isinstance(raw, dict):
        raise CampaignError(422, "payload deve ser um objeto JSON com 'kind'.")
    kind = raw.get("kind")
    # lookup de dicionário com valor não hashable (lista/objeto) estouraria antes
    # da validação: tudo que não for string cai no 422 com o tipo recebido
    model = PAYLOAD_BY_KIND.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise CampaignError(
            422,
            f"kind inválido: {kind!r}. Tipos aceitos: "
            f"{', '.join(sorted(PAYLOAD_BY_KIND))}.",
        )
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise CampaignError(422, f"payload de {kind} inválido -> {_describe(error)}") from error


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc']) or 'payload'}: {issue['msg']}"
        for issue in error.errors()
    )


def title_of(payload: BaseModel) -> str:
    """Título exibido na listagem, derivado do payload (sem campo redundante)."""
    return getattr(payload, "title", None) or payload.name  # type: ignore[attr-defined]


def exposure_warnings(kind: str, payload: dict[str, Any]) -> list[str]:
    """Avisos de visibilidade que a revisão humana precisa ver antes de aplicar.

    Verificado no código: `campaign_sessions` não tem coluna de visibilidade e
    `list_sessions` é lida por qualquer membro, então o `summary` de uma sessão é
    entregue aos jogadores **independentemente** do `status`. NPC e missão têm
    `visibility`, e `party` significa entrega imediata.
    """
    warnings = []
    if kind == "session" and payload.get("summary"):
        warnings.append(
            "summary de sessão não tem coluna de visibilidade: fica visível aos "
            "jogadores ao aplicar, qualquer que seja o status. Sanitizar antes."
        )
    if kind in ("npc", "quest") and payload.get("visibility") == "party":
        label = "NPC" if kind == "npc" else "Missão"
        warnings.append(f"{label} com visibility=party será entregue aos jogadores.")
    if kind == "encounter":
        warnings.append("encontro é do mestre; salvar rascunho não inicia combate.")
    return warnings


def clean_uncertainties(items: list[str] | None) -> list[str]:
    """Normaliza incertezas declaradas; recusa excesso, nunca trunca."""
    if items is None:
        return []
    if len(items) > MAX_UNCERTAINTY_ITEMS:
        raise CampaignError(
            422,
            f"uncertainties com {len(items)} itens excede o limite de "
            f"{MAX_UNCERTAINTY_ITEMS}.",
        )
    cleaned = []
    for item in items:
        text = (item or "").strip()
        if not text:
            continue
        if len(text) > MAX_UNCERTAINTY_CHARS:
            raise CampaignError(
                422,
                f"incerteza com {len(text)} caracteres excede o limite de "
                f"{MAX_UNCERTAINTY_CHARS}.",
            )
        cleaned.append(text)
    return cleaned
