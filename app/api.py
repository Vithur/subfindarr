"""arr 与 Emby API 客户端。"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

log = logging.getLogger("subfindarr.api")


def _http_client(**kw) -> httpx.Client:
    """httpx 0.23 用 `proxies`，0.28 用 `proxy`；按实际版本传，避免宿主/容器版本差炸掉。"""
    proxy = kw.pop("proxy", None)
    if proxy:
        try:
            return httpx.Client(proxy=proxy, **kw)
        except TypeError:
            return httpx.Client(proxies=proxy, **kw)
    return httpx.Client(**kw)


class SonarrClient:
    def __init__(self, url: str, api_key: str, proxy: str | None = None, timeout: float = 20.0) -> None:
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self._c = _http_client(
            base_url=self.url,
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            proxy=proxy,
        )

    def series(self, series_id: int) -> dict:
        return self._c.get(f"/api/v3/series/{series_id}").raise_for_status().json()

    def episode_file(self, file_id: int) -> dict:
        return self._c.get(f"/api/v3/episodefile/{file_id}").raise_for_status().json()

    def episodes(self, series_id: int, season: int | None = None) -> list[dict]:
        params = {"seriesId": series_id}
        if season is not None:
            params["seasonNumber"] = season
        return self._c.get("/api/v3/episode", params=params).raise_for_status().json()

    def ping(self) -> bool:
        try:
            r = self._c.get("/api/v3/system/status")
            return r.status_code == 200
        except httpx.HTTPError:
            return False


class RadarrClient:
    def __init__(self, url: str, api_key: str, proxy: str | None = None, timeout: float = 20.0) -> None:
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self._c = _http_client(
            base_url=self.url,
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            proxy=proxy,
        )

    def movie(self, movie_id: int) -> dict:
        return self._c.get(f"/api/v3/movie/{movie_id}").raise_for_status().json()

    def movie_file(self, file_id: int) -> dict:
        return self._c.get(f"/api/v3/moviefile/{file_id}").raise_for_status().json()

    def ping(self) -> bool:
        try:
            return self._c.get("/api/v3/system/status").status_code == 200
        except httpx.HTTPError:
            return False


class EmbyClient:
    """Emby 4.x。

    刷新链路按 Ops 2026-10-16 实测（`Items?Path=` 与 `SearchTerm=` 都不可行）：
      算字幕落点 → 按路径前缀归属库 → GET /Items?ParentId={libId}&Recursive=true&Fields=Path
      → **客户端侧按 Path 前缀过滤**找到条目 → POST /Items/{Id}/Refresh（204）

    实测反例：
    - `GET /Items?Path=<文件路径>` 只匹配库根，文件级 0 命中 → Path 只用来算落点，不查 item
    - `SearchTerm=Josee` 命中 0（Emby 按中文名建索引）→ 不用 arr 给的原标题去搜
    """

    # Ops 实测 + Forge 2026-10-17 复核的库对照。**三个库都是混合库**（真机读数）：
    #   15557 电影 47 条 /strm/movies + 8 条 /media/movies
    #   15561 剧集 92 条 /strm/tv     + 18 条 /media/tv
    #   15587 动漫 104 条 /strm/anime + 55 条 /media/anime
    # 每个库都要登记 strm 与 media 两个前缀，否则真文件型条目查不到、刷新被跳过。
    # key 用 `真实Id-后缀` 区分，归属时只取 `-` 前那段。可用 config 覆盖。
    DEFAULT_LIBS = {
        "15557": "/media/movies",
        "15557-strm": "/strm/movies",
        "15561": "/media/tv",
        "15561-strm": "/strm/tv",
        "15587": "/media/anime",
        "15587-strm": "/strm/anime",
        "19296": "/media/documentary",
        "19296-strm": "/strm/documentary",
    }

    def __init__(
        self,
        url: str,
        api_key: str,
        proxy: str | None = None,
        timeout: float = 20.0,
        libraries: dict | None = None,
    ) -> None:
        self.url = (url or "").rstrip("/")
        self.api_key = api_key
        self.libraries = {str(k): str(v) for k, v in (libraries or self.DEFAULT_LIBS).items()}
        self._c = _http_client(
            base_url=self.url,
            headers={"X-Emby-Token": api_key},
            timeout=timeout,
            proxy=proxy,
        )
        self._listing_cache: dict[str, list[dict]] = {}
        self._uid: str | None = None

    # ---- 库与条目 ----

    def libraries_matching(self, path: str) -> list[tuple[str, str]]:
        """按最长前缀匹配归属库，返回 [(lib_id, lib_path), ...]（长的优先）。

        key 允许写成 `15557-strm` 这种「库 Id + 区分后缀」的形式——同一库有多个
        路径前缀时用它区分；返回时只取真实的库 Id（`-` 之前那段）。
        """
        hits: list[tuple[str, str]] = []
        for key, lp in self.libraries.items():
            if path.startswith(lp):
                hits.append((key.split("-")[0], lp))
        hits.sort(key=lambda t: len(t[1]), reverse=True)
        return hits

    def list_items(self, lib_id: str) -> list[dict]:
        """拉一个库下的条目（一次，带缓存）。Fields=Path 才能做前缀过滤。"""
        if lib_id in self._listing_cache:
            return self._listing_cache[lib_id]
        r = self._c.get(
            "/Items",
            params={
                "ParentId": str(lib_id),
                "Recursive": "true",
                "Fields": "Path",
                "IncludeItemTypes": "Movie,Episode",
                "Limit": "10000",
            },
        )
        items = (r.json() or {}).get("Items") or [] if r.status_code == 200 else []
        self._listing_cache[lib_id] = items
        return items

    def find_by_path(self, path: str) -> dict | None:
        """客户端侧按 Path 前缀过滤找条目（实测可行的那条路）。"""
        for lib_id, _lib_path in self.libraries_matching(path):
            best: dict | None = None
            for it in self.list_items(lib_id):
                p = str(it.get("Path") or "")
                if not p:
                    continue
                # 条目 Path 即视频文件路径；取相同或同目录的
                if p == path or Path(p).parent == Path(path).parent:
                    return it
                if p.startswith(path) and best is None:
                    best = it
            if best:
                return best
        return None

    def _user_id(self) -> str | None:
        """拿一个 UserId。

        Emby 的 `/Items/{Id}` 直查**一律 404**（真机实测：库内任意条目都如此），
        取单条目必须走 `/Users/{UserId}/Items/{Id}`。所以先解析一次 UserId 并缓存。
        """
        if self._uid is not None:
            return self._uid
        try:
            r = self._c.get("/Users")
            if r.status_code == 200:
                users = r.json() or []
                if users:
                    self._uid = str(users[0].get("Id") or "")
                    return self._uid
        except httpx.HTTPError as e:
            log.warning("取 UserId 失败: %s", e)
        return None

    def item_detail(self, item_id: str, fields: str = "MediaSources,MediaStreams,Path,Type") -> dict:
        """取单条目详情（走 `/Users/{uid}/Items/{id}`，这是唯一可用的路径）。"""
        uid = self._user_id()
        if not uid:
            return {}
        try:
            r = self._c.get(f"/Users/{uid}/Items/{item_id}", params={"Fields": fields})
            if r.status_code == 200:
                return r.json() or {}
            log.debug("取条目 %s 详情返回 %s", item_id, r.status_code)
        except httpx.HTTPError as e:
            log.debug("取条目 %s 详情失败: %s", item_id, e)
        return {}

    def media_source_protocols(self, item_id: str) -> list[dict]:
        """取条目的媒体源协议/路径，用于判 strm 型（http 远程源）。"""
        data = self.item_detail(item_id, "MediaSources,Path")
        out: list[dict] = []
        for ms in (data.get("MediaSources") or []):
            out.append({
                "protocol": ms.get("Protocol"),
                "path": ms.get("Path"),
                "size": ms.get("Size"),
                "container": ms.get("Container"),
            })
        return out

    def media_sources(self, item_id: str) -> list[dict]:
        """回查条目的字幕流，用于确认 Emby 是否已识别外挂字幕。"""
        data = self.item_detail(item_id, "MediaSources,MediaStreams,Path")
        out: list[dict] = []
        for ms in (data.get("MediaSources") or []):
            for st in ms.get("MediaStreams") or []:
                if str(st.get("Type", "")).lower() == "subtitle":
                    out.append({
                        "index": st.get("Index"),
                        "language": st.get("Language"),
                        "display": st.get("DisplayTitle") or st.get("DisplayLanguage"),
                        "codec": st.get("Codec"),
                        "path": st.get("Path") or ms.get("Path"),
                        "is_external": st.get("IsExternal"),
                    })
        return out

    # ---- 刷新 ----

    def refresh_item(self, item_id: str, *, replace_images: bool = False) -> int:
        """POST /Items/{Id}/Refresh，返回状态码（实测 204）。"""
        params = {"ReplaceAllImages": str(replace_images).lower()}
        r = self._c.post(f"/Items/{item_id}/Refresh", params=params)
        return r.status_code

    def refresh_path(self, path: str, container_path: str | None = None) -> dict:
        """按容器内路径定位条目并精确刷新。

        返回 {"mode": "item"|"none", "item_id":..., "lib":..., "status":...}
        """
        probe = container_path or path
        item = self.find_by_path(probe)
        if item is None and container_path and container_path != path:
            item = self.find_by_path(path)
        if item is None:
            return {"mode": "none", "item_id": None, "reason": f"库内按 Path 未匹配到条目: {probe}"}
        iid = str(item.get("Id"))
        status = self.refresh_item(iid)
        return {
            "mode": "item",
            "item_id": iid,
            "name": item.get("Name"),
            "path": item.get("Path"),
            "status": status,
            "ok": status in (200, 204),
        }

    def ping(self) -> bool:
        try:
            return self._c.get("/System/Info").status_code == 200
        except httpx.HTTPError:
            return False
