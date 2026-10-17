"""webhook 鉴权：Authorization: Bearer 校验（不依赖 Web 框架，便于离线测试）。

Chief 2026-10-16 定案：
- 不用 `?token=` 查询参数（arr 的 URL 会被日志留痕），改校验 `Authorization: Bearer <token>` 头。
- 是否强校验做成配置项 `server.webhook_require_token`，默认 true。
- token 为空且强校验开启时**一律拒绝**，避免「以为开了其实没配」。
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Mapping

log = logging.getLogger("subfindarr.auth")


def bearer_token(headers: Mapping[str, Any] | None) -> str | None:
    """从请求头取 Bearer token（scheme 大小写不敏感）。"""
    if not headers:
        return None
    raw = ""
    for k, v in headers.items():
        if str(k).lower() == "authorization":
            raw = str(v)
            break
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return raw.strip() or None


def authorized(
    server_cfg: Mapping[str, Any] | None,
    headers: Mapping[str, Any] | None = None,
    explicit_token: str | None = None,
) -> bool:
    """按配置判断请求是否放行。"""
    cfg = dict(server_cfg or {})
    require = bool(cfg.get("webhook_require_token", True))
    if not require:
        return True
    expected = str(cfg.get("webhook_token") or "")
    if not expected:
        log.warning("webhook 强校验已开启但 webhook_token 为空 → 拒绝请求")
        return False
    got = bearer_token(headers) or explicit_token
    if not got:
        return False
    # 定长比较，避免时序侧信道
    return hmac.compare_digest(str(got), expected)
