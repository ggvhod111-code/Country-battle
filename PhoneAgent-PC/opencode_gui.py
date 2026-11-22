"""Окно «opencode»: чат с настоящим opencode (кодинг-агент), провайдеры/ключи, правила, навыки, MCP."""
import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, ttk

import opencode_client as oc


class TextEditor(tk.Toplevel):
    def __init__(self, parent, title, hint, text, on_save, example=None):
        super().__init__(parent)
        self.title(title)
        self.geometry("640x480")
        self.transient(parent)
        ttk.Label(self, text=hint, wraplength=610, foreground="#555").pack(anchor="w", padx=10, pady=(10, 4))
        self.box = scrolledtext.ScrolledText(self, wrap="word", font=("Consolas", 10))
        self.box.pack(fill="both", expand=True, padx=10)
        self.box.insert("1.0", text)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=8)
        if example:
            ttk.Button(bar, text="Вставить пример", command=lambda: (self.box.delete("1.0", "end"),
                                                                    self.box.insert("1.0", example))).pack(side="left")
        ttk.Button(bar, text="Сохранить", command=lambda: self.save(on_save)).pack(side="right")
        ttk.Button(bar, text="Отмена", command=self.destroy).pack(side="right", padx=6)

    def save(self, on_save):
        try:
            on_save(self.box.get("1.0", "end").strip())
        except oc.OCError as e:
            messagebox.showerror("opencode", str(e), parent=self)
            return
        self.destroy()


