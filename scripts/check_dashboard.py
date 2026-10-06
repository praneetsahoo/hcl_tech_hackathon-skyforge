"""Phase 8 check: log in to the dashboard (headless, via Streamlit AppTest)
against the REAL database and confirm every tab renders without errors.
Usage (on EC2):  python -m scripts.check_dashboard
"""
import os
import secrets
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "dashboard" / "app.py")


def main():
    os.environ["RB_DASHBOARD_PASSWORD"] = password = secrets.token_hex(8)   # throwaway test login
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    at.text_input[0].input(password)
    at.button[0].click()
    at.run()
    errors = [e.value for e in at.error]
    print("tabs:", [t.label for t in at.tabs])
    print("exceptions:", len(at.exception), "| error boxes:", errors or "none")
    print("overview metrics:", {m.label: m.value for m in at.metric[:5]})
    print("tables rendered:", len(at.dataframe), "| charts rendered:",
          sum(1 for el in at.main if el.type in ("arrow_vega_lite_chart", "vega_lite_chart")) or "n/a")
    ok = not at.exception and not errors and len(at.tabs) == 9
    print("RESULT:", "PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
