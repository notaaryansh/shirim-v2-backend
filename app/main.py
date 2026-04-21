import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import FRONTEND_ORIGINS
from .routes import auth as auth_routes
from .routes import edit as edit_routes
from .routes import install as install_routes
from .routes import launch as launch_routes
from .routes import repos
from .routes import search as search_routes
from .routes import secrets as secrets_routes

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
# edit_routes and launch_routes MUST come before install_routes — all share
# /api/v1/install/ prefix, and install's POST /{owner}/{repo} wildcard would
# swallow their POST /{install_id}/edit and /{install_id}/run if registered first.
app.include_router(edit_routes.router, prefix="/api")
app.include_router(launch_routes.router, prefix="/api")
app.include_router(install_routes.router, prefix="/api")
app.include_router(search_routes.router, prefix="/api")
app.include_router(secrets_routes.router, prefix="/api")


@app.get("/health")
async def health():
    return {"ok": True}
