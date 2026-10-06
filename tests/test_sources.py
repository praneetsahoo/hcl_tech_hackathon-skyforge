"""Tests for the landing zone, any-batch support and business-date ordering."""
import dataclasses
from datetime import date

import pytest

from pipeline.config import get_settings, validate_batch_id
from pipeline.load import is_older
from pipeline.run_pipeline import _business_date
from pipeline.sources import resolve_sources, safe_file_name, upload_landing


@pytest.fixture
def local(tmp_path):
    """Local settings (no S3) with an empty data folder."""
    return dataclasses.replace(get_settings(), s3_bucket="", data_dir=tmp_path)


def test_official_batches_still_read_from_repo():
    files, origin, _ = resolve_sources(dataclasses.replace(get_settings(), s3_bucket=""), "day2")
    assert sorted(files) == ["branches", "customers", "products", "transactions"]
    assert files["products"].name == "products_day2.json"


def test_partial_day3_drop_from_landing_zone(local):
    upload_landing(local, "day3", "transactions", "transactions_day3.csv", b"transaction_id\nT1\n")
    files, origin, _ = resolve_sources(local, "day3")
    assert list(files) == ["transactions"]                 # other entities: nothing new
    assert files["transactions"].read_bytes() == b"transaction_id\nT1\n"   # stored as received


def test_reupload_replaces_file_for_that_entity(local):
    upload_landing(local, "day3", "customers", "old.csv", b"a\n1\n")
    upload_landing(local, "day3", "customers", "new.csv", b"a\n2\n")
    files, _, _ = resolve_sources(local, "day3")
    assert files["customers"].name == "new.csv"


def test_batch_with_no_files_is_an_error(local):
    with pytest.raises(FileNotFoundError, match="no source files"):
        resolve_sources(local, "day9")


@pytest.mark.parametrize("bad", ["Day3", "day 3", "../etc", "x" * 11, ""])
def test_unsafe_batch_ids_rejected(bad):
    with pytest.raises(ValueError):
        validate_batch_id(bad)


def test_file_name_sanitised_and_extension_checked():
    assert safe_file_name("../../tmp/evil name.csv", "customers") == "evil_name.csv"
    with pytest.raises(ValueError):
        safe_file_name("products.csv", "products")            # products must be JSON


def test_empty_upload_rejected(local):
    with pytest.raises(ValueError, match="empty"):
        upload_landing(local, "day3", "branches", "b.csv", b"")


def test_batches_ordered_by_business_date():
    dates = {"day1": date(2026, 9, 30), "day2": date(2026, 10, 1), "day3": date(2026, 10, 2)}
    assert is_older("day2", "day3", dates) and not is_older("day3", "day2", dates)
    assert is_older("day1", "day3", dates)


def test_business_date_rules():
    assert _business_date("day1", None, {}) == date(2026, 9, 30)          # fixed
    assert _business_date("day3", "2026-10-02", {}) == date(2026, 10, 2)   # new batch
    assert _business_date("day3", None, {"day3": date(2026, 10, 2)}) == date(2026, 10, 2)   # re-run
    with pytest.raises(ValueError, match="needs a business date"):
        _business_date("day3", None, {})
    with pytest.raises(ValueError, match="registered with business date"):
        _business_date("day3", "2026-10-05", {"day3": date(2026, 10, 2)})
    with pytest.raises(ValueError, match="always has business date"):
        _business_date("day2", "2026-12-01", {})
