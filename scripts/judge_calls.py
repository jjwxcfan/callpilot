#!/usr/bin/env python3
"""任务达成判官（WIL-95 第三期）：对历史通话跑自动初判，写 verdicts.json。

    .venv/bin/python scripts/judge_calls.py                  # 只判还没裁决的
    .venv/bin/python scripts/judge_calls.py --recompute      # 判官升级后全量重算
    .venv/bin/python scripts/judge_calls.py --dir data/recordings

裁决写 ``<call>/verdicts.json``（带判官模型 + 提示词版本），原始记录只读不改。
需要文本模型凭证（DASHSCOPE_API_KEY 或 OPENAI_API_KEY，同通话摘要一套）。
复核队列（needs_review=true）在看板页人工标注，或直接看本脚本的汇总输出。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agentcall import config  # noqa: E402
from agentcall.prompt_gen import (  # noqa: E402
    call_text_model,
    select_text_model,
    text_backend_for_agent,
)
from agentcall.task_verdict import build_evidence, judge_call  # noqa: E402


def _load_events(call_dir: Path) -> list[dict] | None:
    path = call_dir / "events.jsonl"
    if not path.is_file():
        return None
    events: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    except (OSError, ValueError):
        return None
    return events


def _load_summary(call_dir: Path) -> dict | None:
    path = call_dir / "summary.json"
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except (OSError, ValueError):
        return None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default="data/recordings", help="通话记录根目录")
    parser.add_argument(
        "--recompute", action="store_true",
        help="已有 verdicts.json 也重算（判官升级后对全部历史重新裁决）",
    )
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 1

    provider = text_backend_for_agent()
    model = select_text_model(provider, config.get_str("SUMMARY_MODEL"))

    def llm(messages: list[dict[str, str]]) -> tuple[str | None, str | None]:
        return call_text_model(
            messages, provider=provider, model=model, timeout=30.0, max_tokens=500
        )

    judged = skipped = existing = 0
    conclusions: dict[str, int] = {}
    review = 0
    for call_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        out_path = call_dir / "verdicts.json"
        if out_path.is_file() and not args.recompute:
            existing += 1
            continue
        events = _load_events(call_dir)
        if events is None:
            skipped += 1
            continue
        evidence = build_evidence(events, _load_summary(call_dir))
        verdict = judge_call(evidence, llm, judge_model=f"{provider}/{model}")
        try:
            out_path.write_text(
                json.dumps(verdict, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"写入 {call_dir.name}/verdicts.json 失败: {exc}", file=sys.stderr)
            skipped += 1
            continue
        judged += 1
        conclusions[verdict["conclusion"]] = (
            conclusions.get(verdict["conclusion"], 0) + 1
        )
        if verdict["needs_review"]:
            review += 1

    print(
        f"已裁决 {judged} 通（跳过 {skipped}，已有裁决 {existing}"
        f"{'，--recompute 可重算' if existing and not args.recompute else ''}）"
    )
    for conclusion, count in sorted(conclusions.items()):
        print(f"  {conclusion}: {count}")
    if review:
        print(f"  ⚠️ 待人工复核: {review} 通（看板页标注，或查 verdicts.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
