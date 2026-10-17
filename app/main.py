"""FastAPI 服务：接收 arr Webhook，暴露健康检查与手动触发。"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import __version__, auth
from .cache import PageCache
from .config import load_config
from .db import Ledger
from .pipeline import Pipeline
from .sources import build_adapters

log = logging.getLogger("subfindarr")

STATE: dict = {}


def setup_logging(cfg: dict) -> None:
    level = getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [sh]
    log_file = cfg.get("paths", {}).get("log")
    if log_file:
        try:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            logging.getLogger("subfindarr").warning("日志文件不可用(%s)，只输出到 stdout", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg_path = os.environ.get("SUBFINDARR_CONFIG", "/config/config.yaml")
    if not Path(cfg_path).exists():
        example = Path("/app/config.example.yaml")
        if example.exists():
            Path(cfg_path).parent.mkdir(parents=True, exist_ok=True)
            Path(cfg_path).write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            log.warning("配置不存在，已从示例生成: %s（需要填 key/cookie 后重启）", cfg_path)
    cfg = load_config(cfg_path)
    setup_logging(cfg)
    ledger = Ledger(cfg["paths"]["db"])
    cache_cfg = cfg.get("cache", {}) or {}
    cache = PageCache(
        cfg["paths"].get("cache", "/config/cache"),
        ttl_sec=int(cache_cfg.get("ttl_sec", 21600)),
        enabled=bool(cache_cfg.get("enabled", True)),
    )
    adapters = build_adapters(cfg, cache=cache)
    STATE["cfg"] = cfg
    STATE["ledger"] = ledger
    STATE["cache"] = cache
    STATE["pipeline"] = Pipeline(cfg, ledger, adapters)
    STATE["adapters"] = adapters
    log.info("subfindarr %s 启动，源=%s，媒体根=%s，缓存=%s", __version__,
             [a.name for a in adapters], cfg["paths"]["media_root"], cache.stats())
    yield
    STATE.clear()


app = FastAPI(title="subfindarr", version=__version__, lifespan=lifespan)


def _bearer(request: Request) -> str | None:
    """从 Authorization: Bearer <token> 头取 token（Chief 定：不用 ?token=，避免 URL 留痕）。"""
    raw = request.headers.get("authorization") or ""
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return raw.strip() or None


def _authorized(request: Request, token: str | None = None) -> bool:
    """委托给 app.auth（Bearer 头校验，逻辑独立便于离线测试）。"""
    server = (STATE.get("cfg", {}).get("server", {}) or {})
    return auth.authorized(server, dict(request.headers), token)


@app.get("/health")
def health() -> dict:
    cfg = STATE.get("cfg", {}) or {}
    ledger: Ledger | None = STATE.get("ledger")
    adapters = STATE.get("adapters") or []
    emby_ok = None
    try:
        pl: Pipeline | None = STATE.get("pipeline")
        if pl and pl.emby is not None:
            emby_ok = pl.emby.ping()
    except Exception:  # noqa: BLE001
        emby_ok = False
    return {
        "ok": True,
        "version": __version__,
        "sources": [
            {"name": a.name, "configured": getattr(a, "configured", True),
             "base": getattr(a, "base", None)}
            for a in adapters
        ],
        "emby": emby_ok,
        "cache": (STATE.get("cache").stats() if STATE.get("cache") else {}),
        "stats": ledger.stats() if ledger else {},
        "media_root": cfg.get("paths", {}).get("media_root"),
        "write_in_place": cfg.get("write", {}).get("in_place"),
    }


@app.get("/stats")
def stats() -> dict:
    ledger: Ledger | None = STATE.get("ledger")
    return ledger.stats() if ledger else {}


@app.post("/webhook/arr")
@app.post("/webhook/{arr_name}")
async def webhook(request: Request, background: BackgroundTasks, arr_name: str = "arr",
                  token: str | None = Query(default=None)) -> JSONResponse:
    if not _authorized(request, token):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        form = await request.form()
        payload = dict(form)
    event = payload.get("eventType", "")
    # 实测 payload 结构：radarr 顶层有 movie/movieFile，sonarr 顶层有 series/episodes/episodeFile
    kind = _payload_kind(payload)
    log.info("收到 webhook(%s, 来源=%s): event=%s 类型=%s", arr_name, kind or "未知", event, kind)
    pipeline: Pipeline | None = STATE.get("pipeline")
    if pipeline is None:
        return JSONResponse({"ok": False, "error": "服务未就绪"}, status_code=503)
    if event in ("Test", ""):
        return JSONResponse({"ok": True, "echo": "test", "kind": kind})
    background.add_task(_run, payload)
    return JSONResponse({"ok": True, "queued": True, "event": event, "kind": kind})


def _payload_kind(payload: dict) -> str | None:
    if isinstance(payload.get("movie"), dict):
        return "radarr"
    if isinstance(payload.get("series"), dict) or isinstance(payload.get("episodeFile"), dict):
        return "sonarr"
    return None


def _run(payload: dict) -> None:
    pipeline: Pipeline | None = STATE.get("pipeline")
    if pipeline is None:
        return
    try:
        result = pipeline.handle_event(payload)
        log.info("处理结果: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("webhook 处理异常")


@app.post("/process")
def process_manual(request: Request, background: BackgroundTasks, path: str = Query(...),
                   media_type: str = Query("movie"), title: str = Query(""),
                   year: int | None = Query(None), season: int | None = Query(None),
                   episode: int | None = Query(None), force: bool = Query(False),
                   token: str | None = Query(None)) -> JSONResponse:
    """手动触发：给一个容器内视频路径。用于首轮验证。"""
    if not _authorized(request, token):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    from .fingerprint import MediaInfo, parse_release_name

    pipeline: Pipeline | None = STATE.get("pipeline")
    if pipeline is None:
        return JSONResponse({"ok": False, "error": "服务未就绪"}, status_code=503)

    parsed = parse_release_name(Path(path).name)
    info = MediaInfo(
        media_type=media_type,
        title=title or parsed["title"] or "",
        year=year if year is not None else parsed["year"],
        season=season if season is not None else parsed["season"],
        episode=episode if episode is not None else parsed["episode"],
        tmdb_id=parsed["tmdb_id"],
        imdb_id=parsed["imdb_id"],
        resolution=parsed["resolution"],
        video_path=path,
    )
    if force:
        pipeline.write_cfg["force"] = True
    background.add_task(_manual_run, info)
    return JSONResponse({"ok": True, "queued": True, "fingerprint": info.fingerprint, "title": info.title})


def _manual_run(info) -> None:
    pipeline: Pipeline | None = STATE.get("pipeline")
    if pipeline is None:
        return
    try:
        log.info("手动处理结果: %s", pipeline.process(info, arr="manual"))
    except Exception:  # noqa: BLE001
        log.exception("手动处理异常")


def main() -> None:
    import uvicorn

    cfg_path = os.environ.get("SUBFINDARR_CONFIG", "/config/config.yaml")
    host, port = "0.0.0.0", 8097
    try:
        cfg = load_config(cfg_path)
        host = cfg.get("server", {}).get("host", host)
        port = int(cfg.get("server", {}).get("port", port))
    except Exception:  # noqa: BLE001
        pass
    uvicorn.run("app.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
