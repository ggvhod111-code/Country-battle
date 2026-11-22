#!/usr/bin/env python3
"""Phone Agent для ПК.

Режимы:
  adb      - ИИ управляет Android-телефоном, подключённым к ПК по USB (adb + uiautomator).
  desktop  - ИИ управляет самим компьютером по скриншотам (нужна модель со зрением).

Модели: любой OpenAI-совместимый API (см. PROVIDERS: OpenCode Zen, OpenRouter, Groq, Gemini, Cerebras,
Mistral, NVIDIA, Ollama...). Выбор: --provider, ключ: --key или переменная окружения провайдера.
"""
import argparse
import base64
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

BASE_URL = "https://opencode.ai/zen/v1"


class AgentError(Exception):
    """Понятная пользователю ошибка (CLI печатает её, GUI показывает в логе)."""


# не показывать мигающие консольные окна при вызове adb из оконного .exe
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def find_adb():
    import shutil
    found = shutil.which("adb")
    if found:
        return found
    here = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    for rel in ("adb.exe", os.path.join("platform-tools", "adb.exe"),
                "adb", os.path.join("platform-tools", "adb")):
        cand = os.path.join(here, rel)
        if os.path.isfile(cand):
            return cand
    return None


# ---------------------------------------------------------------- провайдеры (OpenAI-совместимые API)
# Лимиты бесплатных тарифов меняются: смотрите сайт провайдера. Список моделей — кнопкой/--list-models.
PROVIDERS = {
    "opencode": dict(
        name="OpenCode Zen", base="https://opencode.ai/zen/v1", env="OPENCODE_API_KEY",
        model="big-pickle", keyurl="https://opencode.ai/zen",
        note="Бесплатные модели помечены словом free (и big-pickle). Подходит для режима телефона."),
    "openrouter": dict(
        name="OpenRouter", base="https://openrouter.ai/api/v1", env="OPENROUTER_API_KEY",
        model="openrouter/free", keyurl="https://openrouter.ai/keys",
        note="Бесплатные модели с суффиксом :free, карта не нужна. Лимит маленький (порядка 20 запросов/мин и "
             "около 50 в день без пополнения баланса). openrouter/free сам выбирает случайную бесплатную модель."),
    "groq": dict(
        name="Groq", base="https://api.groq.com/openai/v1", env="GROQ_API_KEY",
        model="llama-3.3-70b-versatile", keyurl="https://console.groq.com/keys",
        note="Постоянный бесплатный тариф, очень быстро (порядка 30 запросов/мин, для больших моделей ~1000 в сутки). "
             "Подходит для режима телефона; модель со зрением для режима ПК не проверена."),
    "gemini": dict(
        name="Google Gemini", base="https://generativelanguage.googleapis.com/v1beta/openai/", env="GEMINI_API_KEY",
        model="gemini-3.8-flash", keyurl="https://aistudio.google.com/apikey",
        note="Бесплатный ключ из Google AI Studio. Поддерживает вызов функций и картинки, поэтому подходит и для "
             "режима ПК. OpenAI-режим у Google в бете; точное имя модели смотрите в списке."),
    "cerebras": dict(
        name="Cerebras", base="https://api.cerebras.ai/v1", env="CEREBRAS_API_KEY",
        model="gpt-oss-120b", keyurl="https://cloud.cerebras.ai",
        note="Бесплатный уровень без карты, очень быстрый (порядка 30 запросов/мин). Режим телефона."),
    "mistral": dict(
        name="Mistral", base="https://api.mistral.ai/v1", env="MISTRAL_API_KEY",
        model="mistral-small-latest", keyurl="https://console.mistral.ai",
        note="Тариф Experiment: нужен подтверждённый телефон, запросы могут использоваться для обучения моделей Mistral."),
    "nvidia": dict(
        name="NVIDIA NIM", base="https://integrate.api.nvidia.com/v1", env="NVIDIA_API_KEY",
        model="meta/llama-3.3-70b-instruct", keyurl="https://build.nvidia.com",
        note="Бесплатные запросы к каталогу моделей (порядка 40 в минуту), нужен подтверждённый телефон."),
    "ollama": dict(
        name="Ollama (локально)", base="http://localhost:11434/v1", env="",
        model="qwen3", keyurl="https://ollama.com/download",
        note="Работает на вашем ПК, бесплатно и без ключа. Установите Ollama и скачайте модель с поддержкой "
             "инструментов (ollama pull ...). Для режима ПК нужна модель со зрением."),
    "custom": dict(
        name="Свой сервер (OpenAI-совместимый)", base="", env="CUSTOM_API_KEY",
        model="", keyurl="",
        note="Любой совместимый адрес: LM Studio, vLLM, другой сервис. Укажите адрес API, ключ (если нужен) и модель."),
}

