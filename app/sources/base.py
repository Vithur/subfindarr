"""字幕源适配器接口。

Kernel 定接口后实现各站点；工程侧只依赖这里的抽象与数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class SubtitleCandidate:
    """搜索结果里的一条候选。"""

    source: str                   # subhd | assrt | ...
    source_id: str                # 站点内唯一 id（详情页 id / 下载 id）
    title: str                    # 原始标题，评分要靠它
    page_url: str = ""            # 详情页
    download_url: str = ""        # 真实压缩包地址（若列表页已给）
    lang_hint: str = ""           # 站点标注的语言：简体/繁体/双语
    uploader: str = ""            # 发布者/字幕组
    fmt: str = ""                 # srt | assz | ass | sub | 未知
    downloads: int = 0
    rating: float | None = None
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"


@dataclass
class MediaQuery:
    """交给适配器的检索条件。"""

    media_type: str               # movie | episode
    title: str
    original_title: str = ""
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    tmdb_id: str | None = None
    imdb_id: str | None = None
    aliases: list[str] = field(default_factory=list)

    def search_terms(self) -> list[str]:
        terms: list[str] = []
        for t in [self.title, self.original_title, *self.aliases]:
            t = (t or "").strip()
            if t and t not in terms:
                terms.append(t)
        return terms


@dataclass
class DownloadResult:
    """下载产物：磁盘上的压缩包或单个字幕文件。"""

    path: str
    content_type: str | None = None
    magic: bytes = b""
    bytes_written: int = 0
    final_url: str = ""
    resume_rounds: int = 0
    complete: bool = True


@runtime_checkable
class SourceAdapter(Protocol):
    """三段式：search → resolve → download。

    实现约定：
    - 网络层统一走注入的 fetcher（自带直连优先 / CF 回退 FlareSolverr）。
    - 任何一步失败抛 SourceError，消息里带原始证据（URL、状态码、Content-Type）。
    - download 落盘前必须校验魔法字节，不符直接抛错，不许把 HTML 当压缩包。
    """

    name: str

    def search(self, query: MediaQuery) -> list[SubtitleCandidate]:
        """按标题/年份/季集检索，返回未评分的候选列表。"""
        ...

    def resolve(self, candidate: SubtitleCandidate) -> SubtitleCandidate:
        """详情页 → 真实下载地址的状态机（处理嵌套跳转）。"""
        ...

    def download(self, candidate: SubtitleCandidate, dest_dir: str) -> DownloadResult:
        """下载压缩包/字幕文件到 dest_dir。"""
        ...

    def probe_year(self, candidate: SubtitleCandidate) -> int | None:
        """可选：回查候选真正归属的影片年份（用于同名不同片消歧）。

        默认返回 None（适配器不支持）。实现者可以多花 1~2 个请求换准确性，
        调用方只对评分靠前的少数候选调用它。
        """
        return None


class SourceError(RuntimeError):
    """适配器可预期失败，带站点与证据。"""

    def __init__(self, source: str, stage: str, message: str, evidence: dict | None = None) -> None:
        super().__init__(f"[{source}/{stage}] {message}")
        self.source = source
        self.stage = stage
        self.evidence = evidence or {}


class NotConfigured(SourceError):
    """凭据/开关缺失，属于跳过而非失败。"""

    def __init__(self, source: str, why: str) -> None:
        super().__init__(source, "init", why)


class CandidateRejected(SourceError):
    """候选被主动拒收（广告水印、图形字幕等）——属于跳过，不是故障。

    与 SourceError 区分开，是为了让日志与台账能表达「这条不合格」而不是「这条出错」。
    """

    def __init__(self, source: str, stage: str, message: str, evidence: dict | None = None) -> None:
        super().__init__(source, stage, message, evidence)
