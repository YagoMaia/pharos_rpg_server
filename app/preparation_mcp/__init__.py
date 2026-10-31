"""Adaptador MCP de preparação: as ferramentas remotas do agent do mestre.

Etapa 3 do plano `plans/mcp-preparacao-campanha.md`. O MCP é um **adaptador fino**
sobre as stores de preparação que a REST já usa — não um segundo banco nem um
segundo conjunto de regras. A identidade vem da credencial de preparação,
resolvida no transporte, e a autorização continua nas stores (`assert_gm`), então
trocar de transporte não afrouxa nada.

Não existe ferramenta para aplicar rascunho, publicar, excluir ou iniciar combate:
isso é decisão humana, no app do mestre.
"""

from app.preparation_mcp.server import (
    TOOL_NAMES,
    PreparationMcpApp,
    build_preparation_mcp_asgi,
    mount_preparation_mcp,
)

__all__ = [
    "TOOL_NAMES",
    "PreparationMcpApp",
    "build_preparation_mcp_asgi",
    "mount_preparation_mcp",
]
