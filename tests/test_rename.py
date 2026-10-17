"""改名相关测试。

Chief 2026-10-18 拍板的两条：
1. `force=false` 的「已有字幕」判断必须**前缀模糊匹配**（按片名主体、忽略质量标记），
   否则 arr 改名（Bluray-2160p → WEBDL-2160p）后会白下重下一份；
2. 接 `Rename` 事件让字幕**跟随改名**（保留别名与扩展名、前后各记台账、失败只 WARN 不动文件）；
   不做「扫目录删孤儿」。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.db import Ledger  # noqa: E402
from app.pack import existing_subs, existing_subs_loose, title_body  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if detail and not cond else ""))
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)


ALIASES = [".zh-cn", ".zh-Hans", ".zh-CN", ".zh", ".chs", ".cht"]


def t_title_body() -> None:
    cases = [
        ("Spider-Man - Brand New Day (2026) Bluray-2160p.mkv",
         "spider man brand new day"),
        ("Spider-Man - Brand New Day (2026) WEBDL-2160p.mkv",
         "spider man brand new day"),
        ("The.Sheep.Detectives.2026.WEBDL-2160p.mkv",
         "the sheep detectives"),
        ("Josee, the Tiger and the Fish (2020) WEBDL-2160p.mkv",
         "josee, the tiger and the fish"),
    ]
    for name, want in cases:
        got = title_body(name, ALIASES)
        check(f"片名主体: {name[:34]}", got == want, f"{got!r} != {want!r}")
    check("两代质量标记主体相同",
          title_body("Movie (2020) Bluray-2160p.mkv", ALIASES)
          == title_body("Movie (2020) WEBDL-2160p.mkv", ALIASES), "应相同")
    check("字幕名与视频名主体相同",
          title_body("Movie (2020) Bluray-2160p.zh-cn.srt", ALIASES)
          == title_body("Movie (2020) WEBDL-2160p.mkv", ALIASES),
          f"{title_body('Movie (2020) Bluray-2160p.zh-cn.srt', ALIASES)!r}")


def t_loose_matching() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        new_video = d / "Spider-Man - Brand New Day (2026) WEBDL-2160p.mkv"
        new_video.write_bytes(b"x")
        old_sub = d / "Spider-Man - Brand New Day (2026) Bluray-2160p.zh-cn.srt"
        old_sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
        other = d / "别的片子 (2019) WEBDL-1080p.zh-cn.srt"
        other.write_text("x", encoding="utf-8")

        strict = existing_subs(new_video, (".srt",))
        check("严格等值：改名后认不出（这是原缺陷）", len(strict) == 0, str([p.name for p in strict]))

        loose = existing_subs_loose(new_video, (".srt",), None, ALIASES)
        check("模糊匹配：认出改名前的字幕", len(loose) == 1, str([p.name for p in loose]))
        check("模糊匹配不误伤别的片子", all("别的片子" not in p.name for p in loose),
              str([p.name for p in loose]))


def _pipeline(tmp: Path) -> Pipeline:
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    cfg["paths"]["subs_out"] = str(tmp / "subs-out")
    cfg["paths"]["db"] = str(tmp / "t.db")
    cfg["write"]["in_place"] = True
    return Pipeline(cfg, Ledger(cfg["paths"]["db"]), [])


def t_rename_follow() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp)
        d = tmp / "media" / "Spider-Man - Brand New Day (2026)"
        d.mkdir(parents=True)
        (d / "Spider-Man - Brand New Day (2026) WEBDL-2160p.mkv").write_bytes(b"x")
        old_sub = d / "Spider-Man - Brand New Day (2026) Bluray-2160p.zh-cn.srt"
        old_sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")

        payload = {
            "eventType": "Rename",
            "movie": {"title": "Spider-Man: Brand New Day", "year": 2026, "tmdbId": 969681},
            # Ops 实测 schema：movieFile 上同时带 previousPath（旧）与 path（新）
            "movieFile": {
                "previousPath": str(d / "Spider-Man - Brand New Day (2026) Bluray-2160p.mkv"),
                "path": str(d / "Spider-Man - Brand New Day (2026) WEBDL-2160p.mkv"),
            },
        }
        res = pl.handle_rename(payload)
        check("改名动作成功", res.get("action") == "renamed", str(res))
        check("改了一件事", res.get("count") == 1, str(res))
        new_sub = d / "Spider-Man - Brand New Day (2026) WEBDL-2160p.zh-cn.srt"
        check("新名字幕存在", new_sub.exists(), str(list(d.iterdir())))
        check("旧名字幕已不在", not old_sub.exists(), str(list(d.iterdir())))
        check("别名与扩展名保留", new_sub.name.endswith(".zh-cn.srt"), new_sub.name)

        logs = pl.ledger.action_logs("rename")
        phases = [r["phase"] for r in logs]
        check("台账记了 before/after", "before" in phases and "after" in phases, str(phases))


def t_rename_keeps_when_target_exists() -> None:
    """目标已存在时跳过改名——绝不覆盖用户文件。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp)
        d = tmp / "media" / "Movie (2020)"
        d.mkdir(parents=True)
        (d / "Movie (2020) WEBDL-2160p.mkv").write_bytes(b"x")
        old_sub = d / "Movie (2020) Bluray-2160p.zh-cn.srt"
        old_sub.write_text("old", encoding="utf-8")
        new_sub = d / "Movie (2020) WEBDL-2160p.zh-cn.srt"
        new_sub.write_text("existing", encoding="utf-8")

        res = pl.handle_rename({
            "eventType": "Rename",
            "movie": {"title": "Movie", "year": 2020},
            "movieFile": {"path": str(d / "Movie (2020) WEBDL-2160p.mkv")},
        })
        check("目标存在时不覆盖", new_sub.read_text(encoding="utf-8") == "existing", "内容被改了")
        check("原文件保留", old_sub.exists(), str(list(d.iterdir())))
        check("本次没有移动", res.get("count") == 0, str(res))


