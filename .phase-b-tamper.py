from __future__ import annotations

import os
import sys
from pathlib import Path

manifest = Path("/data/household.toml")
backup = Path("/data/household.toml.phase-b-backup")

if sys.argv[1:] == ["tamper"]:
    backup.write_bytes(manifest.read_bytes())
    os.chmod(backup, 0o600)
    lines = manifest.read_text(encoding="utf-8").splitlines()
    replaced = [
        'config_sha256 = "' + "0" * 64 + '"'
        if line.startswith("config_sha256 = ")
        else line
        for line in lines
    ]
    manifest.write_text("\n".join(replaced) + "\n", encoding="utf-8")
    os.chmod(manifest, 0o600)
elif sys.argv[1:] == ["restore"]:
    manifest.write_bytes(backup.read_bytes())
    os.chmod(manifest, 0o600)
    backup.unlink()
else:
    raise SystemExit("usage: phase-b-tamper.py tamper|restore")
