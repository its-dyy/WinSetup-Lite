import json
import re
from dataclasses import asdict, dataclass
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .catalog import PACKAGES, Package


OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "qwen3:1.7b"

# General utilities need a matching use case. Development helpers can be shown
# as optional when the user has not asked for their capability.
_UTILITY_IDS = {"7zip.7zip", "VideoLAN.VLC"}
_TOOL_TERMS = {
    "Python.Python.3.11": r"python(?:\s*3(?:\.11)?)?",
    "Git.Git": r"git(?:hub)?|版本控制|代码仓库|提交代码",
    "Microsoft.VisualStudioCode": r"v\s*s\s*code|visual studio code|编辑器|ide",
    "7zip.7zip": r"7[ -]?zip|解压|压缩(?:包|文件)?|unzip|archive",
    "VideoLAN.VLC": r"vlc|播(?:放)?[影音]|播放.*(?:音频|视频|电影)|看(?:本地|离线)?(?:视频|电影)|听音乐|媒体播放器",
}
_NEGATIVE_TERMS = r"不想|不需要|不推荐|不装|不用|不要|先别|别装|无需|\b(?:no|without|do not|don.t)\b"
_NEGATION = re.compile(_NEGATIVE_TERMS, re.IGNORECASE)
_CLAUSE_BOUNDARY = re.compile(
    rf"[，,。；;！!？?\n]|但是|不过|但|还要|也要|还需要|改用|再装|(?={_NEGATIVE_TERMS})",
    re.IGNORECASE,
)
_ONLY_TOOL = re.compile(r"(?:我)?只(?:想|要|需)?(?:推荐|安装|需要|使用|装|补|用|要)\s*(.+)")


@dataclass(frozen=True)
class Recommendation:
    package_id: str
    reason: str
    selected: bool


@dataclass(frozen=True)
class SearchSuggestion:
    name: str
    reason: str


@dataclass(frozen=True)
class RecommendationPlan:
    recommendations: tuple[Recommendation, ...] = ()
    searches: tuple[SearchSuggestion, ...] = ()
    question: str = ""


def _matches(pattern: str, text: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])", text, re.IGNORECASE))


def _candidate_ids(
    requirements: str, packages_by_id: dict[str, Package]
) -> tuple[set[str], set[str], bool]:
    terms = {
        package.id: _TOOL_TERMS.get(package.id, f"{re.escape(package.name)}|{re.escape(package.id)}")
        for package in packages_by_id.values()
    }
    clauses = [clause.strip() for clause in _CLAUSE_BOUNDARY.split(requirements) if clause.strip()]
    requested = set()
    excluded = set()

    for clause in clauses:
        matches = {package_id for package_id, pattern in terms.items() if _matches(pattern, clause)}
        if _NEGATION.search(clause):
            excluded.update(matches)
        else:
            requested.update(matches)

    allowed = (set(packages_by_id) - _UTILITY_IDS) | requested
    only_known = False
    if clauses and (only := _ONLY_TOOL.fullmatch(clauses[0])):
        exact = {
            package_id for package_id, pattern in terms.items()
            if re.fullmatch(pattern, only[1], re.IGNORECASE)
        }
        if len(exact) == 1:
            allowed = exact
            only_known = True

    return allowed - excluded, requested - excluded, only_known


def _decode_recommendations(
    content: dict, packages_by_id: dict[str, Package], allowed: set[str], requested: set[str]
) -> tuple[Recommendation, ...]:
    recommendations = content.get("recommendations")
    if not isinstance(recommendations, list):
        raise ValueError("AI 回复缺少有效的推荐列表。")

    valid = {}
    for item in recommendations:
        if not isinstance(item, dict):
            raise ValueError("AI 回复中有无效的推荐项。")
        package_id = item.get("package_id")
        reason = item.get("reason")
        if not isinstance(package_id, str) or package_id not in packages_by_id:
            raise ValueError("AI 推荐了目录之外的软件。请调整需求后重试。")
        if package_id not in allowed:
            continue
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("AI 推荐缺少理由。")
        priority = item.get("priority")
        if priority not in ("required", "optional"):
            raise ValueError("AI 推荐缺少有效的优先级。")
        selected = priority == "required" and (
            package_id not in _TOOL_TERMS or package_id == "Python.Python.3.11" or package_id in requested
        )
        if priority == "required" and not selected:
            reason = f"可用于{packages_by_id[package_id].purpose}，按需选择。"
        valid.setdefault(package_id, Recommendation(package_id, reason.strip()[:250], selected))

    return tuple(valid.values())


def recommend(
    requirements: str, model: str = DEFAULT_MODEL, *, packages: tuple[Package, ...] = PACKAGES
) -> tuple[Recommendation, ...]:
    return recommend_plan(requirements, model, packages=packages, allow_search=False).recommendations


