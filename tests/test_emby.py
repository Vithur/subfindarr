"""Emby 刷新链路测试（按 Ops 2026-10-16 实测口径）。

实测反例（必须被代码规避）：
- `GET /Items?Path=<文件路径>` 只匹配库根文件夹 → 不能用它查 item
- `SearchTerm=Josee` 命中 0（Emby 按中文名索引）→ 不用标题查

正确链路：算落点 → 按前缀归属库 → `GET /Items?ParentId=&Recursive=true&Fields=Path`
→ 客户端侧按 Path 过滤 → `POST /Items/{Id}/Refresh`（204）→ 回查字幕流。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.api import EmbyClient  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if detail and not cond else ""))
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)


# ---- 实测形态的替身：库列表 + 条目 + 刷新 + 回查 ----

LIBS = {
    # 电影库是 strm 型前缀；真文件条目仍在 /media/movies —— 两者都要能归属
    "15557": "/media/movies",
    "15561": "/media/tv",
    "15587": "/media/anime",
    "15557s": "/strm/movies",
}

ITEMS = {
    "15557": [
        {"Id": "29188", "Name": "乔西的虎与鱼", "Path": "/media/movies/Josee, the Tiger and the Fish (2020)/Josee (2020) WEBDL-2160p.mkv"},
        {"Id": "30001", "Name": "别的片", "Path": "/media/movies/Other (2021)/Other.mkv"},
    ],
    "15561": [
        {"Id": "40001", "Name": "三体", "Path": "/media/tv/三体/Season 01/三体 - S01E05.mkv"},
    ],
}


class FakeEmby(httpx.Client):
    def __init__(self) -> None:
        super().__init__(base_url="http://emby:8096")
        self.calls: list[tuple[str, str, dict]] = []
        self.refreshed: list[str] = []

    def get(self, url, **kw):  # type: ignore[override]
        u = urlparse(str(url))
        params = {k: v[0] for k, v in parse_qs(u.query).items()}
        params.update({k: v for k, v in (kw.get("params") or {}).items()})
        self.calls.append(("GET", u.path, params))

        if u.path == "/Users":
            return httpx.Response(200, json=[{"Id": "UID1", "Name": "vithur"}],
                                  request=httpx.Request("GET", url))

        if u.path == "/Items":
            # 实测：Path 参数只匹配库根 → 只要带了具体文件路径就返回 0 条
            if params.get("Path"):
                return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0},
                                      request=httpx.Request("GET", url))
            if params.get("SearchTerm"):
                return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0},
                                      request=httpx.Request("GET", url))
            pid = str(params.get("ParentId") or "")
            return httpx.Response(200, json={"Items": ITEMS.get(pid, [])},
                                  request=httpx.Request("GET", url))

        # 实测：/Items/{Id} 直查一律 404（连库内条目也是），必须走 /Users/{uid}/Items/{Id}
        if u.path.startswith("/Items/"):
            return httpx.Response(404, request=httpx.Request("GET", url))

        if u.path.startswith("/Users/") and "/Items/" in u.path:
            iid = u.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={
                "Id": iid, "Name": "乔西的虎与鱼", "Path": ITEMS["15557"][0]["Path"],
                "MediaSources": [{
                    "Path": ITEMS["15557"][0]["Path"],
                    "Protocol": "File",
                    "MediaStreams": [
                        {"Type": "Video", "Index": 0},
                        {"Type": "Subtitle", "Index": 2, "Language": "zho",
                         "DisplayTitle": "Chinese Simplified (SRT)", "Codec": "srt",
                         "IsExternal": True,
                         "Path": "/media/movies/Josee, the Tiger and the Fish (2020)/Josee (2020) WEBDL-2160p.zh-cn.srt"},
                    ],
                }],
            }, request=httpx.Request("GET", url))

        if u.path == "/System/Info":
            return httpx.Response(200, json={"Version": "4.10.1.0"}, request=httpx.Request("GET", url))
        return httpx.Response(404, request=httpx.Request("GET", url))

    def post(self, url, **kw):  # type: ignore[override]
        u = urlparse(str(url))
        self.calls.append(("POST", u.path, kw.get("params") or {}))
        if u.path.startswith("/Items/") and u.path.endswith("/Refresh"):
            iid = u.path.split("/")[2]
            self.refreshed.append(iid)
            return httpx.Response(204, request=httpx.Request("POST", url))
        return httpx.Response(404, request=httpx.Request("POST", url))

    def __enter__(self):  # type: ignore[override]
        return self

    def __exit__(self, *exc):  # type: ignore[override]
        return None


def _client(libs=None) -> tuple[EmbyClient, FakeEmby]:
    c = EmbyClient("http://emby:8096", "k", libraries=libs or LIBS)
    fake = FakeEmby()
    c._c = fake  # type: ignore[assignment]
    return c, fake


def t_lib_prefix() -> None:
    c, _ = _client()
    hits = c.libraries_matching("/media/movies/Josee, the Tiger and the Fish (2020)/x.mkv")
    check("前缀归属库命中电影库", [h[0] for h in hits] == ["15557"], str(hits))
    hits2 = c.libraries_matching("/media/tv/三体/Season 01/x.mkv")
    check("前缀归属剧集库", [h[0] for h in hits2] == ["15561"], str(hits2))
    hits3 = c.libraries_matching("/strm/movies/百米/百米.strm")
    check("strm 前缀单独归属", hits3 and hits3[0][0] == "15557s", str(hits3))
    check("无前缀命中返回空", c.libraries_matching("/nope/x.mkv") == [], "应为空")


def t_find_by_path() -> None:
    c, fake = _client()
    item = c.find_by_path("/media/movies/Josee, the Tiger and the Fish (2020)/Josee (2020) WEBDL-2160p.mkv")
    check("按 Path 前缀过滤找到条目", item is not None and item.get("Id") == "29188",
          json.dumps(item, ensure_ascii=False) if item else "None")
    check("用的是 ParentId 递归列表",
          any(str(prm.get("ParentId", "")) == "15557" for _m, _p, prm in fake.calls), str(fake.calls[:2]))
    check("带了 Fields=Path", any(prm.get("Fields") == "Path" for _m, _p, prm in fake.calls), str(fake.calls[:2]))
    check("没有用 ?Path= 参数查 item",
          not any(prm.get("Path") for _m, _p, prm in fake.calls), str(fake.calls[:2]))


def t_refresh_flow() -> None:
    c, fake = _client()
    res = c.refresh_path("/media/movies/Josee, the Tiger and the Fish (2020)/Josee (2020) WEBDL-2160p.mkv")
    check("刷新命中单条", res.get("mode") == "item", str(res))
    check("刷新状态 204", res.get("status") == 204, str(res))
    check("只刷了那一条", fake.refreshed == ["29188"], str(fake.refreshed))
    check("回查字段可用", "path" in res and res.get("item_id") == "29188", str(res))


def t_subtitle_streams() -> None:
    c, _ = _client()
    streams = c.media_sources("29188")
    check("回查到字幕流", len(streams) == 1, str(streams))
    check("字幕流语言/标题", streams and "Chinese Simplified" in str(streams[0].get("display")), str(streams))
    check("字幕流路径指向 zh-cn", streams and ".zh-cn.srt" in str(streams[0].get("path")), str(streams))


def t_not_found() -> None:
    c, _ = _client()
    res = c.refresh_path("/media/movies/Unmatched (1999)/nope.mkv")
    check("匹配不到时 mode=none", res.get("mode") == "none", str(res))
    check("不回退全库刷新", "library" not in str(res), str(res))


def t_ping() -> None:
    c, _ = _client()
    check("ping 走 /System/Info", c.ping() is True, "ping 应 True")


def t_item_detail_path() -> None:
    """单条目必须走 /Users/{uid}/Items/{id}——/Items/{id} 一律 404（真机踩到）。"""
    c, fake = _client()
    d = c.item_detail("29188")
    check("item_detail 能取到条目", d.get("Id") == "29188", str(d)[:120])
    paths = [p for (_m, p, _prm) in fake.calls if "/Items/" in p]
    check("走的是 /Users/{uid}/Items/{id}",
          any(p.startswith("/Users/") and p.endswith("/Items/29188") for p in paths), str(paths))
    check("没有直接用 /Items/{id}",
          not any(p == "/Items/29188" for p in paths), str(paths))


def t_media_sources_via_user() -> None:
    c, fake = _client()
    streams = c.media_sources("29188")
    check("经 User 路径取到字幕流", len(streams) == 1, str(streams))
    check("字幕流语言/标题", streams and "Chinese Simplified" in str(streams[0].get("display")), str(streams))
    check("外挂标志为真", streams and streams[0].get("is_external") is True, str(streams))
    check("指向 zh-cn 文件", streams and ".zh-cn.srt" in str(streams[0].get("path")), str(streams))


def main() -> int:
    t_lib_prefix()
    t_find_by_path()
    t_item_detail_path()
    t_media_sources_via_user()
    t_refresh_flow()
    t_subtitle_streams()
    t_not_found()
    t_ping()
    print()
    print(f"通过 {PASSED} 项")
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
