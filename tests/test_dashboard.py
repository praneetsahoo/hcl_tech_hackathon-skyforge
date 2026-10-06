"""Dashboard tests with Streamlit's AppTest (no browser, no real database)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("RB_DASHBOARD_PASSWORD", "correct-horse")
    monkeypatch.setenv("RB_DB_PASSWORD", "unused")
    monkeypatch.setenv("RB_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("RB_DB_PORT", "1")          # nothing listens here -> database "down"
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    return at


def log_in(at, password):
    at.text_input[0].input(password)
    at.button[0].click()
    at.run()


def test_login_required_before_any_data(app):
    assert [t.value for t in app.title] == ["RetailBank Analytics"]
    assert len(app.tabs) == 0 and not app.exception


def test_wrong_password_rejected(app):
    log_in(app, "wrong")
    assert any("Incorrect password" in e.value for e in app.error)
    assert len(app.tabs) == 0


def test_database_down_shows_friendly_error_not_crash(app):
    log_in(app, "correct-horse")
    assert len(app.tabs) == 9 and not app.exception
    assert any("could not load" in e.value for e in app.error)
