"""HTTP API. Every route lives on one router, mounted under PATH_PREFIX in `create_app`."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import AppSettings
from app.db import get_engine
from app.models import Item
from app.schemas import ItemIn, ItemOut

router = APIRouter()


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def _get_item_or_404(session: Session, item_id: int) -> Item:
    item = session.get(Item, item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"item {item_id} not found")
    return item


@router.get("/healthz")
def healthz() -> dict[str, str]:
    # Deliberately no DB check: this is the ALB health check, and a DB blip must not
    # make the ALB replace every task at once. DB reachability is /readyz's job.
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request, response: Response) -> dict[str, str]:
    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}


@router.get("/version")
def version(request: Request) -> dict[str, str]:
    settings: AppSettings = request.app.state.settings
    return {
        "service": settings.service_name,
        "env": settings.env_name,
        "branch": settings.git_branch,
        "sha": settings.git_sha,
    }


@router.post("/items", status_code=status.HTTP_201_CREATED)
def create_item(body: ItemIn, session: SessionDep) -> ItemOut:
    item = Item(name=body.name, description=body.description)
    session.add(item)
    session.commit()
    return ItemOut.model_validate(item)


@router.get("/items")
def list_items(session: SessionDep) -> list[ItemOut]:
    items = session.scalars(select(Item).order_by(Item.id))
    return [ItemOut.model_validate(item) for item in items]


@router.get("/items/{item_id}")
def get_item(item_id: int, session: SessionDep) -> ItemOut:
    return ItemOut.model_validate(_get_item_or_404(session, item_id))


@router.put("/items/{item_id}")
def replace_item(item_id: int, body: ItemIn, session: SessionDep) -> ItemOut:
    item = _get_item_or_404(session, item_id)
    item.name = body.name
    item.description = body.description
    session.commit()
    return ItemOut.model_validate(item)


@router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int, session: SessionDep) -> None:
    session.delete(_get_item_or_404(session, item_id))
    session.commit()


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or AppSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Creating the engine does not connect, so the app starts (and /healthz answers)
        # even while the DB is unreachable.
        app.state.engine = get_engine()
        yield
        app.state.engine.dispose()

    prefix = settings.path_prefix
    app = FastAPI(
        title=settings.service_name,
        lifespan=lifespan,
        # The ALB only forwards PATH_PREFIX/*, so the docs must live under it too.
        docs_url=f"{prefix}/docs",
        redoc_url=None,
        openapi_url=f"{prefix}/openapi.json",
    )
    app.state.settings = settings
    app.include_router(router, prefix=prefix)
    return app
