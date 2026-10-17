"""配置加载：YAML + 环境变量展开。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _logical(name: str) -> str:
    """YAML 的 y/n/on/off 之类在 1.1 里会被当成布尔，这里只取字符串。"""
    return name


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = _expand_env(raw)
    cfg.setdefault("server", {})
    cfg.setdefault("paths", {})
    cfg.setdefault("write", {})
    cfg.setdefault("naming", {})
    cfg.setdefault("sources", {})
    cfg.setdefault("scoring", {})
    cfg.setdefault("queue", {})
    cfg.setdefault("network", {})
    cfg.setdefault("cache", {})
    cfg.setdefault("flaresolverr", {})
    cfg["paths"].setdefault("media_root", "/media")
    cfg["paths"].setdefault("subs_out", "/subs-out")
    cfg["paths"].setdefault("cache", "/config/cache")
    cfg["paths"].setdefault("db", "/config/subfindarr.db")
    cfg["paths"].setdefault("log", "/logs/subfindarr.log")
    return cfg

