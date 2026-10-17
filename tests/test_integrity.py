"""下载完整性、续传、zip 文件名编码、图形字幕过滤的测试。

对应 Chief 2026-10-16 的五条要求（Kernel 实测 CDN 会中途掐断）：
1. Content-Length 与实际字节数比对 + ZIP EOCD 校验
2. 不完整时 Range 续传，续完重新校验
3. 续传有上限，到顶判失败（绝不解包半截）
4. zip 内 GBK 文件名按 cp437 → gb18030 反解
5. .sup / .sub+.idx 图形字幕直接过滤
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.fetcher import check_download_complete, check_zip_complete  # noqa: E402
from app.pack import (  # noqa: E402
    SUB_EXT,
    decode_zip_name,
    extract,
    is_graphic_sub,
)

FAILED: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if detail and not cond else ""))
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)


def _make_zip(names: list[str], gbk: bool = False) -> bytes:
    """造一个 zip；gbk=True 时模拟「GBK 原始字节 + 无 UTF-8 标志位」的老式包。

    `zipfile` 没有「按指定编码写名字」的入口（writestr 总是 UTF-8 + 置 0x800），
    所以直接手写 zip 结构：本地头 + 数据 + 中央目录 + EOCD。
    这样能精确控制名字字节与标志位，素材与线上包同形。
    """
    if not gbk:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                z.writestr(n, _sub_payload())
        return buf.getvalue()
    return _build_zip_raw([(n.encode("gb18030"), _sub_payload()) for n in names])


def _sub_payload() -> bytes:
    return "1\n00:00:01,000 --> 00:00:02,000\n你好\n".encode("utf-8")


def _build_zip_raw(entries: list[tuple[bytes, bytes]]) -> bytes:
    """手写一个最小 zip：名字用给定字节、不置 UTF-8 标志位。"""
    import struct
    import zlib

    chunks: list[bytes] = []
    central: list[bytes] = []
    offset = 0
    for name, data in entries:
        crc = zlib.crc32(data) & 0xFFFFFFFF
        # 本地文件头：签名 + 版本 + 标志(0) + 方法(0=store) + 时间 + crc + 大小 + 名长 + 扩展长 + 名字
        lh = struct.pack(
            "<IHHHHHIIIHH", 0x04034B50, 20, 0x0000, 0, 0, 0,
            crc, len(data), len(data), len(name), 0,
        ) + name
        chunks.append(lh + data)
        central.append(
            struct.pack(
                "<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, 0x0000, 0, 0, 0,
                crc, len(data), len(data), len(name), 0, 0, 0, 0, 0, offset,
            ) + name
        )
        offset += len(lh) + len(data)

    cd = b"".join(central)
    eocd = struct.pack(
        "<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(cd), offset, 0
    )
    return b"".join(chunks) + cd + eocd


def _clear_utf8_flag(blob: bytearray) -> bytearray:
    """把 zip 里所有条目的 flag_bits 0x800（UTF-8）位清掉。

    flag_bits 是 u16 小端，位于本地头偏移 6、中央目录项偏移 8。
    """
    import struct

    out = bytearray(blob)
    i = 0
    while True:
        i = bytes(out).find(b"PK\x03\x04", i)
        if i < 0:
            break
        flags = struct.unpack_from("<H", out, i + 6)[0]
        struct.pack_into("<H", out, i + 6, flags & ~0x800)
        i += 4
    i = 0
    while True:
        i = bytes(out).find(b"PK\x01\x02", i)
        if i < 0:
            break
        flags = struct.unpack_from("<H", out, i + 8)[0]
        struct.pack_into("<H", out, i + 8, flags & ~0x800)
        i += 4
    return out


# ---------- 1. EOCD / 完整性 ----------

def t_zip_complete() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "ok.zip"
        data = _make_zip(["a.srt"])
        p.write_bytes(data)

        ok, why = check_zip_complete(str(p))
        check("完整 zip 通过校验", ok is True, why)

        ok2, why2 = check_zip_complete(str(p), expected_len=len(data))
        check("长度一致通过", ok2 is True, why2)

        ok3, why3 = check_zip_complete(str(p), expected_len=len(data) + 100)
        check("长度不符被拒", ok3 is False and "长度不符" in why3, why3)

        # 截断：保留 PK 头，砍掉尾部 EOCD
        trunc = Path(td) / "trunc.zip"
        trunc.write_bytes(data[: len(data) // 2])
        ok4, why4 = check_zip_complete(str(trunc))
        check("截断 zip 被拒（缺 EOCD）", ok4 is False, why4)
        check("拒因指向 EOCD", "EOCD" in why4 or "BadZipFile" in why4 or "中央目录" in why4, why4)

        # 只有头没有内容的极小文件
        tiny = Path(td) / "tiny.zip"
        tiny.write_bytes(b"PK\x03\x04" + b"\x00" * 4)
        ok5, why5 = check_zip_complete(str(tiny))
        check("过小文件被拒", ok5 is False and "过小" in why5, why5)


def t_download_complete_dispatch() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        # zip 走 EOCD 校验
        zp = Path(td) / "a.zip"
        zp.write_bytes(_make_zip(["a.srt"]))
        ok, _ = check_download_complete(str(zp), "zip", None, "application/zip")
        check("zip 类型走 EOCD 校验", ok is True, "应通过")

        tp = Path(td) / "a.zip"
        tp.write_bytes(_make_zip(["a.srt"])[:30])
        bad, why = check_download_complete(str(tp), "zip", None, "application/zip")
        check("截断 zip 判不完整", bad is False, why)

        # 裸字幕只比长度
        sp = Path(td) / "a.srt"
        sp.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nx\n")
        ok2, _ = check_download_complete(str(sp), None, sp.stat().st_size, "text/plain")
        check("裸字幕比长度通过", ok2 is True, "应通过")
        bad2, why2 = check_download_complete(str(sp), None, 99999, "text/plain")
        check("裸字幕长度不符被拒", bad2 is False and "长度不符" in why2, why2)


# ---------- 4. GBK 文件名 ----------

def t_gbk_names() -> None:
    import tempfile

    for names in (["片子.chs.srt"], ["魔女嘉莉.Carrie.1976.chs.srt"],
                  ["魔女嘉莉.Carrie.1976.chs.idx"]):
        raw = _make_zip(names, gbk=True)
        import zipfile as zf

        with zf.ZipFile(io.BytesIO(raw)) as z:
            info = z.infolist()[0]
            got = decode_zip_name(info)
            check(f"GBK 名反解: {names[0]}", got == names[0], f"{got!r} != {names[0]!r}")

    # UTF-8 标志位的名字不该被二次转码
    raw = _make_zip(["中文.简体.srt"], gbk=False)
    import zipfile as zf

    with zf.ZipFile(io.BytesIO(raw)) as z:
        got = decode_zip_name(z.infolist()[0])
        check("UTF-8 名保持原样", got == "中文.简体.srt", repr(got))


def t_gbk_extract() -> None:
    """GBK 名的包解开后，后缀要能正常识别为字幕（否则过滤会把真字幕丢掉）。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "gbk.zip"
        zp.write_bytes(_make_zip(["魔女嘉莉.Carrie.1976.chs.srt"], gbk=True))
        out = extract(zp, Path(td) / "out")
        check("GBK 包解出 1 个字幕", len(out) == 1, str(out))
        check("解出文件名已修正", out and "魔女嘉莉" in out[0].name, str([p.name for p in out]))


