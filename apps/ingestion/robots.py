"""robots.txt по RFC 9309.

urllib.robotparser применяет первое подходящее правило в порядке файла, поэтому частый шаблон
«Disallow: /» + «Allow: /rss.aspx» у него запрещает и ленту. По RFC побеждает самое длинное
совпадение пути (при равной длине — Allow), поддерживаются «*» и «$».
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import unquote, urlsplit


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, шаблон пути)


@lru_cache(maxsize=1024)
def _pattern(path: str) -> re.Pattern:
    anchored = path.endswith("$")
    body = re.escape(unquote(path.rstrip("$"))).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


class Robots:
    def __init__(self, text: str = "", *, disallow_all: bool = False):
        self.disallow_all = disallow_all
        self.groups: list[Group] = []
        current: Group | None = None
        in_agents = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if not in_agents:
                    current = Group()
                    self.groups.append(current)
                current.agents.append(value.lower())
                in_agents = True
            elif key in {"allow", "disallow"} and current is not None:
                in_agents = False
                if value:  # пустой Disallow ничего не запрещает
                    current.rules.append((key == "allow", value))
            else:
                in_agents = False

    def _rules(self, user_agent: str) -> list[tuple[bool, str]]:
        token = user_agent.split("/", 1)[0].strip().lower()
        named = [g for g in self.groups if any(a != "*" and a in token for a in g.agents)]
        chosen = named or [g for g in self.groups if "*" in g.agents]
        return [rule for group in chosen for rule in group.rules]

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        parts = urlsplit(url)
        path = unquote(parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if path == "/robots.txt":
            return True
        best: tuple[int, bool] | None = None
        for allow, pattern in self._rules(user_agent):
            if _pattern(pattern).match(path):
                candidate = (len(pattern), allow)
                if best is None or candidate > best:
                    best = candidate
        return True if best is None else best[1]
