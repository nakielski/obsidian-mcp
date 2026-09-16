"""Vault write policies: write-log, raw immutability, locking, index upkeep.

These hooks enforce vault conventions at the filesystem choke point of the
Vault class, so every client inherits them:

- WRITE_LOG: append a JSON line to .vault-write-log.jsonl for every write op
- RAW_IMMUTABLE: block update/delete on notes under raw/
- LOCK: refuse writes while a foreign .lock file younger than LOCK_MAX_AGE_MIN
  exists in the vault root
- INDEX: keep wiki/index.json in sync for writes under wiki/
- LINK_CHECK: validate new [[wikilink]] targets after a write (advisory)

Every policy is controlled by an environment variable and can be disabled
independently (default: enabled). They are deliberately fail-open for the
link check (advisory warnings) and fail-closed for raw immutability and locks.
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

WRITE_LOG_FILE = ".vault-write-log.jsonl"
WRITE_LOG_SHARD_DIR = ".vault-write-log.d"
LOCK_FILE = ".vault-lock"
LOCK_MAX_AGE_MIN = 30
INDEX_FILE = "wiki/index.json"
RAW_PREFIX = "raw/"
INDEX_GROUP_ORDER = ["comparisons", "concepts", "entities", "sources"]

_WIKILINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


class PolicyViolationError(Exception):
    """Raised when a write violates an enforced vault policy."""


def _flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def _shards_enabled() -> bool:
    """Per-actor shard files are the default; VAULT_WRITE_LOG_SHARDS=0 opts out."""
    return _flag("VAULT_WRITE_LOG_SHARDS")


# ---- 1. write log -------------------------------------------------------

def _sanitize_actor(actor: str) -> str:
    """Make an actor name safe for a filename component (shard naming)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", actor.strip())
    return cleaned[:48] or "unknown"


def log_write(
    vault_root: Path,
    rel_path: str,
    action: str,
    layer: str,
    actor: str | None = None,
    uid: str | None = None,
    related_to: str | None = None,
) -> dict:
    """Append one JSON line describing a completed write to the write log.

    Controlled by VAULT_WRITE_LOG (default on). Never raises: logging must not
    break a successful write.

    Concurrency model: appends go to a per-actor shard file under
    .vault-write-log.d/<actor>.jsonl (single-writer invariant). A Syncthing-
    synchronized vault cannot provide multi-writer append safety for ONE
    shared file: file-level sync replaces whole files, so concurrent appends
    from two devices silently lose each other's lines. Shard files are
    append-only from a single process family and therefore conflict-free.
    The legacy .vault-write-log.jsonl remains untouched for external writers
    (e.g. a differently-managed Windows installation).
    """
    if not _flag("VAULT_WRITE_LOG"):
        return {"logged": False}
    actor = actor or os.environ.get("VAULT_ACTOR", "unknown")
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "actor": actor,
        "layer": layer,
        "action": action,
        "path": rel_path,
        "uid": uid or "",
        "related_to": related_to or "",
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    if _shards_enabled():
        return _append_to_shard(vault_root, line, actor)
    return _append_to_legacy_file(vault_root, line)


