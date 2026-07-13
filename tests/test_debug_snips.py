import time
from pathlib import Path

from hark.debug_snips import purge_old_debug_snips, save_wake_snippet


def test_save_and_purge(tmp_path, monkeypatch):
    import hark.debug_snips as ds

    monkeypatch.setattr(ds, "debug_wake_dir", lambda: tmp_path / "wake")
    # minimal pcm silence
    pcm = b"\x00\x00" * 1600
    path = save_wake_snippet(
        pcm16=pcm,
        text="hey hook",
        matched=False,
        rms=0.01,
        backend="vosk",
        enabled=True,
    )
    assert path is not None and path.is_file()
    meta = path.with_suffix(".json")
    assert meta.is_file()

    # age the file
    old = time.time() - 8 * 86400
    import os

    os.utime(path, (old, old))
    os.utime(meta, (old, old))
    removed = purge_old_debug_snips(retention_days=7, root=tmp_path / "wake")
    assert removed >= 1
    assert not path.exists()
