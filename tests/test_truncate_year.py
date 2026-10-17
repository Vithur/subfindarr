"""截断校验、图形字幕过滤、年份消歧的回归测试。

对应 Kernel 2026-10-17 交付的两条落地规则（见 subhd-truncate-year.md）。
"""

from __future__ import annotations

import io
import re
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import pack  # noqa: E402
from app.fetcher import check_download_complete, check_zip_complete  # noqa: E402
from app.fingerprint import MediaInfo  # noqa: E402
from app.sources.base import SubtitleCandidate  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in entries.items():
            z.writestr(n, d)
    return buf.getvalue()


def t_truncated_zip() -> None:
    tmp = Path(tempfile.mkdtemp())
    good = make_zip({"a.chs.srt": b"1\n00:00:01,000 --> 00:00:02,000\nhi\n"})
    gp = tmp / "good.zip"
    gp.write_bytes(good)

    ok, why = check_zip_complete(str(gp))
    check("完整 zip 通过", ok, why)

    bp = tmp / "trunc.zip"
    bp.write_bytes(good[: len(good) - 60])
    ok2, why2 = check_zip_complete(str(bp))
    check("截断 zip 被拦", not ok2, f"竟然通过: {why2}")
    check("截断后 PK 头仍在", bp.read_bytes()[:4] == b"PK\x03\x04")

    ok3, why3 = check_zip_complete(str(gp), expected_len=len(good) + 100)
    check("长度不符被拦", not ok3, why3)

    tail_junk = tmp / "tailjunk.zip"
    tail_junk.write_bytes(good + b"JUNKJUNK")
    ok4, why4 = check_zip_complete(str(tail_junk), expected_len=len(good) + 8)
    check("尾部拼接被拦", not ok4, f"竟然通过: {why4}")


def t_check_dispatch() -> None:
    tmp = Path(tempfile.mkdtemp())
    srt = tmp / "a.srt"
    data = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n"
    srt.write_bytes(data)
    ok, why = check_download_complete(str(srt), None, len(data), "application/x-subrip")
    check("裸字幕按长度判定（完整）", ok, why)
    ok2, why2 = check_download_complete(str(srt), None, len(data) + 50, "application/x-subrip")
    check("裸字幕长度不符被拦", not ok2, why2)

    z = tmp / "z.zip"
    z.write_bytes(make_zip({"x.srt": b"1\n"}))
    check("zip 分派到 EOCD 校验", check_download_complete(str(z), "zip")[0])


def t_graphic_filter() -> None:
    check("GRAPHIC_EXT 覆盖", {".sup", ".sub", ".idx", ".pgs"} <= pack.GRAPHIC_EXT,
          str(pack.GRAPHIC_EXT))
    check("is_graphic_sub 判 .sup", pack.is_graphic_sub("a.chs.sup"))
    check("is_graphic_sub 判 .idx", pack.is_graphic_sub("a.cht.idx"))
    check("_is_sub_name 拦图形字幕", not pack._is_sub_name("movie.chs.sup"))
    check("_is_sub_name 放行 srt", pack._is_sub_name("movie.chs.srt"))

    tmp = Path(tempfile.mkdtemp())
    files = []
    for n in ("movie.chs.sup", "movie.chs.sub", "movie.chs.idx", "movie.chs.srt"):
        p = tmp / n
        p.write_bytes(b"x")
        files.append(p)
    best = pack.pick_best(files)
    check("pick_best 选到 .srt", best.suffix == ".srt", best.name)


def t_year_parse() -> None:
    from app.sources.subhd import _year_from_title_page

    check("h1 取年份", _year_from_title_page("<h1>乔西的虎与鱼 (2020)</h1>") == 2020)
    check("全角括号", _year_from_title_page("<h1>某片（2003）</h1>") == 2003)
    check("无年份返回 None", _year_from_title_page("<h1>某片</h1>") is None)


def t_d_id_parse() -> None:
    """probe_year 从版本页取 d_id 必须是纯数字。

    真机踩过：group(1) 是带前缀的 /d/1428291，拼出 /d//d/1428291 → 404。
    """
    regex_src = r"href=['\"](?:/d/)?(\d+)['\"]"
    page = '<a href="/d/1428291">qiao xi</a>'
    m = re.search(regex_src, page)
    check("d_id 取到纯数字", m is not None and m.group(1) == "1428291",
          m.group(1) if m else "None")
    url = "https://subhd.tv/d/" + (m.group(1) if m else "")
    check("URL 拼接正确", url == "https://subhd.tv/d/1428291", url)
    check("无重复 /d/", url.count("/d/") == 1, url)

    # 源码里不能再用会带前缀的写法
    src = (ROOT / "app/sources/subhd.py").read_text(encoding="utf-8")
    seg = src[src.find("def probe_year"): src.find("def download")]
    check("probe_year 不用 group(1) 带前缀", "d_id = m.group(1)" not in seg or "(?:/d/)?" in seg,
          "检查 d_id 提取写法")


def t_year_probe() -> None:
    from app.db import Ledger
    from app.pipeline import Pipeline
    from app.scoring import rank

    cfg = {"paths": {"db": ":memory:"}, "sources": {}, "scoring": {},
           "disambiguation": {"probe_top_n": 3, "year_match_bonus": 25, "year_mismatch_penalty": 30}}
    led = Ledger(Path(tempfile.mkdtemp()) / "t.db")
    pl = Pipeline(cfg, led, [])

    class FakeAdapter:
        name = "fake"
        years = {"1": 2003, "2": 2020}

        def probe_year(self, cand):
            return self.years.get(cand.source_id)

    cands = [
        SubtitleCandidate(source="fake", source_id="1", title="Josee 2003", lang_hint="简体"),
        SubtitleCandidate(source="fake", source_id="2", title="Josee 2020", lang_hint="简体"),
    ]
    ranked = rank(cands, {})
    info = MediaInfo(media_type="movie", title="Josee", year=2020, video_path="/x.mkv")
    out = pl._disambiguate_year(FakeAdapter(), info, ranked, {})
    check("年份匹配的排第一", out[0][0].source_id == "2",
          str([(c.source_id, s.total) for c, s in out]))
    check("年份不符被降权", any("year!=2003" in f for f in out[-1][1].flags), str(out[-1][1].flags))
    check("匹配加分有记录", out[0][1].breakdown.get("year_match") == 25, str(out[0][1].breakdown))

    info2 = MediaInfo(media_type="movie", title="Josee", year=None, video_path="/x.mkv")

    class Boom:
        name = "fake"

        def probe_year(self, c):
            raise AssertionError("不该被调用")

    out2 = pl._disambiguate_year(Boom(), info2, ranked, {})
    check("无期望年份不发请求", len(out2) == len(ranked))

    class NoProbe:
        name = "fake"

    out3 = pl._disambiguate_year(NoProbe(), info, ranked, {})
    check("适配器不支持时原样返回", len(out3) == len(ranked))

    class Bad:
        name = "fake"

        def probe_year(self, c):
            raise RuntimeError("boom")

    out4 = pl._disambiguate_year(Bad(), info, ranked, {})
    check("probe 异常不阻断", len(out4) == len(ranked))


def main() -> int:
    t_truncated_zip()
    t_check_dispatch()
    t_graphic_filter()
    t_year_parse()
    t_d_id_parse()
    t_year_probe()
    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
