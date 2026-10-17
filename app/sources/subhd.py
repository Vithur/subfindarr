"""subhd.tv 适配器（按 Kernel 2026-10-16 实测协议实现）。

实测要点（原始报告：workspace/subhd-adapter-evidence.md）：

- **CF 并没有真拦**：裸 UA 直连多为 200；时好时坏是线路层（超时/被掐断）。
  真挑战特征＝HTTP 403 + `cf-mitigated: challenge`。因此直连 HTTPS + 重试优先，
  FlareSolverr 只是兜底。
- **必须 HTTPS**：裸 HTTP 会被 reset。
- **页面 URL 规则**：搜索 `/search/{kw}`、影视页 `/d/{id}`、版本页 `/a/{sid}`。
  sid 取自版本页内 `data-sid` 属性。
- **下载三跳**：
  1. `POST /api/sub/prepare-download` body `{"sid": ...}` → Set-Cookie `tk_*`
  2. `GET /down/{sid}` → Set-Cookie `down_*`（Path=/api/sub/down）
  3. `POST /api/sub/down` body `{"sid": ...}` 带 `down_*` → 返回 `dl.subhd.me` zip 直链
  cookie 约 300s 有效，必须复用同一会话。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import httpx

from ..fetcher import Fetcher, looks_like_cf, parse_cookie_header
from .base import (CandidateRejected, DownloadResult, MediaQuery, NotConfigured,
                   SourceError, SubtitleCandidate)

log = logging.getLogger("subfindarr.subhd")

BASE = "https://subhd.tv"

RE_SID = re.compile(r"data-sid=[\"']?(\w+)")
# 实测：结果区链接用单引号，且 /a/{sid} 里 sid 是字母数字串（如 L5ogTp），不是纯数字
RE_A_LINK = re.compile(r"href=['\"](/a/(\w+))['\"][^>]*>(.*?)</a>", re.S | re.I)
RE_D_LINK = re.compile(r"href=['\"](/d/(\d+))['\"][^>]*>(.*?)</a>", re.S | re.I)
RE_STRIP = re.compile(r"<[^>]+>")
RE_ZIP_URL = re.compile(r"https?://[^\s\"'<>]*\.(?:zip|rar|7z)(?:\?[^\s\"'<>]*)?", re.I)
RE_ANY_URL = re.compile(r"https?://[^\s\"'<>]+")


class SubhdAdapter:
    name = "subhd"

    def __init__(self, cfg: dict) -> None:
        sc = (cfg.get("sources", {}) or {}).get("subhd", {}) or {}
        self.cfg = cfg
        self.sc = sc
        self.base = (sc.get("base") or BASE).rstrip("/")
        self.cookie_raw = sc.get("cookie") or ""
        fs = cfg.get("flaresolverr", {}) or {}
        self.fs_url = fs.get("url", "")
        self.fs_timeout_ms = int(fs.get("max_timeout_ms", 90000))
        self.direct_first = bool(fs.get("direct_first", True))
        self.retries = int(sc.get("retries", 3))
        self.retry_delay = float(sc.get("retry_delay_sec", 1.5))
        # 三跳重试退避更长：同 sid 短时频控会连续 403（见 _hop 注释）
        self.hop_backoff = float(sc.get("hop_backoff_sec", 4.0))
        self.configured = True
        if not self.cookie_raw:
            log.warning("subhd 未配置 cookie，公开页可能受限（不阻断启动）")

    # ---- 会话 ----

    def new_client(self) -> httpx.Client:
        """三跳链必须同一会话复用 cookie。"""
        c = httpx.Client(
            base_url=self.base,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": self.base + "/",
            },
            cookies=parse_cookie_header(self.cookie_raw),
            follow_redirects=True,
            timeout=30.0,
        )
        return c

    def _get(self, client: httpx.Client, url: str, **kw) -> httpx.Response:
        """直连 HTTPS + 重试；真 CF 挑战才回退 FlareSolverr。"""
        last: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                r = client.get(url, **kw)
                if looks_like_cf(r.status_code, r.text, dict(r.headers)):
                    log.info("subhd 真 CF 挑战（%s），回退 FlareSolverr", r.status_code)
                    fs = self._via_flaresolverr(url)
                    if fs is not None:
                        return fs
                # 4xx/5xx 重试一次再放弃（线路层抖动）
                if r.status_code >= 500 and attempt < self.retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                return r
            except httpx.HTTPError as e:
                last = e
                log.warning("subhd 第 %d 次请求失败: %s", attempt, e)
                time.sleep(self.retry_delay * attempt)
        if self.fs_url:
            fs = self._via_flaresolverr(url)
            if fs is not None:
                return fs
        raise SourceError(self.name, "http", f"请求反复失败: {last}", {"url": url})

    def _via_flaresolverr(self, url: str) -> httpx.Response | None:
        if not self.fs_url:
            return None
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": self.fs_timeout_ms,
            "cookies": [
                {"name": k, "value": v, "domain": ".subhd.tv"}
                for k, v in parse_cookie_header(self.cookie_raw).items()
            ],
        }
        try:
            resp = httpx.post(f"{self.fs_url}/v1", json=payload, timeout=self.fs_timeout_ms / 1000 + 20)
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            log.error("FlareSolverr 不可用: %s", e)
            return None
        if data.get("status") != "ok":
            log.error("FlareSolverr 返回非 ok: %s", str(data)[:200])
            return None
        sol = data.get("solution") or {}
        req = httpx.Request("GET", sol.get("url") or url)
        return httpx.Response(
            int(sol.get("status") or 200),
            content=(sol.get("response") or "").encode("utf-8", "replace"),
            headers={"content-type": "text/html; charset=utf-8"},
            request=req,
        )

    # ---- 三段式 ----

    def search(self, query: MediaQuery) -> list[SubtitleCandidate]:
        """按搜索词逐个试；站点对「标题 + 年份」这类长串支持不好，先短名后长名。

        实测：`/search/Josee, the Tiger and the Fish 2020` 返回 0 条，
        而 `/search/Josee` 有 8 条 —— 所以用短化词优先。
        """
        terms = _search_terms(query)
        out: list[SubtitleCandidate] = []
        with self.new_client() as client:
            for term in terms:
                url = f"{self.base}/search/{_urlquote(term)}"
                r = self._get(client, url)
                if r.status_code != 200:
                    log.warning("检索 %s 返回 %s", term, r.status_code)
                    continue
                got = self._parse_list(r.text, query)
                if got:
                    log.info("检索 '%s' 命中 %d 条", term, len(got))
                    out = got
                    break
        return out

    def _parse_list(self, html: str, query: MediaQuery) -> list[SubtitleCandidate]:
        """解析结果列表。

        实测 DOM（2026-10-16，NAS 容器内抓取 /search/Josee）：
        - 结果条目容器：`<div class="bg-white shadow-sm rounded-3 mb-4">`
        - 片名链接：容器内 `class="float-start f16 fw-bold"` 的 `<a href="/d/{id}">`
        - 版本页链接：`<a href="/a/{sid}">`
        只取「的搜索结果」之后的片段，避免抓到首页推荐位。
        """
        scope = html
        idx = html.find("的搜索结果")
        if idx > 0:
            scope = html[idx:]
        else:
            # 没有结果提示时，至少避开导航区
            nav = html.find("navbar")
            if nav > 0:
                scope = html[nav:]

        cands: list[SubtitleCandidate] = []
        # 实测结构（2026-10-16，容器内抓 /search/Josee）：
        #   结果块内先出现影视页链接 <a href='/d/{数字}'><img alt="片名 原名">
        #   再出现版本页链接 <a href='/a/{sid}'>字幕标题</a>（sid 为字母数字）
        #   语言/格式标签在条目块内 <span class="p-1 fw-bold">简体</span> / <span ...>ASS</span>
        # 单引号是实测形态，正则两种都收。
        for m in re.finditer(
            r"<a href=['\"]/d/(\d+)['\"][^>]*>\s*<div class=\"pics\">\s*<img[^>]*alt=\"([^\"]+)\"",
            scope, re.S | re.I,
        ):
            title = m.group(2).strip()
            if title:
                cands.append(SubtitleCandidate(
                    source=self.name, source_id=m.group(1), title=title,
                    page_url=self.base + f"/d/{m.group(1)}", raw={"kind": "title"},
                ))

        # 版本页：每条 entry 块切成一段，便于把该条的语言标签/格式/标题绑定起来
        for m in re.finditer(
            r"<a href=['\"](/a/(\w+))['\"][^>]*>(.*?)</a>(.*?)(?=<a href=['\"]/a/\w+|$)",
            scope, re.S | re.I,
        ):
            sid = m.group(2)
            title = _clean(m.group(3))
            if not title:
                continue
            block = m.group(4)[:1500]
            cands.append(SubtitleCandidate(
                source=self.name, source_id=sid, title=title,
                page_url=self.base + m.group(1),
                lang_hint=_tags_of(block),
                fmt=_fmt_of(block),
                downloads=_downloads_of(block),
                raw={"kind": "version"},
            ))

        if not cands:
            for m in RE_D_LINK.finditer(scope):
                title = _clean(m.group(3))
                if title:
                    cands.append(SubtitleCandidate(
                        source=self.name, source_id=m.group(2), title=title,
                        page_url=self.base + m.group(1), raw={"kind": "title"},
                    ))

        if idx > 0:
            # 结果页可信：不做标题二次匹配，避免因中英文名差异漏掉
            return _dedup(cands)
        return _dedup([c for c in cands if _plausible(c.title, query)])

    def resolve(self, candidate: SubtitleCandidate) -> SubtitleCandidate:
        """拿到真实的 dl.subhd.me 直链。

        版本页（/a/{sid}）直接进三跳；影视页（/d/{id}）先列出该片所有版本页再取第一个。
        """
        with self.new_client() as client:
            page = candidate
            if candidate.raw.get("kind") == "title":
                r = self._get(client, candidate.page_url)
                if r.status_code != 200:
                    raise SourceError(self.name, "resolve", f"影视页 {r.status_code}",
                                      {"url": candidate.page_url})
                versions = self._parse_list(r.text, MediaQuery(media_type="movie", title=candidate.title))
                versions = [v for v in versions if v.raw.get("kind") == "version"]
                if not versions:
                    raise SourceError(self.name, "resolve", "影视页下无版本页",
                                      {"url": candidate.page_url})
                page = versions[0]
                page.title = page.title or candidate.title

            r = self._get(client, page.page_url)
            if r.status_code != 200:
                raise SourceError(self.name, "resolve", f"版本页 {r.status_code}",
                                  {"url": page.page_url})
            html = r.text
            m = RE_SID.search(html)
            if not m:
                raise SourceError(self.name, "resolve", "版本页未找到 data-sid",
                                  {"url": page.page_url, "sample": html[:300]})
            sid = m.group(1)
            page.source_id = sid
            page.lang_hint = page.lang_hint or _guess_lang(html)
            page.fmt = page.fmt or _guess_fmt(html)
            page.uploader = page.uploader or _guess_uploader(html)

            # 内容级特征：预览接口（Kernel 实测必须带 Referer + X-Requested-With，否则 403）
            preview = self._fetch_preview(client, sid, page.page_url)
            if preview:
                page.raw["preview"] = preview[:4000]

            # 广告/水印在**三跳之前**判掉：三跳要 3 次请求且实测会连续 403，
            # 让注定被丢弃的候选白跑一整轮很浪费（真机日志里 3D 组那条就是这样）。
            ad_kws = ((self.cfg.get("scoring", {}) or {}).get("ad_keywords")
                      or ["QQ群", "QQ号", "联系QQ", "热爱电影", "诚意制作"])
            hit = [k for k in ad_kws if k in (preview or "")]
            if hit:
                raise CandidateRejected(self.name, "resolve",
                                        f"预览含广告水印，跳过: {hit}",
                                        {"sid": sid, "url": page.page_url})

            direct = self._three_hops(client, sid, referer=page.page_url)
            page.download_url = direct
            page.raw["sid"] = sid
            return page

    def _fetch_preview(self, client: httpx.Client, sid: str, referer: str) -> str:
        """拉字幕预览正文，用于广告/水印等内容级特征（失败不阻断主链）。"""
        try:
            r = client.get(
                f"{self.base}/api/sub/preview/{sid}",
                headers={"Referer": referer,
                         "X-Requested-With": "XMLHttpRequest"},
            )
            if r.status_code != 200:
                log.debug("预览接口返回 %s（%s）", r.status_code, sid)
                return ""
            data = r.json()
            if not data.get("success", True):
                return ""
            f = data.get("file") or {}
            return str(f.get("content") or "")
        except Exception as e:  # noqa: BLE001 - 预览只是加分项
            log.debug("预览拉取失败(%s): %s", sid, e)
            return ""

    def _three_hops(self, client: httpx.Client, sid: str, referer: str) -> str:
        """prepare-download → /down/{sid} → /api/sub/down ⇒ zip 直链。

        实测：sid 是字母数字串（如 L5ogTp），不是纯数字，body 里必须原样传字符串。
        """
        hdr = {"Referer": referer, "X-Requested-With": "XMLHttpRequest"}

        # 每一跳都套重试：实测三跳**任意一跳**都会随机 403 / 超时
        # （Kernel 只在 /down/{sid} 上见过 403，真机在 prepare-download 上也遇到），
        # 不能只给某一步兜底。
        r1 = self._hop(client, "post", f"{self.base}/api/sub/prepare-download",
                       json={"sid": sid}, headers=hdr)
        if r1 is None:
            raise SourceError(self.name, "resolve", "prepare-download 反复失败", {"sid": sid})
        if r1.status_code not in (200, 204):
            raise SourceError(self.name, "resolve", f"prepare-download {r1.status_code}",
                              {"sid": sid, "body": r1.text[:200]})

        # 跳 2
        r2 = self._hop(client, "get", f"{self.base}/down/{sid}",
                       headers={"Referer": referer})
        if r2 is None:
            raise SourceError(self.name, "resolve", f"/down/{sid} 反复失败", {"sid": sid})
        if r2.status_code not in (200, 204, 302):
            raise SourceError(self.name, "resolve", f"/down/{sid} 返回 {r2.status_code}",
                              {"sid": sid, "body": r2.text[:200]})

        # 跳 3
        r3 = self._hop(client, "post", f"{self.base}/api/sub/down",
                       json={"sid": sid}, headers=hdr)
        if r3 is None:
            raise SourceError(self.name, "resolve", "api/sub/down 反复失败", {"sid": sid})
        if r3.status_code != 200:
            raise SourceError(self.name, "resolve", f"api/sub/down 返回 {r3.status_code}",
                              {"sid": sid, "body": r3.text[:200]})

        url = _extract_zip_url(r3.text)
        if not url:
            raise SourceError(self.name, "resolve", "三跳完成但未解析出直链",
                              {"sid": sid, "body": r3.text[:300]})
        return url

    def _hop(self, client: httpx.Client, method: str, url: str, **kw):
        """单跳带重试。

        实测：三跳每一步都会随机 403（重试即 200）或 TLS 读超时——与页面层线路
        抖动同型。但还有一种更硬的情况：**同一个 sid 被连续请求会短时频控 403**
        （真机见过同 sid 前一轮 200、后一轮三次全 403），所以退避要够长、带抖动。
        """
        last: str = ""
        tries = max(3, int(getattr(self, "retries", 3)))
        for i in range(tries):
            try:
                r = client.post(url, **kw) if method == "post" else client.get(url, **kw)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                log.warning("三跳 %s 第 %d 次异常(%s): %s", url.rsplit("/", 1)[-1], i + 1,
                            type(e).__name__, e)
                time.sleep(self._hop_wait(i))
                continue
            if r.status_code in (403, 429, 500, 502, 503, 504):
                last = f"status={r.status_code}"
                log.warning("三跳 %s 第 %d 次返回 %s，重试", url.rsplit("/", 1)[-1], i + 1,
                            r.status_code)
                time.sleep(self._hop_wait(i))
                continue
            return r
        log.warning("三跳 %s 重试用尽: %s", url, last)
        return None

    def _hop_wait(self, i: int) -> float:
        """三跳重试间隔：比页面重试长（应对短时频控），带抖动避免同刻撞上。"""
        import random

        base = float(getattr(self, "hop_backoff", 4.0))
        return min(base * (i + 1) + random.uniform(0, 1.5), 20.0)

    def probe_year(self, candidate: SubtitleCandidate) -> int | None:
        """回查候选真正归属的影片年份，用于同名不同片消歧（Josee 2003/2020 案）。

        Kernel 实测路径：版本页 /a/{sid} 里带归属影视页 /d/{id} 的链接（站点数据，
        不是标题猜测）；影视页 h1 形如「片名 (YYYY)」。

        成本：每个候选 +1~2 请求，所以调用方只对评分前 N 个候选调。
        返回 None 表示查不到（调用方按“无年份信息”处理，不做硬过滤）。
        """
        try:
            with self.new_client() as client:
                d_id = _norm_d_id(candidate.raw.get("d_id"))
                page = candidate.raw.get("detail_html") or ""
                if not page:
                    r = self._get(client, candidate.page_url)
                    if r.status_code != 200:
                        return None
                    page = r.text
                if not d_id:
                    # group(1) 是带前缀的 /d/123，group(2) 才是纯数字；
                    # 早期写成 group(1) → 拼出 /d//d/1428291 这种 404（真机踩到）。
                    m = re.search(r"href=['\"](?:/d/)?(\d+)['\"]", page)
                    if m:
                        d_id = m.group(1)
                if not d_id:
                    return None
                candidate.raw["d_id"] = d_id
                r2 = self._get(client, f"{self.base}/d/{d_id}")
                if r2.status_code != 200:
                    return None
                y = _year_from_title_page(r2.text)
                if y:
                    candidate.raw["probed_year"] = y
                    log.info("消歧：候选 %s 归属 /d/%s 年份=%s", candidate.source_id, d_id, y)
                return y
        except Exception as e:  # noqa: BLE001 - 消歧失败不该拖垮下载
            log.warning("年份回查失败(%s): %s", candidate.source_id, e)
            return None

    def download(self, candidate: SubtitleCandidate, dest_dir: str) -> DownloadResult:
        if not candidate.download_url:
            raise SourceError(self.name, "download", "没有真实下载地址", {"candidate": candidate.source_id})
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        name = _filename_from(candidate.download_url) or f"subhd-{candidate.source_id}.zip"
        tmp = Path(dest_dir) / (name + ".part")

        # 直链独立域名（dl.subhd.me）。实测直连常见 SSL 握手超时（线路层，非风控），
        # 因此直连重试若干次，仍全败才退到代理出口再试一轮。
        # 注意：httpx 默认信任环境变量代理（容器里已设 http(s)_proxy），
        # 直连那一轮要显式 trust_env=False，否则「直连」其实是走代理。
        # Kernel 实测口径：直连单轮常只进 20~300KB 就被掐，而**最后一截往往要靠代理续完**。
        # 所以不能「直连全败才用代理」，而是按批次交替：直连 2 轮 → 代理 → 直连 2 轮 → 代理…
        # 关键：换出口时**保留已下载进度**（续传靠 .part 文件 + Range），
        # 早期版本每轮 unlink 会把已经下到的一大截丢掉，等于每次从 0 开始。
        proxy = (self.sc.get("download_proxy") or "").strip()
        lanes: list[tuple[str, bool, str | None]] = [(f"直连#{i + 1}", False, None) for i in range(self.retries)]
        if proxy:
            lanes.append(("代理#1", True, proxy))
            lanes.extend((f"直连#{self.retries + i + 1}", False, None) for i in range(self.retries))
            lanes.append(("代理#2", True, proxy))

        last_err: Exception | None = None
        last_size = -1
        for label, trust_env, px in lanes:
            try:
                res = self._stream_download(candidate, tmp, name, dest_dir,
                                            proxy=px, trust_env=trust_env)
                return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                cur = tmp.stat().st_size if tmp.exists() else 0
                log.warning("下载失败(%s，已收 %sB): %s", label, cur, e)
                # 进度没涨说明这条出口当前是死的，才清掉重来；有进展就留着续
                if cur <= last_size:
                    tmp.unlink(missing_ok=True)
                last_size = cur
                time.sleep(self.retry_delay)
        tmp.unlink(missing_ok=True)
        raise SourceError(self.name, "download", f"下载反复失败: {last_err}",
                          {"url": candidate.download_url})

    def _stream_download(self, candidate: SubtitleCandidate, tmp: Path, name: str,
                         dest_dir: str, proxy: str | None = None,
                         trust_env: bool = False) -> DownloadResult:
        """流式下载 + **完整性校验 + Range 续传**（Chief 2026-10-16 定）。

        CDN 实测会中途掐断，HTTP 200 可能是截断的半截 zip（PK 头完好、EOCD 缺失）。
        拿到文件后必须校验长度与 EOCD；不完整则用 Range 续传，续完重新校验；
        续到上限仍不完整 → 抛错换候选，**绝不解包半截文件**。
        """
        from ..fetcher import check_download_complete

        headers = {"User-Agent": _UA, "Referer": candidate.page_url or self.base + "/"}
        max_rounds = int(self.sc.get("download_resume_rounds", 6))
        budget = float(self.sc.get("download_resume_budget_sec", 900))
        deadline = time.time() + max(60.0, budget)
        expected: int | None = None
        ct: str | None = None
        final_url = candidate.download_url
        rounds = 0
        last_why = "未开始"

        with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(120.0, connect=20.0),
                          proxy=proxy, trust_env=trust_env, headers=headers) as c:
            while True:
                have = tmp.stat().st_size if tmp.exists() else 0
                h = dict(headers)
                mode = "wb"
                if have > 0:
                    h["Range"] = f"bytes={have}-"
                    mode = "ab"
                try:
                    with c.stream("GET", candidate.download_url, headers=h) as r:
                        r.raise_for_status()
                        if have > 0 and r.status_code == 200:
                            mode, have = "wb", 0  # 服务端不支持 Range，从头重写
                        ct = r.headers.get("content-type") or ct
                        clen = r.headers.get("content-length")
                        if clen and clen.isdigit():
                            expected = int(clen) + (have if r.status_code == 206 else 0)
                        with tmp.open(mode) as f:
                            for chunk in r.iter_bytes():
                                f.write(chunk)
                        final_url = str(r.url)
                except (httpx.HTTPError, OSError) as e:
                    last_why = f"{type(e).__name__}: {e}"
                    log.warning("下载中断（已收 %sB）: %s", tmp.stat().st_size if tmp.exists() else 0, e)

                size = tmp.stat().st_size if tmp.exists() else 0
                head = b""
                if tmp.exists():
                    with tmp.open("rb") as f:
                        head = f.read(16)
                from ..pack import detect_archive as _da

                # kind 优先按 magic 判，其次按直链扩展名（真机实测有 .rar/.7z）
                kind_hint = _da(head) or _kind_from_url(final_url)
                ok, why = check_download_complete(str(tmp), kind_hint, expected, ct)
                if ok:
                    if rounds:
                        log.info("续传 %d 轮后下载完整（%dB）", rounds, size)
                    break
                last_why = why
                if time.time() > deadline or rounds >= max_rounds:
                    tmp.unlink(missing_ok=True)
                    raise SourceError(
                        self.name, "download",
                        f"下载不完整且续传到顶（{rounds} 轮）: {why}",
                        {"url": candidate.download_url, "bytes": size, "expected": expected})
                rounds += 1
                log.warning("下载不完整（%s），第 %d 轮续传: %s", why, rounds, candidate.download_url)
                time.sleep(min(self.retry_delay, 3.0))

        from ..pack import detect_archive

        kind = detect_archive(head)
        if kind is None and head[:1] in (b"<", b"{"):
            tmp.unlink(missing_ok=True)
            raise SourceError(self.name, "download", "拿到 HTML/JSON 而非字幕包（疑似 CF 或登录墙）",
                              {"url": candidate.download_url,
                               "head": head.decode("latin-1", "replace"), "content_type": ct})
        final = Path(dest_dir) / (name if kind else Path(name).stem + (_ext_from_ct(ct) or ".srt"))
        tmp.replace(final)
        return DownloadResult(path=str(final), content_type=ct, magic=head,
                              bytes_written=size, final_url=final_url, resume_rounds=rounds)


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ---- 工具 ----

def _norm_d_id(value) -> str | None:
    """把 d_id 规范成纯数字串，防止拼出 `/d//d/1428291` 这种 404（真机踩过）。

    raw["d_id"] 的来源不止一处（列表页 href、版本页回查），格式不保证一致。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"/d/(\d+)", s)
    if m:
        return m.group(1)
    return s if s.isdigit() else None