def t_rename_no_subs() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp)
        d = tmp / "media" / "Movie (2020)"
        d.mkdir(parents=True)
        (d / "Movie (2020) WEBDL-2160p.mkv").write_bytes(b"x")
        res = pl.handle_rename({
            "eventType": "Rename",
            "movie": {"title": "Movie", "year": 2020},
            "movieFile": {"path": str(d / "Movie (2020) WEBDL-2160p.mkv")},
        })
        check("无字幕时安全返回", res.get("action") == "rename-no-subs", str(res))
        check("目录未被改动", len(list(d.iterdir())) == 1, str(list(d.iterdir())))


def t_sub_path_updated() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp)
        d = tmp / "media" / "Movie (2020)"
        d.mkdir(parents=True)
        (d / "Movie (2020) WEBDL-2160p.mkv").write_bytes(b"x")
        old_sub = d / "Movie (2020) Bluray-2160p.zh-cn.srt"
        old_sub.write_text("x", encoding="utf-8")
        pl.ledger.add_sub(fingerprint="fp", source="subhd", source_id="s",
                          out_path=str(old_sub), fallback=0)
        pl.handle_rename({
            "eventType": "Rename",
            "movie": {"title": "Movie", "year": 2020},
            "movieFile": {"path": str(d / "Movie (2020) WEBDL-2160p.mkv")},
        })
        rows = pl.ledger._conn().execute("SELECT out_path FROM subs").fetchall()
        got = [r["out_path"] for r in rows]
        check("台账 out_path 已跟随更新",
              got and got[0].endswith("WEBDL-2160p.zh-cn.srt"), str(got))


def t_previous_path_used() -> None:
    """改名用 payload 的 previousPath/path，不自己推导（Ops 实测 schema）。"""
    from app.pipeline import _renamed_paths

    p = {"movieFile": {"previousPath": "/a/Old (2020) Bluray-2160p.mkv",
                       "path": "/a/Old (2020) WEBDL-2160p.mkv"}}
    old, new = _renamed_paths(p)
    check("取到 previousPath", old == "/a/Old (2020) Bluray-2160p.mkv", str(old))
    check("取到 path", new == "/a/Old (2020) WEBDL-2160p.mkv", str(new))

    p2 = {"episodeFile": {"previousPath": "/b/S01E01.old.mkv", "path": "/b/S01E01.mkv"}}
    old2, new2 = _renamed_paths(p2)
    check("sonarr episodeFile 同样适用", (old2, new2) == ("/b/S01E01.old.mkv", "/b/S01E01.mkv"),
          f"{old2} / {new2}")

    old3, new3 = _renamed_paths({"movieFile": {"path": "/c/x.mkv"}})
    check("无 previousPath 时旧的为 None", old3 is None and new3 == "/c/x.mkv", f"{old3} / {new3}")


def main() -> int:
    t_title_body()
    t_loose_matching()
    t_previous_path_used()
    t_rename_follow()
    t_rename_keeps_when_target_exists()
    t_rename_no_subs()
    t_sub_path_updated()
    print()
    print(f"通过 {PASSED} 项")
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
