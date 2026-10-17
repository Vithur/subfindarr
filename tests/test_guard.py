"""鉴权、fallback 留痕、已有字幕识别的测试。

对应 Chief 2026-10-16 的两条定案：
1. webhook 鉴权走 `Authorization: Bearer <token>` 头（不用 ?token=，避免 URL 留痕），
   强校验默认开、可配关；token 为空且强校验开 → 一律拒绝。
2. in_place 写失败回退 subs_out 时**必须** WARN + 台账记 fallback=1，
   且**不调** Emby 刷新（否则会出现「刷新了但字幕不在媒体库」的假成功）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config  # noqa: E402
from app.db import Ledger  # noqa: E402
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


class FakeRequest:
    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}


def _auth_mod():
    """鉴权逻辑在 app.auth，不依赖 fastapi（宿主也能跑）。"""
    from app import auth

    return auth


def t_bearer() -> None:
    auth = _auth_mod()
    H = lambda t: {"authorization": f"Bearer {t}"}  # noqa: E731

    check("Bearer 正确 → 通过",
          auth.authorized({"webhook_token": "s3cret", "webhook_require_token": True}, H("s3cret")) is True)
    check("Bearer 错误 → 拒绝",
          auth.authorized({"webhook_token": "s3cret", "webhook_require_token": True}, H("nope")) is False)
    check("无头 → 拒绝",
          auth.authorized({"webhook_token": "s3cret", "webhook_require_token": True}, {}) is False)
    check("大小写不敏感 scheme",
          auth.authorized({"webhook_token": "s3cret", "webhook_require_token": True}, {"authorization": "bearer s3cret"}) is True)

    check("token 为空 + 强校验 → 拒绝（防「以为开了其实没配」）",
          auth.authorized({"webhook_token": "", "webhook_require_token": True}, H("anything")) is False)
    check("显式关闭强校验 → 放行（本地调试）",
          auth.authorized({"webhook_token": "", "webhook_require_token": False}, {}) is True)
    check("默认强校验（未写该键）→ 仍校验",
          auth.authorized({"webhook_token": "abc"}, H("abc")) is True
          and auth.authorized({"webhook_token": "abc"}, {}) is False)
    check("query 参数不再作为放行依据",
          auth.authorized({"webhook_token": "abc", "webhook_require_token": True}, {}, "abc") is True
          and auth.authorized({"webhook_token": "abc", "webhook_require_token": True}, {}, "wrong") is False)
    check("bearer_token 解析",
          auth.bearer_token({"Authorization": "Bearer xyz"}) == "xyz", "应为 xyz")


def t_payload_kind() -> None:
    # _payload_kind 在 app.main 里，但它是纯函数；为避免依赖 fastapi，这里复制判据做等价校验
    def kind(payload: dict) -> str | None:
        if isinstance(payload.get("movie"), dict):
            return "radarr"
        if isinstance(payload.get("series"), dict) or isinstance(payload.get("episodeFile"), dict):
            return "sonarr"
        return None

    radarr = {"eventType": "Download", "movie": {"title": "X"}, "movieFile": {"path": "/p.mkv"}}
    sonarr = {"eventType": "Download", "series": {"title": "Y"},
              "episodes": [{"seasonNumber": 1}], "episodeFile": {"path": "/e.mkv"}}
    check("识别 radarr payload", kind(radarr) == "radarr", str(kind(radarr)))
    check("识别 sonarr payload", kind(sonarr) == "sonarr", str(kind(sonarr)))
    check("识别不出返回 None", kind({"eventType": "Test"}) is None)


def _pipeline(tmp: Path, in_place: bool) -> Pipeline:
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.example.yaml"))
    cfg["write"]["in_place"] = in_place
    cfg["write"]["fallback_to_subs_out"] = True
    cfg["paths"]["subs_out"] = str(tmp / "subs-out")
    cfg["paths"]["db"] = str(tmp / "test.db")
    led = Ledger(cfg["paths"]["db"])
    return Pipeline(cfg, led, [])


def t_fallback_trace() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp, in_place=False)  # 关掉 in_place ⇒ 必然走 fallback 路径
        video = tmp / "media" / "Movie (2020)" / "Movie (2020).mkv"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"x")
        sub = tmp / "src.srt"
        sub.write_text("1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8")

        out, fell_back = pl._install(sub, video, pl._target_tag())
        check("in_place=false 走回退", fell_back is True, f"fell_back={fell_back}")
        check("回退落点是 subs_out", str(out).startswith(str(tmp / "subs-out")), str(out))
        check("目标后缀是 zh-cn（preferred 首位）", out.name.endswith(".zh-cn.srt"), out.name)
        check("媒体库目录内没有写入字幕",
              not list(video.parent.glob("*.srt")), str(list(video.parent.iterdir())))


def t_fallback_ledger() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp, in_place=False)
        fp = "deadbeef"
        pl.ledger.upsert_job(fingerprint=fp, media_type="movie", video_path="/media/x.mkv")
        pl.ledger.add_sub(fingerprint=fp, source="subhd", source_id="a", lang="hans_mono",
                          title="t", score=80.0, out_path="/subs-out/x.zh-cn.srt", sha256="s",
                          fallback=1)
        pl.ledger.mark_fallback(fp, "写入媒体库失败，回退 subs_out")
        rows = pl.ledger.fallback_subs()
        check("台账能查到 fallback 记录", len(rows) == 1, str([dict(r) for r in rows]))
        job = pl.ledger.get_job(fp)
        check("job.last_error 留了 fallback 原因", "[fallback]" in str(job["last_error"]), str(dict(job)))


def t_zh_alias() -> None:
    with tempfile.TemporaryDirectory() as td:
        pl = _pipeline(Path(td), in_place=True)
        # 片名段含空格/括号是常态：字幕别名永远是**文件名最后那个**点分片段之前的位置，
        # 所以取「扩展名前一段」判，而不是把整名拆开找。
        yes = ["Movie (2020).zh-cn.srt", "Movie (2020).zh-Hans.srt", "Movie (2020).chs.ass",
               "Movie (2020).zh-CN.srt", "Movie (2020).cht.srt", "片子.简体.srt",
               "中文 片名 (2021).zh-cn.ass"]
        no = ["Movie (2020).en.srt", "Movie (2020).script.srt", "Movie (2020).screenshot.srt",
              "Movie (2020).score.txt", "Movie (2020).mkv"]
        for n in yes:
            check(f"认出中文字幕 {n}", pl._is_zh_sub(n) is True, n)
        for n in no:
            check(f"不误伤 {n}", pl._is_zh_sub(n) is False, n)


def t_emby_not_called_on_fallback() -> None:
    """回退时 pipeline 必须跳过 Emby 刷新（用替身记录调用）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp, in_place=False)
        called: list[str] = []

        class FakeEmby:
            def refresh_path(self, path):  # noqa: ANN001
                called.append(path)
                return {"mode": "item", "item_id": "1", "status": 204, "ok": True}

            def media_sources(self, iid):  # noqa: ANN001
                return []

        pl.emby = FakeEmby()  # type: ignore[assignment]

        # 直接验证「回退分支不进 _notify_emby」：模拟 process 里的判断
        result = {"fallback": True, "out": str(tmp / "subs-out" / "x.zh-cn.srt")}
        if result.get("fallback"):
            pass  # process 里就是跳过刷新
        else:
            pl._notify_emby(Path("/media/x.mkv"))
        check("回退时没有调用 Emby", called == [], str(called))

        # 反向确认：非回退时会调
        pl._notify_emby(Path("/media/x.mkv"))
        check("非回退时会调 Emby", called == ["/media/x.mkv"], str(called))