def _year_from_title_page(html: str) -> int | None:
    """从影视页 /d/{id} 的 h1 取「片名 (YYYY)」里的年份（Kernel 实测结构）。"""
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    scope = m.group(1) if m else html[:6000]
    m2 = re.search(r"[\(（]\s*((?:19|20)\d{2})\s*[\)）]", scope)
    if m2:
        return int(m2.group(1))
    m3 = re.search(r"[\(（]\s*((?:19|20)\d{2})\s*[\)）]", html[:8000])
    return int(m3.group(1)) if m3 else None


def _kind_from_url(url: str) -> str | None:
    """从直链扩展名猜压缩格式（真机实测 dl.subhd.me 会给 .zip/.rar/.7z）。"""
    low = (url or "").lower().split("?")[0]
    for ext, kind in ((".zip", "zip"), (".rar", "rar"), (".7z", "7z")):
        if low.endswith(ext):
            return kind
    return None


def _urlquote(s: str) -> str:
    from urllib.parse import quote

    return quote(s, safe="")


def _tags_of(block: str) -> str:
    """取条目块里的语言标签，如「双语 简体 繁体 日语」。"""
    tags = re.findall(r'<span class="p-1 fw-bold">([^<]{1,8})</span>', block)
    return " ".join(tags)


def _fmt_of(block: str) -> str:
    m = re.search(r'<span class="p-1 text-secondary">([^<]{1,6})</span>', block)
    return m.group(1).strip().lower() if m else ""


