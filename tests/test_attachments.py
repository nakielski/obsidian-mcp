"""attach_file / list_attachments / read_attachment_info (rule 12)."""
import asyncio
import base64
import datetime
import io
import json
import pathlib
import sys
import tarfile
import zipfile

import frontmatter
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from obsidian_mcp import attachments, policies
from obsidian_mcp.vault import Vault

DAY = datetime.date(2026, 11, 4)
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, "x")
    return buf.getvalue()


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("VAULT_ACTOR", "claude")
    (tmp_path / "00 Inbox").mkdir()
    (tmp_path / "raw").mkdir()
    return Vault(tmp_path)


def _shard(root):
    f = root / ".vault-write-log.d" / "claude.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


class TestAttach:
    def test_inline_pdf(self, vault):
        info = vault.attach_file(
            filename="Rechnung Oktober.pdf", content=PDF, description="Telekom Rechnung Oktober.",
            source="E-Mail Telekom", related=["ent-telekom"], today=DAY,
        )
        assert info["file"] == "raw/assets/2026/11/2026-11-04_rechnung-oktober.pdf"
        assert info["note"] == info["file"] + ".md"
        assert (vault.root / info["file"]).read_bytes() == PDF
        post = frontmatter.load(vault.root / info["note"])
        assert post["type"] == "attachment" and post["uid"].startswith("att-20261104-")
        assert post["original_name"] == "Rechnung Oktober.pdf"
        assert post["mime"] == "application/pdf" and post["size_bytes"] == len(PDF)
        assert post["related"] == ["ent-telekom"] and post["actor"] == "claude"
        assert "![[2026-11-04_rechnung-oktober.pdf]]" in post.content
        assert "Telekom Rechnung Oktober." in post.content
        assert info["duplicate"] is False
        log = _shard(vault.root)
        paths = {(e["path"], e["action"], e["layer"]) for e in log}
        assert (info["file"], "create", "raw") in paths
        assert (info["note"], "create", "raw") in paths
        assert all(e["uid"] == post["uid"] for e in log)

    def test_duplicate_is_not_stored_twice(self, vault):
        first = vault.attach_file(filename="a.pdf", content=PDF, today=DAY)
        second = vault.attach_file(filename="anders.pdf", content=PDF, today=DAY)
        assert second["duplicate"] is True and second["file"] == first["file"]
        assert len(list((vault.root / "raw/assets/2026/11").iterdir())) == 2

    def test_name_collision_gets_suffix(self, vault):
        a = vault.attach_file(filename="scan.png", content=PNG, today=DAY)
        b = vault.attach_file(filename="scan.png", content=PNG + b"\x01", today=DAY)
        assert b["file"].endswith("2026-11-04_scan-2.png")
        assert a["file"] != b["file"]

    def test_custom_name_and_date_prefix_stripped(self, vault):
        info = vault.attach_file(filename="2026-10-01 Kontoauszug.PDF", content=PDF, today=DAY)
        assert info["file"].endswith("/2026-11-04_kontoauszug.pdf")
        info = vault.attach_file(filename="x.pdf", content=PDF + b"1", name="Vertrag Müller & Co", today=DAY)
        assert info["file"].endswith("/2026-11-04_vertrag-müller-co.pdf")

    def test_source_path_is_moved(self, vault):
        src = vault.root / "00 Inbox" / "Scan 1.pdf"
        src.write_bytes(PDF)
        info = vault.attach_file(source_path="00 Inbox/Scan 1.pdf", today=DAY)
        assert not src.exists()
        assert (vault.root / info["file"]).read_bytes() == PDF
        assert info["source_moved_from"] == "00 Inbox/Scan 1.pdf"
        assert frontmatter.load(vault.root / info["note"])["source"] == "vault:00 Inbox/Scan 1.pdf"
        assert any(e["action"] == "move" and e["path"] == "00 Inbox/Scan 1.pdf" for e in _shard(vault.root))

    def test_source_path_keep_source(self, vault):
        src = vault.root / "00 Inbox" / "liste.csv"
        src.write_text("a;b\n1;2\n", encoding="utf-8")
        info = vault.attach_file(source_path="00 Inbox/liste.csv", keep_source=True, today=DAY)
        assert src.exists() and info["source_moved_from"] == ""
        assert info["mime"] == "text/csv"

    def test_duplicate_source_stays(self, vault):
        vault.attach_file(filename="a.pdf", content=PDF, today=DAY)
        src = vault.root / "00 Inbox" / "kopie.pdf"
        src.write_bytes(PDF)
        info = vault.attach_file(source_path="00 Inbox/kopie.pdf", today=DAY)
        assert info["duplicate"] is True and src.exists()

    def test_attachment_is_immutable(self, vault):
        info = vault.attach_file(filename="a.pdf", content=PDF, today=DAY)
        with pytest.raises(policies.PolicyViolationError):
            vault.update(info["note"], "anders")

    def test_respects_foreign_scope_lock(self, vault):
        policies.lock_scope(vault.root, "raw/assets", actor="codex")
        with pytest.raises(policies.PolicyViolationError):
            vault.attach_file(filename="a.pdf", content=PDF, today=DAY)
        assert not (vault.root / "raw/assets/2026").exists() or not any(
            (vault.root / "raw/assets/2026").rglob("*")
        )

    def test_failed_note_rolls_back_moved_file(self, vault, monkeypatch):
        src = vault.root / "00 Inbox" / "a.pdf"
        src.write_bytes(PDF)

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(vault, "create", boom)
        with pytest.raises(OSError):
            vault.attach_file(source_path="00 Inbox/a.pdf", today=DAY)
        assert src.read_bytes() == PDF
        assert not list((vault.root / "raw/assets/2026/11").glob("*.pdf"))


