# WinSetup Lite

WinSetup Lite 是一款面向 Windows 11 的装机与软件迁移工具。重装系统、换电脑或配置开发环境时，可以整理一份软件清单，让 WinGet 检查本机状态，再批量安装缺少的软件。

软件可以手动添加，也可以由本地 AI 根据你的用途推荐。推荐先显示在右侧，确认后再加入清单。旧电脑上的软件清单可以导出为 JSON，带到新电脑继续使用。

## 快速开始

运行环境为 Windows 11、带 Tkinter 的 Python 3.11 或更新版本，以及可用的 WinGet。程序使用 Python 标准库，通过 WinGet 安装软件。AI 推荐是可选功能。

在项目根目录打开 PowerShell，运行下面的命令。

```powershell
winget --version
python -m winsetup
```

如果找不到 `winget`，可以从 Microsoft Store 安装或更新 App Installer。若提示需要接受软件源条款，先运行 `winget list --source winget`，按终端提示处理。 [WinGet 官方文档](https://learn.microsoft.com/windows/package-manager/winget/)

## 整理清单与批量安装

1. **选软件。** 在左侧勾选所需项目。目录内置 Python 3.11、Git、Visual Studio Code、7-Zip 和 VLC。要加其他软件，点 **搜索并添加软件**，输入软件名或 WinGet ID，核对后点 **添加所选**。
2. **核对安装项。** 点 **安装所选缺失项**，确认窗口会列出待安装、已安装将跳过、检测失败的项目。也可以先点 **检查状态** 单独查看本机状态。打开确认窗口时，程序会复用 90 秒内的有效检测结果。
3. **确认并跟踪进度。** 点确认窗口中的 **安装**。主窗口底部会显示处理进度，结束后汇总成功、跳过和失败的数量。右下角的 **操作记录** 会打开独立窗口，可查看错误详情。点 **重试失败项** 可以重新处理失败的项目。

点击 **保存清单** 可将当前勾选项导出为 JSON。添加或移除软件后，左侧目录会自动保存到本机。

## 用 AI 推荐软件

安装并启动 Ollama，然后运行下面的命令下载默认模型。 [Ollama Windows 使用文档](https://docs.ollama.com/windows)

```powershell
ollama pull qwen3:1.7b
```

在右侧输入电脑用途和偏好。例如「我主要用这台电脑写 Python，需要 Git，编辑器沿用已有的 VS Code」。点击 **生成推荐** 后，点选软件名称可在列表下方查看理由。

- 程序会参考 WinGet 识别到的已安装项。推荐中的已装软件会标记为「已安装」，默认不勾选。
- 对目录之外的建议，程序用 WinGet 核对名称或软件包 ID。唯一匹配且尚未检测为已安装的项目会标记为「新软件」，可手动勾选。未能唯一匹配的名称可在 **操作记录** 中查看。
- 如果 AI 提出需要澄清的问题，可以把回答追加到输入框，再次生成推荐。

确认后点 **加入清单**。所选推荐会合并到左侧，并保留原来的勾选。点击左侧的安装入口并确认，才会开始安装。

推荐请求发给本机 Ollama（`127.0.0.1:11434`）。如果要换模型，先用 `ollama list` 查看可用名称，再替换下面命令中的模型名，在同一个 PowerShell 窗口中执行。

```powershell
$env:WINSETUP_OLLAMA_MODEL = "已安装的模型名"
python -m winsetup
```

## 换机或重装时恢复软件

1. 在旧电脑上点 **扫描本机软件**，勾选要保留的软件，点击 **导出所选**。
2. 把生成的 JSON 文件带到新电脑。在 WinSetup Lite 中点 **导入清单**，程序会勾选清单中的软件并检查安装状态。
3. 核对结果后点 **安装所选缺失项**，按确认窗口的提示完成安装。

扫描范围是 WinGet 社区软件源能够匹配的本机应用。JSON 清单记录软件名称和 WinGet ID。软件设置、个人文件和账号数据需要另行备份。 [WinGet 导出文档](https://learn.microsoft.com/windows/package-manager/winget/export)

## 开发与测试

桌面界面使用 Tkinter，软件搜索、检测和安装通过 WinGet 调用，AI 推荐通过本地 Ollama API 生成。

```powershell
python -m unittest discover -s tests -v
```

测试覆盖清单读写、WinGet 检测与安装结果处理，以及 AI 建议的校验和软件匹配。测试使用模拟接口，可在未安装 WinGet 或 Ollama 的环境中运行。

## 许可证与参考

本项目采用 [MIT 许可证](LICENSE)。桌面软件管理界面的设计参考了 [Winhance](https://github.com/memstechtips/Winhance)。
