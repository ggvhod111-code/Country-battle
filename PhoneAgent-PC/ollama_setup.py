"""Установка и настройка Ollama из приложения: проверка железа, установка, скачивание моделей."""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}
DOCS = "https://docs.ollama.com/quickstart.md"
KEYS_URL = "https://ollama.com/settings/keys"

LOCAL_MODELS = ["qwen3:8b", "qwen3:4b", "gemma3:4b", "qwen3-vl:8b"]
CLOUD_MODELS = ["gemma4:cloud"]


def find_ollama():
    found = shutil.which("ollama")
    if found:
        return found
    if os.name == "nt":
        cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(cand):
            return cand
    return None


def server_models():
    """Имена моделей, уже скачанных в локальный Ollama (или None, если сервер не отвечает)."""
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:  # noqa: BLE001
        return None


def _run(cmd, timeout=15):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **NOWIN)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def hardware():
    """{'ram_gb': float|None, 'gpus': [(name, vram_gb|None)]}"""
    ram = None
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                            ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            ms = MS()
            ms.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))
            ram = ms.total / 2**30
        elif os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as fh:
                ram = int(fh.readline().split()[1]) / 2**20
        else:  # macOS
            ram = int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 2**30 or None
    except Exception:  # noqa: BLE001
        pass
    gpus = []
    smi = shutil.which("nvidia-smi")
    if smi:
        for line in _run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]).splitlines():
            if "," in line:
                n, m = line.rsplit(",", 1)
                try:
                    gpus.append((n.strip(), float(m) / 1024))
                except ValueError:
                    gpus.append((n.strip(), None))
    if not gpus and os.name == "nt":
        out = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_VideoController).Name"])
        gpus = [(n.strip(), None) for n in out.splitlines() if n.strip()]
    return {"ram_gb": ram, "gpus": gpus}


def describe(hw):
    ram = "%.0f ГБ" % hw["ram_gb"] if hw["ram_gb"] else "не определена"
    if hw["gpus"]:
        g = "; ".join("%s%s" % (n, " (%.0f ГБ)" % v if v else "") for n, v in hw["gpus"])
    else:
        g = "не найдена"
    return "Память: %s. Видеокарта: %s." % (ram, g)


def recommend(hw):
    """(текст, 'local'|'cloud')"""
    strong = any(v and v >= 8 for _, v in hw["gpus"])
    discrete = any(("nvidia" in n.lower() or "radeon rx" in n.lower()) for n, _ in hw["gpus"])
    if strong or (discrete and (hw["ram_gb"] or 0) >= 16):
        return ("Видеокарта подходит: можно запускать небольшие модели локально (qwen3:8b, для режима ПК — "
                "qwen3-vl:8b). Крупные модели лучше брать в облаке Ollama.", "local")
    return ("Сильной видеокарты не видно: большие модели локально будут медленными. Рекомендую облачные модели "
            "(имя заканчивается на :cloud, например gemma4:cloud): быстро на любом ПК и ничего не скачивается. "
            "Нужен аккаунт Ollama и кредиты или платный план. Маленькие модели (qwen3:4b) на процессоре тоже работают.",
            "cloud")


def install_cmd():
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                "irm https://ollama.com/install.ps1 | iex"]
    return ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]


def stream(cmd, emit):
    """Запустить команду и построчно отдавать вывод в emit(text). Вернуть код выхода."""
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", **NOWIN)
    except OSError as e:
        emit("Не удалось запустить: %s" % e)
        return 1
    for line in p.stdout:
        line = line.strip("\r\n")
        if line.strip():
            emit(line.split("\r")[-1])
    return p.wait()


def signin(exe):
    """Открыть вход в аккаунт Ollama в отдельном окне: пользователь входит сам."""
    if os.name == "nt":
        subprocess.Popen([exe, "signin"], creationflags=0x00000010)  # CREATE_NEW_CONSOLE
    else:
        subprocess.Popen([exe, "signin"])