class TestRefusals:
    @pytest.mark.parametrize("fname", ["setup.exe", "run.ps1", "x.bat", "tool.sh", "m.xlsm", "a.dll"])
    def test_executable_extension(self, vault, fname):
        with pytest.raises(policies.PolicyViolationError, match="Executable") as exc:
            vault.attach_file(filename=fname, content=b"MZ....", today=DAY)
        assert exc.value.policy == "ATTACHMENT_TYPE"

    def test_unknown_extension(self, vault):
        with pytest.raises(policies.PolicyViolationError, match="not allowed"):
            vault.attach_file(filename="x.iso", content=b"abc", today=DAY)

    @pytest.mark.parametrize("fname,data", [
        ("rechnung.pdf", b"MZ\x90\x00 disguised exe"),
        ("bild.png", b"\x7fELF\x02\x01"),
        ("notiz.txt", b"#!/bin/sh\nrm -rf /\n"),
        ("doku.pdf", b"not a pdf at all"),
        ("bild.jpg", PNG),
        ("text.txt", b"a\x00b"),
        ("x.svg", b"<svg><script>alert(1)</script></svg>"),
    ])
    def test_content_checks(self, vault, fname, data):
        with pytest.raises(policies.PolicyViolationError):
            vault.attach_file(filename=fname, content=data, today=DAY)

    def test_zip_with_executable(self, vault):
        with pytest.raises(policies.PolicyViolationError, match="executable"):
            vault.attach_file(filename="paket.zip", content=_zip(["readme.txt", "bin/run.exe"]), today=DAY)
        info = vault.attach_file(filename="paket.zip", content=_zip(["readme.txt", "a.pdf"]), today=DAY)
        assert info["mime"] == "application/zip"

    def test_tar_gz(self, vault):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"hello"
            ti = tarfile.TarInfo("ok.txt")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        info = vault.attach_file(filename="backup.tar.gz", content=buf.getvalue(), today=DAY)
        assert info["file"].endswith("_backup.tar.gz")

    def test_source_rules(self, vault):
        with pytest.raises(ValueError, match="exactly one"):
            vault.attach_file(filename="a.pdf", today=DAY)
        with pytest.raises(ValueError, match="exactly one"):
            vault.attach_file(filename="a.pdf", content=PDF, source_path="00 Inbox/a.pdf", today=DAY)
        (vault.root / "raw" / "x.pdf").write_bytes(PDF)
        with pytest.raises(ValueError, match="raw/"):
            vault.attach_file(source_path="raw/x.pdf", today=DAY)
        (vault.root / "00 Inbox" / "n.md").write_text("# n", encoding="utf-8")
        with pytest.raises(ValueError, match="note"):
            vault.attach_file(source_path="00 Inbox/n.md", today=DAY)
        with pytest.raises(FileNotFoundError):
            vault.attach_file(source_path="00 Inbox/fehlt.pdf", today=DAY)

    def test_size_limit_source(self, vault, monkeypatch):
        monkeypatch.setenv("ATTACH_MAX_MB", "1")
        (vault.root / "00 Inbox" / "gross.pdf").write_bytes(PDF + b"0" * (1024 * 1024))
        with pytest.raises(ValueError, match="exceeds 1 MB"):
            vault.attach_file(source_path="00 Inbox/gross.pdf", today=DAY)

    def test_base64_limits(self, monkeypatch):
        monkeypatch.setenv("ATTACH_MAX_INLINE_MB", "1")
        with pytest.raises(ValueError, match="exceeds 1 MB"):
            attachments.decode_base64(base64.b64encode(b"0" * (1024 * 1024 + 10)).decode())
        with pytest.raises(ValueError, match="not valid base64"):
            attachments.decode_base64("@@@")
        assert attachments.decode_base64("data:application/pdf;base64," + base64.b64encode(PDF).decode()) == PDF


