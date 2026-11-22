"""Работа с настоящим opencode: установка, запуск `opencode serve`, HTTP-клиент, правила/навыки/MCP.

Приложение запускает opencode как локальный сервер (127.0.0.1, случайный порт, пароль) и общается с ним
по его HTTP API - так же, как это делает официальное десктоп-приложение.
"""
import base64
import io
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
REPOS = ("anomalyco/opencode", "sst/opencode")


class OCError(Exception):
    pass


def app_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "PhoneAgent", "opencode")


def config_dir():
    """Глобальная папка настроек opencode (на всех ОС: ~/.config/opencode)."""
    return os.path.join(os.path.expanduser("~"), ".config", "opencode")


# ---------------------------------------------------------------- установка
def find_exe():
    name = "opencode.exe" if os.name == "nt" else "opencode"
    for root, _, files in os.walk(app_dir()):  # архив мог распаковаться во вложенную папку
        if name in files:
            return os.path.join(root, name)
    return shutil.which("opencode")


def _get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"User-Agent": "PhoneAgent", "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def pick_asset(assets):
    """Нужный zip для Windows x64 (обычный, не baseline) или None."""
    best = None
    for a in assets:
        n = a.get("name", "").lower()
        if n.endswith(".zip") and "windows" in n and ("x64" in n or "amd64" in n) and "baseline" not in n:
            best = best or a
    return best


def install(emit):
    """Скачать opencode с GitHub Releases в папку приложения. Вернуть путь к exe."""
    if os.name != "nt":
        raise OCError("Автоустановка есть только для Windows. На других ОС поставьте opencode с opencode.ai.")
    last = None
    for repo in REPOS:
        try:
            rel = json.loads(_get("https://api.github.com/repos/%s/releases/latest" % repo))
            asset = pick_asset(rel.get("assets", []))
            if asset:
                break
        except Exception as e:  # noqa: BLE001
            last = e
    else:
        raise OCError("Не нашёл сборку opencode для Windows на GitHub (%s)." % (last or "нет подходящего файла"))
    emit("Скачиваю %s (%s)…" % (asset["name"], rel.get("tag_name", "")))
    data = _get(asset["browser_download_url"], "application/octet-stream")
    dest = app_dir()
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(dest)
    exe = find_exe()
    if not exe:
        raise OCError("В архиве нет opencode.exe.")
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
        self.proc = subprocess.Popen(
            [self.exe, "serve", "--hostname", "127.0.0.1", "--port", str(self.port)],
            cwd=self.cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **NOWIN)
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
        if self.proc and self.proc.poll() is None:
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
