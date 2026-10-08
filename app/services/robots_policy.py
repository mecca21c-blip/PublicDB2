"""RFC 9309 robots policy parsing and matching for PublicDB2 crawling."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


PUBLICDB2_PRODUCT_TOKEN = "PublicDB2"
MAX_CRAWL_DELAY_SECONDS = 60.0
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_PRE_WRAPPER = re.compile(
    r"\A\s*(?:<!doctype\s+html\s*>\s*)?(?:<html(?:\s[^>]*)?>\s*)?"
    r"(?:<body(?:\s[^>]*)?>\s*)?<pre(?:\s[^>]*)?>(?P<body>.*?)</pre>"
    r"\s*(?:</body>\s*)?(?:</html>\s*)?\Z",
    re.IGNORECASE | re.DOTALL,
)


def _unwrap_simple_pre(document: str) -> str:
    match = _PRE_WRAPPER.fullmatch(document)
    if match is None:
        return document
    body = match.group("body")
    if re.search(r"<[^>]+>", body):
        return document
    return html.unescape(body)


def _normalize_octets(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if (
            character == "%" and index + 2 < len(value)
            and re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 1:index + 3])
        ):
            octet = int(value[index + 1:index + 3], 16)
            if octet in _UNRESERVED:
                output.append(chr(octet))
            else:
                output.append(f"%{octet:02X}")
            index += 3
            continue
        codepoint = ord(character)
        if codepoint < 128:
            output.append(character)
        else:
            output.extend(f"%{octet:02X}" for octet in character.encode("utf-8"))
        index += 1
    return "".join(output)


def _specificity(pattern: str) -> int:
    value = pattern[:-1] if pattern.endswith("$") else pattern
    value = value.replace("*", "")
    count = 0
    index = 0
    while index < len(value):
        if (
            value[index] == "%" and index + 2 < len(value)
            and re.fullmatch(r"[0-9A-F]{2}", value[index + 1:index + 3])
        ):
            index += 3
        else:
            index += 1
        count += 1
    return count


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str
    specificity: int
    expression: re.Pattern[str]

    @classmethod
    def create(cls, *, allow: bool, pattern: str) -> "RobotsRule | None":
        normalized = _normalize_octets(pattern.strip())
        if not normalized:
            return None
        anchored = normalized.endswith("$")
        matching = normalized[:-1] if anchored else normalized
        expression = "^" + ".*".join(re.escape(part) for part in matching.split("*"))
        if anchored:
            expression += "$"
        return cls(allow, normalized, _specificity(normalized), re.compile(expression))

    def matches(self, path_query: str) -> bool:
        return self.expression.search(path_query) is not None


@dataclass(frozen=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[RobotsRule, ...]
    crawl_delays: tuple[float, ...]


@dataclass(frozen=True)
class RobotsPolicy:
    rules: tuple[RobotsRule, ...]
    crawl_delay_seconds: float | None = None
    selected_group: str = "NONE"

    @classmethod
    def parse(
        cls, document: str, *, product_token: str = PUBLICDB2_PRODUCT_TOKEN,
    ) -> "RobotsPolicy":
        groups: list[_Group] = []
        agents: list[str] = []
        rules: list[RobotsRule] = []
        delays: list[float] = []
        records_started = False

        def finish_group() -> None:
            nonlocal agents, rules, delays, records_started
            if agents:
                groups.append(_Group(tuple(agents), tuple(rules), tuple(delays)))
            agents, rules, delays = [], [], []
            records_started = False

        for raw_line in _unwrap_simple_pre(document).splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.casefold()
            if field == "user-agent":
                if agents and records_started:
                    finish_group()
                if value:
                    agents.append(value.casefold())
                continue
            if not agents:
                continue
            records_started = True
            if field in {"allow", "disallow"}:
                rule = RobotsRule.create(allow=field == "allow", pattern=value)
                if rule is not None:
                    rules.append(rule)
            elif field == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if 0 <= delay <= MAX_CRAWL_DELAY_SECONDS:
                    delays.append(delay)
        finish_group()

        token = product_token.casefold()
        selected = [group for group in groups if token in group.agents]
        selected_group = "EXACT"
        if not selected:
            selected = [group for group in groups if "*" in group.agents]
            selected_group = "WILDCARD" if selected else "NONE"
        selected_rules = tuple(rule for group in selected for rule in group.rules)
        selected_delays = [delay for group in selected for delay in group.crawl_delays]
        return cls(
            selected_rules,
            max(selected_delays) if selected_delays else None,
            selected_group,
        )

    def allows(self, target: str) -> bool:
        parsed = urlsplit(target)
        path_query = parsed.path or "/"
        if parsed.query:
            path_query += "?" + parsed.query
        normalized = _normalize_octets(path_query)
        matches = [rule for rule in self.rules if rule.matches(normalized)]
        if not matches:
            return True
        greatest = max(rule.specificity for rule in matches)
        return any(rule.allow for rule in matches if rule.specificity == greatest)
