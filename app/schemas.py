from typing import Literal

from pydantic import BaseModel


class SmartSummary(BaseModel):
    tagline: str
    description: str
    features: list[str]
    categories: list[str]
    install_difficulty: Literal["easy", "medium", "hard"]
    requirements: list[str]


class Repository(BaseModel):
    id: int
    name: str
    repo: str
    desc: str
    language: str
    stars: str
    summary: SmartSummary | None = None


class CategoryBlock(BaseModel):
    name: str
    repos: list[Repository]


class TabResponse(BaseModel):
    tab: Literal["home", "discover"]
    categories: list[CategoryBlock]


class RepoDetail(Repository):
    images: list[str]
    github_url: str
