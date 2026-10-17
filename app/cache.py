"""简单的响应缓存：减少对字幕站的重复请求（站点链路本来就慢）。

用途有二：
1. 同一轮任务里 /search 与 /d/{id} 可能被多个候选复用；
2. 重跑/幂等时不必再打站点。
只缓存页面文本，带 TTL，落盘到可写的 subs_out 下，容器重建不丢。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("subfindarr.cache")


class PageCache:
    def __init__(self, root: str | Path, ttl_sec: int = 6 * 3600, enabled: bool = True) -> None:
        self.root = Path(root)
        self.ttl = max(0, int(ttl_sec))
        self.enabled = bool(enabled and self.ttl > 0)
        if self.enabled:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                log.warning("缓存目录不可写(%s)，缓存关闭", e)
                self.enabled = False

    def _path(self, key: str) -> Path:
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.root / f"{h}.json"

    def get(self, key: str) -> str | None:
        if not self.enabled:
            return None
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self.ttl and time.time() - float(data.get("ts", 0)) > self.ttl:
            return None
        return data.get("body")

    def put(self, key: str, body: str) -> None:
        if not self.enabled:
            return
        try:
            self._path(key).write_text(
                json.dumps({"ts": time.time(), "key": key, "body": body}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning("缓存写入失败: %s", e)

    def clear(self) -> int:
        if not self.root.exists():
            return 0
        n = 0
        for p in self.root.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        return n

    def stats(self) -> dict:
        if not self.root.exists():
            return {"enabled": self.enabled, "files": 0}
        files = list(self.root.glob("*.json"))
        return {"enabled": self.enabled, "files": len(files), "ttl_sec": self.ttl}

