"""Loading and validating the TOML redaction config."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# Built-in patterns. Digit groups use lookarounds rather than \b so that a
# match never starts or ends in the middle of a longer number.
BUILTIN_PATTERNS: dict[str, str] = {
    # SSN or ITIN written with dashes or spaces: 123-45-6789, 123 45 6789
    "ssn": r"(?<!\d)\d{3}(?:[ ]*-[ ]*|[ ]+)\d{2}(?:[ ]*-[ ]*|[ ]+)\d{4}(?!\d)",
    # Employer ID number: 12-3456789
    "ein": r"(?<!\d)\d{2}[ ]*-[ ]*\d{7}(?!\d)",
    # Bare 9-digit run: an undashed SSN/EIN/ITIN or a bank routing number
    "nine_digit": r"(?<![\d,$])(?<!\d\.)\d{9}(?![\d,]|\.\d)",
    # Bank account and similar long numbers
    "account_number": r"(?<![\d,$])(?<!\d\.)\d{10,17}(?![\d,]|\.\d)",
    "phone": r"(?<![\d\w])(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]\d{4}(?!\d)",
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    # Digits spread across separate boxes, as in a form's SSN/EIN cells:
    # "1 2 0 0 2 5 0 0 0". Matches whole runs of digits joined only by
    # spaces, tabs, or dashes; _spaced_id keeps the runs of exactly 9.
    "spaced_digits": r"\d(?:[ \t-]*\d)*",
    # Dates: off by default, because tax forms are full of non-sensitive dates
    "date": r"(?<!\d)(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}(?!\d)",
}

DEFAULT_ENABLED = {
    "ssn": True,
    "ein": True,
    "nine_digit": True,
    "account_number": True,
    "phone": True,
    "email": True,
    "spaced_digits": True,
    "date": False,
}


def _spaced_id(m: re.Match) -> bool:
    if sum(c.isdigit() for c in m.group()) != 9:
        return False
    s, start, end = m.string, m.start(), m.end()
    if start and s[start - 1] in ",.$":  # tail of an amount like 1,234 567 890
        return False
    if end < len(s) and s[end] in ",." and s[end + 1:end + 2].isdigit():
        return False
    return True


# Extra checks a built-in pattern's matches must pass.
BUILTIN_FILTERS = {"spaced_digits": _spaced_id}


class ConfigError(Exception):
    pass


@dataclass
class Box:
    page: int  # 1-based, as the user sees it
    rect: tuple[float, float, float, float]


@dataclass
class Config:
    terms: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    patterns: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_ENABLED))
    custom: dict[str, str] = field(default_factory=dict)
    boxes: list[Box] = field(default_factory=list)

    def compiled_patterns(self) -> list[tuple[str, re.Pattern, Callable | None]]:
        """Return (kind, regex, filter) in matching order: terms, custom, built-ins."""
        out = []
        for term in self.terms:
            out.append(("term", term_regex(term), None))
        for name, rx in self.custom.items():
            out.append((name, re.compile(rx, re.IGNORECASE), None))
        for name, enabled in self.patterns.items():
            if enabled:
                out.append((name, re.compile(BUILTIN_PATTERNS[name]),
                            BUILTIN_FILTERS.get(name)))
        return out


def term_regex(term: str) -> re.Pattern:
    """Build a forgiving regex for a literal term.

    Case is ignored, any run of whitespace matches any other, and periods or
    commas after a word are optional, so "Jane Q. Doe" also matches "JANE Q DOE"
    and "Springfield, IL" matches "SPRINGFIELD IL".
    """
    tokens = [t.rstrip(".,") for t in term.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        raise ConfigError(f"empty term: {term!r}")
    body = r"[.,]?\s+".join(re.escape(t) for t in tokens)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def load_config(path: Path | None) -> Config:
    if path is None:
        return Config()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}")

    known = {"terms", "allow", "patterns", "custom", "boxes"}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"{path}: unknown keys: {', '.join(sorted(unknown))}")

    cfg = Config()
    cfg.terms = _str_list(data, "terms", path)
    cfg.allow = _str_list(data, "allow", path)

    for name, enabled in data.get("patterns", {}).items():
        if name not in BUILTIN_PATTERNS:
            raise ConfigError(
                f"{path}: unknown pattern {name!r}; "
                f"choose from {', '.join(BUILTIN_PATTERNS)}"
            )
        if not isinstance(enabled, bool):
            raise ConfigError(f"{path}: patterns.{name} must be true or false")
        cfg.patterns[name] = enabled

    for name, rx in data.get("custom", {}).items():
        if not isinstance(rx, str):
            raise ConfigError(f"{path}: custom.{name} must be a regex string")
        try:
            re.compile(rx)
        except re.error as e:
            raise ConfigError(f"{path}: custom.{name}: bad regex: {e}")
        cfg.custom[name] = rx

    for i, box in enumerate(data.get("boxes", [])):
        try:
            page = int(box["page"])
            x0, y0, x1, y1 = (float(v) for v in box["rect"])
        except (KeyError, TypeError, ValueError):
            raise ConfigError(
                f"{path}: boxes[{i}] needs page = N and rect = [x0, y0, x1, y1]"
            )
        if page < 1:
            raise ConfigError(f"{path}: boxes[{i}]: pages are numbered from 1")
        cfg.boxes.append(Box(page, (x0, y0, x1, y1)))

    return cfg


def _str_list(data: dict, key: str, path: Path) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{path}: {key} must be a list of strings")
    return [v for v in value if v.strip()]


def _toml_str(s: str) -> str:
    # A literal string keeps regex backslashes readable; fall back to a basic
    # string (JSON escapes are valid TOML) when it holds a quote or newline.
    if "'" not in s and "\n" not in s:
        return f"'{s}'"
    import json
    return json.dumps(s)


def dump_config(cfg: Config) -> str:
    """Serialize cfg as TOML that load_config reads back unchanged."""
    lines = [
        "# redacter config. This file lists your PII: keep it private",
        "# and out of version control.",
        "",
        "terms = [",
        *(f"    {_toml_str(t)}," for t in cfg.terms),
        "]",
        "",
        "allow = [",
        *(f"    {_toml_str(a)}," for a in cfg.allow),
        "]",
        "",
        "[patterns]",
        *(f"{name} = {'true' if on else 'false'}" for name, on in cfg.patterns.items()),
        "",
        "[custom]",
        *(f"{name} = {_toml_str(rx)}" for name, rx in cfg.custom.items()),
    ]
    for b in cfg.boxes:
        lines += ["", "[[boxes]]", f"page = {b.page}",
                  "rect = [" + ", ".join(f"{v:.1f}" for v in b.rect) + "]"]
    return "\n".join(lines) + "\n"


def save_config(cfg: Config, path: Path) -> None:
    """Write cfg to path with owner-only permissions, atomically."""
    import os
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(dump_config(cfg))
    os.replace(tmp, path)