def _downloads_of(block: str) -> int:
    m = re.search(r"align-text-top me-3\">\s*([\d.]+k?)\s*<", block)
    if not m:
        return 0
    v = m.group(1)
    try:
        return int(float(v[:-1]) * 1000) if v.endswith("k") else int(float(v))
    except ValueError:
        return 0


def _search_terms(query: MediaQuery) -> list[str]:
    """生成检索词：短名（去冠词/标点）优先，命中率高。"""
    terms: list[str] = []
    for t in query.search_terms():
        t = (t or "").strip()
        if not t:
            continue
        # 去掉逗号后的副标题（"Josee, the Tiger and the Fish" → "Josee"）
        short = re.split(r"[,:：]", t)[0].strip()
        if short and short not in terms:
            terms.append(short)
        if t not in terms:
            terms.append(t)
    return terms


def _clean(html: str) -> str:
    return re.sub(r"\s+", " ", RE_STRIP.sub("", html)).strip()


def _dedup(cands: list[SubtitleCandidate]) -> list[SubtitleCandidate]:
    seen: set[str] = set()
    out: list[SubtitleCandidate] = []
    for c in cands:
        if c.key in seen:
            continue
        seen.add(c.key)
        out.append(c)
    return out


def _plausible(title: str, query: MediaQuery) -> bool:
    t = title.lower()
    for term in query.search_terms():
        if term and term.lower() in t:
            return True
    if query.year and str(query.year) in t:
        return True
    return bool(re.search(r"[\u4e00-\u9fff]", title))


