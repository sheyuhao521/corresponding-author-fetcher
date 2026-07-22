#!/usr/bin/env python3
"""GUI wrapper for corresponding_author_fetcher.py using Tkinter.

Calls the shared search API in a background thread, captures progress
messages in real time, and displays them in a scrollable text
widget.  No core business logic is duplicated here — all the work is
delegated to corresponding_author_fetcher.py.

Usage:
    python gui.py
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from corresponding_author_fetcher import SearchOptions, SearchResult, run_search


# ---- main application class -------------------------------------------------

class CorrespondingAuthorGUI:
    """Tkinter window that drives corresponding_author_fetcher.py."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Corresponding Author Fetcher")
        self.root.geometry("900x700")
        self.root.minsize(760, 580)

        # Search worker state
        self._cancel_event: threading.Event | None = None
        self._worker_thread: threading.Thread | None = None
        self._output_queue: queue.Queue[tuple[str, str | SearchResult]] = queue.Queue()

        # Results tracking for "Open Results Folder" button
        self._results_path: str = ""

        self._build_ui()
        self._poll_queue()

        # Signal the worker thread when the window is closed
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
        """Validate form fields and launch run_search() in a background thread."""
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
            from_year = int(from_str) if from_str else 1900
            to_year = int(to_str) if to_str else time.localtime().tm_year
        except ValueError:
            messagebox.showerror(
                "输入验证", "年份必须为整数。"
            )
            return

        if from_year > to_year:
            messagebox.showerror(
                "输入验证", "起始年份不能晚于结束年份。"
            )
            return

        options = SearchOptions(
            name=name,
            institution=institution,
            email=email,
            from_year=from_year,
            to_year=to_year,
            skip_publisher_page_check=self.skip_publisher_var.get(),
            no_download=self.no_download_var.get(),
        )

        # --- prepare UI ---
        self._cancel_event = threading.Event()
        self._clear_output()
        self._set_running(True)
        self._append_output(
            f"> run_search(name={name!r}, institution={institution!r}, "
            f"from_year={from_year}, to_year={to_year})\n\n"
        )

        # --- launch ---
        self._worker_thread = threading.Thread(
            target=self._run_search,
            args=(options, self._cancel_event),
            daemon=True,
        )
        self._worker_thread.start()

    def _run_search(self, options: SearchOptions, cancel_event: threading.Event) -> None:
        """Execute run_search() in a worker thread, feeding output to queue."""
        try:
            result = run_search(
                options,
                progress_callback=lambda message: self._output_queue.put(("output", message + "\n")),
                cancel_event=cancel_event,
            )
            self._output_queue.put(("result", result))
        except Exception as exc:
            self._output_queue.put(("error", str(exc)))

    def _cancel_search(self) -> None:
        """Signal cancellation to the running search thread."""
        if self._cancel_event is not None:
            self._cancel_event.set()
            self._append_output("\n[\u5df2取消\u68c0\u7d22]\n")
            self.cancel_btn.configure(state=tk.DISABLED)
            self.status_var.set("正在取消，请等待当前网络请求结束……")

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
                elif kind == "result" and isinstance(payload, SearchResult):
                    self._set_running(False)
                    summary = payload.summary
                    total = (
                        summary["explicit"]
                        + summary["uncertain"]
                        + summary["excluded_not_corresponding"]
                    )
                    self._append_output_now("\n\u2500\u2500 检索完成 \u2500\u2500\n")
                    if total == 0:
                        self._append_output_now("未找到匹配的论文。\n")
                    else:
                        self._append_output_now(
                            f"明确通讯作者: {summary['explicit']}  |  "
                            f"无法确定: {summary['uncertain']}  |  "
                            f"已排除: {summary['excluded_not_corresponding']}\n"
                        )
                        if payload.output_dir:
                            self._results_path = payload.output_dir
                            self.open_folder_btn.configure(state=tk.NORMAL)
                    self.status_var.set("检索完成。")
                    self._cancel_event = None
                    self._worker_thread = None
                elif kind == "error":
                    self._append_output_now(f"\n[错误] {payload}\n")
                    self._append_output_now("\u2500\u2500 检索失败 \u2500\u2500\n")
                    self._set_running(False)
                    self.status_var.set("检索失败，请查看上方日志了解详情。")
                    self._cancel_event = None
                    self._worker_thread = None
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
        """Handle window close: signal cancellation and destroy."""
        self._cancel_search()
        self.root.destroy()


# ---- entry point ------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    _ = CorrespondingAuthorGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
