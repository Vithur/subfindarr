"""核心纯逻辑自测：指纹解析、评分、压缩包处理、路径映射。"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.fingerprint import from_arr_payload, parse_release_name  # noqa: E402
from app.pack import detect_archive, extract, install, pick_best, sniff_encoding, target_name  # noqa: E402
from app.scoring import rank, score  # noqa: E402
from app.sources.base import SubtitleCandidate  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def t_parse() -> None:
    p = parse_release_name("The.Matrix.1999.2160p.UHD.BluRay.x265.HDR.mkv")
    check("解析片名", p["title"].lower().startswith("the matrix"), str(p))
    check("解析年份", p["year"] == 1999, str(p))
    check("解析分辨率", p["resolution"] == "2160p", str(p))

    p2 = parse_release_name("Some Show - S02E07 - 1080p WEB-DL x264.mkv")
    check("解析季集", (p2["season"], p2["episode"]) == (2, 7), str(p2))

    p3 = parse_release_name("电影名.2023.{tmdb-12345}.1080p.mkv")
    check("解析 tmdb id", p3["tmdb_id"] == "12345", str(p3))


def t_payload() -> None:
    radarr = {
        "eventType": "Download",
        "movie": {"title": "流浪地球2", "year": 2023, "tmdbId": 843527, "imdbId": "tt13539646"},
        "movieFile": {"path": "/data/media/movies/流浪地球2 (2023)/流浪地球2.2023.1080p.mkv"},
    }
    mi = from_arr_payload(radarr)
    check("radarr payload 解析", mi is not None and mi.media_type == "movie", str(mi))
    assert mi
    check("radarr tmdb 指纹", "tmdb" not in mi.fingerprint, "")
    check("radarr 标题", mi.title == "流浪地球2", mi.title)

    sonarr = {
        "eventType": "Download",
        "series": {"title": "三体", "year": 2023, "tmdbId": 108545},
        "episodes": [{"seasonNumber": 1, "episodeNumber": 5}],
        "episodeFile": {"path": "/data/media/tv/三体/Season 01/三体 - S01E05 - 1080p.mkv"},
    }
    mi2 = from_arr_payload(sonarr)
    check("sonarr payload 解析", mi2 is not None and mi2.media_type == "episode", str(mi2))
    assert mi2
    check("sonarr 季集", (mi2.season, mi2.episode) == (1, 5), str(mi2))

    check("Test 事件被忽略", from_arr_payload({"eventType": "Test"}) is None)

    # 指纹稳定性
    check("指纹稳定", mi2.fingerprint == from_arr_payload(sonarr).fingerprint)


def t_score() -> None:
    """按 Kernel 的三级语言规则：语言取自列表页标签，版本页标题不作来源。"""
    # 官方简中单语：列表页给了「简体」标签 + 官方 badge
    good = SubtitleCandidate(source="subhd", source_id="1", title="流浪地球2",
                             lang_hint="简体", fmt="srt",
                             raw={"lang_source": "list", "official": True})
    # 简繁双语
    bil = SubtitleCandidate(source="subhd", source_id="4", title="流浪地球2",
                            lang_hint="双语 简体 繁体", fmt="srt",
                            raw={"lang_source": "list"})
    # 纯繁
    hant = SubtitleCandidate(source="subhd", source_id="3", title="流浪地球2",
                             lang_hint="繁体", fmt="srt", raw={"lang_source": "list"})
    # AI 机翻：标题命中黑名单
    bad_ai = SubtitleCandidate(source="subhd", source_id="2", title="流浪地球2 AI 机翻字幕",
                               lang_hint="简体", fmt="srt", raw={"lang_source": "list"})
    # 只有版本页标题、没有标签 → 必须落 unknown 且排在后面
    no_tag = SubtitleCandidate(source="subhd", source_id="5", title="流浪地球2 官方简体中文单语",
                               fmt="srt")

    s_good, s_ai, s_hant, s_bil = score(good), score(bad_ai), score(hant), score(bil)
    s_none = score(no_tag)
    check("列表页标签的简中单语 > 双语", s_good.total > s_bil.total, f"{s_good.total} vs {s_bil.total}")
    check("双语高于纯繁", s_bil.total > s_hant.total, f"{s_bil.total} vs {s_hant.total}")
    check("AI 字幕被压到底", s_ai.total < s_hant.total, f"{s_ai.total} vs {s_hant.total}")
    check("AI 标记", any(f.startswith("ai:") for f in s_ai.flags), str(s_ai.flags))
    check("标题里的语言不算数（无标签=unknown）", s_none.tier == "unknown", s_none.tier)
    check("unknown 排在有标签候选之后", s_none.total < s_hant.total, f"{s_none.total} vs {s_hant.total}")
    check("unknown 带 lang-unknown 标记", "lang-unknown" in s_none.flags, str(s_none.flags))

    ranked = rank([hant, bad_ai, bil, good, no_tag])
    check("排序首位=官方简中单语", ranked[0][0].source_id == "1", ranked[0][0].title)
    check("语言来源可追溯", ranked[0][1].breakdown.get("lang_source") == "list",
          str(ranked[0][1].breakdown))

    # 降级来源：列表页拉不到时用文件名
    from_name = SubtitleCandidate(source="subhd", source_id="6", title="流浪地球2",
                                  raw={"files": ["x.chs.srt", "x.cht.srt"]})
    s_fn = score(from_name)
    check("文件名降级判出双语", s_fn.tier == "hans_hant_bilingual", s_fn.tier)
    check("文件名来源标记", s_fn.breakdown.get("lang_source") == "filename", str(s_fn.breakdown))


def t_pack() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("sub/流浪地球2.chs.srt", "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8"))
        z.writestr("sub/流浪地球2.cht.srt", "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8"))
        z.writestr("readme.txt", b"nope")
    zdata = buf.getvalue()
    check("识别 zip", detect_archive(zdata[:16]) == "zip")

    tmp = Path("/tmp/sfa-test")
    if tmp.exists():
        import shutil

        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    zp = tmp / "a.zip"
    zp.write_bytes(zdata)
    files = extract(zp, tmp / "out")
    check("解出字幕 2 个", len(files) == 2, str(files))
    best = pick_best(files)
    check("选简中", "chs" in best.name, best.name)

    gbk = "1\n00:00:01,000 --> 00:00:02,000\n你好世界\n".encode("gb18030")
    check("GBK 编码探测", sniff_encoding(gbk) in ("gb18030", "cp936"), sniff_encoding(gbk))

    video = Path("/media/movies/流浪地球2 (2023)/流浪地球2.2023.1080p.mkv")
    check("目标名（zh-cn 为落地首选）",
          target_name(video, ".zh-cn.srt").name == "流浪地球2.2023.1080p.zh-cn.srt",
          target_name(video, ".zh-cn.srt").name)
    out = install(best, video, tmp / "dest", ".zh-cn.srt")
    check("落盘", out.exists() and out.name.endswith(".zh-cn.srt"), str(out))


def t_pathmap() -> None:
    from app.config import load_config

    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    check("配置加载 media_root=/media", cfg["paths"]["media_root"] == "/media", str(cfg["paths"]))
    check("cookie 环境变量展开", isinstance(cfg["sources"]["subhd"]["cookie"], str), "")

    from app.db import Ledger

    led = Ledger("/tmp/sfa-test/test.db")
    a = led.upsert_job(fingerprint="fp1", media_type="movie", video_path="/media/x.mkv")
    b = led.upsert_job(fingerprint="fp1", media_type="movie", video_path="/media/x.mkv")
    led.set_job_status("fp1", "done")
    c = led.upsert_job(fingerprint="fp1", media_type="movie", video_path="/media/x.mkv")
    check("台账 created", a == "created", a)
    check("台账 requeued", b == "requeued", b)
    check("台账已完成后 exists", c == "exists", c)
    check("统计", led.stats().get("done") == 1, str(led.stats()))


def t_path_mapping() -> None:
    """arr 传来的路径映射到本容器媒体根。

    映射表全部来自 `paths.path_map`；代码默认只认 arr 容器视角的 `/data/media`。
    宿主路径要映射，必须在配置里显式登记。
    """
    from app.config import load_config
    from app.pipeline import Pipeline

    # 默认配置：只有 /data/media
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    pl = Pipeline(cfg, ledger=None, adapters=[])  # type: ignore[arg-type]
    cases = {
        "/data/media/movies/Film (2023)/x.mkv": "/media/movies/Film (2023)/x.mkv",
        "/media/movies/already/mapped.mkv": "/media/movies/already/mapped.mkv",
    }
    for src, want in cases.items():
        got = str(pl._to_container_path(Path(src)))
        check(f"路径映射 {src[:28]}", got == want, f"{got} != {want}")

    # 显式登记宿主前缀后，宿主视角路径也能映射
    cfg2 = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    cfg2["paths"]["path_map"] = {"/data/media": "/media", "/host/media": "/media"}
    pl2 = Pipeline(cfg2, ledger=None, adapters=[])  # type: ignore[arg-type]
    got2 = str(pl2._to_container_path(Path("/host/media/tv/Show/Season 01/x.mkv")))
    check("自定义宿主前缀映射", got2 == "/media/tv/Show/Season 01/x.mkv", got2)

    # 未登记的前缀原样返回，不瞎猜
    got3 = str(pl2._to_container_path(Path("/unknown/place/x.mkv")))
    check("未知前缀不瞎改", got3 == "/unknown/place/x.mkv", got3)


def t_scoring_samples() -> None:
    """Kernel 指定的词表回归用例（样本原样取自 `subhd-ranking-samples.md`）。

    应排最前：Ksk2N5（繁英自校）、6XeBEg（自称官方简繁英粵）
    应垫底：kALC5K（HDTS 枪版）、UKG2BL（3D 组广告）
    """
    good_a = SubtitleCandidate(source="subhd", source_id="Ksk2N5",
                               title="[02:25:00] 蜘蛛人：重生日 Spider-Man Brand New Day (2026) 專注台灣習慣用詞的繁英雙語字幕",
                               lang_hint="繁体 英语", fmt="srt", raw={"lang_source": "list"})
    good_b = SubtitleCandidate(source="subhd", source_id="6XeBEg",
                               title="蜘蛛侠：崭新之日.Spider-Man.Brand.New.Day.2026.官方简繁英粵",
                               lang_hint="简体 繁体 英语", fmt="ass",
                               raw={"lang_source": "list", "official": True})
    cam = SubtitleCandidate(source="subhd", source_id="kALC5K",
                            title="蜘蛛侠：崭新之日.Spider-Man.Brand.New.Day.2026.1080p.HDTS.DD2.0.H.264-LMNTRY.chs",
                            lang_hint="简体", fmt="srt", raw={"lang_source": "list"})
    # 广告特征在**预览内容**里，不在标题里（Kernel 样本：标题只写 3D 组名）
    ad = SubtitleCandidate(source="subhd", source_id="UKG2BL",
                           title="3D蜘蛛侠：崭新之日.中英字幕.Spider-Man.Brand.New.Day.2026.3D.1080p.WEBRipx264-3DYC",
                           lang_hint="简体", fmt="srt",
                           raw={"lang_source": "list",
                                "preview": "热爱电影，诚意为3D影迷精心制作\n联系QQ号:484589941 QQ群：168290174"})
    graphic = SubtitleCandidate(source="subhd", source_id="xDm0mo",
                                title="Spider-Man.Brand.New.Day.2026.1080p.AMZN.WEB-DL.H.264-LEN-X中英特效字幕",
                                lang_hint="简体", fmt="sup", raw={"lang_source": "list"})

    sc = {c.source_id: s for c, s in rank([cam, ad, graphic, good_a, good_b])}
    check("枪版被罚（cam_penalty）", sc["kALC5K"].breakdown.get("cam_penalty") is not None,
          str(sc["kALC5K"].breakdown))
    check("预览含QQ群被罚（ad_penalty）", sc["UKG2BL"].breakdown.get("ad_penalty") is not None,
          str(sc["UKG2BL"].breakdown))
    check("图形字幕被罚（graphic_penalty）", sc["xDm0mo"].breakdown.get("graphic_penalty") is not None,
          str(sc["xDm0mo"].breakdown))
    check("枪版分数低于官方候选", sc["kALC5K"].total < sc["6XeBEg"].total,
          f"{sc['kALC5K'].total} vs {sc['6XeBEg'].total}")
    check("广告候选分数低于官方候选", sc["UKG2BL"].total < sc["6XeBEg"].total,
          f"{sc['UKG2BL'].total} vs {sc['6XeBEg'].total}")
    check("图形字幕垫底", sc["xDm0mo"].total == min(s.total for s in sc.values()),
          str({k: round(v.total, 1) for k, v in sc.items()}))
    check("官方简繁英粵在前列", sc["6XeBEg"].total >= sorted((s.total for s in sc.values()))[-2],
          str({k: round(v.total, 1) for k, v in sc.items()}))


def main() -> int:
    t_parse()
    t_payload()
    t_score()
    t_scoring_samples()
    t_pack()
    t_pathmap()
    t_path_mapping()
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