def _append_to_shard(vault_root: Path, line: str, actor: str) -> dict:
    """Append to .vault-write-log.d/<actor>.jsonl (single-writer invariant)."""
    shard_dir = vault_root / WRITE_LOG_SHARD_DIR
    shard_path = shard_dir / (_sanitize_actor(actor) + ".jsonl")
    try:
        shard_dir.mkdir(parents=True, exist_ok=True)
        with open(shard_path, "a", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(line)
                    fh.flush()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            else:
                fh.write(line)
        return {"logged": True, "shard": shard_path.name}
    except OSError:
        # Shard dir not writable (e.g. read-only mount): fall back to the
        # legacy single file rather than losing the log line.
        return _append_to_legacy_file(vault_root, line)


def _append_to_legacy_file(vault_root: Path, line: str) -> dict:
    """Append to the legacy .vault-write-log.jsonl with flock."""
    log_path = vault_root / WRITE_LOG_FILE
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.write(line)
                    fh.flush()
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            else:
                fh.write(line)
        return {"logged": True, "shard": WRITE_LOG_FILE}
    except OSError:
        return {"logged": False}


# ---- 2. raw immutability ------------------------------------------------


SYSTEM_PREFIX = "90 System/"


def classify_layer(rel_path: str) -> str:
    """Classify a vault-relative path into its layer: raw, wiki, system, or para."""
    p = rel_path.replace("\\", "/").lstrip("/")
    if p.startswith(RAW_PREFIX):
        return "raw"
    if p.startswith("wiki/"):
        return "wiki"
    if p.startswith(SYSTEM_PREFIX):
        return "system"
    return "para"


def assert_mutable(rel_path: str) -> None:
    """Block update/delete on raw/ notes when RAW_IMMUTABLE is enabled."""
    if not _flag("RAW_IMMUTABLE"):
        return
    if classify_layer(rel_path) == "raw":
        raise PolicyViolationError(
            f"raw/ is immutable: {rel_path}. raw notes are original sources and "
            "must not be modified after creation. Store corrections or additions "
            "in a wiki/ or PARA note linking to the raw source instead. "
            "(Disable with RAW_IMMUTABLE=0.)"
        )


# ---- 3. archive helper --------------------------------------------------


def archive_target(rel_path: str, archive_dir: str) -> str:
    """Return the archive destination path for a note."""
    p = rel_path.replace("\\", "/").lstrip("/")
    return f"{archive_dir.rstrip('/')}/{Path(p).name}"


# ---- 4. lock ------------------------------------------------------------


def _lock_age_minutes(lock_path: Path) -> float:
    mtime = lock_path.stat().st_mtime
    age_sec = datetime.datetime.now().timestamp() - mtime
    return age_sec / 60.0


def parse_lock(lock_path: Path) -> dict:
    """Parse a .vault-lock file. Tolerates missing/malformed content."""
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def assert_no_foreign_lock(vault_root: Path, actor: str | None = None) -> dict | None:
    """Raise if a foreign, fresh lock exists. Returns lock info if own lock."""
    if not _flag("VAULT_LOCK"):
        return None
    actor = actor or os.environ.get("VAULT_ACTOR", "unknown")
    lock_path = vault_root / LOCK_FILE
    if not lock_path.exists():
        return None
    age = _lock_age_minutes(lock_path)
    holder = parse_lock(lock_path).get("agent", "unknown")
    if age <= LOCK_MAX_AGE_MIN and holder != actor:
        raise PolicyViolationError(
            f"Vault is locked by '{holder}' (lock age {age:.0f} min, max "
            f"{LOCK_MAX_AGE_MIN}). Wait for the other agent to finish or remove "
            f"{LOCK_FILE} if it is stale. (Disable locking with VAULT_LOCK=0.)"
        )
    return {"agent": holder, "age_min": age}


def acquire_lock(vault_root: Path, scope: str = "", actor: str | None = None) -> dict:
    """Create/refresh the .vault-lock file for this actor."""
    actor = actor or os.environ.get("VAULT_ACTOR", "unknown")
    lock = {
        "agent": actor,
        "started": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": scope,
    }
    lock_path = vault_root / LOCK_FILE
    lock_path.write_text(json.dumps(lock, ensure_ascii=False), encoding="utf-8")
    return lock


def release_lock(vault_root: Path, actor: str | None = None) -> bool:
    """Remove the .vault-lock file if it belongs to this actor."""
    actor = actor or os.environ.get("VAULT_ACTOR", "unknown")
    lock_path = vault_root / LOCK_FILE
    if not lock_path.exists():
        return False
    if parse_lock(lock_path).get("agent") == actor:
        lock_path.unlink(missing_ok=True)
        return True
    return False


# ---- 5. wiki index upkeep ----------------------------------------------


def _index_sort_key(entry: dict) -> tuple:
    path = entry.get("path", "")
    p = path.replace("\\", "/")
    if p.startswith("wiki/"):
        p = p[len("wiki/"):]
    parts = Path(p).parts
    group = parts[0].lower() if parts else ""
    group_rank = INDEX_GROUP_ORDER.index(group) if group in INDEX_GROUP_ORDER else len(INDEX_GROUP_ORDER)
    return (group_rank,) + tuple(x.lower() for x in parts)


def update_wiki_index(
    vault_root: Path,
    rel_path: str,
    title: str,
    note_type: str,
    status: str,
    uid: str | None = None,
    action: str = "modify",
) -> dict:
    """Insert, update, or remove an entry in wiki/index.json.

    Controlled by VAULT_INDEX_UPDATE (default on). Failures are returned, not
    raised: index upkeep must not break a completed write.
    """
    if not _flag("VAULT_INDEX_UPDATE"):
        return {"updated": False}
    if not rel_path.replace("\\", "/").lstrip("/").startswith("wiki/"):
        return {"updated": False, "reason": "not a wiki path"}
    idx_path = vault_root / INDEX_FILE
    entry_path = rel_path.replace("\\", "/").lstrip("/")
    entry_path = entry_path[len("wiki/"):] if entry_path.startswith("wiki/") else entry_path
    try:
        data = {"pages": [], "total_pages": 0}
        if idx_path.exists():
            try:
                data = json.loads(idx_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {"pages": [], "total_pages": 0}
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            data = {"pages": [], "total_pages": 0}

        pages = [p for p in data["pages"] if p.get("path") != entry_path]
        if action != "archive":
            pages.append({
                "path": entry_path,
                "title": title,
                "type": note_type or "page",
                "status": status or "active",
                "uid": uid or "",
            })
        pages.sort(key=_index_sort_key)
        data["pages"] = pages
        data["total_pages"] = len(pages)
        data["generated"] = datetime.datetime.now().isoformat(timespec="seconds")
        idx_path.parent.mkdir(parents=True, exist_ok=True)
        idx_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"updated": True, "total_pages": len(pages)}
    except OSError as e:
        return {"updated": False, "error": str(e)}


# ---- 6. link check ------------------------------------------------------


def extract_wikilinks(content: str) -> list[str]:
    """Extract wikilink targets from content (code blocks excluded)."""
    cleaned = re.sub(r"```.*?```", "", content, flags=re.DOTALL)
    cleaned = re.sub(r"`[^`\n]*`", "", cleaned)
    seen: set[str] = set()
    out: list[str] = []
    for m in _WIKILINK_RE.finditer(cleaned):
        t = m.group(1).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def check_links(
    vault_root: Path,
    content: str,
    known_titles: set[str],
) -> list[str]:
    """Return wikilink targets in content that do not resolve to known titles.

    Advisory only — never raises. Controlled by VAULT_LINK_CHECK (default on).
    """
    if not _flag("VAULT_LINK_CHECK"):
        return []
    targets = extract_wikilinks(content)
    lowered = {t.lower() for t in known_titles}
    return [t for t in targets if t.lower() not in lowered]
