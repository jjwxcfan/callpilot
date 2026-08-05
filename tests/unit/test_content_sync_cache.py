"""内容同步不得每次请求全量重读所有通话目录。

回归 #73：`_call_artifacts()` 每次请求都重读所有通话目录的 meta.json +
summary.json + events.jsonl，`_find_call` 解析单个 id 也走全量扫描，无索引
无缓存。

叠加云侧 relay 只有 5s deadline、且 WSS 读循环同时兼管心跳：通话历史一长，
内容同步就从「慢」退化成「超时失败」，还会连带拖慢心跳。

这里断言的是**真实的文件读取次数**，不是「跑得快」——计时断言在负载机上不
可靠，而读取次数是确定性的。
"""

from __future__ import annotations

import json

import pytest

from agentcall.content_sync import ContentSyncRepository


class FakeHub:
    def snapshot_messages(self, *args, **kwargs):
        return []

    def recent_events(self, *args, **kwargs):
        return []


class FakeCallLogger:
    def __init__(self, base_dir):
        self.base_dir = base_dir


def make_call(base_dir, index: int) -> None:
    path = base_dir / f"2026080{index % 9}-1200{index:02d}-outbound-10086"
    path.mkdir(parents=True)
    (path / "meta.json").write_text(
        json.dumps(
            {
                "id": path.name,
                "direction": "outbound",
                "number": "10086",
                "started_at": 1785738110.0 + index,
                "ended_at": 1785738280.0 + index,
                "duration": 170.0,
                "status": "completed",
                "answered": True,
                "summary_state": "READY",
            }
        ),
        encoding="utf-8",
    )
    (path / "summary.json").write_text(
        json.dumps({"ok": True, "summary": f"call {index}", "intent": "查询"}),
        encoding="utf-8",
    )
    (path / "events.jsonl").write_text("", encoding="utf-8")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    base = tmp_path / "recordings"
    base.mkdir()
    for index in range(5):
        make_call(base, index)
    return ContentSyncRepository(FakeHub(), FakeCallLogger(base))  # type: ignore[arg-type]


def count_reads(monkeypatch) -> list[str]:
    """记录每一次 meta.json 的实际读取。"""
    from pathlib import Path

    reads: list[str] = []
    original = Path.read_text

    def spy(self, *args, **kwargs):
        if self.name == "meta.json":
            reads.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    return reads


def test_repeated_listing_does_not_reread_unchanged_calls(repo, monkeypatch):
    """核心：第二次列表不应再读任何 meta.json。"""
    repo.list_call_records(limit=50, cursor=None)  # 预热
    reads = count_reads(monkeypatch)
    repo.list_call_records(limit=50, cursor=None)
    assert reads == [], f"重复请求仍重读了 {len(reads)} 个 meta.json"


def test_first_listing_still_reads_everything(repo, monkeypatch):
    """缓存不能掩盖首次读取——冷启动仍要能拿到全部数据。"""
    reads = count_reads(monkeypatch)
    payload = repo.list_call_records(limit=50, cursor=None)
    assert len(reads) == 5
    assert len(payload["items"]) == 5


def test_a_changed_call_is_reparsed(repo, monkeypatch, tmp_path):
    """mtime 签名必须真的失效：summary 写入后要能读到新内容。"""
    repo.list_call_records(limit=50, cursor=None)
    base = repo._call_logger.base_dir
    target = sorted(p for p in base.iterdir() if p.is_dir())[0]
    (target / "summary.json").write_text(
        json.dumps({"ok": True, "summary": "UPDATED_SENTINEL", "intent": "查询"}),
        encoding="utf-8",
    )

    payload = repo.list_call_records(limit=50, cursor=None)
    summaries = [item.get("summaryPreview") or "" for item in payload["items"]]
    assert any("UPDATED_SENTINEL" in text for text in summaries), (
        "改了 summary.json 却读到了旧缓存"
    )


def test_summary_appearing_later_invalidates_the_cache(repo, tmp_path):
    """从「没有 summary.json」变成「有」也要失效——只看 mtime 会漏掉这种。"""
    base = repo._call_logger.base_dir
    fresh = base / "20260803-999999-outbound-10086"
    fresh.mkdir()
    (fresh / "meta.json").write_text(
        json.dumps(
            {
                "id": fresh.name,
                "direction": "outbound",
                "number": "10086",
                "started_at": 1785739000.0,
                "ended_at": 1785739100.0,
                "duration": 100.0,
                "status": "completed",
                "answered": True,
                "summary_state": "PENDING",
            }
        ),
        encoding="utf-8",
    )
    repo.list_call_records(limit=50, cursor=None)

    (fresh / "summary.json").write_text(
        json.dumps({"ok": True, "summary": "LATE_SUMMARY", "intent": "查询"}),
        encoding="utf-8",
    )
    payload = repo.list_call_records(limit=50, cursor=None)
    summaries = [item.get("summaryPreview") or "" for item in payload["items"]]
    assert any("LATE_SUMMARY" in text for text in summaries)


