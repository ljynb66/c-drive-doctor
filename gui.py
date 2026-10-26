import contextlib
import io
import os
import queue
import shutil
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import scan

ROOT = "C:\\"
SCAN_DEPTH = 3
RISK_TAGS = {
    "安全": "safe",
    "谨慎": "caution",
    "禁止手动删": "forbidden",
    "无法读取": "unreadable",
}

STATE_ORDER = {
    "partial": 0,
    "sized": 0,
    "empty": 1,
    "unreadable": 2,
}

SMALL_UNREADABLE_LIMIT = 1024 ** 2


def format_size_precise(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.2f} KB"
    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.2f} MB"
    return f"{size / 1024 ** 3:.2f} GB"


def directory_state(path, size, skip_paths):
    normalized = os.path.normpath(path).lower()
    prefix = normalized + os.sep

    if normalized in skip_paths:
        return "unreadable"

    has_failure = any(
        skipped.startswith(prefix) for skipped in skip_paths
    )
    if not has_failure:
        return "sized" if size > 0 else "empty"

    if size < SMALL_UNREADABLE_LIMIT:
        return "unreadable"

    return "partial"


class DiskCleanerApp:
    def __init__(self, root):
        self.root = root
        self.message_queue = queue.Queue()
        self.busy = False
        self.operation = None
        self.worker_progress = None
        self.phase_text = ""
        self.cancel_event = threading.Event()
        self.closing = False

        self._build_ui()
        self.refresh_capacity()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        self.root.after(100, self._drain_queue)

    def _build_ui(self):
        self.root.title("C 盘扫描清理工具")
        self.root.geometry("1040x680")
        self.root.minsize(900, 560)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=26)
        style.configure(
            "Footer.TLabel",
            foreground="#666666",
            font=("Segoe UI", 9),
        )

        top = ttk.LabelFrame(
            self.root,
            text="C 盘容量概览",
            padding=(12, 8),
        )
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        top.columnconfigure(0, weight=1)

        stats = ttk.Frame(top)
        stats.grid(row=0, column=0, sticky="ew")

        self.total_var = tk.StringVar(value="--")
        self.used_var = tk.StringVar(value="--")
        self.free_var = tk.StringVar(value="--")
        self.capacity_pct_var = tk.StringVar(value="已用 --")

        ttk.Label(stats, text="总容量：").grid(row=0, column=0, sticky="w")
        ttk.Label(
            stats,
            textvariable=self.total_var,
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(stats, text="已用：").grid(
            row=0, column=2, sticky="w", padx=(24, 0)
        )
        ttk.Label(
            stats,
            textvariable=self.used_var,
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=3, sticky="w")
        ttk.Label(stats, text="可用：").grid(
            row=0, column=4, sticky="w", padx=(24, 0)
        )
        ttk.Label(
            stats,
            textvariable=self.free_var,
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=5, sticky="w")
        ttk.Label(
            stats,
            textvariable=self.capacity_pct_var,
            foreground="#555555",
        ).grid(row=0, column=6, sticky="e", padx=(24, 0))
        stats.columnconfigure(6, weight=1)

        self.capacity_bar = ttk.Progressbar(
            top,
            mode="determinate",
            maximum=100,
        )
        self.capacity_bar.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(8, 0),
        )

        controls = ttk.Frame(self.root)
        controls.grid(row=1, column=0, sticky="ew", padx=12, pady=6)
        controls.columnconfigure(2, weight=1)

        self.scan_btn = ttk.Button(
            controls,
            text="开始扫描",
            command=self.start_scan,
        )
        self.scan_btn.grid(row=0, column=0, padx=(0, 8))

        self.clean_btn = ttk.Button(
            controls,
            text="清理",
            command=self.start_clean,
        )
        self.clean_btn.grid(row=0, column=1, padx=(0, 12))

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(
            controls,
            textvariable=self.status_var,
            anchor="w",
        ).grid(row=0, column=2, sticky="ew")

        self.task_bar = ttk.Progressbar(
            controls,
            mode="determinate",
            maximum=100,
            length=220,
        )
        self.task_bar.grid(row=0, column=3, sticky="e")

        table_frame = ttk.Frame(self.root)
        table_frame.grid(
            row=2,
            column=0,
            sticky="nsew",
            padx=12,
            pady=6,
        )
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("dir", "size", "ratio", "risk")
        self.tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("dir", text="目录")
        self.tree.heading("size", text="大小")
        self.tree.heading("ratio", text="占比")
        self.tree.heading("risk", text="风险等级")
        self.tree.column("dir", width=600, anchor="w", stretch=True)
        self.tree.column("size", width=120, anchor="e", stretch=False)
        self.tree.column("ratio", width=90, anchor="e", stretch=False)
        self.tree.column("risk", width=120, anchor="center", stretch=False)

        scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.tree.tag_configure("safe", foreground="#2e7d32")
        self.tree.tag_configure("caution", foreground="#b8860b")
        self.tree.tag_configure("forbidden", foreground="#c62828")
        self.tree.tag_configure("unreadable", foreground="#6b7280")

        footer = ttk.Label(
            self.root,
            text=(
                "本工具数据为扫描估算，"
                "与系统实际占用可能存在 1~2% 偏差"
            ),
            style="Footer.TLabel",
            anchor="center",
        )
        footer.grid(
            row=3,
            column=0,
            sticky="ew",
            padx=12,
            pady=(0, 8),
        )

    def refresh_capacity(self):
        try:
            usage = shutil.disk_usage(ROOT)
        except OSError as exc:
            self.total_var.set("读取失败")
            self.used_var.set("--")
            self.free_var.set("--")
            self.capacity_pct_var.set(str(exc))
            self.capacity_bar["value"] = 0
            return

        self.total_var.set(scan.format_size(usage.total))
        self.used_var.set(scan.format_size(usage.used))
        self.free_var.set(scan.format_size(usage.free))
        percent = usage.used / usage.total * 100 if usage.total else 0
        self.capacity_pct_var.set(f"已用 {percent:.1f}%")
        self.capacity_bar["value"] = percent

    def _set_busy(self, busy, operation=None):
        self.busy = busy
        self.operation = operation if busy else None
        state = "disabled" if busy else "normal"
        self.scan_btn.configure(state=state)
        self.clean_btn.configure(state=state)

    def _task_indeterminate(self, text):
        self.status_var.set(text)
        self.task_bar.stop()
        self.task_bar.configure(
            mode="indeterminate",
            maximum=100,
            value=0,
        )
        self.task_bar.start(12)

    def _task_determinate(self, text, value, maximum):
        self.task_bar.stop()
        self.task_bar.configure(
            mode="determinate",
            maximum=max(maximum, 1),
            value=value,
        )
        self.status_var.set(text)

    def _task_stop(self):
        self.task_bar.stop()
        self.task_bar.configure(
            mode="determinate",
            maximum=100,
            value=0,
        )

    def start_scan(self):
        if self.busy:
            return

        self.cancel_event.clear()
        self.worker_progress = None
        self.phase_text = "正在准备扫描…"
        self.tree.delete(*self.tree.get_children())
        self._set_busy(True, "scan")
        self._task_indeterminate("正在准备扫描…")

        threading.Thread(
            target=self._scan_worker,
            name="scan-worker",
            daemon=True,
        ).start()

    def _scan_worker(self):
        try:
            with contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                totals = {}
                skips = scan.SkipTracker()

                self.message_queue.put(
                    (
                        "status",
                        "阶段一：浅层扫描 C 盘（最多 3 层）…",
                    )
                )
                totals[ROOT] = scan.scan_directory(
                    ROOT,
                    0,
                    SCAN_DEPTH,
                    totals,
                    skips,
                )

                if self.cancel_event.is_set():
                    return

                usage = shutil.disk_usage(ROOT)
                normalized_root = os.path.normpath(ROOT).lower()
                first_level_dirs = sorted(
                    path
                    for path in totals
                    if path != ROOT
                    and os.path.dirname(
                        os.path.normpath(path)
                    ).lower()
                    == normalized_root
                )

                first_level_sizes = {}
                wanted = set(first_level_dirs)
                full_totals = {}
                total_dirs = len(first_level_dirs)

                for index, path in enumerate(first_level_dirs, 1):
                    if self.cancel_event.is_set():
                        return

                    self.message_queue.put(
                        (
                            "task",
                            f"完整扫描 {index}/{total_dirs}：{path}",
                            index - 1,
                            total_dirs,
                        )
                    )
                    progress = scan.new_progress()
                    self.worker_progress = progress
                    total = scan.full_scan(
                        path,
                        wanted,
                        full_totals,
                        progress,
                        skips,
                    )
                    first_level_sizes[path] = full_totals.get(
                        path, total
                    )

                self.worker_progress = None
                root_files, _ = scan.scan_root_files(ROOT, skips)
                skip_paths = {
                    os.path.normpath(path).lower()
                    for path in skips.seen
                }

                rows = []
                for path, size in first_level_sizes.items():
                    state = directory_state(path, size, skip_paths)
                    if state == "unreadable":
                        risk = "无法读取"
                    else:
                        risk = scan.classify_risk(path)
                    rows.append((path, size, risk, state))

                rows.append(
                    (
                        "C:\\ 根目录直接文件",
                        root_files,
                        "谨慎",
                        "sized" if root_files > 0 else "empty",
                    )
                )
                rows.sort(
                    key=lambda item: (
                        STATE_ORDER[item[3]],
                        -item[1],
                    )
                )

            self.message_queue.put(
                ("scan_done", rows, usage, skips.count)
            )
        except Exception as exc:
            self.message_queue.put(
                ("error", f"扫描失败：{exc}", "scan")
            )

    def start_clean(self):
        if self.busy:
            return

        if not scan.send2trash_available():
            messagebox.showerror(
                "缺少依赖",
                "请先安装 Send2Trash：\npip install Send2Trash",
                parent=self.root,
            )
            return

        self.cancel_event.clear()
        self.worker_progress = None
        self._set_busy(True, "plan")
        self._task_indeterminate("正在统计可清理的文件…")

        threading.Thread(
            target=self._plan_worker,
            name="clean-plan-worker",
            daemon=True,
        ).start()

    def _plan_worker(self):
        try:
            with contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                skips = scan.SkipTracker()
                files, recycle = scan.build_clean_plan(skips)

            summary = {}
            total_bytes = 0
            for item in files:
                category = item["category"]
                entry = summary.setdefault(
                    category,
                    {"size": 0, "count": 0},
                )
                entry["size"] += item["size"]
                entry["count"] += 1
                total_bytes += item["size"]

            summary_rows = sorted(
                (
                    (category, data["size"], data["count"])
                    for category, data in summary.items()
                ),
                key=lambda item: item[1],
                reverse=True,
            )
            self.message_queue.put(
                (
                    "plan_ready",
                    files,
                    recycle,
                    summary_rows,
                    total_bytes,
                    skips.count,
                )
            )
        except Exception as exc:
            self.message_queue.put(
                ("error", f"统计清理项失败：{exc}", "plan")
            )

    def _confirm_clean_plan(
        self,
        files,
        recycle,
        summary_rows,
        total_bytes,
        skipped,
    ):
        self._task_stop()
        self.status_var.set("统计完成，等待确认…")

        recycle_size = recycle.get("size")
        recycle_count = recycle.get("count")
        recycle_has_items = (
            recycle_count is None
            or recycle_count > 0
            or (recycle_size or 0) > 0
        )

        if not files and not recycle_has_items:
            self._finish_operation("没有需要清理的项目。")
            return

        lines = ["将清理以下类别：", ""]
        for category, size, count in summary_rows:
            lines.append(
                f"  {category}：{scan.format_size(size)}"
                f"（{count} 个文件）"
            )

        if recycle_has_items:
            if recycle_size is None:
                recycle_size_text = "大小未知"
            else:
                recycle_size_text = scan.format_size(recycle_size)
            if recycle_count is None:
                recycle_count_text = "数量未知"
            else:
                recycle_count_text = f"{recycle_count} 项"
            lines.append(
                f"  回收站：{recycle_size_text}"
                f"（{recycle_count_text}）"
            )

        total_text = scan.format_size(total_bytes)
        if recycle_has_items and recycle_size is None:
            total_text += "（不含回收站）"
        elif recycle_size:
            total_text = scan.format_size(
                total_bytes + recycle_size
            )

        lines.extend(
            [
                "",
                f"合计约 {total_text}",
                "",
                "确定要执行清理吗？",
            ]
        )

        if skipped:
            lines.append(f"统计时跳过 {skipped} 项。")

        if not messagebox.askyesno(
            "确认清理",
            "\n".join(lines),
            parent=self.root,
        ):
            self._finish_operation("已取消清理，未做任何修改。")
            return

        self.operation = "clean"
        self.status_var.set("正在清理…")
        self._task_indeterminate("正在清理（回收站优先）…")

        threading.Thread(
            target=self._clean_worker,
            args=(files, recycle),
            name="clean-worker",
            daemon=True,
        ).start()

    def _clean_worker(self, files, recycle):
        try:
            last_update = [0.0]

            def on_progress(info):
                if info["stage"] == "recycle":
                    self.message_queue.put(
                        ("clean_stage", "正在清空回收站…")
                    )
                    return

                total = info["total"]
                index = info["index"]
                now = time.monotonic()
                if index == total or now - last_update[0] >= 0.2:
                    last_update[0] = now
                    self.message_queue.put(
                        (
                            "clean_progress",
                            f"正在清理 {index}/{total} 个文件",
                            index,
                            max(total, 1),
                        )
                    )

            with contextlib.redirect_stdout(
                io.StringIO()
            ), contextlib.redirect_stderr(io.StringIO()):
                stats = scan.execute_clean_plan(
                    files,
                    recycle,
                    on_progress,
                )

            self.message_queue.put(("clean_done", stats))
        except Exception as exc:
            self.message_queue.put(
                ("error", f"清理失败：{exc}", "clean")
            )

    def _finish_scan(self, rows, usage, skipped):
        self.worker_progress = None
        self.phase_text = ""
        self.tree.delete(*self.tree.get_children())

        used = usage.used or 1
        for path, size, risk, state in rows:
            if state == "empty":
                size_text = "空"
                ratio_text = "0.00%"
            elif state == "unreadable":
                size_text = "无权限"
                ratio_text = "—"
            else:
                size_text = format_size_precise(size)
                if state == "partial":
                    size_text = "≥" + size_text
                ratio = size / used * 100 if used else 0
                if ratio and ratio < 0.01:
                    ratio_text = "<0.01%"
                else:
                    ratio_text = f"{ratio:.2f}%"
                if state == "partial" and ratio >= 0.01:
                    ratio_text = "≥" + ratio_text

            tag = RISK_TAGS.get(risk, "caution")
            self.tree.insert(
                "",
                "end",
                values=(
                    path,
                    size_text,
                    ratio_text,
                    risk,
                ),
                tags=(tag,),
            )

        empty_count = sum(1 for row in rows if row[3] == "empty")
        partial_count = sum(
            1 for row in rows if row[3] == "partial"
        )
        unreadable_count = sum(
            1 for row in rows if row[3] == "unreadable"
        )

        self._task_stop()
        self._set_busy(False)
        self.refresh_capacity()
        self.status_var.set(
            f"扫描完成：{len(rows)} 个目录，空 {empty_count} 个，"
            f"部分无权限 {partial_count} 个，"
            f"无法读取 {unreadable_count} 个，跳过 {skipped} 项。"
        )

    def _finish_clean(self, stats):
        self.worker_progress = None
        self.phase_text = ""
        self._task_stop()
        self._set_busy(False)
        self.refresh_capacity()

        success = stats.get("success", 0)
        failed = stats.get("failed", 0)
        moved = stats.get("moved_bytes", 0)
        freed = stats.get("freed_bytes", 0)
        self.status_var.set(
            f"清理完成：成功 {success} 个 / 失败 {failed} 个 / "
            f"释放或移入回收站 "
            f"{scan.format_size(moved + freed)}。建议重新扫描。"
        )

    def _finish_operation(self, message):
        self.worker_progress = None
        self.phase_text = ""
        self._task_stop()
        self._set_busy(False)
        self.refresh_capacity()
        self.status_var.set(message)

    def _finish_error(self, text, operation):
        self.worker_progress = None
        self.phase_text = ""
        self._task_stop()
        self._set_busy(False)
        self.refresh_capacity()
        self.status_var.set(text)
        messagebox.showerror(
            "操作失败",
            text,
            parent=self.root,
        )

    def _handle_message(self, message):
        kind = message[0]

        if kind == "status":
            self.phase_text = message[1]
            self.status_var.set(message[1])
        elif kind == "task":
            self.phase_text = message[1]
            self._task_determinate(
                message[1],
                message[2],
                message[3],
            )
        elif kind == "scan_done":
            self._finish_scan(
                message[1],
                message[2],
                message[3],
            )
        elif kind == "plan_ready":
            self._confirm_clean_plan(
                message[1],
                message[2],
                message[3],
                message[4],
                message[5],
            )
        elif kind == "clean_stage":
            self._task_indeterminate(message[1])
        elif kind == "clean_progress":
            self._task_determinate(
                message[1],
                message[2],
                message[3],
            )
        elif kind == "clean_done":
            self._finish_clean(message[1])
        elif kind == "error":
            operation = message[2] if len(message) > 2 else None
            self._finish_error(message[1], operation)

    def _refresh_scan_live_status(self):
        progress = self.worker_progress
        if progress is None:
            return

        self.status_var.set(
            f"{self.phase_text} | "
            f"文件 {progress['files']:,} / "
            f"目录 {progress['dirs']:,} / "
            f"已扫描 {scan.format_size(progress['bytes'])} / "
            f"跳过 {progress['skipped']:,}"
        )

    def _drain_queue(self):
        if self.closing:
            return

        try:
            while True:
                message = self.message_queue.get_nowait()
                self._handle_message(message)
        except queue.Empty:
            pass

        if self.busy and self.operation == "scan":
            self._refresh_scan_live_status()

        try:
            if self.root.winfo_exists():
                self.root.after(100, self._drain_queue)
        except tk.TclError:
            pass

    def on_closing(self):
        if self.busy:
            if not messagebox.askyesno(
                "退出确认",
                "扫描或清理还在进行，确定退出吗？\n"
                "退出后当前操作会中止。",
                parent=self.root,
            ):
                return
            self.cancel_event.set()

        self.closing = True
        self.root.destroy()


def main():
    root = tk.Tk()
    DiskCleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
