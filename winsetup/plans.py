import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .catalog import PACKAGES_BY_ID, Package, is_package_id


def load_plan(path: Path) -> tuple[Package, ...]:
    plan = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] not in (1, 2):
        raise ValueError("只支持版本为 1 或 2 的 WinSetup Lite 清单。")

    items = plan.get("packages")
    if not isinstance(items, list):
        raise ValueError("清单中的 packages 必须是列表。")

    builtins = {package_id.casefold(): package for package_id, package in PACKAGES_BY_ID.items()}
    packages = {}
    for item in items:
        if plan["version"] == 1:
            if not isinstance(item, str) or item not in PACKAGES_BY_ID:
                raise ValueError("旧版清单包含未收录的软件包。")
            package = PACKAGES_BY_ID[item]
        else:
            if not isinstance(item, dict) or not is_package_id(item.get("id")):
                raise ValueError("清单包含无效的 WinGet 软件包 ID。")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip() or not name.isprintable():
                raise ValueError("清单包含无效的软件名称。")
            package = builtins.get(item["id"].casefold()) or Package(item["id"], name.strip())
        packages.setdefault(package.id.casefold(), package)

    return tuple(packages.values())


def save_plan(path: Path, packages: tuple[Package, ...]) -> None:
    plan = {
        "version": 2,
        "packages": [{"id": package.id, "name": package.name} for package in packages],
    }
    temporary_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary_path = Path(file.name)
            json.dump(plan, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
