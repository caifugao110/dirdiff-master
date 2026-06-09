from __future__ import annotations

import csv
import hashlib
import html
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Iterable, Literal

import ttkbootstrap as ttk
from ttkbootstrap.constants import BOTH, END, LEFT, RIGHT, X, YES


def bundled_path(name: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    return Path(__file__).resolve().parent / name


def load_project_metadata() -> dict[str, str]:
    pyproject_path = bundled_path("pyproject.toml")
    metadata = {"version": "unknown", "author": "unknown", "homepage": ""}
    if not pyproject_path.exists():
        return metadata

    text = pyproject_path.read_text(encoding="utf-8")
    try:
        import tomllib

        project = tomllib.loads(text).get("project", {})
        urls = project.get("urls", {})
        authors = project.get("authors", [])
        metadata["version"] = project.get("version", metadata["version"])
        if authors:
            metadata["author"] = authors[0].get("name", metadata["author"])
        metadata["homepage"] = urls.get("Homepage", metadata["homepage"])
        return metadata
    except Exception:
        pass

    patterns = {
        "version": r'(?m)^version\s*=\s*"([^"]+)"',
        "author": r'authors\s*=\s*\[\{\s*name\s*=\s*"([^"]+)"',
        "homepage": r'(?m)^Homepage\s*=\s*"([^"]+)"',
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            metadata[key] = match.group(1)
    return metadata


PROJECT_METADATA = load_project_metadata()
__version__ = PROJECT_METADATA["version"]
__author__ = PROJECT_METADATA["author"]
__homepage__ = PROJECT_METADATA["homepage"]

CompareMode = Literal["full", "ignore_ext", "selected_ext"]

STATUS_LABELS = {
    "same": "相同",
    "changed": "内容不同",
    "only_base": "仅基准存在",
    "only_compare": "仅对比存在",
    "conflict": "键冲突",
}


@dataclass(frozen=True)
class FileRecord:
    root: Path
    path: Path
    relative_path: str
    extension: str
    size: int
    modified_time: float

    @property
    def display_size(self) -> str:
        return format_bytes(self.size)


@dataclass(frozen=True)
class DiffRow:
    status: str
    key: str
    base: FileRecord | None
    compare: FileRecord | None
    detail: str = ""

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def compare_path(self) -> str:
        return str(self.compare.path) if self.compare else ""

    @property
    def base_path(self) -> str:
        return str(self.base.path) if self.base else ""


def normalize_extension(extension: str) -> str:
    value = extension.strip().lower()
    if not value or value == "[无后缀]":
        return ""
    return value if value.startswith(".") else f".{value}"


def discover_extensions(*roots: str | Path) -> list[str]:
    extensions: set[str] = set()
    for root in roots:
        root_path = Path(root)
        if not root_path.exists():
            continue
        for file_path in iter_files(root_path):
            suffix = file_path.suffix.lower()
            extensions.add(suffix if suffix else "[无后缀]")
    return sorted(extensions, key=lambda item: (item == "[无后缀]", item))


def compare_folders(
    base_root: str | Path,
    compare_root: str | Path,
    mode: CompareMode = "full",
    selected_extensions: Iterable[str] | None = None,
    hash_changed_files: bool = True,
    mtime_tolerance: float = 1.0,
    recursive: bool = False,
) -> list[DiffRow]:
    base_path = Path(base_root)
    compare_path = Path(compare_root)
    if not base_path.is_dir():
        raise ValueError(f"基准文件夹不存在或不可访问：{base_path}")
    if not compare_path.is_dir():
        raise ValueError(f"对比文件夹不存在或不可访问：{compare_path}")

    extension_filter = None
    if mode == "selected_ext":
        extension_filter = {normalize_extension(item) for item in selected_extensions or []}
        if not extension_filter:
            raise ValueError("请选择至少一种文件格式。")

    base_records = collect_records(base_path, mode, extension_filter, recursive)
    compare_records = collect_records(compare_path, mode, extension_filter, recursive)

    rows: list[DiffRow] = []
    for key in sorted(set(base_records) | set(compare_records), key=str.casefold):
        left = base_records.get(key, [])
        right = compare_records.get(key, [])
        if len(left) > 1 or len(right) > 1:
            rows.append(
                DiffRow(
                    "conflict",
                    key,
                    left[0] if left else None,
                    right[0] if right else None,
                    "忽略后缀或筛选后出现多个文件映射到同一比较键，请人工确认。",
                )
            )
            continue
        if left and right:
            base_record = left[0]
            compare_record = right[0]
            if files_match(base_record, compare_record, hash_changed_files, mtime_tolerance):
                detail = "文件大小和内容一致" if hash_changed_files else "文件大小和修改时间一致"
                rows.append(DiffRow("same", key, base_record, compare_record, detail))
            else:
                detail = "文件大小或内容不同" if hash_changed_files else "文件大小或修改时间不同"
                rows.append(DiffRow("changed", key, base_record, compare_record, detail))
        elif left:
            rows.append(DiffRow("only_base", key, left[0], None, "对比文件夹缺少该文件"))
        elif right:
            rows.append(DiffRow("only_compare", key, None, right[0], "对比文件夹新增该文件"))
    return rows


def collect_records(root: Path, mode: CompareMode, extension_filter: set[str] | None = None, recursive: bool = False) -> dict[str, list[FileRecord]]:
    records: dict[str, list[FileRecord]] = {}
    for file_path in iter_files(root, recursive):
        extension = file_path.suffix.lower()
        if extension_filter is not None and extension not in extension_filter:
            continue
        stat = file_path.stat()
        relative_path = file_path.relative_to(root).as_posix()
        record = FileRecord(
            root=root,
            path=file_path,
            relative_path=relative_path,
            extension=extension,
            size=stat.st_size,
            modified_time=stat.st_mtime,
        )
        key = make_key(relative_path, mode)
        records.setdefault(key, []).append(record)
    return records


def iter_files(root: Path, recursive: bool = False):
    if recursive:
        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                yield Path(dirpath) / filename
    else:
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.is_file():
                    yield Path(entry.path)


def make_key(relative_path: str, mode: CompareMode) -> str:
    normalized = relative_path.replace("\\", "/")
    if mode == "ignore_ext":
        parent = Path(normalized).parent.as_posix()
        stem = Path(normalized).stem
        normalized = stem if parent == "." else f"{parent}/{stem}"
    return normalized.casefold()


def files_match(base: FileRecord, compare: FileRecord, hash_changed_files: bool, mtime_tolerance: float = 1.0) -> bool:
    if base.size != compare.size:
        return False
    if not hash_changed_files:
        return abs(base.modified_time - compare.modified_time) <= mtime_tolerance
    return sha256(base.path) == sha256(compare.path)


def sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_reports(rows: list[DiffRow], output_dir: str | Path) -> tuple[Path, Path]:
    target = Path(output_dir)
    clear_report_dir(target)
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = target / f"dirdiff_report_{stamp}.csv"
    html_path = target / f"dirdiff_report_{stamp}.html"
    write_csv(rows, csv_path)
    write_html(rows, html_path)
    return csv_path, html_path


def clear_report_dir(target: Path) -> None:
    if not target.exists():
        return
    for child in target.iterdir():
        if child.is_file() and child.name.startswith("dirdiff_report_") and child.suffix.lower() in {".csv", ".html"}:
            child.unlink()


def write_csv(rows: list[DiffRow], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(["状态", "比较键", "基准路径", "对比路径", "基准大小", "对比大小", "说明"])
        for row in rows:
            writer.writerow(
                [
                    row.status_label,
                    row.key,
                    row.base_path,
                    row.compare_path,
                    row.base.display_size if row.base else "",
                    row.compare.display_size if row.compare else "",
                    row.detail,
                ]
            )


def write_html(rows: list[DiffRow], path: Path) -> None:
    summary = summarize(rows)
    body_rows = "\n".join(
        f"<tr data-status=\"{html.escape(row.status_label)}\">"
        f"<td>{html.escape(row.status_label)}</td>"
        f"<td>{html.escape(row.key)}</td>"
        f"<td>{html.escape(row.base_path)}</td>"
        f"<td>{html.escape(row.compare_path)}</td>"
        f"<td>{html.escape(row.base.display_size if row.base else '')}</td>"
        f"<td>{html.escape(row.compare.display_size if row.compare else '')}</td>"
        f"<td>{html.escape(row.detail)}</td>"
        "</tr>"
        for row in rows
    )
    chips = "".join(
        f"<span><b>{html.escape(STATUS_LABELS.get(key, key))}</b>{value}</span>"
        for key, value in summary.items()
    )
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>DirDiff Master 对比清单</title>
  <style>
    body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin: 32px; color: #18212f; background: #f6f8fb; }}
    h1 {{ margin: 0 0 16px; }}
    .summary {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }}
    .summary span {{ border: 1px solid #d7dee8; border-radius: 8px; padding: 8px 12px; background: #fff; }}
    .summary b {{ margin-right: 8px; }}
    .toolbar {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin: 0 0 16px; }}
    input, select {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 8px 10px; min-height: 36px; background: #fff; }}
    input {{ min-width: 320px; }}
    button {{ border: 1px solid #2563eb; border-radius: 8px; padding: 8px 12px; color: #fff; background: #2563eb; cursor: pointer; }}
    .count {{ color: #52616f; margin-left: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: #fff; }}
    th, td {{ border-bottom: 1px solid #e3e8ef; padding: 9px 10px; text-align: left; vertical-align: top; }}
    th {{ background: #eef3f8; position: sticky; top: 0; }}
    tr:nth-child(even) {{ background: #fbfcfe; }}
    tr.hidden {{ display: none; }}
  </style>
</head>
<body>
  <h1>DirDiff Master 对比清单</h1>
  <div class="summary">{chips}</div>
  <div class="toolbar">
    <select id="statusFilter">
      <option value="全部">全部状态</option>
      <option value="相同">相同</option>
      <option value="内容不同">内容不同</option>
      <option value="仅基准存在">仅基准存在</option>
      <option value="仅对比存在">仅对比存在</option>
      <option value="键冲突">键冲突</option>
    </select>
    <input id="keywordFilter" placeholder="输入关键字筛选路径、比较键或说明">
    <button type="button" id="resetFilter">清空筛选</button>
    <span class="count" id="visibleCount"></span>
  </div>
  <table>
    <thead><tr><th>状态</th><th>比较键</th><th>基准路径</th><th>对比路径</th><th>基准大小</th><th>对比大小</th><th>说明</th></tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
  <script>
    const statusFilter = document.getElementById("statusFilter");
    const keywordFilter = document.getElementById("keywordFilter");
    const resetFilter = document.getElementById("resetFilter");
    const visibleCount = document.getElementById("visibleCount");
    const rows = Array.from(document.querySelectorAll("tbody tr"));

    function applyFilter() {{
      const status = statusFilter.value;
      const keyword = keywordFilter.value.trim().toLowerCase();
      let visible = 0;
      rows.forEach(row => {{
        const statusMatched = status === "全部" || row.dataset.status === status;
        const keywordMatched = !keyword || row.innerText.toLowerCase().includes(keyword);
        const show = statusMatched && keywordMatched;
        row.classList.toggle("hidden", !show);
        if (show) visible += 1;
      }});
      visibleCount.textContent = `显示 ${{visible}} / ${{rows.length}} 项`;
    }}

    statusFilter.addEventListener("change", applyFilter);
    keywordFilter.addEventListener("input", applyFilter);
    resetFilter.addEventListener("click", () => {{
      statusFilter.value = "全部";
      keywordFilter.value = "";
      applyFilter();
    }});
    applyFilter();
  </script>
</body>
</html>
""",
        encoding="utf-8",
    )


def summarize(rows: Iterable[DiffRow]) -> dict[str, int]:
    result = {key: 0 for key in STATUS_LABELS}
    for row in rows:
        result[row.status] = result.get(row.status, 0) + 1
    return result


def move_compare_files(rows: Iterable[DiffRow], destination_root: str | Path) -> list[Path]:
    destination = Path(destination_root)
    destination.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for row in rows:
        if not row.compare or row.status == "same":
            continue
        relative = Path(row.compare.relative_path)
        target_path = unique_path(destination / relative)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(row.compare.path), str(target_path))
        moved.append(target_path)
    return moved


def delete_compare_files(rows: Iterable[DiffRow]) -> int:
    count = 0
    for row in rows:
        if not row.compare or row.status == "same":
            continue
        row.compare.path.unlink()
        count += 1
    return count


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(1, 10_000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法生成唯一文件名：{path}")


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def asset_path(name: str) -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets" / name
    return project_root() / "assets" / name


ASSET_ICON = asset_path("app.ico")
REPORT_DIR = project_root() / "reports"


class DirDiffApp(ttk.Window):
    def __init__(self) -> None:
        super().__init__(themename="flatly")
        self.title(f"DirDiff Master V{__version__}")
        self.geometry("1240x760")
        self.minsize(980, 620)
        if ASSET_ICON.exists():
            self.iconbitmap(str(ASSET_ICON))

        self.base_var = tk.StringVar()
        self.compare_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="full")
        self.theme_var = tk.StringVar(value="flatly")
        self.status_filter_var = tk.StringVar(value="全部")
        self.report_var = tk.StringVar(value="尚未生成报告")
        self.summary_var = tk.StringVar(value="选择两个文件夹后开始对比")
        self.progress_var = tk.StringVar(value="")
        self.precise_compare_var = tk.BooleanVar(value=False)
        self.include_subfolders_var = tk.BooleanVar(value=False)
        self.latest_html_report: Path | None = None
        self.extension_vars: dict[str, tk.BooleanVar] = {}
        self.rows: list[DiffRow] = []
        self.filtered_rows: list[DiffRow] = []
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._build_ui()

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, padding=(18, 14, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text="DirDiff Master", font=("Microsoft YaHei UI", 22, "bold"))
        title.grid(row=0, column=0, sticky="w")
        meta = ttk.Label(header, text=f"V{__version__} 文件夹对比工具", bootstyle="secondary")
        meta.grid(row=1, column=0, sticky="w", pady=(2, 0))

        theme_bar = ttk.Frame(header)
        theme_bar.grid(row=0, column=1, rowspan=2, sticky="e")
        ttk.Label(theme_bar, text="主题").pack(side=LEFT, padx=(0, 8))
        theme_box = ttk.Combobox(
            theme_bar,
            textvariable=self.theme_var,
            values=sorted(self.style.theme_names()),
            width=18,
            state="readonly",
        )
        theme_box.pack(side=LEFT)
        theme_box.bind("<<ComboboxSelected>>", self._change_theme)
        ttk.Button(theme_bar, text="关于", bootstyle="secondary-outline", command=self.show_about).pack(side=LEFT, padx=(8, 0))

        main = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 12))

        controls = ttk.Frame(main, padding=12)
        main.add(controls, weight=1)
        results = ttk.Frame(main, padding=(10, 12, 12, 12))
        main.add(results, weight=4)

        self._build_controls(controls)
        self._build_results(results)

        footer = ttk.Frame(self, padding=(18, 0, 18, 14))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.summary_var, bootstyle="secondary").grid(row=0, column=0, sticky="w")
        ttk.Label(footer, textvariable=self.progress_var, bootstyle="info").grid(row=0, column=1, sticky="e", padx=(12, 0))

    def _build_controls(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)

        path_box = ttk.Labelframe(parent, text="文件夹", padding=12)
        path_box.grid(row=0, column=0, sticky="ew")
        path_box.columnconfigure(1, weight=1)

        self._path_row(path_box, 0, "基准文件夹", "通常选择文件较少或较旧的文件夹，作为对照基准。", self.base_var, "info")
        self._path_row(path_box, 3, "对比文件夹", "选择文件较多或较新的文件夹。差异文件移动/删除仅作用于这里。", self.compare_var, "warning")

        option_box = ttk.Labelframe(parent, text="比较选项", padding=12)
        option_box.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Radiobutton(
            option_box,
            text="比较全部文件名，包含文件格式后缀",
            value="full",
            variable=self.mode_var,
            command=self._sync_extension_state,
        ).pack(anchor="w", pady=3)
        ttk.Radiobutton(
            option_box,
            text="只比较文件名，忽略文件格式后缀",
            value="ignore_ext",
            variable=self.mode_var,
            command=self._sync_extension_state,
        ).pack(anchor="w", pady=3)
        ttk.Radiobutton(
            option_box,
            text="只比较指定文件格式",
            value="selected_ext",
            variable=self.mode_var,
            command=self._sync_extension_state,
        ).pack(anchor="w", pady=3)
        ttk.Checkbutton(
            option_box,
            text="精确读取文件内容进行校验",
            variable=self.precise_compare_var,
            bootstyle="round-toggle",
        ).pack(anchor="w", pady=(8, 2))
        ttk.Checkbutton(
            option_box,
            text="包含子文件夹",
            variable=self.include_subfolders_var,
            bootstyle="round-toggle",
        ).pack(anchor="w", pady=(4, 0))

        ext_header = ttk.Frame(option_box)
        ext_header.pack(fill=X, pady=(10, 4))
        ttk.Label(ext_header, text="动态文件格式").pack(side=LEFT)
        ttk.Button(ext_header, text="刷新", bootstyle="secondary-outline", command=self.refresh_extensions).pack(side=RIGHT)

        ext_outer = ttk.Frame(option_box)
        ext_outer.pack(fill=X)
        self.ext_canvas = tk.Canvas(ext_outer, height=126, highlightthickness=0)
        self.ext_scroll = ttk.Scrollbar(ext_outer, orient=tk.VERTICAL, command=self.ext_canvas.yview)
        self.ext_inner = ttk.Frame(self.ext_canvas)
        self.ext_inner.bind("<Configure>", lambda _: self.ext_canvas.configure(scrollregion=self.ext_canvas.bbox("all")))
        self.ext_canvas.create_window((0, 0), window=self.ext_inner, anchor="nw")
        self.ext_canvas.configure(yscrollcommand=self.ext_scroll.set)
        self.ext_canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        self.ext_scroll.pack(side=RIGHT, fill=tk.Y)

        
        self._sync_extension_state()

    def _path_row(self, parent: ttk.Frame, row: int, title: str, hint: str, var: tk.StringVar, style: str) -> None:
        ttk.Label(parent, text=title, font=("Microsoft YaHei UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky="w")
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(4, 0), padx=(0, 8))
        ttk.Button(parent, text="选择", bootstyle="secondary-outline", command=lambda: self.choose_folder(var)).grid(row=row + 1, column=2, sticky="e")
        ttk.Label(
            parent,
            text=hint,
            bootstyle=f"{style}-inverse",
            padding=(8, 10),
            wraplength=260,
            justify=LEFT,
        ).grid(
            row=row + 2,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(6, 12),
        )

    def _build_results(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(1, weight=1)
        parent.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="开始对比", bootstyle="success", command=self.start_compare).pack(side=LEFT, padx=(0, 10))
        ttk.Label(toolbar, text="结果筛选").pack(side=LEFT, padx=(0, 8))
        filter_box = ttk.Combobox(
            toolbar,
            textvariable=self.status_filter_var,
            values=["全部", "相同", "内容不同", "仅基准存在", "仅对比存在", "键冲突"],
            width=14,
            state="readonly",
        )
        filter_box.pack(side=LEFT)
        filter_box.bind("<<ComboboxSelected>>", lambda _: self.render_rows())
        ttk.Button(toolbar, text="删除全部差异", bootstyle="danger-outline", command=self.delete_all_differences).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(toolbar, text="移动全部差异", bootstyle="warning-outline", command=self.move_all_differences).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(toolbar, text="删除选中差异", bootstyle="danger-outline", command=self.delete_selected).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(toolbar, text="移动选中差异", bootstyle="warning-outline", command=self.move_selected).pack(side=RIGHT, padx=(8, 0))
        ttk.Button(toolbar, text="打开生成报告", bootstyle="secondary-outline", command=self.open_latest_report).pack(side=RIGHT)

        columns = ("status", "key", "base", "compare", "base_size", "compare_size", "detail")
        self.tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="extended")
        headings = {
            "status": "状态",
            "key": "比较键",
            "base": "基准路径",
            "compare": "对比路径",
            "base_size": "基准大小",
            "compare_size": "对比大小",
            "detail": "说明",
        }
        widths = {"status": 96, "key": 220, "base": 260, "compare": 260, "base_size": 90, "compare_size": 90, "detail": 190}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=70, stretch=column in {"key", "base", "compare", "detail"})
        self.tree.grid(row=1, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.tree.yview)
        yscroll.grid(row=1, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=self.tree.xview)
        xscroll.grid(row=2, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

    def choose_folder(self, var: tk.StringVar) -> None:
        folder = filedialog.askdirectory(title="选择文件夹")
        if folder:
            var.set(folder)
            self.refresh_extensions()

    def refresh_extensions(self) -> None:
        for child in self.ext_inner.winfo_children():
            child.destroy()
        self.extension_vars.clear()
        roots = [self.base_var.get().strip(), self.compare_var.get().strip()]
        extensions = discover_extensions(*[root for root in roots if root])
        if not extensions:
            ttk.Label(self.ext_inner, text="选择文件夹后显示可用格式", bootstyle="secondary").pack(anchor="w")
        for index, extension in enumerate(extensions):
            var = tk.BooleanVar(value=True)
            self.extension_vars[extension] = var
            checkbox = ttk.Checkbutton(self.ext_inner, text=extension, variable=var)
            checkbox.grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 14), pady=2)
        self._sync_extension_state()

    def _sync_extension_state(self) -> None:
        state = "normal" if self.mode_var.get() == "selected_ext" else "disabled"
        for child in self.ext_inner.winfo_children():
            try:
                child.configure(state=state)
            except tk.TclError:
                pass

    def start_compare(self) -> None:
        base = self.base_var.get().strip()
        compare = self.compare_var.get().strip()
        if not base or not compare:
            messagebox.showwarning("缺少路径", "请先选择基准文件夹和对比文件夹。")
            return
        selected_extensions = [ext for ext, var in self.extension_vars.items() if var.get()]
        self.progress.start(12)
        self.progress_var.set("正在扫描和比较文件...")
        self.summary_var.set("比较进行中")
        thread = threading.Thread(
            target=self._compare_worker,
            args=(base, compare, self.mode_var.get(), selected_extensions, self.precise_compare_var.get(), self.include_subfolders_var.get()),
            daemon=True,
        )
        thread.start()
        self.after(120, self._poll_worker)

    def _compare_worker(
        self,
        base: str,
        compare: str,
        mode: str,
        selected_extensions: list[str],
        precise_compare: bool,
        recursive: bool,
    ) -> None:
        try:
            rows = compare_folders(
                base,
                compare,
                mode=mode,
                selected_extensions=selected_extensions,
                hash_changed_files=precise_compare,
                recursive=recursive,
            )
            reports = write_reports(rows, REPORT_DIR)
            self.worker_queue.put(("done", (rows, reports)))
        except Exception as exc:
            self.worker_queue.put(("error", exc))

    def _poll_worker(self) -> None:
        try:
            kind, payload = self.worker_queue.get_nowait()
        except queue.Empty:
            self.after(120, self._poll_worker)
            return
        self.progress.stop()
        self.progress_var.set("")
        if kind == "error":
            messagebox.showerror("对比失败", str(payload))
            self.summary_var.set("对比失败")
            return
        rows, reports = payload
        self.rows = rows
        self.render_rows()
        csv_path, html_path = reports
        self.latest_html_report = html_path
        self.report_var.set(f"已生成：{csv_path.name} / {html_path.name}")
        self.summary_var.set(self._summary_text(rows))

    def render_rows(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        label = self.status_filter_var.get()
        self.filtered_rows = [row for row in self.rows if label == "全部" or row.status_label == label]
        for index, row in enumerate(self.filtered_rows):
            self.tree.insert(
                "",
                END,
                iid=str(index),
                values=(
                    row.status_label,
                    row.key,
                    row.base_path,
                    row.compare_path,
                    row.base.display_size if row.base else "",
                    row.compare.display_size if row.compare else "",
                    row.detail,
                ),
            )

    def selected_diff_rows(self) -> list[DiffRow]:
        rows = []
        for item in self.tree.selection():
            row = self.filtered_rows[int(item)]
            if row.compare and row.status in {"changed", "only_compare"}:
                rows.append(row)
        return rows

    def all_compare_diff_rows(self) -> list[DiffRow]:
        return [row for row in self.rows if row.compare and row.status in {"changed", "only_compare"}]

    def move_selected(self) -> None:
        self.move_rows(self.selected_diff_rows(), "选中")

    def move_all_differences(self) -> None:
        self.move_rows(self.all_compare_diff_rows(), "全部")

    def move_rows(self, rows: list[DiffRow], scope: str) -> None:
        if not rows:
            messagebox.showinfo("没有可移动文件", f"没有可移动的{scope}差异文件。")
            return
        destination = filedialog.askdirectory(title="选择移动目标目录")
        if not destination:
            return
        if not messagebox.askyesno("确认移动", f"将移动对比文件夹中的 {len(rows)} 个{scope}差异文件，是否继续？"):
            return
        try:
            moved = move_compare_files(rows, destination)
        except Exception as exc:
            messagebox.showerror("移动失败", str(exc))
            return
        messagebox.showinfo("移动完成", f"已移动 {len(moved)} 个文件。")
        self.start_compare()

    def delete_selected(self) -> None:
        self.delete_rows(self.selected_diff_rows(), "选中")

    def delete_all_differences(self) -> None:
        self.delete_rows(self.all_compare_diff_rows(), "全部")

    def delete_rows(self, rows: list[DiffRow], scope: str) -> None:
        if not rows:
            messagebox.showinfo("没有可删除文件", f"没有可删除的{scope}差异文件。")
            return
        if not messagebox.askyesno("确认删除", f"将删除对比文件夹中的 {len(rows)} 个{scope}差异文件，是否继续？"):
            return
        try:
            count = delete_compare_files(rows)
        except Exception as exc:
            messagebox.showerror("删除失败", str(exc))
            return
        messagebox.showinfo("删除完成", f"已删除 {count} 个文件。")
        self.start_compare()

    def clear_all(self) -> None:
        self.base_var.set("")
        self.compare_var.set("")
        self.status_filter_var.set("全部")
        self.report_var.set("尚未生成报告")
        self.summary_var.set("选择两个文件夹后开始对比")
        self.progress_var.set("")
        self.latest_html_report = None
        self.rows = []
        self.filtered_rows = []
        self.render_rows()
        self.refresh_extensions()

    def open_latest_report(self) -> None:
        if not self.latest_html_report or not self.latest_html_report.exists():
            messagebox.showinfo("没有报告", "请先完成一次对比，生成 HTML 报告。")
            return
        if sys.platform.startswith("win"):
            os.startfile(self.latest_html_report)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(self.latest_html_report)], check=False)
        else:
            subprocess.run(["xdg-open", str(self.latest_html_report)], check=False)

    def open_reports(self) -> None:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(REPORT_DIR)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(REPORT_DIR)], check=False)
        else:
            subprocess.run(["xdg-open", str(REPORT_DIR)], check=False)

    def _change_theme(self, _: object = None) -> None:
        self.style.theme_use(self.theme_var.get())

    def open_homepage(self) -> None:
        webbrowser.open(__homepage__)

    def show_about(self) -> None:
        dialog = ttk.Toplevel(self)
        dialog.title("关于 DirDiff Master")
        dialog.geometry("460x260")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        self._center_dialog(dialog, 460, 260)

        container = ttk.Frame(dialog, padding=22)
        container.pack(fill=BOTH, expand=YES)
        ttk.Label(container, text="DirDiff Master", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(container, text=f"版本 V{__version__}", bootstyle="secondary").pack(anchor="w", pady=(4, 0))
        ttk.Label(container, text=f"作者：{__author__}", bootstyle="secondary").pack(anchor="w", pady=(8, 0))
        ttk.Label(container, text="开源协议：MIT", bootstyle="secondary").pack(anchor="w", pady=(4, 0))
        link = ttk.Label(container, text=__homepage__, bootstyle="primary", cursor="hand2")
        link.pack(anchor="w", pady=(14, 0))
        link.bind("<Button-1>", lambda _: self.open_homepage())
        ttk.Button(container, text="关闭", bootstyle="primary", command=dialog.destroy).pack(anchor="e", pady=(24, 0))

    def _center_dialog(self, dialog: tk.Toplevel, width: int, height: int) -> None:
        self.update_idletasks()
        x = self.winfo_rootx() + max((self.winfo_width() - width) // 2, 0)
        y = self.winfo_rooty() + max((self.winfo_height() - height) // 2, 0)
        dialog.geometry(f"{width}x{height}+{x}+{y}")

    def _summary_text(self, rows: list[DiffRow]) -> str:
        summary = summarize(rows)
        return (
            f"共 {len(rows)} 项 | 相同 {summary.get('same', 0)} | 内容不同 {summary.get('changed', 0)} | "
            f"仅基准 {summary.get('only_base', 0)} | 仅对比 {summary.get('only_compare', 0)} | 冲突 {summary.get('conflict', 0)}"
        )


def main() -> None:
    app = DirDiffApp()
    app.mainloop()


if __name__ == "__main__":
    main()
