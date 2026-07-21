#!/usr/bin/env python3
"""GUI wrapper for corresponding_author_fetcher.py using Tkinter.

Launches the existing CLI script as a subprocess, captures its combined
stdout/stderr output in real time, and displays it in a scrollable text
widget.  No core business logic is duplicated here — all the work is
delegated to corresponding_author_fetcher.py.

Usage:
    python gui.py
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


# ---- paths ------------------------------------------------------------------

# Resolve base directory ? works in dev mode and when frozen by PyInstaller
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys._MEIPASS)                 # PyInstaller temp directory
else:
    _BASE_DIR = Path(__file__).resolve().parent      # normal Python execution

SCRIPT_DIR = _BASE_DIR
MAIN_SCRIPT = _BASE_DIR / "corresponding_author_fetcher.py"


# ---- main application class -------------------------------------------------

class CorrespondingAuthorGUI:
    """Tkinter window that drives corresponding_author_fetcher.py."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Corresponding Author Fetcher")
        self.root.geometry("900x700")
        self.root.minsize(760, 580)

        # Subprocess state
        self._proc: subprocess.Popen[str] | None = None
        self._output_queue: queue.Queue[tuple[str, str | int]] = queue.Queue()

        # Results tracking for "Open Results Folder" button
        self._results_path: str = ""

        self._build_ui()
        self._poll_queue()

        # Tear-down the subprocess when the window is closed
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI construction --------------------------------------------------

    def _build_ui(self) -> None:
        """Create and grid all widgets — Chinese UI for medical researchers."""
        main = ttk.Frame(self.root, padding="20 16 20 16")
        main.pack(fill=tk.BOTH, expand=True)

        # --- header ---
        header = ttk.Frame(main)
        header.pack(fill=tk.X, pady=(0, 16))
        ttk.Label(
            header,
            text="Corresponding Author Fetcher",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            header,
            text="科研文献通讯作者检索工具",
            font=("Segoe UI", 10),
            foreground="#666",
        ).pack(anchor=tk.W, pady=(2, 0))

        # --- input section ---
        input_frame = ttk.LabelFrame(
            main, text="检索参数", padding="16 14 16 14"
        )
        input_frame.pack(fill=tk.X, pady=(0, 12))
        input_frame.columnconfigure(1, weight=1)

        # Author Name (required)
        ttk.Label(input_frame, text="作者姓名：", width=10, anchor=tk.E).grid(
            row=0, column=0, sticky=tk.W, pady=6, padx=(4, 8)
        )
        self.name_var = tk.StringVar(value="例如：She Yuhao")
        self.name_entry = ttk.Entry(input_frame, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=1, sticky=tk.EW, pady=6)

        # Institution (required)
        ttk.Label(input_frame, text="所属机构：", width=10, anchor=tk.E).grid(
            row=1, column=0, sticky=tk.W, pady=6, padx=(4, 8)
        )
        self.inst_var = tk.StringVar(value="例如：Peking Union Medical College Hospital")
        self.inst_entry = ttk.Entry(input_frame, textvariable=self.inst_var)
        self.inst_entry.grid(row=1, column=1, sticky=tk.EW, pady=6)

        # Email (optional)
        ttk.Label(input_frame, text="电子邮箱：", width=10, anchor=tk.E).grid(
            row=2, column=0, sticky=tk.W, pady=6, padx=(4, 8)
        )
        self.email_var = tk.StringVar(value="例如：Sheyuhao521@gmail.com（可选）")
        self.email_entry = ttk.Entry(input_frame, textvariable=self.email_var)
        self.email_entry.grid(row=2, column=1, sticky=tk.EW, pady=6)

        # Year range
        ttk.Label(input_frame, text="年份范围：", width=10, anchor=tk.E).grid(
            row=3, column=0, sticky=tk.W, pady=6, padx=(4, 8)
        )
        yr = ttk.Frame(input_frame)
        yr.grid(row=3, column=1, sticky=tk.W, pady=6)
        current_year = time.localtime().tm_year
        ttk.Label(yr, text="从").pack(side=tk.LEFT)
        self.from_var = tk.StringVar(value=str(current_year - 5))
        self.from_entry = ttk.Entry(yr, textvariable=self.from_var, width=6)
        self.from_entry.pack(side=tk.LEFT, padx=(4, 14))
        ttk.Label(yr, text="至").pack(side=tk.LEFT)
        self.to_var = tk.StringVar(value=str(current_year))
        self.to_entry = ttk.Entry(yr, textvariable=self.to_var, width=6)
        self.to_entry.pack(side=tk.LEFT, padx=(4, 0))

        # Checkboxes
        opt = ttk.Frame(main)
        opt.pack(fill=tk.X, pady=(0, 12))
        self.skip_publisher_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt,
            text="跳过出版商页面验证（更快）",
            variable=self.skip_publisher_var,
        ).pack(side=tk.LEFT, padx=(4, 0))
        self.no_download_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt,
            text="不下载 PDF",
            variable=self.no_download_var,
        ).pack(side=tk.LEFT, padx=(20, 0))

        # Buttons
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(0, 10))

        # Custom style for search button
        style = ttk.Style()
        style.configure(
            "Search.TButton",
            font=("Segoe UI", 10, "bold"),
            padding=(24, 6),
        )

        self.search_btn = ttk.Button(
            btn_frame,
            text="▶ 开始检索",
            style="Search.TButton",
            command=self._start_search,
        )
        self.search_btn.pack(side=tk.LEFT)

        self.cancel_btn = ttk.Button(
            btn_frame,
            text="取消",
            command=self._cancel_search,
            state=tk.DISABLED,
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(10, 0))

        self.open_folder_btn = ttk.Button(
            btn_frame,
            text="📂 打开结果文件夹",
            command=self._open_results_folder,
            state=tk.DISABLED,
        )
        self.open_folder_btn.pack(side=tk.LEFT, padx=(10, 0))

        # Output section
        out_frame = ttk.LabelFrame(main, text="运行日志", padding="4 4 4 4")
        out_frame.pack(fill=tk.BOTH, expand=True)

        self.output_text = tk.Text(
            out_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            font=("Consolas", 9),
        )
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(out_frame, command=self.output_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.output_text.configure(yscrollcommand=scrollbar.set)

        # Status bar
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(
            main, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W,
            padding=(6, 2),
        ).pack(fill=tk.X, pady=(10, 0))

        self.name_entry.focus_set()

        # Set up placeholder behaviours
        self._setup_placeholder(self.name_entry, self.name_var,
                                "例如：She Yuhao")
        self._setup_placeholder(self.inst_entry, self.inst_var,
                                "例如：Peking Union Medical College Hospital")
        self._setup_placeholder(self.email_entry, self.email_var,
                                "例如：Sheyuhao521@gmail.com（可选）")

    # -- search orchestration ---------------------------------------------

    def _start_search(self) -> None:
        """Validate form fields and launch the CLI script in a background thread."""
        name = self._strip_placeholder(
            self.name_var.get().strip(), "例如：She Yuhao")
        institution = self._strip_placeholder(
            self.inst_var.get().strip(), "例如：Peking Union Medical College Hospital")
        email = self._strip_placeholder(
            self.email_var.get().strip(), "例如：Sheyuhao521@gmail.com（可选）")
        from_str = self.from_var.get().strip()
        to_str = self.to_var.get().strip()

        # --- validation ---
        if not name:
            messagebox.showerror("输入验证", "请输入作者姓名。")
            return
        if not institution:
            messagebox.showerror("输入验证", "请输入所属机构。")
            return

        try:
            if from_str:
                int(from_str)
            if to_str:
                int(to_str)
        except ValueError:
            messagebox.showerror(
                "输入验证", "年份必须为整数。"
            )
            return

        if from_str and to_str and int(from_str) > int(to_str):
            messagebox.showerror(
                "输入验证", "起始年份不能晚于结束年份。"
            )
            return

        # --- build command ---
        cmd = [
            sys.executable,
            "-u",                    # unbuffered — needed for real-time output
            str(MAIN_SCRIPT),
            "--name", name,
            "--institution", institution,
            "--email", email,         # always pass (empty = no email)
            "--from-year", from_str,
            "--to-year", to_str,
        ]
        if self.skip_publisher_var.get():
            cmd.append("--skip-publisher-page-check")
        if self.no_download_var.get():
            cmd.append("--no-download")

        # Reset results tracking
        self._results_path = ""
        self.open_folder_btn.configure(state=tk.DISABLED)

        # --- prepare UI ---
        self._clear_output()
        self._set_running(True)
        self._append_output(f"> {' '.join(cmd)}\n\n")

        # --- launch ---
        thread = threading.Thread(
            target=self._run_subprocess, args=(cmd,), daemon=True
        )
        thread.start()

    def _run_subprocess(self, cmd: list[str]) -> None:
        """Execute *cmd* in a subprocess, pushing every line to the queue."""
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,          # merge stderr → stdout
                cwd=str(SCRIPT_DIR),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            # -- robust binary-mode reader with process-exit detection --
            # On Windows the TextIOWrapper layer can fail to detect EOF
            # when a child exits quickly, causing readline() to block
            # forever.  We use a reader thread + polling loop so the pipe
            # is always drained and process exit is reliably detected.
            inner: queue.Queue[bytes | None] = queue.Queue()

            def _reader() -> None:
                """Read raw chunks from the pipe; push None on EOF/error."""
                try:
                    while True:
                        chunk = self._proc.stdout.read(4096)
                        if not chunk:            # EOF
                            break
                        inner.put(chunk)
                except Exception:
                    pass
                finally:
                    inner.put(None)               # sentinel

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            buf = b""
            while True:
                try:
                    item = inner.get(timeout=0.15)
                    if item is None:
                        break                     # reader signalled EOF
                    buf += item
                    # emit complete lines immediately for real-time output
                    while b"\n" in buf:
                        nl = buf.index(b"\n") + 1
                        line = buf[:nl].decode("utf-8", errors="replace")
                        buf = buf[nl:]
                        self._output_queue.put(("output", line))
                except queue.Empty:
                    if self._proc.poll() is not None:
                        # child exited — close pipe to unblock reader
                        try:
                            self._proc.stdout.close()
                        except Exception:
                            pass
                        break

            if buf:
                self._output_queue.put(
                    ("output", buf.decode("utf-8", errors="replace"))
                )
            returncode = self._proc.wait()
            self._output_queue.put(("done", returncode))
        except Exception as exc:
            self._output_queue.put(("error", str(exc)))
        finally:
            self._proc = None

    def _cancel_search(self) -> None:
        """Terminate a running search gracefully."""
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._append_output("\n[\u5df2取消\u68c0\u7d22]\n")

    # -- thread-safe output helpers ---------------------------------------

    def _append_output(self, text: str) -> None:
        """Schedule *text* to be appended to the output widget on the main thread."""
        self.root.after(0, self._append_output_now, text)

    def _append_output_now(self, text: str) -> None:
        """Actually insert *text* into the output widget (main-thread only)."""
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)
        self.output_text.configure(state=tk.DISABLED)

    def _clear_output(self) -> None:
        """Wipe the output widget."""
        self.output_text.configure(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self.output_text.configure(state=tk.DISABLED)

    # -- queue polling ----------------------------------------------------

    def _poll_queue(self) -> None:
        """Periodically drain the message queue on the main thread."""
        try:
            while True:
                kind, payload = self._output_queue.get_nowait()
                if kind == "output":
                    self._append_output_now(str(payload))
                elif kind == "done":
                    returncode = int(payload)
                    self._set_running(False)
                    if returncode == 0:
                        self._append_output_now("\n\u2500\u2500 检索完成 \u2500\u2500\n")
                        # exit code 0 always means success; check if the
                        # search came back empty so we can give a hint
                        all_text = self.output_text.get("1.0", tk.END)
                        if "identity-filtered: 0" in all_text:
                            self._append_output_now(
                                "未找到匹配的论文。\n"
                            )
                        else:
                            # Extract results folder path
                            for line in reversed(all_text.splitlines()):
                                if line.startswith("Results: "):
                                    self._results_path = line[len("Results: "):].strip()
                                    self.open_folder_btn.configure(state=tk.NORMAL)
                                    break
                        self.status_var.set("检索完成\u3002")
                    else:
                        self._append_output_now("\n\u2500\u2500 检索异常 \u2500\u2500\n")
                        self.status_var.set(
                            f"检索异常\u7ed3\u675f\uff08\u9000\u51fa\u7801\uff1a{returncode}\uff09\uff0c\u8bf7\u67e5\u770b\u4e0a\u65b9\u65e5\u5fd7\u4e86\u89e3\u8be6\u60c5\u3002"
                        )
                elif kind == "error":
                    self._append_output_now(f"\n[错误] {payload}\n")
                    self._append_output_now("\u2500\u2500 检索失败 \u2500\u2500\n")
                    self._set_running(False)
                    self.status_var.set("检索失败\uff0c\u8bf7\u67e5\u770b\u4e0a\u65b9\u65e5\u5fd7\u4e86\u89e3\u8be6\u60c5\u3002")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    # -- UI state management ----------------------------------------------

    def _set_running(self, running: bool) -> None:
        """Toggle widget states between idle and searching."""
        state_normal = tk.NORMAL if not running else tk.DISABLED
        state_cancel = tk.NORMAL if running else tk.DISABLED

        self.search_btn.configure(state=state_normal)
        self.cancel_btn.configure(state=state_cancel)
        for w in (self.name_entry, self.inst_entry, self.email_entry,
                  self.from_entry, self.to_entry):
            w.configure(state="" if not running else "disabled")

        if running:
            self.open_folder_btn.configure(state=tk.DISABLED)
            self._results_path = ""
            self.status_var.set("正在检索，请稍候……")

    def _open_results_folder(self) -> None:
        """Open the most recent results folder in Windows Explorer."""
        path = self._results_path
        if path and Path(path).exists():
            os.startfile(path)
        else:
            messagebox.showwarning(
                "提示", "结果文件夹不存在或已被移动。"
            )

    def _setup_placeholder(self, entry: ttk.Entry, var: tk.StringVar,
                            placeholder: str) -> None:
        """Add placeholder behaviour: gray hint text that clears on focus."""
        entry.configure(foreground="gray")

        def on_focus_in(_event):
            if var.get() == placeholder:
                var.set("")
                entry.configure(foreground="black")

        def on_focus_out(_event):
            if var.get().strip() == "":
                var.set(placeholder)
                entry.configure(foreground="gray")

        entry.bind("<FocusIn>", on_focus_in)
        entry.bind("<FocusOut>", on_focus_out)

    def _strip_placeholder(self, value: str, placeholder: str) -> str:
        """Return empty string if *value* is the placeholder text."""
        return "" if value == placeholder else value

    def _on_close(self) -> None:
        """Handle window close: terminate subprocess and destroy."""
        self._cancel_search()
        self.root.destroy()


# ---- entry point ------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    _ = CorrespondingAuthorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
