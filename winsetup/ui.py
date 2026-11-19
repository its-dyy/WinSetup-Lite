import os
import tkinter as tk
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
from time import monotonic
from tkinter import filedialog, font, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from .advisor import Advice, advise
from .catalog import PACKAGES_BY_ID, Package
from .ollama import DEFAULT_MODEL
from .plans import load_plan, save_plan
from .winget import PackageResult, State, WinGet


AI_HELP = "根据用途推荐软件，也可搜索 WinGet 中的软件。"
CHECK_CACHE_SECONDS = 90


class WinSetupApp:
    def __init__(self, root: tk.Tk, catalog_path: Path | None = None) -> None:
        self.root = root
        self.winget = WinGet()
        self.ai_model = os.environ.get("WINSETUP_OLLAMA_MODEL", "").strip() or DEFAULT_MODEL
        self.busy = False
        self.events = SimpleQueue()
        self.controls = []
        self.catalog_path = catalog_path or (
            Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "WinSetup-Lite" / "catalog.json"
        )
        self.packages = PACKAGES_BY_ID.copy()
        catalog_error = None
        try:
            self.packages = {package.id: package for package in load_plan(self.catalog_path)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            catalog_error = error

        self.selected: set[str] = set()
        self.states: dict[str, str] = {}
        self.checked: dict[str, tuple[float, PackageResult]] = {}
        self.ai_picks: set[str] = set()
        self.ai_reasons: dict[str, str] = {}
        self.ai_packages: dict[str, Package] = {}
        self.retry_ids: tuple[str, ...] = ()
        self.status = tk.StringVar(root, "请勾选软件、导入清单或使用 AI 推荐。")
        self.ai_note = tk.StringVar(root, AI_HELP)
        self._build()
        if catalog_error is not None:
            self._write_log(f"无法加载保存的软件目录，当前显示内置目录。{catalog_error}")

    def _build(self) -> None:
        self.root.title("WinSetup Lite")
        scale = max(1.0, float(self.root.tk.call("tk", "scaling")) / (96 / 72))
        max_width, max_height = self.root.maxsize()
        width = min(round(960 * scale), max_width)
        height = min(round(730 * scale), max_height)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(870, 710)
        self.root.protocol("WM_DELETE_WINDOW", self._close)

        available_fonts = set(font.families(self.root))
        preferred = ("Microsoft YaHei UI", "Microsoft YaHei", "SimSun", "Noto Sans CJK SC")
        family = next((name for name in preferred if name in available_fonts), None)
        if family:
            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkFixedFont"):
                font.nametofont(name, root=self.root).configure(family=family, size=10)
        base_font = font.nametofont("TkDefaultFont", root=self.root).actual("family")

        style = ttk.Style(self.root)
        style.theme_use("clam")
        for name in ("TFrame", "TLabel", "TCheckbutton", "TLabelframe", "TLabelframe.Label"):
            style.configure(name, background="#f5f7fb", foreground="#152738")
        style.configure("TLabelframe", bordercolor="#b9c7d6", relief="solid")
        style.configure("TButton", padding=(10, 4))
        style.configure("Primary.TButton", background="#1c547d", foreground="white")
        style.map(
            "Primary.TButton",
            background=[("disabled", "#64748b"), ("active", "#164669"), ("!disabled", "#1c547d")],
            foreground=[("disabled", "#f8fafc"), ("!disabled", "white")],
        )
        style.configure("Title.TLabel", font=(base_font, 24, "bold"))
        style.configure("Heading.TLabel", font=(base_font, 12, "bold"))
        style.configure("Subtle.TLabel", foreground="#475569")
        row_height = font.nametofont("TkDefaultFont", root=self.root).metrics("linespace") + 6
        style.configure("Treeview", rowheight=row_height)
        style.configure("Status.TLabel", foreground="#245c86")

        page = ttk.Frame(self.root, padding=(24, 16))
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=1)

        ttk.Label(page, text="WinSetup Lite", style="Title.TLabel").grid(row=0, sticky="w")
        ttk.Label(
            page,
            text="Windows 11 装机助手",
            style="Subtle.TLabel",
        ).grid(row=1, sticky="w", pady=(2, 12))

        workspace = ttk.Frame(page)
        workspace.grid(row=2, sticky="nsew")
        workspace.columnconfigure((0, 1), weight=1, uniform="workspace")
        workspace.rowconfigure(0, weight=1)
        panels = (
            self._package_panel(workspace),
            self._ai_panel(workspace),
        )
        for column, panel in enumerate(panels):
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 14) if column == 0 else 0)

        self.log_window = tk.Toplevel(self.root)
        self.log_window.withdraw()
        self.log_window.title("操作记录")
        self.log_window.transient(self.root)
        self.log_window.geometry("700x240")
        self.log_window.minsize(360, 150)
        self.log_window.protocol("WM_DELETE_WINDOW", self.log_window.withdraw)
        self.log_window.bind("<Escape>", lambda _event: self.log_window.withdraw())
        self.log = ScrolledText(self.log_window, wrap="word", state="disabled", padx=8, pady=6)
        self.log.pack(fill="both", expand=True, padx=12, pady=12)

        footer = ttk.Frame(page)
        footer.grid(row=3, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        status_label = ttk.Label(footer, textvariable=self.status, style="Status.TLabel", wraplength=650)
        status_label.grid(row=0, column=0, sticky="ew")
        status_label.bind("<Configure>", self._wrap_to_width)
        self.progress = ttk.Progressbar(footer, length=140)
        self.progress.grid(row=0, column=1, padx=(10, 0))
        self.progress.grid_remove()
        self.retry_button = ttk.Button(footer, text="重试失败项", command=self._retry_install)
        self.retry_button.grid(row=0, column=2, padx=(10, 0))
        self.retry_button.grid_remove()
        ttk.Button(footer, text="操作记录", command=self._show_log).grid(row=0, column=3, padx=(10, 0))
        if self.winget.executable is None:
            self._write_log("当前未找到 WinGet。手动编辑和保存清单仍可使用。")

    def _package_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="软件清单", padding=(12, 10))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        rows = ttk.Frame(panel)
        rows.grid(row=0, column=0, sticky="nsew")
        rows.columnconfigure(0, weight=1)
        rows.rowconfigure(0, weight=1)
        self.package_rows = ttk.Treeview(
            rows,
            columns=("selection", "software", "state"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        self.package_rows.heading("selection", text="勾选")
        self.package_rows.heading("software", text="软件")
        self.package_rows.heading("state", text="本机状态")
        self.package_rows.column("selection", width=60, stretch=False, anchor="center")
        self.package_rows.column("software", width=190)
        self.package_rows.column("state", width=85, stretch=False, anchor="e")
        self.package_rows.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(rows, orient="vertical", command=self.package_rows.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.package_rows.configure(yscrollcommand=scroll.set)
        self.package_rows.bind("<ButtonRelease-1>", self._toggle_on_click)
        self.package_rows.bind("<space>", self._toggle_focused)
        for package_id in self.packages:
            self._refresh_row(package_id)

        actions = ttk.Frame(panel)
        actions.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for index, (label, action) in enumerate(
            (
                ("搜索并添加软件", self._search_and_add),
                ("从清单移除", self._remove_package),
                ("导入清单", self._import_plan),
                ("保存清单", self._export_plan),
                ("检查状态", self._check_selection),
                ("安装所选缺失项", self._install_selection),
            )
        ):
            style_name = "Primary.TButton" if index == 5 else "TButton"
            self._button(actions, label, action, style_name).grid(
                row=index // 2, column=index % 2, sticky="ew", padx=(0, 7), pady=(0, 6)
            )
        self._button(actions, "扫描本机软件", self._scan_installed).grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=(0, 7)
        )
        actions.columnconfigure((0, 1), weight=1)
        return panel

    def _ai_panel(self, parent: ttk.Frame) -> ttk.LabelFrame:
        panel = ttk.LabelFrame(parent, text="AI 软件推荐", padding=(12, 10))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(4, weight=1)
        ttk.Label(panel, text="这台电脑主要用来做什么？", style="Heading.TLabel").grid(
            row=0, sticky="w", pady=(0, 6)
        )

        self.requirements = tk.Text(
            panel, height=2, width=30, wrap="word", padx=8, pady=6, font="TkTextFont"
        )
        self.requirements.grid(row=1, sticky="ew")
        self.controls.append(self.requirements)

        actions = ttk.Frame(panel)
        actions.grid(row=2, sticky="ew", pady=(8, 6))
        self._button(actions, "生成推荐", self._generate_plan).pack(side="left")
        self.ai_apply = ttk.Button(
            actions, text="加入清单", command=self._add_ai_picks, state="disabled"
        )
        self.ai_apply.pack(side="right")
        note = ttk.Label(panel, textvariable=self.ai_note, style="Subtle.TLabel", wraplength=310)
        note.grid(row=3, sticky="ew")
        note.bind("<Configure>", self._wrap_to_width)

        results = ttk.Frame(panel)
        results.grid(row=4, sticky="nsew", pady=(6, 4))
        self.ai_rows = ttk.Treeview(
            results, columns=("pick", "name", "priority"), show="headings", height=7, selectmode="browse"
        )
        for name, label, width in (("pick", "勾选", 48), ("name", "软件", 148), ("priority", "建议", 64)):
            self.ai_rows.heading(name, text=label)
            self.ai_rows.column(
                name, width=width, stretch=name == "name", anchor="w" if name == "name" else "center"
            )
        self.ai_rows.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(results, orient="vertical", command=self.ai_rows.yview)
        scroll.pack(side="right", fill="y")
        self.ai_rows.configure(yscrollcommand=scroll.set)
        self.ai_rows.bind("<ButtonRelease-1>", self._toggle_ai_on_click)
        self.ai_rows.bind("<space>", self._toggle_focused_ai)
        self.ai_rows.bind("<<TreeviewSelect>>", self._show_ai_reason)

        self.ai_reason = ScrolledText(panel, height=2, width=30, wrap="word", state="disabled", padx=6, pady=4)
        self.ai_reason.grid(row=5, sticky="ew")
        self._show_ai_reason()
        return panel

    def _button(self, parent, text, command, style="TButton") -> ttk.Button:
        button = ttk.Button(parent, text=text, command=command, style=style)
        self.controls.append(button)
        return button

    @staticmethod
    def _write_text(widget: tk.Text, text: str, *, replace: bool = False) -> None:
        widget.configure(state="normal")
        if replace:
            widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.see("1.0" if replace else "end")
        widget.configure(state="disabled")

    def _write_log(self, text: str) -> None:
        self._write_text(self.log, text + "\n")

    @staticmethod
    def _wrap_to_width(event: tk.Event) -> None:
        event.widget.configure(wraplength=max(1, event.width - 2))

    def _show_log(self) -> None:
        self.log_window.deiconify()
        self.log_window.lift()
        self.log.see("end-1c")

    def _refresh_row(self, package_id: str) -> None:
        mark = "✓" if package_id in self.selected else "□"
        values = (mark, self.packages[package_id].name, self.states.get(package_id, "未检查"))
        if self.package_rows.exists(package_id):
            self.package_rows.item(package_id, values=values)
        else:
            self.package_rows.insert("", "end", iid=package_id, values=values)

    def _toggle(self, package_id: str) -> None:
        if self.busy or package_id not in self.packages:
            return
        if package_id in self.selected:
            self.selected.remove(package_id)
        else:
            self.selected.add(package_id)
        self._refresh_row(package_id)

    def _toggle_on_click(self, event: tk.Event) -> None:
        if self.package_rows.identify_region(event.x, event.y) == "cell":
            if self.package_rows.identify_column(event.x) == "#1":
                self._toggle(self.package_rows.identify_row(event.y))

    def _toggle_focused(self, _event: tk.Event) -> str:
        self._toggle(self.package_rows.focus())
        return "break"

    def _selection(self) -> tuple[str, ...]:
        return tuple(package_id for package_id in self.packages if package_id in self.selected)

    def _apply_selection(self, package_ids: tuple[str, ...]) -> None:
        self.selected = set(package_ids).intersection(self.packages)
        for package_id in self.packages:
            self._refresh_row(package_id)

    def _update_catalog(self, packages: dict[str, Package]) -> bool:
        if packages == self.packages:
            return True

        try:
            self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
            save_plan(self.catalog_path, tuple(packages.values()))
        except OSError as error:
            messagebox.showerror("保存目录失败", str(error), parent=self.root)
            return False

        removed = self.packages.keys() - packages.keys()
        for package_id in removed:
            self.package_rows.delete(package_id)
            self.states.pop(package_id, None)
            self.checked.pop(package_id, None)
        self.packages = packages
        self._apply_selection(self._selection())
        if removed:
            self._clear_ai_preview()
        self.retry_ids = tuple(package_id for package_id in self.retry_ids if package_id in packages)
        self._refresh_action_buttons()
        return True

    def _merge_packages(self, packages: tuple[Package, ...]) -> tuple[str, ...] | None:
        combined = {package.id.casefold(): package for package in self.packages.values()}
        merged_ids = tuple(combined.setdefault(package.id.casefold(), package).id for package in packages)
        catalog = {package.id: package for package in combined.values()}
        return merged_ids if self._update_catalog(catalog) else None

    def _search_and_add(self) -> None:
        if self.winget.executable is None:
            self.status.set("搜索需要 WinGet。请先在 PowerShell 中运行 winget --version。")
            return
        query = simpledialog.askstring(
            "搜索并添加软件", "输入软件名称或 WinGet ID，例如 WeChat。", parent=self.root
        )
        if query and query.strip():
            self._run_job(
                lambda: self.winget.search(query),
                self._show_search_results,
                "正在搜索 WinGet 软件目录。",
            )

    def _show_search_results(self, packages: tuple[Package, ...]) -> None:
        if not packages:
            self.status.set("没有找到软件。可以换个名称或尝试英文名。")
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("WinGet 搜索结果")
        dialog.transient(self.root)
        dialog.geometry("740x360")
        content = ttk.Frame(dialog, padding=12)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="核对软件名称和 ID，然后选择要添加的一项。").pack(
            anchor="w", pady=(0, 8)
        )
        area = ttk.Frame(content)
        area.pack(fill="both", expand=True)
        columns = ("name", "package_id")
        results = ttk.Treeview(area, columns=columns, show="headings", selectmode="browse")
        results.heading("name", text="软件名称")
        results.heading("package_id", text="WinGet ID")
        results.column("name", width=320)
        results.column("package_id", width=275)
        results.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(area, orient="vertical", command=results.yview)
        scroll.pack(side="right", fill="y")
        results.configure(yscrollcommand=scroll.set)
        for package in packages:
            results.insert("", "end", iid=package.id, values=(package.name, package.id))

        button = ttk.Button(content, text="添加所选", state="disabled")
        button.pack(anchor="e", pady=(8, 0))

        def enable_add(_event: tk.Event) -> None:
            button.configure(state="normal" if results.selection() else "disabled")

        def add() -> None:
            if not results.selection():
                return
            selected_id = results.selection()[0]
            package = next(package for package in packages if package.id == selected_id)
            package_ids = self._merge_packages((package,))
            if package_ids is None:
                return
            package_id = package_ids[0]
            self.selected.add(package_id)
            self._refresh_row(package_id)
            self.package_rows.selection_set(package_id)
            self.package_rows.see(package_id)
            self._write_log(f"已加入清单 · {package.name}（{package.id}）")
            self.status.set("可以继续调整清单，或检查所选软件的安装状态。")
            dialog.destroy()

        button.configure(command=add)
        results.bind("<<TreeviewSelect>>", enable_add)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        results.focus_set()

    def _remove_package(self) -> None:
        focused = self.package_rows.selection()
        if not focused:
            self.status.set("请先点击要移除的软件名称。")
            return
        packages = self.packages.copy()
        removed = packages.pop(focused[0])
        if self._update_catalog(packages):
            self.status.set(f"已从清单移除 {removed.name}。")

    def _require_selection(self) -> tuple[str, ...]:
        package_ids = self._selection()
        if not package_ids:
            self.status.set("请先勾选至少一项软件。")
        return package_ids

    def _import_plan(self) -> None:
        name = filedialog.askopenfilename(
            parent=self.root, title="导入装机清单", filetypes=[("JSON 清单", "*.json")]
        )
        if not name:
            return
        try:
            packages = load_plan(Path(name))
        except (OSError, ValueError) as error:
            messagebox.showerror("导入失败", str(error), parent=self.root)
            return
        package_ids = self._merge_packages(packages)
        if package_ids is None:
            return
        self._apply_selection(package_ids)
        self._write_log(f"已导入 {Path(name).name}，共 {len(package_ids)} 项。")
        self.status.set("清单已导入，可以修改后保存或安装。")
        if package_ids and self.winget.executable:
            self._inspect(package_ids)

    def _export_plan(self) -> None:
        package_ids = self._require_selection()
        if package_ids:
            self._save_packages(
                tuple(self.packages[package_id] for package_id in package_ids),
                parent=self.root,
                initialfile="winsetup-plan.json",
            )

    def _save_packages(self, packages: tuple[Package, ...], *, parent, initialfile: str) -> bool:
        name = filedialog.asksaveasfilename(
            parent=parent,
            title="保存装机清单",
            initialfile=initialfile,
            defaultextension=".json",
            filetypes=[("JSON 清单", "*.json")],
        )
        if not name:
            return False
        try:
            save_plan(Path(name), packages)
        except OSError as error:
            messagebox.showerror("保存失败", str(error), parent=parent)
            return False
        self.status.set(f"已导出 {len(packages)} 项软件。")
        self._write_log(f"已保存 {Path(name).name}，共 {len(packages)} 项。")
        return True

    def _scan_installed(self) -> None:
        if self.winget.executable is None:
            self.status.set("扫描需要 WinGet。请先在 PowerShell 中运行 winget --version。")
            return
        self._run_job(
            self.winget.installed_packages,
            self._show_installed_packages,
            "正在扫描本机安装的软件。",
        )

    def _show_installed_packages(self, packages: tuple[Package, ...]) -> None:
        if not packages:
            self.status.set("未找到可由 WinGet 软件源匹配的已安装软件。")
            return

        self.status.set(f"扫描到 {len(packages)} 项可导出软件。")
        dialog = tk.Toplevel(self.root)
        dialog.title("扫描本机软件")
        dialog.transient(self.root)
        dialog.geometry("760x470")
        dialog.minsize(560, 350)
        content = ttk.Frame(dialog, padding=12)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="勾选要备份的软件。列表来自 WinGet 软件源可匹配的项目。").pack(
            anchor="w", pady=(0, 8)
        )

        area = ttk.Frame(content)
        area.pack(fill="both", expand=True)
        columns = ("selected", "name", "package_id")
        rows = ttk.Treeview(area, columns=columns, show="headings", selectmode="browse")
        rows.heading("selected", text="勾选")
        rows.heading("name", text="软件")
        rows.heading("package_id", text="WinGet ID")
        rows.column("selected", width=65, stretch=False, anchor="center")
        rows.column("name", width=270)
        rows.column("package_id", width=300)
        rows.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(area, orient="vertical", command=rows.yview)
        scroll.pack(side="right", fill="y")
        rows.configure(yscrollcommand=scroll.set)
        for package in packages:
            rows.insert("", "end", iid=package.id, values=("□", package.name, package.id))

        controls = ttk.Frame(content)
        controls.pack(fill="x", pady=(10, 0))
        picked: set[str] = set()
        count = tk.StringVar(dialog, f"已选 0 / {len(packages)}")
        export = ttk.Button(controls, text="导出所选", style="Primary.TButton", state="disabled")
        export.pack(side="right")
        ttk.Label(controls, textvariable=count).pack(side="right", padx=10)

        def refresh() -> None:
            count.set(f"已选 {len(picked)} / {len(packages)}")
            export.configure(state="normal" if picked else "disabled")

        def toggle(package_id: str) -> None:
            if not package_id:
                return
            if package_id in picked:
                picked.remove(package_id)
            else:
                picked.add(package_id)
            rows.set(package_id, "selected", "✓" if package_id in picked else "□")
            refresh()

        def click(event: tk.Event) -> None:
            if rows.identify_region(event.x, event.y) == "cell" and rows.identify_column(event.x) == "#1":
                toggle(rows.identify_row(event.y))

        def toggle_focus(_event: tk.Event) -> str:
            toggle(rows.focus())
            return "break"

        def select_all(select: bool) -> None:
            picked.clear()
            if select:
                picked.update(package.id for package in packages)
            for package in packages:
                rows.set(package.id, "selected", "✓" if select else "□")
            refresh()

        def save() -> None:
            selection = tuple(package for package in packages if package.id in picked)
            if selection and self._save_packages(
                selection, parent=dialog, initialfile="winsetup-installed.json"
            ):
                dialog.destroy()

        ttk.Button(controls, text="全选", command=lambda: select_all(True)).pack(side="left")
        ttk.Button(controls, text="清空", command=lambda: select_all(False)).pack(side="left", padx=8)
        export.configure(command=save)
        rows.bind("<ButtonRelease-1>", click)
        rows.bind("<space>", toggle_focus)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        rows.focus_set()

    def _check_selection(self) -> None:
        package_ids = self._require_selection()
        if package_ids:
            self._inspect(package_ids)

    def _inspect(self, package_ids: tuple[str, ...]) -> None:
        self._run_package_job(package_ids, "正在检查", self.winget.check, self._finish_check)

    def _finish_check(self, results: tuple[tuple[str, PackageResult], ...]) -> None:
        summary = " · ".join(
            f"{state.value} {sum(result.state is state for _, result in results)} 项"
            for state in (State.INSTALLED, State.MISSING, State.UNKNOWN)
        )
        self.status.set(summary)

    def _install_selection(self) -> None:
        package_ids = self._require_selection()
        if package_ids:
            self._prepare_install(package_ids)

    def _retry_install(self) -> None:
        if self.retry_ids:
            self._prepare_install(self.retry_ids)

    def _prepare_install(self, package_ids: tuple[str, ...]) -> None:
        if not self.winget.executable:
            self.status.set("未找到 WinGet。请先在 PowerShell 中运行 winget --version。")
            return
        known = self._fresh_checks(package_ids)
        outdated = tuple(package_id for package_id in package_ids if package_id not in known)

        def preview(results):
            known.update(results)
            self._show_install_preview(tuple((package_id, known[package_id]) for package_id in package_ids))

        if outdated:
            self._run_package_job(outdated, "正在检查安装清单", self.winget.check, preview)
        else:
            preview(())

    def _fresh_checks(self, package_ids) -> dict[str, PackageResult]:
        cutoff = monotonic() - CHECK_CACHE_SECONDS
        selected = set(package_ids)
        return {
            package_id: result for package_id, (checked_at, result) in self.checked.items()
            if package_id in selected and checked_at >= cutoff
        }

    def _show_install_preview(self, results: tuple[tuple[str, PackageResult], ...]) -> None:
        states = (State.MISSING, State.INSTALLED, State.UNKNOWN)
        groups = {
            state: tuple(package_id for package_id, result in results if result.state is state)
            for state in states
        }
        labels = {State.MISSING: "待安装", State.INSTALLED: "已安装，跳过", State.UNKNOWN: "检测失败"}
        pending = groups[State.MISSING]
        self._finish_check(results)

        dialog = tk.Toplevel(self.root)
        dialog.title("确认安装清单")
        dialog.transient(self.root)
        dialog.geometry("690x410")
        dialog.minsize(560, 300)
        content = ttk.Frame(dialog, padding=14)
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.rowconfigure(2, weight=1)
        ttk.Label(content, text=self.status.get(), style="Heading.TLabel").grid(row=0, sticky="w", pady=(0, 8))
        if pending:
            message = "核对待安装项后，点击安装。"
        elif groups[State.UNKNOWN]:
            message = "本次没有待安装项。检测失败的原因可在操作记录查看。"
        else:
            message = "所选软件均已安装。"
        ttk.Label(content, text=message, wraplength=500).grid(row=1, sticky="w", pady=(0, 8))

        area = ttk.Frame(content)
        area.grid(row=2, sticky="nsew")
        rows = ttk.Treeview(area, columns=("state", "name", "id"), show="headings")
        for name, label, width in (("state", "处理方式", 125), ("name", "软件", 210), ("id", "WinGet ID", 260)):
            rows.heading(name, text=label)
            rows.column(name, width=width, stretch=name != "state")
        rows.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(area, orient="vertical", command=rows.yview)
        scroll.pack(side="right", fill="y")
        rows.configure(yscrollcommand=scroll.set)
        for state in states:
            for package_id in groups[state]:
                rows.insert("", "end", values=(labels[state], self.packages[package_id].name, package_id))

        def install() -> None:
            dialog.destroy()
            for package_id in groups[State.INSTALLED]:
                self._show_package_result(
                    package_id, PackageResult(State.SKIPPED, "本机已安装。"), record_check=False
                )
            self._run_package_job(
                pending,
                "正在安装",
                self.winget.install_missing,
                lambda installed: self._finish_install(results, installed),
            )

        actions = ttk.Frame(content)
        actions.grid(row=3, sticky="ew", pady=(10, 0))
        if pending:
            ttk.Button(actions, text=f"安装 {len(pending)} 项", command=install, style="Primary.TButton").pack(
                side="right", padx=(8, 0)
            )
        else:
            self._finish_install(results, ())
        close = ttk.Button(actions, text="取消" if pending else "关闭", command=dialog.destroy)
        close.pack(side="right")
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()
        close.focus_set()

    def _finish_install(
        self,
        checked: tuple[tuple[str, PackageResult], ...],
        installed: tuple[tuple[str, PackageResult], ...],
    ) -> None:
        skipped = sum(result.state is State.INSTALLED for _, result in checked)
        skipped += sum(result.state is State.SKIPPED for _, result in installed)
        succeeded = sum(result.state is State.SUCCESS for _, result in installed)
        failed = tuple(package_id for package_id, result in installed if result.state is State.FAILED)
        unknown = tuple(package_id for package_id, result in (*checked, *installed) if result.state is State.UNKNOWN)
        self.retry_ids = failed + unknown
        self._refresh_action_buttons()
        summary = f"安装成功 {succeeded} 项 · 已跳过 {skipped} 项 · 安装失败 {len(failed)} 项"
        if unknown:
            summary += f" · 检测失败 {len(unknown)} 项"
        self.status.set(summary)
        self._write_log(summary)

    def _run_package_job(self, package_ids, label, action, on_done) -> None:
        def work():
            results = []
            for index, package_id in enumerate(package_ids, start=1):
                package = self.packages[package_id]
                self.events.put(("status", f"{label} {index}/{len(package_ids)} · {package.name}"))
                result = action(package)
                results.append((package_id, result))
                self.events.put(("package", (package_id, result)))
            return tuple(results)

        self._run_job(work, on_done, label, total=len(package_ids))

    def _clear_ai_preview(self, note: str = AI_HELP) -> None:
        self.ai_picks.clear()
        self.ai_reasons.clear()
        self.ai_packages.clear()
        self.ai_rows.delete(*self.ai_rows.get_children())
        self.ai_note.set(note)
        self._show_ai_reason()
        self._refresh_action_buttons()

    def _show_ai_preview(self, advice: Advice) -> None:
        for item in advice.packages:
            package = item.package
            self.ai_packages[package.id] = package
            self.ai_reasons[package.id] = item.reason
            if item.selected:
                self.ai_picks.add(package.id)
            if item.installed:
                state = "已安装"
            elif item.discovered:
                state = "新软件"
            else:
                state = "建议" if item.selected else "可选"
            self.ai_rows.insert(
                "", "end", iid=package.id,
                values=("✓" if item.selected else "□", package.name, state),
            )
        if advice.packages:
            first = advice.packages[0].package.id
            self.ai_rows.selection_set(first)
            self.ai_rows.focus(first)
            self._show_ai_reason()
        if advice.question:
            self.ai_note.set(advice.question)
        elif advice.packages:
            self.ai_note.set("核对并勾选推荐项，再点加入清单。")
        else:
            self.ai_note.set("没有找到可推荐的软件。可调整描述，或手动搜索。")
        summary = f"AI 推荐了 {len(advice.packages)} 项软件。"
        if advice.question:
            summary += "可在上方补充回答，再生成推荐。"
        if advice.unresolved:
            summary += f"另有 {len(advice.unresolved)} 项未匹配到唯一软件，可手动搜索。"
            self._write_log("未能核实的软件 · " + "、".join(advice.unresolved))
            if not advice.packages:
                self._write_text(self.ai_reason, "未能核实 · " + "、".join(advice.unresolved), replace=True)
        self.status.set(summary)
        self._refresh_action_buttons()

    def _show_ai_reason(self, _event=None) -> None:
        selected = self.ai_rows.selection()
        reason = self.ai_reasons.get(selected[0], "") if selected else ""
        self._write_text(self.ai_reason, reason or "点选推荐项可查看理由。", replace=True)

    def _toggle_ai_pick(self, package_id: str) -> None:
        if self.busy or package_id not in self.ai_reasons:
            return
        if package_id in self.ai_picks:
            self.ai_picks.remove(package_id)
        else:
            self.ai_picks.add(package_id)
        self.ai_rows.set(package_id, "pick", "✓" if package_id in self.ai_picks else "□")
        self._refresh_action_buttons()

    def _toggle_ai_on_click(self, event: tk.Event) -> None:
        if self.ai_rows.identify_region(event.x, event.y) == "cell" and self.ai_rows.identify_column(event.x) == "#1":
            self._toggle_ai_pick(self.ai_rows.identify_row(event.y))

    def _toggle_focused_ai(self, _event: tk.Event) -> str:
        self._toggle_ai_pick(self.ai_rows.focus())
        return "break"

    def _add_ai_picks(self) -> None:
        if self.busy or not self.ai_picks:
            return
        picked = tuple(package for package_id, package in self.ai_packages.items() if package_id in self.ai_picks)
        package_ids = self._merge_packages(picked)
        if package_ids is None:
            return
        added = set(package_ids) - self.selected
        self.selected.update(added)
        for package_id in added:
            self._refresh_row(package_id)
        message = f"已加入 {len(added)} 项推荐软件。" if added else "选中的推荐软件已在当前清单中。"
        self.status.set(message)
        self._write_log(message)
        self._refresh_action_buttons()

    def _generate_plan(self) -> None:
        requirements = self.requirements.get("1.0", "end-1c").strip()
        if not requirements:
            self.status.set("请先输入电脑用途。")
            return

        packages = tuple(self.packages.values())
        known_installed = frozenset(
            package_id.casefold() for package_id, result in self._fresh_checks(self.packages).items()
            if result.state is State.INSTALLED
        )
        self._clear_ai_preview(f"正在准备推荐（{self.ai_model}）。")
        self._run_job(
            lambda: advise(
                requirements, packages, self.winget, known_installed=known_installed, model=self.ai_model
            ),
            self._show_ai_preview,
            "正在生成 AI 推荐。",
            on_error=self.ai_note.set,
        )

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        for control in self.controls:
            control.configure(state="disabled" if busy else "normal")
        self._refresh_action_buttons()

    def _refresh_action_buttons(self) -> None:
        self.ai_apply.configure(state="normal" if self.ai_picks and not self.busy else "disabled")
        self.retry_button.configure(state="disabled" if self.busy else "normal")
        if self.retry_ids:
            self.retry_button.grid()
        else:
            self.retry_button.grid_remove()

    def _run_job(self, task, on_done, message: str, *, total: int = 0, on_error=None) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self.status.set(message)
        self.progress.configure(mode="determinate" if total else "indeterminate", maximum=total or 100, value=0)
        self.progress.grid()
        if not total:
            self.progress.start(40)

        def run() -> None:
            try:
                self.events.put(("done", task()))
            except Exception as error:
                self.events.put(("error", str(error)))

        Thread(target=run, daemon=True).start()
        self.root.after(60, self._poll_job, on_done, on_error)

    def _poll_job(self, on_done, on_error) -> None:
        while True:
            try:
                event, data = self.events.get_nowait()
            except Empty:
                self.root.after(60, self._poll_job, on_done, on_error)
                return
            if event == "status":
                self.status.set(data)
            elif event == "package":
                self._show_package_result(*data)
                self.progress.configure(value=float(self.progress.cget("value")) + 1)
            else:
                self.progress.stop()
                self.progress.grid_remove()
                self._set_busy(False)
                if event == "done":
                    on_done(data)
                else:
                    self.status.set("操作失败。详情见操作记录。")
                    self._write_log(data)
                    if on_error is not None:
                        on_error(data)
                return

    def _show_package_result(self, package_id: str, result: PackageResult, *, record_check: bool = True) -> None:
        if record_check:
            if result.state in (State.INSTALLED, State.MISSING):
                self.checked[package_id] = (monotonic(), result)
            elif result.state in (State.SUCCESS, State.SKIPPED):
                self.checked[package_id] = (monotonic(), PackageResult(State.INSTALLED))
            else:
                self.checked.pop(package_id, None)
        self.states[package_id] = result.state.value
        self._refresh_row(package_id)
        line = f"{self.packages[package_id].name} · {result.state.value}"
        if result.detail:
            line += "\n" + result.detail
        self._write_log(line)

    def _close(self) -> None:
        if self.busy:
            messagebox.showinfo("操作进行中", "请等待当前操作完成后再关闭窗口。", parent=self.root)
            return
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    WinSetupApp(root)
    root.mainloop()
