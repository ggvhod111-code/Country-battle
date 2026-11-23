#!/usr/bin/env python3
"""Phone Agent - приложение с окном (tkinter). Запуск: двойной клик по PhoneAgent.exe или `python gui.py`."""
import json
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, scrolledtext, ttk

import agent
import ollama_setup as osu
import opencode_gui
import updater

CFG_PATH = os.path.join(os.path.expanduser("~"), ".phone_agent.json")

HINTS = {
    "adb": "Телефон подключён к ПК по USB, в нём включена «Отладка по USB». "
           "Нужен adb: положите папку platform-tools рядом с программой. Подойдёт любая модель с вызовом инструментов.",
    "desktop": "ИИ видит экран ПК по скриншотам и двигает мышь/клавиатуру. Нужна модель СО ЗРЕНИЕМ. "
               "Окно свернётся, останется маленькая панель со «Стоп».",
}


class Panel(tk.Toplevel):
    """Маленькая панель поверх всех окон: статус, подтверждение действия, кнопка «Стоп»."""

    def __init__(self, root, stop_cb):
        super().__init__(root)
        self.stop_cb = stop_cb
        self.pending = None
        self.title("Phone Agent")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.stop)
        self.lbl = tk.Label(self, text="Агент работает…", wraplength=330, justify="left", anchor="w",
                            padx=10, pady=8)
        self.lbl.pack(fill="x")
        self.row = tk.Frame(self)
        self.row.pack(pady=(0, 8))
        self.ok = tk.Button(self.row, text="Выполнить", width=11)
        self.no = tk.Button(self.row, text="Пропустить", width=11)
        self.stopb = tk.Button(self.row, text="Стоп", width=8, command=self.stop)
        self.stopb.pack(side="right", padx=4)
        self.update_idletasks()
        w, h = 360, 130
        x = self.winfo_screenwidth() - w - 20
        y = self.winfo_screenheight() - h - 90
        self.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def say(self, text):
        if self.pending is None:
            self.lbl.config(text=text[:160])

    def ask(self, desc, reply):
        self.pending = reply
        self.lbl.config(text="Выполнить действие?\n" + desc[:160])
        self.ok.config(command=lambda: self._answer("y"))
        self.no.config(command=lambda: self._answer("n"))
        self.ok.pack(side="left", padx=4)
        self.no.pack(side="left", padx=4)
        self.lift()

    def _answer(self, value):
        reply, self.pending = self.pending, None
        self.ok.pack_forget()
        self.no.pack_forget()
        if reply is not None:
            reply.put(value)
        self.lbl.config(text="Агент работает…")

    def stop(self):
        self.stop_cb()
        if self.pending is not None:
            self._answer("q")
        self.lbl.config(text="Останавливаю…")


