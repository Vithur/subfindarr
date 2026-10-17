"""subhd 适配器测试：以 2026-10-16 容器内实测的 DOM 形态为准。

实测要点（Forge 在 NAS 容器内抓取，2026-10-16 23:2x）：
- 结果区在「的搜索结果」之后的片段；结果条目容器 `bg-white shadow-sm rounded-3`
- 影视页链接：`<a href='/d/{数字}'>`，片名在 `<img alt="...">`
- 版本页链接：`<a href='/a/{sid}'>`，**sid 是字母数字串**（如 L5ogTp），单引号
- 语言标签：`<span class="p-1 fw-bold">简体</span>`；格式：`<span class="p-1 text-secondary">ASS</span>`
- 下载数：`align-text-top me-3">1.2k<`
- 三跳：prepare-download → /down/{sid} → api/sub/down ⇒ dl.subhd.me zip 直链
- 时间线：`Josee, the Tiger and the Fish 2020` 检索 0 条，用短名 `Josee` 才命中

这些用例全部离线跑（HTTP 用替身），只验证「解析与链路编排」是否符合实测形态；
真站连通性由 tools/probe_live.py 单独验证。
"""

from __future__ import annotations

import re  # noqa: E402
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.fetcher import is_cf_challenge, looks_like_cf, parse_cookie_header  # noqa: E402
from app.sources.base import MediaQuery, SubtitleCandidate  # noqa: E402
from app.sources.subhd import (  # noqa: E402
    SubhdAdapter,
    _extract_zip_url,
    _filename_from,
    _search_terms,
    _tags_of,
)

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if detail and not cond else ""))
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)


# ---------- 实测 HTML 样本 ----------

SEARCH_HTML = """
<html><body>
<nav class="navbar"><a href='/d/999'>首页推荐别抓我</a></nav>
<div class="container">Josee 的搜索结果</div>
<div class="bg-white shadow-sm rounded-3 mb-4">
  <a href='/d/36439868'><div class="pics"><img alt="乔西的虎与鱼 Josee, the Tiger and the Fish"></div></a>
</div>
<div class="bg-white shadow-sm rounded-3 mb-4">
  <a href='/a/L5ogTp' class="float-start f16 fw-bold">乔西的虎与鱼.BD.1080P.简繁双语</a>
  <span class="p-1 fw-bold">简体</span><span class="p-1 fw-bold">繁体</span>
  <span class="p-1 text-secondary">ASS</span>
  <span class="align-text-top me-3">1.2k</span>
</div>
<div class="bg-white shadow-sm rounded-3 mb-4">
  <a href='/a/Qm3xZ9' class="float-start f16 fw-bold">乔西的虎与鱼.WEB.1080P.简体</a>
  <span class="p-1 fw-bold">简体</span>
  <span class="p-1 text-secondary">SRT</span>
  <span class="align-text-top me-3">340</span>
</div>
</body></html>
"""

VERSION_HTML = """
<html><body>
<button class="btn btn-danger down subtitle-prepare-download" data-sid="L5ogTp">下载</button>
<div>发布者：远鉴字幕组</div>
</body></html>
"""


class FakeClient(httpx.Client):
    """替身：按 URL 关键词返回样本，并记录调用顺序。"""

    def __init__(self, routes: dict[str, str]) -> None:
        super().__init__(base_url="https://subhd.tv")
        self.routes = routes
        self.flow: list[str] = []
        self.cookies_used: list[str] = []

    def get(self, url, **kw):  # type: ignore[override]
        self.flow.append(f"GET {str(url).split('subhd.tv')[-1]}")
        target = str(url)
        req = httpx.Request("GET", "https://subhd.tv" + target)
        # 三跳第 2 跳：/down/{sid} 中转页（实测存在，返回倒计时页并种 down_* cookie）
        if re.search(r"/down/[\w]+$", target):
            r = httpx.Response(200, text="<html>倒计时中…</html>", request=req)
            r.cookies.set("down_xyz", "1", domain="subhd.tv", path="/api/sub/down")
            return r
        for pat, body in self.routes.items():
            if pat in target:
                r = httpx.Response(200, text=body, request=req)
                return r
        return httpx.Response(404, text="", request=req)

    def post(self, url, **kw):  # type: ignore[override]
        self.flow.append(f"POST {str(url).split('subhd.tv')[-1]}")
        req = httpx.Request("POST", "https://subhd.tv" + str(url))
        body = kw.get("json") or {}
        assert body.get("sid") == "L5ogTp", f"sid 必须以字符串原样传: {body!r}"
        if str(url).endswith("/api/sub/prepare-download"):
            r = httpx.Response(200, json={"success": True, "url": "/down/L5ogTp"}, request=req)
            r.cookies.set("tk_abc", "1", domain="subhd.tv", path="/")
            return r
        if str(url).endswith("/api/sub/down"):
            return httpx.Response(
                200, json={"success": True, "pass": True,
                           "url": "https://dl.subhd.me/2016/09/147515243225669.zip"},
                request=req,
            )
        return httpx.Response(404, text="", request=req)

    def __enter__(self):  # type: ignore[override]
        return self

    def __exit__(self, *exc):  # type: ignore[override]
        return None


