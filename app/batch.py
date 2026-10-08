"""
Running a batch: screen every row of an uploaded file, one after the other, in a background thread.

Each row is a normal screening (the same function the single screening route uses), so it appears in the
person's history, has a case and an evidence PDF, and can be put under monitoring. The browser starts the
batch, then polls GET /api/batches/{id} for progress.

What this deliberately does not do:
  * Survive a restart. The rows are held in memory while the batch runs. If the server restarts, the batch is
    marked "interrupted" on the next start; rows already screened are kept and the rest are reported as not
    screened. It is never treated as clear.
  * Run in parallel. Rows go one at a time; at most BATCH_MAX_RUNNING batches run at once on a server.
"""

import threading

from pydantic import ValidationError

from app import config
from app import database as db
from app.errors import AppError, logger
from app.schemas import ScreenRequest

_lock = threading.Lock()
_cancel: dict[int, threading.Event] = {}      # batch id -> set when the person pressed Cancel


_LABELS = {"full_name": "Full name", "dob": "Date of birth", "nationality": "Nationality", "cnic": "CNIC",
           "father_name": "Father or husband", "province": "Province"}


def _reason(exc: ValidationError) -> str:
    """The first problem with a row, in words, without echoing what was in it."""
    err = exc.errors()[0]
    field = next((str(p) for p in err.get("loc", ()) if str(p) in _LABELS), None)
    msg = str(err.get("msg", "invalid")).removeprefix("Value error, ").rstrip(".")
    return f"{_LABELS[field]}: {msg}." if field else f"{msg}."


def prepare_rows(parsed: list, threshold: float, monitor: bool) -> list:
    """
    Turn parsed file rows into (row dict for the database, ScreenRequest or None). A row the screening would
    refuse (no name, a name with no Latin letters, a value too long) is kept as 'invalid' with the reason, so
    the person sees exactly which row to fix instead of the whole file being rejected.
    """
    out = []
    for p in parsed:
        v = p.values
        name = v.get("full_name", "")
        if not name.strip():
            out.append(({"row_no": p.row_no, "full_name": "", "state": "invalid", "error": "Full name is missing."}, None))
            continue
        try:
            req = ScreenRequest(full_name=name, dob=v.get("dob") or None, nationality=v.get("nationality") or None,
                                cnic=v.get("cnic") or None, father_name=v.get("father_name") or None,
                                province=v.get("province") or None, threshold=threshold, monitor=monitor)
            out.append(({"row_no": p.row_no, "full_name": req.full_name, "state": "pending"}, req))
        except ValidationError as exc:
            out.append(({"row_no": p.row_no, "full_name": name[:200], "state": "invalid", "error": _reason(exc)}, None))
    return out


def capacity_left() -> bool:
    with _lock:
        return len(_cancel) < config.BATCH_MAX_RUNNING


def start(batch_id: int, items: list, screen_one, finished=None) -> None:
    """
    Begin screening `items` (the (row, ScreenRequest) pairs from prepare_rows) in a background thread.
    `screen_one(req)` screens one row and returns its ScreenResponse; `finished(status)` is told the outcome.
    """
    event = threading.Event()
    with _lock:
        _cancel[batch_id] = event
    t = threading.Thread(target=_run, args=(batch_id, items, screen_one, finished, event),
                         name=f"batch-{batch_id}", daemon=True)
    t.start()


def cancel(batch_id: int) -> bool:
    """Ask a running batch to stop after the row it is on. False if it is not running here."""
    with _lock:
        event = _cancel.get(batch_id)
    if event is None:
        return False
    event.set()
    return True


def _run(batch_id: int, items: list, screen_one, finished, event: threading.Event) -> None:
    status = "done"
    try:
        todo = [(row, req) for row, req in items if req is not None]
        for n, (row, req) in enumerate(todo):
            if event.is_set():
                status = "cancelled"
                break
            try:
                result = screen_one(req)
                db.batch_row_screened(batch_id, row["row_no"], result.applicant_id)
            except AppError as exc:
                db.batch_row_failed(batch_id, row["row_no"], exc.message)
            except Exception:
                # the details (which may name someone) stay in the server log, keyed by the batch
                logger.exception("Batch %s: row %s could not be screened", batch_id, row["row_no"])
                db.batch_row_failed(batch_id, row["row_no"], "This row could not be screened. Try it again on its own.")
            if config.BATCH_ROW_DELAY_SECONDS and n < len(todo) - 1:
                event.wait(config.BATCH_ROW_DELAY_SECONDS)
    except Exception:
        logger.exception("Batch %s stopped unexpectedly", batch_id)
        status = "interrupted"
    finally:
        try:
            db.batch_finish(batch_id, status)
            if finished:
                finished(status)
        except Exception:
            logger.exception("Batch %s: could not record how it ended", batch_id)
        with _lock:
            _cancel.pop(batch_id, None)
