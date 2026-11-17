#!/usr/bin/env python3
"""Build a clean installation ZIP of the K70 AI Copilot extension (not a publish action).

Checks: every file the manifest references exists; only the expected permissions are requested;
no secrets/.env/dev files are packaged; no forbidden send APIs in any shipped script.
Usage: python3 scripts/package_k70.py [--check-only]
"""
import json, re, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "services" / "extension"
DIST = ROOT / "dist"
ALLOWED_PERMS = {"storage"}
FORBIDDEN = [r"\.click\s*\(", r"\.submit\s*\(", r"requestSubmit", r"new\s+(Keyboard|Mouse|Pointer)Event", r"sendBeacon", r"WebSocket"]
SECRET = re.compile(r"\b[0-9a-f]{40,}\b|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9_-]{20,}")


def referenced(m):
    files = {"manifest.json", m["background"]["service_worker"], m["options_ui"]["page"], "options.js"}
    for c in m["content_scripts"]:
        files |= set(c.get("js", [])) | set(c.get("css", []))
    files |= set(m.get("icons", {}).values())
    return sorted(files)


def check():
    m = json.loads((EXT / "manifest.json").read_text())
    assert m["manifest_version"] == 3
    assert set(m.get("permissions", [])) <= ALLOWED_PERMS, m.get("permissions")
    files = referenced(m)
    for f in files:
        assert (EXT / f).is_file(), "manifest references a missing file: " + f
    for f in files:
        if f.endswith(".js"):
            code = re.sub(r"/\*.*?\*/", "", (EXT / f).read_text(), flags=re.S)
            code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("//"))
            for pat in FORBIDDEN:
                assert not re.search(pat, code), f"forbidden API {pat} in {f}"
        if f.endswith((".js", ".json", ".html", ".css")):
            assert not SECRET.search((EXT / f).read_text()), "possible secret in " + f
    hosts = sorted({u for c in m["content_scripts"] for u in c["matches"]})
    print("PASS manifest v%s | permissions: %s | sites: %s" % (m["version"], sorted(m.get("permissions", [])), ", ".join(hosts)))
    return m, files


def build(m, files):
    DIST.mkdir(exist_ok=True)
    out = DIST / ("k70-ai-copilot-%s.zip" % m["version"])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(EXT / f, f)
    with zipfile.ZipFile(out) as z:
        assert z.testzip() is None
        names = z.namelist()
        assert not any(n.startswith(".") or "node_modules" in n or n.endswith(".env") for n in names)
    print("PASS ZIP:", out, "(%d files, %.1f KB)" % (len(names), out.stat().st_size / 1024))


if __name__ == "__main__":
    manifest, files = check()
    if "--check-only" not in sys.argv:
        build(manifest, files)
