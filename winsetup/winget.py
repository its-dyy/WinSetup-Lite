import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from unicodedata import category, east_asian_width

from .catalog import PACKAGES, Package, is_package_id


NO_PACKAGES_FOUND = 0x8A150014
CHECK_TIMEOUT_SECONDS = 45
SCAN_TIMEOUT_SECONDS = 120
LIST_NAMES_TIMEOUT_SECONDS = 20


class State(Enum):
    INSTALLED = "已安装"
    MISSING = "待安装"
    UNKNOWN = "检测失败"
    SUCCESS = "安装成功"
    SKIPPED = "已跳过"
    FAILED = "安装失败"


@dataclass(frozen=True)
class PackageResult:
    state: State
    detail: str = ""


def _error_detail(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return f"WinGet 退出码 0x{result.returncode & 0xFFFFFFFF:08X}\n{output[-1500:]}"


def _column_width(character: str) -> int:
    if category(character) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if east_asian_width(character) in ("F", "W") else 1


def _split_at_column(line: str, column: int) -> tuple[str, str]:
    width = 0
    for index, character in enumerate(line):
        if width >= column:
            return line[:index], line[index:]
        width += _column_width(character)
    return line, ""


def _parse_package_table(output: str) -> tuple[Package, ...]:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    lines = text.splitlines()
    for index in range(1, len(lines)):
        if not re.fullmatch(r"-{3,}", lines[index].strip()):
            continue
        header = lines[index - 1]
        # Name is the first column and the package ID is the second.
        # WinGet pads the first column using display width, including CJK glyphs.
        id_header = re.search(r"(?<!\S)ID(?=\s)", header, re.IGNORECASE)
        if id_header is None:
            columns = list(re.finditer(r"\S+(?: \S+)*", header))
            if len(columns) < 2:
                continue
            id_header = columns[1]
        id_column = sum(map(_column_width, header[:id_header.start()]))
        packages = {}
        for line in lines[index + 1:]:
            name, remainder = _split_at_column(line, id_column)
            fields = remainder.split()
            name = name.strip()
            if name and name.isprintable() and len(fields) >= 2 and is_package_id(fields[0]):
                package_id = fields[0]
                packages.setdefault(package_id.casefold(), Package(package_id, name))
        if packages:
            return tuple(packages.values())
    raise ValueError("无法读取 WinGet 软件表格。")


def _inventory_ids(export: object) -> tuple[str, ...]:
    if not isinstance(export, dict) or not isinstance(export.get("Sources"), list):
        raise ValueError("WinGet 清单缺少 Sources。")

    ids = {}
    for source in export["Sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("Packages"), list):
            raise ValueError("WinGet 清单缺少 Packages。")
        for item in source["Packages"]:
            package_id = item.get("PackageIdentifier") if isinstance(item, dict) else None
            if is_package_id(package_id):
                ids.setdefault(package_id.casefold(), package_id)
    return tuple(ids.values())


class WinGet:
    def __init__(self) -> None:
        self.executable = shutil.which("winget.exe") if os.name == "nt" else None

    def _run(self, *arguments: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        if self.executable is None:
            raise FileNotFoundError("当前环境无法运行 WinGet。请在 Windows 11 上检查 App Installer。")

        return subprocess.run(
            [self.executable, *arguments, "--source", "winget", "--disable-interactivity"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )

    def _execute(
        self, verb: str, package: Package, *, timeout: float | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._run(verb, "--id", package.id, "--exact", timeout=timeout)

    def search(self, query: str, *, timeout: float = CHECK_TIMEOUT_SECONDS) -> tuple[Package, ...]:
        query = query.strip()
        if not query or query.startswith("-") or not query.isprintable():
            raise ValueError("请输入软件名称或软件包 ID。")
        try:
            result = self._run("search", "--query", query, "--count", "25", timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("WinGet 搜索超时。请稍后重试。") from error

        code = result.returncode & 0xFFFFFFFF
        if code == NO_PACKAGES_FOUND:
            return ()
        if code != 0:
            raise RuntimeError(_error_detail(result))
        try:
            return _parse_package_table(result.stdout)
        except ValueError as error:
            raise RuntimeError("无法读取 WinGet 的搜索结果。请换个软件名或完整的软件包 ID。") from error

    def installed_ids(self, *, timeout: float = SCAN_TIMEOUT_SECONDS) -> tuple[str, ...]:
        with TemporaryDirectory(prefix="winsetup-inventory-") as directory:
            output = Path(directory) / "installed.json"
            try:
                result = self._run("export", "--output", str(output), timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("扫描本机软件超时。请稍后重试。") from error

            code = result.returncode & 0xFFFFFFFF
            if code == NO_PACKAGES_FOUND:
                return ()
            if code:
                raise RuntimeError(_error_detail(result))

            try:
                return _inventory_ids(json.loads(output.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError) as error:
                raise RuntimeError("无法读取 WinGet 导出的本机软件清单。") from error

    def installed_packages(self) -> tuple[Package, ...]:
        ids = self.installed_ids()
        if not ids:
            return ()

        names = {package.id.casefold(): package.name for package in PACKAGES}
        # The export JSON supplies authoritative IDs. The listing adds display names when available.
        try:
            result = self._run("list", timeout=LIST_NAMES_TIMEOUT_SECONDS)
            if result.returncode == 0:
                names.update({
                    package.id.casefold(): package.name for package in _parse_package_table(result.stdout)
                })
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass

        packages = [Package(package_id, names.get(package_id.casefold(), package_id)) for package_id in ids]
        return tuple(sorted(packages, key=lambda package: (package.name.casefold(), package.id.casefold())))

    def check(self, package: Package) -> PackageResult:
        try:
            result = self._execute("list", package, timeout=CHECK_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return PackageResult(State.UNKNOWN, "检测超时。请稍后重试。")
        except OSError as error:
            return PackageResult(State.UNKNOWN, str(error))

        code = result.returncode & 0xFFFFFFFF
        if code == 0:
            return PackageResult(State.INSTALLED)
        if code == NO_PACKAGES_FOUND:
            return PackageResult(State.MISSING)
        return PackageResult(State.UNKNOWN, _error_detail(result))

    def install_missing(self, package: Package) -> PackageResult:
        check = self.check(package)
        if check.state is State.INSTALLED:
            return PackageResult(State.SKIPPED, "本机已安装。")
        if check.state is State.UNKNOWN:
            return check

        try:
            result = self._execute("install", package)
        except OSError as error:
            return PackageResult(State.FAILED, str(error))
        if result.returncode == 0:
            return PackageResult(State.SUCCESS)
        return PackageResult(State.FAILED, _error_detail(result))
