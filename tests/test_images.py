"""note_images / read_image: pictures embedded in notes, handed out as images."""
import asyncio
import base64
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.exceptions import McpError

from obsidian_mcp import images
from obsidian_mcp.server import mcp, set_vault
from obsidian_mcp.vault import Vault

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24

NOTE = """# Messung

Vorher: ![[vorher.png]] und skaliert ![[plots/nachher.jpg|400]].
Markdown: ![Kurve](plots/kurve.gif) und ![mit Leerzeichen](<Bild mit Leerzeichen.webp>).
Kodiert: ![x](Bild%20mit%20Leerzeichen.webp "Titel")
Aus dem Netz: ![logo](https://example.org/logo.png) ![d](data:image/png;base64,AAAA)
Anderswo: ![[irgendwo.png]] und vom Stamm ![[Anhang/stamm.png]]
Kein Bild: ![[Andere Notiz]] ![[skizze.svg]] ![[bericht.pdf]]
Fehlt: ![[gibt-es-nicht.png]]
Doppelt: ![[vorher.png]]

```
![[im-code.png]]
```
"""


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_SEARCH_RESTRICTED", "05 People")
    monkeypatch.delenv("OBSIDIAN_IMAGE_MAX_MB", raising=False)
    v = Vault(tmp_path)
    v.create("Projekt/Messung.md", NOTE)
    for rel, data in (
        ("Projekt/vorher.png", PNG),
        ("Projekt/plots/nachher.jpg", JPEG),
        ("Projekt/plots/kurve.gif", GIF),
        ("Projekt/Bild mit Leerzeichen.webp", WEBP),
        ("Archiv/tief/irgendwo.png", PNG),
        ("Bilder/irgendwo.png", PNG),
        ("Anhang/stamm.png", PNG),
        ("Projekt/im-code.png", PNG),
        ("Projekt/skizze.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>"),
        ("05 People/anna.png", PNG),
        (".obsidian/plugins/x/icon.png", PNG),
    ):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    set_vault(v)
    return v


def call(tool, **args):
    return asyncio.run(mcp.call_tool(tool, args))


def data_of(result):
    return json.loads(result[0][0].text)


class TestEmbeddedTargets:
    def test_order_dedupe_and_filters(self):
        targets, external = images.embedded_targets(NOTE)
        assert targets == [
            "vorher.png",
            "plots/nachher.jpg",
            "plots/kurve.gif",
            "Bild mit Leerzeichen.webp",
            "irgendwo.png",
            "Anhang/stamm.png",
            "gibt-es-nicht.png",
        ]
        assert external == 2

    def test_candidates_never_leave_the_vault(self):
        assert images.candidates("plots/a.png", "Projekt/Messung.md") == ["Projekt/plots/a.png", "plots/a.png"]
        assert images.candidates("a.png", "Messung.md") == ["a.png"]
        assert images.candidates("/a.png", "Projekt/Messung.md") == ["Projekt/a.png", "a.png"]
        for bad in ("../a.png", "x/../../a.png", "./a.png", ""):
            assert images.candidates(bad, "Projekt/Messung.md") == []


class TestNoteImages:
    def test_resolves_like_obsidian(self, vault):
        out = vault.note_images("Projekt/Messung.md")
        got = {i["link"]: i for i in out["images"]}
        assert out["note"] == "Projekt/Messung.md"
        assert got["vorher.png"]["path"] == "Projekt/vorher.png"
        assert got["plots/nachher.jpg"]["path"] == "Projekt/plots/nachher.jpg"
        assert got["plots/nachher.jpg"]["mime"] == "image/jpeg"
        assert got["plots/kurve.gif"]["path"] == "Projekt/plots/kurve.gif"
        assert got["Bild mit Leerzeichen.webp"]["path"] == "Projekt/Bild mit Leerzeichen.webp"
        # by file name anywhere in the vault: the shortest path wins
        assert got["irgendwo.png"]["path"] == "Bilder/irgendwo.png"
        assert got["Anhang/stamm.png"]["path"] == "Anhang/stamm.png"
        assert got["gibt-es-nicht.png"] == {
            "link": "gibt-es-nicht.png", "path": "", "mime": "", "size_bytes": 0,
            "readable": False, "reason": "not found",
        }
        assert all(i["readable"] and i["size_bytes"] > 0 for i in out["images"] if i["path"])
        assert out["external_skipped"] == 2 and out["truncated"] is False
        assert "im-code.png" not in got and "skizze.svg" not in got

    def test_size_limit_is_reported(self, vault, monkeypatch):
        (vault.root / "Projekt/vorher.png").write_bytes(PNG + b"\x00" * (1024 * 1024))
        monkeypatch.setenv("OBSIDIAN_IMAGE_MAX_MB", "1")
        first = vault.note_images("Projekt/Messung.md")["images"][0]
        assert first["readable"] is False and first["reason"] == "larger than 1 MB"

    def test_restricted_images_are_left_out(self, vault):
        vault.create("Notes/Treffen.md", "![[anna.png]] ![[vorher.png]]")
        out = data_of(call("note_images", path="Notes/Treffen.md"))
        assert [i["path"] for i in out["images"]] == ["Projekt/vorher.png"]
        assert out["restricted_hidden"] is True
        full = data_of(call("note_images", path="Notes/Treffen.md", include_restricted=True))
        assert [i["path"] for i in full["images"]] == ["05 People/anna.png", "Projekt/vorher.png"]

    def test_restricted_note_needs_the_flag(self, vault):
        vault.create("05 People/Anna.md", "![[anna.png]]")
        with pytest.raises((McpError, ToolError), match="restricted"):
            call("note_images", path="05 People/Anna.md")
        out = data_of(call("note_images", path="05 People/Anna.md", include_restricted=True))
        assert out["images"][0]["path"] == "05 People/anna.png"

    def test_internal_folders_are_never_searched(self, vault):
        vault.create("Notes/Icon.md", "![[icon.png]]")
        out = vault.note_images("Notes/Icon.md")
        assert out["images"][0]["reason"] == "not found"

    def test_many_embeds_are_capped(self, vault):
        vault.create("Notes/Viele.md", " ".join(f"![[b{i}.png]]" for i in range(60)))
        out = vault.note_images("Notes/Viele.md")
        assert len(out["images"]) == images.MAX_EMBEDS and out["truncated"] is True


