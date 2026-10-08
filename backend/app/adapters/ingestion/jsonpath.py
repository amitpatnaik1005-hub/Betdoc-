"""A JSONPath subset for declarative provider mappings (no third-party dependency).

Supported syntax::

    $                   the document root (optional; a path without it is relative to the node
                        it is evaluated on, which is how mappings walk event -> book -> outcome)
    .name  ['name']     child member (bracket form allows any characters, e.g. ['1x2'])
    [3]  [-1]           list index
    [*]  .*             every element of a list, or every value of an object
    [?(@.a.b == 'x')]   filter list elements (or object values) by a field: ==, != against a
                        quoted string, a number, true, false or null; string comparison ignores case

Examples: ``$.data.events[*]``, ``participants[?(@.side == 'home')].name``, ``markets['1x2']``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

__all__ = ["JsonPathError", "compile_path", "first", "select"]

_NAME = re.compile(r"[A-Za-z0-9_\-]+")
_FILTER = re.compile(r"^@((?:\.[A-Za-z0-9_\-]+)+)\s*(==|!=)\s*(.+)$")


class JsonPathError(ValueError):
    """The expression is not valid in the supported subset."""


@dataclass(frozen=True, slots=True)
class _Field:
    name: str


@dataclass(frozen=True, slots=True)
class _Index:
    index: int


@dataclass(frozen=True, slots=True)
class _Wildcard:
    pass


@dataclass(frozen=True, slots=True)
class _Filter:
    path: tuple[str, ...]
    negate: bool
    value: Any


_Step = _Field | _Index | _Wildcard | _Filter


def _literal(raw: str, expression: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered == "null":
        return None
    try:
        return float(raw)
    except ValueError:
        raise JsonPathError(f"Unsupported filter value {raw!r} in {expression!r}") from None


@lru_cache(maxsize=1024)
def compile_path(expression: str) -> tuple[_Step, ...]:
    text = expression.strip()
    if not text:
        raise JsonPathError("Empty path")
    steps: list[_Step] = []
    i = 0
    if text[0] == "$":
        i = 1
    elif text[0] not in ".[":
        match = _NAME.match(text)  # relative path starting with a bare member name
        if not match:
            raise JsonPathError(f"Cannot parse {expression!r}")
        steps.append(_Field(match.group()))
        i = match.end()
    while i < len(text):
        char = text[i]
        if char == ".":
            if text.startswith(".*", i):
                steps.append(_Wildcard())
                i += 2
                continue
            match = _NAME.match(text, i + 1)
            if not match:
                raise JsonPathError(f"Expected a member name at position {i + 1} of {expression!r}")
            steps.append(_Field(match.group()))
            i = match.end()
        elif char == "[":
            close = text.find("]", i)
            if close == -1:
                raise JsonPathError(f"Unclosed '[' in {expression!r}")
            inner = text[i + 1 : close].strip()
            if inner.startswith("?("):
                close = text.find(")]", i)
                if close == -1:
                    raise JsonPathError(f"Unclosed filter in {expression!r}")
                match = _FILTER.match(text[i + 3 : close].strip())
                if not match:
                    raise JsonPathError(f"Unsupported filter in {expression!r} (use [?(@.field == 'value')])")
                path = tuple(p for p in match.group(1).split(".") if p)
                steps.append(_Filter(path, match.group(2) == "!=", _literal(match.group(3), expression)))
                i = close + 2
                continue
            if inner == "*":
                steps.append(_Wildcard())
            elif len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
                steps.append(_Field(inner[1:-1]))
            else:
                try:
                    steps.append(_Index(int(inner)))
                except ValueError:
                    raise JsonPathError(f"Unsupported selector [{inner}] in {expression!r}") from None
            i = close + 1
        else:
            raise JsonPathError(f"Unexpected {char!r} at position {i} of {expression!r}")
    return tuple(steps)


def _children(node: Any) -> list[Any]:
    if isinstance(node, Mapping):
        return list(node.values())
    if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        return list(node)
    return []


def _matches(item: Any, step: _Filter) -> bool:
    current = item
    for part in step.path:
        if not isinstance(current, Mapping) or part not in current:
            return step.negate  # a missing field is "not equal"
        current = current[part]
    if isinstance(current, str) and isinstance(step.value, str):
        equal = current.casefold() == step.value.casefold()
    elif isinstance(step.value, float) and isinstance(current, (int, float)) and not isinstance(current, bool):
        equal = float(current) == step.value
    else:
        equal = current == step.value
    return equal != step.negate


def select(document: Any, expression: str) -> list[Any]:
    """Every node the path reaches (possibly none)."""
    nodes: list[Any] = [document]
    for step in compile_path(expression):
        found: list[Any] = []
        for node in nodes:
            if isinstance(step, _Field):
                if isinstance(node, Mapping) and step.name in node:
                    found.append(node[step.name])
            elif isinstance(step, _Index):
                if isinstance(node, Sequence) and not isinstance(node, (str, bytes)) and -len(node) <= step.index < len(node):
                    found.append(node[step.index])
            elif isinstance(step, _Wildcard):
                found.extend(_children(node))
            else:
                found.extend(item for item in _children(node) if _matches(item, step))
        nodes = found
        if not nodes:
            break
    return nodes


def first(document: Any, expression: str, default: Any = None) -> Any:
    found = select(document, expression)
    return found[0] if found else default