_NOT_CHAT = re.compile(r"whisper|guard|embed|tts|orpheus|rerank|moderation|transcri|image-gen|dall|safeguard", re.I)


def _retry_delay(err, attempt):
    try:
        ra = err.headers.get("Retry-After") if err.headers else None
        if ra:
            return min(60.0, max(1.0, float(ra)))
    except (ValueError, TypeError):
        pass
    return min(30.0, 3.0 * (attempt + 1) ** 2)


# ---------------------------------------------------------------- API

def http(method, url, key, body=None, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    last = "unknown"
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method)
        if key:
            req.add_header("Authorization", "Bearer " + key)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "phone-agent-pc/1.0")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")[:300]
            last = "HTTP %s: %s" % (e.code, text)
            if e.code == 429 or e.code >= 500:
                time.sleep(_retry_delay(e, attempt))
                continue
            raise AgentError(last)
        except urllib.error.URLError as e:
            last = str(e)
            time.sleep(1.5)
    raise AgentError("Запрос не удался: " + last)


def list_models(base, key, provider=None):
    """Чат-модели провайдера; бесплатные сверху (для OpenRouter — только бесплатные)."""
    data = http("GET", base.rstrip("/") + "/models", key).get("data", [])
    ids = []
    for m in data:
        i = m.get("id") if isinstance(m, dict) else None
        if not i:
            continue
        if i.startswith("models/"):  # Gemini отдаёт имена с префиксом
            i = i[len("models/"):]
        if _NOT_CHAT.search(i):
            continue
        ids.append(i)
    if provider == "openrouter":
        ids = [i for i in ids if i.endswith(":free") or i == "openrouter/free"]
    return sorted(ids, key=lambda i: (not ("free" in i or i == "big-pickle"), i))


def chat(base, key, model, messages, tools):
    body = {"model": model, "messages": messages, "tools": tools,
            "tool_choice": "auto", "temperature": 0.2}
    resp = http("POST", base.rstrip("/") + "/chat/completions", key, body)
    choices = resp.get("choices") or []
    if not choices:
        raise AgentError("Пустой ответ: " + json.dumps(resp)[:300])
    return choices[0]["message"]


# ---------------------------------------------------------------- tools schema

def tool(name, desc, props=None, req=None):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props or {}, "required": req or []}}}


def P(t, d, **kw):
    return dict({"type": t, "description": d}, **kw)


T_WAIT = tool("wait", "Wait for the screen to load.", {"seconds": P("integer", "1-10")}, ["seconds"])
T_DONE = tool("done", "Finish the task. Also use this to ask the user a question or report you are blocked.",
              {"summary": P("string", "Short result or question, in the user's language")}, ["summary"])

RULES = """- Work in small steps and look at the new screen before continuing.
- NEVER make purchases or payments, transfer money, delete data, change account settings, or send/post messages, emails or comments unless the task explicitly asks for exactly that. If such a step seems needed, call done with a question to the user instead.
- Never enter passwords, codes or card data that were not given in the task.
- When the task is complete, or you are blocked, call done with a short summary."""


# ---------------------------------------------------------------- backend: Android over ADB

