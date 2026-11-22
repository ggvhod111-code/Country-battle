"""Работа с настоящим opencode: установка, запуск `opencode serve`, HTTP-клиент, правила/навыки/MCP.

Приложение запускает opencode как локальный сервер (127.0.0.1, случайный порт, пароль) и общается с ним
по его HTTP API - так же, как это делает официальное десктоп-приложение.
"""
import base64
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile

NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


class OCError(Exception):
    pass


def app_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "PhoneAgent", "opencode")


def config_dir():
    """Глобальная папка настроек opencode (на всех ОС: ~/.config/opencode)."""
    return os.path.join(os.path.expanduser("~"), ".config", "opencode")


# ---------------------------------------------------------------- установка
def _saved_path_file():
    return os.path.join(os.path.dirname(app_dir()), "opencode_path.txt")


def set_exe(path):
    os.makedirs(os.path.dirname(_saved_path_file()), exist_ok=True)
    with open(_saved_path_file(), "w", encoding="utf-8") as fh:
        fh.write(path)


def _candidates():
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    names = ("opencode.exe", "opencode.cmd", "opencode")
    dirs = [os.path.join(home, ".opencode", "bin"), os.path.join(home, ".local", "bin"),
            os.path.join(home, "scoop", "shims"), os.path.join(appdata, "npm"),
            os.path.join(local, "Programs", "opencode"), os.path.join(local, "Programs", "OpenCode"),
            os.path.join(local, "OpenCode"), os.path.join(local, "opencode"),
            os.path.join(os.environ.get("ProgramFiles", ""), "opencode"),
            os.path.join(os.environ.get("ProgramFiles", ""), "OpenCode"),
            os.path.join(os.environ.get("ChocolateyInstall", ""), "bin")]
    for d in dirs:
        if d and os.path.isdir(d):
            for n in names:
                yield os.path.join(d, n)


def check_exe(path):
    """None, если файл можно запускать; иначе причина (строка)."""
    if os.name != "nt":
        return None
    low = path.lower()
    if low.endswith((".cmd", ".bat")):
        return None
    if not low.endswith(".exe"):
        return "это не .exe (нужен opencode.exe или opencode.cmd)"
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError as e:
        return "файл не читается: %s" % e
    try:
        if os.path.getsize(path) < 50 * 2**20:  # настоящий opencode.exe больше 100 МБ: меньше - обрезанный файл
            return "файл слишком маленький: скачался не полностью. Нажмите «Установить opencode» ещё раз"
    except OSError:
        pass
    if head[:2] != b"MZ":
        return "файл повреждён (это не программа Windows)"
    off = int.from_bytes(head[0x3C:0x40], "little") if len(head) > 0x40 else 0
    if off and off + 6 <= len(head):
        mach = int.from_bytes(head[off + 4:off + 6], "little")
        import platform
        want = {"AMD64": 0x8664, "ARM64": 0xAA64, "x86": 0x14C}.get(platform.machine())
        if want and mach != want and not (platform.machine() == "ARM64" and mach in (0x8664, 0x14C)):
            return "файл собран для другой архитектуры процессора"
    return None


def find_exe():
    """Путь к opencode: выбранный вручную, своя папка приложения, типичные места установки, PATH."""
    try:
        with open(_saved_path_file(), encoding="utf-8") as fh:
            saved = fh.read().strip()
        if os.path.isfile(saved) and not check_exe(saved):
            return saved
    except OSError:
        pass
    name = "opencode.exe" if os.name == "nt" else "opencode"
    for root, _, files in os.walk(app_dir()):  # архив мог распаковаться во вложенную папку
        if name in files and not check_exe(os.path.join(root, name)):
            return os.path.join(root, name)
    found = shutil.which("opencode")
    if found and not check_exe(found):
        return found
    for c in _candidates():
        if os.path.isfile(c) and not check_exe(c):
            return c
    return None


def _get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": "PhoneAgent", "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def npm_package():
    import platform
    return "opencode-windows-arm64" if platform.machine() == "ARM64" else "opencode-windows-x64"


