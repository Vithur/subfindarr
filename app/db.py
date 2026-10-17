"""SQLite 台账：指纹去重、来源记录、幂等重跑。"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL,
    media_type    TEXT NOT NULL,      -- movie | episode
    title         TEXT,
    year          INTEGER,
    season        INTEGER,
    episode       INTEGER,
    video_path    TEXT NOT NULL,
    arr           TEXT,               -- sonarr | radarr
    status        TEXT NOT NULL,      -- pending | done | skipped | failed
    attempts      INTEGER DEFAULT 0,
    last_error    TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_fp ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS subs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT NOT NULL,
    source        TEXT NOT NULL,      -- subhd | assrt | ...
    source_id     TEXT,
    lang          TEXT,               -- zh-Hans | zh-Hant | ...
    title         TEXT,
    score         REAL,
    score_detail  TEXT,
    out_path      TEXT,
    sha256        TEXT,
    fallback      INTEGER DEFAULT 0,  -- 1 = 没写进媒体库，落到 subs_out
    created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subs_fp ON subs(fingerprint);

CREATE TABLE IF NOT EXISTS seen_urls (
    url         TEXT PRIMARY KEY,
    source      TEXT,
    fingerprint TEXT,
    created_at  REAL NOT NULL
);

-- 改名等动作的审计流水：每一步都可查（改名前/后/失败）
CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,     -- rename | ...
    phase       TEXT NOT NULL,     -- before | after | failed
    src         TEXT,
    dst         TEXT,
    note        TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_log_action ON action_log(action, id);
"""


