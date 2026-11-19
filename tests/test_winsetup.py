import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from winsetup.advisor import advise
from winsetup.catalog import PACKAGES, PACKAGES_BY_ID, Package
from winsetup.ollama import OLLAMA_URL, recommend
from winsetup.plans import load_plan, save_plan
from winsetup.winget import NO_PACKAGES_FOUND, State, WinGet


GIT_ID = "Git.Git"
GIT = PACKAGES_BY_ID[GIT_ID]
VSCODE_ID = "Microsoft.VisualStudioCode"
WECHAT = Package("Tencent.WeChat", "微信")


def process(code, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class PlanTests(unittest.TestCase):
    def test_round_trip_preserves_selection_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "清单.json"
            packages = (WECHAT, PACKAGES_BY_ID[VSCODE_ID], GIT)
            save_plan(path, packages)
            self.assertEqual(load_plan(path), packages)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)
            self.assertEqual(tuple(Path(directory).iterdir()), (path,))

    def test_legacy_plan_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps({"version": 1, "packages": [GIT_ID, VSCODE_ID]}), encoding="utf-8")
            self.assertEqual(load_plan(path), (GIT, PACKAGES_BY_ID[VSCODE_ID]))

    def test_rejects_invalid_ids_and_bad_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            for plan in (
                {"version": 1, "packages": ["Outside.Catalog"]},
                {"version": True, "packages": [GIT_ID]},
                {"version": 1, "packages": GIT_ID},
                {"version": 2, "packages": [{"id": "--name", "name": "不合法"}]},
                {"version": 2, "packages": [{"id": "Tencent.WeChat", "name": ""}]},
            ):
                with self.subTest(plan=plan):
                    path.write_text(json.dumps(plan), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_plan(path)


class WinGetTests(unittest.TestCase):
    def setUp(self):
        self.winget = WinGet()
        self.winget.executable = "winget.exe"

    @patch("winsetup.winget.subprocess.run")
    def test_search_reads_cjk_and_keeps_exact_package_id(self, run):
        run.return_value = process(0, stdout=(
            "\x1b[?25l\r  |\r\x1b[?25h"
            "名称            ID                     版本         匹配方式\n"
            "---------------------------------------------------------\n"
            "微信            Tencent.WeChat         4.1.0\n"
            "Tencent WeChat  Tencent.WeChat         4.1.0\n"
            "微信开发工具    Tencent.WeChatDevTools  1.2\n"
            "Long package    Publisher.LongId…      2.0\n"
        ))
        self.assertEqual(
            self.winget.search("微信"),
            (WECHAT, Package("Tencent.WeChatDevTools", "微信开发工具")),
        )
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["winget.exe", "search", "--query", "微信"])
        self.assertIn("--source", args)

    @patch("winsetup.winget.subprocess.run", return_value=process(NO_PACKAGES_FOUND - 2**32))
    def test_search_with_no_matches(self, _run):
        self.assertEqual(self.winget.search("no match"), ())

    @patch("winsetup.winget.subprocess.run")
    def test_installed_scan_reads_export_json_and_resolves_names(self, run):
        temporary_paths = []

        def execute(args, **_kwargs):
            if args[1] == "export":
                self.assertEqual(args[2], "--output")
                self.assertEqual(args[4:], ["--source", "winget", "--disable-interactivity"])
                output = Path(args[3])
                temporary_paths.append(output)
                output.write_text(json.dumps({
                    "Sources": [{
                        "Packages": [
                            {"PackageIdentifier": "Tencent.WeChat"},
                            {"PackageIdentifier": "tencent.wechat"},
                            {"PackageIdentifier": "Git.Git"},
                            {"PackageIdentifier": "--unsafe"},
                        ],
                    }],
                }), encoding="utf-8-sig")
                return process(0, stdout="warning: unmatched app")
            self.assertEqual(args[1:], ["list", "--source", "winget", "--disable-interactivity"])
            return process(0, stdout=(
                "名称            ID                     版本\n"
                "-----------------------------------------\n"
                "微信            Tencent.WeChat         4.1.0\n"
            ))

        run.side_effect = execute
        self.assertEqual(self.winget.installed_packages(), (Package(GIT_ID, "Git"), WECHAT))
        self.assertEqual(run.call_count, 2)
        self.assertFalse(temporary_paths[0].exists())

    @patch("winsetup.winget.subprocess.run")
    def test_installed_scan_keeps_ids_if_display_names_unavailable(self, run):
        def execute(args, **_kwargs):
            if args[1] == "list":
                raise subprocess.TimeoutExpired(args, 20)
            Path(args[3]).write_text(json.dumps({
                "Sources": [{"Packages": [{"PackageIdentifier": "Tencent.WeChat"}]}]
            }), encoding="utf-8")
            return process(0)

        run.side_effect = execute
        self.assertEqual(self.winget.installed_packages(), (Package(WECHAT.id, WECHAT.id),))

    @patch("winsetup.winget.subprocess.run", return_value=process(0))
    def test_installed_scan_reports_missing_inventory_file(self, run):
        with self.assertRaisesRegex(RuntimeError, "无法读取"):
            self.winget.installed_packages()
        run.assert_called_once()

    @patch("winsetup.winget.subprocess.run", return_value=process(0))
    def test_already_installed_skips_install(self, run):
        result = self.winget.install_missing(GIT)
        self.assertIs(result.state, State.SKIPPED)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(
            run.call_args.args[0],
            ["winget.exe", "list", "--id", GIT_ID, "--exact", "--source", "winget", "--disable-interactivity"],
        )

    @patch("winsetup.winget.subprocess.run")
    def test_installs_missing_package_by_exact_id(self, run):
        run.side_effect = [process(NO_PACKAGES_FOUND - 2**32), process(0)]
        result = self.winget.install_missing(GIT)
        self.assertIs(result.state, State.SUCCESS)
        install_command = run.call_args_list[1].args[0]
        self.assertEqual(
            install_command,
            ["winget.exe", "install", "--id", GIT_ID, "--exact", "--source", "winget", "--disable-interactivity"],
        )

    @patch("winsetup.winget.subprocess.run", return_value=process(1, stderr="source unavailable"))
    def test_failed_check_is_not_treated_as_missing(self, run):
        result = self.winget.install_missing(GIT)
        self.assertIs(result.state, State.UNKNOWN)
        self.assertIn("source unavailable", result.detail)
        run.assert_called_once()

    @patch("winsetup.winget.subprocess.run")
    def test_install_error_is_shown(self, run):
        run.side_effect = [process(NO_PACKAGES_FOUND), process(1, stderr="installer exited")]
        result = self.winget.install_missing(GIT)
        self.assertIs(result.state, State.FAILED)
        self.assertIn("installer exited", result.detail)


