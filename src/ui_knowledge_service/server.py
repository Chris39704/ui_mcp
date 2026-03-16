"""ASGI entrypoint for direct uvicorn usage."""

from ui_knowledge_service.app import create_app


app = create_app()
