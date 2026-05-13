import asyncio
import os
import shutil

import pytest


@pytest.mark.parametrize("size", [10, 1024])
def test_download_success_monkeypatched(tmp_path, monkeypatch, size):
    """
    Simulate a successful yt-dlp run by pre-creating a file in the dest dir and
    monkeypatching subprocess creation so no external program is invoked.
    """
    from src.downloader import download

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    file_path = dest_dir / "video123.mp4"
    file_path.write_bytes(b"x" * size)

    class DummyProc:
        def __init__(self):
            self.returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def fake_create(*args, **kwargs):
        return DummyProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    latest, meta = asyncio.run(
        download("http://example.com/video", str(dest_dir), timeout=2)
    )

    assert os.path.basename(latest) == "video123.mp4"
    assert meta["final_size"] == file_path.stat().st_size
    assert isinstance(meta.get("compressed"), bool)


def test_download_no_file(tmp_path, monkeypatch):
    """If no file appears in `dest_dir` after the (mocked) download, ensure an error is raised."""
    from src.downloader import download

    dest_dir = tmp_path / "empty"
    dest_dir.mkdir()

    class DummyProc:
        def __init__(self):
            self.returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def fake_create(*args, **kwargs):
        return DummyProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="no file downloaded"):
        asyncio.run(download("http://example.com/video", str(dest_dir), timeout=2))
