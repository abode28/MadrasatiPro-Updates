# -*- coding: utf-8 -*-
"""Stamp a release version and bind the installed app to this public GitHub repository."""
from __future__ import annotations
import json, re, sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
if len(sys.argv) != 3:
    raise SystemExit("Usage: set_release_metadata.py VERSION OWNER/REPOSITORY")
version, repository = sys.argv[1].strip(), sys.argv[2].strip()
if not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit("Version must use x.y.z format")
if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
    raise SystemExit("GitHub repository must use owner/repository format")
parts = tuple(int(p) for p in version.split("."))
source = root / "school_manager.py"
content = source.read_text(encoding="utf-8")
content, count = re.subn(r'^APP_VERSION\s*=\s*"[^"]+"', f'APP_VERSION = "{version}"', content, count=1, flags=re.M)
if count != 1:
    raise SystemExit("APP_VERSION declaration was not found")
source.write_text(content, encoding="utf-8")

iss = root / "installer" / "SchoolManagerPro.iss"
content = iss.read_text(encoding="utf-8")
content, count = re.subn(r'^#define MyAppVersion "[^"]+"', f'#define MyAppVersion "{version}"', content, count=1, flags=re.M)
if count != 1:
    raise SystemExit("MyAppVersion declaration was not found")
iss.write_text(content, encoding="utf-8")

vi = root / "version_info.txt"
content = vi.read_text(encoding="utf-8")
vers = f"({parts[0]}, {parts[1]}, {parts[2]}, 0)"
content = re.sub(r'filevers=\([^\n]+\)', f'filevers={vers}', content, count=1)
content = re.sub(r'prodvers=\([^\n]+\)', f'prodvers={vers}', content, count=1)
content = re.sub(r"StringStruct\('FileVersion', '[^']+'\)", f"StringStruct('FileVersion', '{version}.0')", content, count=1)
content = re.sub(r"StringStruct\('ProductVersion', '[^']+'\)", f"StringStruct('ProductVersion', '{version}')", content, count=1)
vi.write_text(content, encoding="utf-8")

config = root / "update_config.json"
config.write_text(json.dumps({"repository": repository, "check_on_startup": True, "require_sha256": True}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"Prepared Madrasati Pro {version} for {repository}")