def _adapter(routes: dict[str, str]) -> tuple[SubhdAdapter, FakeClient]:
    ad = SubhdAdapter({"sources": {"subhd": {"enabled": True}}})
    client = FakeClient(routes)
    ad.new_client = lambda: client  # type: ignore[assignment]
    return ad, client


# ---------- CF 判别 ----------

def t_cf() -> None:
    check("403+cf-mitigated 判挑战", is_cf_challenge(403, {"cf-mitigated": "challenge"}, ""))
    check("403 纯状态码不算挑战", not is_cf_challenge(403, {}, "403 Forbidden"))
    check("503 不算挑战（实测是线路层）", not is_cf_challenge(503, {"server": "cloudflare"}, ""))
    check("403+挑战页体算挑战", is_cf_challenge(403, {}, "cf-chl-opt"))
    check("looks_like_cf 兼容签名", looks_like_cf(403, "x", {"cf-mitigated": "challenge"}))


# ---------- 搜索解析 ----------

def t_search_parse() -> None:
    ad, _ = _adapter({"/search/Josee": SEARCH_HTML})
    cands = ad.search(MediaQuery(media_type="movie", title="Josee, the Tiger and the Fish", year=2020))
    kinds = [c.raw.get("kind") for c in cands]
    check("抓到影视页条目", "title" in kinds, str(kinds))
    check("抓到版本页条目", "version" in kinds, str(kinds))
    check("没混进首页推荐（999）", all("999" not in c.page_url for c in cands),
          str([c.page_url for c in cands]))
    vers = [c for c in cands if c.raw.get("kind") == "version"]
    check("版本页 sid 是字母数字串", vers and vers[0].source_id == "L5ogTp", str([v.source_id for v in vers]))
    check("版本页带语言标签", vers and "简体" in vers[0].lang_hint, str(vers[0].lang_hint if vers else ""))
    check("版本页带格式", vers and vers[0].fmt == "ass", str(vers[0].fmt if vers else ""))
    check("下载数解析 1.2k", vers and vers[0].downloads == 1200, str(vers[0].downloads if vers else ""))
    check("srt 那条下载数 340", len(vers) > 1 and vers[1].downloads == 340, str([v.downloads for v in vers]))


def t_search_terms() -> None:
    terms = _search_terms(MediaQuery(media_type="movie", title="Josee, the Tiger and the Fish", year=2020))
    check("短名优先（逗号前）", terms[0] == "Josee", str(terms))
    check("全名也在列表里", "Josee, the Tiger and the Fish" in terms, str(terms))


# ---------- 三跳 ----------

def t_three_hops() -> None:
    ad, client = _adapter({"/a/L5ogTp": VERSION_HTML})
    cand = SubtitleCandidate(source="subhd", source_id="L5ogTp", title="乔西的虎与鱼",
                             page_url="https://subhd.tv/a/L5ogTp")
    got = ad.resolve(cand)
    check("跳序 1=版本页", client.flow[0] == "GET /a/L5ogTp", str(client.flow))
    check("跳序 2=prepare-download",
          any(f == "POST /api/sub/prepare-download" for f in client.flow), str(client.flow))
    check("跳序 3=/down/{sid}", any(f == "GET /down/L5ogTp" for f in client.flow), str(client.flow))
    check("跳序 4=api/sub/down", any(f == "POST /api/sub/down" for f in client.flow), str(client.flow))
    check("拿到 dl.subhd.me 直链",
          got.download_url == "https://dl.subhd.me/2016/09/147515243225669.zip", got.download_url)
    check("data-sid 原样保留", got.raw.get("sid") == "L5ogTp", str(got.raw))
    check("发布者解析", got.uploader == "远鉴字幕组", got.uploader)


def t_resolve_from_title_page() -> None:
    """影视页 → 展开版本页 → 取第一个。"""
    ad, client = _adapter({"/d/36439868": SEARCH_HTML, "/a/L5ogTp": VERSION_HTML})
    cand = SubtitleCandidate(source="subhd", source_id="36439868", title="乔西的虎与鱼",
                             page_url="https://subhd.tv/d/36439868", raw={"kind": "title"})
    got = ad.resolve(cand)
    check("影视页先展开", client.flow[0] == "GET /d/36439868", str(client.flow))
    check("选到版本页并取到直链", "dl.subhd.me" in (got.download_url or ""), str(got.download_url))
    check("sid 来自版本页", got.raw.get("sid") == "L5ogTp", str(got.raw.get("sid")))