# ---------- 5. 图形字幕过滤 ----------

def t_graphic_filter() -> None:
    for n in ["a.sup", "a.idx", "a.sub", "a.mks", "a.pgs"]:
        check(f"图形字幕被识别 {n}", is_graphic_sub(n) is True, n)
    for n in ["a.srt", "a.ass", "a.ssa", "a.vtt"]:
        check(f"文本字幕不误判 {n}", is_graphic_sub(n) is False, n)
    check("SUB_EXT 不再含图形扩展", not ({".sup", ".sub", ".idx"} & SUB_EXT), str(SUB_EXT))

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "mix.zip"
        zp.write_bytes(_make_zip(["pkg/a.sup", "pkg/b.sub", "pkg/c.idx", "pkg/d.chs.srt"]))
        out = extract(zp, Path(td) / "out")
        names = sorted(p.name for p in out)
        check("只解出文本字幕", names == ["d.chs.srt"], str(names))

    # 全是图形字幕 → 直接判失败（不给候选）
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "graphic.zip"
        zp.write_bytes(_make_zip(["x.sup", "y.sub", "y.idx"]))
        try:
            extract(zp, Path(td) / "out")
            check("包内只有图形字幕 → 报错", False, "没抛错")
        except ValueError as e:
            check("包内只有图形字幕 → 报错", "字幕" in str(e), str(e))


