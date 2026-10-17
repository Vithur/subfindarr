"""HTTP 抓取层：直连优先 + 重试，代理兜底，FlareSolverr 仅作最后手段。

依据 Kernel 的 subhd 实测（`subhd-adapter-evidence.md`）：
- **CF 没有拦**：裸 UA 也 200；「时好时坏」是线路层（TLS 握手超时 / 连接被掐断）。
- 裸 HTTP 被 reset ⇒ 本模块**只走 HTTPS**。
- 代理不是首选：实测容器走代理访问 subhd 返回 502（sonarr 容器原始输出）。
- 真 CF challenge 的判别特征是 **HTTP 403 + `cf-mitigated: challenge`**，不是「任意失败」。

因此升级链为：直连 HTTPS（重试）→ 代理（重试）→ FlareSolverr。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger("subfindarr.fetch")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 真正代表 CF 挑战的响应头/特征（缺一不算）
CF_CHALLENGE_HEADERS = {"cf-mitigated": "challenge"}


def is_cf_challenge(status: int, headers: dict | None = None, body: str = "") -> bool:
    """只认 CF 的挑战特征，不把普通失败当风控。

    - 403 + `cf-mitigated: challenge`（最可靠）
    - 403 且响应体是 CF 挑战页
    - 200 但响应体是 CF 的 challenge 脚本（站点把挑战页当 200 返回的情况）

    单纯 503 / 超时 / 连接重置**不算**——Kernel 实测 subhd 的失败都是线路层。
    """
    h = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    low = (body or "")[:4000].lower()
    if h.get("cf-mitigated") == "challenge":
        return True
    if status == 403:
        return "cf-chl" in low or "challenge-platform" in low or "just a moment" in low
    if status == 200:
        return "cf-chl-opt" in low or "/cdn-cgi/challenge-platform/" in low
    return False


def looks_like_cf(status: int, body: str = "", headers: dict | None = None) -> bool:
    """兼容旧调用签名的别名，语义同 is_cf_challenge。"""
    return is_cf_challenge(status, headers, body)


def is_challenge(status: int, body: str = "", headers: dict | None = None) -> bool:
    """兼容旧调用签名的别名，语义同 is_cf_challenge。"""
    return is_cf_challenge(status, headers, body)


class FetchError(RuntimeError):
    """所有升级路径都失败。"""

    def __init__(self, url: str, stage: str, message: str, evidence: dict | None = None) -> None:
        super().__init__(f"[fetch/{stage}] {message} :: {url}")
        self.url = url
        self.stage = stage
        self.evidence = evidence or {}


def check_zip_complete(path: str, expected_len: int | None = None) -> tuple[bool, str]:
    """校验 zip 完整性：长度一致 + EOCD 存在 + 中央目录可解析。

    截断的 zip 会有完好的 PK 头但缺 EOCD —— 这种静默损坏必须挡住，
    否则半截字幕会直接落进媒体库。
    """
    import os
    import struct
    import zipfile

    p = Path(path)
    if not p.exists():
        return False, "文件不存在"
    size = os.path.getsize(p)
    if expected_len is not None and expected_len > 0 and size != expected_len:
        return False, f"长度不符: 实收 {size} / Content-Length {expected_len}"

    if size < 22:
        return False, f"文件过小({size}B)，不可能是完整 zip"
    with p.open("rb") as f:
        tail = f.read()
    idx = tail.rfind(b"PK\x05\x06")
    if idx < 0:
        return False, "缺 EOCD（PK\\x05\\x06 签名），zip 被截断"
    if len(tail) - idx < 22:
        return False, "EOCD 记录不完整"
    cd_size, cd_offset = struct.unpack("<II", tail[idx + 12: idx + 20])
    eocd_at = idx
    cd_end = cd_offset + cd_size
    # Kernel 实测判据：EOCD 位置必须等于「中央目录结束偏移」，
    # 且该偏移必须落在文件内 —— 只判 > 会放过「尾部多挂了东西」的变体。
    # 注意：zip 允许注释尾（EOCD 后可有 comment），所以比较 EOCD 起点与 cd_end。
    if cd_end > size:
        return False, f"中央目录越界: offset+size={cd_end} > 文件 {size}"
    if eocd_at != cd_end:
        return False, (f"EOCD 起点({eocd_at}) != 中央目录结束({cd_end})，"
                       f"尾部落差异 {eocd_at - cd_end}B，zip 不完整（截断或拼接）")
    # 尾部校验：zip 规范允许 EOCD 后跟 n 字节注释，n 写在 EOCD 里。
    # 「EOCD 记录(22) + 注释长度(n)」必须正好等于文件剩余长度，
    # 否则尾部挂了多余字节（实测：完整 zip + b"JUNKJUNK" 会被前面的判据放行）。
    comment_len = struct.unpack("<H", tail[idx + 20: idx + 22])[0]
    if idx + 22 + comment_len != size:
        extra = size - (idx + 22 + comment_len)
        return False, (f"尾部多余 {extra}B（EOCD@{idx} + 记录22 + 注释{comment_len} "
                       f"!= 文件 {size}），zip 被拼接或截断")
    try:
        with zipfile.ZipFile(p) as z:
            bad = z.testzip()
            if bad is not None:
                return False, f"中央目录校验失败: {bad}"
    except zipfile.BadZipFile as e:
        return False, f"BadZipFile: {e}"
    return True, "ok"


def check_rar_complete(path: str, expected_len: int | None = None) -> tuple[bool, str]:
    """校验 rar 完整性：长度一致 + 结尾有 RAR 结束块签名。

    rar 的结束标志是末尾的 `\\xc4\\x3d\\x7b\\x00`（RAR4/5 通用尾标记）。
    截断的 rar 缺这个尾巴；长度已知时优先用长度判。
    """
    import os

    p = Path(path)
    if not p.exists():
        return False, "文件不存在"
    size = os.path.getsize(p)
    if expected_len is not None and expected_len > 0 and size != expected_len:
        return False, f"长度不符: 实收 {size} / Content-Length {expected_len}"
    if size < 32:
        return False, f"文件过小({size}B)，不可能是完整 rar"
    with p.open("rb") as f:
        f.seek(max(0, size - 64))
        tail = f.read()
    if b"\xc4\x3d\x7b\x00" in tail:
        return True, "ok"
    # rar5 允许无该标记；长度已对上就放行，长度未知则保守拒绝
    if expected_len is not None and expected_len > 0:
        return True, "ok（无结束标记但长度一致）"
    return False, "缺 rar 结束标记，文件疑似被截断"


def check_7z_complete(path: str, expected_len: int | None = None) -> tuple[bool, str]:
    """校验 7z 完整性：长度一致 + 结尾是 7z 的 next-header 结束标记 0x00。"""
    import os

    p = Path(path)
    if not p.exists():
        return False, "文件不存在"
    size = os.path.getsize(p)
    if expected_len is not None and expected_len > 0 and size != expected_len:
        return False, f"长度不符: 实收 {size} / Content-Length {expected_len}"
    if size < 32:
        return False, f"文件过小({size}B)，不可能是完整 7z"
    with p.open("rb") as f:
        f.seek(size - 1)
        last = f.read(1)
    if last == b"\x00":
        return True, "ok"
    return False, "7z 结尾标记缺失，文件被截断"


def check_download_complete(path: str, kind: str | None, expected_len: int | None = None,
                            content_type: str | None = None) -> tuple[bool, str]:
    """按**实际格式**校验完整性。

    踩过的坑：只按 zip 的 EOCD 校验会把 rar/7z 全判成截断
    （真机实测 dl.subhd.me 给的 `.rar` 被误判）——必须按 magic 分派。
    """
    import os

    lk = (kind or "").lower()
    ct = (content_type or "").lower()
    if lk in ("zip", "zip-empty"):
        return check_zip_complete(path, expected_len)
    if lk == "rar":
        return check_rar_complete(path, expected_len)
    if lk == "7z":
        return check_7z_complete(path, expected_len)

    # 类型未知：读头部 magic 自己判
    head = b""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False, "文件不可读"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return check_zip_complete(path, expected_len)
    if head.startswith(b"Rar!\x1a\x07"):
        return check_rar_complete(path, expected_len)
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return check_7z_complete(path, expected_len)
    if "zip" in ct:
        return check_zip_complete(path, expected_len)
    # 裸字幕等：只比长度
    size = os.path.getsize(path) if os.path.exists(path) else 0
    if expected_len is not None and expected_len > 0 and size != expected_len:
        return False, f"长度不符: 实收 {size} / Content-Length {expected_len}"
    return True, "ok"


@dataclass
class FetchTrace:
    """一次请求的升级过程，便于日志与验收取证。"""

    attempts: list[dict] = field(default_factory=list)

    def add(self, stage: str, n: int, ok: bool, detail: str = "") -> None:
        self.attempts.append({"stage": stage, "n": n, "ok": ok, "detail": detail[:200]})

    @property
    def stages(self) -> str:
        return "→".join(f"{a['stage']}{'✓' if a['ok'] else '✗'}" for a in self.attempts)


class Fetcher:
    """带 cookie 会话的抓取器：直连重试 → 代理重试 → FlareSolverr。"""

    def __init__(
        self,
        *,
        fs_url: str = "",
        fs_timeout_ms: int = 90000,
        cookies: dict[str, str] | None = None,
        proxy: str | None = None,
        timeout: float = 45.0,
        retries: int = 3,
        backoff: float = 1.5,
        use_proxy: bool = True,
        fs_enabled: bool = True,
    ) -> None:
        self.fs_url = (fs_url or "").rstrip("/")
        self.fs_timeout_ms = fs_timeout_ms
        self.timeout = timeout
        self.retries = max(1, retries)
        self.backoff = max(0.0, backoff)
        self.cookies = dict(cookies or {})
        # 直连客户端：只走 HTTPS，出错重试
        self._direct = httpx.Client(
            headers=dict(DEFAULT_HEADERS),
            cookies=self.cookies,
            follow_redirects=True,
            timeout=timeout,
            verify=True,
        )
        # 代理客户端：仅在直连全部失败后使用（实测直连 subhd 走代理会 502，故不是首选）
        self._proxied = None
        if use_proxy and proxy:
            self._proxied = httpx.Client(
                headers=dict(DEFAULT_HEADERS),
                cookies=self.cookies,
                follow_redirects=True,
                timeout=timeout,
                proxy=proxy,
            )
        self._fs_enabled = bool(fs_enabled and self.fs_url)
        self._fs_client = httpx.Client(timeout=max(timeout, fs_timeout_ms / 1000 + 20))
        self.trace = FetchTrace()

    # ---------- 基础请求 ----------

    def _sleep(self, n: int) -> None:
        if self.backoff <= 0:
            return
        # 指数退避 + 抖动，避开同一时刻重试撞在同一段坏链路上
        delay = self.backoff ** n + random.uniform(0, 0.4)
        time.sleep(min(delay, 12.0))

    def _try(self, client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
        if method == "POST":
            kw = {k: v for k, v in kw.items() if v is not None}
            return client.post(url, **kw)
        kw.pop("json", None)
        kw.pop("data", None)
        return client.get(url, **kw)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json_body: dict | None = None,
        data: dict | None = None,
        allow_fs: bool = True,
    ) -> httpx.Response:
        """按升级链请求；返回状态码 < 400 的响应，或抛 FetchError。"""
        if url.lower().startswith("http://"):
            raise FetchError(url, "scheme", "裸 HTTP 会被站点重置，只允许 HTTPS", {"url": url})

        last: str = "未尝试"
        cf_seen = False

        for stage, client in self._clients():
            for n in range(self.retries):
                try:
                    r = self._try(client, method, url, headers=headers, json=json_body, data=data)
                except httpx.HTTPError as e:
                    last = f"{type(e).__name__}: {e}"
                    self.trace.add(stage, n + 1, False, last)
                    log.warning("%s 第 %d 次失败(%s): %s", stage, n + 1, e.__class__.__name__, url)
                    self._sleep(n)
                    continue
                if is_cf_challenge(r.status_code, dict(r.headers), r.text[:4000]):
                    cf_seen = True
                    last = f"CF challenge status={r.status_code}"
                    self.trace.add(stage, n + 1, False, last)
                    log.warning("%s 命中 CF challenge，停用该路径: %s", stage, url)
                    break  # 换更高一级的路径（代理 → FlareSolverr），同路径重试无意义
                if r.status_code >= 500:
                    last = f"status={r.status_code}"
                    self.trace.add(stage, n + 1, False, last)
                    self._sleep(n)
                    continue
                self.trace.add(stage, n + 1, True, f"status={r.status_code}")
                return r
            if cf_seen:
                break  # 直连被 CF 挑战 → 不再试代理（IP 变更通常也不能过），直接 FlareSolverr

        if (cf_seen or True) and allow_fs and self._fs_enabled:
            r = self._via_flaresolverr(method, url, json_body=json_body)
            if r is not None:
                self.trace.add("fs", 1, True, f"status={r.status_code}")
                return r
            self.trace.add("fs", 1, False, "FlareSolverr 失败")

        raise FetchError(url, "all", f"所有路径失败: {last}", {"trace": self.trace.attempts})

    def _clients(self) -> list[tuple[str, httpx.Client]]:
        out = [("direct", self._direct)]
        if self._proxied is not None:
            out.append(("proxy", self._proxied))
        return out

    def get(self, url: str, *, headers: dict | None = None, allow_fs: bool = True) -> httpx.Response:
        return self.request("GET", url, headers=headers, allow_fs=allow_fs)

    def post_json(self, url: str, payload: dict, *, headers: dict | None = None,
                  allow_fs: bool = False) -> httpx.Response:
        h = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
        h.update(headers or {})
        return self.request("POST", url, headers=h, json_body=payload, allow_fs=allow_fs)

    def post_form(self, url: str, data: dict, *, headers: dict | None = None,
                  allow_fs: bool = False) -> httpx.Response:
        return self.request("POST", url, headers=headers, data=data, allow_fs=allow_fs)

    # ---------- FlareSolverr ----------

    def _via_flaresolverr(self, method: str, url: str, *, json_body: dict | None = None) -> httpx.Response | None:
        if not self._fs_enabled:
            return None
        cmd = "request.post" if method == "POST" else "request.get"
        payload: dict = {"cmd": cmd, "url": url, "maxTimeout": self.fs_timeout_ms}
        if method == "POST":
            payload["postData"] = _to_form(json_body)
        if self.cookies:
            payload["cookies"] = [
                {"name": k, "value": v, "domain": _domain_of(url)} for k, v in self.cookies.items()
            ]
        try:
            resp = self._fs_client.post(f"{self.fs_url}/v1", json=payload)
            data = resp.json()
        except Exception as e:  # noqa: BLE001 - FS 不可用不能拖垮整条链
            log.error("FlareSolverr 调用失败: %s", e)
            return None
        if data.get("status") != "ok":
            log.error("FlareSolverr 返回非 ok: %s", str(data)[:300])
            return None
        sol = data.get("solution") or {}
        body = sol.get("response") or ""
        status = int(sol.get("status") or 200)
        req = httpx.Request(method, sol.get("url") or url)
        # 回填 FS 解出的 cookie，后续直连可用
        for c in sol.get("cookies") or []:
            try:
                self._direct.cookies.set(c.get("name"), c.get("value"), domain=c.get("domain"))
            except Exception:  # noqa: BLE001
                pass
        return httpx.Response(
            status,
            content=body.encode("utf-8", "replace"),
            headers={"content-type": "text/html; charset=utf-8", "x-fetch-path": "flaresolverr"},
            request=req,
        )

    # ---------- 二进制 ----------

    def download_file(
        self,
        url: str,
        dest_path: str,
        *,
        referer: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        kind: str | None = None,
        max_resume_rounds: int = 6,
        resume_budget_sec: float = 900.0,
    ) -> tuple[int, bytes, str, str | None]:
        """下载到 dest_path 并**校验完整性**，返回 (字节数, 前 16 字节, 最终 URL, Content-Type)。

        关键点（Chief 2026-10-16 定，Kernel 实测 CDN 会中途掐断）：
        - HTTP 200 不代表文件完整：截断的 zip 有完好 PK 头、缺 EOCD。
        - 拿到 Content-Length 后比对实收字节数；再校验 zip 的 EOCD 与中央目录。
        - 不完整时用 `Range` 断点续传补齐，续完**重新校验**。
        - 续传有轮数与总时长上限；到顶仍不完整 → 抛 FetchError（**绝不解包半截文件**）。
        """
        base_headers = {"Referer": referer} if referer else {}
        attempts = retries if retries is not None else self.retries
        to = timeout if timeout is not None else self.timeout * 6
        last: str = ""
        deadline = time.time() + max(60.0, resume_budget_sec)
        resume_rounds = 0

        for stage, client in self._clients():
            for n in range(attempts):
                # 每轮重新读已落盘大小，用于 Range 续传
                have = 0
                try:
                    have = os.path.getsize(dest_path)
                except OSError:
                    have = 0
                req_headers = dict(base_headers)
                mode = "wb"
                if have > 0:
                    req_headers["Range"] = f"bytes={have}-"
                    mode = "ab"
                try:
                    with client.stream("GET", url, headers=req_headers, timeout=to) as r:
                        if r.status_code >= 400:
                            last = f"status={r.status_code}"
                            self.trace.add(f"dl:{stage}", n + 1, False, last)
                            if is_cf_challenge(r.status_code, dict(r.headers)):
                                break
                            self._sleep(n)
                            continue
                        # 服务端不支持 Range 时会回 200 并从头给，此时必须重写而不是追加
                        if have > 0 and r.status_code == 200:
                            mode = "wb"
                            have = 0
                        ct = r.headers.get("content-type")
                        clen = r.headers.get("content-length")
                        expected = None
                        if clen and clen.isdigit():
                            expected = int(clen) + (have if r.status_code == 206 else 0)
                        head = b""
                        n_written = 0
                        with open(dest_path, mode) as f:
                            for chunk in r.iter_bytes():
                                if len(head) < 16:
                                    head += chunk[: 16 - len(head)]
                                f.write(chunk)
                                n_written += len(chunk)
                        total = have + n_written
                        self.trace.add(f"dl:{stage}", n + 1, True,
                                       f"+{n_written}B total={total}B expected={expected}")
                except httpx.HTTPError as e:
                    last = f"{type(e).__name__}: {e}（已写入 {have}B）"
                    self.trace.add(f"dl:{stage}", n + 1, False, last)
                    log.warning("下载第 %d 次失败(%s): %s", n + 1, e.__class__.__name__, url)
                    self._sleep(n)
                    continue

                # ---- 完整性校验 ----
                total = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
                ok, why = check_download_complete(dest_path, kind,
                                                  expected if expected else None, ct)
                if ok:
                    with open(dest_path, "rb") as f:
                        head = f.read(16)
                    return total, head, url, ct

                last = f"下载不完整: {why}"
                self.trace.add(f"dl:{stage}", n + 1, False, last)
                if time.time() > deadline or resume_rounds >= max_resume_rounds:
                    log.warning("续传到顶（轮 %d，%s），放弃该候选: %s",
                                resume_rounds, why, url)
                    break
                resume_rounds += 1
                log.warning("下载不完整（%s），第 %d 轮续传: %s", why, resume_rounds, url)
                self._sleep(0)

        raise FetchError(url, "download",
                         f"下载失败/不完整（续传 {resume_rounds} 轮）: {last}",
                         {"trace": self.trace.attempts, "resumed": resume_rounds})

    # ---------- cookie 同步 ----------

    def sync_cookies_from(self, response: httpx.Response) -> None:
        """把某次响应种下的 cookie 同步到所有客户端（三跳链路必须连续带上）。"""
        for c in response.cookies.jar:
            for client in self._all_clients():
                try:
                    client.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
                except Exception:  # noqa: BLE001
                    pass

    def cookie_header(self) -> str:
        seen: dict[str, str] = {}
        for client in self._all_clients():
            for c in client.cookies.jar:
                seen.setdefault(c.name, c.value)
        return "; ".join(f"{k}={v}" for k, v in seen.items())

    def _all_clients(self) -> list[httpx.Client]:
        out = [self._direct]
        if self._proxied is not None:
            out.append(self._proxied)
        return out

    def close(self) -> None:
        self._direct.close()
        if self._proxied is not None:
            self._proxied.close()
        self._fs_client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------- 工具 ----------


def _to_form(payload: dict | None) -> str:
    from urllib.parse import urlencode

    return urlencode(payload or {})


def _domain_of(url: str) -> str:
    try:
        return httpx.URL(url).host
    except Exception:  # noqa: BLE001
        return ""


def parse_cookie_header(raw: str) -> dict[str, str]:
    """把浏览器复制的 `k=v; k2=v2` Cookie 头解析成 dict。"""
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out
