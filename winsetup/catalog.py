import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Package:
    id: str
    name: str
    purpose: str = ""


PACKAGES = (
    Package("Python.Python.3.11", "Python 3.11", "运行 Python 项目和脚本"),
    Package("Git.Git", "Git", "版本控制和操作 Git 仓库"),
    Package("Microsoft.VisualStudioCode", "Visual Studio Code", "编辑代码和配置文件"),
    Package("7zip.7zip", "7-Zip", "压缩和解压文件"),
    Package("VideoLAN.VLC", "VLC", "播放本地音频和视频"),
)

PACKAGES_BY_ID = {package.id: package for package in PACKAGES}


_PACKAGE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*(?:\.[a-zA-Z0-9][a-zA-Z0-9_-]*)*")


def is_package_id(package_id: object) -> bool:
    return (
        isinstance(package_id, str)
        and len(package_id) <= 128
        and _PACKAGE_ID.fullmatch(package_id) is not None
    )
