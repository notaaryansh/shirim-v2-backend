import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import FRONTEND_ORIGINS
from .routes import agent as agent_routes
from .routes import auth as auth_routes
from .routes import repos
from .routes import search as search_routes

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="shirim-v2-backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

app.include_router(repos.router, prefix="/api")
app.include_router(auth_routes.router, prefix="/api")
app.include_router(search_routes.router, prefix="/api")
app.include_router(agent_routes.router, prefix="/api")


@app.get("/health")
async def health():
    return {"ok": True}