def test_lookup_by_id_does_not_scan_every_call(repo, monkeypatch):
    """Codex/#73 的第二点：取单条记录不该为此扫全部历史。"""
    payload = repo.list_call_records(limit=50, cursor=None)  # 预热并建索引
    call_id = payload["items"][0]["callId"]

    reads = count_reads(monkeypatch)
    repo._find_call(call_id)
    assert reads == [], f"按 id 取单条仍读了 {len(reads)} 个 meta.json"


def test_deleted_call_is_evicted(repo):
    """历史清理删掉目录后，缓存和索引不能继续留着它。"""
    import shutil

    payload = repo.list_call_records(limit=50, cursor=None)
    call_id = payload["items"][0]["callId"]
    base = repo._call_logger.base_dir
    directory = repo._call_id_index[call_id]
    shutil.rmtree(base / directory)

    repo.list_call_records(limit=50, cursor=None)
    assert directory not in repo._artifact_cache
    assert call_id not in repo._call_id_index


def count_all_reads(monkeypatch) -> dict[str, int]:
    """Codex P2：只数 meta.json 不够，summary.json / events.jsonl 也要证明省掉了。"""
    from pathlib import Path

    counts = {"meta.json": 0, "summary.json": 0, "events.jsonl": 0}
    original_text = Path.read_text
    original_open = Path.open

    def spy_text(self, *args, **kwargs):
        if self.name in counts:
            counts[self.name] += 1
        return original_text(self, *args, **kwargs)

    def spy_open(self, *args, **kwargs):
        if self.name in counts:
            counts[self.name] += 1
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy_text)
    monkeypatch.setattr(Path, "open", spy_open)
    return counts


def test_warm_listing_touches_no_artifact_file_at_all(repo, monkeypatch):
    repo.list_call_records(limit=50, cursor=None)
    counts = count_all_reads(monkeypatch)
    repo.list_call_records(limit=50, cursor=None)
    assert counts == {"meta.json": 0, "summary.json": 0, "events.jsonl": 0}, (
        f"命中路径仍在读文件: {counts}"
    )


def test_a_file_changing_mid_parse_is_not_cached(repo, monkeypatch):
    """Codex P1：解析途中文件被改，这份可能新旧混合的结果不得入缓存。

    否则它会带着一个「看起来很新」的签名把陈旧数据永久钉住，只能靠进程重启自愈。
    """
    base = repo._call_logger.base_dir
    target = sorted(p for p in base.iterdir() if p.is_dir())[0]
    repo.list_call_records(limit=50, cursor=None)  # 预热
    repo._artifact_cache.clear()

    import agentcall.content_sync as module

    original = module._read_call_artifact

    def racing_read(path, **kwargs):
        artifact = original(path, **kwargs)
        if path.name == target.name:
            # 模拟解析期间通话仍在追加事件。
            (path / "events.jsonl").write_text('{"type":"late"}\n', encoding="utf-8")
        return artifact

    monkeypatch.setattr(module, "_read_call_artifact", racing_read)
    repo.list_call_records(limit=50, cursor=None)

    assert target.name not in repo._artifact_cache, (
        "解析途中被改的目录不该进缓存"
    )


def test_cache_is_bounded(repo):
    """Codex P2：把「扫描慢」换成「内存无限涨」不算修好。"""
    import agentcall.content_sync as module

    base = repo._call_logger.base_dir
    for index in range(module._MAX_CACHED_ARTIFACTS + 20):
        make_call(base, 1000 + index)
    # list 的 limit 上限是 100；缓存是在扫描时填充的，直接走内部扫描更贴切。
    repo._call_artifacts()

    assert len(repo._artifact_cache) <= module._MAX_CACHED_ARTIFACTS
    # 索引不受 LRU 淘汰（很小），否则 _find_call 会退回全量扫描。
    assert len(repo._call_id_index) > module._MAX_CACHED_ARTIFACTS
