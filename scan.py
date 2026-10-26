import argparse
import ctypes
import glob
import importlib.util
import os
import shutil
import stat
import sys
import time

GB = 1024 ** 3
MB = 1024 ** 2

SAFE_DIR_NAMES = {
    "$recycle.bin",
    "recycle bin",
    "temp",
    "tmp",
    "cache",
    "caches",
    "inetcache",
    "crashdumps",
    "prefetch",
}

FORBIDDEN_PREFIXES = (
    "c:\\windows",
    "c:\\program files\\windowsapps",
    "c:\\programdata\\microsoft\\windows",
    "c:\\recovery",
    "c:\\system volume information",
    "c:\\$winreagent",
    "c:\\$windows.~bt",
    "c:\\$windows.~ws",
    "c:\\boot",
    "c:\\efi",
)

RECYCLE_BIN_PATH = "C:\\$Recycle.Bin"


def format_size(size):
    if size >= GB:
        return f"{size / GB:.2f} GB"
    return f"{size / MB:.2f} MB"


def format_delta(value):
    prefix = "+" if value > 0 else ("-" if value < 0 else "")
    return f"{prefix}{format_size(abs(value))}"


def format_signed_percent(value):
    return f"{value:+.2f}%"


def is_under(path, base):
    return path == base or path.startswith(base + "\\")


def path_depth(path, root):
    relative = os.path.relpath(path, root)
    if relative == ".":
        return 0
    return relative.count(os.sep) + 1


def classify_risk(path):
    normalized = os.path.normpath(path).lower()

    if normalized == "c:\\":
        return "禁止手动删"

    if any(is_under(normalized, base) for base in FORBIDDEN_PREFIXES):
        return "禁止手动删"

    components = {
        component.strip(".")
        for component in normalized.split("\\")
        if component
    }

    if components & SAFE_DIR_NAMES:
        return "安全"

    return "谨慎"


def is_reparse_point(st):
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def skip_reason(exc):
    return exc.strerror or str(exc) or type(exc).__name__


class SkipTracker:
    def __init__(self, limit=20):
        self.limit = limit
        self.count = 0
        self.items = []
        self.seen = set()

    def add(self, path, reason):
        self.count += 1
        if path in self.seen:
            return
        self.seen.add(path)
        if len(self.items) < self.limit:
            self.items.append((path, reason))

    def print_report(self):
        print()
        print(
            f"跳过清单（累计 {self.count} 项，按路径去重后前 "
            f"{len(self.items)} 个）："
        )
        if not self.items:
            print("  无")
            return
        for path, reason in self.items:
            print(f"  - {path}：{reason}")


def scan_directory(path, depth, max_depth, totals, skips):
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                    if is_reparse_point(st):
                        continue

                    if stat.S_ISDIR(st.st_mode):
                        child_total = 0
                        if depth < max_depth:
                            child_total = scan_directory(
                                entry.path,
                                depth + 1,
                                max_depth,
                                totals,
                                skips,
                            )
                        totals[entry.path] = child_total
                        total += child_total
                    elif stat.S_ISREG(st.st_mode):
                        total += st.st_size
                except OSError as exc:
                    skips.add(
                        entry.path,
                        f"读取条目失败：{skip_reason(exc)}",
                    )
    except OSError as exc:
        skips.add(path, f"无法列出目录：{skip_reason(exc)}")
    return total


def full_scan(path, wanted, full_totals, progress, skips):
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                    if is_reparse_point(st):
                        continue

                    if stat.S_ISDIR(st.st_mode):
                        progress["dirs"] += 1
                        total += full_scan(
                            entry.path,
                            wanted,
                            full_totals,
                            progress,
                            skips,
                        )
                    elif stat.S_ISREG(st.st_mode):
                        progress["files"] += 1
                        progress["bytes"] += st.st_size
                        total += st.st_size
                except OSError as exc:
                    progress["skipped"] += 1
                    skips.add(
                        entry.path,
                        f"读取条目失败：{skip_reason(exc)}",
                    )

                now = time.monotonic()
                if now - progress["last"] >= 5:
                    print(
                        f"    进度：{progress['files']:,} 个文件 / "
                        f"{progress['dirs']:,} 个目录 / "
                        f"{format_size(progress['bytes'])}",
                        flush=True,
                    )
                    progress["last"] = now
    except OSError as exc:
        progress["skipped"] += 1
        skips.add(path, f"无法列出目录：{skip_reason(exc)}")

    if path in wanted:
        full_totals[path] = total
    return total


