"""路径映射的回归测试。

映射表全部来自 `paths.path_map`（最长前缀优先）；代码默认只认 arr 容器视角的
`/data/media`，宿主路径必须在配置里显式登记。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _pipeline(path_map: dict | None = None) -> Pipeline:
    cfg = load_config(str(Path(__file__).resolve().parent.parent / "config.example.yaml"))
    cfg["paths"]["media_root"] = "/media"
    if path_map is not None:
        cfg["paths"]["path_map"] = path_map
    else:
        cfg["paths"].pop("path_map", None)  # 走代码默认
    return Pipeline(cfg, ledger=None, adapters=[])  # type: ignore[arg-type]


def main() -> int:
    # 1) 默认映射（配置里没有 path_map）
    p = _pipeline()
    cases = {
        "/data/media/movies/Film (2020)/Film.mkv": "/media/movies/Film (2020)/Film.mkv",
        "/data/media/tv/Show/Season 01/S01E01.mkv": "/media/tv/Show/Season 01/S01E01.mkv",
        "/media/movies/Film (2020)/Film.mkv": "/media/movies/Film (2020)/Film.mkv",
    }
    for src, want in cases.items():
        got = str(p._to_container_path(Path(src)))
        check(f"默认映射 {src}", got == want, f"得到 {got}，期望 {want}")

    # 2) 配置了宿主前缀后，宿主视角路径也能映射
    p2 = _pipeline({"/data/media": "/media", "/host/media": "/media"})
    got = str(p2._to_container_path(Path("/host/media/movies/Film (2020)/Film.mkv")))
    check("宿主前缀映射", got == "/media/movies/Film (2020)/Film.mkv", got)

    # 3) 最长前缀优先（/host/media 比 /host 长，应命中前者）
    p3 = _pipeline({"/host": "/wrong", "/host/media": "/media"})
    got3 = str(p3._to_container_path(Path("/host/media/movies/x.mkv")))
    check("最长前缀优先", got3 == "/media/movies/x.mkv", got3)

    # 4) 未登记的前缀原样返回，不瞎猜
    got4 = str(p._to_container_path(Path("/unknown/place/x.mkv")))
    check("未知前缀不瞎改", got4 == "/unknown/place/x.mkv", got4)

    check("示例配置媒体根为 /media", load_config(
        str(Path(__file__).resolve().parent.parent / "config.example.yaml"))["paths"]["media_root"] == "/media")

    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
