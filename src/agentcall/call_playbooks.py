"""呼叫情报库（Call Playbooks，WIL-129）：按热线沉淀"要什么信息 + IVR 怎么走"。

核心设计边界——**键值分离**：本库只存"要什么"（必备信息的 key/label/purpose、
IVR 流程知识 ivr_notes），全部是公共知识；"是什么"（PIN/ZIP 等真实值）只存
number_profiles 的 ``task_package.verification``（WIL-95 §7：值只进模型上下文，
不进 events/metrics/日志）。因此本库无隐私，未来可整体云端共享（P3）。

与 number_profiles 同款存储纪律：JSON 按通读取（无缓存、改文件即生效、无需
重启）；读路径 never raises（坏文件降级为空库）；写路径（learner 回流）严格
校验 + 进程内写锁 + 原子替换落盘。

条目结构::

    {
      "id": "att_prepaid_611",
      "numbers": ["611"],
      "label": {"zh": ..., "en": ...},
      "required_info": [
        {"key": "account_pin", "label": {...}, "purpose": {...}},
        ...
      ],
      "ivr_notes": {"zh": ..., "en": ...},
      "updated": "2026-08-18",
      "source": {"zh": ..., "en": ...}
    }

``required_info[].key`` 与 number_profiles ``task_package.verification`` 的键
对齐：拨前拦截即"该热线要的 key，预设的 verification 里有没有"。

自动回流（learner）的隐私硬校验：模型输出的任何文本字段出现 ≥4 位连续数字
即整条拒绝——转写里可能包含口述的 PIN/ZIP 值，绝不允许渗入知识库。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from . import config
from .number_profiles import (
    _norm,
    _pick_lang,
    _write_profiles_file,
    same_dial_number,
)

logger = logging.getLogger(__name__)

_PLAYBOOK_WRITE_LOCK = threading.RLock()

_KEY_RE = re.compile(r"[a-z0-9_]{1,40}")
# 隐私硬校验：≥4 位连续数字（PIN/ZIP/账号值的最短形态）出现在知识字段里
# 即拒绝。号码字段（numbers）不受此限——热线号码本来就是数字。
_PRIVACY_DIGIT_RE = re.compile(r"\d{4}")

_MAX_REQUIRED_INFO = 16
_MAX_TEXT = 400
_MAX_IVR_NOTES = 1200
_MAX_LEARNED_NOTE = 400


def default_playbooks_file() -> Path:
    configured = config.get_str("CALL_PLAYBOOKS_FILE").strip()
    if configured:
        return Path(configured).expanduser()
    return config.data_dir() / "call_playbooks.json"


def bundled_seed_file() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "seed" / "call_playbooks.example.json"
    return Path(__file__).resolve().parents[2] / "data" / "call_playbooks.example.json"


def ensure_seeded(
    *,
    target: str | Path | None = None,
    seed: str | Path | None = None,
) -> bool:
    """Copy the bundled playbook seed on first run; never overwrite or raise."""
    target_path = (
        Path(target).expanduser() if target is not None else default_playbooks_file()
    )
    seed_path = Path(seed).expanduser() if seed is not None else bundled_seed_file()
    if target_path.exists():
        return False
    try:
        if not seed_path.exists():
            logger.warning("呼叫情报库种子文件不存在: %s", seed_path)
            return False
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed_path, target_path)
        return True
    except OSError as exc:
        logger.warning("初始化呼叫情报库失败: %s", exc)
        return False


def playbooks_enabled() -> bool:
    return config.get_bool("CALL_PLAYBOOKS_ENABLED")


def _load_playbooks_file(path: Path) -> list[dict[str, Any]]:
    """宽松读：任何问题降级为空列表，绝不抛出。"""
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("呼叫情报库读取/解析失败: %s", exc)
        return []
    if not isinstance(loaded, dict) or not isinstance(loaded.get("playbooks"), list):
        logger.warning("呼叫情报库格式无效: 顶层缺少 playbooks 列表")
        return []
    return [item for item in loaded["playbooks"] if isinstance(item, dict)]


def list_playbooks(*, path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path).expanduser() if path is not None else default_playbooks_file()
    return _load_playbooks_file(target)


def lookup_playbook(
    number: Any, *, path: str | Path | None = None
) -> dict[str, Any] | None:
    """按被叫号码命中情报条目；未命中/库损坏返回 None。"""
    if not _norm(number):
        return None
    for item in list_playbooks(path=path):
        numbers = item.get("numbers")
        if not isinstance(numbers, list):
            continue
        if any(same_dial_number(number, candidate) for candidate in numbers):
            return item
    return None


def _normalized_required_info(playbook: dict[str, Any]) -> list[dict[str, Any]]:
    entries = playbook.get("required_info")
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = _norm(entry.get("key")).lower()
        if not _KEY_RE.fullmatch(key):
            continue
        out.append({**entry, "key": key})
    return out


def missing_required_info(
    playbook: dict[str, Any] | None, task_package: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """该热线要求、但预设 verification 里没有的必备信息条目。"""
    if not playbook:
        return []
    verification = {}
    if isinstance(task_package, dict):
        raw = task_package.get("verification")
        if isinstance(raw, dict):
            verification = raw
    have = {_norm(k).lower() for k in verification if _norm(str(verification[k]))}
    return [
        entry
        for entry in _normalized_required_info(playbook)
        if entry["key"] not in have
    ]


def missing_info_message(missing: list[dict[str, Any]], lang: str = "zh") -> str:
    """拨前拦截的错误文案：列出缺失项的 label（purpose）与 key。"""
    parts = []
    for entry in missing:
        label = _pick_lang(entry.get("label"), lang) or entry["key"]
        purpose = _pick_lang(entry.get("purpose"), lang)
        parts.append(f"{label}（{purpose}，键 {entry['key']}）" if purpose else f"{label}（键 {entry['key']}）")
    listed = "；".join(parts)
    return (
        f"该号码需要必备信息才能完成任务：{listed}。"
        "请在预设任务的核身信息(task_package.verification)中补充后再拨。"
    )


def ivr_notes_text(playbook: dict[str, Any] | None, lang: str) -> str:
    """命中情报条目时给 prompt 的 IVR 流程知识；无则空串。"""
    if not playbook:
        return ""
    return _pick_lang(playbook.get("ivr_notes"), lang).strip()


# ---------------------------------------------------------------------------
# 自动回流（learner 合并）：只增不删、来源留痕、隐私硬校验
# ---------------------------------------------------------------------------


class PlaybookValidationError(ValueError):
    """learner 输出不符合契约或触发隐私校验。"""


def _reject_if_private(text: str, field: str) -> str:
    cleaned = _norm(text)
    if _PRIVACY_DIGIT_RE.search(cleaned):
        raise PlaybookValidationError(
            f"{field} 含 ≥4 位连续数字，疑似真实值，拒绝写入情报库"
        )
    return cleaned


def _validate_learned_entry(entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise PlaybookValidationError("required_info 条目必须是对象")
    key = _norm(entry.get("key")).lower()
    if not _KEY_RE.fullmatch(key):
        raise PlaybookValidationError(f"required_info.key 不合法: {key!r}")
    out: dict[str, Any] = {"key": key}
    for field in ("label", "purpose"):
        raw = entry.get(field)
        if isinstance(raw, str):
            raw = {"zh": raw, "en": raw}
        if not isinstance(raw, dict):
            raise PlaybookValidationError(f"required_info.{field} 缺失或类型错误")
        picked = {}
        for lang_key in ("zh", "en"):
            value = raw.get(lang_key)
            if isinstance(value, str) and value.strip():
                picked[lang_key] = _reject_if_private(
                    value.strip()[:_MAX_TEXT], f"required_info.{field}"
                )
        if not picked:
            raise PlaybookValidationError(f"required_info.{field} 没有可用语言文本")
        out[field] = picked
    return out


def merge_learned(
    number: str,
    learned: dict[str, Any],
    *,
    call_id: str,
    updated: str,
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    """把 learner 的发现合并进情报库。

    合并策略（防污染）：required_info **只增不删**（已有 key 不动）；
    ivr_notes 以"实测补充"段追加并限长；来源记 call_id + 日期；号码无条目时
    自动新建（热线自动发现）。返回合并后的条目；没有任何新知识时返回 None。

    :raises PlaybookValidationError: learner 输出畸形或触发隐私校验。
    """
    target = Path(path).expanduser() if path is not None else default_playbooks_file()
    new_entries = [
        _validate_learned_entry(item)
        for item in (learned.get("new_required_info") or [])
    ]
    notes_update = learned.get("ivr_notes_update")
    if notes_update is not None and not isinstance(notes_update, dict):
        raise PlaybookValidationError("ivr_notes_update 必须是 {zh,en} 对象或 null")
    notes_clean: dict[str, str] = {}
    if isinstance(notes_update, dict):
        for lang_key in ("zh", "en"):
            value = notes_update.get(lang_key)
            if isinstance(value, str) and value.strip():
                notes_clean[lang_key] = _reject_if_private(
                    value.strip()[:_MAX_LEARNED_NOTE], "ivr_notes_update"
                )

    number_clean = _norm(number)
    if not number_clean:
        raise PlaybookValidationError("number 不能为空")

    with _PLAYBOOK_WRITE_LOCK:
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PlaybookValidationError(f"情报库无法读取: {exc}") from exc
            if not isinstance(data, dict) or not isinstance(
                data.get("playbooks"), list
            ):
                raise PlaybookValidationError("情报库顶层必须包含 playbooks 列表")
        else:
            data = {"playbooks": []}
        playbooks: list[Any] = data["playbooks"]

        entry = None
        for item in playbooks:
            if isinstance(item, dict) and isinstance(item.get("numbers"), list):
                if any(same_dial_number(number_clean, c) for c in item["numbers"]):
                    entry = item
                    break
        if entry is None:
            entry = {
                "id": f"learned_{re.sub(r'[^0-9a-z]', '', number_clean.lower()) or 'unknown'}",
                "numbers": [number_clean],
                "label": {"zh": f"自动发现热线 {number_clean}", "en": f"Auto-discovered hotline {number_clean}"},
                "required_info": [],
                "ivr_notes": {},
            }
            playbooks.append(entry)

        existing = entry.get("required_info")
        if not isinstance(existing, list):
            existing = []
            entry["required_info"] = existing
        have = {
            _norm(item.get("key")).lower()
            for item in existing
            if isinstance(item, dict)
        }
        added = False
        for candidate in new_entries:
            if candidate["key"] in have:
                continue
            candidate["source"] = {"call_id": call_id, "date": updated}
            existing.append(candidate)
            have.add(candidate["key"])
            added = True
        if len(existing) > _MAX_REQUIRED_INFO:
            del existing[_MAX_REQUIRED_INFO:]

        if notes_clean:
            notes = entry.get("ivr_notes")
            if not isinstance(notes, dict):
                notes = {}
            for lang_key, text in notes_clean.items():
                base = notes.get(lang_key)
                base = base.strip() if isinstance(base, str) else ""
                if text in base:
                    continue
                merged = f"{base}\n实测补充({updated}): {text}" if base else text
                notes[lang_key] = merged[-_MAX_IVR_NOTES:]
                added = True
            entry["ivr_notes"] = notes

        if not added:
            return None
        entry["updated"] = updated
        _write_profiles_file(target, data)
        return entry


# ---------------------------------------------------------------------------
# learner：通话后从转写里提炼"对方要了什么我们没有的信息 / 流程事实"
# ---------------------------------------------------------------------------

_LEARNER_SYSTEM = """You maintain a per-hotline knowledge base for an AI phone agent.
Given a call transcript, the hotline's current known requirements, and the list of
verification KEYS the caller already had, identify ONLY:
1. new_required_info: identity/verification items the remote side demanded that are
   NOT in the known keys (e.g. account PIN, ZIP code, account number). Use short
   snake_case keys. NEVER include actual values or digits — describe the field only.
