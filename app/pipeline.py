"""主流程：收到事件 → 查源 → 选优 → 下载 → 解压 → 落盘 → 通知 Emby。"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from . import pack, scoring
from .api import EmbyClient
from .db import Ledger
from .fetcher import Fetcher, parse_cookie_header
from .fingerprint import MediaInfo, from_arr_payload
from .pack import detect_sub_format
from .sources.base import (CandidateRejected, MediaQuery, NotConfigured,
                           SourceAdapter, SourceError)

log = logging.getLogger("subfindarr.pipeline")


class Pipeline:
    def __init__(self, cfg: dict, ledger: Ledger, adapters: list[SourceAdapter]) -> None:
        self.cfg = cfg
        self.ledger = ledger
        self.adapters = adapters
        self.paths = cfg.get("paths", {})
        self.write_cfg = cfg.get("write", {})
        self.naming = cfg.get("naming", {})
        self.subs_out = Path(self.paths.get("subs_out", "/subs-out"))
        self.media_root = Path(self.paths.get("media_root", "/data"))
        self.emby_cfg = cfg.get("emby", {})
        self.emby = (
            EmbyClient(
                self.emby_cfg.get("url", ""),
                self.emby_cfg.get("api_key", ""),
                libraries=self.emby_cfg.get("libraries") or None,
            )
            if self.emby_cfg.get("url") and self.emby_cfg.get("api_key")
            else None
        )

    # ---------- 入口 ----------

    def handle_event(self, payload: dict) -> dict:
        event = (payload.get("eventType") or "").strip()
        # 改名/升级：arr 主动告知新旧文件名，把字幕跟着改前缀（根因修复，不做破坏性动作）
        if event in ("Rename", "MovieFileRename", "EpisodeFileRename"):
            return self.handle_rename(payload)
        info = from_arr_payload(payload)
        if info is None:
            return {"ok": True, "skipped": "event 不含可处理文件或属于 Test/Health"}
        return self.process(info, arr=_arr_of(payload))

    def handle_rename(self, payload: dict) -> dict:
        """跟随 arr 改名：把该片目录下的中文字幕改成新前缀。

        Chief 定的四步：拿目录+新旧名 → 按**识别别名集**找同片中文字幕（不硬编码扩展名）
        → 改名成新前缀（保留别名与扩展名）→ 改名前后台账各记一条；失败只 WARN **不动文件**。
        另：不做「扫目录删孤儿」——判据易误伤，风险与收益不成比例。
        """
        old_path, new_path = _renamed_paths(payload)
        if not new_path:
            log.warning("Rename 事件缺少新文件名，跳过: keys=%s", sorted(payload.keys())[:12])
            return {"ok": True, "action": "skip-rename-no-path"}
        new_v = self._to_container_path(Path(new_path))
        old_v = self._to_container_path(Path(old_path)) if old_path else None
        log.info("收到改名事件: 旧=%s 新=%s", old_v, new_v)

        # 目录里找「同片名主体 + 中文别名」的字幕（兼容改名后前缀不一致）
        exts = tuple(self.naming.get("extensions", [".srt", ".ass", ".ssa", ".sub"]))
        cands = pack.existing_subs_loose(new_v, exts, self._is_zh_sub, self._zh_aliases())
        # 老前缀完全等值的那一份也要带上（目录里可能只有它）
        if old_v is not None:
            for p in pack.existing_subs(old_v, exts):
                if self._is_zh_sub(p.name) and p not in cands:
                    cands.append(p)
        if not cands:
            log.info("改名后目录里没有中文字幕，无需处理: %s", new_v.parent)
            return {"ok": True, "action": "rename-no-subs"}

        moved: list[dict] = []
        for old_sub in cands:
            # 别名段（.zh-cn/.chs/…）+ 扩展名保留，只换前缀
            alias = self._alias_of(old_sub.name)
            new_sub = new_v.with_name(new_v.stem + alias)
            self.ledger.add_log("rename", "before", str(old_sub), str(new_sub))
            try:
                if old_sub.resolve() == new_sub.resolve():
                    continue
                if new_sub.exists():
                    log.warning("目标已存在，跳过改名以免覆盖: %s", new_sub)
                    continue
                old_sub.rename(new_sub)
            except OSError as e:
                # Chief 定：失败只 WARN，**不动文件**
                log.warning("字幕改名失败（保持原样）: %s -> %s :: %s", old_sub, new_sub, e)
                self.ledger.add_log("rename", "failed", str(old_sub), str(new_sub), str(e))
                continue
            self.ledger.add_log("rename", "after", str(old_sub), str(new_sub))
            # 台账 out_path 跟着更新，避免台账与磁盘长期不一致
            self.ledger.update_sub_path(str(old_sub), str(new_sub))
            moved.append({"from": old_sub.name, "to": new_sub.name})
            log.info("字幕已跟随改名: %s → %s", old_sub.name, new_sub.name)

        return {"ok": True, "action": "renamed", "moved": moved,
                "count": len(moved)}

    def _alias_of(self, filename: str) -> str:
        """取字幕的「别名 + 扩展名」段（如 `.zh-cn.srt`），用于改名时保留。"""
        for a in self._zh_aliases():
            base = a[1:] if a.startswith(".") else a
            idx = filename.lower().rfind("." + base.lower() + ".")
            if idx >= 0:
                return filename[idx:]
        # 兜底：无别名时保留「.中文名.扩展名」形态
        return "." + ".".join(Path(filename).name.split(".")[-2:])

    def process(self, info: MediaInfo, arr: str | None = None) -> dict:
        fp = info.fingerprint
        action = self.ledger.upsert_job(
            fingerprint=fp,
            media_type=info.media_type,
            video_path=info.video_path,
            title=info.title,
            year=info.year,
            season=info.season,
            episode=info.episode,
            arr=arr,
        )
        if action == "exists" and not self.write_cfg.get("force"):
            return {"ok": True, "fingerprint": fp, "action": "skip-exists"}

        video = Path(info.video_path)
        container_video = self._to_container_path(video)

        # Chief 定：**先判条目类型，再谈文件存在与否**。strm 型（媒体不在本地）
        # 对外挂字幕是死的，写字幕没有任何条目会扫到；刷新返回 204 也是假象，
        # 必须整条跳过并留可查状态，不能落成 failed/success。
        strm_reason = self._strm_reason(container_video)
        if strm_reason:
            log.warning("strm 条目不支持外挂字幕，跳过: %s（%s）", container_video, strm_reason)
            self.ledger.set_job_status(fp, "skipped_strm", strm_reason)
            return {"ok": True, "fingerprint": fp, "action": "skip-strm",
                    "reason": strm_reason, "path": str(container_video)}

        # 已有合格字幕则跳过。
        # ★ 用**前缀模糊匹配**（按片名主体，忽略质量标记）：arr 改名/质量升级会换
        # 文件名（Bluray-2160p → WEBDL-2160p），完全等值判断会漏判成「无字幕」而白下重下。
        exts = tuple(self.naming.get("extensions", [".srt", ".ass", ".ssa", ".sub"]))
        if container_video.parent.exists() and not self.write_cfg.get("force"):
            zh = pack.existing_subs_loose(container_video, exts, self._is_zh_sub,
                                          self._zh_aliases())
            if zh:
                self.ledger.set_job_status(fp, "skipped", f"已有字幕 {zh[0].name}")
                return {"ok": True, "fingerprint": fp, "action": "skip-has-subs",
                        "found": [p.name for p in zh], "match": "loose"}

        if not container_video.exists():
            self.ledger.set_job_status(fp, "failed", f"视频文件不存在: {container_video}")
            return {"ok": False, "fingerprint": fp, "error": f"视频不存在: {container_video}"}

        query = self._build_query(info)
        scoring_cfg = self.cfg.get("scoring", {})

        errors: list[str] = []
        for adapter in self.adapters:
            try:
                result = self._try_source(adapter, query, info, container_video, scoring_cfg)
                if result is not None:
                    # Chief 定：只有**真正写进媒体库**才调刷新。
                    # fallback 到 subs-out 时 Emby 看不到该文件，刷新是假成功，必须跳过并留痕。
                    if result.get("fallback"):
                        log.warning("字幕回退到 subs_out（未进媒体库），跳过 Emby 刷新: %s",
                                    result.get("out"))
                        self.ledger.mark_fallback(fp, "写入媒体库失败，回退 subs_out")
                        self.ledger.set_job_status(fp, "fallback", "写入媒体库失败，回退 subs_out")
                        return {"ok": True, "fingerprint": fp, "action": "installed-fallback",
                                **result, "emby": {"mode": "skipped", "reason": "fallback 未进媒体库"}}
                    refreshed = self._notify_emby(container_video)
                    # 终态由**回查结果**决定，不能写入就先记 done：
                    # 回查没看到字幕流时记 refresh_pending（不进 success 计数），
                    # 否则会出现「台账全绿但没字幕」。
                    if refreshed.get("status_detail") == "refresh_pending":
                        self.ledger.set_job_status(fp, "refresh_pending",
                                                   refreshed.get("reason", "回查未见字幕流"))
                    else:
                        self.ledger.set_job_status(fp, "done")
                    return {"ok": True, "fingerprint": fp, "action": "installed", **result,
                            "emby": refreshed}
            except NotConfigured as e:
                errors.append(str(e))
                log.info("源未启用/未配置，跳过: %s", e)
            except SourceError as e:
                errors.append(str(e))
                log.warning("源处理失败: %s evidence=%s", e, e.evidence)
            except Exception as e:  # noqa: BLE001 - 单源失败不影响其它源
                errors.append(f"[{adapter.name}] {e}")
                log.exception("源异常: %s", adapter.name)

        self.ledger.set_job_status(fp, "failed", "; ".join(errors)[:800])
        return {"ok": False, "fingerprint": fp, "errors": errors}

    # ---------- 内部 ----------

    def _try_source(
        self,
        adapter: SourceAdapter,
        query: MediaQuery,
        info: MediaInfo,
        video: Path,
        scoring_cfg: dict,
    ) -> dict | None:
        cands = adapter.search(query)
        if not cands:
            return None
        ranked = scoring.rank(cands, scoring_cfg)
        min_score = float((self.cfg.get("sources", {}).get(adapter.name, {}) or {}).get("min_score", 60))
        ranked = self._disambiguate_year(adapter, info, ranked, scoring_cfg)

        with tempfile.TemporaryDirectory(prefix="subfindarr-") as tmp:
            for cand, sc in ranked:
                if sc.total < min_score:
                    log.info("全部候选低于阈值 %.1f（最高 %.1f），放弃该源", min_score, ranked[0][1].total)
                    break
                try:
                    resolved = adapter.resolve(cand)
                    # resolve 会把预览正文塞进 raw["preview"]；命中广告/水印就换下一个，
                    # 别把带 QQ 群水印的字幕装进库（评分在下载前已罚，这里是下载前的最后一道）。
                    ad_hit = scoring.ad_hit(resolved, scoring_cfg)
                    if ad_hit:
                        log.warning("候选含广告水印，跳过: %s :: %s", cand.title, ad_hit)
                        continue
                    dl = adapter.download(resolved, tmp)
                    files = pack.extract(dl.path, Path(tmp) / "x")
                    best = pack.pick_best(files)
                    out, fell_back = self._install(best, video, self._target_tag())
                    sha = _sha256(out)
                    self.ledger.add_sub(
                        fingerprint=info.fingerprint,
                        source=adapter.name,
                        source_id=cand.source_id,
                        lang=sc.tier,
                        title=cand.title,
                        score=sc.total,
                        score_detail=sc.as_dict(),
                        out_path=str(out),
                        sha256=sha,
                        fallback=1 if fell_back else 0,
                    )
                    if fell_back:
                        log.warning("字幕未进媒体库，落到 subs_out: %s ← %s", out, cand.title)
                    log.info("已装字幕 %s ← %s (%s, %.1f, %s)", out, cand.title, adapter.name, sc.total, sc.flags)
                    return {
                        "source": adapter.name,
                        "candidate": cand.title,
                        "score": sc.total,
                        "tier": sc.tier,
                        "flags": sc.flags,
                        "out": str(out),
                        "sha256": sha,
                        "fallback": fell_back,
                    }
                except CandidateRejected as e:
                    # 候选被主动拒收（广告水印等）——是「跳过」不是「故障」
                    log.info("候选不合格，跳过: %s :: %s", cand.title, e)
                    continue
                except (SourceError, ValueError, OSError) as e:
                    log.warning("候选失败，试下一个: %s :: %s", cand.title, e)
                    continue
        return None

    def _strm_reason(self, container_video: Path) -> str | None:
        """判断是否 strm 型条目；是则返回原因字符串，否则 None。

        判据（Chief 2026-10-16 定，Ops 实测 52897/52903）：
        - 落点路径以 `/strm/` 开头，或
        - 文件后缀是 `.strm`，或
        - Emby 侧条目 MediaSources 指向 http 协议

        strm 的媒体不在本地，外挂字幕没有任何条目会扫到；刷新即使返 204 也是假象。
        """
        p = str(container_video)
        if p.startswith("/strm/") or "/strm/" in p:
            return f"路径属于 strm 库: {p}"
        if container_video.suffix.lower() == ".strm":
            return f"目标本身是 .strm 文件: {p}"
        # 本地只有 .strm 没有真视频的目录，也按 strm 处理
        try:
            if container_video.parent.exists():
                siblings = list(container_video.parent.glob("*.strm"))
                real = [f for f in container_video.parent.iterdir()
                        if f.is_file() and f.suffix.lower() in (".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov", ".m4v", ".wmv")]
                if siblings and not real:
                    return f"目录内只有 .strm、无本地视频文件: {container_video.parent}"
        except OSError:
            pass
        if self.emby is not None:
            try:
                item = self.emby.find_by_path(p)
                if item is not None:
                    srcs = self.emby.media_source_protocols(str(item.get("Id")))
                    for s in srcs:
                        proto = str(s.get("protocol") or "").lower()
                        spath = str(s.get("path") or "").lower()
                        if proto in ("http", "https") or spath.startswith(("http://", "https://")):
                            return f"Emby 条目媒体源是远程 URL（{proto or spath[:40]}）"
            except Exception as e:  # noqa: BLE001 - 判型失败不阻断主链
                log.debug("strm 判型时查询 Emby 失败: %s", e)
        return None

    def _disambiguate_year(self, adapter, info: MediaInfo, ranked, scoring_cfg: dict):
        """同名不同片消歧：只对评分前 N 个候选回查真实年份，年份只作加分不硬过滤。

        Kernel 实测背景：搜索页平铺混排同名不同片（Josee 2003 / 2020 是两个独立 /d/{id}），
        标题里的年份缺失或不可靠，所以不能靠标题硬过滤；
        真源是「版本页 /a/{sid} → 归属影视页 /d/{id} → h1 片名 (YYYY)」。

        成本控制：每个候选多 1~2 个请求，所以只看前 N 个（默认 3）。
        """
        want = info.year
        if not want or not hasattr(adapter, "probe_year"):
            return ranked
        top_n = int((self.cfg.get("disambiguation", {}) or {}).get("probe_top_n", 3))
        bonus = float((self.cfg.get("disambiguation", {}) or {}).get("year_match_bonus", 25))
        penalty = float((self.cfg.get("disambiguation", {}) or {}).get("year_mismatch_penalty", 30))

        out = []
        for i, (cand, sc) in enumerate(ranked):
            if i >= top_n:
                out.append((cand, sc))
                continue
            try:
                probed = adapter.probe_year(cand)
            except Exception as e:  # noqa: BLE001 - 消歧失败不该阻断主流程
                log.warning("年份回查异常(%s): %s", cand.source_id, e)
                probed = None
            if probed is None:
                out.append((cand, sc))
                continue
            if probed == want:
                sc.total += bonus
                sc.breakdown["year_match"] = bonus
                sc.flags.append(f"year:{probed}")
            else:
                sc.total -= penalty
                sc.breakdown["year_mismatch"] = -penalty
                sc.flags.append(f"year!={probed}")
            out.append((cand, sc))

        out.sort(key=lambda t: t[1].total, reverse=True)
        return out

    def _target_tag(self) -> str:
        """写入用后缀＝naming.preferred 的第一项（其余仅作识别别名）。"""
        tags = self.naming.get("preferred", [".zh-cn.srt"])
        return tags[0] if tags else ".zh-cn.srt"

    def _zh_aliases(self) -> list[str]:
        """已有中文字幕的识别别名。

        naming.aliases 专门管「哪些名字算已经有字幕了」（不含扩展名）；
        naming.preferred 只决定「写出去叫什么」。两者分开，避免 preferred 只写了
        `.zh-cn.srt` 就认不出 `.chs.ass`。
        """
        out: list[str] = []
        for a in self.naming.get("aliases", []) or []:
            v = str(a).lower().strip()
            if v and v not in out:
                out.append(v)
        if not out:  # 没配 aliases 时退回 preferred
            for t in self.naming.get("preferred", []) or []:
                v = str(t).lower()
                if v.endswith((".srt", ".ass", ".ssa", ".sub")):
                    v = v.rsplit(".", 1)[0]
                if v and v not in out:
                    out.append(v)
        return out

    def _is_zh_sub(self, filename: str) -> bool:
        """判「已有中文字幕」。

        字幕别名紧邻扩展名结尾（`片名 (2020).zh-cn.srt`），用**后缀**判断最稳：
        文件名里可能含各种点（版本号、括号年份），拆片段会误判。
        """
        low = filename.lower()
        for alias in self._zh_aliases():
            # 别名形如 ".zh-cn.srt" / ".chs"（识别别名可能不带扩展名）
            a = alias.lower()
            if low.endswith(a):
                return True
            # 别名没带扩展名时，做成「.别名.」中缀判断
            base = a[1:] if a.startswith(".") else a
            if "." not in base and f".{base}." in low:
                return True
        # 中文语义词兜底（这些词不会出现在英文文件名里）
        return any(k in low for k in ("简体", "繁体", "简中", "中字", "简繁", "chinese"))

    def _install(self, src: Path, video: Path, preferred_tag: str) -> tuple[Path, bool]:
        """返回 (落点, 是否回退)。回退＝写进了 subs_out 而非媒体库。

        目标扩展名按**文件内容**判定，不只看源扩展名——真机踩过：
        包里解出的文件叫 `*.srt`，内容却是 ASS（`[Script Info]`），
        按名字落成 `.srt` 后 Emby 解析不出字幕流。
        """
        tag = preferred_tag or ".zh-cn.srt"
        real_ext = detect_sub_format(src) or src.suffix.lower()
        tag = str(Path(tag).with_suffix(real_ext))

        if self.write_cfg.get("in_place", True):
            if pack.writable(video.parent):
                return pack.install(src, video, video.parent, tag), False
            log.warning("媒体库目录不可写，无法就地写: %s", video.parent)
        if self.write_cfg.get("fallback_to_subs_out", True) or not self.write_cfg.get("in_place", True):
            return pack.install(src, video, self.subs_out, tag), True
        raise SourceError("pipeline", "install", f"媒体库目录不可写且未允许回退: {video.parent}")

    def _notify_emby(self, container_video: Path) -> dict:
        if self.emby is None:
            return {"mode": "none", "reason": "emby 未配置"}
        try:
            res = self.emby.refresh_path(str(container_video))
            if res.get("mode") != "item" or not res.get("item_id"):
                return res
            # 刷完回查字幕流，作为「Emby 是否真认出」的证据。
            # 204 只代表请求被接受，扫描是异步的 → 带延迟重试；查不到记 refresh_pending，
            # **不记失败**（Chief 定：避免把时序问题当功能故障）。
            delays = self.emby_cfg.get("verify_delays") or [5, 20, 45]
            iid = str(res["item_id"])
            for i, wait in enumerate(delays):
                time.sleep(float(wait))
                try:
                    streams = self.emby.media_sources(iid)
                except Exception as e:  # noqa: BLE001
                    log.debug("回查字幕流失败: %s", e)
                    streams = []
                if streams:
                    res["subtitle_streams"] = streams
                    res["verified_after_sec"] = sum(float(d) for d in delays[: i + 1])
                    return res
            res["subtitle_streams"] = []
            res["status_detail"] = "refresh_pending"
            res["reason"] = "刷新已接受，回查未见字幕流（判为扫描未完成，不记失败）"
            return res
        except Exception as e:  # noqa: BLE001
            log.warning("Emby 刷新失败: %s", e)
            return {"mode": "error", "reason": str(e)}

    def _build_query(self, info: MediaInfo) -> MediaQuery:
        return MediaQuery(
            media_type=info.media_type,
            title=info.title,
            original_title=info.extra.get("original_title", ""),
            year=info.year,
            season=info.season,
            episode=info.episode,
            tmdb_id=info.tmdb_id,
            imdb_id=info.imdb_id,
            aliases=info.extra.get("aliases", []),
        )

    def _to_container_path(self, host_path: Path) -> Path:
        """把 arr 传来的路径映射到本容器的媒体根。

        arr 可能给出两种视角的路径：它自己容器内的视角，或宿主机视角。
        两种前缀都要能被识别，否则落到错误位置（或判成「文件不存在」）。

        **映射表全部来自配置** `paths.path_map`（按最长前缀优先），代码里不含任何
        具体宿主路径——换部署环境只需改配置：

            paths:
              path_map:
                /data/media: /media          # arr 容器视角（默认已内置）
                /srv/media: /media           # 你的宿主路径，按需添加

        已在媒体根之下的路径原样返回。
        """
        p = str(host_path)
        root = str(self.media_root)
        if p == root or p.startswith(root.rstrip("/") + "/"):
            return host_path
        for prefix, target in self._path_map().items():
            pref = prefix.rstrip("/") + "/"
            if p.startswith(pref):
                return Path(target.rstrip("/") + "/" + p[len(pref):])
        return host_path

    def _path_map(self) -> dict[str, str]:
        """前缀映射表（按 key 长度降序，保证最长前缀优先）。

        config 里的 `paths.path_map` 为准；未配置时只留一条通用默认：
        arr 容器视角的 `/data/media`。**代码默认值不含任何具体部署路径。**
        """
        cfg = self.paths.get("path_map") if isinstance(self.paths, dict) else None
        out: dict[str, str] = {}
        for k, v in (cfg or {}).items():
            if k and v:
                out[str(k)] = str(v)
        if not out:
            out = {"/data/media": str(self.media_root)}
        return dict(sorted(out.items(), key=lambda kv: len(kv[0]), reverse=True))


    def build_fetcher(self, source_name: str) -> Fetcher:
        sc = self.cfg.get("sources", {}).get(source_name, {}) or {}
        fs = self.cfg.get("flaresolverr", {}) or {}
        net = self.cfg.get("network", {}) or {}
        cookie_raw = sc.get("cookie", "") or ""
        return Fetcher(
            fs_url=fs.get("url", ""),
            fs_timeout_ms=int(fs.get("max_timeout_ms", 90000)),
            fs_enabled=bool(fs.get("enabled", True)),
            cookies=parse_cookie_header(cookie_raw),
            proxy=net.get("proxy") or None,
            use_proxy=bool(net.get("use_proxy", True)),
            timeout=float(net.get("timeout_sec", 45)),
            retries=int(net.get("retries", 3)),
            backoff=float(net.get("backoff_sec", 1.5)),
        )



# ---------- 工具 ----------

_ZH_RE = re.compile(r"(zh|chs|cht|sc|tc|简体|繁体|简中|中字|chinese)", re.IGNORECASE)


def _is_zh(name: str) -> bool:
    return bool(_ZH_RE.search(name))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _renamed_paths(payload: dict) -> tuple[str | None, str | None]:
    """从 Rename/Upgrade payload 取 (旧路径, 新路径)。

    Ops 实测 schema：radarr 的 `movieFile` / sonarr 的 `episodeFile` 上都带
    `previousPath`（旧）与 `path`（新）——**直接用这两个字段，不要自己拼新旧名**。
    缺 `previousPath` 时旧的留 None（调用方会按目录做模糊匹配兜底）。
    """
    for key in ("movieFile", "episodeFile", "renamedMovieFile", "renamedEpisodeFile"):
        f = payload.get(key) or {}
        new = f.get("path")
        if new:
            return (str(f["previousPath"]) if f.get("previousPath") else None, str(new))
    return (None, None)


def _renamed_path_of(payload: dict) -> str | None:
    """取**新**文件路径（Rename 事件主入口用）。"""
    return _renamed_paths(payload)[1]


def _old_path_of(payload: dict) -> str | None:
    """取**旧**文件路径（用于兜底找老前缀那份字幕）。"""
    return _renamed_paths(payload)[0]


def _arr_of(payload: dict) -> str | None:
    if "movie" in payload:
        return "radarr"
    if "series" in payload or "episodeFile" in payload:
        return "sonarr"
    return None


def env_flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def now() -> float:
    return time.time()


def jsonable(obj: Any) -> Any:
    return obj
