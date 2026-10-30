import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.preparation_mcp import mount_preparation_mcp
from app.routers import campaigns, preparation, websocket
from app.routers.campaigns import get_campaign_store
from app.routers.preparation import get_preparation_store
from app.services.campaign_store import CampaignError

app = FastAPI(title="RPG Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(websocket.router)
app.include_router(campaigns.router)
app.include_router(preparation.router)

# MCP das notas do mestre: mesmo processo, mesmas stores, autenticação própria.
# Sem hosts liberados na configuração o endpoint simplesmente não existe.
if settings.mcp_allowed_hosts():
    mount_preparation_mcp(
        app,
        get_campaign_store,
        get_preparation_store,
        path=settings.MCP_PATH,
        allowed_hosts=settings.mcp_allowed_hosts(),
        allowed_origins=settings.mcp_allowed_origins(),
    )


@app.exception_handler(CampaignError)
async def campaign_error(_request: Request, error: CampaignError):
    return JSONResponse(status_code=error.status, content={"detail": str(error)})


@app.get("/")
def ping():
    return "Pong"


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
