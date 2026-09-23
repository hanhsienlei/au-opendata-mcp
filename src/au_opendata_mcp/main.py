import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from au_opendata_mcp.api import STATUS_FOR, router
from au_opendata_mcp.deps import close_services
from au_opendata_mcp.errors import AuOpenDataError
from au_opendata_mcp.mcp_server import mcp

log = logging.getLogger(__name__)


def create_app() -> FastAPI:
    # FastMCP's HTTP app owns a session-manager lifespan. If it is not propagated to the
    # parent app, REST keeps working and every /mcp request fails with
    # "Task group is not initialized". See tests/test_app_assembly.py.
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.lifespan(app):
            yield
        await close_services()

    app = FastAPI(
        title="au-opendata-mcp",
        description="Australian government open data over MCP and REST.",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(AuOpenDataError)
    async def domain_error(_: Request, exc: AuOpenDataError) -> JSONResponse:
        status = STATUS_FOR.get(type(exc), 500)
        return JSONResponse(
            status_code=status,
            content={"detail": exc.message, "remedy_url": exc.remedy_url},
        )

    @app.exception_handler(ValueError)
    async def bad_argument(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.include_router(router)
    app.mount("/mcp", mcp_app)
    return app


def cli() -> None:
    parser = argparse.ArgumentParser(prog="au-opendata-mcp")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    cli()
