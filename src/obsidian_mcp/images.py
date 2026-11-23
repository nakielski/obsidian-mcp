"""Images embedded in notes: find them and hand them out as images.

Two read-only operations for clients that show pictures (a HUD, a chat UI):

* list the images a note embeds (``![[plot.png]]``, ``![alt](img/plot.png)``),
  resolved the way Obsidian resolves them, and
* return one image file as bytes.

Only raster formats a browser can show and that cannot carry script: PNG, JPEG,
GIF, WebP. No SVG. The content is checked against the extension, so a renamed
file is refused. Files above the size limit are listed but not returned.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}
MAX_EMBEDS = 50

# ![[target]], ![[target|300]], ![[target#anchor]]
_WIKI_EMBED = re.compile(r"!\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
# ![alt](target), ![alt](<target with spaces>), ![alt](target "title")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^)\s]+))(?:\s+\"[^\"]*\")?\s*\)")


def max_bytes() -> int:
    """Size limit for one image (OBSIDIAN_IMAGE_MAX_MB, default 4, at least 1)."""
    try:
        mb = int(os.environ.get("OBSIDIAN_IMAGE_MAX_MB", "4"))
    except ValueError:
        mb = 4
    return max(1, mb) * 1024 * 1024


def image_mime(path: str | Path) -> str | None:
    """MIME type by extension, None when the file is not a supported image."""
    return IMAGE_TYPES.get(Path(str(path)).suffix.lower())


def check_content(data: bytes, mime: str) -> None:
    """Raise ValueError unless the first bytes match the type the extension claims."""
    if not any(data.startswith(m) for m in _MAGIC.get(mime, ())):
        raise ValueError("File content does not match its image extension")
    if mime == "image/webp" and data[8:12] != b"WEBP":
        raise ValueError("File content does not match its image extension")


def _strip_code(content: str) -> str:
    cleaned = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    return re.sub(r"`[^`]+`", "", cleaned)


def embedded_targets(content: str) -> tuple[list[str], int]:
    """Image link targets of a note in order of appearance, plus the count of web images.

    Code blocks are ignored. Links to the web (http, https, data and other
    schemes) are never followed, only counted. Duplicates are dropped.
    """
    cleaned = _strip_code(content)
    found: list[tuple[int, str]] = []
    for m in _WIKI_EMBED.finditer(cleaned):
        found.append((m.start(), m.group(1).strip()))
    for m in _MD_IMAGE.finditer(cleaned):
        found.append((m.start(), unquote((m.group(1) or m.group(2) or "").strip())))
    targets: list[str] = []
    seen: set[str] = set()
    external = 0
    for _, target in sorted(found):
        if not target:
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            external += 1
            continue
        if image_mime(target) is None:
            continue  # embedded notes, PDFs, SVG and the like
        key = target.replace("\\", "/").casefold()
        if key not in seen:
            seen.add(key)
            targets.append(target.replace("\\", "/"))
    return targets, external


def candidates(target: str, note_rel: str) -> list[str]:
    """Vault-relative paths to try for a link target, most specific first.

    Obsidian accepts a path from the vault root, a path relative to the note
    and a bare file name that lives anywhere in the vault. The first two are
    returned here; the search by name is done by the caller.
    """
    clean = target.strip().lstrip("/")
    parts = [s for s in clean.split("/") if s]
    if not parts or any(s in (".", "..") for s in parts):
        return []
    clean = "/".join(parts)
    folder = note_rel.replace("\\", "/").rpartition("/")[0]
    out = []
    if folder:
        out.append(f"{folder}/{clean}")
    out.append(clean)
    return out
