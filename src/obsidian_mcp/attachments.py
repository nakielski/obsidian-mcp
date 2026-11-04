"""Attachments: original files (PDF, Office, images, archives) filed under raw/.

Rule set (vault rule 12):

- Every attachment lives in ``raw/assets/<YYYY>/<MM>/`` and is named
  ``<YYYY-MM-DD>_<name>.<ext>`` (filing date, lowercase slug).
- Every attachment gets a companion note ``<file>.md`` next to it with uid,
  SHA-256, original name, MIME type, size, origin, related items and a
  description. The companion note is what search, links and the write log see.
- Both are raw/ and therefore immutable after filing. Corrections and
  evaluations belong in wiki/ or PARA notes that link the companion note.
- Identical content (same SHA-256) is filed once: a second attach returns the
  existing attachment instead of a copy.
- Only an allow-list of file types is accepted; executables are always
  refused, also when disguised behind an allowed extension.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import os
import re
import tarfile
import unicodedata
import zipfile
from pathlib import Path

import frontmatter

from . import policies

ATTACH_ROOT = "raw/assets"
UID_PREFIX = "att-"

ALLOWED_EXTENSIONS = (
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".txt", ".md", ".msg", ".eml",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".zip", ".7z", ".tar.gz",
)
# Never accepted, also not inside zip/tar archives.
EXECUTABLE_EXTENSIONS = frozenset({
    ".exe", ".dll", ".com", ".scr", ".msi", ".msp", ".bat", ".cmd", ".ps1",
    ".psm1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".cpl",
    ".jar", ".sh", ".bash", ".zsh", ".py", ".pl", ".rb", ".php", ".apk",
    ".app", ".dmg", ".pkg", ".deb", ".rpm", ".appimage", ".lnk", ".reg",
    ".so", ".dylib", ".bin", ".run", ".elf", ".xlsm", ".docm", ".pptm",
})
_EXEC_MAGIC = (
    b"MZ", b"\x7fELF", b"#!", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe",
)
_ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
_TEXT_TYPES = frozenset({".csv", ".txt", ".md", ".eml", ".svg"})
_EMBEDDABLE = frozenset({".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})
# Fixed map: mimetypes.guess_type() reads the Windows registry and returns
# platform-dependent values (e.g. application/x-zip-compressed for .zip).
_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".msg": "application/vnd.ms-outlook",
    ".eml": "message/rfc822",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".tar.gz": "application/gzip",
}
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[_ -]+")


def _mb_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


def max_bytes() -> int:
    """Size cap for source_path attachments (ATTACH_MAX_MB, default 100)."""
    return _mb_env("ATTACH_MAX_MB", 100) * 1024 * 1024


def max_inline_bytes() -> int:
    """Size cap for content_base64 attachments (ATTACH_MAX_INLINE_MB, default 10)."""
    return min(_mb_env("ATTACH_MAX_INLINE_MB", 10) * 1024 * 1024, max_bytes())


def split_extension(filename: str) -> tuple[str, str]:
    """Return (stem, lowercase extension); '.tar.gz' counts as one extension."""
    name = Path(filename.replace("\\", "/")).name
    low = name.lower()
    if low.endswith(".tar.gz") and len(name) > len(".tar.gz"):
        return name[: -len(".tar.gz")], ".tar.gz"
    stem, ext = os.path.splitext(name)
    return stem, ext.lower()


def check_extension(filename: str) -> str:
    stem, ext = split_extension(filename)
    if not ext or not stem.strip(" ."):
        raise ValueError(f"File name needs a name and an extension: {filename!r}")
    if ext in EXECUTABLE_EXTENSIONS:
        raise policies.PolicyViolationError(
            f"Executable files are never stored as attachments: {filename!r}.",
            policy="ATTACHMENT_TYPE",
        )
    if ext not in ALLOWED_EXTENSIONS:
        raise policies.PolicyViolationError(
            f"File type {ext} is not allowed as attachment. Allowed: "
            + ", ".join(ALLOWED_EXTENSIONS) + ".",
            policy="ATTACHMENT_TYPE",
        )
    return ext


def slugify(stem: str) -> str:
    """Lowercase, hyphenated, filesystem-safe name part (umlauts kept)."""
    s = unicodedata.normalize("NFC", stem).strip().lower()
    s = _DATE_PREFIX_RE.sub("", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^\w.-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return s[:80].rstrip("-._") or "datei"


def check_content(head: bytes, ext: str, path: Path | None = None, data: bytes | None = None) -> None:
    """Refuse executables and files whose content does not match the extension."""
    def refuse(msg: str) -> None:
        raise policies.PolicyViolationError(msg, policy="ATTACHMENT_TYPE")

    if any(head.startswith(m) for m in _EXEC_MAGIC):
        refuse("Content looks like an executable or script; attachments never store programs.")
    expected = {
        ".pdf": lambda h: b"%PDF-" in h[:1024],
        ".png": lambda h: h.startswith(b"\x89PNG\r\n\x1a\n"),
        ".jpg": lambda h: h.startswith(b"\xff\xd8\xff"),
        ".jpeg": lambda h: h.startswith(b"\xff\xd8\xff"),
        ".gif": lambda h: h.startswith((b"GIF87a", b"GIF89a")),
        ".webp": lambda h: h.startswith(b"RIFF") and h[8:12] == b"WEBP",
        ".docx": lambda h: h.startswith(_ZIP_MAGIC),
        ".xlsx": lambda h: h.startswith(_ZIP_MAGIC),
        ".pptx": lambda h: h.startswith(_ZIP_MAGIC),
        ".zip": lambda h: h.startswith(_ZIP_MAGIC),
        ".7z": lambda h: h.startswith(b"7z\xbc\xaf\x27\x1c"),
        ".tar.gz": lambda h: h.startswith(b"\x1f\x8b"),
        ".msg": lambda h: h.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    }.get(ext)
    if expected is not None and not expected(head):
        refuse(f"Content does not match the file type {ext}; check the file and its extension.")
    if ext in _TEXT_TYPES and b"\x00" in head:
        refuse(f"{ext} must be a text file, but the content is binary.")
    if ext == ".svg":
        text = head.decode("utf-8", errors="replace").lower()
        if "<svg" not in text:
            refuse("Content is not an SVG image.")
        if "<script" in text or "javascript:" in text:
            refuse("SVG with scripts is not stored as attachment.")
    if ext in {".zip", ".tar.gz"}:
        _check_archive_members(ext, path, data)


def _check_archive_members(ext: str, path: Path | None, data: bytes | None) -> None:
    import io

    src = path if path is not None else io.BytesIO(data or b"")
    try:
        if ext == ".zip":
            with zipfile.ZipFile(src) as zf:
                names = zf.namelist()
        else:
            with tarfile.open(fileobj=src if path is None else None, name=path, mode="r:gz") as tf:
                names = tf.getnames()
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
        raise policies.PolicyViolationError(
            f"Archive cannot be read ({type(exc).__name__}); not stored.",
            policy="ATTACHMENT_TYPE",
        )
    bad = [n for n in names if split_extension(n)[1] in EXECUTABLE_EXTENSIONS]
    if bad:
        raise policies.PolicyViolationError(
            "Archive contains executable files ("
            + ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else "")
            + "); not stored.",
            policy="ATTACHMENT_TYPE",
        )


def mime_type(ext: str) -> str:
    return _MIME_TYPES.get(ext, "application/octet-stream")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_base64(content_base64: str) -> bytes:
    raw = (content_base64 or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    limit = max_inline_bytes()
    if len(raw) > (limit * 4) // 3 + 16:
        raise ValueError(
            f"content_base64 exceeds {limit // (1024 * 1024)} MB. Put larger files into "
            "the vault (e.g. 00 Inbox/) and attach them with source_path."
        )
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"content_base64 is not valid base64: {exc}") from exc
    if not data:
        raise ValueError("Attachment is empty.")
    if len(data) > limit:
        raise ValueError(f"Attachment exceeds {limit // (1024 * 1024)} MB.")
    return data


def _sidecar_meta(note_path: Path) -> dict | None:
    try:
        post = frontmatter.load(note_path)
    except Exception:
        return None
    meta = dict(post.metadata)
    if str(meta.get("type", "")) != "attachment" or not meta.get("sha256"):
        return None
    meta["_body"] = post.content
    return meta


def iter_sidecars(vault_root: Path):
    """Yield (vault-relative sidecar path, metadata) for every companion note."""
    base = vault_root / ATTACH_ROOT
    if not base.is_dir():
        return
    for p in sorted(base.rglob("*.md")):
        if not p.is_file() or not split_extension(p.name[:-3])[1]:
            continue
        meta = _sidecar_meta(p)
        if meta is not None:
            yield str(p.relative_to(vault_root)).replace("\\", "/"), meta


def find_by_sha(vault_root: Path, sha: str) -> tuple[str, dict] | None:
    for rel, meta in iter_sidecars(vault_root):
        if str(meta.get("sha256", "")).lower() == sha:
            return rel, meta
    return None


def summary(rel_note: str, meta: dict) -> dict:
    folder = rel_note.rsplit("/", 1)[0]
    body = (meta.get("_body") or "").strip()
    desc_lines = [
        ln for ln in body.splitlines()
        if ln.strip() and not ln.startswith("#") and not ln.lstrip().startswith(("![[", "[["))
    ]
    related = meta.get("related") or []
    if isinstance(related, str):
        related = [related]
    return {
        "file": f"{folder}/{meta.get('file', '')}",
        "note": rel_note,
        "uid": str(meta.get("uid", "")),
        "original_name": str(meta.get("original_name", "")),
        "mime": str(meta.get("mime", "")),
        "size_bytes": int(meta.get("size_bytes", 0) or 0),
        "sha256": str(meta.get("sha256", "")),
        "source": str(meta.get("source", "")),
        "related": [str(r) for r in related],
        "created": str(meta.get("created", "")),
        "actor": str(meta.get("actor", "")),
        "description": " ".join(desc_lines)[:300],
    }


def build_sidecar(
    *, uid: str, file_name: str, ext: str, original_name: str, size: int, sha: str,
    source: str, related: list[str], description: str, actor: str, created: str,
) -> str:
    link = f"![[{file_name}]]" if ext in _EMBEDDABLE else f"[[{file_name}]]"
    body = f"# {original_name}\n\n{link}\n"
    if description.strip():
        body += f"\n{description.strip()}\n"
    post = frontmatter.Post(
        body,
        uid=uid,
        type="attachment",
        file=file_name,
        original_name=original_name,
        mime=mime_type(ext),
        size_bytes=size,
        sha256=sha,
        source=source,
        related=related,
        created=created,
        actor=actor,
        tags=["attachment"],
    )
    return frontmatter.dumps(post) + "\n"


def target_folder(today: datetime.date) -> str:
    return f"{ATTACH_ROOT}/{today:%Y}/{today:%m}"