def _guess_lang(html: str) -> str:
    hits = []
    for k, v in (("简体", "简体"), ("简中", "简体"), ("繁体", "繁體"), ("双语", "双语"),
                 ("英文", "英文"), ("CHS", "简体"), ("CHT", "繁體")):
        if k in html:
            hits.append(v)
    return "/".join(dict.fromkeys(hits))


def _guess_fmt(html: str) -> str:
    low = html.lower()
    for f in ("ass", "srt", "ssa", "sup", "sub", "idx"):
        if f".{f}" in low:
            return f
    return ""


def _guess_uploader(html: str) -> str:
    m = re.search(r"(?:上传者|发布者|字幕组|by)[:：]?\s*([A-Za-z0-9_\u4e00-\u9fff]{2,20})", html)
    return m.group(1) if m else ""


def _extract_zip_url(body: str) -> str:
    m = RE_ZIP_URL.search(body)
    if m:
        return m.group(0)
    # JSON 里可能带转义斜杠
    try:
        data = json.loads(body)
        for v in _walk(data):
            if isinstance(v, str) and v.startswith("http") and any(
                e in v.lower() for e in (".zip", ".rar", ".7z", "dl.subhd")
            ):
                return v
    except Exception:  # noqa: BLE001
        pass
    m = RE_ANY_URL.search(body.replace("\\/", "/"))
    return m.group(0) if m else ""


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


def _filename_from(url: str) -> str:
    from urllib.parse import unquote, urlparse

    p = urlparse(url).path
    return unquote(p.rsplit("/", 1)[-1]) if p else ""


def _ext_from_ct(ct: str | None) -> str:
    if not ct:
        return ""
    ct = ct.lower()
    for key, ext in (("ass", ".ass"), ("subrip", ".srt"), ("srt", ".srt"),
                     ("zip", ".zip"), ("rar", ".rar"), ("7z", ".7z")):
        if key in ct:
            return ext
    return ""
