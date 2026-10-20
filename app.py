#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""相似图检索 —— 图形界面

双击 run.bat 启动。选好文件夹和样图，点「开始检索」，结果会以网页报告形式打开。
"""
import io
import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
try:
    import find_similar as fs  # noqa: E402
except Exception:
    import ctypes
    ctypes.windll.user32.MessageBoxW(
        0, "无法加载 find_similar.py，请确认它和本程序在同一个文件夹。\n\n"
           + traceback.format_exc(), "相似图检索", 0x10)
    raise SystemExit(1)

CONFIG = SCRIPT_DIR / "config.json"

if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()


class QueueWriter(io.TextIOBase):
    """把 print 输出搬到 GUI 的日志框里。"""

    def __init__(self, q):
        self.q = q
        self.buf = ""

    def write(self, s):
        self.buf += s
        while True:
            i_n = self.buf.find("\n")
            i_r = self.buf.find("\r")
            idx = [i for i in (i_n, i_r) if i >= 0]
            if not idx:
                break
            i = min(idx)
            line = self.buf[:i].strip()
            self.buf = self.buf[i + 1:]
            if line:
                self.q.put(line)
        return len(s)

    def flush(self):
        if self.buf.strip():
            self.q.put(self.buf.strip())
            self.buf = ""


class App:
    def __init__(self, root, selftest=False):
        self.root = root
        self.selftest = selftest
        self.q = queue.Queue()
        self.busy = False
        self.last_report = None

        root.title("相似图检索")
        root.geometry("920x660")
        root.minsize(760, 560)
        root.configure(bg="#f5f6f8")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#f5f6f8")
        style.configure("Card.TFrame", background="#ffffff")
        style.configure("TLabel", background="#f5f6f8", foreground="#1f2328",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Card.TLabel", background="#ffffff", foreground="#1f2328",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Hint.TLabel", background="#ffffff", foreground="#8a919b",
                        font=("Microsoft YaHei UI", 8))
        style.configure("Title.TLabel", background="#f5f6f8", foreground="#111418",
                        font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Sub.TLabel", background="#f5f6f8", foreground="#6b7280",
                        font=("Microsoft YaHei UI", 9))
        style.configure("Go.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(18, 8))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(10, 5))
        style.configure("TEntry", padding=5)
        style.configure("TSpinbox", padding=4)
        style.configure("TCheckbutton", background="#ffffff", font=("Microsoft YaHei UI", 9))
        style.configure("TProgressbar", thickness=6)

        self._build()
        self._load_config()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self._poll)

    # ---------------- 界面 ----------------
    def _build(self):
        head = ttk.Frame(self.root, padding=(24, 20, 24, 0))
        head.pack(fill="x")
        ttk.Label(head, text="相似图检索", style="Title.TLabel").pack(anchor="w")
        ttk.Label(head, text="在图片文件夹里找出与样图最相似的前 N 张（本地运行，无需联网）",
                  style="Sub.TLabel").pack(anchor="w", pady=(4, 0))

        card = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e3e6ea",
                        highlightthickness=1, bd=0)
        card.pack(fill="x", padx=24, pady=16)
        inner = ttk.Frame(card, style="Card.TFrame", padding=(18, 16))
        inner.pack(fill="x")
        inner.columnconfigure(1, weight=1)

        ttk.Label(inner, text="图片文件夹", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        self.var_folder = tk.StringVar()
        ttk.Entry(inner, textvariable=self.var_folder).grid(
            row=0, column=1, sticky="ew", pady=6)
        ttk.Button(inner, text="选择文件夹", command=self.browse_folder).grid(
            row=0, column=2, padx=(10, 0), pady=6)

        ttk.Label(inner, text="样图", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.var_query = tk.StringVar()
        ttk.Entry(inner, textvariable=self.var_query).grid(
            row=1, column=1, sticky="ew", pady=6)
        ttk.Button(inner, text="选择图片", command=self.browse_query).grid(
            row=1, column=2, padx=(10, 0), pady=6)

        opts = ttk.Frame(inner, style="Card.TFrame")
        opts.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(opts, text="返回前", style="Card.TLabel").pack(side="left")
        self.var_top = tk.StringVar(value="10")
        ttk.Spinbox(opts, from_=1, to=500, width=6, textvariable=self.var_top).pack(
            side="left", padx=(6, 4))
        ttk.Label(opts, text="张", style="Card.TLabel").pack(side="left")

        ttk.Label(opts, text="　最低分", style="Card.TLabel").pack(side="left")
        self.var_thr = tk.StringVar(value="0")
        ttk.Spinbox(opts, from_=0, to=100, width=6, textvariable=self.var_thr).pack(
            side="left", padx=(6, 4))
        ttk.Label(opts, text="分", style="Card.TLabel").pack(side="left")

        self.var_copy = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="　把命中图片复制到 output\\命中结果",
                        variable=self.var_copy).pack(side="left", padx=(18, 0))

        self.var_rebuild = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="　强制重建索引", variable=self.var_rebuild).pack(
            side="left", padx=(6, 0))

        # 操作条
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=24)
        self.btn_go = ttk.Button(bar, text="开始检索", style="Go.TButton", command=self.start)
        self.btn_go.pack(side="left")
        self.btn_open = ttk.Button(bar, text="打开结果报告", command=self.open_report,
                                   state="disabled")
        self.btn_open.pack(side="left", padx=(10, 0))

        self.var_status = tk.StringVar(value="选择好文件夹和样图后点「开始检索」")
        ttk.Label(bar, textvariable=self.var_status, style="Sub.TLabel").pack(
            side="left", padx=(16, 0))

        self.pbar = ttk.Progressbar(self.root, mode="indeterminate")
        self.pbar.pack(fill="x", padx=24, pady=(12, 0))

        # 日志
        logwrap = tk.Frame(self.root, bg="#ffffff", highlightbackground="#e3e6ea",
                           highlightthickness=1, bd=0)
        logwrap.pack(fill="both", expand=True, padx=24, pady=(12, 20))
        self.log = tk.Text(logwrap, bg="#ffffff", fg="#25292e", bd=0, wrap="word",
                           font=("Consolas", 9), padx=14, pady=12,
                           insertbackground="#25292e", state="disabled")
        sb = ttk.Scrollbar(logwrap, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    # ---------------- 交互 ----------------
    def log_line(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def browse_folder(self):
        p = filedialog.askdirectory(title="选择图片文件夹")
        if p:
            self.var_folder.set(p)

    def browse_query(self):
        p = filedialog.askopenfilename(
            title="选择样图",
            filetypes=[("图片", "*.jpg *.jpeg *.png *.bmp *.webp *.gif *.tif *.tiff"),
                       ("所有文件", "*.*")])
        if p:
            self.var_query.set(p)

    def open_report(self):
        if self.last_report and Path(self.last_report).exists():
            os.startfile(str(self.last_report))

    def _int(self, var, default):
        try:
            return int(str(var.get()).strip())
        except Exception:
            return default

    def start(self):
        if self.busy:
            return
        folder = self.var_folder.get().strip().strip('"')
        query = self.var_query.get().strip().strip('"')
        if not folder or not Path(folder).is_dir():
            messagebox.showwarning("提示", "请先选择一个存在的图片文件夹。")
            return
        if not query or not Path(query).is_file():
            messagebox.showwarning("提示", "请先选择一张样图。")
            return

        top_n = max(1, self._int(self.var_top, 10))
        thr = max(0, min(100, self._int(self.var_thr, 0)))

        self.busy = True
        self.btn_go.configure(state="disabled")
        self.btn_open.configure(state="disabled")
        self.var_status.set("正在检索…")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.pbar.start(14)
        if not self.selftest:
            self._save_config()

        args = dict(folder=Path(folder), query=Path(query), top_n=top_n,
                    workers=min(8, os.cpu_count() or 4),
                    rebuild=bool(self.var_rebuild.get()), use_orb=True,
                    threshold=thr)
        t = threading.Thread(target=self._worker, args=(args,), daemon=True)
        t.start()

    def _worker(self, args):
        t0 = time.time()
        old = sys.stdout
        try:
            sys.stdout = QueueWriter(self.q)
            q, results = fs.search(args["folder"], args["query"], args["top_n"],
                                   args["workers"], args["rebuild"], args["use_orb"],
                                   args["threshold"])
            sys.stdout = old
            self.q.put(("done", (q, results, time.time() - t0)))
        except Exception:
            sys.stdout = old
            self.q.put(("error", traceback.format_exc()))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "done":
                    self._finish(*item[1])
                elif isinstance(item, tuple) and item and item[0] == "error":
                    self._fail(item[1])
                else:
                    self.log_line(str(item))
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _finish(self, q, results, elapsed):
        self.busy = False
        self.pbar.stop()
        self.btn_go.configure(state="normal")
        if q is None:
            self.var_status.set("没有可用索引")
            return
        if not results:
            self.var_status.set("没有符合条件的结果")
            self.log_line("没有找到符合条件的结果，可降低「最低分」或调大「返回前 N 张」。")
            return

        out = fs.OUTPUT_DIR / f"report_{time.strftime('%Y%m%d_%H%M%S')}.html"
        fs.write_report(Path(self.var_query.get().strip()), results, out,
                        Path(self.var_folder.get().strip()))
        meta = out.with_suffix(".json")
        meta.write_text(json.dumps({"query": self.var_query.get(), "folder": self.var_folder.get(),
                                    "results": results}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        self.last_report = str(out)

        self.log_line("")
        self.log_line(f"—— 完成，用时 {elapsed:.1f} 秒 ——")
        self.log_line(f"{'排名':<6}{'分数':<10}{'pHash':<9}{'ORB':<8}文件")
        for i, r in enumerate(results, 1):
            orb = "-" if r["orb"] is None else f"{r['orb']:.2f}"
            self.log_line(f"{i:<8}{r['score']:<11.1f}{r['phash_dist']:<10}{orb:<9}{r['file']}")
        self.log_line("")
        self.log_line(f"报告：{out}")

        if self.var_copy.get():
            dst = fs.OUTPUT_DIR / "命中结果"
            dst.mkdir(parents=True, exist_ok=True)
            n = 0
            for i, r in enumerate(results, 1):
                src = Path(r["path"])
                try:
                    import shutil
                    shutil.copy2(src, dst / f"{i:02d}_{r['score']:.0f}_{src.name}")
                    n += 1
                except Exception as e:
                    self.log_line(f"复制失败 {src.name}: {e}")
            self.log_line(f"已复制 {n} 张到：{dst}")

        self.btn_open.configure(state="normal")
        self.var_status.set(f"找到 {len(results)} 张，最高 {results[0]['score']:.1f} 分")
        if not self.selftest:
            self.open_report()

    def _fail(self, tb):
        self.busy = False
        self.pbar.stop()
        self.btn_go.configure(state="normal")
        self.var_status.set("出错了，详见下方日志")
        self.log_line(tb)
        (SCRIPT_DIR / "error.log").write_text(tb, encoding="utf-8")

    # ---------------- 配置 ----------------
    def _load_config(self):
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            self.var_folder.set(cfg.get("folder", ""))
            self.var_query.set(cfg.get("query", ""))
            self.var_top.set(str(cfg.get("top", 10)))
            self.var_thr.set(str(cfg.get("threshold", 0)))
        except Exception:
            pass

    def _save_config(self):
        try:
            CONFIG.write_text(json.dumps({
                "folder": self.var_folder.get(),
                "query": self.var_query.get(),
                "top": self._int(self.var_top, 10),
                "threshold": self._int(self.var_thr, 0),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def on_close(self):
        self._save_config()
        self.root.destroy()


def main():
    selftest = "--selftest" in sys.argv
    root = tk.Tk()
    if selftest:
        root.withdraw()
    app = App(root, selftest=selftest)

    if selftest:
        base = SCRIPT_DIR / "test"
        app.var_folder.set(str(base / "album"))
        app.var_query.set(str(base / "album" / "v_blur.jpg"))
        app.var_top.set("5")
        root.update()
        app.start()

        deadline = time.time() + 120
        while time.time() < deadline:
            root.update()
            if not app.busy and app.last_report:
                break
            time.sleep(0.05)
        root.update()
        print("SELFTEST folder =", app.var_folder.get())
        print("SELFTEST report =", app.last_report)
        print("SELFTEST status =", app.var_status.get())
        root.destroy()
        return

    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            (SCRIPT_DIR / "error.log").write_text(tb, encoding="utf-8")
        except Exception:
            pass
        try:
            r = tk.Tk()
            r.withdraw()
            messagebox.showerror("相似图检索 启动失败", tb)
        except Exception:
            pass
