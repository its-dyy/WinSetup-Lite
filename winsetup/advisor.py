from dataclasses import dataclass

from .catalog import Package
from .ollama import DEFAULT_MODEL, recommend_plan
from .winget import WinGet


@dataclass(frozen=True)
class SuggestedPackage:
    package: Package
    reason: str
    selected: bool = False
    installed: bool = False
    discovered: bool = False


@dataclass(frozen=True)
class Advice:
    packages: tuple[SuggestedPackage, ...]
    question: str = ""
    unresolved: tuple[str, ...] = ()


def _name_key(text: str) -> str:
    return "".join(character for character in text.casefold() if character.isalnum())


def advise(
    requirements: str,
    packages: tuple[Package, ...],
    winget: WinGet,
    *,
    known_installed: frozenset[str] = frozenset(),
    model: str = DEFAULT_MODEL,
) -> Advice:
    installed = known_installed
    can_search = winget.executable is not None
    if can_search:
        try:
            installed = installed | frozenset(package_id.casefold() for package_id in winget.installed_ids(timeout=8))
        except (OSError, RuntimeError):
            # Recommendations can still use the catalog when the local scan is unavailable.
            pass

    plan = recommend_plan(requirements, model, packages=packages, installed=installed, allow_search=can_search)
    current = {package.id.casefold(): package for package in packages}
    suggestions = {}
    for item in plan.recommendations:
        key = item.package_id.casefold()
        suggestions[key] = SuggestedPackage(
            current[key], item.reason, selected=item.selected and key not in installed, installed=key in installed
        )

    unresolved = []
    known_names = {_name_key(value) for package in packages for value in (package.name, package.id)}
    for search in plan.searches:
        target = _name_key(search.name)
        if not target or target in known_names:
            continue
        try:
            matches = {
                item.id.casefold(): item for item in winget.search(search.name, timeout=8)
                if target in (_name_key(item.name), _name_key(item.id))
            }
        except (OSError, RuntimeError, ValueError):
            matches = {}
        if len(matches) != 1:
            unresolved.append(search.name)
            continue
        key, discovered = next(iter(matches.items()))
        package = current.get(key, Package(discovered.id, discovered.name, search.reason))
        suggestions.setdefault(
            key, SuggestedPackage(package, search.reason, installed=key in installed, discovered=key not in current)
        )

    return Advice(tuple(suggestions.values()), plan.question, tuple(unresolved))
