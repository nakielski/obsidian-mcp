"""Background updates of the semantic index: a write returns without waiting
for the embedding. Skipped when the 'semantic' extra is missing."""
import pathlib
import sys
import threading
import time

import pytest

pytest.importorskip("qdrant_client")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from qdrant_client import QdrantClient

from obsidian_mcp.semantic_index import EmbeddingError, SemanticConfig, SemanticIndex
from obsidian_mcp.vault import Vault

DIM = 8


class SlowEmbedder:
    """Blocks inside embed() until released, like Ollama loading its model."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.texts: list[str] = []
        self.threads: set[str] = set()
        self.fail = False

    def embed(self, texts):
        self.entered.set()
        self.threads.add(threading.current_thread().name)
        assert self.release.wait(10), "test never released the embedder"
        if self.fail:
            raise EmbeddingError("ollama down")
        self.texts.extend(texts)
        return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_SEMANTIC", "1")
    monkeypatch.setenv("OBSIDIAN_SEMANTIC_EXCLUDE", "Private")
    monkeypatch.delenv("OBSIDIAN_SEMANTIC_BACKGROUND", raising=False)
    vault = Vault(tmp_path)
    emb = SlowEmbedder()
    sx = SemanticIndex(vault.root, SemanticConfig.from_env(), client=QdrantClient(location=":memory:"),
                       embedder=emb, state_dir=tmp_path / ".obsidian-mcp-index")
    vault._semantic, vault._semantic_checked, vault.semantic_error = sx, True, ""
    yield vault, sx, emb
    emb.release.set()
    sx.drain(10)


def count(sx, path):
    if not sx._exists():
        return 0
    return sx.client.count(sx.collection, count_filter=sx._path_filter(path), exact=True).count


def test_background_is_the_default_and_can_be_switched_off(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_SEMANTIC_BACKGROUND", raising=False)
    assert SemanticConfig.from_env().background is True
    for off in ("0", "false", "off"):
        monkeypatch.setenv("OBSIDIAN_SEMANTIC_BACKGROUND", off)
        assert SemanticConfig.from_env().background is False


def test_write_returns_while_the_embedding_is_still_running(env):
    v, sx, emb = env
    start = time.monotonic()
    note = v.create("Notes/Kalt.md", "# Kalt\n\nOllama lädt gerade sein Modell.")
    assert time.monotonic() - start < 2  # the embedder is still blocked
    assert note.path == "Notes/Kalt.md" and pathlib.Path(v.root, note.path).exists()
    assert emb.entered.wait(5)
    # durable marker while the vectors are missing
    assert sx.pending.all() == ["Notes/Kalt.md"] and not sx.drain(0)
    emb.release.set()
    assert sx.drain(10)
    assert count(sx, "Notes/Kalt.md") == 1 and sx.pending.all() == []
    assert emb.threads == {"obsidian-mcp-semantic"}


def test_after_write_reports_queued(env):
    v, sx, emb = env
    emb.release.set()
    note = v.create("Notes/A.md", "# A\n\nText.")
    assert v._after_write("Notes/A.md", "modify", note)["semantic"] == "queued"
    assert sx.drain(10)


def test_writes_are_applied_in_order(env):
    v, sx, emb = env
    v.create("Notes/Folge.md", "# Folge\n\nErste Fassung.")
    v.update("Notes/Folge.md", "# Folge\n\nZweite Fassung.")
    v.create("Notes/Weg.md", "# Weg\n\nWird gleich gelöscht.")
    v.delete("Notes/Weg.md")
    emb.release.set()
    assert sx.drain(10)
    assert count(sx, "Notes/Folge.md") == 1 and count(sx, "Notes/Weg.md") == 0
    assert any("Zweite Fassung" in t for t in emb.texts)
    pts, _ = sx.client.scroll(sx.collection, limit=10, with_payload=True,
                              scroll_filter=sx._path_filter("Notes/Folge.md"))
    assert "Zweite Fassung" in pts[0].payload["text"]
    assert sx.pending.all() == []


def test_backend_failure_keeps_the_note_pending(env):
    v, sx, emb = env
    emb.fail = True
    emb.release.set()
    v.create("Notes/Offline.md", "# Offline\n\nOllama ist weg.")
    assert sx.drain(10)
    assert sx.pending.all() == ["Notes/Offline.md"] and sx.backend_down()
    # the worker is gone and starts again for the next write
    v.create("Notes/Offline2.md", "# Offline2\n\nAuch dieser wartet.")
    assert sx.drain(10)
    assert sorted(sx.pending.all()) == ["Notes/Offline.md", "Notes/Offline2.md"]
    emb.fail = False
    sx._down_until = 0
    assert sx.sync(v.list_notes())["complete"] and sx.pending.all() == []
    assert count(sx, "Notes/Offline.md") == 1


def test_excluded_folder_leaves_nothing_pending(env):
    v, sx, emb = env
    emb.release.set()
    v.create("Private/Geheim.md", "# Geheim\n\nNie einbetten.")
    assert sx.drain(10)
    assert emb.texts == [] and sx.pending.all() == [] and count(sx, "Private/Geheim.md") == 0


def test_sync_waits_for_the_worker(env):
    v, sx, emb = env
    v.create("Notes/Eins.md", "# Eins\n\nText.")
    assert emb.entered.wait(5)
    done = []
    t = threading.Thread(target=lambda: done.append(sx.sync(v.list_notes())))
    t.start()
    time.sleep(0.3)
    assert not done  # the worker holds the write lock
    emb.release.set()
    t.join(10)
    assert done and done[0]["complete"] and sx.pending.all() == []


def test_switched_off_updates_inside_the_write(env):
    v, sx, emb = env
    sx.cfg = SemanticConfig(**{**sx.cfg.__dict__, "background": False})
    emb.release.set()
    v.create("Notes/Direkt.md", "# Direkt\n\nText.")
    assert count(sx, "Notes/Direkt.md") == 1 and emb.threads == {"MainThread"}
