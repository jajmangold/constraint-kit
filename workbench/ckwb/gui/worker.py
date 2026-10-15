# -*- coding: utf-8 -*-
"""One HTTP request on a background thread, delivered back by a Qt signal.

This exists because of a defect worth naming: the first version of the Describe panel called the
planner synchronously, so a slow model froze FreeCAD's entire interface for as long as the
request took — up to the client's 180 s timeout. An unresponsive CAD application is not a
cosmetic problem; the user cannot even see what it is waiting for.

The obvious fix — run the whole planner in a worker — is **wrong**, and that is the interesting
part. Validation builds the proposed parts in order to check their frames against real geometry,
and driving OCCT from a non-GUI thread is not safe. So the split follows what is actually
thread-safe rather than what is convenient:

* here, off-thread: the HTTP call. Pure networking, no FreeCAD, no OCCT.
* in `PlanSession`, on the GUI thread: prompting, parsing, validating, building.

The panel owns a `PlanSession` and alternates between them, which is also why the repair loop
needs no special handling — it is just a second trip through the same pair.
"""
from __future__ import annotations

from PySide import QtCore


class ChatWorker(QtCore.QThread):
    """Runs one chat completion. `replied` carries the text; `failed` carries a message."""

    replied = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, messages: list, cfg: dict, parent=None):
        super().__init__(parent)
        self._messages = messages
        self._cfg = dict(cfg)
        self._cancelled = False

    def cancel(self) -> None:
        """Mark the result unwanted.

        It cannot interrupt a socket read that is already blocked, so the request still runs to
        completion in the background — but nothing is delivered and the panel stops waiting.
        Pretending otherwise would be worse than saying so.
        """
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:  # noqa: D102 - QThread entry point
        from ..llm import chat

        try:
            reply = chat(self._messages,
                         base_url=self._cfg.get("base_url", ""),
                         api_key=self._cfg.get("api_key", ""),
                         model=self._cfg.get("planner_model", ""))
        except Exception as exc:  # noqa: BLE001 - reported to the panel, never swallowed
            if not self._cancelled:
                self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        if not self._cancelled:
            self.replied.emit(reply)
