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


# --- the reCAPTCHA NACTA now asks for before an export ---------------------------------------------------------------

class _Clock:
    """A fake clock that only moves when the code under test waits, so these tests take no real time."""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


class _FakePage:
    def __init__(self, clock, captcha_until=None, captcha_from=0.0, download_at=None, downloads=None):
        self.clock, self.downloads = clock, downloads
        self.captcha_from, self.captcha_until, self.download_at = captcha_from, captcha_until, download_at
        self.clicks = 0

    def wait_for_timeout(self, ms):
        self.clock.t += ms / 1000
        if self.download_at is not None and self.clock.t >= self.download_at and not self.downloads:
            self.downloads.append("the-file")

    def showing(self):
        if self.captcha_until is None:
            return False
        return self.captcha_from <= self.clock.t < self.captcha_until


def _run(page, downloads, show, timeout_s=120, captcha_wait_s=300, monkeypatch=None, clock=None):
    monkeypatch.setattr(fetch_nacta, "captcha_showing", lambda p: p.showing())

    def click():
        page.clicks += 1
    return fetch_nacta.await_download(page, downloads, click, show, timeout_s, captcha_wait_s, now=clock.now)


def test_headless_stops_at_once_when_the_captcha_appears(monkeypatch):
    clock, downloads = _Clock(), []
    page = _FakePage(clock, captcha_until=10_000, downloads=downloads)
    with pytest.raises(fetch_nacta.CaptchaRequired, match="cannot run unattended"):
        _run(page, downloads, show=False, monkeypatch=monkeypatch, clock=clock)
    assert clock.t < 1          # no 120 second wait


def test_a_download_with_no_captcha_is_returned(monkeypatch):
    clock, downloads = _Clock(), []
    page = _FakePage(clock, download_at=2, downloads=downloads)
    assert _run(page, downloads, show=False, monkeypatch=monkeypatch, clock=clock) == "the-file"
    assert page.clicks == 1


def test_no_download_and_no_captcha_times_out_with_a_plain_message(monkeypatch):
    clock, downloads = _Clock(), []
    page = _FakePage(clock, downloads=downloads)
    with pytest.raises(TimeoutError, match="no reCAPTCHA was shown"):
        _run(page, downloads, show=False, timeout_s=10, monkeypatch=monkeypatch, clock=clock)


def test_shown_window_waits_for_the_person_and_carries_on(monkeypatch, capsys):
    clock, downloads = _Clock(), []
    # the dialog is up for 40 s, the person ticks it, and the site starts the download by itself
    page = _FakePage(clock, captcha_until=40, download_at=43, downloads=downloads)
    assert _run(page, downloads, show=True, monkeypatch=monkeypatch, clock=clock) == "the-file"
    assert page.clicks == 1     # it did not click again: the site did the rest
    assert "Tick" in capsys.readouterr().err


def test_shown_window_clicks_again_if_the_site_does_not_start_the_download(monkeypatch):
    clock, downloads = _Clock(), []
    page = _FakePage(clock, captcha_until=20, downloads=downloads)

    def second_click_downloads(ms, _orig=page.wait_for_timeout):
        _orig(ms)
        if page.clicks >= 2 and not downloads:
            downloads.append("the-file")
    page.wait_for_timeout = second_click_downloads
    assert _run(page, downloads, show=True, monkeypatch=monkeypatch, clock=clock) == "the-file"
    assert page.clicks == 2


def test_shown_window_gives_up_when_nobody_ticks_the_box(monkeypatch):
    clock, downloads = _Clock(), []
    page = _FakePage(clock, captcha_until=10_000, downloads=downloads)
    with pytest.raises(fetch_nacta.CaptchaRequired, match="not solved within 60 seconds"):
        _run(page, downloads, show=True, captcha_wait_s=60, monkeypatch=monkeypatch, clock=clock)


def test_captcha_showing_is_false_when_the_page_errors():
    class Broken:
        def get_by_text(self, *a, **k):
            raise RuntimeError("page closed")
    assert fetch_nacta.captcha_showing(Broken()) is False