class Adb:
    name = "phone"

    system = ("You control an Android phone through tools. Reply and report in the user's language.\n"
              "After every action you receive the current screen as a numbered list: "
              "[id] Class \"text\" desc=\"...\" (centerX,centerY) flags.\n"
              "Prefer tap(id). To type: tap the input field, then type_text; press_enter submits searches. "
              "If the needed element is not visible, scroll. Use list_apps/open_app (by package name) to launch apps.\n"
              + RULES)

    tools = [
        tool("tap", "Tap the element with this id.", {"id": P("integer", "Element id")}, ["id"]),
        tool("tap_xy", "Tap raw coordinates. Only if no suitable id exists.",
             {"x": P("integer", "X px"), "y": P("integer", "Y px")}, ["x", "y"]),
        tool("type_text", "Type text into an input field (optionally tap it first by id).",
             {"text": P("string", "Text"), "id": P("integer", "Input element id (optional)")}, ["text"]),
        tool("press_enter", "Press Enter / IME action."),
        tool("scroll", "Scroll to reveal more content.",
             {"direction": P("string", "Which content to reveal", enum=["up", "down", "left", "right"])},
             ["direction"]),
        tool("back", "Press Back."),
        tool("home", "Go to the home screen."),
        tool("recents", "Open recent apps."),
        tool("list_apps", "List installed third-party app package names."),
        tool("open_app", "Launch an app by package name (see list_apps), e.g. com.android.chrome.",
             {"package": P("string", "Package name")}, ["package"]),
        T_WAIT, T_DONE,
    ]

    def __init__(self, serial=None):
        exe = find_adb()
        if not exe:
            raise AgentError("adb не найден. Скачайте Android platform-tools и положите папку "
                             "platform-tools рядом с программой (или добавьте adb в PATH).")
        self.base = [exe] + (["-s", serial] if serial else [])
        self.nodes = []
        try:
            state = self.run("get-state").strip()
        except FileNotFoundError:
            raise AgentError("adb не найден. Установите Android platform-tools и добавьте в PATH.")
        if state != "device":
            raise AgentError("Телефон не найден. Включите «Отладку по USB», подключите кабель, "
                     "подтвердите ключ на телефоне и проверьте `adb devices`.")
        sizes = re.findall(r"(\d+)x(\d+)", self.run("shell", "wm", "size"))
        self.w, self.h = (int(sizes[-1][0]), int(sizes[-1][1])) if sizes else (1080, 2400)

    def run(self, *args, timeout=30):
        r = subprocess.run(self.base + list(args), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, **_NOWIN)
        return r.stdout if r.returncode == 0 else r.stdout + r.stderr

    def _xml(self):
        r = subprocess.run(self.base + ["exec-out", "uiautomator", "dump", "/dev/tty"],
                           capture_output=True, timeout=30, **_NOWIN)
        s = r.stdout.decode("utf-8", "replace")
        i, j = s.find("<?xml"), s.rfind("</hierarchy>")
        if i >= 0 and j >= 0:
            return s[i:j + len("</hierarchy>")]
        self.run("shell", "uiautomator", "dump", "/sdcard/ui.xml")
        s = self.run("exec-out", "cat", "/sdcard/ui.xml")
        i, j = s.find("<?xml"), s.rfind("</hierarchy>")
        if i < 0 or j < 0:
            raise RuntimeError("uiautomator не вернул разметку (экран заблокирован?)")
        return s[i:j + len("</hierarchy>")]

    def observe(self):
        try:
            root = ET.fromstring(self._xml())
        except Exception as e:
            return "(не удалось прочитать экран: %s)" % e
        self.nodes = []
        lines, pkg = [], ""
        for n in root.iter("node"):
            pkg = pkg or n.get("package", "")
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            cls = n.get("class", "View").split(".")[-1]
            text = n.get("text", "").replace("\n", " ")[:80]
            desc = n.get("content-desc", "").replace("\n", " ")[:80]
            clickable = n.get("clickable") == "true"
            scrollable = n.get("scrollable") == "true"
            editable = cls == "EditText"
            if not (clickable or scrollable or editable or text or desc):
                continue
            if len(self.nodes) >= 150:
                break
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            flags = [f for f, on in (("clickable", clickable), ("input", editable),
                                     ("scrollable", scrollable), ("checked", n.get("checked") == "true")) if on]
            line = "[%d] %s" % (len(self.nodes), cls)
            if text:
                line += ' "%s"' % text
            if desc and desc != text:
                line += ' desc="%s"' % desc
            line += " (%d,%d)" % (cx, cy)
            if flags:
                line += " " + ",".join(flags)
            lines.append(line)
            self.nodes.append({"cx": cx, "cy": cy, "text": text})
        if not lines:
            lines.append("(нет элементов)")
        return "Current screen. App: %s\n%s" % (pkg, "\n".join(lines))

    def act(self, name, args):
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return "ошибка: неизвестный инструмент " + name
        try:
            return fn(**args)
        except TypeError as e:
            return "ошибка аргументов: %s" % e

    # --- actions
    def _tap(self, x, y):
        self.run("shell", "input", "tap", str(int(x)), str(int(y)))

    def t_tap(self, id):
        if not (0 <= int(id) < len(self.nodes)):
            return "ошибка: нет элемента с id=%s" % id
        n = self.nodes[int(id)]
        self._tap(n["cx"], n["cy"])
        return "нажат [%s]" % id

    def t_tap_xy(self, x, y):
        self._tap(x, y)
        return "тап (%s,%s)" % (x, y)

    def t_type_text(self, text, id=None):
        if id is not None:
            if not (0 <= int(id) < len(self.nodes)):
                return "ошибка: нет элемента с id=%s" % id
            n = self.nodes[int(id)]
            self._tap(n["cx"], n["cy"])
            time.sleep(0.5)
            if n["text"]:  # очистить старое содержимое
                self.run("shell", "input", "keyevent", "KEYCODE_MOVE_END", *["KEYCODE_DEL"] * (len(n["text"]) + 2))
        if text.isascii():
            self.run("shell", "input", "text", shlex.quote(text.replace(" ", "%s")))
            return "текст введён"
        # не-ASCII: нужен ADBKeyBoard (https://github.com/senzhk/ADBKeyBoard), выбранный как клавиатура
        out = self.run("shell", "am", "broadcast", "-a", "ADB_INPUT_TEXT", "--es", "msg", shlex.quote(text))
        if "result=0" in out or "Broadcast completed" not in out:
            return "ошибка: для не-ASCII текста нужна клавиатура ADBKeyBoard"
        return "текст введён (ADBKeyBoard)"

    def t_press_enter(self):
        self.run("shell", "input", "keyevent", "66")
        return "enter нажат"

    def t_scroll(self, direction="down"):
        w, h = self.w, self.h
        pts = {"down": (w // 2, int(h * .7), w // 2, int(h * .3)),
               "up": (w // 2, int(h * .3), w // 2, int(h * .7)),
               "right": (int(w * .8), h // 2, int(w * .2), h // 2),
               "left": (int(w * .2), h // 2, int(w * .8), h // 2)}.get(direction)
        if not pts:
            return "ошибка: направление " + direction
        self.run("shell", "input", "swipe", *map(str, pts), "350")
        return "прокручено " + direction

    def t_back(self):
        self.run("shell", "input", "keyevent", "4")
        return "назад"

    def t_home(self):
        self.run("shell", "input", "keyevent", "3")
        return "домой"

    def t_recents(self):
        self.run("shell", "input", "keyevent", "187")
        return "недавние"

    def t_list_apps(self):
        out = self.run("shell", "pm", "list", "packages", "-3")
        pk = sorted(l.replace("package:", "").strip() for l in out.splitlines() if l.startswith("package:"))
        return "\n".join(pk[:120]) or "(пусто)"

    def t_open_app(self, package):
        out = self.run("shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
        return "запущено " + package if "No activities found" not in out else "ошибка: пакет не найден"

    def t_wait(self, seconds=2):
        time.sleep(max(1, min(10, int(seconds))))
        return "подождали"


# ---------------------------------------------------------------- backend: desktop (screenshots)

class Desktop:
    name = "computer"

    system = ("You control a desktop computer through tools. Reply and report in the user's language.\n"
              "After every action you receive a screenshot. Coordinates you give are in the screenshot's pixels "
              "(its size is stated with it). Click precisely on the centre of targets. Use key for shortcuts "
              "like ctrl+l, enter, tab, alt+tab, win. Use type_text to type into the focused field.\n" + RULES)

    tools = [
        tool("click", "Click at screenshot coordinates.",
             {"x": P("integer", "X"), "y": P("integer", "Y"),
              "button": P("string", "left/right/middle", enum=["left", "right", "middle"]),
              "double": P("boolean", "Double click")}, ["x", "y"]),
        tool("type_text", "Type text into the focused field.", {"text": P("string", "Text")}, ["text"]),
        tool("key", "Press a key or combo joined with +, e.g. enter, ctrl+c, alt+tab.",
             {"keys": P("string", "Key combo")}, ["keys"]),
        tool("scroll", "Scroll the mouse wheel at the pointer (optionally move there first).",
             {"direction": P("string", "up or down", enum=["up", "down"]),
              "amount": P("integer", "Wheel clicks, default 5"),
              "x": P("integer", "Optional X"), "y": P("integer", "Optional Y")}, ["direction"]),
        T_WAIT, T_DONE,
    ]

    MAX_W = 1280

    def __init__(self):
        try:
            import pyautogui
            from PIL import Image  # noqa: F401
        except ImportError:
            raise AgentError("Нужны пакеты: pip install pyautogui pillow")
        self.pg = pyautogui
        self.pg.PAUSE = 0.15
        self.sx = self.sy = 1.0

    def observe(self):
        img = self.pg.screenshot().convert("RGB")
        size = self.pg.size()
        if img.width > self.MAX_W:
            from PIL import Image
            img = img.resize((self.MAX_W, int(img.height * self.MAX_W / img.width)), Image.LANCZOS)
        self.sx, self.sy = size.width / img.width, size.height / img.height
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return [{"type": "text", "text": "Current screenshot, size %dx%d." % img.size},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]

    def act(self, name, args):
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return "ошибка: неизвестный инструмент " + name
        try:
            return fn(**args)
        except TypeError as e:
            return "ошибка аргументов: %s" % e

    def _xy(self, x, y):
        return int(x * self.sx), int(y * self.sy)

    def t_click(self, x, y, button="left", double=False):
        px, py = self._xy(x, y)
        self.pg.click(px, py, button=button, clicks=2 if double else 1, interval=0.1)
        return "клик (%s,%s)" % (x, y)

    def t_type_text(self, text):
        if text.isascii():
            self.pg.write(text, interval=0.02)
            return "текст введён"
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        self.pg.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
        time.sleep(0.4)
        r.destroy()
        return "текст вставлен"

    def t_key(self, keys):
        self.pg.hotkey(*[k.strip().lower() for k in keys.split("+")])
        return "нажато " + keys

    def t_scroll(self, direction="down", amount=5, x=None, y=None):
        if x is not None and y is not None:
            px, py = self._xy(x, y)
            self.pg.moveTo(px, py)
        self.pg.scroll(int(amount) * (1 if direction == "up" else -1))
        return "прокручено " + direction

    def t_wait(self, seconds=2):
        time.sleep(max(1, min(10, int(seconds))))
        return "подождали"


# ---------------------------------------------------------------- agent loop

def cli_ask(desc):
    return input("   выполнить? [Y/n/q] ")


def run_agent(task, backend, base, key, model, confirm, max_steps,
              log=print, ask=cli_ask, stop=lambda: False):
    """log(str) - вывод, ask(desc)->'y'/'n'/'q' - подтверждение, stop()->bool - запрос остановки."""
    messages = [{"role": "system", "content": backend.system},
                {"role": "user", "content": "Задача: " + task}]
    obs = []

    def add_obs():
        for i in obs:
            messages[i]["content"] = "(старое наблюдение убрано)"
        obs.clear()
        messages.append({"role": "user", "content": backend.observe()})
        obs.append(len(messages) - 1)

    add_obs()
    nudges = 0
    for _ in range(max_steps):
        if stop():
            log("⏹ Остановлено.")
            return
        msg = chat(base, key, model, messages, backend.tools)
        content = msg.get("content") or ""
        calls = msg.get("tool_calls") or []
        for i, c in enumerate(calls):
            if not c.get("id"):
                c["id"] = "call_%d_%d" % (len(messages), i)
        if content.strip():
            log("🤖 " + content)
        if not calls:
            messages.append({"role": "assistant", "content": content or "..."})
            nudges += 1
            if nudges > 2:
                log("Модель не вызывает инструменты. Выберите другую модель.")
                return
            messages.append({"role": "user", "content": "Вызови инструмент. Если задача выполнена — вызови done."})
            continue
        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        finished = False
        for c in calls:
            if stop():
                log("⏹ Остановлено.")
                return
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            if name == "done":
                log("✅ " + str(args.get("summary", "")))
                finished, result = True, "ok"
            else:
                desc = "%s %s" % (name, json.dumps(args, ensure_ascii=False))
                log("▶ " + desc)
                answer = "y"
                if confirm:
                    answer = (ask(desc) or "y").strip().lower()
                if answer == "q":
                    log("⏹ Остановлено.")
                    return
                if answer == "n":
                    result = "Пользователь отклонил это действие. Предложи другой путь или вызови done."
                else:
                    try:
                        result = backend.act(name, args)
                    except Exception as e:  # noqa: BLE001
                        result = "ошибка: %s" % e
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": str(result)})
        if finished:
            return
        time.sleep(1)
        add_obs()
    log("Достигнут лимит шагов (%d)." % max_steps)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="ИИ-агент для управления телефоном (adb) или ПК (desktop)")
    ap.add_argument("task", nargs="?", help="что сделать")
    ap.add_argument("--mode", choices=["adb", "desktop"], default="adb")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="opencode", help="откуда берём модели")
    ap.add_argument("--model", default=None, help="id модели (по умолчанию — из настроек провайдера)")
    ap.add_argument("--key", default="", help="API-ключ (или переменная окружения провайдера)")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--serial", help="серийник телефона для adb -s")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--yes", action="store_true", help="не спрашивать подтверждение действий")
    ap.add_argument("--confirm", action="store_true", help="спрашивать подтверждение каждого действия")
    ap.add_argument("--list-models", action="store_true")
    a = ap.parse_args()

    prov = PROVIDERS[a.provider]
    a.base_url = a.base_url or prov["base"]
    a.model = a.model or prov["model"]
    if not a.base_url:
        raise AgentError("Укажите --base-url")
    needs_key = a.provider != "ollama"
    if not a.key and prov["env"]:
        a.key = os.environ.get(prov["env"], "")
    if needs_key and not a.key:
        a.key = input("API-ключ %s%s: " % (prov["name"], " (%s)" % prov["env"] if prov["env"] else "")).strip()
    if needs_key and not a.key and a.provider != "custom":
        raise AgentError("Нужен API-ключ (получить: %s)" % prov["keyurl"])
    if a.list_models:
        print("\n".join(list_models(a.base_url, a.key, a.provider)))
        return
    if not a.model:
        raise AgentError("Укажите --model (список: --list-models)")

    task = a.task or input("Что сделать? ").strip()
    if not task:
        raise AgentError("Пустая задача")
    backend = Adb(a.serial) if a.mode == "adb" else Desktop()
    confirm = a.confirm or (a.mode == "desktop" and not a.yes)
    if a.mode == "desktop":
        print("Режим desktop: аварийная остановка — резко уведите мышь в угол экрана; Ctrl+C в терминале.")
    try:
        run_agent(task, backend, a.base_url, a.key, a.model, confirm, a.max_steps)
    except KeyboardInterrupt:
        print("\n⏹ Остановлено.")


if __name__ == "__main__":
    try:
        main()
    except AgentError as e:
        print("Ошибка:", e)
        sys.exit(1)
    except EOFError:
        print("Ошибка: нужен ввод (ключ или задача). Передайте --key и текст задачи аргументами.")
        sys.exit(1)
    finally:
        if getattr(sys, "frozen", False):  # запуск .exe двойным кликом: не закрывать окно сразу
            try:
                input("\nНажмите Enter, чтобы закрыть окно...")
            except EOFError:
                pass