def install(emit, pkg=None, dest=None):
    """Скачать opencode из реестра npm (с проверкой контрольной суммы) в папку приложения. Вернуть путь к exe."""
    import hashlib
    import tarfile
    import tempfile
    if os.name != "nt" and pkg is None:
        raise OCError("Автоустановка есть только для Windows. На других ОС поставьте opencode с opencode.ai.")
    pkg = pkg or npm_package()
    dest = dest or app_dir()
    meta = json.loads(_get("https://registry.npmjs.org/%s/latest" % pkg))
    url, integrity = meta["dist"]["tarball"], meta["dist"].get("integrity", "")
    if not integrity.startswith("sha512-"):
        raise OCError("Реестр не дал контрольную сумму, установка отменена.")
    emit("Скачиваю %s %s (около 80 МБ)…" % (pkg, meta.get("version", "")))
    os.makedirs(dest, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".tgz", dir=dest)
    os.close(fd)
    try:
        h = hashlib.sha512()
        req = urllib.request.Request(url, headers={"User-Agent": "PhoneAgent"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done = last = 0
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total and done * 10 // total != last:
                    last = done * 10 // total
                    emit("Скачано %d%%" % (done * 100 // total))
            if total and done != total:
                raise OCError("Файл скачался не полностью (%d из %d байт). Повторите установку." % (done, total))
        if base64.b64encode(h.digest()).decode() != integrity[len("sha512-"):]:
            raise OCError("Контрольная сумма не совпала: файл повреждён при скачивании. Повторите установку.")
        emit("Распаковываю…")
        name = "opencode.exe"
        with tarfile.open(tmp, "r:gz") as t:
            member = next((m for m in t.getmembers() if m.name.endswith("/bin/" + name)), None)
            if not member:
                raise OCError("В пакете нет %s." % name)
            exe = os.path.join(dest, name)
            part = exe + ".part"
            with t.extractfile(member) as src, open(part, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
        if os.path.getsize(part) != member.size:
            raise OCError("Файл распаковался не полностью.")
        os.replace(part, exe)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    why = check_exe(exe)
    if why:
        raise OCError("%s: %s" % (exe, why))
    emit("Установлено: %s" % exe)
    return exe


# ---------------------------------------------------------------- сервер и клиент
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, exe, cwd):
        self.exe, self.cwd = exe, cwd
        self.port = free_port()
        self.password = secrets.token_hex(16)
        self.proc = None

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def start(self, timeout=60):
        env = dict(os.environ, OPENCODE_SERVER_PASSWORD=self.password)
        cmd = [self.exe, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)]
        if os.name == "nt" and self.exe.lower().endswith((".cmd", ".bat")):
            cmd = ["cmd", "/c"] + cmd
        why = check_exe(self.exe)
        if why:
            raise OCError("%s: %s" % (self.exe, why))
        try:
            self.proc = subprocess.Popen(cmd, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **NOWIN)
        except OSError as e:
            raise OCError("Не удалось запустить %s: %s" % (self.exe, e))
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                raise OCError("opencode сразу завершился (код %s)." % self.proc.returncode)
            try:
                if self.request("GET", "/global/health", timeout=2).get("healthy"):
                    return
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
        raise OCError("opencode не ответил за %d с." % timeout)

    def stop(self):
        if self.proc and self.proc.poll() is None and os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(self.proc.pid)], capture_output=True, **NOWIN)
        elif self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None

    def _headers(self):
        tok = base64.b64encode(("opencode:%s" % self.password).encode()).decode()
        return {"Authorization": "Basic " + tok, "Content-Type": "application/json"}

    def request(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise OCError("HTTP %s %s: %s" % (e.code, path, e.read()[:300].decode("utf-8", "replace")))
        return json.loads(raw) if raw.strip() else {}

    # --- API
    def providers(self):
        """([(id, name, [model ids])...], connected-ids). Подключённые - первыми."""
        d = self.request("GET", "/provider")
        con = d.get("connected", [])
        items = [(p["id"], p.get("name", p["id"]), sorted(p.get("models", {}))) for p in d["all"]]
        items.sort(key=lambda x: (x[0] not in con, x[1].lower()))
        return items, con

    def set_key(self, provider, key):
        self.request("PUT", "/auth/" + provider, {"type": "api", "key": key})

    def new_session(self, title=None):
        return self.request("POST", "/session", {"title": title} if title else {})["id"]

    def prompt(self, sid, text, provider, model):
        body = {"parts": [{"type": "text", "text": text}]}
        if provider and model:
            body["model"] = {"providerID": provider, "modelID": model}
        self.request("POST", "/session/%s/prompt_async" % sid, body)

    def abort(self, sid):
        self.request("POST", "/session/%s/abort" % sid)

    def reply_permission(self, rid, reply):
        self.request("POST", "/permission/%s/reply" % rid, {"reply": reply})

    def events(self):
        """Бесконечный поток событий (dict) из /event."""
        req = urllib.request.Request(self.base + "/event", headers=self._headers())
        with urllib.request.urlopen(req, timeout=None) as r:
            buf = []
            for raw in r:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    buf.append(line[5:].lstrip())
                elif not line and buf:
                    try:
                        yield json.loads("\n".join(buf))
                    except ValueError:
                        pass
                    buf = []


def error_text(err):
    if not isinstance(err, dict):
        return str(err)
    d = err.get("data") or {}
    return "%s: %s" % (err.get("name", "Ошибка"), d.get("message") or json.dumps(d, ensure_ascii=False)[:300])


class Renderer:
    """Превращает события opencode в строки лога + сообщает об окончании ответа/запросах разрешений."""

    def __init__(self, sid, say, ask_permission, done):
        self.sid, self.say, self.ask, self.done = sid, say, ask_permission, done
        self.roles = {}
        self.text = {}
        self.shown_tools = set()
        self.printed = set()
        self.last_err = None

    def err(self, text):
        if text != self.last_err:  # opencode присылает одну ошибку несколькими событиями
            self.last_err = text
            self.say("Ошибка: " + text)

    def flush_text(self):
        for pid, (mid, txt) in list(self.text.items()):
            if pid not in self.printed and self.roles.get(mid) == "assistant" and txt.strip():
                self.say(txt.strip())
                self.printed.add(pid)

    def handle(self, ev):
        t, p = ev.get("type"), ev.get("properties", {})
        if p.get("sessionID") not in (None, self.sid):
            return
        if t == "session.status" and p.get("status", {}).get("type") == "busy":
            self.last_err = None
        if t == "message.updated":
            info = p.get("info", {})
            self.roles[info.get("id")] = info.get("role")
            if info.get("error"):
                self.err(error_text(info["error"]))
        elif t == "message.part.updated":
            part = p.get("part", {})
            if part.get("type") == "text":
                self.text[part["id"]] = (part.get("messageID"), part.get("text", ""))
                if part.get("time", {}).get("end"):
                    self.flush_text()
            elif part.get("type") == "tool":
                st = part.get("state", {})
                key = (part.get("callID"), st.get("status"))
                if st.get("status") in ("running", "completed", "error") and key not in self.shown_tools:
                    self.shown_tools.add(key)
                    title = st.get("title") or json.dumps(st.get("input", {}), ensure_ascii=False)[:120]
                    mark = {"running": "▶", "completed": "✓", "error": "✗"}[st["status"]]
                    self.say("%s %s: %s" % (mark, part.get("tool"), title))
        elif t == "permission.asked":
            self.ask(p)
        elif t == "session.error":
            self.err(error_text(p.get("error")))
        elif t == "session.status" and p.get("status", {}).get("type") == "idle":
            self.flush_text()
            self.done()
        elif t == "session.idle":
            self.flush_text()
            self.done()


# ---------------------------------------------------------------- правила, навыки, MCP
def rules_path(cdir=None):
    return os.path.join(cdir or config_dir(), "AGENTS.md")


def read_rules(cdir=None):
    try:
        with open(rules_path(cdir), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def write_rules(text, cdir=None):
    cdir = cdir or config_dir()
    os.makedirs(cdir, exist_ok=True)
    with open(rules_path(cdir), "w", encoding="utf-8") as fh:
        fh.write(text)


_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def parse_skill(text):
    """(name, description) из YAML-шапки SKILL.md; иначе OCError."""
    m = re.match(r"^\ufeff?---\s*\r?\n(.*?)\r?\n---", text, re.S)
    if not m:
        raise OCError("В SKILL.md нет шапки --- name/description ---.")
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip().strip("\"'")
    name, desc = fields.get("name", ""), fields.get("description", "")
    if not _NAME.match(name) or len(name) > 64:
        raise OCError("name в SKILL.md: только строчные латинские буквы, цифры и дефисы (1-64 символа).")
    if not desc:
        raise OCError("В SKILL.md нужно поле description.")
    return name, desc


def skills_dir(cdir=None):
    return os.path.join(cdir or config_dir(), "skills")


def list_skills(cdir=None):
    d = skills_dir(cdir)
    return sorted(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n, "SKILL.md"))) if os.path.isdir(d) else []


def add_skill(path, cdir=None):
    """Добавить навык из SKILL.md, папки с SKILL.md или zip. Вернуть имя навыка."""
    cdir = cdir or config_dir()
    if os.path.isdir(path):
        src = os.path.join(path, "SKILL.md")
        if not os.path.isfile(src):
            raise OCError("В папке нет файла SKILL.md.")
        with open(src, encoding="utf-8") as fh:
            name, _ = parse_skill(fh.read())
        dst = os.path.join(skills_dir(cdir), name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(path, dst)
        return name
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith("SKILL.md") and not n.startswith("/") and ".." not in n]
            if not names:
                raise OCError("В архиве нет SKILL.md.")
            top = min(names, key=len)
            prefix = top[:-len("SKILL.md")]
            name, _ = parse_skill(z.read(top).decode("utf-8"))
            dst = os.path.join(skills_dir(cdir), name)
            shutil.rmtree(dst, ignore_errors=True)
            for n in z.namelist():
                if n.startswith(prefix) and not n.endswith("/") and ".." not in n and not n.startswith("/"):
                    out = os.path.join(dst, *n[len(prefix):].split("/"))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "wb") as fh:
                        fh.write(z.read(n))
        return name
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    name, _ = parse_skill(text)
    dst = os.path.join(skills_dir(cdir), name)
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write(text)
    return name