class OllamaDialog(tk.Toplevel):
    """Установка Ollama, проверка железа, скачивание моделей (по мотивам официального промпта Ollama)."""

    def __init__(self, root, app):
        super().__init__(root)
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.title("Настройка Ollama")
        self.geometry("640x560")
        self.transient(root)
        f = ttk.Frame(self, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)
        self.st = ttk.Label(f, text="", wraplength=600, justify="left")
        self.st.grid(row=0, column=0, columnspan=2, sticky="w")
        self.hw = ttk.Label(f, text="Проверяю компьютер…", wraplength=600, justify="left")
        self.hw.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.rec = ttk.Label(f, text="", wraplength=600, justify="left", foreground="#145a14")
        self.rec.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 8))
        self.inst_btn = ttk.Button(f, text="1. Установить Ollama", command=self.install)
        self.inst_btn.grid(row=3, column=0, sticky="w")
        ttk.Label(f, text="Модель").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.model = tk.StringVar(value=app.model.get() or "qwen3:8b")
        ttk.Combobox(f, textvariable=self.model, values=osu.CLOUD_MODELS + osu.LOCAL_MODELS).grid(
            row=5, column=0, sticky="ew", pady=2)
        self.pull_btn = ttk.Button(f, text="2. Скачать / подключить модель", command=self.pull)
        self.pull_btn.grid(row=5, column=1, padx=6)
        ttk.Label(f, text="Имя с :cloud — облачная модель (скачивания нет, нужен вход в аккаунт Ollama).",
                  foreground="#555", wraplength=600).grid(row=6, column=0, columnspan=2, sticky="w")
        rb = ttk.Frame(f)
        rb.grid(row=7, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(rb, text="Войти в аккаунт Ollama (для :cloud)", command=self.signin).pack(side="left")
        ttk.Button(rb, text="Ключ API", command=lambda: webbrowser.open(osu.KEYS_URL)).pack(side="left", padx=6)
        ttk.Button(rb, text="Документация", command=lambda: webbrowser.open("https://docs.ollama.com")).pack(
            side="left")
        ttk.Button(rb, text="Использовать в приложении", command=self.use).pack(side="left", padx=6)
        self.log = scrolledtext.ScrolledText(f, height=10, wrap="word", state="disabled")
        self.log.grid(row=8, column=0, columnspan=2, sticky="nsew")
        f.rowconfigure(8, weight=1)
        self.refresh()
        threading.Thread(target=self.probe, daemon=True).start()
        self.after(100, self.poll)

    def say(self, t):
        self.log.config(state="normal")
        self.log.insert("end", t + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def refresh(self):
        exe = osu.find_ollama()
        self.st.config(text=("Ollama установлена: %s" % exe) if exe else "Ollama не установлена.")
        self.inst_btn.config(state="disabled" if exe else "normal")

    def probe(self):
        hw = osu.hardware()
        text, kind = osu.recommend(hw)
        self.q.put(("hw", osu.describe(hw), text, kind))

    def run(self, cmd, after=None):
        if self.busy:
            return
        self.busy = True

        def work():
            code = osu.stream(cmd, lambda t: self.q.put(("log", t)))
            self.q.put(("done", code, after))

        threading.Thread(target=work, daemon=True).start()

    def install(self):
        if not messagebox.askyesno("Ollama", "Скачать и установить Ollama с ollama.com? Это официальный установщик.",
                                   parent=self):
            return
        self.say("Устанавливаю Ollama…")
        self.run(osu.install_cmd(), "install")

    def pull(self):
        exe = osu.find_ollama()
        m = self.model.get().strip()
        if not exe:
            messagebox.showinfo("Ollama", "Сначала установите Ollama (кнопка 1).", parent=self)
            return
        if not m:
            return
        self.say("ollama pull %s" % m)
        self.run([exe, "pull", m], "pull")

    def signin(self):
        exe = osu.find_ollama()
        if not exe:
            messagebox.showinfo("Ollama", "Сначала установите Ollama.", parent=self)
            return
        osu.signin(exe)
        self.say("Откроется окно входа: подтвердите вход в браузере. Для облачных моделей нужны кредиты или платный план.")

    def use(self):
        self.app.stash()  # не потерять введённое для прежнего провайдера
        self.app.bases["ollama"] = ""
        self.app.models["ollama"] = self.model.get().strip()
        self.app.apply_provider("ollama")
        self.app.append("Выбран провайдер Ollama, модель %s." % self.model.get().strip())
        self.destroy()

    def poll(self):
        try:
            while True:
                it = self.q.get_nowait()
                if it[0] == "log":
                    self.say(it[1])
                elif it[0] == "hw":
                    self.hw.config(text=it[1])
                    self.rec.config(text=it[2])
                    if it[3] == "cloud":
                        self.model.set(osu.CLOUD_MODELS[0])
                elif it[0] == "done":
                    self.busy = False
                    self.say("Готово." if it[1] == 0 else "Завершилось с ошибкой (код %s)." % it[1])
                    self.refresh()
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self.poll)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stop_ev = threading.Event()
        self.thread = None
        self.panel = None

        root.title("Phone Agent (%s)" % updater.version.VERSION)
        root.geometry("800x840")
        root.minsize(680, 640)
        cfg = self.load()

        # ключи/модели/адреса запоминаются отдельно для каждого провайдера
        self.keys = dict(cfg.get("keys", {}))
        if cfg.get("key") and "opencode" not in self.keys:  # совместимость со старой версией
            self.keys["opencode"] = cfg["key"]
        self.models = dict(cfg.get("models", {}))
        self.bases = dict(cfg.get("bases", {}))
        self.names = {v["name"]: k for k, v in agent.PROVIDERS.items()}
        self.cur = cfg.get("provider", "opencode")
        if self.cur not in agent.PROVIDERS:
            self.cur = "opencode"

        self.provider = tk.StringVar(value=agent.PROVIDERS[self.cur]["name"])
        self.base = tk.StringVar()
        self.key = tk.StringVar()
        self.model = tk.StringVar()
        self.remember = tk.BooleanVar(value=cfg.get("remember", True))
        self.mode = tk.StringVar(value=cfg.get("mode", "adb"))
        self.serial = tk.StringVar(value=cfg.get("serial", ""))
        self.steps = tk.IntVar(value=cfg.get("steps", 40))
        self.confirm = tk.BooleanVar(value=cfg.get("confirm", self.mode.get() == "desktop"))

        f = ttk.Frame(root, padding=12)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Провайдер ИИ").grid(row=0, column=0, sticky="w", pady=2)
        pb = ttk.Combobox(f, textvariable=self.provider, state="readonly",
                          values=[v["name"] for v in agent.PROVIDERS.values()])
        pb.grid(row=0, column=1, sticky="ew", padx=6)
        pb.bind("<<ComboboxSelected>>", lambda e: self.on_provider())
        bx = ttk.Frame(f)
        bx.grid(row=0, column=2, sticky="w")
        self.keylink_btn = ttk.Button(bx, text="Получить ключ", command=self.open_keyurl)
        self.keylink_btn.pack(side="left")
        self.ollama_btn = ttk.Button(bx, text="Настроить Ollama", command=self.open_ollama)
        self.note = ttk.Label(f, text="", wraplength=730, foreground="#555")
        self.note.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 6))

        ttk.Label(f, text="Адрес API").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(f, textvariable=self.base).grid(row=2, column=1, columnspan=2, sticky="ew", padx=6)

        ttk.Label(f, text="API-ключ").grid(row=3, column=0, sticky="w", pady=2)
        self.key_entry = ttk.Entry(f, textvariable=self.key, show="•")
        self.key_entry.grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Checkbutton(f, text="запомнить (без шифрования)", variable=self.remember).grid(row=3, column=2, sticky="w")

        ttk.Label(f, text="Модель").grid(row=4, column=0, sticky="w", pady=2)
        self.model_box = ttk.Combobox(f, textvariable=self.model, values=[])
        self.model_box.grid(row=4, column=1, sticky="ew", padx=6)
        self.models_btn = ttk.Button(f, text="Список моделей", command=self.load_models)
        self.models_btn.grid(row=4, column=2, sticky="w")

        ttk.Separator(f).grid(row=5, column=0, columnspan=3, sticky="ew", pady=8)

        ttk.Label(f, text="Что управляем").grid(row=6, column=0, sticky="w")
        mf = ttk.Frame(f)
        mf.grid(row=6, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(mf, text="Телефон по USB (adb)", variable=self.mode, value="adb",
                        command=self.on_mode).pack(side="left", padx=(0, 16))
        ttk.Radiobutton(mf, text="Этот компьютер", variable=self.mode, value="desktop",
                        command=self.on_mode).pack(side="left")
        self.hint = ttk.Label(f, text="", wraplength=730, foreground="#555")
        self.hint.grid(row=7, column=0, columnspan=3, sticky="w", pady=(2, 6))

        ttk.Label(f, text="Устройство (serial)").grid(row=8, column=0, sticky="w", pady=2)
        ttk.Entry(f, textvariable=self.serial).grid(row=8, column=1, sticky="ew", padx=6)
        ttk.Label(f, text="нужно, если подключено несколько", foreground="#777").grid(row=8, column=2, sticky="w")

        ttk.Label(f, text="Задача").grid(row=9, column=0, sticky="nw", pady=(8, 2))
        self.task = tk.Text(f, height=4, wrap="word")
        self.task.grid(row=9, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 2))
        self.task.insert("1.0", cfg.get("task", ""))

        of = ttk.Frame(f)
        of.grid(row=10, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Checkbutton(of, text="Спрашивать подтверждение каждого действия", variable=self.confirm).pack(side="left")
        ttk.Label(of, text="   Макс. шагов:").pack(side="left")
        ttk.Spinbox(of, from_=5, to=200, width=5, textvariable=self.steps).pack(side="left")

        bf = ttk.Frame(f)
        bf.grid(row=11, column=0, columnspan=3, sticky="ew", pady=4)
        self.start_btn = ttk.Button(bf, text="▶ Запустить", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(bf, text="⏹ Стоп", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.status = ttk.Label(bf, text="Готов")
        self.status.pack(side="left", padx=8)
        ttk.Button(bf, text="Обновления", command=self.check_update).pack(side="right", padx=6)
        ttk.Button(bf, text="opencode (кодинг-агент)", command=lambda: opencode_gui.OpenCodeWindow(root)).pack(
            side="right")

        self.log = scrolledtext.ScrolledText(f, height=12, wrap="word", state="disabled")
        self.log.grid(row=12, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        f.rowconfigure(12, weight=1)

        self.apply_provider(self.cur)
        self.on_mode(first=True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll()
        if updater.frozen():
            self.check_update(silent=True)

    # ---------- обновления
    def check_update(self, silent=False):
        def work():
            try:
                self.q.put(("update", updater.check(), silent))
            except Exception as e:  # noqa: BLE001
                if not silent:
                    self.q.put(("log", "Не удалось проверить обновления: %s" % e))
        threading.Thread(target=work, daemon=True).start()

    def on_update(self, info, silent):
        if not info:
            if not silent:
                messagebox.showinfo("Phone Agent", "У вас последняя версия." if updater.frozen() else
                                    "Обновление работает только в собранном PhoneAgent.exe.")
            return
        if not messagebox.askyesno("Phone Agent", "Доступна новая версия. Обновить сейчас? Приложение перезапустится."):
            return
        self.status.config(text="Обновляю…")

        def work():
            try:
                updater.apply(info[1], info[2], lambda t: self.q.put(("log", t)))
                self.q.put(("restart",))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", "Обновление не удалось: %s" % e))
        threading.Thread(target=work, daemon=True).start()

    # ---------- провайдеры
    def stash(self):
        """Запомнить поля текущего провайдера перед переключением/сохранением."""
        self.keys[self.cur] = self.key.get().strip()
        self.models[self.cur] = self.model.get().strip()
        self.bases[self.cur] = self.base.get().strip()

    def apply_provider(self, pid):
        self.cur = pid
        p = agent.PROVIDERS[pid]
        self.provider.set(p["name"])
        self.base.set(self.bases.get(pid) or p["base"])
        self.key.set(self.keys.get(pid) or (os.environ.get(p["env"], "") if p["env"] else ""))
        self.model.set(self.models.get(pid) or p["model"])
        self.model_box.config(values=[])
        self.note.config(text=p["note"])
        self.key_entry.config(state="disabled" if pid == "ollama" else "normal")
        if pid == "ollama":
            self.ollama_btn.pack(side="left", padx=(6, 0))
        else:
            self.ollama_btn.pack_forget()
        self.keylink_btn.config(state="normal" if p["keyurl"] else "disabled")

    def on_provider(self):
        self.stash()
        self.apply_provider(self.names.get(self.provider.get(), "opencode"))

    def open_keyurl(self):
        url = agent.PROVIDERS[self.cur]["keyurl"]
        if url:
            webbrowser.open(url)

    def open_ollama(self):
        OllamaDialog(self.root, self)

    def needs_key(self):
        return self.cur not in ("ollama", "custom")

    # ---------- настройки
    def load(self):
        try:
            with open(CFG_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return {}

    def save(self):
        self.stash()
        data = {"provider": self.cur, "models": self.models, "bases": self.bases,
                "mode": self.mode.get(), "serial": self.serial.get(),
                "steps": self.steps.get(), "confirm": self.confirm.get(), "remember": self.remember.get(),
                "task": self.task.get("1.0", "end").strip()}
        if self.remember.get():
            data["keys"] = self.keys
        try:
            with open(CFG_PATH, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass

    def on_mode(self, first=False):
        self.hint.config(text=HINTS[self.mode.get()])
        if not first:
            self.confirm.set(self.mode.get() == "desktop")

    # ---------- лог
    def append(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ---------- модели
    def load_models(self):
        key = self.key.get().strip()
        base = self.base.get().strip()
        if self.needs_key() and not key:
            messagebox.showwarning("Phone Agent", "Сначала введите API-ключ (кнопка «Получить ключ»).")
            return
        if not base:
            messagebox.showwarning("Phone Agent", "Укажите адрес API.")
            return
        pid = self.cur
        self.models_btn.config(state="disabled")
        self.status.config(text="Загружаю список моделей…")

        def work():
            try:
                self.q.put(("models", agent.list_models(base, key, pid)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("log", "Ошибка загрузки моделей: %s" % e))
                self.q.put(("models", None))

        threading.Thread(target=work, daemon=True).start()

    # ---------- запуск агента
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running():
            return
        key = self.key.get().strip()
        base = self.base.get().strip()
        model = self.model.get().strip()
        task = self.task.get("1.0", "end").strip()
        if self.needs_key() and not key:
            messagebox.showwarning("Phone Agent", "Введите API-ключ (кнопка «Получить ключ»).")
            return
        if not base or not model:
            messagebox.showwarning("Phone Agent", "Укажите адрес API и модель (можно нажать «Список моделей»).")
            return
        if not task:
            messagebox.showwarning("Phone Agent", "Опишите задачу.")
            return
        try:
            steps = max(1, int(self.steps.get()))
        except (ValueError, tk.TclError):
            steps = 40
        self.save()
        self.stop_ev.clear()
        self.append("— Запуск: %s, модель %s, %s —" % (
            agent.PROVIDERS[self.cur]["name"], model, "телефон" if self.mode.get() == "adb" else "компьютер"))
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.status.config(text="Работаю…")
        mode = self.mode.get()
        self.panel = Panel(self.root, self.stop)
        if mode == "desktop":
            self.root.iconify()
        args = (mode, base, key, model, self.serial.get().strip() or None,
                task, self.confirm.get(), steps)
        self.thread = threading.Thread(target=self.worker, args=args, daemon=True)
        self.thread.start()

    def ask_from_thread(self, desc):
        reply = queue.Queue()
        self.q.put(("ask", desc, reply))
        return reply.get()

    def worker(self, mode, base, key, model, serial, task, confirm, steps):
        log = lambda s: self.q.put(("log", s))  # noqa: E731
        try:
            if mode == "desktop":
                time.sleep(1.2)  # дать окну свернуться, чтобы скриншот показал рабочий стол
                backend = agent.Desktop()
            else:
                backend = agent.Adb(serial)
            agent.run_agent(task, backend, base, key, model, confirm, steps,
                            log=log, ask=self.ask_from_thread, stop=self.stop_ev.is_set)
        except agent.AgentError as e:
            log("Ошибка: %s" % e)
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if name == "FailSafeException":
                log("⏹ Аварийная остановка (мышь в углу экрана).")
            else:
                log("Ошибка: %s: %s" % (name, e))
        finally:
            self.q.put(("done",))

    def stop(self):
        self.stop_ev.set()
        self.status.config(text="Останавливаю…")
        if self.panel is not None and self.panel.pending is not None:
            self.panel._answer("q")

    def finish(self):
        if self.panel is not None:
            try:
                self.panel.destroy()
            except tk.TclError:
                pass
            self.panel = None
        self.root.deiconify()
        self.root.lift()
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.config(text="Готов")

    def poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.append(item[1])
                    if self.panel is not None:
                        self.panel.say(item[1])
                elif kind == "ask":
                    if self.stop_ev.is_set() or self.panel is None:
                        item[2].put("q")
                    else:
                        self.panel.ask(item[1], item[2])
                elif kind == "models":
                    self.models_btn.config(state="normal")
                    self.status.config(text="Готов")
                    if item[1]:
                        self.model_box.config(values=item[1])
                        self.append("Моделей: %d. Выберите в списке «Модель»." % len(item[1]))
                    elif item[1] is not None:
                        self.append("Подходящих моделей не найдено.")
                elif kind == "update":
                    self.on_update(item[1], item[2])
                elif kind == "restart":
                    self.stop_ev.set()
                    self.save()
                    self.root.destroy()
                    return
                elif kind == "done":
                    self.finish()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        self.stop_ev.set()
        self.save()
        self.root.destroy()


def main():
    try:  # чёткий рендер на экранах с масштабом
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