def t_format_dispatch() -> None:
    """真机教训：只按 zip 的 EOCD 校验会把 rar/7z 误判成截断。"""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        # 造一个「像样的」rar：RAR4 头 + 结尾标记
        rp = Path(td) / "a.rar"
        rp.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 100 + b"\xc4\x3d\x7b\x00")
        ok, why = check_download_complete(str(rp), "rar", rp.stat().st_size, "application/x-rar")
        check("完整 rar 通过（不再被 zip 规则误判）", ok is True, why)

        trunc = Path(td) / "t.rar"
        trunc.write_bytes(b"Rar!\x1a\x07\x00" + b"\x00" * 100)
        ok2, why2 = check_download_complete(str(trunc), "rar", 200, "application/x-rar")
        check("截断 rar（长度不符）被拒", ok2 is False and "长度不符" in why2, why2)

        # 7z：以 0x00 结尾
        sp = Path(td) / "a.7z"
        sp.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x11" * 100 + b"\x00")
        ok3, why3 = check_download_complete(str(sp), "7z", sp.stat().st_size, "application/x-7z")
        check("完整 7z 通过", ok3 is True, why3)

        st = Path(td) / "t.7z"
        st.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x11" * 100)
        ok4, why4 = check_download_complete(str(st), "7z", None, "application/x-7z")
        check("截断 7z 被拒", ok4 is False, why4)

        # 类型未知时按 magic 自动分派
        ok5, _ = check_download_complete(str(rp), None, None, "application/octet-stream")
        check("未知 kind 时按 magic 认出 rar", ok5 is True, "应通过")

        zp = Path(td) / "a.zip"
        zp.write_bytes(_make_zip(["a.srt"]))
        ok6, _ = check_download_complete(str(zp), None, None, "application/octet-stream")
        check("未知 kind 时按 magic 认出 zip", ok6 is True, "应通过")


def t_bare_subtitle_download() -> None:
    """裸字幕直链：临时名（*.part）+ BOM 开头，必须认出来（真机踩过）。"""
    import tempfile

    from app.pack import detect_sub_format

    payload = b"\xef\xbb\xbf1\r\n00:01:18,725 --> 00:01:20,394\r\n\xe5\x97\xa8\r\n"
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "1791591491349.srt.part"
        p.write_bytes(payload)
        check("嗅探带 BOM 的 srt", detect_sub_format(p) == ".srt", str(detect_sub_format(p)))

        out = extract(p, Path(td) / "out")
        check("裸字幕从 *.part 解出", len(out) == 1, str(out))
        check("落盘名补了 .srt", out and out[0].name.endswith(".srt"), str(out))
        check("没有残留 .part", out and not out[0].name.endswith(".part"), str(out))
        check("内容保留 BOM 之后的正文", out and "00:01:18" in out[0].read_text(encoding="utf-8-sig"),
              str(out))

    # 真不是字幕的，仍要报错
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "junk.part"
        p.write_bytes(b"\x00\x01\x02\x03not a subtitle at all")
        try:
            extract(p, Path(td) / "out")
            check("非字幕内容仍报错", False, "没抛错")
        except ValueError as e:
            check("非字幕内容仍报错", "压缩格式" in str(e) or "字幕" in str(e), str(e))


def main() -> int:
    t_zip_complete()
    t_download_complete_dispatch()
    t_format_dispatch()
    t_gbk_names()
    t_gbk_extract()
    t_graphic_filter()
    t_bare_subtitle_download()
    print()
    print(f"通过 {PASSED} 项")
    if FAILED:
        print(f"{len(FAILED)} 项失败: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
