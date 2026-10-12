from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "RPG WebSocket Server"
    REDIS_URL: str = "redis://localhost:6379/0"
    CAMPAIGN_DB_PATH: str = "pharos.sqlite3"

    class Config:
        env_file = ".env"


settings = Settings()