class Ledger:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conn()) as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            c.commit()

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """老库补列：CREATE TABLE IF NOT EXISTS 不会给已存在的表加新列。"""
        cols = {r[1] for r in c.execute("PRAGMA table_info(subs)").fetchall()}
        if "fallback" not in cols:
            c.execute("ALTER TABLE subs ADD COLUMN fallback INTEGER DEFAULT 0")
            c.execute("UPDATE subs SET fallback=0 WHERE fallback IS NULL")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ---- jobs ----

    def upsert_job(
        self,
        *,
        fingerprint: str,
        media_type: str,
        video_path: str,
        title: str | None = None,
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        arr: str | None = None,
    ) -> str:
        """返回动作：created | requeued | exists。"""
        now = time.time()
        with closing(self._conn()) as c:
            row = c.execute(
                "SELECT id, status FROM jobs WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO jobs (fingerprint, media_type, title, year, season, episode,
                                         video_path, arr, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,'pending',?,?)""",
                    (fingerprint, media_type, title, year, season, episode, video_path, arr, now, now),
                )
                c.commit()
                return "created"
            if row["status"] in ("done", "skipped", "skipped_strm"):
                c.execute(
                    "UPDATE jobs SET updated_at=? WHERE id=?", (now, row["id"])
                )
                c.commit()
                return "exists"
            c.execute(
                "UPDATE jobs SET status='pending', video_path=?, updated_at=? WHERE id=?",
                (video_path, now, row["id"]),
            )
            c.commit()
            return "requeued"

    def set_job_status(self, fingerprint: str, status: str, error: str | None = None) -> None:
        with closing(self._conn()) as c:
            c.execute(
                """UPDATE jobs SET status=?, last_error=?, attempts=attempts+1, updated_at=?
                   WHERE fingerprint=?""",
                (status, error, time.time(), fingerprint),
            )
            c.commit()

    def get_job(self, fingerprint: str) -> sqlite3.Row | None:
        with closing(self._conn()) as c:
            return c.execute("SELECT * FROM jobs WHERE fingerprint=?", (fingerprint,)).fetchone()

    def pending_jobs(self, limit: int = 100) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT * FROM jobs WHERE status IN ('pending','failed') ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()

    def stats(self) -> dict[str, int]:
        """按状态分组计数。

        **成功口径只有 `done`**：`skipped_strm` / `fallback` / `refresh_pending`
        各自独立成键，不进 done——防止「台账全绿但没字幕」。
        """
        with closing(self._conn()) as c:
            rows = c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        out = {r["status"]: r["n"] for r in rows}
        out["_ok_only_done"] = out.get("done", 0)
        out["_degraded"] = (out.get("skipped_strm", 0) + out.get("fallback", 0)
                            + out.get("refresh_pending", 0))
        return out

    # ---- subs ----

    def add_sub(
        self,
        *,
        fingerprint: str,
        source: str,
        source_id: str | None = None,
        lang: str | None = None,
        title: str | None = None,
        score: float | None = None,
        score_detail: dict[str, Any] | None = None,
        out_path: str | None = None,
        sha256: str | None = None,
        fallback: int = 0,
    ) -> None:
        import json

        with closing(self._conn()) as c:
            c.execute(
                """INSERT INTO subs (fingerprint, source, source_id, lang, title, score,
                                     score_detail, out_path, sha256, fallback, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fingerprint, source, source_id, lang, title, score,
                    json.dumps(score_detail, ensure_ascii=False) if score_detail else None,
                    out_path, sha256, int(fallback), time.time(),
                ),
            )
            c.commit()

    def add_log(self, action: str, phase: str, src: str | None = None,
                dst: str | None = None, note: str | None = None) -> None:
        """记录一条动作流水（改名等），改名前后各一条，失败也留痕。"""
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO action_log (action, phase, src, dst, note, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (action, phase, src, dst, note, time.time()),
            )
            c.commit()

    def action_logs(self, action: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            if action:
                return c.execute(
                    "SELECT * FROM action_log WHERE action=? ORDER BY id DESC LIMIT ?",
                    (action, limit),
                ).fetchall()
            return c.execute(
                "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def update_sub_path(self, old_path: str, new_path: str) -> int:
        """字幕改名后同步台账 out_path，避免台账与磁盘长期不一致。"""
        with closing(self._conn()) as c:
            cur = c.execute(
                "UPDATE subs SET out_path=? WHERE out_path=?", (new_path, old_path)
            )
            c.commit()
            return cur.rowcount

    def mark_fallback(self, fingerprint: str, reason: str) -> None:
        """把该指纹下最近一条 subs 标为 fallback，并把原因写进 job 的 last_error。

        Chief 定：fallback 发生时必须留痕，否则「刷新了但字幕不在媒体库」最难查。
        """
        with closing(self._conn()) as c:
            c.execute(
                """UPDATE subs SET fallback=1
                   WHERE id = (SELECT MAX(id) FROM subs WHERE fingerprint=?)""",
                (fingerprint,),
            )
            c.execute(
                "UPDATE jobs SET last_error=?, updated_at=? WHERE fingerprint=?",
                (f"[fallback] {reason}"[:800], time.time(), fingerprint),
            )
            c.commit()

    def fallback_subs(self, limit: int = 50) -> list[sqlite3.Row]:
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT fingerprint, source, title, out_path, created_at FROM subs "
                "WHERE fallback=1 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def has_accepted_sub(self, fingerprint: str) -> bool:
        with closing(self._conn()) as c:
            r = c.execute(
                "SELECT COUNT(*) n FROM subs WHERE fingerprint=? AND out_path IS NOT NULL",
                (fingerprint,),
            ).fetchone()
        return bool(r["n"])

    # ---- url 去重 ----

    def mark_url(self, url: str, source: str, fingerprint: str) -> bool:
        """首次见到返回 True；已见过返回 False。"""
        with closing(self._conn()) as c:
            try:
                c.execute(
                    "INSERT INTO seen_urls (url, source, fingerprint, created_at) VALUES (?,?,?,?)",
                    (url, source, fingerprint, time.time()),
                )
                c.commit()
                return True
            except sqlite3.IntegrityError:
                return False
