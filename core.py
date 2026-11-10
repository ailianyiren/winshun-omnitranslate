# -*- coding: utf-8 -*-
"""
OmniTranslate (顺译 · 多源翻译) Core Pure Functions:
- Language detection (CJK vs Latin word ratio heuristic) & resolution
- Word & phrase detection and bilingual dictionary prompts
- Youdao keyless dictionary URL & JSON parser
- Cloud provider cryptographic signing: Baidu MD5, Youdao v3 SHA256,
  Volcengine HMAC-SHA256 SigV4, Tencent TC3-HMAC-SHA256
- AI endpoint resolution, vendor detection, max_tokens & thinking tiers
- Streaming text accumulator with <think>...</think> and <原文>...</原文> stripping
- Translation history storage & CSV / Anki TSV export
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# 1. Language Model, Detection & Resolution (Ported from Lang.cs)
# ---------------------------------------------------------------------------

LANG_META: dict[str, tuple[str, str]] = {
    "zh-Hans": ("简体中文", "Simplified Chinese"),
    "zh-Hant": ("繁体中文", "Traditional Chinese"),
    "en": ("英语", "English"),
    "ja": ("日语", "Japanese"),
    "ko": ("韩语", "Korean"),
    "fr": ("法语", "French"),
    "de": ("德语", "German"),
    "es": ("西班牙语", "Spanish"),
    "ru": ("俄语", "Russian"),
    "pt": ("葡萄牙语", "Portuguese"),
    "it": ("意大利语", "Italian"),
}

LANG_ALIASES: dict[str, str] = {
    "zh": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "chs": "zh-Hans",
    "cn": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-hant": "zh-Hant",
    "cht": "zh-Hant",
    "tw": "zh-Hant",
    "en": "en",
    "en-us": "en",
    "en-gb": "en",
    "ja": "ja",
    "jp": "ja",
    "ja-jp": "ja",
    "ko": "ko",
    "kr": "ko",
    "ko-kr": "ko",
    "fr": "fr",
    "fr-fr": "fr",
    "de": "de",
    "de-de": "de",
    "es": "es",
    "es-es": "es",
    "ru": "ru",
    "ru-ru": "ru",
    "pt": "pt",
    "pt-br": "pt",
    "pt-pt": "pt",
    "it": "it",
    "it-it": "it",
}

_KANA_RE = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
_HANGUL_RE = re.compile(r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F]")
_HAN_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_LATIN_STRIP_RE = re.compile(r"[A-Za-z0-9]+")
_INLINE_PREFIX_RE = re.compile(r"^:([A-Za-z-]+)\s+(.+)$", re.DOTALL)


def normalize_lang(code: str | None) -> str | None:
    if not code or not isinstance(code, str):
        return None
    return LANG_ALIASES.get(code.strip().lower())


def lang_title(code: str, zh: bool = True) -> str:
    norm = normalize_lang(code) or "en"
    zh_title, en_title = LANG_META.get(norm, ("英语", "English"))
    return zh_title if zh else en_title


def lang_english_name(code: str) -> str:
    norm = normalize_lang(code) or "en"
    return LANG_META.get(norm, ("英语", "English"))[1]


def is_chinese(code: str | None) -> bool:
    return code in ("zh-Hans", "zh-Hant")


def same_language(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return a == b or (is_chinese(a) and is_chinese(b))


def detect_lang(text: str, preferring: list[str] | None = None) -> str | None:
    """
    Ported from KittyTools.Core.Langs.Detect:
    When CJK characters are mixed with English words (e.g. '请帮我 review 一下这个 PR'),
    if cjk_count >= latin_words_count, only inspect the CJK part so it is not misclassified as English.
    Pure numbers/symbols return None.
    """
    if not text:
        return None
    sample = text[:2000]
    kana = len(_KANA_RE.findall(sample))
    hangul = len(_HANGUL_RE.findall(sample))
    han = len(_HAN_RE.findall(sample))
    cyrillic = len(_CYRILLIC_RE.findall(sample))
    latin_words = len(_LATIN_WORD_RE.findall(sample))
    latin_letters = len(_LATIN_LETTER_RE.findall(sample))

    cjk = kana + hangul + han
    if cjk > 0 and cjk >= latin_words:
        only_cjk = _LATIN_STRIP_RE.sub(" ", sample)
        if _KANA_RE.search(only_cjk):
            return "ja"
        if _HANGUL_RE.search(only_cjk):
            return "ko"
        if _HAN_RE.search(only_cjk):
            if preferring:
                for p in preferring:
                    if is_chinese(p):
                        return p
            return "zh-Hans"

    if cyrillic > 0:
        return "ru"
    if latin_letters > 0:
        return "en"
    return None


def preferred_pair(first_code: str | None, second_code: str | None) -> tuple[str, str]:
    first = normalize_lang(first_code) or "zh-Hans"
    second = normalize_lang(second_code) or "en"
    if same_language(first, second):
        second = "zh-Hans" if first == "en" else "en"
    return first, second


@dataclass(frozen=True, slots=True)
class LangPlan:
    source: str | None      # None means auto-detect on provider side
    target: str             # Concrete target language code
    detected: str | None    # Locally detected source language
    fell_back: bool = False


def resolve_lang_plan(
    text: str,
    first_code: str | None = "zh-Hans",
    second_code: str | None = "en",
    explicit_source: str | None = None,
    explicit_target: str | None = None,
) -> LangPlan:
    first, second = preferred_pair(first_code, second_code)
    source = normalize_lang(explicit_source)
    target = normalize_lang(explicit_target)
    detected = detect_lang(text, preferring=[first, second])
    original = source or detected
    auto_target = second if (original and same_language(original, first)) else first

    if target is None or target == source:
        return LangPlan(source=source, target=auto_target, detected=detected, fell_back=False)
    if source is None and original == target:
        return LangPlan(source=None, target=auto_target, detected=detected, fell_back=True)
    return LangPlan(source=source, target=target, detected=detected, fell_back=False)


def parse_inline_target(raw_text: str) -> tuple[str | None, str]:
    """
    Supports inline target prefix such as `:en 你好` or `:ja Hello`.
    Returns (target_lang_or_None, cleaned_text).
    """
    m = _INLINE_PREFIX_RE.match(raw_text.strip())
    if m:
        lang = normalize_lang(m.group(1))
        if lang:
            return lang, m.group(2).strip()
    return None, raw_text.strip()


def join_paragraph_lines(text: str) -> str:
    """
    Ported from KittyTools paragraph line joining (F5):
    Preserves paragraph breaks (blank lines), joins single soft line breaks within a paragraph.
    """
    if not text or "\n" not in text and "\r" not in text:
        return text
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = re.split(r"\n\s*\n", normalized)
    joined_paras: list[str] = []
    for p in paragraphs:
        lines = [line.strip() for line in p.split("\n") if line.strip()]
        if not lines:
            continue
        # Join CJK neighbors without space, Latin neighbors with space
        acc = lines[0]
        for nxt in lines[1:]:
            if acc and nxt and _is_cjk_char(acc[-1]) and _is_cjk_char(nxt[0]):
                acc += nxt
            elif acc.endswith("-") and len(acc) > 1 and acc[-2].isalpha() and nxt[0].isalpha():
                acc = acc[:-1] + nxt
            else:
                acc += " " + nxt
        joined_paras.append(acc)
    return "\n\n".join(joined_paras)


# ---------------------------------------------------------------------------
# 2. Word & Phrase Lookup (Ported from WordLookup.cs)
# ---------------------------------------------------------------------------

MAX_LATIN_WORD_LEN = 40
MAX_CJK_WORD_LEN = 4


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x30FF
        or 0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0xAC00 <= cp <= 0xD7AF
    )


def is_word(text: str | None) -> bool:
    if not text:
        return False
    val = text.strip()
    if not val or "\n" in val or "\r" in val:
        return False
    cjk_count = sum(1 for ch in val if _is_cjk_char(ch))
    if cjk_count > 0:
        return cjk_count == len(val) and len(val) <= MAX_CJK_WORD_LEN
    if len(val) > MAX_LATIN_WORD_LEN:
        return False
    if any(not (ch.isalpha() or ch in ("-", "'", "’", " ")) for ch in val):
        return False
    words = [w for w in val.split(" ") if w]
    if len(words) != 1 or not words[0][0].isalpha():
        return False
    # Exclude camelCase / PascalCase identifiers from single-word dictionary mode
    w0 = words[0]
    if any(ch.isupper() for ch in w0[1:]) and any(ch.islower() for ch in w0):
        return False
    return True


_CAMEL_BOUNDARY_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")
_CODE_STOP_WORDS = {"the", "a", "an", "of", "to", "in", "for", "on", "at"}


def split_code_identifier(text: str | None) -> str | None:
    """
    Detects programming identifiers (camelCase, PascalCase, snake_case, SCREAMING_SNAKE_CASE)
    and splits them into lowercase space-separated words for natural translation.
    Returns None if the input is normal prose or a regular single word.
    """
    if not text:
        return None
    val = text.strip()
    if len(val) < 4 or len(val) > 80 or " " in val or "\n" in val or "\r" in val:
        return None
    if not all(ch.isascii() and (ch.isalnum() or ch in ("_", "-")) for ch in val):
        return None

    # 1. snake_case or SCREAMING_SNAKE_CASE
    if "_" in val:
        parts = [p for p in val.split("_") if p]
        if len(parts) >= 2 and all(any(c.isalpha() for c in p) for p in parts):
            expanded: list[str] = []
            for p in parts:
                s = _CAMEL_BOUNDARY_1.sub(r"\1 \2", p)
                s = _CAMEL_BOUNDARY_2.sub(r"\1 \2", s)
                expanded.extend(s.lower().split())
            return " ".join(expanded) if len(expanded) >= 2 else None

    # 2. kebab-case with 3+ segments (avoid splitting normal hyphenated words like 'well-known')
    if "-" in val and "_" not in val:
        parts = [p for p in val.split("-") if p]
        if len(parts) >= 3 and all(p.isalnum() for p in parts):
            return " ".join(p.lower() for p in parts)

    # 3. camelCase or PascalCase
    if val.isalnum() and any(c.isupper() for c in val[1:]) and any(c.islower() for c in val):
        s = _CAMEL_BOUNDARY_1.sub(r"\1 \2", val)
        s = _CAMEL_BOUNDARY_2.sub(r"\1 \2", s)
        words = [w.lower() for w in s.split() if w]
        if len(words) >= 2:
            return " ".join(words)

    return None


def generate_code_names(english_text: str | None) -> dict[str, str] | None:
    """
    Converts a short English translation (2 to 5 words) into standard developer variable names:
    camelCase, PascalCase, snake_case, CONSTANT_CASE, kebab-case.
    Returns None for long sentences or non-ASCII text.
    """
    if not english_text:
        return None
    cleaned = english_text.strip().strip(".,;:!?\"'()[]{}")
    if not cleaned or "\n" in cleaned or len(cleaned) > 48:
        return None
    if not all(ch.isascii() and (ch.isalnum() or ch in (" ", "-", "_")) for ch in cleaned):
        return None
    raw_words = [w.lower() for w in re.split(r"[\s_\-]+", cleaned) if w and w.isalnum()]
    if len(raw_words) < 2 or len(raw_words) > 6:
        return None
    words = [w for w in raw_words if w not in _CODE_STOP_WORDS]
    if len(words) < 2:
        words = raw_words
    if len(words) < 2 or len(words) > 5 or not words[0][0].isalpha():
        return None
    camel = words[0] + "".join(w.capitalize() for w in words[1:])
    pascal = "".join(w.capitalize() for w in words)
    snake = "_".join(words)
    constant = snake.upper()
    kebab = "-".join(words)
    return {
        "camel": camel,
        "pascal": pascal,
        "snake": snake,
        "constant": constant,
        "kebab": kebab,
    }


def is_phrase(text: str | None) -> bool:
    if not text:
        return False
    val = text.strip()
    if not val or len(val) > MAX_LATIN_WORD_LEN or "\n" in val or "\r" in val:
        return False
    words = [w for w in val.split(" ") if w]
    if not (2 <= len(words) <= 3):
        return False
    return all(w[0].isalpha() and all(ch.isalpha() or ch in ("-", "'", "’") for ch in w) for w in words)


def _word_example(target_lang: str) -> tuple[str, str]:
    if same_language(target_lang, "zh-Hans"):
        return (
            "run",
            "美 /rʌn/  英 /rʌn/\nv. 跑；奔跑；运转；经营\nn. 跑步；一段时间；连续\n例：I run every morning. 我每天早上跑步。",
        )
    return (
        "苹果",
        "píngguǒ\nn. apple; apple tree\nExample: 我每天吃一个苹果。 I eat an apple every day.",
    )


def zhipu_word_prompt(word: str, target_lang: str) -> str:
    ex_word, ex_entry = _word_example(target_lang)
    target_title = lang_title(target_lang, zh=True)
    return (
        f"你是一部简明词典。把输入的词写成一条词条，释义用{target_title}，格式和示例完全一样：第一行读音，"
        "然后每个词性一行，最后一到两行例句。只输出词条本身，不要任何说明。\n\n"
        f"输入：{ex_word}\n输出：\n{ex_entry}\n\n输入：{word}\n输出："
    )


def ai_word_prompt(target_lang: str) -> str:
    ex_word, ex_entry = _word_example(target_lang)
    target_en = lang_english_name(target_lang)
    return (
        "You are a concise bilingual dictionary. Write the user's word as one dictionary entry with meanings in "
        f"{target_en}, in exactly the same format as the example: pronunciation on the first line, one line "
        "per part of speech, then one or two example sentences with translations. Output only the entry.\n\n"
        f"Example input: {ex_word}\nExample output:\n{ex_entry}"
    )


# ---------------------------------------------------------------------------
# 3. Youdao Dictionary (Ported from YoudaoDict.cs)
# ---------------------------------------------------------------------------

def youdao_dict_url(word: str) -> str:
    q = urllib.parse.quote(word.strip(), safe="")
    dicts = urllib.parse.quote('{"count":99,"dicts":[["ec","ce","blng_sents_part"]]}', safe="")
    return f"https://dict.youdao.com/jsonapi?q={q}&dicts={dicts}"


@dataclass(slots=True)
class DictEntry:
    phonetic: str
    definitions: list[str]
    forms: str
    example: str
    full_text: str


def parse_youdao_dict(root: Any) -> DictEntry | None:
    """Ported from KittyTools.Core.YoudaoDict.Entry."""
    if not isinstance(root, dict):
        return None

    phonetic_line = ""
    definitions: list[str] = []
    forms_line = ""
    example_line = ""

    ec_word = _first_word_section(root, "ec")
    ce_word = _first_word_section(root, "ce")

    if isinstance(ec_word, dict):
        phonetics: list[str] = []
        for label, key in (("美", "usphone"), ("英", "ukphone")):
            val = ec_word.get(key)
            if isinstance(val, str) and val.strip():
                phonetics.append(f"{label} /{val.strip()}/")
        if phonetics:
            phonetic_line = "  ".join(phonetics)

        for tr in _as_list(ec_word.get("trs")):
            line_obj = _first_tr_line(tr)
            if isinstance(line_obj, dict):
                for item in _as_list(line_obj.get("i")):
                    if isinstance(item, str) and item.strip():
                        definitions.append(item.strip())
                        break

        forms: list[str] = []
        for item in _as_list(ec_word.get("wfs")):
            if isinstance(item, dict) and isinstance(item.get("wf"), dict):
                wf = item["wf"]
                name = wf.get("name")
                val = wf.get("value")
                if isinstance(name, str) and isinstance(val, str) and name and val:
                    forms.append(f"{name} {val}")
        if definitions and forms:
            forms_line = "变形：" + "；".join(forms)

    elif isinstance(ce_word, dict):
        phone = ce_word.get("phone")
        if isinstance(phone, str) and phone.strip():
            phonetic_line = phone.strip()

        for tr in _as_list(ce_word.get("trs")):
            line_obj = _first_tr_line(tr)
            if not isinstance(line_obj, dict):
                continue
            parts: list[str] = []
            for item in _as_list(line_obj.get("i")):
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("#text"), str):
                    parts.append(item["#text"])
            joined = "".join(parts).strip()
            if not joined:
                continue
            pos = line_obj.get("pos")
            prefix = f"{pos.strip()} " if isinstance(pos, str) and pos.strip() else ""
            definitions.append(prefix + joined)

    if not definitions:
        return None

    blng = root.get("blng_sents_part")
    if isinstance(blng, dict):
        pairs = _as_list(blng.get("sentence-pair"))
        if pairs and isinstance(pairs[0], dict):
            sent = pairs[0].get("sentence")
            trans = pairs[0].get("sentence-translation")
            if isinstance(sent, str) and isinstance(trans, str) and sent.strip() and trans.strip():
                example_line = f"例：{sent.strip()} {trans.strip()}"

    lines: list[str] = []
    if phonetic_line:
        lines.append(phonetic_line)
    lines.extend(definitions)
    if forms_line:
        lines.append(forms_line)
    if example_line:
        lines.append(example_line)

    return DictEntry(
        phonetic=phonetic_line,
        definitions=definitions,
        forms=forms_line,
        example=example_line,
        full_text="\n".join(lines),
    )


def _first_word_section(root: dict, section: str) -> dict | None:
    part = root.get(section)
    if not isinstance(part, dict):
        return None
    words = part.get("word")
    if isinstance(words, list) and words and isinstance(words[0], dict):
        return words[0]
    return None


def _first_tr_line(tr: Any) -> dict | None:
    if not isinstance(tr, dict):
        return None
    sub = tr.get("tr")
    if isinstance(sub, list) and sub and isinstance(sub[0], dict):
        l_obj = sub[0].get("l")
        if isinstance(l_obj, dict):
            return l_obj
    return None


def _as_list(val: Any) -> list:
    return val if isinstance(val, list) else []


# ---------------------------------------------------------------------------
# 4. Cloud Provider Signing (Ported from TranslateSigning.cs)
# ---------------------------------------------------------------------------

def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().lower()


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest().lower()


def hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def hmac_hex(key: bytes, msg: str) -> str:
    return hmac_sha256(key, msg).hex().lower()


# -- Baidu --

def baidu_sign(app_id: str, text: str, salt: str, secret: str) -> str:
    return md5_hex(f"{app_id}{text}{salt}{secret}")


def baidu_code(lang: str | None) -> str:
    if not lang:
        return "auto"
    return {
        "zh-Hans": "zh",
        "zh-Hant": "cht",
        "ja": "jp",
        "ko": "kor",
        "fr": "fra",
        "es": "spa",
    }.get(lang, lang)


# -- Youdao v3 --

def youdao_sign(app_key: str, text: str, salt: str, curtime: str, secret: str) -> str:
    inp = text if len(text) <= 20 else f"{text[:10]}{len(text)}{text[-10:]}"
    return sha256_hex(f"{app_key}{inp}{salt}{curtime}{secret}")


def youdao_code(lang: str | None) -> str:
    if not lang:
        return "auto"
    return {
        "zh-Hans": "zh-CHS",
        "zh-Hant": "zh-CHT",
    }.get(lang, lang)


# -- Volcengine --

VOLCENGINE_HOST = "open.volcengineapi.com"
VOLCENGINE_QUERY = "Action=TranslateText&Version=2020-06-01"


def volcengine_code(lang: str | None) -> str:
    if not lang:
        return "auto"
    if lang == "zh-Hans":
        return "zh"
    return lang


def volcengine_headers(
    access_key: str,
    secret_key: str,
    body_bytes: bytes,
    utc_now: datetime.datetime | None = None,
) -> dict[str, str]:
    region = "cn-north-1"
    service = "translate"
    dt = utc_now or datetime.datetime.now(datetime.timezone.utc)
    x_date = dt.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    body_hash = sha256_hex(body_bytes)
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_request = "\n".join([
        "POST",
        "/",
        VOLCENGINE_QUERY,
        f"content-type:application/json\nhost:{VOLCENGINE_HOST}\nx-content-sha256:{body_hash}\nx-date:{x_date}\n",
        signed_headers,
        body_hash,
    ])
    scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        x_date,
        scope,
        sha256_hex(canonical_request),
    ])
    k = secret_key.encode("utf-8")
    for part in (short_date, region, service, "request"):
        k = hmac_sha256(k, part)
    signature = hmac_hex(k, string_to_sign)
    return {
        "Authorization": f"HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}",
        "Content-Type": "application/json",
        "Host": VOLCENGINE_HOST,
        "X-Content-Sha256": body_hash,
        "X-Date": x_date,
    }


# -- Tencent Cloud TMT --

TENCENT_HOST = "tmt.tencentcloudapi.com"


def tencent_code(lang: str | None) -> str:
    if not lang:
        return "auto"
    return {
        "zh-Hans": "zh",
        "zh-Hant": "zh-TW",
    }.get(lang, lang)


def tencent_headers(
    secret_id: str,
    secret_key: str,
    body_bytes: bytes,
    unix_seconds: int | None = None,
) -> dict[str, str]:
    ts = int(time.time()) if unix_seconds is None else int(unix_seconds)
    content_type = "application/json; charset=utf-8"
    date_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
    canonical_request = "\n".join([
        "POST",
        "/",
        "",
        f"content-type:{content_type}\nhost:{TENCENT_HOST}\n",
        "content-type;host",
        sha256_hex(body_bytes),
    ])
    scope = f"{date_str}/tmt/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(ts),
        scope,
        sha256_hex(canonical_request),
    ])
    k = ("TC3" + secret_key).encode("utf-8")
    for part in (date_str, "tmt", "tc3_request"):
        k = hmac_sha256(k, part)
    signature = hmac_hex(k, string_to_sign)
    return {
        "Authorization": f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders=content-type;host, Signature={signature}",
        "Content-Type": content_type,
        "Host": TENCENT_HOST,
        "X-TC-Action": "TextTranslate",
        "X-TC-Timestamp": str(ts),
        "X-TC-Version": "2018-03-21",
        "X-TC-Region": "ap-beijing",
    }


# ---------------------------------------------------------------------------
# 5. AI Endpoint, Thinking Tiers & StreamingText (Ported from AiEndpoint.cs & StreamingText.cs)
# ---------------------------------------------------------------------------

def resolve_ai_endpoint(raw: str | None, protocol: str = "openai") -> str | None:
    proto = (protocol or "openai").strip().lower()
    val = (raw or "").strip()
    if not val and proto == "anthropic":
        val = "https://api.anthropic.com"
    if not val:
        return None
    if "://" not in val:
        val = "https://" + val
    val = val.rstrip("/")
    suffix = "/messages" if proto == "anthropic" else "/chat/completions"
    if suffix.lower() in val.lower():
        return val
    last_seg = val.split("/")[-1].lower()
    is_versioned = last_seg == "openai" or (
        last_seg.startswith("v") and len(last_seg) > 1 and all(c.isdigit() or c == "." for c in last_seg[1:])
    )
    return val + (suffix if is_versioned else "/v1" + suffix)


def is_local_network(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or host.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return "." not in host


def vendor_of(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return "openai"
    if port == 11434:
        return "ollama"
    if "bigmodel.cn" in host or "zhipuai" in host:
        return "zhipu"
    if "deepseek" in host:
        return "deepseek"
    if "dashscope" in host or "aliyuncs" in host:
        return "qwen"
    if "siliconflow" in host:
        return "siliconflow"
    if "moonshot" in host:
        return "kimi"
    if "volces.com" in host or "volcengine" in host:
        return "doubao"
    if "openrouter" in host:
        return "openrouter"
    if "anthropic" in host:
        return "anthropic"
    if "openai.com" in host:
        return "openai"
    return "openai"


def max_tokens_of(url: str, protocol: str = "openai") -> int | None:
    proto = (protocol or "openai").lower()
    if proto == "azure":
        return None
    vendor = vendor_of(url)
    if vendor == "openai":
        return None
    if vendor == "zhipu":
        return 1024
    return 4096


def thinking_tiers(url: str, protocol: str = "openai") -> list[dict[str, Any]]:
    proto = (protocol or "openai").lower()
    disabled = {"thinking": {"type": "disabled"}}
    if proto == "anthropic":
        return [disabled, {}]
    if proto == "azure":
        return [{"reasoning_effort": "none"}, {"reasoning_effort": "minimal"}, {"reasoning_effort": "low"}, {}]
    if is_local_network(url):
        kwargs = {"chat_template_kwargs": {"enable_thinking": False}}
        both = {"chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
        return [both, kwargs, {}]
    vendor = vendor_of(url)
    if vendor in ("zhipu", "kimi", "doubao"):
        return [disabled, {"reasoning_effort": "low"}, {}]
    if vendor == "deepseek":
        return [disabled, {}]
    if vendor in ("qwen", "siliconflow"):
        return [{"enable_thinking": False}, {}]
    if vendor == "openrouter":
        return [{"reasoning": {"effort": "none"}}, {}]
    return [{}]


SOURCE_OPEN = "<原文>"
SOURCE_CLOSE = "</原文>"


def strip_source_tags(text: str) -> str:
    if text.startswith(SOURCE_OPEN):
        text = text[len(SOURCE_OPEN):]
    elif text and SOURCE_OPEN.startswith(text):
        return ""
    if text.endswith(SOURCE_CLOSE):
        text = text[:-len(SOURCE_CLOSE)]
    else:
        for length in range(len(SOURCE_CLOSE) - 1, 1, -1):
            if text.endswith(SOURCE_CLOSE[:length]):
                text = text[:-length]
                break
    return text.strip()


class StreamingText:
    """
    Accumulates streaming deltas, strips leading <think>...</think> (waiting while incomplete),
    strips <原文>...</原文> tags, and strips outer quotation marks on Final.
    """

    __slots__ = ("_parts",)

    def __init__(self) -> None:
        self._parts: list[str] = []

    def append(self, delta: str) -> None:
        if delta:
            self._parts.append(delta)

    @property
    def raw(self) -> str:
        return "".join(self._parts)

    @property
    def visible(self) -> str:
        text = self.raw
        trimmed = text.lstrip()
        if trimmed.startswith("<think>"):
            end = trimmed.find("</think>")
            if end < 0:
                return ""
            text = trimmed[end + len("</think>"):]
        elif trimmed and "<think>".startswith(trimmed):
            return ""
        return strip_source_tags(text.strip())

    @property
    def is_thinking(self) -> bool:
        trimmed = self.raw.lstrip()
        if not trimmed:
            return False
        if trimmed.startswith("<think>"):
            return "</think>" not in trimmed
        return "<think>".startswith(trimmed)

    @property
    def final(self) -> str:
        text = self.visible
        for open_q, close_q in (('"', '"'), ("“", "”"), ("「", "」")):
            if len(text) >= 2 and text.startswith(open_q) and text.endswith(close_q):
                inner = text[1:-1]
                if close_q not in inner:
                    return inner.strip()
        return text


# ---------------------------------------------------------------------------
# 6. History & Export (Ported from HistoryStore & HistoryExport.cs)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class HistoryRecord:
    id: str
    source: str
    result: str
    target_lang: str
    service: str
    created_at: int  # Unix ms
    favorite: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "result": self.result,
            "targetLang": self.target_lang,
            "service": self.service,
            "createdAt": self.created_at,
            "favorite": self.favorite,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> HistoryRecord:
        return HistoryRecord(
            id=str(d.get("id") or ""),
            source=str(d.get("source") or ""),
            result=str(d.get("result") or ""),
            target_lang=str(d.get("targetLang") or "zh-Hans"),
            service=str(d.get("service") or ""),
            created_at=int(d.get("createdAt") or int(time.time() * 1000)),
            favorite=bool(d.get("favorite", False)),
        )


def format_csv(rows: list[HistoryRecord]) -> str:
    """Ported from KittyTools.Core.HistoryExport.Csv (with formula-injection guard)."""
    lines = ["原文,译文,目标语言,服务,时间,收藏"]
    for r in rows:
        dt_str = datetime.datetime.fromtimestamp(r.created_at / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
        fields = [
            r.source,
            r.result,
            lang_title(r.target_lang, zh=True),
            r.service,
            dt_str,
            "是" if r.favorite else "",
        ]
        lines.append(",".join(_csv_field(f) for f in fields))
    return "\r\n".join(lines) + "\r\n"


def _csv_field(field: str) -> str:
    if field and field[0] in "=+-@":
        field = "'" + field
    if any(c in field for c in (',', '"', '\n', '\r')):
        return '"' + field.replace('"', '""') + '"'
    return field


def format_anki_tsv(rows: list[HistoryRecord]) -> str:
    """Ported from KittyTools.Core.HistoryExport.AnkiTsv."""
    lines = ["#separator:tab", "#html:true"]
    for r in rows:
        lines.append(f"{_anki_field(r.source)}\t{_anki_field(r.result)}")
    return "\n".join(lines) + "\n"


def _anki_field(text: str) -> str:
    html = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\t", " ")
    html = re.sub(r"\r\n|\r|\n|\u2028|\u2029", "<br>", html)
    if html.startswith('"') or html.startswith("#") or '"' in html:
        return '"' + html.replace('"', '""') + '"'
    return html


class HistoryStore:
    """Thread-safe history store persisted in WS_DATA_DIR/history.json."""

    __slots__ = ("_data_dir", "_max_non_fav", "_lock", "_records", "_loaded")

    def __init__(self, data_dir: str, max_non_favorites: int = 500) -> None:
        self._data_dir = data_dir
        self._max_non_fav = max_non_favorites
        self._lock = threading.Lock()
        self._records: list[HistoryRecord] = []
        self._loaded = False

    def set_data_dir(self, data_dir: str) -> None:
        with self._lock:
            if data_dir and data_dir != self._data_dir:
                self._data_dir = data_dir
                self._loaded = False

    def _file_path(self) -> str:
        os.makedirs(self._data_dir, exist_ok=True)
        return os.path.join(self._data_dir, "history.json")

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = self._file_path()
        if not os.path.isfile(path):
            self._records = []
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, list):
                self._records = [HistoryRecord.from_dict(x) for x in raw if isinstance(x, dict)]
        except Exception:
            self._records = []

    def _save_locked(self) -> None:
        path = self._file_path()
        tmp = path + ".tmp"
        data = [r.to_dict() for r in self._records]
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def record(
        self,
        source: str,
        result: str,
        target_lang: str,
        service: str,
        favorite: bool | None = None,
    ) -> HistoryRecord:
        source = source.strip()
        result = result.strip()
        now_ms = int(time.time() * 1000)
        rec_id = md5_hex(f"{source}\n{target_lang}\n{service}")[:12]
        with self._lock:
            self._ensure_loaded()
            existing_fav = False
            kept: list[HistoryRecord] = []
            for r in self._records:
                if r.id == rec_id or (r.source == source and r.target_lang == target_lang and r.service == service):
                    existing_fav = existing_fav or r.favorite
                else:
                    kept.append(r)
            final_fav = existing_fav if favorite is None else favorite
            entry = HistoryRecord(
                id=rec_id,
                source=source,
                result=result,
                target_lang=target_lang,
                service=service,
                created_at=now_ms,
                favorite=final_fav,
            )
            kept.insert(0, entry)
            # Evict non-favorites beyond limit (favorites are never evicted)
            non_fav_count = 0
            pruned: list[HistoryRecord] = []
            for r in kept:
                if r.favorite:
                    pruned.append(r)
                else:
                    non_fav_count += 1
                    if non_fav_count <= self._max_non_fav:
                        pruned.append(r)
            self._records = pruned
            self._save_locked()
            return entry

    def toggle_favorite(self, rec_id: str) -> HistoryRecord | None:
        with self._lock:
            self._ensure_loaded()
            for r in self._records:
                if r.id == rec_id:
                    r.favorite = not r.favorite
                    self._save_locked()
                    return r
        return None

    def clear_non_favorites(self) -> tuple[int, int]:
        """Clears non-favorite items; returns (removed_count, kept_favorites_count)."""
        with self._lock:
            self._ensure_loaded()
            favs = [r for r in self._records if r.favorite]
            removed = len(self._records) - len(favs)
            self._records = favs
            self._save_locked()
            return removed, len(favs)

    def search(self, keyword: str = "", favorites_only: bool = False, limit: int = 100) -> list[HistoryRecord]:
        q = keyword.strip().lower()
        with self._lock:
            self._ensure_loaded()
            out: list[HistoryRecord] = []
            for r in self._records:
                if favorites_only and not r.favorite:
                    continue
                if q and q not in r.source.lower() and q not in r.result.lower() and q not in r.service.lower():
                    continue
                out.append(r)
                if len(out) >= limit:
                    break
            return out

    def stats(self) -> tuple[int, int]:
        with self._lock:
            self._ensure_loaded()
            total = len(self._records)
            favs = sum(1 for r in self._records if r.favorite)
            return total, favs

    def export_files(self, favorites_only: bool = False) -> tuple[str, str, int]:
        with self._lock:
            self._ensure_loaded()
            rows = [r for r in self._records if (not favorites_only or r.favorite)]
        export_dir = os.path.join(self._data_dir, "exports")
        os.makedirs(export_dir, exist_ok=True)
        suffix = "favorites" if favorites_only else "all"
        csv_path = os.path.join(export_dir, f"omnitranslate-{suffix}.csv")
        tsv_path = os.path.join(export_dir, f"omnitranslate-anki-{suffix}.tsv")
        # CSV with UTF-8 BOM (as required by KittyTools B12)
        with open(csv_path, "wb") as f:
            f.write(b"\xef\xbb\xbf" + format_csv(rows).encode("utf-8"))
        # Anki TSV without BOM (as required by KittyTools B12)
        with open(tsv_path, "wb") as f:
            f.write(format_anki_tsv(rows).encode("utf-8"))
        return csv_path, tsv_path, len(rows)
