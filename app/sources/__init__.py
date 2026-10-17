"""源适配器注册表。

cache 参数为可选：适配器若支持传入 PageCache 就用，不支持则忽略，
避免注册表与适配器签名不同步导致启动崩溃。
"""

from __future__ import annotations

import inspect
import logging

from .base import CandidateRejected, NotConfigured, SourceAdapter, SourceError
from .subhd import SubhdAdapter

log = logging.getLogger("subfindarr.sources")

__all__ = ["SourceAdapter", "SourceError", "NotConfigured", "CandidateRejected",
           "SubhdAdapter", "build_adapters"]


def build_adapters(cfg: dict, cache=None) -> list[SourceAdapter]:
    out: list[SourceAdapter] = []
    sources = cfg.get("sources", {}) or {}
    for name, sc in sources.items():
        if not (sc or {}).get("enabled"):
            continue
        if name == "subhd":
            out.append(_make(SubhdAdapter, cfg, cache))
        # assrt / opensubtitles 等兜底源按同一接口后续接入
    return out


def _make(cls, cfg: dict, cache):
    """适配器支持 cache 就传，不支持就按老签名构造。"""
    try:
        params = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        params = {}
    if "cache" in params:
        return cls(cfg, cache=cache)
    return cls(cfg)
