"""
Running a batch: screen every row of an uploaded file, one after the other, in a background thread.

Each row is a normal screening (the same function the single screening route uses), so it appears in the
person's history, has a case and an evidence PDF, and can be put under monitoring. The browser starts the
batch, then polls GET /api/batches/{id} for progress.

Several servers can run batches side by side. Everything they must agree on lives in the database, not in
memory: before each row the running server records a heartbeat and reads the batch's cancel flag (so Cancel
works whichever server receives it), and a batch whose heartbeat has gone stale because its server died is
marked "interrupted" by any server. A server that shuts down on purpose interrupts its own batches at once.

What this deliberately does not do:
  * Survive losing its server. The rows are held in memory while the batch runs, so a batch cut off by a
    restart or crash is marked "interrupted"; rows already screened are kept and the rest are reported as not
    screened. It is never treated as clear.
  * Run in parallel. Rows go one at a time; at most BATCH_MAX_RUNNING batches run at once on each server, so
    adding servers adds capacity.
"""

import threading
import uuid

from pydantic import ValidationError

from app import config
from app import database as db
from app.errors import AppError, logger
from app.schemas import ScreenRequest

OWNER = uuid.uuid4().hex[:12]                  # names this server process in batches.owner
_lock = threading.Lock()
_active: set[int] = set()                      # batches running in this process (the capacity limit is per server)
_stop = threading.Event()                      # set when this server is shutting down


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
        return len(_active) < config.BATCH_MAX_RUNNING


def start(batch_id: int, items: list, screen_one, finished=None) -> None:
    """
    Begin screening `items` (the (row, ScreenRequest) pairs from prepare_rows) in a background thread.
    `screen_one(req)` screens one row and returns its ScreenResponse; `finished(status)` is told the outcome.
    """
    with _lock:
        _active.add(batch_id)
    t = threading.Thread(target=_run, args=(batch_id, items, screen_one, finished), name=f"batch-{batch_id}", daemon=True)
    t.start()


def startup() -> None:
    """The server is (re)starting in this process: batches may run again."""
    _stop.clear()


def shutdown() -> None:
    """This server is stopping: tell its batches to stop after the row they are on, and mark them interrupted now."""
    _stop.set()
    try:
        n = db.batch_interrupt_owned(OWNER)
        if n:
            logger.warning("%d running batch(es) interrupted by this server shutting down", n)
    except Exception:
        logger.exception("Could not mark this server's batches interrupted")


def _checkpoint(batch_id: int) -> dict | None:
    """Heartbeat plus 'should I go on?'. A database hiccup here must not end the batch: carry on and retry."""
    try:
        return db.batch_checkpoint(batch_id)
    except Exception:
        logger.exception("Batch %s: heartbeat failed", batch_id)
        return {"cancel_requested": False, "user_status": "approved"}


def _run(batch_id: int, items: list, screen_one, finished) -> None:
    status = "done"
    try:
        todo = [(row, req) for row, req in items if req is not None]
        for n, (row, req) in enumerate(todo):
            if _stop.is_set():
                status = "interrupted"
                break
            cp = _checkpoint(batch_id)
            if cp is None:                       # another server gave this batch up as stale: do not carry on
                status = "interrupted"
                break
            if cp["cancel_requested"]:
                status = "cancelled"
                break
            if cp["user_status"] != "approved":  # rejected, disabled or deleted since the upload: no more screening for them
                logger.warning("Batch %s stopped: its owner is no longer approved", batch_id)
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
                _stop.wait(config.BATCH_ROW_DELAY_SECONDS)
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
            _active.discard(batch_id)