def new_progress():
    return {
        "files": 0,
        "dirs": 0,
        "bytes": 0,
        "skipped": 0,
        "last": time.monotonic(),
    }


def scan_root_files(root, skips):
    total = 0
    skipped = 0
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                    if is_reparse_point(st):
                        continue
                    if stat.S_ISREG(st.st_mode):
                        total += st.st_size
                except OSError as exc:
                    skipped += 1
                    skips.add(
                        entry.path,
                        f"读取条目失败：{skip_reason(exc)}",
                    )
    except OSError as exc:
        skipped += 1
        skips.add(root, f"无法列出目录：{skip_reason(exc)}")
    return total, skipped


def is_within(path, base):
    normalized_path = os.path.normpath(path).lower()
    normalized_base = os.path.normpath(base).lower()
    return (
        normalized_path == normalized_base
        or normalized_path.startswith(normalized_base + "\\")
    )


def send2trash_available():
    return importlib.util.find_spec("send2trash") is not None


class SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    ]


def query_recycle_bin():
    info = SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    try:
        result = ctypes.windll.shell32.SHQueryRecycleBinW(
            None, ctypes.byref(info)
        )
    except (AttributeError, OSError):
        return None, None
    if result != 0:
        return None, None
    return info.i64Size, info.i64NumItems


def empty_recycle_bin():
    flags = 0x1 | 0x2 | 0x4
    try:
        result = ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, flags)
    except (AttributeError, OSError):
        return False
    return result in (0, 1)


def is_reparse_path(path):
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return is_reparse_point(st)


def discover_cache_dirs():
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []

    found = []
    browser_roots = (
        ("Chrome 缓存", os.path.join(local, "Google", "Chrome", "User Data")),
        ("Edge 缓存", os.path.join(local, "Microsoft", "Edge", "User Data")),
    )
    for category, user_data in browser_roots:
        for cache in glob.glob(os.path.join(user_data, "*", "Cache")):
            if os.path.isdir(cache):
                found.append((category, os.path.normpath(cache)))

    firefox_profiles = os.path.join(local, "Mozilla", "Firefox", "Profiles")
    for profile in glob.glob(os.path.join(firefox_profiles, "*")):
        for name in ("cache2", "Cache"):
            cache = os.path.join(profile, name)
            if os.path.isdir(cache):
                found.append(("Firefox 缓存", os.path.normpath(cache)))

    unique = []
    seen = set()
    for category, path in found:
        key = os.path.normpath(path).lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append((category, path))
    return unique


def collect_clean_files(root, category, skips):
    items = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                    if is_reparse_point(st):
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        items.extend(
                            collect_clean_files(entry.path, category, skips)
                        )
                    elif stat.S_ISREG(st.st_mode):
                        items.append(
                            {
                                "path": entry.path,
                                "size": st.st_size,
                                "category": category,
                            }
                        )
                except OSError as exc:
                    skips.add(
                        entry.path,
                        f"读取条目失败：{skip_reason(exc)}",
                    )
    except OSError as exc:
        skips.add(root, f"无法列出目录：{skip_reason(exc)}")
    return items


def build_clean_plan(skips):
    file_targets = []

    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if temp:
        temp = os.path.normpath(temp)
        if os.path.isdir(temp):
            if is_reparse_path(temp):
                print(f"拒绝：{temp} 是重解析点，不能安全处理。")
            else:
                file_targets.append(("用户临时目录", temp))
        else:
            print(f"跳过：用户临时目录不存在或不可访问：{temp}")
    else:
        print("跳过：未找到用户临时目录（%TEMP%）。")

    for category, path in discover_cache_dirs():
        if is_reparse_path(path):
            print(f"拒绝：{path} 是重解析点，不能安全处理。")
            continue
        file_targets.append((category, path))

    unique_targets = []
    seen_roots = set()
    for category, path in file_targets:
        key = os.path.normpath(path).lower()
        if key in seen_roots:
            continue
        seen_roots.add(key)
        unique_targets.append((category, path))

    allowed_roots = [path for _, path in unique_targets]
    allowed_roots.append(RECYCLE_BIN_PATH)

    files = []
    seen_files = set()
    for category, root in unique_targets:
        for item in collect_clean_files(root, category, skips):
            if not any(
                is_within(item["path"], allowed)
                for allowed in allowed_roots
            ):
                print(
                    f"拒绝：{item['path']} 不在清理白名单内，已跳过。"
                )
                continue
            key = os.path.normpath(item["path"]).lower()
            if key in seen_files:
                continue
            seen_files.add(key)
            files.append(item)

    recycle_size, recycle_count = query_recycle_bin()
    recycle = {
        "path": RECYCLE_BIN_PATH,
        "size": recycle_size,
        "count": recycle_count,
        "category": "回收站",
    }
    return files, recycle