# ---------- 下载与校验 ----------

def t_download_guard() -> None:
    ad, _ = _adapter({})
    cand = SubtitleCandidate(source="subhd", source_id="x", title="t")
    try:
        ad.download(cand, "/tmp")
        check("无直链时拒绝下载", False, "没抛错")
    except Exception as e:  # noqa: BLE001
        check("无直链时拒绝下载", "没有真实下载地址" in str(e), str(e))


# ---------- 工具函数 ----------

def t_utils() -> None:
    check("zip 直链提取", _extract_zip_url('{"url":"https://dl.subhd.me/a/b.zip"}')
          == "https://dl.subhd.me/a/b.zip")
    check("转义斜杠也能提", _extract_zip_url('{"url":"https:\\/\\/dl.subhd.me\\/a\\/b.zip"}')
          == "https://dl.subhd.me/a/b.zip")
    check("无直链返回空", _extract_zip_url('{"pass":false}') == "")
    check("文件名提取", _filename_from("https://dl.subhd.me/2016/09/x.zip") == "x.zip")
    check("语言标签提取", _tags_of('<span class="p-1 fw-bold">双语</span><span class="p-1 fw-bold">简体</span>')
          == "双语 简体")
    check("cookie 解析", parse_cookie_header("a=1; b=2") == {"a": "1", "b": "2"})


class FlakyClient(httpx.Client):
    """三跳里随机 403 的替身：前 N 次失败，之后成功。

    实测现象：三跳**任意一跳**都会随机 403（重试即 200），所以每跳都要能重试。
    """

    def __init__(self) -> None:
        super().__init__(base_url="https://subhd.tv")
        self.hits: dict[str, int] = {}
        self.fail_times = {"/api/sub/prepare-download": 2, "/down/L5ogTp": 1, "/api/sub/down": 0}

    def _handle(self, key: str, url: str, req: httpx.Request):
        n = self.hits.get(key, 0) + 1
        self.hits[key] = n
        if n <= self.fail_times.get(key, 0):
            return httpx.Response(403, text="", request=req)
        if key == "/api/sub/prepare-download":
            return httpx.Response(200, json={"success": True, "url": "/down/L5ogTp"}, request=req)
        if key.startswith("/down/"):
            return httpx.Response(200, text="<html>中转页</html>", request=req)
        if key == "/api/sub/down":
            return httpx.Response(200, json={"success": True, "pass": True,
                                             "url": "https://dl.subhd.me/x/y.zip"}, request=req)
        return httpx.Response(404, request=req)

    def post(self, url, **kw):  # type: ignore[override]
        return self._handle(str(url).split("subhd.tv")[-1], str(url),
                            httpx.Request("POST", str(url)))

    def get(self, url, **kw):  # type: ignore[override]
        return self._handle(str(url).split("subhd.tv")[-1], str(url),
                            httpx.Request("GET", str(url)))

    def __enter__(self):  # type: ignore[override]
        return self

    def __exit__(self, *exc):  # type: ignore[override]
        return None


def t_hop_retry() -> None:
    """三跳每跳都要能重试（实测任意一跳都会随机 403，重试即 200）。"""
    import tempfile as _tf

    ad = SubhdAdapter({"sources": {"subhd": {"enabled": True}}})
    ad.retries = 4
    ad.retry_delay = 0.01
    ad.hop_backoff = 0.01
    client = FlakyClient()
    ad.new_client = lambda: client  # type: ignore[assignment]

    cand = SubtitleCandidate(source="subhd", source_id="L5ogTp", title="t",
                             page_url="https://subhd.tv/a/L5ogTp")
    # download() 要求先 resolve 拿到直链；这里直接测三跳本身
    with _tf.TemporaryDirectory() as tmp:
        direct = ad._three_hops(client, "L5ogTp", referer="https://subhd.tv/a/L5ogTp")
        check("跳 1 被重试到第 3 次才成功", client.hits.get("/api/sub/prepare-download") == 3,
              str(client.hits))
        check("跳 2 被重试到第 2 次才成功", client.hits.get("/down/L5ogTp") == 2, str(client.hits))
        check("403 重试后仍拿到直链", "dl.subhd.me" in str(direct), str(direct))


def main() -> int:
    t_cf()
    t_search_parse()
    t_search_terms()
    t_three_hops()
    t_hop_retry()
    t_resolve_from_title_page()
    t_download_guard()
    t_utils()
    print()
    print(f"通过 {PASSED} 项")
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