class OpenCodeWindow(tk.Toplevel):
    def __init__(self, root):
        super().__init__(root)
        self.title("opencode — кодинг-агент")
        self.geometry("820x700")
        self.minsize(640, 520)
        self.q = queue.Queue()
        self.server = None
        self.sid = None
        self.busy = False
        self.providers = []
        self.connected = []
        self.stop_ev = threading.Event()

        f = ttk.Frame(self, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        self.status = ttk.Label(f, text="", wraplength=780)
        self.status.grid(row=0, column=0, columnspan=4, sticky="w")
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 6))
        self.inst = ttk.Button(bar, text="Установить opencode", command=self.install)
        self.inst.pack(side="left")
        self.conn = ttk.Button(bar, text="Подключиться", command=self.connect)
        self.conn.pack(side="left", padx=6)
        for text, cmd in (("Правила", self.rules), ("Навыки", self.skills), ("MCP", self.mcp)):
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Button(bar, text="Сайт opencode", command=lambda: webbrowser.open("https://opencode.ai")).pack(
            side="left", padx=6)

        ttk.Label(f, text="Рабочая папка").grid(row=2, column=0, sticky="w", pady=2)
        self.cwd = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "opencode-work"))
        ttk.Entry(f, textvariable=self.cwd).grid(row=2, column=1, columnspan=2, sticky="ew", padx=6)
        ttk.Button(f, text="Обзор…", command=self.browse).grid(row=2, column=3)

        ttk.Label(f, text="Провайдер").grid(row=3, column=0, sticky="w", pady=2)
        self.prov = tk.StringVar()
        self.prov_box = ttk.Combobox(f, textvariable=self.prov, state="readonly")
        self.prov_box.grid(row=3, column=1, sticky="ew", padx=6)
        self.prov_box.bind("<<ComboboxSelected>>", lambda e: self.on_prov())
        self.key = tk.StringVar()
        self.key_entry = ttk.Entry(f, textvariable=self.key, show="•")
        self.key_entry.grid(row=3, column=2, sticky="ew")
        ttk.Button(f, text="Сохранить ключ", command=self.save_key).grid(row=3, column=3, padx=(6, 0))

        ttk.Label(f, text="Модель").grid(row=4, column=0, sticky="w", pady=2)
        self.model = tk.StringVar()
        self.model_box = ttk.Combobox(f, textvariable=self.model)
        self.model_box.grid(row=4, column=1, columnspan=2, sticky="ew", padx=6)
        self.auto = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Не спрашивать разрешений", variable=self.auto).grid(row=4, column=3, padx=(6, 0))

        self.log = scrolledtext.ScrolledText(f, wrap="word", state="disabled")
        self.log.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=8)
        f.rowconfigure(5, weight=1)
        self.entry = tk.Text(f, height=3, wrap="word")
        self.entry.grid(row=6, column=0, columnspan=3, sticky="ew")
        self.entry.bind("<Control-Return>", lambda e: (self.send(), "break")[1])
        bb = ttk.Frame(f)
        bb.grid(row=6, column=3, padx=(6, 0))
        self.send_btn = ttk.Button(bb, text="Отправить", command=self.send)
        self.send_btn.pack(fill="x")
        self.stop_btn = ttk.Button(bb, text="Стоп", command=self.stop, state="disabled")
        self.stop_btn.pack(fill="x", pady=(4, 0))
        ttk.Label(f, text="Ctrl+Enter — отправить. Агент читает и меняет файлы только в выбранной папке "
                          "и спрашивает разрешение на опасные действия.", foreground="#777").grid(
            row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()
        self.after(100, self.poll)

    # ---------- вспомогательное
    def say(self, text):
        self.q.put(("log", text))

    def append(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def refresh(self):
        exe = oc.find_exe()
        if self.server:
            self.status.config(text="opencode запущен. Выберите провайдера, модель и пишите задачу.")
        elif exe:
            self.status.config(text="opencode установлен (%s). Нажмите «Подключиться»." % exe)
        else:
            self.status.config(text="opencode не установлен. Нажмите «Установить opencode»: скачается с GitHub "
                                    "(официальная сборка, Python и Node не нужны).")
        self.inst.config(state="disabled" if exe else "normal")
        self.conn.config(state="disabled" if self.server else "normal")

    def browse(self):
        d = filedialog.askdirectory(parent=self, initialdir=self.cwd.get() or None)
        if d:
            self.cwd.set(d)

    def thread(self, fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self.say("Ошибка: %s" % e)
                self.q.put(("idle",))
        threading.Thread(target=run, daemon=True).start()

    # ---------- установка и запуск
    def install(self):
        if not messagebox.askyesno("opencode", "Скачать opencode с GitHub (около 50–150 МБ) и положить в папку "
                                   "приложения?", parent=self):
            return
        self.inst.config(state="disabled")
        self.append("Устанавливаю opencode…")

        def work():
            oc.install(self.say)
            self.q.put(("refresh",))
        self.thread(work)

    def connect(self):
        exe = oc.find_exe()
        cwd = self.cwd.get().strip()
        if not exe:
            messagebox.showinfo("opencode", "opencode не найден. Нажмите «Установить opencode».", parent=self)
            return
        if not cwd:
            messagebox.showinfo("opencode", "Укажите рабочую папку.", parent=self)
            return
        os.makedirs(cwd, exist_ok=True)
        self.conn.config(state="disabled")
        self.append("Запускаю opencode в папке %s…" % cwd)

        def work():
            srv = oc.Server(exe, cwd)
            srv.start()
            self.server = srv
            items, con = srv.providers()
            self.sid = srv.new_session()
            threading.Thread(target=self.listen, args=(srv, self.sid), daemon=True).start()
            self.q.put(("ready", items, con))
        self.thread(work)

    def listen(self, srv, sid):
        r = oc.Renderer(sid, self.say, lambda p: self.q.put(("perm", p)), lambda: self.q.put(("idle",)))
        while self.server is srv and not self.stop_ev.is_set():
            try:
                for ev in srv.events():
                    r.handle(ev)
            except Exception:  # noqa: BLE001
                if self.server is not srv:
                    return
                threading.Event().wait(1.5)

    def on_ready(self, items, con):
        self.providers, self.connected = items, con
        names = ["%s%s" % ("✓ " if pid in con else "", name) for pid, name, _ in items]
        self.prov_box.config(values=names)
        pick = next((i for i, (pid, _, _) in enumerate(items) if pid == "opencode" and pid in con), 0)
        self.prov_box.current(pick)
        self.on_prov()
        self.refresh()
        self.append("Готово. Провайдеры с ✓ уже подключены. Для остальных вставьте API-ключ и нажмите «Сохранить ключ».")

    def cur_provider(self):
        i = self.prov_box.current()
        return self.providers[i] if 0 <= i < len(self.providers) else None

    def on_prov(self):
        p = self.cur_provider()
        if p:
            self.model_box.config(values=p[2])
            self.model.set(p[2][0] if p[2] else "")
            self.key.set("")

    def save_key(self):
        p, key = self.cur_provider(), self.key.get().strip()
        if not self.server or not p or not key:
            messagebox.showinfo("opencode", "Нажмите «Подключиться», выберите провайдера и введите ключ.", parent=self)
            return

        def work():
            self.server.set_key(p[0], key)
            items, con = self.server.providers()
            self.q.put(("keys", items, con, p[0]))
        self.thread(work)

    # ---------- чат
    def send(self):
        text = self.entry.get("1.0", "end").strip()
        p = self.cur_provider()
        if not self.server or self.busy or not text:
            if not self.server:
                messagebox.showinfo("opencode", "Сначала нажмите «Подключиться».", parent=self)
            return
        if not p or not self.model.get().strip():
            messagebox.showinfo("opencode", "Выберите провайдера и модель.", parent=self)
            return
        if p[0] not in self.connected:
            messagebox.showinfo("opencode", "Провайдер не подключён: введите API-ключ и нажмите «Сохранить ключ».",
                                parent=self)
            return
        self.entry.delete("1.0", "end")
        self.append("Вы: " + text)
        self.busy = True
        self.send_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        srv, sid, model = self.server, self.sid, self.model.get().strip()
        self.thread(lambda: srv.prompt(sid, text, p[0], model))

    def stop(self):
        if self.server and self.sid:
            self.thread(lambda: self.server.abort(self.sid))

    def ask_permission(self, p):
        if self.auto.get():
            self.thread(lambda: self.server.reply_permission(p["id"], "once"))
            return
        pats = ", ".join(p.get("patterns", []))[:300]
        ans = messagebox.askyesnocancel(
            "opencode просит разрешение",
            "Действие: %s\n%s\n\nДа — разрешить один раз, Нет — запретить, Отмена — запретить." % (
                p.get("permission", "?"), pats), parent=self)
        reply = "once" if ans else "reject"
        self.thread(lambda: self.server.reply_permission(p["id"], reply))

    def poll(self):
        try:
            while True:
                it = self.q.get_nowait()
                k = it[0]
                if k == "log":
                    self.append(it[1])
                elif k == "idle":
                    self.busy = False
                    self.send_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.refresh()
                elif k == "refresh":
                    self.refresh()
                elif k == "ready":
                    self.on_ready(it[1], it[2])
                elif k == "keys":
                    self.providers, self.connected = it[1], it[2]
                    cur = it[3]
                    self.prov_box.config(values=["%s%s" % ("✓ " if pid in it[2] else "", n) for pid, n, _ in it[1]])
                    idx = next((i for i, x in enumerate(it[1]) if x[0] == cur), 0)
                    self.prov_box.current(idx)
                    self.on_prov()
                    self.append("Ключ сохранён." if cur in it[2] else "Ключ сохранён, но провайдер не подключился.")
                elif k == "perm":
                    self.ask_permission(it[1])
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self.poll)

    # ---------- правила, навыки, MCP (применяются при следующем запуске opencode)
    def restart_note(self):
        if self.server:
            messagebox.showinfo("opencode", "Изменения подхватятся при следующем запуске: закройте это окно "
                                "и откройте снова.", parent=self)

    def rules(self):
        def save(t):
            oc.write_rules(t)
            self.restart_note()
        TextEditor(self, "Правила (AGENTS.md)",
                   "Глобальные правила для агента: стиль, язык ответов, запреты. Файл: %s" % oc.rules_path(),
                   oc.read_rules(), save)

    def mcp(self):
        def save(t):
            oc.write_mcp(t)
            self.restart_note()
        TextEditor(self, "MCP-серверы",
                   "JSON-объект серверов (раздел \"mcp\" настроек opencode). type: local (command — список) или "
                   "remote (url, headers). Файл: %s" % os.path.join(oc.config_dir(), "opencode.json"),
                   oc.read_mcp(), save, example=oc.MCP_EXAMPLE)

    def skills(self):
        w = tk.Toplevel(self)
        w.title("Навыки")
        w.geometry("420x360")
        w.transient(self)
        ttk.Label(w, text="Навык — папка со SKILL.md (name и description в шапке). Можно добавить файл SKILL.md, "
                          "папку или zip.", wraplength=390, foreground="#555").pack(anchor="w", padx=10, pady=8)
        lb = tk.Listbox(w)
        lb.pack(fill="both", expand=True, padx=10)

        def fill():
            lb.delete(0, "end")
            for n in oc.list_skills():
                lb.insert("end", n)

        def add(kind):
            path = filedialog.askdirectory(parent=w) if kind == "dir" else filedialog.askopenfilename(
                parent=w, filetypes=[("SKILL.md или zip", "*.md *.zip")])
            if not path:
                return
            try:
                name = oc.add_skill(path)
            except (oc.OCError, OSError, ValueError) as e:
                messagebox.showerror("Навыки", str(e), parent=w)
                return
            fill()
            messagebox.showinfo("Навыки", "Добавлен навык «%s»." % name, parent=w)
            self.restart_note()

        def remove():
            sel = lb.curselection()
            if sel and messagebox.askyesno("Навыки", "Удалить навык?", parent=w):
                oc.remove_skill(lb.get(sel[0]))
                fill()
        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="Файл/zip…", command=lambda: add("file")).pack(side="left")
        ttk.Button(bar, text="Папка…", command=lambda: add("dir")).pack(side="left", padx=6)
        ttk.Button(bar, text="Удалить", command=remove).pack(side="right")
        fill()

    def close(self):
        self.stop_ev.set()
        srv, self.server = self.server, None
        if srv:
            srv.stop()
        self.destroy()