2. ivr_notes_update: one short factual note about how this hotline's IVR flow works,
   ONLY if the transcript reveals something not already in the current notes
   (menu behavior, transfer path, verification order). Otherwise null.
Respond with JSON only:
{"new_required_info": [{"key": "...", "label": {"zh": "...", "en": "..."},
  "purpose": {"zh": "...", "en": "..."}}], "ivr_notes_update": {"zh": "...", "en": "..."} | null}
Empty findings => {"new_required_info": [], "ivr_notes_update": null}.
Never output any digit sequence of 4 or more digits anywhere."""


def learn_from_call(
    number: str,
    transcripts: list[tuple[str, str]],
    task_package: dict[str, Any] | None,
    *,
    call_id: str,
    updated: str,
    path: str | Path | None = None,
    timeout: float | None = None,
) -> dict[str, Any] | None:
    """通话后回流：调文本模型提炼新知识并合并入库。

    契约：绝不抛出；无新知识/模型失败/校验拒绝都返回 None（拒绝会留 warning）。
    输入隐私：只把 task_package.verification 的**键列表**给模型，绝不给值。
    """
    try:
        if not playbooks_enabled():
            return None
        if not any((text or "").strip() for _, text in transcripts or []):
            return None
        from .prompt_gen import (
            call_text_model,
            parse_json_payload,
            select_text_model,
            text_backend_for_agent,
        )

        playbook = lookup_playbook(number, path=path)
        known_keys: list[str] = [e["key"] for e in _normalized_required_info(playbook or {})]
        if isinstance(task_package, dict) and isinstance(
            task_package.get("verification"), dict
        ):
            known_keys.extend(
                _norm(k).lower() for k in task_package["verification"] if _norm(k)
            )
        notes_now = ""
        if playbook:
            zh = _pick_lang(playbook.get("ivr_notes"), "zh")
            en = _pick_lang(playbook.get("ivr_notes"), "en")
            notes_now = zh if len(zh) >= len(en) else en
        convo = "\n".join(
            f"{'CALLER-AI' if role == 'agent' else 'REMOTE'}: {text}"
            for role, text in transcripts
            if (text or "").strip()
        )
        user_prompt = (
            f"Hotline number: {number}\n"
            f"Known required-info keys (already collected or known): {sorted(set(known_keys))}\n"
            f"Current IVR notes: {notes_now or '(none)'}\n\n"
            f"Transcript:\n{convo[-6000:]}"
        )
        provider = text_backend_for_agent()
        model = select_text_model(provider, config.get_str("SUMMARY_MODEL"))
        if timeout is None:
            timeout = config.get_float("SUMMARY_TIMEOUT")
        text, error = call_text_model(
            [
                {"role": "system", "content": _LEARNER_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            provider=provider,
            model=model,
            timeout=timeout,
            max_tokens=500,
        )
        if error is not None or not text:
            logger.warning("playbook learner 模型调用失败: %s", error or "空响应")
            return None
        learned = parse_json_payload(text)
        if not isinstance(learned, dict):
            logger.warning("playbook learner 输出不是合法 JSON，放弃: %.120s", text)
            return None
        try:
            return merge_learned(
                number, learned, call_id=call_id, updated=updated, path=path
            )
        except PlaybookValidationError as exc:
            logger.warning("playbook learner 输出被拒绝: %s", exc)
            return None
    except Exception:  # noqa: BLE001 —— 契约：绝不抛出
        logger.exception("playbook learner 出现未预期异常")
        return None
