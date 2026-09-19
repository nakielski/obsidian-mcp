"""Obsidian MCP Server - expose an Obsidian vault to LLM clients."""
__version__ = "1.1.0"

from .server import main  # noqa: E402  (needs __version__ defined above)
from .vault import Note, Vault  # noqa: E402

__all__ = ["Vault", "Note", "main", "__version__"]
