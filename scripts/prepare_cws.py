#!/usr/bin/env python3
"""Legacy MV3 Chrome-extension preflight and ZIP packager; not a publish action."""
from pathlib import Path
import json
import re
import struct
import sys
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "services" / "extension"
OUT = ROOT / "dist"
SIZES = (16, 48, 128)
GLYPH = {
 "L": ("10000","10000","10000","10000","10000","10000","11111"),
 "C": ("01111","10000","10000","10000","10000","10000","01111")
}

def chunk(name, data):
    return struct.pack(">I",len(data))+name+data+struct.pack(">I",zlib.crc32(name+data)&0xffffffff)

def create_icon(size):
    zoom=max(1,size//18)
    left=(size-12*zoom)//2
    top=(size-7*zoom)//2
    scan=bytearray()
    radius=max(2,size//6)
    for y in range(size):
        scan.append(0)
        for x in range(size):
            dx=min(x,size-1-x)
            dy=min(y,size-1-y)
            corner=(dx<radius and dy<radius and (radius-dx)**2+(radius-dy)**2>radius*radius)
            if corner:
                scan.extend((0,0,0,0))
                continue
            rgb=(17,40+y*24//size,77+x*26//size,255)
            col=(x-left)//zoom
            row=(y-top)//zoom
            if 0<=row<7 and 0<=col<12:
                if (col<5 and GLYPH["L"][row][col]=="1") or (7<=col<12 and GLYPH["C"][row][col-7]=="1"):
                    rgb=(255,255,255,255)
            scan.extend(rgb)
    ihdr=struct.pack(">IIBBBBB",size,size,8,6,0,0,0)
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",ihdr)+chunk(b"IDAT",zlib.compress(bytes(scan),9))+chunk(b"IEND",b"")

def generate_icons():
    out=EXT/"icons"
    out.mkdir(exist_ok=True)
    for size in SIZES:
        (out/("icon-%d.png"%size)).write_bytes(create_icon(size))

def verify():
    m=json.loads((EXT/"manifest.json").read_text())
    assert m.get("manifest_version")==3, "MV3 required"
    assert m.get("permissions")==["storage"], "Excess permissions"
    assert m.get("host_permissions")==["http://localhost:8080/*"], "Gateway must be localhost"
    assert m.get("content_scripts",[{}])[0].get("matches")==["https://www.linkedin.com/*"], "LinkedIn scope changed"
    assert m.get("background",{}).get("service_worker")=="background.js"
    assert m.get("name") and m.get("description") and m.get("version")
    for size in SIZES:
        fn="icons/icon-%d.png"%size
        assert m.get("icons",{}).get(str(size))==fn
        data=(EXT/fn).read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II",data[16:24])==(size,size)
    cs=(EXT/"content.js").read_text()
    bg=(EXT/"background.js").read_text()
    assert "chrome.runtime.sendMessage" in cs and "chrome.runtime.onMessage" in bg
    assert "http://localhost:8080" in bg
    deny=[r"\.click\s*\(",r"\.submit\s*\(",r"\.requestSubmit\s*\(",r"new\s+KeyboardEvent\s*\(",r"\bXMLHttpRequest\b",r"\bWebSocket\s*\(",r"\bchrome\.(?:debugger|cookies|webRequest)\b",r"\beval\s*\(",r"\bfetch\s*\("]
    for line in cs.splitlines():
        line=line.split("//",1)[0].strip()
        if not line or line.startswith("*"):continue
        for rule in deny:
            assert re.search(rule,line) is None, "Forbidden content script capability: "+rule
    for bad in ("chrome.debugger","chrome.cookies","chrome.webRequest"):
        assert bad not in bg,"Forbidden background capability"
    assert not (EXT/".env").exists()
    print("PASS MV3, minimum permissions, valid icons and no send/network calls in content.js")

def build_zip():
    m=json.loads((EXT/"manifest.json").read_text())
    OUT.mkdir(exist_ok=True)
    name="linkedin-copilot-%s-LEGACY-PREFLIGHT.zip"%m["version"]
    dest=OUT/name
    paths=["manifest.json","background.js","content.js"]+["icons/icon-%d.png"%n for n in SIZES]
    with zipfile.ZipFile(dest,"w",compression=zipfile.ZIP_DEFLATED) as z:
        for path in paths:z.write(EXT/path,path)
    with zipfile.ZipFile(dest) as z:
        assert set(z.namelist())==set(paths)
        assert z.testzip() is None
    print("PASS ZIP:",dest)
    print("IMPORTANT legacy GitHub branch, not Chrome Web Store ready without source sync and privacy approval")

if __name__=="__main__":
    generate_icons()
    verify()
    if "--check-only" not in sys.argv:build_zip()
