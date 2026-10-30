"""Normalização canônica de nomes de sala WebSocket.

O gate de autenticação comparava o nome da sala com um prefixo sensível a caixa
(`startswith("campaign-")`), enquanto a inscrição no canal Redis normalizava o
mesmo nome para minúsculas. As duas decisões divergiam, e `CAMPAIGN-<uuid>`
conseguia entrar no canal de uma campanha autenticada sem passar pelo handshake.

Este módulo existe para que a validação aconteça **uma única vez**, e para que a
decisão do ramo e a inscrição usem exatamente o mesmo valor.
"""

from uuid import UUID

CAMPAIGN_PREFIX = "campaign-"


def canonical_campaign_room(session_id: str) -> str | None:
    """Retorna a sala no formato canônico `campaign-<uuid minúsculo>`, ou None.

    >>> canonical_campaign_room("CAMPAIGN-00000000-0000-4000-8000-000000000000")
    'campaign-00000000-0000-4000-8000-000000000000'
    >>> canonical_campaign_room("MESA_01") is None
    True
    """
    candidate = session_id.lower()
    if not candidate.startswith(CAMPAIGN_PREFIX):
        return None
    campaign_id = candidate.removeprefix(CAMPAIGN_PREFIX)
    try:
        if str(UUID(campaign_id)) != campaign_id:
            return None
    except ValueError:
        return None
    return f"{CAMPAIGN_PREFIX}{campaign_id}"
