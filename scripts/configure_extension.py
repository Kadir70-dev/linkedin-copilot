#!/usr/bin/env python3
"""Point the extension at one gateway origin before packaging.

    python3 scripts/configure_extension.py https://copilot.example.com
    python3 scripts/configure_extension.py http://localhost:8080   # local dev

Rewrites the GATEWAY constant in background.js and the single manifest
host_permissions entry. Only https:// origins (or http://localhost:8080) are
accepted. No secrets are written: the access key is entered in the extension's
Options page and stays in the browser.
"""
import json, re, sys
from pathlib import Path
from urllib.parse import urlsplit

EXT = Path(__file__).resolve().parent.parent / "services" / "extension"
LOCAL = "http://localhost:8080"


def origin(raw):
    u = urlsplit(raw.strip())
    if raw.strip().rstrip("/") == LOCAL:
        return LOCAL
    if u.scheme != "https" or not u.hostname or u.username or u.password:
        sys.exit("gateway must be an https:// origin (or %s)" % LOCAL)
    if u.path not in ("", "/") or u.query or u.fragment:
        sys.exit("gateway must be an origin only, without path/query")
    if u.hostname.endswith("linkedin.com"):
        sys.exit("gateway must not be on linkedin.com")
    return "https://%s%s" % (u.hostname.lower(), ":%d" % u.port if u.port else "")


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    gw = origin(sys.argv[1])
    bg = EXT / "background.js"
    src, n = re.subn(r'^const GATEWAY = "[^"]*";$', 'const GATEWAY = "%s";' % gw, bg.read_text(), flags=re.M)
    if n != 1:
        sys.exit("GATEWAY constant not found exactly once in background.js")
    bg.write_text(src)
    mf = EXT / "manifest.json"
    m = json.loads(mf.read_text())
    m["host_permissions"] = [gw + "/*"]
    mf.write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    print("extension gateway set to", gw)


if __name__ == "__main__":
    main()