def recommend_plan(
    requirements: str,
    model: str = DEFAULT_MODEL,
    *,
    packages: tuple[Package, ...] = PACKAGES,
    installed: frozenset[str] = frozenset(),
    allow_search: bool = True,
) -> RecommendationPlan:
    packages_by_id = {package.id: package for package in packages}
    allowed, requested, only_known = _candidate_ids(requirements, packages_by_id)
    allow_search = allow_search and not only_known
    if not allowed and not allow_search:
        return RecommendationPlan()

    catalog = [asdict(package) for package in packages if package.id in allowed]
    ids = {"type": "string"}
    if catalog:
        ids["enum"] = [package["id"] for package in catalog]
    schema = {
        "type": "object",
        "properties": {
            "recommendations": {
                "type": "array",
                "maxItems": len(catalog),
                "items": {
                    "type": "object",
                    "properties": {
                        "package_id": ids,
                        "reason": {"type": "string"},
                        "priority": {"type": "string", "enum": ["required", "optional"]},
                    },
                    "required": ["package_id", "reason", "priority"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["recommendations"],
        "additionalProperties": False,
    }
    if allow_search:
        schema["properties"]["searches"] = {
            "type": "array",
            "maxItems": 3,
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["name", "reason"],
                "additionalProperties": False,
            },
        }
        schema["properties"]["question"] = {"type": "string"}
        schema["required"].extend(("searches", "question"))
    installed_ids = [package["id"] for package in catalog if package["id"].casefold() in installed]
    search_prompt = (
        "若目录缺少用户明确点名的软件，或缺少满足主要任务且无合适已装替代的软件，"
        "用 searches 提供最多三个可搜索的正式软件名和理由。不要猜 WinGet ID，也不要搜索目录中已有的软件。"
        "考虑用户的版本、平台、价格和排除要求。若关键选择取决于用户没有提供的信息，"
        "用 question 提出一个具体问题，保留已经明确的建议。无需追问时填空字符串。"
        if allow_search else ""
    )
    messages = [
        {
            "role": "system",
            "content": (
                "根据用户描述，从候选目录选出最小够用的装机清单。优先沿用已安装且符合要求的软件。"
                "required 仅用于明确请求或完成任务必需的软件；optional 仅用于确实有帮助的补充。"
                "只有写 Python 这一需求时，Python 必需，Git 和 VS Code 可选。"
                "严格遵守「只装」「不要装」等限制。可把用户说要沿用的软件列入清单供程序核对。"
                "忽略不相关的软件。无法匹配时返回空列表。"
                "每条中文理由要说明它与用户实际需求的关系，尽量不超过 30 字。" + search_prompt
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {"requirements": requirements, "catalog": catalog, "installed_ids": installed_ids},
                ensure_ascii=False,
            ),
        },
    ]
    data = json.dumps(
        {
            "model": model,
            "messages": messages,
            "format": schema,
            "think": False,
            "stream": False,
            "options": {"temperature": 0, "num_predict": max(512, len(catalog) * 64)},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(request, timeout=90) as response:
            payload = json.load(response)
            message = payload.get("message") if isinstance(payload, dict) else None
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise ValueError("AI 回复缺少内容。")
            content = json.loads(message["content"])
            if not isinstance(content, dict):
                raise ValueError("AI 回复格式有误。")
            recommendations = _decode_recommendations(content, packages_by_id, allowed, requested)
            if not allow_search:
                return RecommendationPlan(recommendations)
            return _add_search_suggestions(content, recommendations, requirements)
    except HTTPError as error:
        if error.code == 404:
            raise RuntimeError(f"本机 Ollama 没有模型 {model}。请先运行 ollama pull {model}。") from error
        raise RuntimeError(f"Ollama 返回 HTTP {error.code}。请检查本地服务。") from error
    except (URLError, TimeoutError, HTTPException) as error:
        raise RuntimeError("无法从本机 Ollama 获取回复。请确认服务已启动，或稍后重试。") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError("AI 回复无法解析。请调整需求后重试。") from error


def _add_search_suggestions(
    content: dict, recommendations: tuple[Recommendation, ...], requirements: str
) -> RecommendationPlan:
    raw_searches = content.get("searches")
    searches = {}
    excluded = tuple(clause for clause in _CLAUSE_BOUNDARY.split(requirements) if _NEGATION.search(clause))
    if isinstance(raw_searches, list):
        for item in raw_searches[:3]:
            if not isinstance(item, dict):
                continue
            name, reason = item.get("name"), item.get("reason")
            if not isinstance(name, str) or not isinstance(reason, str):
                continue
            name, reason = name.strip(), reason.strip()
            if name and reason and len(name) <= 96 and name.isprintable() and not name.startswith("-"):
                if any(_matches(re.escape(name), clause) for clause in excluded):
                    continue
                searches.setdefault(name.casefold(), SearchSuggestion(name, reason[:250]))
    question = content.get("question", "")
    question = question.strip()[:120] if isinstance(question, str) else ""
    return RecommendationPlan(recommendations, tuple(searches.values()), question)
