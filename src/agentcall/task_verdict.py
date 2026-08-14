"""任务达成判定层（WIL-95 §4 第三期）：自动初判 + 不确定人工复核。

铁律（2026-08-14 机主拍板的落地形态）：

- **证据不可变，裁决可重算**：本模块只**读**事件流（含 ``task_goal`` /
  ``transcript`` / ``dtmf_outcome`` / ``tool_call``）与 summary，裁决写单独的
  ``verdicts.json``（带判官模型 + 提示词版本），绝不回写原始记录——判官会改进、
  会换模型、会被发现判错，裁决写死进原始记录等于历史作废。
- **fail-closed**：JSON 不合法 / 证据不足 / 没有任务目标 → 一律「uncertain」，
  **绝不默认达成**（WIL-74 教训：判官曾*很自信地*把营销短信判成已核实结果）。
- **复核队列 = 全部 uncertain + 高置信结论抽检**（默认 15%）——危险象限是
  「自信但错」，只审不确定会漏掉它；抽检攒出实测准确率后比例可收窄。
- **置信度不信 LLM 自报**（校准差）：硬证据（按键推进 observed / 工具成功 /
  短信核验）缺席时，无论判官多自信一律标记需复核。
- **非枚举**：判官靠读对话与证据理解任务是否达成，不写关键词表。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

from .prompt_gen import parse_json_payload
from .prompts import agent_language

# 裁决契约版本（2026-08-14 v1）。改任何字段/枚举先升版本——与 metrics 同规矩。
VERDICT_SCHEMA_VERSION = 1
# 提示词版本：判官提示词一改就 +1，历史裁决据此可辨「哪一代判官判的」。
PROMPT_VERSION = 1
# 高置信结论的人工抽检比例（WIL-74 教训：只审 uncertain 会漏「自信但错」）。
REVIEW_SAMPLE_RATE = 0.15

CONCLUSIONS = ("achieved", "not_achieved", "objectively_unreachable", "uncertain")
ATTRIBUTIONS = ("agent_fault", "objective", "unknown")

# LLM 调用契约与 prompt_gen.call_text_model 对齐：(messages) -> (text, error)。
LlmFn = Callable[[list[dict[str, str]]], tuple[str | None, str | None]]

_TRANSCRIPT_TURNS = 24  # 判官读的对话尾窗；再长的对话前文对结论贡献有限


def build_evidence(
    events: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """从事件流提炼判官要读的证据（纯函数，不做 IO、不下判断）。"""
    goal = ""
    transcripts: list[tuple[str, str]] = []
    dtmf_outcomes: dict[str, int] = {}
    tool_results: list[dict[str, Any]] = []
    termination_status = ""
    for event in events:
        etype = event.get("type")
        if etype == "task_goal":
            goal = str(event.get("goal") or "") or goal
        elif etype == "transcript":
            role = str(event.get("role") or "")
            text = str(event.get("text") or "").strip()
            if role and text:
                transcripts.append((role, text))
        elif etype == "dtmf_outcome":
            status = str(event.get("status") or "unknown")
            dtmf_outcomes[status] = dtmf_outcomes.get(status, 0) + 1
        elif etype == "tool_call":
            result = event.get("result")
            tool_results.append({
                "tool": str(event.get("tool") or "unknown"),
                "success": bool(
                    isinstance(result, dict) and result.get("success")
                ),
            })
        elif etype == "call_finished":
            termination_status = str(event.get("status") or "")
    summary_text = ""
    summary_ok = False
    if isinstance(summary, dict):
        summary_ok = bool(summary.get("ok"))
        summary_text = str(summary.get("summary") or "")
    return {
        "goal": goal,
        "transcripts": transcripts[-_TRANSCRIPT_TURNS:],
        "dtmf_outcomes": dtmf_outcomes,
        "tool_results": tool_results,
        "termination_status": termination_status,
        "summary_ok": summary_ok,
        "summary_text": summary_text,
    }


# 这些工具的 success 是**本机信号**，不作硬证据：hangup_call 挂断成功≠任务
# 达成；send_dtmf 的本机 success 已被真机证实是假阳性（WIL-49/72——按键
# 「成功」与 IVR 是否推进完全脱节），它的硬证据形态是 dtmf_outcome=observed。
# summary.ok 同理不算：那只表示「摘要生成成功」，不是核验（2026-08-14 review
# 抓出的两处「把本机信号当证据」，恰是本规格第四节反复强调的坑）。
_LOCAL_SIGNAL_TOOLS = frozenset({"hangup_call", "send_dtmf"})


def hard_evidence_present(evidence: dict[str, Any]) -> bool:
    """硬证据判定（确定性规则，先于一切 LLM 置信度）：

    按键推进被**对端下一句**证实（dtmf_outcome=observed），或产生真实外部
    副作用的工具明确成功（如 send_sms）。这些之外的「达成」都只是判官的
    阅读理解，必须进复核队列。
    """
    if evidence["dtmf_outcomes"].get("observed"):
        return True
    return any(
        t["success"] and t["tool"] not in _LOCAL_SIGNAL_TOOLS
        for t in evidence["tool_results"]
    )


def _fail_closed(reason: str) -> dict[str, Any]:
    return {
        "conclusion": "uncertain",
        "attribution": "unknown",
        "confidence": 0.0,
        "reasons": reason,
        "evidence_refs": [],
    }


def _build_messages(evidence: dict[str, Any]) -> list[dict[str, str]]:
    lang = agent_language()
    roles = {"agent": "AI", "user": "对方" if lang == "zh" else "Peer"}
    convo = "\n".join(
        f"{roles.get(r, r)}: {t}" for r, t in evidence["transcripts"]
    )
    system = (
        "你是通话结果审计员。根据任务目标与证据，判断这通电话的任务是否达成。"
        "只输出一个 JSON 对象，字段："
        '{"conclusion": "achieved|not_achieved|objectively_unreachable|uncertain", '
        '"attribution": "agent_fault|objective|unknown", '
        '"confidence": 0到1的小数, "reasons": "一句话依据", '
        '"evidence_refs": ["引用的证据片段"]}。'
        "规则：①客观不可达（如对方已满/明确拒绝）不是 agent 的失败，"
        "conclusion 用 objectively_unreachable、attribution 用 objective；"
        "②证据不足以下结论时必须用 uncertain，不要猜；"
        "③achieved 必须能从证据里指出对方的确认或系统性回执，不能只凭 AI 自己说完成了。"
    )
    user = (
        f"任务目标：{evidence['goal']}\n"
        f"通话结束方式：{evidence['termination_status'] or '未知'}\n"
        f"按键推进证据：{evidence['dtmf_outcomes'] or '无'}\n"
        f"工具调用：{evidence['tool_results'] or '无'}\n"
        f"摘要层核验：{'已核验' if evidence['summary_ok'] else '无'}"
        + (f"（{evidence['summary_text']}）" if evidence["summary_text"] else "")
        + f"\n对话（末尾 {len(evidence['transcripts'])} 轮）：\n{convo}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def judge_call(
    evidence: dict[str, Any],
    llm: LlmFn,
    *,
    judge_model: str = "",
    sample_rate: float = REVIEW_SAMPLE_RATE,
    rng: random.Random | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """对一通通话产出裁决（可重算；失败一律 fail-closed 到 uncertain）。"""
    rng = rng or random.Random()
    if not evidence.get("goal"):
        payload = _fail_closed("no_task_goal")
        llm_used = False
    elif not evidence.get("transcripts"):
        payload = _fail_closed("no_dialogue")
        llm_used = False
    else:
        llm_used = True
        try:
            text, error = llm(_build_messages(evidence))
        except Exception as exc:  # noqa: BLE001
            text, error = None, f"llm_exception: {exc}"
        parsed = parse_json_payload(text or "") if not error else None
        if parsed is None:
            payload = _fail_closed(error or "invalid_verdict_json")
        else:
            conclusion = str(parsed.get("conclusion") or "")
            attribution = str(parsed.get("attribution") or "unknown")
            if conclusion not in CONCLUSIONS:
                payload = _fail_closed(f"invalid_conclusion: {conclusion!r}")
            else:
                raw_conf = parsed.get("confidence")
                confidence = (
                    max(0.0, min(1.0, float(raw_conf)))
                    if isinstance(raw_conf, (int, float)) else 0.0
                )
                payload = {
                    "conclusion": conclusion,
                    "attribution": (
                        attribution if attribution in ATTRIBUTIONS else "unknown"
                    ),
                    "confidence": confidence,
                    "reasons": str(parsed.get("reasons") or ""),
                    "evidence_refs": [
                        str(x) for x in (parsed.get("evidence_refs") or [])
                    ][:8],
                }

    # 复核门（确定性，先于置信度）：uncertain 全审；无硬证据的任何结论全审；
    # 其余高置信结论按比例抽检——「自信但错」只能靠抽检暴露（WIL-74）。
    hard = hard_evidence_present(evidence)
    if payload["conclusion"] == "uncertain":
        needs_review, review_reason = True, "uncertain"
    elif not hard:
        needs_review, review_reason = True, "no_hard_evidence"
    elif rng.random() < sample_rate:
        needs_review, review_reason = True, "sampled"
    else:
        needs_review, review_reason = False, None

    return {
        "schema_version": VERDICT_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "judge_model": judge_model,
        "generated_at": time.time() if now is None else now,
        "llm_used": llm_used,
        "hard_evidence": hard,
        "needs_review": needs_review,
        "review_reason": review_reason,
        **payload,
    }
