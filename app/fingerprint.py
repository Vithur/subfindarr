"""指纹与媒体信息解析：从 arr payload + 文件名生成稳定匹配键。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# 常见发布组/质量标签，用于从文件名里剥离片名
_NOISE = re.compile(
    r"\b("
    r"2160p|1080p|720p|480p|4k|uhd|hdr|hdr10\+?|dv|dolby\.?vision|sdr|"
    r"bluray|blu-ray|bdrip|brrip|web-?dl|webrip|hdtv|dvdrip|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|10bit|8bit|"
    r"aac|ac3|eac3|ddp?5\.?1|dts(-hd)?|truehd|atmos|flac|"
    r"proper|repack|internal|extended|unrated|imax|"
    r"chinese|chs|cht|gb|big5|简体|繁体|双语|中字|字幕"
    r")\b",
    re.IGNORECASE,
)

_YEAR = re.compile(r"(?:^|[\s._(\[])((?:19|20)\d{2})(?:[\s._)\]]|$)")
_TMDB = re.compile(r"\{?tmdb[-=_ ]?(\d+)\}?", re.IGNORECASE)
_IMDB = re.compile(r"\{?imdb[-=_ ]?(tt\d+)\}?", re.IGNORECASE)
_SE = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
_SE_ALT = re.compile(r"(?:^|\D)(\d{1,2})x(\d{1,3})(?:\D|$)")
_VIDEO_EXT = {".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".wmv", ".mov", ".flv", ".iso", ".m4v"}


@dataclass
class MediaInfo:
    media_type: str                     # movie | episode
    title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    tmdb_id: str | None = None
    imdb_id: str | None = None
    resolution: str | None = None
    video_path: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        """稳定指纹：优先外部 ID，否则用 标题+年份+季集。"""
        if self.tmdb_id:
            key = f"tmdb:{self.tmdb_id}:{self.media_type}"
            if self.media_type == "episode":
                key += f":s{self.season:02d}e{self.episode:02d}" if self.season else ""
        elif self.imdb_id:
            key = f"imdb:{self.imdb_id}:{self.media_type}"
            if self.media_type == "episode":
                key += f":s{self.season:02d}e{self.episode:02d}" if self.season else ""
        else:
            t = re.sub(r"[^a-z0-9]+", "", (self.title or "").lower())
            key = f"n:{t}:{self.year or ''}:{self.media_type}"
            if self.media_type == "episode":
                key += f":s{self.season or 0:02d}e{self.episode or 0:02d}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def parse_release_name(name: str) -> dict:
    """从文件名（不含目录）解析片名/年份/分辨率/季集/外部 ID。"""
    stem = Path(name).stem
    out: dict = {"title": "", "year": None, "season": None, "episode": None,
                 "resolution": None, "tmdb_id": None, "imdb_id": None}

    m = _TMDB.search(stem)
    if m:
        out["tmdb_id"] = m.group(1)
    m = _IMDB.search(stem)
    if m:
        out["imdb_id"] = m.group(1)

    m = _SE.search(stem) or _SE_ALT.search(stem)
    if m:
        out["season"] = int(m.group(1))
        out["episode"] = int(m.group(2))

    m = _YEAR.search(stem)
    if m:
        out["year"] = int(m.group(1))

    m = re.search(r"\b(2160p|1080p|720p|480p|4k|uhd)\b", stem, re.IGNORECASE)
    if m:
        out["resolution"] = m.group(1).lower()

    # 片名：去掉 ID 段、年份之后的所有内容、噪声标签
    head = stem
    head = re.sub(r"\{?tmdb[-=_ ]?\d+\}?", " ", head, flags=re.IGNORECASE)
    head = re.sub(r"\{?imdb[-=_ ]?tt\d+\}?", " ", head, flags=re.IGNORECASE)
    if out["year"]:
        idx = head.find(str(out["year"]))
        if idx > 0:
            head = head[: idx + 4]
    head = _NOISE.sub(" ", head)
    head = re.sub(r"[Ss]\d{1,2}[Ee]\d{1,3}", " ", head)
    head = re.sub(r"[._\[\](){}]+", " ", head)
    # 去掉尾部残留的季集/数字段
    head = re.sub(r"\s+-\s*\d{1,2}\s*$", " ", head)
    head = re.sub(r"\s{2,}", " ", head).strip(" -_")
    out["title"] = head
    return out


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _VIDEO_EXT


def from_arr_payload(payload: dict) -> MediaInfo | None:
    """把 Sonarr/Radarr v3 Webhook payload 转成 MediaInfo。

    支持 onDownload / onImport / onUpgrade / Test。
    优先取 movieFile / episodeFile 的路径；拿不到就返回 None（调用方去走 API 兜底）。
    """
    event = payload.get("eventType") or ""
    if event in ("Test", "Health", "HealthRestored", "ApplicationUpdate", "ManualInteractionRequired"):
        return None

    if "movie" in payload:
        mv = payload["movie"] or {}
        # Rename/Upgrade 事件里新文件在 `renamedMovieFile`（缺失时退回 `movieFile`）
        mf = payload.get("renamedMovieFile") or payload.get("movieFile") or {}
        path = mf.get("path") or ""
        if not path:
            return None
        parsed = parse_release_name(Path(path).name)
        return MediaInfo(
            media_type="movie",
            title=mv.get("title") or parsed["title"] or "",
            year=mv.get("year") or parsed["year"],
            tmdb_id=str(mv.get("tmdbId")) if mv.get("tmdbId") else parsed["tmdb_id"],
            imdb_id=mv.get("imdbId") or parsed["imdb_id"],
            resolution=parsed["resolution"],
            video_path=path,
        )

    if "series" in payload or "episodeFile" in payload:
        se = payload.get("series") or {}
        ep = payload.get("episodes") or []
        ef = payload.get("episodeFile") or {}
        path = ef.get("path") or ""
        if not path:
            return None
        parsed = parse_release_name(Path(path).name)
        first = ep[0] if ep else {}
        season = first.get("seasonNumber")
        epno = first.get("episodeNumber")
        if season is None:
            season = parsed["season"]
        if epno is None:
            epno = parsed["episode"]
        return MediaInfo(
            media_type="episode",
            title=se.get("title") or parsed["title"] or "",
            year=(se.get("year") or parsed["year"]),
            season=season,
            episode=epno,
            tmdb_id=str(se.get("tmdbId")) if se.get("tmdbId") else parsed["tmdb_id"],
            imdb_id=se.get("imdbId") or parsed["imdb_id"],
            resolution=parsed["resolution"],
            video_path=path,
        )
    return None
