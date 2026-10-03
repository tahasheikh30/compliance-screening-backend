"""The checks scripts/fetch_nacta.py applies to a downloaded file before it is used. The browser part is
tested by hand against a local imitation of the page, because it needs Chromium."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("fetch_nacta", Path(__file__).resolve().parent.parent / "scripts" / "fetch_nacta.py")
fetch_nacta = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch_nacta)


def _file(tmp_path, n, with_cnic=True, name="nacta.json"):
    rows = [{"#": i, "NAME": f"PERSON {i}", "FATHER NAME": "NILL",
             "CNIC": f"{4200000000000 + i}" if with_cnic else "", "PROVINCE": "PUNJAB", "DISTRICT": "BANNU"}
            for i in range(1, n + 1)]
    p = tmp_path / name
    p.write_text(json.dumps(rows))
    return p


def test_a_complete_download_passes(tmp_path):
    count, warnings = fetch_nacta.check_file(_file(tmp_path, 1500), 1500, 1000)
    assert count == 1500 and warnings == []


def test_too_few_people_is_refused(tmp_path):
    with pytest.raises(ValueError, match="fewer than the 1000 expected"):
        fetch_nacta.check_file(_file(tmp_path, 300), None, 1000)


def test_a_file_far_shorter_than_the_page_total_is_refused(tmp_path):
    with pytest.raises(ValueError, match="probably partial"):
        fetch_nacta.check_file(_file(tmp_path, 1500), 5294, 1000)


def test_a_small_difference_from_the_page_total_only_warns(tmp_path):
    _, warnings = fetch_nacta.check_file(_file(tmp_path, 1500), 1540, 1000)     # 2.6 percent short
    assert len(warnings) == 1 and "1,540" in warnings[0]
    assert fetch_nacta.check_file(_file(tmp_path, 1500), 1510, 1000)[1] == []    # within tolerance


def test_missing_cnics_are_reported(tmp_path):
    _, warnings = fetch_nacta.check_file(_file(tmp_path, 1500, with_cnic=False), None, 1000)
    assert any("usable CNIC" in w for w in warnings)


def test_an_unreadable_file_raises_the_parsers_message(tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text("<html>Access denied</html>")
    with pytest.raises(ValueError):
        fetch_nacta.check_file(bad, None, 1000)


def test_upload_requires_credentials(capsys):
    assert fetch_nacta.main(["--upload", "--api-url", "", "--api-key", ""]) == 3
    assert "SCREENING_API_URL" in capsys.readouterr().err