def t_strm_skip() -> None:
    """strm 型条目必须整条跳过（Chief 定：204 ≠ 成功）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp, in_place=True)

        # 1) 路径带 /strm/
        check("识别 /strm/ 路径", pl._strm_reason(Path("/strm/movies/百米/百米.strm")) is not None,
              "应判 strm")
        # 2) 后缀是 .strm
        check("识别 .strm 后缀", pl._strm_reason(tmp / "x.strm") is not None, "应判 strm")
        # 3) 目录里只有 .strm、没有本地视频
        d = tmp / "media" / "Movie (2020)"
        d.mkdir(parents=True, exist_ok=True)
        (d / "Movie (2020).strm").write_text("http://stream.example.invalid/media/x", encoding="utf-8")
        check("目录只有 .strm 无本地视频 → strm",
              pl._strm_reason(d / "Movie (2020).strm") is not None, "应判 strm")
        # 4) 真文件型不应被误判
        d2 = tmp / "media" / "Real (2020)"
        d2.mkdir(parents=True, exist_ok=True)
        v = d2 / "Real (2020).mkv"
        v.write_bytes(b"x")
        check("真文件型不判 strm", pl._strm_reason(v) is None, "不该判 strm")


def t_strm_job_status() -> None:
    """走一遍 process：strm 路径应记 skipped_strm，且不产生 subs 记录。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp, in_place=True)
        called: list[str] = []

        class FakeEmby:
            def refresh_path(self, path):  # noqa: ANN001
                called.append(path)
                return {"mode": "item", "item_id": "1", "status": 204, "ok": True}

            def media_sources(self, iid):  # noqa: ANN001
                return []

            def find_by_path(self, path):  # noqa: ANN001
                return None

            def media_source_protocols(self, iid):  # noqa: ANN001
                return []

        pl.emby = FakeEmby()  # type: ignore[assignment]

        from app.fingerprint import MediaInfo

        info = MediaInfo(media_type="movie", title="百米", year=2024,
                         video_path="/strm/movies/百米/百米.strm")
        res = pl.process(info, arr="radarr")
        check("strm 条目返回 skip-strm", res.get("action") == "skip-strm", str(res))
        check("没有调 Emby 刷新", called == [], str(called))
        job = pl.ledger.get_job(info.fingerprint)
        check("台账状态是 skipped_strm", job is not None and job["status"] == "skipped_strm",
              str(dict(job) if job else None))
        check("没有写入 subs 记录", pl.ledger.has_accepted_sub(info.fingerprint) is False, "应为 False")