class TestMime:
    def test_every_allowed_type_has_a_fixed_mime(self):
        for ext in attachments.ALLOWED_EXTENSIONS:
            assert attachments.mime_type(ext) != "application/octet-stream", ext
        assert attachments.mime_type(".zip") == "application/zip"


class TestListAndInfo:
    def test_list_filters_and_info(self, vault):
        a = vault.attach_file(filename="Rechnung.pdf", content=PDF, description="Telekom Oktober",
                              related=["ent-telekom"], today=DAY)
        vault.attach_file(filename="Foto.png", content=PNG, description="Zaun hinten", today=DAY)
        assert len(vault.list_attachments()) == 2
        assert [i["file"] for i in vault.list_attachments(related="telekom")] == [a["file"]]
        assert [i["original_name"] for i in vault.list_attachments(query="zaun")] == ["Foto.png"]
        info = vault.attachment_info(a["file"], verify=True)
        assert info["exists"] and info["integrity"] == "ok" and info["description"] == "Telekom Oktober"
        assert vault.attachment_info(a["note"])["uid"] == a["uid"]
        (vault.root / a["file"]).write_bytes(PDF + b"x")
        assert vault.attachment_info(a["file"], verify=True)["integrity"] == "mismatch"
        with pytest.raises(FileNotFoundError):
            vault.attachment_info("raw/assets/2026/11/fehlt.pdf")

    def test_companion_note_is_searchable(self, vault):
        vault.attach_file(filename="Vertrag.pdf", content=PDF, description="Mietvertrag Garage", today=DAY)
        assert any(n.path.endswith("vertrag.pdf.md") for n in vault.search("Mietvertrag"))


class TestToolsOverMcp:
    def _run(self, tool, **args):
        from obsidian_mcp.server import mcp

        res = asyncio.run(mcp.call_tool(tool, args))
        return res[1] if isinstance(res, tuple) else res

    def test_attach_list_info(self, vault):
        from obsidian_mcp.server import set_vault

        set_vault(vault)
        out = self._run("attach_file", filename="Plan.pdf", content_base64=base64.b64encode(PDF).decode(),
                        description="Bauplan", related_to="ent-20261104-bau")
        assert out["duplicate"] is False and out["file"].startswith("raw/assets/")
        assert all(e["related_to"] == "ent-20261104-bau" for e in _shard(vault.root))
        lst = self._run("list_attachments")
        assert lst["attachments"][0]["file"] == out["file"]
        info = self._run("read_attachment_info", path=out["file"], verify=True)
        assert info["integrity"] == "ok"

    def test_type_refusal_is_named(self, vault):
        from mcp.shared.exceptions import McpError

        from obsidian_mcp.server import mcp, set_vault

        set_vault(vault)
        with pytest.raises(Exception) as exc:
            asyncio.run(mcp.call_tool("attach_file", {"filename": "x.exe", "content_base64": "TVo="}))
        cause = exc.value if isinstance(exc.value, McpError) else exc.value.__cause__
        assert cause.error.message.startswith("Blocked by ATTACHMENT_TYPE")