def stdin_is_interactive():
    return sys.stdin is not None and sys.stdin.isatty()


def refuse_non_interactive_clean():
    print("拒绝执行清理：当前标准输入不是交互式终端（疑似脚本或管道调用）。")
    print("为避免绕过确认机制，本次未做任何修改。请在真实终端中运行。")


def execute_clean_plan(files, recycle, on_progress=None):
    from send2trash import send2trash

    success = 0
    failed = 0
    moved_bytes = 0
    freed_bytes = 0

    recycle_has_items = (
        recycle["count"] is None
        or recycle["count"] > 0
        or (recycle["size"] or 0) > 0
    )
    if recycle_has_items:
        print(f"清空回收站：{recycle['path']}")
        if on_progress is not None:
            on_progress(
                {
                    "stage": "recycle",
                    "index": 0,
                    "total": len(files),
                    "moved_bytes": moved_bytes,
                    "failed": failed,
                }
            )
        if empty_recycle_bin():
            success += (
                int(recycle["count"])
                if recycle["count"] is not None else 1
            )
            freed_bytes += int(recycle["size"] or 0)
            print("  成功：回收站已清空。")
        else:
            failed += 1
            print("  失败：系统接口未能清空回收站。")

    for index, item in enumerate(files, 1):
        try:
            send2trash(item["path"])
            success += 1
            moved_bytes += item["size"]
        except Exception as exc:
            failed += 1
            print(f"  失败：{item['path']}（{exc}）")
        if on_progress is not None:
            on_progress(
                {
                    "stage": "files",
                    "index": index,
                    "total": len(files),
                    "moved_bytes": moved_bytes,
                    "failed": failed,
                }
            )
        if index % 500 == 0:
            print(
                f"  进度：已处理 {index}/{len(files)} 个文件",
                flush=True,
            )

    print()
    print(
        f"完成：成功 {success} 个 / 失败 {failed} 个 / "
        f"释放 {format_size(freed_bytes + moved_bytes)}"
    )
    print(
        f"  其中：回收站实际释放 {format_size(freed_bytes)}；"
        f"移入回收站 {format_size(moved_bytes)}"
        "（需再次清空回收站才真正释放）"
    )
    return {
        "success": success,
        "failed": failed,
        "moved_bytes": moved_bytes,
        "freed_bytes": freed_bytes,
        "recycle_cleared": recycle_has_items,
    }


def run_clean(dry_run):
    if not dry_run and not stdin_is_interactive():
        refuse_non_interactive_clean()
        return

    clean_skips = SkipTracker()
    files, recycle = build_clean_plan(clean_skips)

    print()
    mode = "dry-run（只打印，不删除）" if dry_run else "真实执行"
    print(f"清理模式：{mode}")
    print("待删除清单（路径 + 大小 + 类别）：")

    total_bytes = 0
    for item in files:
        print(
            f"  {format_size(item['size']):>10}  "
            f"[{item['category']}]  {item['path']}"
        )
        total_bytes += item["size"]

    recycle_size_text = (
        format_size(recycle["size"])
        if recycle["size"] is not None else "未知"
    )
    recycle_count_text = (
        f"{recycle['count']} 项"
        if recycle["count"] is not None else "数量未知"
    )
    print(
        f"  {recycle_size_text:>10}  [回收站]  {recycle['path']}"
        f"（系统接口清空，{recycle_count_text}）"
    )
    if recycle["size"]:
        total_bytes += recycle["size"]

    print()
    print(
        f"计划：{len(files)} 个文件 + 回收站 {recycle_count_text}，"
        f"合计 {format_size(total_bytes)}"
    )

    if not send2trash_available():
        print()
        print("缺少 send2trash 库。")
        print("请先安装：pip install Send2Trash")
        if not dry_run:
            print("未做任何修改。")
            clean_skips.print_report()
            return

    if dry_run:
        print()
        print("dry-run：以上仅为计划，未删除任何文件。")
        clean_skips.print_report()
        return

    print()
    try:
        answer = input(
            "确认执行清理？输入 yes 继续，其他任意内容退出："
        ).strip()
    except EOFError:
        print("已取消，未做任何修改。")
        clean_skips.print_report()
        return
    if answer != "yes":
        print("已取消，未做任何修改。")
        clean_skips.print_report()
        return

    execute_clean_plan(files, recycle)
    clean_skips.print_report()


