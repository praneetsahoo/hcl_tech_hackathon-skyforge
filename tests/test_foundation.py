"""Foundation checks: config resolves, every raw file is present, and the
profiler handles the deliberately broken products.json."""
import json

import pytest

from pipeline.config import BATCH_FILES, get_settings, raw_path
from pipeline.readers import read_json_salvaging


@pytest.mark.parametrize("batch_id", sorted(BATCH_FILES))
def test_all_raw_files_present(batch_id):
    settings = get_settings()
    for entity in BATCH_FILES[batch_id]:
        path = raw_path(settings, batch_id, entity)
        assert path.exists(), f"missing {path}"
        assert path.stat().st_size > 0


def test_unknown_batch_rejected():
    with pytest.raises(ValueError):
        raw_path(get_settings(), "day9", "customers")


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("RB_DB_NAME", "test_db")
    assert get_settings().db_name == "test_db"


def test_broken_json_is_salvaged_not_crashed():
    broken = '[{"product_id": "P1"}\n {"product_id": "P2"}]'  # missing comma
    records, error, lost = read_json_salvaging(broken)
    assert error, "defect should be reported"
    assert [r["product_id"] for r in records] == ["P1", "P2"]
    assert lost == []


def test_valid_json_has_no_error():
    records, error, lost = read_json_salvaging(json.dumps([{"a": 1}]))
    assert records == [{"a": 1}] and error == "" and lost == []