class TestReadImage:
    def test_returns_an_image(self, vault):
        result = call("read_image", path="Projekt/plots/nachher.jpg")
        content = result[0] if isinstance(result, tuple) else result
        part = content[0]
        assert part.type == "image" and part.mimeType == "image/jpeg"
        assert base64.b64decode(part.data) == JPEG

    @pytest.mark.parametrize("rel,mime", [
        ("Projekt/vorher.png", "image/png"),
        ("Projekt/plots/kurve.gif", "image/gif"),
        ("Projekt/Bild mit Leerzeichen.webp", "image/webp"),
    ])
    def test_every_type(self, vault, rel, mime):
        data, got, norm = vault.read_image(rel)
        assert got == mime and norm == rel and data

    @pytest.mark.parametrize("rel", [
        "Projekt/skizze.svg",          # no SVG
        "Projekt/Messung.md",          # not an image
        "Projekt/vorher",              # would resolve to a note
        "../aussen.png",
        "Projekt/../../aussen.png",
        ".obsidian/plugins/x/icon.png",
    ])
    def test_refused_paths(self, vault, rel):
        with pytest.raises((ValueError, PermissionError)):
            vault.read_image(rel)

    def test_absolute_path_is_refused(self, vault):
        with pytest.raises((ValueError, PermissionError)):
            vault.read_image(str(vault.root / "Projekt/vorher.png"))

    def test_missing_file(self, vault):
        with pytest.raises(FileNotFoundError):
            vault.read_image("Projekt/fehlt.png")

    def test_content_must_match_extension(self, vault):
        (vault.root / "Projekt/falsch.png").write_bytes(b"<html><script>alert(1)</script></html>")
        (vault.root / "Projekt/falsch.webp").write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt ")
        (vault.root / "Projekt/falsch.jpg").write_bytes(PNG)
        for rel in ("Projekt/falsch.png", "Projekt/falsch.webp", "Projekt/falsch.jpg"):
            with pytest.raises(ValueError, match="does not match"):
                vault.read_image(rel)

    def test_size_limit(self, vault, monkeypatch):
        (vault.root / "Projekt/gross.png").write_bytes(PNG + b"\x00" * (1024 * 1024))
        monkeypatch.setenv("OBSIDIAN_IMAGE_MAX_MB", "1")
        with pytest.raises(ValueError, match="too large"):
            vault.read_image("Projekt/gross.png")
        monkeypatch.setenv("OBSIDIAN_IMAGE_MAX_MB", "kaputt")
        assert images.max_bytes() == 10 * 1024 * 1024

    def test_restricted_folder_needs_the_flag(self, vault):
        with pytest.raises((McpError, ToolError), match="restricted"):
            call("read_image", path="05 People/anna.png")
        result = call("read_image", path="05 People/anna.png", include_restricted=True)
        content = result[0] if isinstance(result, tuple) else result
        assert content[0].type == "image"

    def test_symlink_out_of_the_vault_is_refused(self, vault, tmp_path_factory):
        outside = tmp_path_factory.mktemp("aussen") / "geheim.png"
        outside.write_bytes(PNG)
        link = vault.root / "Projekt/link.png"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not available")
        with pytest.raises(PermissionError):
            vault.read_image("Projekt/link.png")

    def test_nothing_is_written(self, vault):
        before = sorted(p.name for p in vault.root.rglob("*"))
        vault.note_images("Projekt/Messung.md")
        vault.read_image("Projekt/vorher.png")
        assert sorted(p.name for p in vault.root.rglob("*")) == before
