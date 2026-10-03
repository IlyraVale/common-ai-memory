import ast
import tempfile
from pathlib import Path
from unittest.mock import patch

from memory_doctor import inspect_memory
from memory_store import MemoryStore, PASSIVE_CONTEXT_BUDGET, passive_query_signal


def rec(mid, content, owner="gpt", lifecycle=None):
    row = {"id": mid, "owner": owner, "scope": "agent", "category": "life/daily",
           "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
           "content": content, "source": "observed", "status": "done"}
    if lifecycle:
        row["lifecycle"] = lifecycle
    return row


def make_store():
    temp = tempfile.TemporaryDirectory(prefix="public-passive-")
    store = MemoryStore(temp.name, "gpt")
    for row in (rec("grammar", "不喜欢硬啃英语时态，更适合从基础句式开始"),
                rec("stack", "Moonharbor 使用 Vue3 + Vite + Pinia"),
                rec("food", "今天想吃西瓜")):
        store.import_record(row)
    return temp, store


def test_passive_false_preserves_normal_and_exact_positive():
    temp, store = make_store()
    try:
        assert store.recall("英语时态") == store.recall("英语时态", passive=False)
        assert store.recall("英语时态", passive=True)[0]["id"] == "grammar"
    finally: temp.cleanup()


def test_passive_semantic_paraphrase_and_zero_negative():
    temp, store = make_store()
    try:
        rows = [{"memory_id": "grammar", "similarity": .63, "source": "observed", "updated_at": "2026"},
                {"memory_id": "food", "similarity": .10, "source": "observed", "updated_at": "2026"}]
        with patch("memory_vectors.MemoryVectorIndex.search", return_value=rows):
            assert store.recall("看到复杂语法规则我就不想学了", passive=True)[0]["id"] == "grammar"
        with patch("memory_vectors.MemoryVectorIndex.search", return_value=[]):
            assert store.recall("量子引力的实验结果是什么", passive=True) == []
    finally: temp.cleanup()


def test_low_information_and_emoji_are_zero():
    temp, store = make_store()
    try:
        for query in ("", "😀", "嗯", "好吧", "哈哈哈哈", "知道了", "继续"):
            assert store.recall(query, passive=True) == []
    finally: temp.cleanup()


def test_max_three_no_padding_and_owner_isolation():
    temp, store = make_store()
    try:
        for index in range(5): store.import_record(rec(f"x{index}", "passive exact cluster"))
        assert len(store.recall("passive exact cluster", passive=True, limit=30)) == 3
        store.import_record(rec("private", "claude private marker", owner="claude"))
        assert store.recall("claude private marker", owner="gpt", passive=True) == []
    finally: temp.cleanup()


def test_lifecycle_and_fallback():
    temp, store = make_store()
    try:
        store.import_record(rec("stale", "stale exact marker", lifecycle="stale"))
        assert store.recall("stale exact marker", passive=True) == []
        with patch("memory_vectors.MemoryVectorIndex.search", side_effect=RuntimeError("offline")):
            assert store.recall("英语时态", passive=True)[0]["id"] == "grammar"
    finally: temp.cleanup()


def test_doctor_and_schema_contract():
    temp, store = make_store()
    try:
        passive = inspect_memory(temp.name)["passive_recall"]
        assert passive["positive_probe"] == passive["negative_probe"] == "PASS"
        assert passive["max_results"] == 3
        assert passive["context_budget"] == PASSIVE_CONTEXT_BUDGET
    finally: temp.cleanup()
    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8-sig"))
    recall = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "recall")
    assert "passive" in [arg.arg for arg in recall.args.args]


def test_signal_combines_structure_and_filler_guard():
    assert passive_query_signal("此前项目里决定的部署顺序是什么")["eligible"]
    assert not passive_query_signal("今天随便聊聊")["eligible"]