class OllamaTests(unittest.TestCase):
    @patch("winsetup.ollama.build_opener")
    def test_advice_checks_local_installation_and_resolves_only_unique_matches(self, build_opener):
        opener = build_opener.return_value
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"message": {"content": json.dumps({
            "recommendations": [{"package_id": GIT_ID, "reason": "管理作业版本", "priority": "required"}],
            "searches": [{"name": "Zotero", "reason": "管理参考文献"}, {"name": "Editor", "reason": "编辑 Markdown"}],
            "question": "还需要 PDF 批注功能吗？",
        })}})
        winget = WinGet()
        winget.executable = "winget.exe"
        zotero = Package("DigitalScholar.Zotero", "Zotero")
        responses = {
            "Zotero": (zotero, Package("Vendor.Other", "Zotero Helper")),
            "Editor": (Package("Vendor.Editor", "Editor"), Package("Other.Editor", "Editor")),
        }
        with patch.object(winget, "installed_ids", return_value=("git.git",)), patch.object(
            winget, "search", side_effect=lambda query, **_: responses[query]
        ):
            result = advise("用 Git 管代码，用 Zotero 管文献", PACKAGES, winget, model="local-test")

        request = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(request["model"], "local-test")
        self.assertEqual(json.loads(request["messages"][1]["content"])["installed_ids"], [GIT_ID])
        installed, new = result.packages
        self.assertTrue(installed.installed)
        self.assertFalse(installed.selected)
        self.assertEqual(new.package.id, zotero.id)
        self.assertTrue(new.discovered)
        self.assertFalse(new.selected)
        self.assertEqual(result.unresolved, ("Editor",))
        self.assertEqual(result.question, "还需要 PDF 批注功能吗？")

    @patch("winsetup.ollama.build_opener")
    def test_advice_does_not_search_excluded_or_unrequested_apps_in_explicit_only_request(self, build_opener):
        response = build_opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({"message": {"content": json.dumps({
            "recommendations": [{"package_id": GIT_ID, "reason": "管理版本", "priority": "required"}],
            "searches": [{"name": "Zotero", "reason": "管理文献"}],
            "question": "",
        })}})
        winget = WinGet()
        winget.executable = "winget.exe"
        with patch.object(winget, "installed_ids", side_effect=RuntimeError("WinGet 超时")), patch.object(
            winget, "search"
        ) as search:
            for requirements in ("只装 Git", "需要 Git，不要 Zotero"):
                result = advise(requirements, PACKAGES, winget)
                self.assertEqual([item.package.id for item in result.packages], [GIT_ID])
                self.assertTrue(result.packages[0].selected)
            search.assert_not_called()

    @patch("winsetup.ollama.build_opener")
    def test_recommend_uses_local_endpoint_and_validates_catalog(self, build_opener):
        opener = build_opener.return_value
        response = opener.open.return_value.__enter__.return_value

        def model_reply(package_id):
            return json.dumps({
                "message": {
                    "content": json.dumps({
                        "recommendations": [{
                            "package_id": package_id,
                            "reason": "需要管理代码版本",
                            "priority": "required",
                        }]
                    })
                }
            })

        response.read.return_value = model_reply(GIT_ID)
        result = recommend("只补 Git")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, OLLAMA_URL)
        self.assertEqual(json.loads(request.data)["model"], "qwen3:1.7b")
        self.assertEqual(result[0].package_id, GIT_ID)

        response.read.return_value = model_reply("Outside.Catalog")
        with self.assertRaisesRegex(ValueError, "目录之外"):
            recommend("安装其他软件")

    @patch("winsetup.ollama.build_opener")
    def test_recommendation_limits_selection_to_user_intent(self, build_opener):
        opener = build_opener.return_value
        response = opener.open.return_value.__enter__.return_value
        python_id = "Python.Python.3.11"
        zip_id = "7zip.7zip"
        vlc_id = "VideoLAN.VLC"
        # Reproduce the original failure: the model calls every tool required.
        response.read.return_value = json.dumps({
            "message": {
                "content": json.dumps({
                    "recommendations": [
                        {"package_id": package_id, "reason": "测试理由", "priority": "required"}
                        for package_id in PACKAGES_BY_ID
                    ]
                })
            }
        })

        for need, selected, optional in (
            ("写python", {python_id}, {GIT_ID, VSCODE_ID}),
            ("只推荐 Git，其他软件先不用。", {GIT_ID}, set()),
            ("用 Python 不用 Git，也要解压文件，但不要 VLC", {python_id, zip_id}, {VSCODE_ID}),
            ("写 Python，另外想播放本地视频", {python_id, vlc_id}, {GIT_ID, VSCODE_ID}),
        ):
            with self.subTest(need=need):
                result = recommend(need)
                self.assertEqual({item.package_id for item in result if item.selected}, selected)
                self.assertEqual({item.package_id for item in result if not item.selected}, optional)
                request = json.loads(opener.open.call_args.args[0].data)
                fields = request["format"]["properties"]["recommendations"]["items"]["properties"]
                self.assertEqual(set(fields["package_id"]["enum"]), selected | optional)

    @patch("winsetup.ollama.build_opener")
    def test_optional_recommendation_is_not_selected(self, build_opener):
        response = build_opener.return_value.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({
            "message": {
                "content": json.dumps({
                    "recommendations": [{
                        "package_id": GIT_ID,
                        "reason": "可用于版本管理",
                        "priority": "optional",
                    }]
                })
            }
        })
        result = recommend("Python 需要版本控制工具")
        self.assertEqual(result[0].package_id, GIT_ID)
        self.assertFalse(result[0].selected)

    @patch("winsetup.ollama.build_opener")
    def test_ai_uses_the_current_catalog(self, build_opener):
        opener = build_opener.return_value
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = json.dumps({
            "message": {
                "content": json.dumps({
                    "recommendations": [{
                        "package_id": WECHAT.id,
                        "reason": "用于微信消息",
                        "priority": "required",
                    }]
                })
            }
        })
        packages = tuple(package for package in PACKAGES if package.id != GIT_ID) + (WECHAT,)
        result = recommend("需要微信", packages=packages)
        request = json.loads(opener.open.call_args.args[0].data)
        fields = request["format"]["properties"]["recommendations"]["items"]["properties"]
        self.assertIn(WECHAT.id, fields["package_id"]["enum"])
        self.assertNotIn(GIT_ID, fields["package_id"]["enum"])
        self.assertTrue(result[0].selected)

    @patch("winsetup.ollama.build_opener")
    def test_unavailable_ollama_returns_actionable_error(self, build_opener):
        build_opener.return_value.open.side_effect = URLError("connection refused")
        with self.assertRaisesRegex(RuntimeError, "本机 Ollama"):
            recommend("需要 Git")