def print_comparison(candidates, full_totals, max_depth, root):
    print()
    print("两阶段对比（浅层列统计到 C:\\ 下第 "
          f"{max_depth} 层；完整列递归到所有子层）")
    print("注意：存在父子包含关系，父目录已包含子目录；缩进和 └─ 只表示层级，不可直接相加。")
    print(
        f"{'#':>2}  {'浅层(≤' + str(max_depth) + '层)':>12}  "
        f"{'完整(全层)':>12}  {'低估':>10}  {'风险':>10}  目录"
    )

    for index, (path, shallow) in enumerate(candidates, 1):
        full = full_totals.get(path)
        if full is None:
            full_text = "未完成"
            delta_text = "-"
        else:
            full_text = format_size(full)
            delta_text = format_delta(max(0, full - shallow))

        depth = path_depth(path, root)
        indent = "    " * max(0, depth - 1)
        branch = "└─ " if depth > 1 else ""
        risk = classify_risk(path)
        print(
            f"{index:>2}. {format_size(shallow):>10}  "
            f"{full_text:>10}  {delta_text:>10}  "
            f"[{risk}]  {indent}{branch}{path}"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="将白名单目标移入回收站（默认关闭，程序默认只读）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印清理计划，不执行任何删除",
    )
    args = parser.parse_args()

    if args.depth < 0:
        parser.error("--depth 必须是大于或等于 0 的整数")

    if (
        args.clean
        and not args.dry_run
        and not stdin_is_interactive()
    ):
        refuse_non_interactive_clean()
        return

    root = "C:\\"
    totals = {}
    skips = SkipTracker()
    totals[root] = scan_directory(root, 0, args.depth, totals, skips)

    usage = shutil.disk_usage(root)
    scanned = totals[root]
    difference = usage.used - scanned

    print(f"阶段一：浅层扫描（速度优先，最多 {args.depth} 层）")
    print(
        f"C 盘容量：总 {format_size(usage.total)} / "
        f"已用 {format_size(usage.used)} / 可用 {format_size(usage.free)}"
    )
    print(
        f"浅层合计（C:\\，≤{args.depth}层）：{format_size(scanned)} / "
        f"已用 - 浅层：{format_delta(difference)} / "
        f"无法访问而跳过：{skips.count} 项"
    )
    print("浅层差异说明：")
    print("  - 深度限制：浅层只累计到指定层数内的文件，更深层文件未计入。")
    print("  - 权限与异常：无权限或读取失败的条目被跳过。")
    print("  - 硬链接：WinSxS 等目录按路径重复计算同一文件，可能虚高；符号链接/重解析点被跳过。")
    print("  - 统计口径：扫描用文件逻辑大小，disk_usage 按卷实际占用统计，并包含 NTFS 元数据、系统保留等。")

    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    candidates = [
        (path, size)
        for path, size in ranked
        if os.path.normpath(path).lower() != "c:\\"
    ][:15]

    wanted = {path for path, _ in candidates}
    full_totals = {}

    print()
    print(
        f"阶段二：完整递归（不受深度限制，候选 {len(candidates)} 个；"
        "排除 C:\\ 根目录，完整递归根目录等同于整盘扫描）"
    )

    for index, (path, _) in enumerate(candidates, 1):
        if path in full_totals:
            print(
                f"[{index}/{len(candidates)}] {path}："
                "复用父目录的完整递归结果",
                flush=True,
            )
            continue

        print(
            f"[{index}/{len(candidates)}] 开始完整递归：{path}",
            flush=True,
        )
        progress = new_progress()
        total = full_scan(path, wanted, full_totals, progress, skips)
        full_totals[path] = total
        print(
            f"    完成：{progress['files']:,} 个文件 / "
            f"{progress['dirs']:,} 个目录 / {format_size(total)} / "
            f"跳过 {progress['skipped']} 项",
            flush=True,
        )

    normalized_root = os.path.normpath(root).lower()
    first_level_dirs = sorted(
        path
        for path in totals
        if path != root
        and os.path.dirname(os.path.normpath(path)).lower() == normalized_root
    )
    first_level_sizes = {}

    print()
    print(
        f"阶段三：补齐所有一级目录的完整递归大小"
        f"（共 {len(first_level_dirs)} 个，用于互不重叠合计）"
    )

    for path in first_level_dirs:
        if path in full_totals:
            first_level_sizes[path] = full_totals[path]
            continue

        print(f"  开始完整递归：{path}", flush=True)
        progress = new_progress()
        total = full_scan(path, wanted, full_totals, progress, skips)
        full_totals[path] = total
        first_level_sizes[path] = total
        print(
            f"    完成：{progress['files']:,} 个文件 / "
            f"{progress['dirs']:,} 个目录 / {format_size(total)} / "
            f"跳过 {progress['skipped']} 项",
            flush=True,
        )

    top_paths = [
        path
        for path, _ in sorted(
            first_level_sizes.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:15]
    ]
    candidates = [(path, totals.get(path, 0)) for path in top_paths]

    first_level_sum = sum(first_level_sizes.values())
    root_files, root_files_skipped = scan_root_files(root, skips)
    adjusted_sum = first_level_sum + root_files
    raw_error = first_level_sum - usage.used
    raw_error_pct = (
        raw_error / usage.used * 100 if usage.used else 0.0
    )
    adjusted_error = adjusted_sum - usage.used
    adjusted_error_pct = (
        adjusted_error / usage.used * 100 if usage.used else 0.0
    )

    print()
    print("一级目录完整递归合计与磁盘对照")
    print("  一级目录合计（互不包含，不含 C:\\ 根目录直接文件）："
          f"{format_size(first_level_sum)}")
    print(
        f"  C:\\ 根目录直接文件：{format_size(root_files)}"
        f"（跳过 {root_files_skipped} 项）"
    )
    print(f"  一级目录合计 + 根目录文件：{format_size(adjusted_sum)}")
    print(
        f"  磁盘：总 {format_size(usage.total)} / "
        f"已用 {format_size(usage.used)} / 可用 {format_size(usage.free)}"
    )
    print(
        f"  一级目录合计 vs 已用：{format_delta(raw_error)}"
        f"（{format_signed_percent(raw_error_pct)}）"
    )
    print(
        f"  参考：一级目录合计 + 根文件 vs 已用：{format_delta(adjusted_error)}"
        f"（{format_signed_percent(adjusted_error_pct)}）"
    )
    print("  误差说明：")
    print("    - 一级目录合计不含 C:\\ 根目录直接文件（如 pagefile.sys、hiberfil.sys），会偏低。")
    print("    - 硬链接（尤其 WinSxS）按路径重复计数，会偏高。")
    print("    - 无权限目录、符号链接/重解析点被跳过，会偏低。")
    print("    - disk_usage 的“已用”包含 NTFS 元数据、系统保留空间和簇分配开销，与文件逻辑大小口径不同。")

    print_comparison(candidates, full_totals, args.depth, root)

    if any("\\winsxs" in path.lower() for path in full_totals):
        print()
        print("注意：WinSxS 的表观大小因硬链接重复计数而虚高，禁止手动删除；")
        print("      应使用 DISM 组件清理或系统存储设置处理。")

    print()
    print("风险等级：安全=临时文件/缓存/回收站；谨慎=需确认；禁止手动删=系统核心或硬链接目录。")

    skips.print_report()

    if args.clean or args.dry_run:
        run_clean(args.dry_run)


if __name__ == "__main__":
    main()