def remove_skill(name, cdir=None):
    if _NAME.match(name):
        shutil.rmtree(os.path.join(skills_dir(cdir), name), ignore_errors=True)


def _cfg_file(cdir):
    return os.path.join(cdir, "opencode.json")


def read_mcp(cdir=None):
    """Текст JSON-объекта "mcp" из нашего opencode.json (пустой объект, если нет)."""
    try:
        with open(_cfg_file(cdir or config_dir()), encoding="utf-8") as fh:
            return json.dumps(json.load(fh).get("mcp", {}), ensure_ascii=False, indent=2)
    except (OSError, ValueError):
        return "{}"


def write_mcp(text, cdir=None):
    """Сохранить "mcp" в ~/.config/opencode/opencode.json, не трогая остальные ключи."""
    cdir = cdir or config_dir()
    try:
        mcp = json.loads(text or "{}")
    except ValueError as e:
        raise OCError("Это не JSON: %s" % e)
    if not isinstance(mcp, dict):
        raise OCError("Нужен JSON-объект: {\"имя\": {\"type\": \"local\", \"command\": [...]}}.")
    for name, cfg in mcp.items():
        if not isinstance(cfg, dict) or cfg.get("type") not in ("local", "remote"):
            raise OCError("У сервера «%s» должно быть type: local или remote." % name)
    os.makedirs(cdir, exist_ok=True)
    path = _cfg_file(cdir)
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except ValueError:
            raise OCError("%s не читается как JSON: поправьте или удалите файл." % path)
    data.setdefault("$schema", "https://opencode.ai/config.json")
    if mcp:
        data["mcp"] = mcp
    else:
        data.pop("mcp", None)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


MCP_EXAMPLE = """{
  "files": {
    "type": "local",
    "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "C:\\\\Users"],
    "enabled": true
  },
  "context7": {
    "type": "remote",
    "url": "https://mcp.context7.com/mcp",
    "enabled": true
  }
}"""
