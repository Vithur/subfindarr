"""解压与整理：压缩包解开、编码探测、ASS 保样式、按规范改名。"""

from __future__ import annotations

import gzip
import io
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

try:  # 容器内装了 py7zr；宿主裸跑时降级为不支持 7z
    import py7zr
except ImportError:  # pragma: no cover
    py7zr = None  # type: ignore[assignment]

log = logging.getLogger("subfindarr.pack")

# 纯文本字幕（图形字幕另见 GRAPHIC_EXT，单独排除）
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".smi"}
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".gz", ".tar"}

MAGIC = {
    b"PK\x03\x04": "zip",
    b"PK\x05\x06": "zip-empty",
    b"Rar!\x1a\x07": "rar",
    b"7z\xbc\xaf\x27\x1c": "7z",
    b"\x1f\x8b": "gzip",
}

ENCODINGS = ["utf-8-sig", "utf-8", "gb18030", "big5", "utf-16", "cp936"]


def detect_archive(head: bytes) -> str | None:
    for magic, kind in MAGIC.items():
        if head.startswith(magic):
            return kind
    return None


def detect_sub_format(path: str | Path) -> str | None:
    """按**内容**判字幕格式，返回扩展名（.ass/.ssa/.srt/.vtt）或 None。

    真机踩过：包里解出的文件叫 `*.srt`，内容却是 ASS（首行 `[Script Info]`），
    按名字落盘成 `.srt` 后 Emby 解析不出字幕流。所以扩展名要按内容定。
    """
    p = Path(path)
    try:
        with p.open("rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    # ASS/SRT 的关键标记都是 ASCII，直接在**字节**上判最稳：
    # 先按编码解码再判会被 big5/gb18030 之类「能解但解出乱码」的编码截胡。
    low_bytes = head.lower().lstrip(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
    if low_bytes.startswith(b"[script info]") or low_bytes.startswith(b"; script") \
            or b"[v4+ styles]" in low_bytes or b"[v4 styles]" in low_bytes:
        return ".ssa" if b"[v4 styles]" in low_bytes and b"[v4+ styles]" not in low_bytes else ".ass"
    if low_bytes.startswith(b"webvtt"):
        return ".vtt"
    import re

    # srt：第一条是「序号 CRLF 时间轴 --> 」
    if re.match(rb"^\s*\d+\s*\r?\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->", head):
        return ".srt"
    if b"-->" in head:
        return ".srt"
    return None


def sniff_encoding(data: bytes) -> str:
    for enc in ENCODINGS:
        try:
            data.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def safe_name(name: str) -> str:
    """去掉路径穿越与非法字符。"""
    name = name.replace("\\", "/").split("/")[-1]
    name = name.replace("\x00", "")
    return name.strip() or "unnamed"


def decode_zip_name(info) -> str:
    """解出 zip 条目的真实文件名。

    zip 规范里非 ASCII 名要么置 UTF-8 标志位（`flag_bits & 0x800`），
    要么按 CP437 存。中文站点的包基本是后者——`zipfile` 会给出乱码名，
    而乱码名会让后续 `.srt/.ass` 后缀识别失效。

    Kernel 的判据：**flag bit11(0x800)=0 且名字里有 >0x7F 的字节** 才走
    cp437 → gbk 分支（全 ASCII 的名字不该被转，否则可能改坏正确名字）。
    gb18030 是 gbk 的超集，用它做解码更宽容（两者对 GBK 字节等价）。
    """
    raw = info.filename
    if getattr(info, "flag_bits", 0) & 0x800:
        return raw
    try:
        blob = raw.encode("cp437")
    except UnicodeEncodeError:
        return raw
    if not any(b > 0x7F for b in blob):
        return raw  # 纯 ASCII，不需要转
    for enc in ("gb18030", "gbk", "big5", "utf-8"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw


# 图形字幕：需要 OCR 才能转文本，比机翻标记更硬，直接排除
GRAPHIC_EXT = {".sup", ".idx", ".sub", ".mks", ".pgs"}


def is_graphic_sub(name: str) -> bool:
    """判是否图形字幕（.sup / .sub+.idx 这类）。"""
    return Path(name).suffix.lower() in GRAPHIC_EXT


def _is_sub_name(name: str) -> bool:
    """可用的字幕：排除图形字幕。"""
    if is_graphic_sub(name):
        return False
    return Path(name).suffix.lower() in SUB_EXT


def extract(archive_path: str | Path, dest_dir: str | Path) -> list[Path]:
    """解开压缩包，返回解出的字幕文件列表（按需求已过滤）。"""
    src = Path(archive_path)
    dst = Path(dest_dir)
    dst.mkdir(parents=True, exist_ok=True)
    head = src.open("rb").read(16)
    kind = detect_archive(head)
    out: list[Path] = []

    if kind in ("zip", "zip-empty"):
        with zipfile.ZipFile(src) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                nm = safe_name(decode_zip_name(info))
                if not _is_sub_name(nm):
                    continue
                data = z.read(info)
                out.append(_write_fixed(dst / nm, data))
    elif kind == "7z":
        if py7zr is None:
            raise ValueError("7z 需要 py7zr（容器内已装）")
        with py7zr.SevenZipFile(src, mode="r") as z:
            for nm, bio in (z.readall() or {}).items():
                base = safe_name(nm)
                if not _is_sub_name(base):
                    continue
                out.append(_write_fixed(dst / base, bio.read()))
    elif kind == "gzip":
        nm = safe_name(src.stem) or "sub.srt"
        if not Path(nm).suffix:
            nm += ".srt"
        with gzip.open(src, "rb") as f:
            out.append(_write_fixed(dst / nm, f.read()))
    elif kind == "rar":
        out = _extract_rar(src, dst)
    else:
        # 不是压缩包：很可能是**裸字幕文件**（真机实测 dl.subhd.me 会直接给 .srt）。
        # 判据用内容而不是文件名——下载时落的是临时名（*.part），后缀不可靠。
        fmt = detect_sub_format(src)
        if fmt:
            nm = safe_name(src.name)
            # 临时名/无扩展名 → 按嗅探出的格式补名
            if not nm.lower().endswith(fmt):
                stem = nm
                for junk in (".part", ".tmp"):
                    if stem.lower().endswith(junk):
                        stem = stem[: -len(junk)]
                nm = (stem or "sub") + fmt
            out.append(_write_fixed(dst / nm, src.read_bytes()))
        else:
            raise ValueError(f"不认识的压缩格式，头字节={head!r} 文件={src}")

    if not out:
        # 有些包直接就是字幕本体（后缀可信时兜底）
        if src.suffix.lower() in SUB_EXT:
            out.append(src)
        else:
            raise ValueError(f"包里没找到字幕文件: {src}")
    return out


def _extract_rar(src: Path, dst: Path) -> list[Path]:
    import subprocess

    for exe in ("unrar", "unrar-free", "7z", "7za"):
        if shutil.which(exe) is None:
            continue
        cmd = [exe, "x", "-y", f"-o{dst}", str(src)]
        if exe in ("7z", "7za"):
            cmd = [exe, "x", "-y", f"-o{dst}", str(src)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=120)
            return [p for p in dst.rglob("*") if p.is_file() and _is_sub_name(p.name)]
        except Exception as e:  # noqa: BLE001
            log.warning("%s 解压失败: %s", exe, e)
    raise ValueError(f"无可用 rar 解压器: {src}")


def _write_fixed(path: Path, data: bytes) -> Path:
    """统一写 UTF-8，保留 ASS 原样式（只转码文本，不碰样式块）。"""
    enc = sniff_encoding(data)
    if enc != "utf-8":
        try:
            data = data.decode(enc).encode("utf-8")
        except UnicodeDecodeError:
            pass
    path.write_bytes(data)
    return path


def pick_best(files: list[Path], prefer: str = "zh-Hans") -> Path:
    """多文件时挑简体优先的那个。"""
    def rank(p: Path) -> tuple[int, int]:
        n = p.name.lower()
        score = 0
        if any(k in n for k in ("zh-hans", "chs", "sc", "简体", "简中")):
            score -= 10
        if any(k in n for k in ("zh-hant", "cht", "tc", "繁體", "繁体")):
            score += 5
        if any(k in n for k in ("eng", "english", "英文")):
            score += 20
        if any(k in n for k in ("forced", "强制")):
            score += 30
        score += 0 if p.suffix.lower() in (".srt", ".ass", ".ssa") else 5
        return (score, len(p.name))

    return sorted(files, key=rank)[0]


def target_name(video_path: str | Path, tag: str = ".zh-cn.srt") -> Path:
    v = Path(video_path)
    return v.with_name(v.stem + tag)


def install(src: Path, video_path: str | Path, dst_dir: Path, tag: str = ".zh-cn.srt") -> Path:
    """把选中的字幕放到目标名（tag 必传，默认与 naming.preferred 首位一致）。"""
    dst = dst_dir / target_name(video_path, tag).name
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def existing_subs(video_path: str | Path, extensions: tuple[str, ...]) -> list[Path]:
    """前缀**完全等值**的既有字幕（严格口径）。"""
    v = Path(video_path)
    if not v.parent.exists():
        return []
    stem = v.stem
    found = []
    for p in v.parent.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in extensions:
            continue
        if p.name.startswith(stem + ".") or p.stem == stem:
            found.append(p)
    return found


# 质量/发行标记：这些段在 arr 改名（质量升级）时会变，不能算进「片名主体」。
# 注意用「分隔符或串首尾」界定，但**不消费**分隔符——否则 `WEBDL-2160p` 里的 `-`
# 会被前一个标记吃掉，导致 `2160p` 匹配不上（踩过）。
_QUALITY_TOKEN = re.compile(
    r"(?:(?<=[\s._\-])|^)("
    r"2160p|1080p|720p|480p|4k|uhd|hdr10\+?|hdr|dv|dolby\.?vision|sdr|"
    r"bluray|blu-ray|bdrip|brrip|web-?dl|webrip|web|hdtv|dvdrip|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|10bit|8bit|"
    r"aac|ac3|eac3|ddp?5\.?1|dd5\.?1|dts(?:-hd)?|truehd|atmos|flac|"
    r"proper|repack|internal|extended|unrated|imax"
    r")(?=$|[\s._\-])",
    re.IGNORECASE,
)


def title_body(name: str, lang_aliases: list[str] | None = None) -> str:
    """取「片名主体」：去掉扩展名、语言别名段、质量标记、年份与括号年份。

    用途：arr 改名（如 `…Bluray-2160p.mkv` → `…WEBDL-2160p.mkv`）后，
    前缀不再等值，但仍属**同一部片**。按主体比较才能正确判「已有字幕」。

    `lang_aliases` 传字幕别名集（`.zh-cn`/`.chs`…）时会把它们一并剥掉——
    字幕名（`片名.zh-cn.srt`）与视频名（`片名.mkv`）才能比出同一主体。
    """
    stem = Path(name).stem
    s = stem
    # 先剥语言别名段（通常紧邻扩展名，如 `zh-cn` / `chs` / `cht`）
    for a in (lang_aliases or []):
        base = str(a).lower().lstrip(".")
        if not base:
            continue
        s = re.sub(rf"(?<=[\s._\-]){re.escape(base)}(?=$|[\s._\-])", " ", s, flags=re.IGNORECASE)
        if s.lower().endswith("." + base):
            s = s[: -(len(base) + 1)]
    s = _QUALITY_TOKEN.sub(" ", s)
    s = re.sub(r"[\(\[]\s*(?:19|20)\d{2}\s*[\)\]]", " ", s)      # (2020) / [2020]
    s = re.sub(r"(?:^|[\s._\-])((?:19|20)\d{2})(?:$|[\s._\-])", " ", s)  # 裸年份
    s = re.sub(r"[\s._\-]+", " ", s).strip().lower()
    return s


def existing_subs_loose(video_path: str | Path, extensions: tuple[str, ...],
                        zh_alias_check=None, lang_aliases: list[str] | None = None) -> list[Path]:
    """按「片名主体」模糊匹配的既有字幕（改名后仍能认出）。

    Chief 定的纪律：落点由视频文件名推导 ⇒ 判「是否已有字幕」必须用**前缀模糊匹配**，
    不能完全等值，否则 arr 改名后会重复下载一次。

    只比主体、不看质量标记；`lang_aliases` 用于把字幕名的语言段一并剥掉，
    使 `片名.zh-cn.srt` 与 `片名.mkv` 主体相同；可选 `zh_alias_check(name)` 再筛中文别名。
    """
    v = Path(video_path)
    if not v.parent.exists():
        return []
    want = title_body(v.name, lang_aliases)
    if not want:
        return []
    out: list[Path] = []
    for p in v.parent.iterdir():
        if not p.is_file() or p.suffix.lower() not in extensions:
            continue
        if title_body(p.name, lang_aliases) != want:
            continue
        if zh_alias_check is not None and not zh_alias_check(p.name):
            continue
        out.append(p)
    return out


def writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".sf_write_probe"
        probe.write_bytes(b"x")
        probe.unlink()
        return True
    except OSError:
        return False
