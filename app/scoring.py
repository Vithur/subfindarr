"""评分与选优：简中单语优先，AI/机翻降权。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sources.base import SubtitleCandidate

DEFAULT_WEIGHTS = {
    "official_hans_mono": 100,
    "official_hans": 90,
    "hans_mono": 80,
    "hans_hant_bilingual": 60,
    "hant_only": 40,
    "unknown": 20,
}

DEFAULT_AI_KEYWORDS = ["AI翻译", "AI字幕", "机翻", "机器翻译", "gpt", "chatgpt", "whisper",
                       "deepl翻译", "auto-translate", "ai翻译"]
# 广告/水印：Kernel 预览样本硬特征（`联系QQ号:`、`QQ群：`、`热爱电影…`）
DEFAULT_AD_KEYWORDS = ["QQ群", "QQ号", "联系QQ", "热爱电影", "诚意制作"]
# 枪版源：与机翻分开计分（非 WEB-DL/BluRay，画质与字幕质量都差）
DEFAULT_CAM_KEYWORDS = ["HDTS", "TELESYNC", "TELECINE", "HDTC", "CAM"]
# 宣传噪音：营销文本，轻微降权
DEFAULT_NOISE_KEYWORDS = ["特效 HDR防炫目", "完美双语同轴合并"]
DEFAULT_HANT = ["繁體", "繁体", "CHT", "Big5", "zh-Hant", "港版", "台版"]
DEFAULT_HANS = ["简体", "简中", "CHS", "GB", "zh-Hans", "简繁", "双语"]
DEFAULT_MONO = ["单语", "简体中文", "纯简体", "英文"]

# 官方/权威来源的发布者白名单（可被配置覆盖）
DEFAULT_UPLOADER_WHITELIST = ["官方", "官译", "subhd", "字幕组", "远鉴", "深影", "人人", "TLF", "CMCT"]

GROUP_PREFIX = re.compile(r"^\[([^\]]{1,20})\]")


@dataclass
class ScoreResult:
    total: float
    tier: str
    breakdown: dict[str, float] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"total": self.total, "tier": self.tier, "breakdown": self.breakdown, "flags": self.flags}


def _hit(text: str, kws: list[str]) -> list[str]:
    low = text.lower()
    return [k for k in kws if k.lower() in low]


def _lang_source(candidate: SubtitleCandidate, scoring: dict) -> tuple[str, str]:
    """语言标签三级来源（Kernel 2026-10-16 定案）。

    1. 列表页标签（`lang_hint`，来自 `/d/{id}` 的 `<span class="p-1 fw-bold">简体</span>`）——首选
    2. 版本页 subtitle-file-name（由 adapter 塞进 `lang_hint` 的第二来源，同样走 lang_hint）
    3. 版本页标题——**不作语言来源**

    返回 (来源名, 该来源的文本)。三级都没命中时来源为 none。
    """
    hint = (candidate.lang_hint or "").strip()
    if hint:
        # adapter 已按「列表标签优先、文件名回退」拼好，并标了来源
        src = (candidate.raw or {}).get("lang_source") or "list"
        return src, hint
    raw = candidate.raw or {}
    files = raw.get("files") or []
    if files:
        return "filename", " ".join(str(f) for f in files)
    return "none", ""


def classify_lang(candidate: SubtitleCandidate, scoring: dict) -> tuple[str, list[str]]:
    """返回 (语言档, 命中标记)。档位对应 weights 的 key。

    按 Kernel 的三级规则：语言只从列表页标签 / 文件名取，**不看版本页标题**；
    「缺某语言标签」不扣分（缺标签 ≠ 不是该语言），只按出现的标签加分。
    """
    src, text = _lang_source(candidate, scoring)
    official_hint = "官方" in text or bool((candidate.raw or {}).get("official"))

    hant = _hit(text, scoring.get("hant_markers", DEFAULT_HANT))
    hans = _hit(text, scoring.get("hans_markers", DEFAULT_HANS))
    mono = _hit(text, scoring.get("mono_markers", DEFAULT_MONO))

    # 发布者/官方 badge 只作「官方」判据，不参与语言判定
    uploader_text = (candidate.uploader or "")
    official = official_hint or bool(
        _hit(uploader_text, scoring.get("uploader_whitelist", DEFAULT_UPLOADER_WHITELIST))
    )

    if src == "none":
        tier = "official_hans" if official else "unknown"
    elif hans and hant:
        tier = "hans_hant_bilingual"
    elif hans:
        if official and mono:
            tier = "official_hans_mono"
        elif official:
            tier = "official_hans"
        else:
            tier = "hans_mono"
    elif hant:
        tier = "hant_only"
    elif official:
        tier = "official_hans"
    else:
        tier = "unknown"
    return tier, hant + hans + mono


def score(candidate: SubtitleCandidate, scoring: dict | None = None) -> ScoreResult:
    scoring = scoring or {}
    weights = {**DEFAULT_WEIGHTS, **(scoring.get("weights") or {})}
    penalties = scoring.get("penalties") or {"ai_keywords": -100, "machine_translate_keywords": -100, "ocr_suspect": -30}
    ai_kws = scoring.get("ai_keywords", DEFAULT_AI_KEYWORDS)

    tier, marks = classify_lang(candidate, scoring)
    base = float(weights.get(tier, weights.get("unknown", 20)))

    breakdown: dict[str, float] = {"base": base, "tier": tier}
    flags: list[str] = []

    # 三级来源都拿不到语言标签 → 排在有标签候选之后（Kernel 规则：unknown 靠后）
    if tier == "unknown":
        breakdown["unknown_lang"] = float(scoring.get("unknown_penalty", -10.0))
        flags.append("lang-unknown")

    # 标题含 AI/机翻 → 直接淘汰级
    hits = _hit(candidate.title or "", ai_kws)
    if hits:
        breakdown["ai_penalty"] = float(penalties.get("ai_keywords", -100))
        flags.append("ai:" + ",".join(hits))

    # 广告/水印（Kernel 预览样本硬特征：联系QQ号 / QQ群： / 热爱电影…）
    ad_kws = scoring.get("ad_keywords", DEFAULT_AD_KEYWORDS)
    ad_hits = _hit((candidate.title or "") + " " + str((candidate.raw or {}).get("preview") or ""), ad_kws)
    if ad_hits:
        breakdown["ad_penalty"] = float(penalties.get("ad_keywords", -100))
        flags.append("ad:" + ",".join(ad_hits))

    # 枪版源（HDTS/TELESYNC/CAM…）——单独一类，与机翻分开计分
    cam_kws = scoring.get("cam_keywords", DEFAULT_CAM_KEYWORDS)
    cam_hits = _hit(candidate.title or "", cam_kws)
    if cam_hits:
        breakdown["cam_penalty"] = float(penalties.get("cam_keywords", -80))
        flags.append("cam:" + ",".join(cam_hits))

    # 宣传噪音：轻微降权（营销文本，不等于质量问题）
    noise_kws = scoring.get("noise_keywords", DEFAULT_NOISE_KEYWORDS)
    noise_hits = _hit(candidate.title or "", noise_kws)
    if noise_hits:
        breakdown["noise_penalty"] = float(scoring.get("noise_penalty", -5))
        flags.append("noise:" + ",".join(noise_hits))

    # [] 前缀多为转载/机翻组标记
    m = GROUP_PREFIX.match((candidate.title or "").strip())
    if m and any(x in m.group(1).lower() for x in ("ai", "gpt", "机翻", "ocr")):
        breakdown["prefix_penalty"] = float(penalties.get("machine_translate_keywords", -50))
        flags.append("prefix:" + m.group(1))

    # 图形字幕（.sup / .sub+.idx）比机翻标记更硬：需要 OCR 才能转文本，直接重罚
    raw = candidate.raw or {}
    names = " ".join([str(n) for n in (raw.get("files") or [])]) + " " + (candidate.title or "")
    fmt = (candidate.fmt or "").lower()
    if fmt in ("sup", "sub", "idx") or re.search(r"\.(sup|idx)\b", names, re.I):
        breakdown["graphic_penalty"] = float(penalties.get("graphic_sub", -200))
        flags.append("graphic:" + (fmt or "from-name"))
    elif fmt in ("srt", "ass", "ssa"):
        breakdown["format_ok"] = 0.0

    # 语言标签来源可追溯（验收时要能说清这条为什么排前面）
    src, _text = _lang_source(candidate, scoring)
    breakdown["lang_source"] = src  # type: ignore[assignment]
    breakdown["lang_from"] = src  # type: ignore[assignment]

    # 下载量/评分做小幅加权（同档位内排序用）
    if candidate.downloads:
        breakdown["popularity"] = min(10.0, candidate.downloads / 500.0)
    if candidate.rating:
        breakdown["rating"] = min(5.0, float(candidate.rating) / 2.0)

    total = sum(v for k, v in breakdown.items() if k not in ("tier", "lang_source", "lang_from"))
    return ScoreResult(total=total, tier=tier, breakdown=breakdown, flags=flags)


def rank(candidates: list[SubtitleCandidate], scoring: dict | None = None) -> list[tuple[SubtitleCandidate, ScoreResult]]:
    scored = [(c, score(c, scoring)) for c in candidates]
    scored.sort(key=lambda t: t[1].total, reverse=True)
    return scored


def ad_hit(candidate: SubtitleCandidate, scoring: dict | None = None) -> list[str]:
    """候选是否命中广告/水印词（标题或预览正文）。

    预览正文来自 `/api/sub/preview/{sid}`（Kernel 实测样本：3D 组在开头灌
    `联系QQ号:… QQ群：…`）。命中就应在下载前换候选。
    """
    scoring = scoring or {}
    kws = scoring.get("ad_keywords", DEFAULT_AD_KEYWORDS)
    text = (candidate.title or "") + " " + str((candidate.raw or {}).get("preview") or "")
    return _hit(text, kws)
