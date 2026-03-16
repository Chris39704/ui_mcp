"""Command line entry points."""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from ui_knowledge_service.app import create_app
from ui_knowledge_service.config import Settings
from ui_knowledge_service.mcp_server import build_mcp_server
from ui_knowledge_service.models import RefreshRequest
from ui_knowledge_service.service import KnowledgeService


def main() -> None:
    parser = argparse.ArgumentParser(description="UI knowledge service")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI + MCP server")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    subparsers.add_parser("stdio", help="Run the MCP server over stdio")
    subparsers.add_parser("prewarm", help="Populate the starter offline cache")

    refresh_parser = subparsers.add_parser("refresh", help="Refresh one component")
    refresh_parser.add_argument("library")
    refresh_parser.add_argument("component")
    refresh_parser.add_argument("--doc-type", default=None)
    refresh_parser.add_argument("--force", action="store_true")

    args = parser.parse_args()
    settings = Settings.from_env()

    if args.command == "serve":
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return
    if args.command == "stdio":
        asyncio.run(_run_stdio_command(settings))
        return

    asyncio.run(_run_async_command(args, settings))


def main_stdio() -> None:
    """Console script entrypoint that runs the MCP server over stdio."""

    settings = Settings.from_env()
    asyncio.run(_run_stdio_command(settings))


async def _run_async_command(args: argparse.Namespace, settings: Settings) -> None:
    service = KnowledgeService(settings)
    await service.startup()
    try:
        if args.command == "prewarm":
            result = await service.prewarm(force=False)
            print(result.model_dump_json(indent=2))
        elif args.command == "refresh":
            result = await service.refresh(
                RefreshRequest(
                    library=args.library,
                    component=args.component,
                    doc_type=args.doc_type,
                    force=args.force,
                )
            )
            print(result.model_dump_json(indent=2))
    finally:
        await service.shutdown()


async def _run_stdio_command(
    settings: Settings,
    *,
    service: KnowledgeService | None = None,
    mcp_factory=build_mcp_server,
) -> None:
    active_service = service or KnowledgeService(settings)
    await active_service.startup()
    try:
        mcp_server = mcp_factory(active_service)
        await mcp_server.run_stdio_async()
    finally:
        await active_service.shutdown()