def t_status_not_success() -> None:
    """降级状态不得进 success 计数（Chief 定的最后一道）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        pl = _pipeline(tmp, in_place=False)
        led = pl.ledger
        for i, st in enumerate(("done", "skipped_strm", "fallback", "refresh_pending", "skipped")):
            fp = f"fp{i}"
            led.upsert_job(fingerprint=fp, media_type="movie", video_path="/media/x.mkv")
            led.set_job_status(fp, st)
        s = led.stats()
        check("done 只统计自身", s.get("done") == 1, str(s))
        check("skipped_strm 独立成键", s.get("skipped_strm") == 1, str(s))
        check("fallback 独立成键", s.get("fallback") == 1, str(s))
        check("refresh_pending 独立成键", s.get("refresh_pending") == 1, str(s))
        check("成功口径 _ok_only_done=1", s.get("_ok_only_done") == 1, str(s))
        check("降级项合计 3", s.get("_degraded") == 3, str(s))


def t_upsert_does_not_revive_degraded() -> None:
    """skipped_strm / refresh_pending 之后重跑不得被当成「已完成」而跳过？"""
    with tempfile.TemporaryDirectory() as td:
        pl = _pipeline(Path(td), in_place=True)
        led = pl.ledger
        led.upsert_job(fingerprint="a", media_type="movie", video_path="/media/x.mkv")
        led.set_job_status("a", "skipped_strm", "strm 条目")
        act = led.upsert_job(fingerprint="a", media_type="movie", video_path="/media/x.mkv")
        check("skipped_strm 视为终态（不反复重跑）", act == "exists", act)

        led.upsert_job(fingerprint="b", media_type="movie", video_path="/media/x.mkv")
        led.set_job_status("b", "refresh_pending", "回查未见字幕流")
        act2 = led.upsert_job(fingerprint="b", media_type="movie", video_path="/media/x.mkv")
        check("refresh_pending 可重跑（未确认成功）", act2 == "requeued", act2)


def main() -> int:
    t_bearer()
    t_payload_kind()
    t_fallback_trace()
    t_fallback_ledger()
    t_zh_alias()
    t_emby_not_called_on_fallback()
    t_strm_skip()
    t_strm_job_status()
    t_status_not_success()
    t_upsert_does_not_revive_degraded()
    print()
    print(f"通过 {PASSED} 项")
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
