"""Fyller i ansökningsformulär i en övervakad webbläsare. Användaren skickar själv.

Playwrights sync-API måste köras i en och samma tråd. FormApplier äger därför en
bakgrundstråd med en beständig Chromium-kontext (paths.browser); uppdrag läggs i
en kö och svaret kommer tillbaka via en Future. Varje uppdrag öppnar en ny flik
som lämnas öppen så att användaren kan granska, komplettera och skicka.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from jobaplier.apply.base import SubmitError, SubmitResult
from jobaplier.apply.form.fields import Applicant
from jobaplier.apply.form.generic import FillReport, FormAdapter
from jobaplier.apply.pdf import render_letter_pdf
from jobaplier.config import Config, Paths
from jobaplier.models import ApplicationStatus, Channel, Draft, Job

SUBMIT_TIMEOUT_S = 120
CLOSE_JOIN_TIMEOUT_S = 10
NAVIGATION_TIMEOUT_MS = 60_000

_STOP = object()
_Task = tuple[str, Applicant, "Future[FillReport]"]


class FormApplier:
    def __init__(
        self,
        config: Callable[[], Config],
        paths: Paths,
        adapters: list[FormAdapter],
        headless: bool = False,
        *,
        allow_file_urls: bool = False,
    ) -> None:
        self.config = config
        self.paths = paths
        self.adapters = adapters
        self.headless = headless
        self.allow_file_urls = allow_file_urls
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._tasks: queue.Queue[_Task | object] | None = None

    # --- anropas från valfri tråd -------------------------------------------------

    def submit(self, job: Job, draft: Draft) -> SubmitResult:
        url = self._checked_url(job.apply_form_url)
        config = self.config()
        if not self.paths.cv.is_file():
            raise SubmitError("Ditt CV saknas (data/cv.pdf) – ladda upp det först")
        try:
            letter_pdf = render_letter_pdf(
                draft.letter_text,
                config.profile.name,
                self.paths.data_dir / "tmp" / f"letter-{job.id}.pdf",
            )
        except Exception as e:
            raise SubmitError(f"Kunde inte skapa det personliga brevet som PDF: {e}") from e
        applicant = Applicant.from_config(config, self.paths, draft.letter_text, letter_pdf)

        future: Future[FillReport] = Future()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._start_worker()
            assert self._tasks is not None
            self._tasks.put((url, applicant, future))
        try:
            report = future.result(timeout=SUBMIT_TIMEOUT_S)
        except FutureTimeout as e:
            # Ett ej påbörjat uppdrag får inte öppnas senare (appen har redan
            # registrerat felet; en sen ifyllning kunde ge en dubbelansökan).
            future.cancel()
            raise SubmitError(
                f"Formuläret hann inte fyllas i inom {SUBMIT_TIMEOUT_S} sekunder"
            ) from e
        except Exception as e:
            # Playwright-fel har en lång anropslogg efter första raden.
            reason = (str(e).splitlines() or [type(e).__name__])[0]
            raise SubmitError(f"Kunde inte fylla i formuläret: {reason}") from e
        return SubmitResult(
            channel=Channel.form,
            status=ApplicationStatus.submitting,
            notes=report.unanswered,
        )

    def close(self) -> None:
        with self._lock:
            thread, tasks = self._thread, self._tasks
            self._thread = self._tasks = None
        if thread is None or tasks is None:
            return
        tasks.put(_STOP)
        thread.join(timeout=CLOSE_JOIN_TIMEOUT_S)

    def _checked_url(self, url: str | None) -> str:
        if not url:
            raise SubmitError("Jobbet saknar länk till ansökningsformulär")
        try:
            parts = urlsplit(url.strip())
        except ValueError as e:
            raise SubmitError("Länken till ansökningsformuläret är ogiltig") from e
        if parts.scheme in ("http", "https") and parts.hostname:
            return url.strip()
        if parts.scheme == "file" and self.allow_file_urls:
            return url.strip()
        raise SubmitError("Länken till ansökningsformuläret är ogiltig")

    def _start_worker(self) -> None:
        """Anropas med self._lock hållet."""
        tasks: queue.Queue[_Task | object] = queue.Queue()
        thread = threading.Thread(
            target=self._run, args=(tasks,), name="jobaplier-form-browser", daemon=True
        )
        self._tasks, self._thread = tasks, thread
        thread.start()

    # --- körs bara i bakgrundstråden ----------------------------------------------

    def _run(self, tasks: queue.Queue[_Task | object]) -> None:
        try:
            with sync_playwright() as pw:
                context: BrowserContext | None = None
                try:
                    while (item := tasks.get()) is not _STOP:
                        url, applicant, future = item  # type: ignore[misc]
                        if not future.set_running_or_notify_cancel():
                            continue
                        try:
                            context, page = self._open_page(pw, context)
                            future.set_result(self._fill(page, url, applicant))
                        except Exception as e:  # noqa: BLE001 – skickas vidare till submit
                            future.set_exception(e)
                finally:
                    if context is not None:
                        try:
                            context.close()
                        except PlaywrightError:
                            pass  # användaren har redan stängt webbläsaren
        except Exception as e:  # noqa: BLE001 – skickas vidare till väntande submit
            self._fail_pending(tasks, e)

    def _launch(self, pw: Playwright) -> BrowserContext:
        return pw.chromium.launch_persistent_context(
            str(self.paths.browser), headless=self.headless
        )

    def _open_page(
        self, pw: Playwright, context: BrowserContext | None
    ) -> tuple[BrowserContext, Page]:
        if context is None:
            context = self._launch(pw)
        try:
            return context, context.new_page()
        except PlaywrightError:
            # Användaren har troligen stängt webbläsarfönstret: starta om kontexten.
            try:
                context.close()
            except PlaywrightError:
                pass
            context = self._launch(pw)
            return context, context.new_page()

    def _fill(self, page: Page, url: str, applicant: Applicant) -> FillReport:
        page.goto(url, timeout=NAVIGATION_TIMEOUT_MS)
        html = page.content()
        for adapter in self.adapters:
            if adapter.matches(url, html):
                return adapter.fill(page, applicant)
        raise SubmitError("Ingen formuläradapter passar sidan")

    def _fail_pending(self, tasks: queue.Queue[_Task | object], error: Exception) -> None:
        """Tråden dör: nästa submit startar en ny, väntande uppdrag får felet."""
        with self._lock:
            if self._tasks is tasks:
                self._thread = self._tasks = None
            while True:
                try:
                    item = tasks.get_nowait()
                except queue.Empty:
                    break
                if item is not _STOP:
                    _, _, future = item  # type: ignore[misc]
                    if future.set_running_or_notify_cancel():
                        future.set_exception(error)
