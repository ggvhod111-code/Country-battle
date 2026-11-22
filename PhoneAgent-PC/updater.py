"""Автообновление PhoneAgent.exe из GitHub Releases (тег latest)."""
import json
import os
import re
import subprocess
import sys
import urllib.request

import version

REPO = "ggvhod111-code/Country-battle"
NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def frozen():
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def check():
    """(новая_версия, url_exe) если на GitHub есть другая сборка, иначе None."""
    req = urllib.request.Request("https://api.github.com/repos/%s/releases/tags/latest" % REPO,
                                 headers={"User-Agent": "PhoneAgent", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rel = json.load(r)
    m = re.search(r"build:([0-9a-f]{7,40})", rel.get("body") or "")
    asset = next((a for a in rel.get("assets", []) if a["name"] == "PhoneAgent.exe"), None)
    if not m or not asset or m.group(1) == version.VERSION:
        return None
    return m.group(1), asset["browser_download_url"]


def apply(url, progress=lambda s: None):
    """Скачать новый exe рядом с текущим и запустить замену + перезапуск. Потом приложение должно закрыться."""
    exe = sys.executable
    new = exe + ".new"
    req = urllib.request.Request(url, headers={"User-Agent": "PhoneAgent"})
    with urllib.request.urlopen(req, timeout=60) as r, open(new, "wb") as out:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                progress("Скачано %d%%" % (done * 100 // total))
    if os.path.getsize(new) < 1_000_000:
        os.remove(new)
        raise RuntimeError("Скачанный файл слишком маленький, обновление отменено.")
    bat = exe + ".update.bat"
    with open(bat, "w", encoding="utf-8") as fh:
        fh.write('@echo off\r\nfor /l %%i in (1,1,30) do (\r\n  move /y "{new}" "{exe}" >nul 2>nul && goto ok\r\n'
                 '  ping -n 2 127.0.0.1 >nul\r\n)\r\nexit /b 1\r\n:ok\r\nstart "" "{exe}"\r\n'
                 '(goto) 2>nul & del "%~f0"\r\n'.format(new=new, exe=exe))
    # новый экземпляр не должен наследовать временную папку старого (_MEI...), иначе: «Failed to load Python DLL»
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", bat], close_fds=True, env=env, **NOWIN)
