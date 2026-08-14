"""Similarity-based suppression for repeated agent speech.

This deliberately compares only the agent's own recent downlink transcripts.
It does not inspect user speech and does not use phrase/keyword lists, so it
stays useful across languages and IVR wording.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from collections import deque
from difflib import SequenceMatcher
from typing import Callable, Iterable

from . import config

logger = logging.getLogger(__name__)

DEFAULT_RECENT_LIMIT = 5
MIN_NORMALIZED_CHARS = 6
DEFAULT_NUDGE_COOLDOWN_SECONDS = 8.0
DEFAULT_STUCK_LIMIT = 3


def normalize_for_similarity(text: str) -> str:
    """Normalize text for similarity: case-fold and drop separators/punctuation."""
    normalized: list[str] = []
    for ch in (text or "").casefold():
        category = unicodedata.category(ch)
        if category[0] in {"P", "Z"} or ch.isspace():
            continue
        normalized.append(ch)
    return "".join(normalized)


def is_repetitive(
    new_text: str,
    recent_texts: Iterable[str],
    threshold: float,
    *,
    min_chars: int = MIN_NORMALIZED_CHARS,
) -> bool:
    """Return whether ``new_text`` is highly similar to any recent agent text."""
    if threshold <= 0:
        return False
    new_norm = normalize_for_similarity(new_text)
    if len(new_norm) < min_chars:
        return False
    for old_text in recent_texts:
        old_norm = normalize_for_similarity(old_text)
        if len(old_norm) < min_chars:
            continue
        if SequenceMatcher(None, new_norm, old_norm).ratio() >= threshold:
            return True
    return False


class RepeatSuppressor:
    """Keeps one call's recent agent transcripts and judges repeated responses."""

    def __init__(
        self,
        *,
        recent_limit: int = DEFAULT_RECENT_LIMIT,
        threshold_getter: Callable[[], float] | None = None,
    ) -> None:
        self._recent: deque[str] = deque(maxlen=recent_limit)
        self._threshold_getter = threshold_getter or (
            lambda: config.get_float("REPEAT_SUPPRESS_SIMILARITY")
        )
        self._repeat_hits = 0

    def should_suppress(self, text: str) -> bool:
        threshold = self._threshold_getter()
        normalized = normalize_for_similarity(text)
        if not normalized:
            return False
        if is_repetitive(text, self._recent, threshold):
            self._repeat_hits += 1
            if self._repeat_hits >= 2:
                return True
            self._recent.append(text)
            return False
        self._repeat_hits = 0
        self._recent.append(text)
        return False

    @property
    def disabled(self) -> bool:
        return self._threshold_getter() <= 0


class ResponseAudioGate:
    """Stream response audio immediately; cut a response once判定为复读。

    旧实现把每轮音频扣到整轮转写过完复读检查才放行——复读从不出声，但**每一轮**
    都要多等「首音频→转写完成」，实测 0.3~2s（回复越长越久），是 WIL-112 轮次
    延迟里最大的一段自造开销。复读本身是小概率事件，不值得让每轮都买单。

    现改为：音频到即放（streaming），转写完成后才知道是复读的，标记该 response
    丢弃后续分块，并通过 ``on_late_cut`` 让上层清掉设备侧未播积压（与 barge-in
    同一套清积压机制）。代价：复读可能已播出 1~2s 开头才被掐；收益：每轮延迟
    直降为零开销。``on_late_cut`` 未接线（如半双工模式）时仅丢后续分块。
    """

    def __init__(
        self,
        provider: str,
        emit_audio: Callable[[bytes], None],
        suppressor: RepeatSuppressor | None = None,
        on_suppressed: Callable[[str], None] | None = None,
        on_stuck: Callable[[int, str], None] | None = None,
        time_fn: Callable[[], float] = time.monotonic,
        nudge_cooldown_seconds: float = DEFAULT_NUDGE_COOLDOWN_SECONDS,
        stuck_limit: int = DEFAULT_STUCK_LIMIT,
        on_late_cut: Callable[[], None] | None = None,
    ) -> None:
        self._provider = provider
        self._emit_audio = emit_audio
        self._suppressor = suppressor or RepeatSuppressor()
        self._on_suppressed = on_suppressed
        self._on_stuck = on_stuck
        self._on_late_cut = on_late_cut
        self._time_fn = time_fn
        self._nudge_cooldown_seconds = nudge_cooldown_seconds
        self._stuck_limit = stuck_limit
        self._last_nudge_at: float | None = None
        self._consecutive_suppressed = 0
        self._stuck_notified = False
        self._streamed_bytes: dict[str, int] = {}
        self._suppressed: set[str] = set()

    def push_audio(self, response_id: str | None, chunk: bytes) -> None:
        if not chunk:
            return
        if not response_id or self._suppressor.disabled:
            self._emit_audio(chunk)
            return
        if response_id in self._suppressed:
            return
        self._streamed_bytes[response_id] = (
            self._streamed_bytes.get(response_id, 0) + len(chunk)
        )
        self._emit_audio(chunk)

    def complete_transcript(self, response_id: str | None, transcript: str) -> bool:
        if not response_id or self._suppressor.disabled:
            return False
        if self._suppressor.should_suppress(transcript):
            streamed = self._streamed_bytes.pop(response_id, 0)
            self._suppressed.add(response_id)
            self._consecutive_suppressed += 1
            if streamed:
                logger.info(
                    "[%s] 抑制复读（已开播 %d 字节，截断剩余并清积压）: %s",
                    self._provider, streamed, transcript,
                )
                if self._on_late_cut is not None:
                    try:
                        self._on_late_cut()
                    except Exception:  # noqa: BLE001
                        logger.exception("[%s] 复读截断清积压回调失败", self._provider)
            else:
                logger.info("[%s] 抑制复读: %s", self._provider, transcript)
            self._notify_suppressed(transcript)
            if (
                self._stuck_limit > 0
                and self._consecutive_suppressed >= self._stuck_limit
                and not self._stuck_notified
            ):
                self._stuck_notified = True
                if self._on_stuck is not None:
                    try:
                        self._on_stuck(self._consecutive_suppressed, transcript)
                    except Exception:  # noqa: BLE001
                        logger.exception("[%s] 复读卡死回调失败", self._provider)
            return True
        self._consecutive_suppressed = 0
        self._stuck_notified = False
        self._streamed_bytes.pop(response_id, None)
        return False

    def complete_response(self, response_id: str | None) -> None:
        if not response_id:
            return
        self._streamed_bytes.pop(response_id, None)
        self._suppressed.discard(response_id)

    def _notify_suppressed(self, transcript: str) -> None:
        if self._on_suppressed is None:
            return
        now = self._time_fn()
        if (
            self._last_nudge_at is not None
            and now - self._last_nudge_at < self._nudge_cooldown_seconds
        ):
            return
        self._last_nudge_at = now
        try:
            self._on_suppressed(transcript)
        except Exception:  # noqa: BLE001
            logger.exception("[%s] 复读换说法提示失败", self._provider)
