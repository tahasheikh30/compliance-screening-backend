"""
A person's own settings, stored as one validated JSON document in `user_preferences`.

  notifications  what the app tells them about: everything on or off, pop-ups on or off, and each kind of message
  lists          the optional lists their own screenings leave out ("off"). Only the sources an operator allows
                 people to switch off (USER_SWITCHABLE_SOURCES) can be listed: the core sanctions and watch lists
                 can never be switched off by a person.
  screening      the match threshold they start from, and whether new screenings are monitored by default
  security       minutes without activity before they are signed out (never more than the 30 minute policy)
  display        the tab they open on, and how dense the tables are

A screening that leaves a list out says so on its result row ("Switched off in the analyst's settings"), and the list
never counts as screened, so it can never make a case look more thoroughly checked than it was.
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import config

CATEGORIES = ("screening", "monitoring", "lists", "account", "admin")
IDLE_CHOICES = (5, 10, 15, 30)
TABS = ("screen", "history", "monitoring", "lists")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotificationCategories(_Strict):
    screening: bool = True      # a screening finished, or failed
    monitoring: bool = True     # continuous monitoring found something
    lists: bool = True          # a watch list changed or could not be read
    account: bool = True        # password changed, history cleared
    admin: bool = True          # people waiting for approval (administrators)


class NotificationPrefs(_Strict):
    enabled: bool = True        # false: no message at all (errors that need attention are still shown)
    popups: bool = True         # false: messages are kept in the Notifications list but do not pop up
    categories: NotificationCategories = Field(default_factory=NotificationCategories)


class ListPrefs(_Strict):
    off: list[str] = Field(default_factory=list)

    @field_validator("off")
    @classmethod
    def unique_known(cls, v):
        out = []
        for key in v:
            key = str(key).strip().upper()
            if key and key not in out:
                out.append(key)
        return out


class ScreeningPrefs(_Strict):
    threshold: Optional[float] = Field(default=None, ge=50, le=100)
    monitor_by_default: bool = False


class SecurityPrefs(_Strict):
    idle_minutes: Literal[5, 10, 15, 30] = 30


class DisplayPrefs(_Strict):
    default_tab: Literal["screen", "history", "monitoring", "lists"] = "screen"
    density: Literal["comfortable", "compact"] = "comfortable"


class Preferences(_Strict):
    notifications: NotificationPrefs = Field(default_factory=NotificationPrefs)
    lists: ListPrefs = Field(default_factory=ListPrefs)
    screening: ScreeningPrefs = Field(default_factory=ScreeningPrefs)
    security: SecurityPrefs = Field(default_factory=SecurityPrefs)
    display: DisplayPrefs = Field(default_factory=DisplayPrefs)


def switchable_sources() -> list:
    """The sources a person may switch off for their own screenings (an operator setting)."""
    return list(config.USER_SWITCHABLE_SOURCES)


def defaults() -> dict:
    return Preferences().model_dump()


def load(raw) -> dict:
    """The stored document as a full, valid preferences dict. Anything missing or unreadable falls back to defaults."""
    try:
        prefs = Preferences.model_validate(raw or {})
    except Exception:       # noqa: BLE001  (a document from an older or newer version: never block screening on it)
        prefs = Preferences()
    out = prefs.model_dump()
    allowed = set(switchable_sources())
    out["lists"]["off"] = [k for k in out["lists"]["off"] if k in allowed]
    return out


def skipped_sources(raw) -> frozenset:
    """The sources this person's screenings leave out."""
    return frozenset(load(raw)["lists"]["off"])
