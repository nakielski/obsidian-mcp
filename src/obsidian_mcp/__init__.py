"""Obsidian MCP Server - expose an Obsidian vault to LLM clients."""
from .server import main
from .vault import Note, Vault

__version__ = "1.0.0"
__all__ = ["Vault", "Note", "main", "__version__"]
