# -*- coding: utf-8 -*-
"""
OmniTranslate (顺译 · 多源翻译) Plugin for WinShun (Win顺) (`anghost.translate`)

Supports:
- Resident JSON-RPC 2.0 mode (async query worker with bounded ThreadPoolExecutor,
  per-provider LRU cache, cancel(queryId), partial() streaming, details() preview,
  action() effects, hostHotkeys registration & hotkey handling)
- Once / CLI environment fallback (WS_QUERY / WS_SETTINGS) for standalone testing
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
import threading
import time
from typing import Any

import winshun
import core
import providers
import selection

ICON_PLUGIN = "icons/plugin.png"
ICON_DICT = "icons/youdaoDict.png"
ICON_CODE = "icons/code.png"
ICON_HISTORY = "glyph:U+E81C"
ICON_ERROR = "glyph:U+E783"
ICON_STAR = "glyph:U+E735"

PROVIDER_ICONS: dict[str, str] = {
    "youdaoDict": "icons/youdaoDict.png",
    "microsoft": "icons/microsoft.png",
    "zhipu": "icons/zhipu.png",
    "ai": "icons/ai.png",
    "deepl": "icons/deepl.png",
    "baidu": "icons/baidu.png",
    "volcengine": "icons/volcengine.png",
    "tencent": "icons/tencent.png",
    "youdao": "icons/youdao.png",
    "google": "icons/google.png",
}

BUDGET_SEC = 8.5
PARTIAL_THROTTLE_SEC = 0.12

# Shared bounded thread pool to minimize OS thread creation & stack memory overhead
_PROVIDER_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="omni")

DEFAULT_SETTINGS: dict[str, Any] = {
    "firstLang": "zh-Hans",
    "secondLang": "en",
    "joinLines": True,
    "selectionHotkey": "Alt+D",
    "replaceHotkey": "Alt+Shift+D",
    "enableYoudaoDict": True,
    "enableMicrosoft": True,
    "enableZhipu": False,
    "zhipuKey": "",
    "zhipuModel": "glm-4-flash",
    "enableAi": False,
    "aiProtocol": "openai",
    "aiBaseUrl": "https://api.deepseek.com",
    "aiModel": "deepseek-chat",
    "aiKey": "",
    "enableDeepl": False,
    "deeplKey": "",
    "deeplxUrl": "",
    "enableBaidu": False,
    "baiduAppId": "",
    "baiduSecret": "",
    "enableYoudao": False,
    "youdaoAppKey": "",
    "youdaoAppSecret": "",
    "enableVolcengine": False,
    "volcAccessKey": "",
    "volcSecretKey": "",
    "enableTencent": False,
    "tencentSecretId": "",
    "tencentSecretKey": "",
    "enableGoogle": False,
    "googleKey": "",
    "microsoftKey": "",
    "microsoftRegion": "",
}

PROVIDER_SPECS: list[tuple[str, str, str, str]] = [
    # (id, setting_key, zh_name, en_name)
    ("youdaoDict", "enableYoudaoDict", "有道词典", "Youdao Dict"),
    ("microsoft", "enableMicrosoft", "微软翻译", "Microsoft Translator"),
    ("zhipu", "enableZhipu", "智谱 GLM", "Zhipu GLM"),
    ("ai", "enableAi", "AI 大模型", "AI Model"),
    ("deepl", "enableDeepl", "DeepL", "DeepL"),
    ("baidu", "enableBaidu", "百度翻译", "Baidu Translate"),
    ("volcengine", "enableVolcengine", "火山翻译", "Volcengine"),
    ("tencent", "enableTencent", "腾讯翻译", "Tencent TMT"),
    ("youdao", "enableYoudao", "有道翻译", "Youdao Cloud"),
    ("google", "enableGoogle", "Google 翻译", "Google Translate"),
]


def merged_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None:
                out[k] = v
    return out


def _bool_setting(s: dict[str, Any], key: str, default: bool = False) -> bool:
    val = s.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return bool(val)


def active_providers(
    settings: dict[str, Any],
    text: str,
    is_dict_cmd: bool,
    zh: bool = True,
) -> list[tuple[str, str]]:
    """
    Returns ordered list of (provider_id, display_name) to run for this query:
    - In `dict` command: runs youdaoDict + any enabled AI/Zhipu.
    - In `tr` command: youdaoDict only activates when input is a word or 2-3 word phrase.
    """
    word_or_phrase = core.is_word(text) or core.is_phrase(text)
    out: list[tuple[str, str]] = []

    for pid, key, zh_name, en_name in PROVIDER_SPECS:
        enabled = _bool_setting(settings, key, DEFAULT_SETTINGS.get(key, False))
        name = zh_name if zh else en_name
        if pid == "ai":
            model_name = str(settings.get("aiModel") or "").strip()
            if model_name:
                name = f"AI ({model_name})"
        elif pid == "zhipu":
            z_model = str(settings.get("zhipuModel") or "glm-4-flash").strip()
            name = f"智谱 ({z_model})" if zh else f"Zhipu ({z_model})"

        if is_dict_cmd:
            if pid == "youdaoDict":
                out.append((pid, name))
            elif pid in ("zhipu", "ai") and enabled:
                out.append((pid, name))
            continue

        if not enabled:
            continue
        if pid == "youdaoDict" and not word_or_phrase:
            continue
        out.append((pid, name))

    return out


# ---------------------------------------------------------------------------
# Provider Dispatch (with Bounded In-Memory LRU Cache)
# ---------------------------------------------------------------------------

def run_single_provider(
    pid: str,
    pname: str,
    text: str,
    plan: core.LangPlan,
    word_mode: bool,
    settings: dict[str, Any],
    on_stream: Any = None,
    timeout_sec: float = BUDGET_SEC,
    cancel_event: threading.Event | None = None,
) -> providers.ProviderResult:
    cached = providers.get_cached_result(pid, pname, text, plan.source, plan.target, word_mode)
    if cached is not None:
        return cached

    started = time.monotonic()
    try:
        if pid == "youdaoDict":
            full_text, entry = providers.query_youdao_dict(text, timeout_sec=timeout_sec, cancel_event=cancel_event)
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(
                provider_id=pid,
                provider_name=pname,
                ok=True,
                text=full_text,
                duration_ms=elapsed,
                dict_entry=entry,
            )
        elif pid == "microsoft":
            txt = providers.query_microsoft(
                text, plan.source, plan.target, settings, timeout_sec=timeout_sec, cancel_event=cancel_event
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(provider_id=pid, provider_name=pname, ok=True, text=txt, duration_ms=elapsed)
        elif pid == "zhipu":
            txt, trunc = providers.query_zhipu(
                text,
                plan.source,
                plan.target,
                word_mode,
                settings,
                on_stream=on_stream,
                timeout_sec=timeout_sec,
                cancel_event=cancel_event,
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(
                provider_id=pid, provider_name=pname, ok=True, text=txt, is_truncated=trunc, duration_ms=elapsed
            )
        elif pid == "ai":
            txt, trunc = providers.query_ai_service(
                text,
                plan.source,
                plan.target,
                word_mode,
                settings,
                on_stream=on_stream,
                timeout_sec=timeout_sec,
                cancel_event=cancel_event,
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(
                provider_id=pid, provider_name=pname, ok=True, text=txt, is_truncated=trunc, duration_ms=elapsed
            )
        elif pid == "deepl":
            txt = providers.query_deepl(
                text, plan.source, plan.target, settings, timeout_sec=timeout_sec, cancel_event=cancel_event
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(provider_id=pid, provider_name=pname, ok=True, text=txt, duration_ms=elapsed)
        elif pid == "baidu":
            txt = providers.query_baidu(
                text, plan.source, plan.target, settings, timeout_sec=timeout_sec, cancel_event=cancel_event
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(provider_id=pid, provider_name=pname, ok=True, text=txt, duration_ms=elapsed)
        elif pid == "youdao":
            txt = providers.query_youdao(
                text, plan.source, plan.target, settings, timeout_sec=timeout_sec, cancel_event=cancel_event
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(provider_id=pid, provider_name=pname, ok=True, text=txt, duration_ms=elapsed)
        elif pid == "volcengine":
            txt = providers.query_volcengine(
                text, plan.source, plan.target, settings, timeout_sec=timeout_sec, cancel_event=cancel_event
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(provider_id=pid, provider_name=pname, ok=True, text=txt, duration_ms=elapsed)
        elif pid == "tencent":
            txt = providers.query_tencent(
                text, plan.source, plan.target, settings, timeout_sec=timeout_sec, cancel_event=cancel_event
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(provider_id=pid, provider_name=pname, ok=True, text=txt, duration_ms=elapsed)
        elif pid == "google":
            txt = providers.query_google(
                text, plan.source, plan.target, settings, timeout_sec=timeout_sec, cancel_event=cancel_event
            )
            elapsed = int((time.monotonic() - started) * 1000)
            res = providers.ProviderResult(provider_id=pid, provider_name=pname, ok=True, text=txt, duration_ms=elapsed)
        else:
            raise providers.TranslateError(f"未知翻译服务 {pid}", kind="config")

        providers.put_cached_result(pid, text, plan.source, plan.target, word_mode, res)
        return res
    except providers.TranslateError as e:
        elapsed = int((time.monotonic() - started) * 1000)
        return providers.ProviderResult(
            provider_id=pid,
            provider_name=pname,
            ok=False,
            error=e.message,
            error_kind=e.kind,
            duration_ms=elapsed,
        )
    except Exception:
        elapsed = int((time.monotonic() - started) * 1000)
        return providers.ProviderResult(
            provider_id=pid,
            provider_name=pname,
            ok=False,
            error="无法完成翻译请求",
            error_kind="service",
            duration_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# Item Builders (Scheme A+B+C De-Noised UI, Brand Icons, Code Naming)
# ---------------------------------------------------------------------------

def _single_line_preview(text: str, max_len: int = 120) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return text.strip()
    joined = " · ".join(lines)
    return joined if len(joined) <= max_len else joined[: max_len - 1] + "…"


def build_provider_items(
    source_text: str,
    plan: core.LangPlan,
    ordered_specs: list[tuple[str, str]],
    states: dict[str, providers.ProviderResult],
    is_dict_cmd: bool,
    zh: bool,
    split_from_identifier: str | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    target_label = core.lang_title(plan.target, zh=zh)
    from_label = core.lang_title(plan.detected, zh=zh) if plan.detected else ("自动" if zh else "Auto")
    direction_note = f"{from_label} → {target_label}"
    if plan.fell_back and plan.detected:
        direction_note += f"（原文已是{core.lang_title(plan.detected, zh=True)}）" if zh else " (already target)"
    if split_from_identifier:
        direction_note += f" · 已拆词：{source_text}" if zh else f" · Split: {source_text}"

    # Build comparison summary for #202 details pane
    all_completed = {
        pid: st.text
        for pid, st in states.items()
        if st.ok and st.text.strip()
    }

    score = 95
    first_english_Candidate: str | None = None

    for pid, pname in ordered_specs:
        st = states.get(pid)
        row_id = f"srv-{pid}"
        icon = PROVIDER_ICONS.get(pid, ICON_DICT if is_dict_cmd else ICON_PLUGIN)

        if st is None:
            items.append(
                winshun.item(
                    row_id,
                    f"{pname}：正在翻译…" if zh else f"{pname}: Translating…",
                    f"{pname} · {direction_note}",
                    icon=icon,
                    valid=False,
                )
            )
            continue

        if st.is_streaming:
            if st.is_thinking and not st.text:
                title = f"{pname}：思考中…" if zh else f"{pname}: Thinking…"
                sub = f"{pname} · {direction_note}"
                items.append(winshun.item(row_id, title, sub, icon=icon, valid=False))
            else:
                preview_title = st.text + " ▍"
                sub = f"{pname} · 流式输出中 · {direction_note}" if zh else f"{pname} · Streaming · {direction_note}"
                items.append(
                    winshun.item(
                        row_id,
                        preview_title,
                        sub,
                        icon=icon,
                        copy=st.text,
                        score=max(50, score),
                        details=True,
                        data={
                            "kind": "translation",
                            "providerId": pid,
                            "providerName": pname,
                            "source": source_text,
                            "result": st.text,
                            "targetLang": plan.target,
                            "direction": direction_note,
                            "allResults": all_completed,
                        },
                    )
                )
            score -= 2
            continue

        if not st.ok:
            if st.error_kind == "config":
                items.append(
                    winshun.item(
                        row_id,
                        f"{pname}：{st.error}",
                        "按 Enter 打开插件设置填写密钥" if zh else "Press Enter to open plugin settings",
                        icon=ICON_ERROR,
                        url="winshun:settings",
                    )
                )
            else:
                items.append(
                    winshun.item(
                        row_id,
                        f"{pname}：{st.error}",
                        "网络或服务异常，请稍后重试" if zh else "Network or service error, please retry",
                        icon=ICON_ERROR,
                        valid=False,
                    )
                )
            continue

        # Successful result
        if st.dict_entry is not None:
            entry = st.dict_entry
            if is_dict_cmd:
                # Dedicated `dict` command: expanded multi-row breakdown
                head_title = f"{source_text}  {entry.phonetic}".strip()
                items.append(
                    winshun.item(
                        f"{row_id}-head",
                        head_title,
                        f"{pname} · 词头与音标" if zh else f"{pname} · Headword & Phonetics",
                        icon=icon,
                        copy=source_text,
                        score=98,
                        details=True,
                        data={
                            "kind": "translation",
                            "providerId": pid,
                            "providerName": pname,
                            "source": source_text,
                            "result": entry.full_text,
                            "targetLang": plan.target,
                            "direction": direction_note,
                            "allResults": all_completed,
                        },
                    )
                )
                for idx, def_line in enumerate(entry.definitions[:5]):
                    items.append(
                        winshun.item(
                            f"{row_id}-def-{idx}",
                            def_line,
                            f"{pname} · 释义" if zh else f"{pname} · Definition",
                            icon=icon,
                            copy=def_line,
                            score=94 - idx,
                            details=True,
                            data={
                                "kind": "translation",
                                "providerId": pid,
                                "providerName": pname,
                                "source": source_text,
                                "result": entry.full_text,
                                "targetLang": plan.target,
                                "direction": direction_note,
                                "allResults": all_completed,
                            },
                        )
                    )
                if entry.forms:
                    items.append(
                        winshun.item(
                            f"{row_id}-forms",
                            entry.forms,
                            f"{pname} · 词形变化" if zh else f"{pname} · Inflections",
                            icon=icon,
                            copy=entry.forms,
                            score=85,
                            details=True,
                            data={
                                "kind": "translation",
                                "providerId": pid,
                                "providerName": pname,
                                "source": source_text,
                                "result": entry.full_text,
                                "targetLang": plan.target,
                                "direction": direction_note,
                                "allResults": all_completed,
                            },
                        )
                    )
                if entry.example:
                    items.append(
                        winshun.item(
                            f"{row_id}-ex",
                            entry.example,
                            f"{pname} · 双语例句" if zh else f"{pname} · Example",
                            icon=icon,
                            copy=entry.example,
                            score=84,
                            details=True,
                            data={
                                "kind": "translation",
                                "providerId": pid,
                                "providerName": pname,
                                "source": source_text,
                                "result": entry.full_text,
                                "targetLang": plan.target,
                                "direction": direction_note,
                                "allResults": all_completed,
                            },
                        )
                    )
                continue
            else:
                # Scheme B in `tr` mode: Meaning-First headline on line 1 + full multi-line card for #117 detailBlock!
                core_defs = "；".join(entry.definitions[:2])
                phonetic_tail = f"   [{entry.phonetic}]" if entry.phonetic else ""
                first_line = f"{core_defs}{phonetic_tail}"
                full_card_title = f"{first_line}\n{entry.full_text}"
                sub_parts = [pname]
                if entry.forms:
                    sub_parts.append(entry.forms.replace("变形：", ""))
                else:
                    sub_parts.append(direction_note)
                items.append(
                    winshun.item(
                        row_id,
                        full_card_title,
                        " · ".join(sub_parts),
                        icon=icon,
                        copy=core_defs,
                        score=max(50, score),
                        details=True,
                        data={
                            "kind": "translation",
                            "providerId": pid,
                            "providerName": pname,
                            "source": source_text,
                            "result": entry.full_text,
                            "targetLang": plan.target,
                            "direction": direction_note,
                            "allResults": all_completed,
                        },
                    )
                )
                score -= 2
                continue

        # Standard translation row (Scheme A+B: clean subtitle, full multi-line title for #117)
        if first_english_Candidate is None and plan.target == "en" and pid != "youdaoDict":
            first_english_Candidate = st.text

        trunc_tag = (" · 输出已截断" if zh else " · Truncated") if st.is_truncated else ""
        sub = f"{pname} · {direction_note}{trunc_tag}"
        items.append(
            winshun.item(
                row_id,
                st.text,
                sub,
                icon=icon,
                copy=st.text,
                score=max(50, score),
                details=True,
                data={
                    "kind": "translation",
                    "providerId": pid,
                    "providerName": pname,
                    "source": source_text,
                    "result": st.text,
                    "targetLang": plan.target,
                    "direction": direction_note,
                    "allResults": all_completed,
                },
            )
        )
        score -= 2

    # Scheme C: Smart Developer Variable Naming Card when translating short terms into English
    if not is_dict_cmd and first_english_Candidate and len(source_text) <= 20:
        code_names = core.generate_code_names(first_english_Candidate)
        if code_names:
            code_summary = f"{code_names['camel']}  ·  {code_names['snake']}  ·  {code_names['pascal']}  ·  {code_names['constant']}"
            code_detail = (
                f"camelCase (小驼峰): {code_names['camel']}\n"
                f"PascalCase (大驼峰): {code_names['pascal']}\n"
                f"snake_case (下划线): {code_names['snake']}\n"
                f"CONSTANT_CASE (常量): {code_names['constant']}\n"
                f"kebab-case (短横线): {code_names['kebab']}"
            )
            items.append(
                winshun.item(
                    "code-naming",
                    f"{code_summary}\n{code_detail}",
                    "编程变量命名 · Enter 复制小驼峰 (camelCase)" if zh else "Code Naming · Enter copies camelCase",
                    icon=ICON_CODE,
                    copy=code_names["camel"],
                    score=45,
                    details=True,
                    data={
                        "kind": "code_naming",
                        "providerName": "编程变量命名",
                        "source": source_text,
                        "result": code_detail,
                        "targetLang": "en",
                        "direction": direction_note,
                        "allResults": all_completed,
                    },
                )
            )

    return items


# ---------------------------------------------------------------------------
# Resident Plugin Implementation
# ---------------------------------------------------------------------------

class OmniTranslatePlugin(winshun.Plugin):
    def __init__(self) -> None:
        super().__init__(
            on_query=self._sync_query_fallback,
            on_action=self.handle_action,
            on_settings=self.handle_settings,
            on_cancel=self.handle_cancel,
            name="anghost.translate",
            on_details=self.handle_details,
        )
        self.capabilities: dict[str, Any] = {}
        self.history = core.HistoryStore(winshun.data_dir())
        self._query_lock = threading.Lock()
        self._active_query_id: int = 0
        self._active_cancel: threading.Event | None = None
        self._registered_hotkeys: list[str] = []
        self._rpc_waiters: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._rpc_lock = threading.Lock()
        self._is_resident = False

    def log(self, text: str, level: str = "info") -> None:
        if self._is_resident:
            super().log(text, level=level)
        else:
            winshun.debug(f"[{level}] {text}")

    def tool_async_safe(self, name: str, **arguments: Any) -> dict[str, Any]:
        """Thread-safe winshun/tool caller that cooperates with the main stdin loop."""
        with self._rpc_lock:
            call_id = self._next_id
            self._next_id += 1
            ev = threading.Event()
            holder: dict[str, Any] = {}
            self._rpc_waiters[call_id] = (ev, holder)
        self._send({"id": call_id, "method": "winshun/tool", "params": {"name": name, "arguments": arguments}})
        ev.wait(timeout=3.0)
        with self._rpc_lock:
            self._rpc_waiters.pop(call_id, None)
        return holder.get("response", {"error": {"code": -32000, "message": "timeout"}})

    def _handle(self, message: dict[str, Any]) -> bool:
        if "method" not in message and "id" in message:
            call_id = message.get("id")
            with self._rpc_lock:
                waiter = self._rpc_waiters.get(call_id)
            if waiter:
                ev, holder = waiter
                holder["response"] = {"error": message["error"]} if "error" in message else message.get("result")
                ev.set()
                return False

        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            self._is_resident = True
            self.settings = params.get("settings") or {}
            self.locale = params.get("locale") or "zh"
            self.theme = params.get("theme") or "light"
            self.plugin_dir = params.get("pluginDir") or self.plugin_dir
            self.data_dir = params.get("dataDir") or winshun.data_dir()
            self.capabilities = params.get("capabilities") or {}
            self.history.set_data_dir(self.data_dir)
            self._send({"id": msg_id, "result": {"protocolVersion": "1", "name": self._name}})
            self._sync_hotkeys_async()
            return False

        if method == "query":
            q = winshun.Query(
                text=params.get("text", ""),
                raw=params.get("raw", ""),
                command=params.get("command", "tr"),
                trigger=params.get("trigger", "keyword"),
                keyword=params.get("keyword", ""),
                match=params.get("match") or [],
                query_id=params.get("queryId", 0),
                limit=params.get("limit", 200),
                filters=params.get("filters") or {},
            )
            self._start_query_async(msg_id, q)
            return False

        if method == "cancel":
            qid = int(params.get("queryId", 0))
            self.handle_cancel(qid, self)
            return False

        if method == "hotkey":
            pressed_key = str(params.get("key") or "")
            if pressed_key:
                _PROVIDER_POOL.submit(self._on_hotkey_pressed, pressed_key)
            return False

        if method == "appearance":
            self.locale = params.get("locale") or self.locale
            self.theme = params.get("theme") or self.theme
            return False

        if method == "shutdown":
            self._cancel_active_query()
            self._send({"id": msg_id, "result": {}})
            return True

        return super()._handle(message)

    # -- Hotkeys (hostHotkeys protocol) --

    def _desired_hotkeys(self) -> list[str]:
        s = merged_settings(self.settings)
        keys: list[str] = []
        for k_name in ("selectionHotkey", "replaceHotkey"):
            val = str(s.get(k_name) or "").strip()
            if val and val not in keys:
                keys.append(val)
        return keys

    def _sync_hotkeys_async(self) -> None:
        if not self.capabilities.get("hostHotkeys"):
            return
        keys = self._desired_hotkeys()
        if keys == self._registered_hotkeys:
            return
        self._registered_hotkeys = list(keys)

        def _worker() -> None:
            try:
                self._notify("winshun/hotkeys", {"keys": keys})
                res = self.tool_async_safe("register_hotkeys", keys=keys)
                if isinstance(res, dict) and res.get("errors"):
                    errs = res["errors"]
                    self.log(f"hotkey register errors={json.dumps(errs)}", level="warn")
                    if isinstance(errs, dict) and errs:
                        conflict_keys = "、".join(str(k) for k in errs.keys())
                        self.notify_user(f"顺译快捷键冲突：{conflict_keys} 已被其他软件占用，请在插件设置中修改")
            except Exception as ex:
                self.log(f"hotkey sync failed: {type(ex).__name__}", level="warn")

        _PROVIDER_POOL.submit(_worker)

    def _on_hotkey_pressed(self, key: str) -> None:
        s = merged_settings(self.settings)
        sel_key = str(s.get("selectionHotkey") or "").strip().lower()
        rep_key = str(s.get("replaceHotkey") or "").strip().lower()
        pressed = key.strip().lower()

        if sel_key and pressed == sel_key:
            selected = selection.read_selection()
            if selected and selected.strip():
                cleaned = selected.strip()
                if _bool_setting(s, "joinLines", True):
                    cleaned = core.join_paragraph_lines(cleaned)
                self.tool_async_safe("show_launcher", text=f"ot {cleaned}")
            else:
                self.tool_async_safe("show_launcher", text="ot ")
        elif rep_key and pressed == rep_key:
            selected = selection.read_selection()
            if not selected or not selected.strip():
                self.notify_user("未读取到选中的文字")
                return
            self._silent_translate_and_replace(selected, s)

    def _silent_translate_and_replace(self, raw_selected: str, settings: dict[str, Any]) -> None:
        lead_ws = raw_selected[: len(raw_selected) - len(raw_selected.lstrip())]
        trail_ws = raw_selected[len(raw_selected.rstrip()) :]
        core_text = raw_selected.strip()
        plan = core.resolve_lang_plan(
            core_text,
            first_code=str(settings.get("firstLang") or "zh-Hans"),
            second_code=str(settings.get("secondLang") or "en"),
        )
        specs = [
            (pid, name)
            for pid, name in active_providers(settings, core_text, is_dict_cmd=False, zh=(self.locale != "en"))
            if pid != "youdaoDict"
        ]
        if not specs:
            self.notify_user("没有可用的翻译服务，请先在插件设置中启用")
            return
        pid, pname = specs[0]
        res = run_single_provider(pid, pname, core_text, plan, word_mode=False, settings=settings, timeout_sec=BUDGET_SEC)
        if not res.ok or not res.text.strip():
            self.notify_user(f"替换翻译失败：{res.error or '无结果'}")
            return
        final_replacement = f"{lead_ws}{res.text.strip()}{trail_ws}"
        if selection.replace_selection(final_replacement):
            self.history.record(core_text, res.text.strip(), plan.target, pname)
            self.notify_user(f"已用 {pname} 替换原文")
        else:
            self.notify_user("未能写入原窗口")

    # -- Asynchronous Query Lifecycle --

    def _cancel_active_query(self) -> None:
        with self._query_lock:
            cancel_ev = self._active_cancel
        if cancel_ev is not None:
            cancel_ev.set()

    def handle_cancel(self, query_id: int, _plugin: Any = None) -> None:
        with self._query_lock:
            if query_id == self._active_query_id and self._active_cancel is not None:
                self._active_cancel.set()

    def handle_settings(self, new_settings: dict[str, Any], _plugin: Any = None) -> None:
        self._cancel_active_query()
        providers.clear_provider_cache()
        self._sync_hotkeys_async()

    def _start_query_async(self, msg_id: Any, q: winshun.Query) -> None:
        self._cancel_active_query()
        cancel_ev = threading.Event()
        snapshot_settings = merged_settings(self.settings)
        zh = self.locale != "en"

        with self._query_lock:
            self._active_query_id = q.query_id
            self._active_cancel = cancel_ev

        def _worker() -> None:
            try:
                outcome = self._execute_query(q, snapshot_settings, zh, cancel_ev)
            except Exception as ex:
                winshun.debug(f"anghost.translate query error: {ex!r}")
                outcome = {
                    "items": [
                        winshun.item(
                            "error",
                            "无法完成翻译请求" if zh else "Could not complete translation request",
                            str(ex)[:120],
                            icon=ICON_ERROR,
                            valid=False,
                        )
                    ],
                    "cache": 0,
                }
            self._send({"id": msg_id, "result": outcome})

        _PROVIDER_POOL.submit(_worker)

    def _sync_query_fallback(self, q: winshun.Query, _plugin: Any) -> dict[str, Any]:
        return self._execute_query(q, merged_settings(self.settings), self.locale != "en", threading.Event())

    # -- Query Execution (tr, dict, trh) --

    def _execute_query(
        self,
        q: winshun.Query,
        settings: dict[str, Any],
        zh: bool,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        cmd = (q.command or "tr").strip().lower()
        if cmd == "trh":
            return {"items": self._query_history(q.text, zh), "cache": 0}

        is_dict_cmd = cmd == "dict"
        inline_target, cleaned_text = core.parse_inline_target(q.text or "")
        if not is_dict_cmd and _bool_setting(settings, "joinLines", True):
            cleaned_text = core.join_paragraph_lines(cleaned_text)

        if not cleaned_text:
            if is_dict_cmd:
                return {
                    "items": [
                        winshun.item(
                            "hint",
                            "输入要查询的单词或短语" if zh else "Type a word or phrase to look up",
                            "有道词典免 Key 即查，同时支持智谱与 AI 词典显示音标、词性与双语例句"
                            if zh
                            else "Keyless Youdao Dictionary & AI bilingual dictionary",
                            icon=ICON_DICT,
                            valid=False,
                        )
                    ],
                    "cache": 0,
                }
            hint_items = [
                winshun.item(
                    "hint",
                    "输入要翻译的句子、单词或代码变量名" if zh else "Type a sentence, word, or code identifier",
                    "支持 :en / :zh / :ja 指定目标语言 · 支持驼峰/下划线自动拆词"
                    if zh
                    else "Prefix with :en / :zh / :ja for target language · Auto-splits camelCase/snake_case",
                    icon=ICON_PLUGIN,
                    valid=False,
                )
            ]
            # Smart Clipboard Quick-Translate entry (zero network side effects)
            if self._is_resident:
                clip = selection.get_clipboard_text()
                if clip:
                    clip_clean = clip.strip()
                    if 1 < len(clip_clean) <= 800 and not clip_clean.lower().startswith("tr "):
                        hint_items.append(
                            winshun.item(
                                "clip-quick",
                                f"翻译剪贴板：{_single_line_preview(clip_clean, 72)}"
                                if zh
                                else f"Translate clipboard: {_single_line_preview(clip_clean, 72)}",
                                "按 Enter 立即填入并翻译" if zh else "Press Enter to fill and translate",
                                icon=ICON_PLUGIN,
                                data={"kind": "fill_clipboard", "text": clip_clean},
                            )
                        )
            return {"items": hint_items, "cache": 0}

        if len(cleaned_text) > 32768:
            return {
                "items": [
                    winshun.item(
                        "too-long",
                        "文本超过 32 KB，请分段翻译" if zh else "Text exceeds 32 KB, please split into smaller parts",
                        f"当前长度：{len(cleaned_text)} 字符" if zh else f"Length: {len(cleaned_text)} chars",
                        icon=ICON_ERROR,
                        valid=False,
                    )
                ],
                "cache": 0,
            }

        # Check if input is a camelCase / snake_case code identifier
        split_from_identifier: str | None = None
        effective_text = cleaned_text
        if not is_dict_cmd:
            split_words = core.split_code_identifier(cleaned_text)
            if split_words:
                split_from_identifier = cleaned_text
                effective_text = split_words

        plan = core.resolve_lang_plan(
            effective_text,
            first_code=str(settings.get("firstLang") or "zh-Hans"),
            second_code=str(settings.get("secondLang") or "en"),
            explicit_target=inline_target,
        )

        # Pure numbers/symbols check (unless explicit target was given)
        if plan.detected is None and inline_target is None and not is_dict_cmd:
            return {
                "items": [
                    winshun.item(
                        "symbols-only",
                        cleaned_text,
                        "纯数字或符号无需翻译 · Enter 直接复制" if zh else "Numbers or symbols only · Enter copies",
                        icon=ICON_PLUGIN,
                        copy=cleaned_text,
                        valid=True,
                    )
                ],
                "cache": 60,
            }

        specs = active_providers(settings, effective_text, is_dict_cmd=is_dict_cmd, zh=zh)
        if not specs:
            return {
                "items": [
                    winshun.item(
                        "setup",
                        "还没有启用任何翻译服务" if zh else "No translation service enabled",
                        "按 Enter 打开设置 → 插件 → 顺译 · 多源翻译，开启至少一个服务"
                        if zh
                        else "Press Enter to open plugin settings and enable a service",
                        icon=ICON_ERROR,
                        url="winshun:settings",
                    )
                ],
                "cache": 0,
            }

        word_mode = is_dict_cmd or core.is_word(effective_text)
        states: dict[str, providers.ProviderResult] = {}
        state_lock = threading.Lock()
        last_partial_ts = [0.0]
        done_count = [0]
        all_done_ev = threading.Event()

        def emit_partial(force: bool = False) -> None:
            if cancel_event.is_set() or q.query_id <= 0:
                return
            now = time.monotonic()
            if not force and (now - last_partial_ts[0]) < PARTIAL_THROTTLE_SEC:
                return
            last_partial_ts[0] = now
            with state_lock:
                snapshot = dict(states)
            rows = build_provider_items(
                effective_text,
                plan,
                specs,
                snapshot,
                is_dict_cmd,
                zh,
                split_from_identifier=split_from_identifier,
            )
            self.partial(q.query_id, rows)

        # Immediately emit initial working rows
        emit_partial(force=True)

        def run_one(pid: str, pname: str) -> None:
            if cancel_event.is_set():
                return

            def on_stream_delta(visible: str, is_thinking: bool) -> None:
                if cancel_event.is_set():
                    return
                with state_lock:
                    states[pid] = providers.ProviderResult(
                        provider_id=pid,
                        provider_name=pname,
                        ok=True,
                        text=visible,
                        is_thinking=is_thinking,
                        is_streaming=True,
                    )
                emit_partial(force=False)

            res = run_single_provider(
                pid,
                pname,
                effective_text,
                plan,
                word_mode=word_mode,
                settings=settings,
                on_stream=on_stream_delta,
                timeout_sec=BUDGET_SEC,
                cancel_event=cancel_event,
            )
            if not cancel_event.is_set():
                self.log(
                    f"provider={pid} durationMs={res.duration_ms} ok={int(res.ok)} errKind={res.error_kind or 'none'}",
                    level="info",
                )
            with state_lock:
                states[pid] = res
                done_count[0] += 1
                is_all_done = done_count[0] >= len(specs)
            if is_all_done:
                all_done_ev.set()
            else:
                emit_partial(force=True)

        for pid, pname in specs:
            _PROVIDER_POOL.submit(run_one, pid, pname)

        deadline = time.monotonic() + BUDGET_SEC
        while not all_done_ev.is_set() and not cancel_event.is_set():
            rem = deadline - time.monotonic()
            if rem <= 0:
                break
            all_done_ev.wait(timeout=min(0.05, rem))

        if cancel_event.is_set():
            return {"items": [], "cache": 0}

        with state_lock:
            final_states = dict(states)

        final_items = build_provider_items(
            effective_text,
            plan,
            specs,
            final_states,
            is_dict_cmd,
            zh,
            split_from_identifier=split_from_identifier,
        )
        any_ok = any(st.ok and not st.is_truncated for st in final_states.values())
        return {"items": final_items, "cache": 300 if any_ok else 0}

    # -- History Command (trh) --

    def _query_history(self, raw_query: str, zh: bool) -> list[dict[str, Any]]:
        q = (raw_query or "").strip()
        q_lower = q.lower()

        if q_lower in ("export", "导出", "anki"):
            total, favs = self.history.stats()
            return [
                winshun.item(
                    "export-all",
                    f"导出全部翻译历史为 Anki TSV 与 CSV（共 {total} 条）" if zh else f"Export all history to Anki TSV & CSV ({total} items)",
                    "按 Enter 导出到插件数据目录并打开所在文件夹" if zh else "Press Enter to export and open folder",
                    icon=ICON_HISTORY,
                    data={"kind": "export", "favoritesOnly": False},
                ),
                winshun.item(
                    "export-fav",
                    f"仅导出收藏记录为 Anki TSV 与 CSV（共 {favs} 条）" if zh else f"Export favorites only to Anki TSV & CSV ({favs} items)",
                    "生成带 UTF-8 BOM 的 CSV 表格与 #separator:tab 格式的 Anki 记忆卡" if zh else "Exports UTF-8 BOM CSV & Anki TSV",
                    icon=ICON_STAR,
                    data={"kind": "export", "favoritesOnly": True},
                ),
            ]

        if q_lower in ("clear", "清空"):
            total, favs = self.history.stats()
            return [
                winshun.item(
                    "clear-history",
                    f"清空非收藏翻译历史（将删除 {max(0, total - favs)} 条，保留 {favs} 条收藏）"
                    if zh
                    else f"Clear non-favorite history (keeps {favs} favorites)",
                    "按 Enter 确认清空，收藏的记录会永久保留" if zh else "Press Enter to confirm; favorites are kept",
                    icon=ICON_ERROR,
                    data={"kind": "clear"},
                )
            ]

        fav_only = False
        search_kw = q
        if q_lower.startswith("fav ") or q_lower == "fav" or q.startswith("★"):
            fav_only = True
            search_kw = q[3:].strip() if q_lower.startswith("fav") else q.lstrip("★").strip()

        rows = self.history.search(keyword=search_kw, favorites_only=fav_only, limit=80)
        total, favs = self.history.stats()
        items: list[dict[str, Any]] = []

        if not rows:
            items.append(
                winshun.item(
                    "history-empty",
                    "暂无匹配的翻译历史" if zh else "No matching translation history",
                    f"共 {total} 条记录 · 收藏 {favs} 条 · 在翻译结果上按 Enter 复制或 Ctrl+Enter 收藏即可记入历史"
                    if zh
                    else f"Total {total} · Favorites {favs}",
                    icon=ICON_HISTORY,
                    valid=False,
                )
            )
        else:
            for r in rows:
                star = "★ " if r.favorite else ""
                title = f"{star}{r.result}"
                sub = f"{r.service} · 原文：{_single_line_preview(r.source, 56)}"
                items.append(
                    winshun.item(
                        f"hist-{r.id}",
                        title,
                        sub,
                        icon=ICON_STAR if r.favorite else ICON_HISTORY,
                        copy=r.result,
                        details=True,
                        data={
                            "kind": "history",
                            "recordId": r.id,
                            "source": r.source,
                            "result": r.result,
                            "targetLang": r.target_lang,
                            "service": r.service,
                            "favorite": r.favorite,
                        },
                    )
                )

        if total > 0:
            items.append(
                winshun.item(
                    "util-export",
                    f"导出历史与收藏为 Anki 记忆卡 (TSV) / CSV 表格（共 {total} 条 · 收藏 {favs} 条）"
                    if zh
                    else f"Export to Anki TSV / CSV ({total} items · {favs} favorites)",
                    "按 Enter 立即导出到插件数据目录并打开文件夹" if zh else "Press Enter to export and open folder",
                    icon=ICON_HISTORY,
                    data={"kind": "export", "favoritesOnly": fav_only},
                )
            )
        return items

    # -- Details Pane (#202) --

    def handle_details(self, item: dict[str, Any], _text: str, _plugin: Any = None) -> dict[str, Any]:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        kind = data.get("kind")
        if kind == "history":
            fav_tag = "★ 已收藏" if data.get("favorite") else "未收藏"
            content = (
                f"【服务】{data.get('service', '')} ({fav_tag})\n"
                f"【目标语言】{core.lang_title(data.get('targetLang', 'zh-Hans'), zh=True)}\n\n"
                f"【译文】\n{data.get('result', '')}\n\n"
                f"【原文】\n{data.get('source', '')}"
            )
            return {"text": content}

        source = data.get("source") or ""
        result = data.get("result") or item.get("copy") or item.get("title") or ""
        pname = data.get("providerName") or "翻译结果"
        direction = data.get("direction") or ""
        all_results = data.get("allResults") if isinstance(data.get("allResults"), dict) else {}

        sections = [
            f"【{pname}】 {direction}",
            result,
            "",
            "【原文】",
            source,
        ]
        if len(all_results) > 1:
            sections.append("")
            sections.append("【多服务并发对比】")
            for pid, _, zh_name, _ in PROVIDER_SPECS:
                if pid in all_results and all_results[pid]:
                    sections.append(f"▸ {zh_name}：\n{all_results[pid]}")

        return {"text": "\n".join(sections)}

    # -- Actions (Enter / Ctrl+Enter / Shift+Enter) --

    def handle_action(self, req: winshun.ActionRequest, _plugin: Any = None) -> list[dict[str, Any]]:
        zh = self.locale != "en"
        act = req.action or "default"
        item = req.item or {}
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        kind = data.get("kind")

        if kind == "fill_clipboard":
            clip_txt = str(data.get("text") or "").strip()
            return [winshun.set_query(f"ot {clip_txt}"), winshun.keep()]

        if kind == "export":
            fav_only = bool(data.get("favoritesOnly", False))
            csv_path, tsv_path, count = self.history.export_files(favorites_only=fav_only)
            export_dir = os.path.dirname(tsv_path)
            return [
                winshun.notify(f"已导出 {count} 条记录（Anki TSV 与 CSV）" if zh else f"Exported {count} records"),
                winshun.open_path(export_dir),
                winshun.hide(),
            ]

        if kind == "clear":
            removed, kept = self.history.clear_non_favorites()
            return [
                winshun.notify(
                    f"已清空 {removed} 条翻译历史 · 保留了 {kept} 条收藏"
                    if zh
                    else f"Cleared {removed} items · Kept {kept} favorites"
                ),
                winshun.set_query("trh "),
                winshun.keep(),
            ]

        if kind == "history":
            rec_id = str(data.get("recordId") or "")
            source_text = str(data.get("source") or "")
            result_text = str(data.get("result") or item.get("copy") or "")
            if act == "retranslate":
                return [winshun.set_query(f"ot {source_text}"), winshun.keep()]
            if act == "toggle_fav":
                updated = self.history.toggle_favorite(rec_id)
                msg = (
                    ("★ 已加入收藏" if updated and updated.favorite else "已取消收藏")
                    if zh
                    else ("★ Added to favorites" if updated and updated.favorite else "Removed from favorites")
                )
                return [winshun.notify(msg), winshun.set_query(f"trh {req.text}"), winshun.keep()]
            return [
                winshun.copy(result_text),
                winshun.notify("已复制译文" if zh else "Copied translation"),
                winshun.hide(),
            ]

        # Standard translation, dictionary, or code_naming row
        copy_text = str(item.get("copy") or data.get("result") or item.get("title") or "")
        source_text = str(data.get("source") or "")
        target_lang = str(data.get("targetLang") or "zh-Hans")
        provider_name = str(data.get("providerName") or "翻译")

        if act == "speak":
            selection.speak_async(copy_text)
            return [
                winshun.notify("正在朗读…" if zh else "Speaking…"),
                winshun.keep(),
            ]

        if act == "favorite":
            if source_text and copy_text:
                self.history.record(source_text, copy_text, target_lang, provider_name, favorite=True)
            return [
                winshun.copy(copy_text),
                winshun.notify("★ 已收藏并复制译文" if zh else "★ Favorited and copied"),
                winshun.hide(),
            ]

        # Default / copy action: record into history on explicit user selection (Constraint 2)
        if kind == "translation" and source_text and copy_text:
            self.history.record(source_text, copy_text, target_lang, provider_name, favorite=None)

        return [
            winshun.copy(copy_text),
            winshun.notify("已复制译文" if zh else "Copied"),
            winshun.hide(),
        ]


def main() -> int:
    # Support once / environment-variable invocation if WS_QUERY or WS_ACTION is set
    if "WS_QUERY" in os.environ or "WS_ACTION" in os.environ:
        plugin = OmniTranslatePlugin()
        plugin.settings = winshun.settings()
        plugin.locale = winshun.locale()
        plugin.data_dir = winshun.data_dir()
        plugin.history.set_data_dir(plugin.data_dir)
        if winshun.is_action():
            effs = plugin.handle_action(winshun.action_request(), plugin)
            winshun.effects(*effs)
            return 0
        q = winshun.query()
        if not q.command:
            q.command = "tr"
        res = plugin._execute_query(q, merged_settings(plugin.settings), plugin.locale != "en", threading.Event())
        winshun.reply(res.get("items", []), cache=res.get("cache", 0))
        return 0

    return OmniTranslatePlugin().serve()


if __name__ == "__main__":
    sys.exit(main())
