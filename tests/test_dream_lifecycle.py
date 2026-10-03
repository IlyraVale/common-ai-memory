from datetime import datetime, timezone

from dream_nightly import run_owner
from dreams import PreparedDreamStore, claim_on_wake_dream, dream_commit_result, dream_get_result


NOW = datetime(2026, 9, 30, 4, tzinfo=timezone.utc)


def test_default_on_wake_nightly_prepare_then_commit(tmp_path):
    first = run_owner(tmp_path, "example-owner", timezone_name="Asia/Shanghai", now=NOW)
    second = run_owner(tmp_path, "example-owner", timezone_name="Asia/Shanghai", now=NOW)
    assert first["status"] == "created"
    assert second["status"] == "reused"
    assert first["generation_id"] == second["generation_id"]

    pending = claim_on_wake_dream(tmp_path, "example-owner", now=NOW)
    assert pending["generation_id"] == first["generation_id"]
    result = dream_commit_result(
        tmp_path, "example-owner", pending["dream_date"], pending["claim_token"], "derived body", now=NOW
    )
    assert result["ok"] is True
    assert PreparedDreamStore(tmp_path).load("example-owner", pending["dream_date"]) is None
    assert dream_get_result(tmp_path, "example-owner", pending["dream_date"])["status"] == "found"


def test_no_scheduler_fallback_prepares_and_claims(tmp_path):
    pending = claim_on_wake_dream(tmp_path, "another-owner", now=NOW)
    assert pending is not None
    assert pending["materials"] == []
    assert PreparedDreamStore(tmp_path).load("another-owner", pending["dream_date"]) is not None
