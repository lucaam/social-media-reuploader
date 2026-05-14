import asyncio
import os
import shutil

import pytest

# Worker imports depend on prometheus_client via src.metrics. Skip these tests
# if prometheus_client is not available in the execution environment.
pytest.importorskip("prometheus_client")

from src.worker import WorkerPool


class DummyProc:
    def __init__(self, out_path=None):
        self.returncode = None
        self._out_path = out_path

    async def communicate(self):
        # simulate ffmpeg creating the thumbnail file
        try:
            if self._out_path:
                with open(self._out_path, "wb") as fh:
                    fh.write(b"\xff\xd8\xff")
        except Exception:
            pass
        self.returncode = 0
        return (b"", b"")


async def fake_create(*args, **kwargs):
    # last arg is expected to be the thumbnail destination in this command
    out = args[-1] if args else None
    return DummyProc(out)


def test_generate_thumbnail(monkeypatch, tmp_path):
    src = tmp_path / "video.mp4"
    src.write_bytes(b"video")
    dst = tmp_path

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    worker = WorkerPool("TOK", workers=1)
    thumb = asyncio.run(
        worker._generate_thumbnail("/usr/bin/ffmpeg", str(src), str(dst))
    )
    assert thumb is not None
    assert os.path.exists(thumb)
