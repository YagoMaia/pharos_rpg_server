from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "RPG WebSocket Server"
    REDIS_URL: str = "redis://localhost:6379/0"
    CAMPAIGN_DB_PATH: str = "pharos.sqlite3"

    # MCP de preparação: o transporte valida Host/Origin (DNS rebinding), então a
    # lista precisa casar com o domínio atrás do túnel. Vazio = MCP desligado.
    MCP_PATH: str = "/mcp"
    MCP_ALLOWED_HOSTS: str = "pharos-server.yagomaia.com.br"
    MCP_ALLOWED_ORIGINS: str = ""

    class Config:
        env_file = ".env"

    def mcp_allowed_hosts(self) -> list[str]:
        return _csv(self.MCP_ALLOWED_HOSTS)

    def mcp_allowed_origins(self) -> list[str]:
        return _csv(self.MCP_ALLOWED_ORIGINS)


def _csv(value: str) -> list[str]:
    """Lista em variável de ambiente sem exigir JSON no compose."""
    return [item.strip() for item in (value or "").split(",") if item.strip()]


settings = Settings()
