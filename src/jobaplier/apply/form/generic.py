"""Formuläradaptrar och den generella adaptern som fyller i okända formulär.

Adaptrarna fyller bara i fält. De klickar aldrig på något, trycker aldrig Enter,
skickar aldrig formuläret och rör aldrig samtyckes-/villkorsrutor.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from jobaplier.apply.form.fields import Applicant, classify_field

FIELD_ATTR = "data-jobaplier-field"
UNANSWERED_ATTR = "data-jobaplier-unanswered"
UNANSWERED_SELECTOR = f"[{UNANSWERED_ATTR}]"
CONSENT_RE = re.compile(r"samtycke|villkor|consent|terms|gdpr|integritet")
OUTLINE_CSS = "outline: 3px solid #e11d48 !important;"
TEXT_KINDS = {"text", "email", "tel", "url", "search", "textarea"}
FILL_TIMEOUT_MS = 5_000
NO_FIELDS_NOTE = "Inga formulärfält hittades – fyll i manuellt"

# Synliga, aktiverade input/textarea/select (ej hidden/submit/button/image/reset).
# Varje element märks med ett stabilt id i FIELD_ATTR så att Python kan hitta det.
_DISCOVER_JS = """
(attr) => {
  const SKIP = new Set(["hidden", "submit", "button", "image", "reset"]);
  const clean = (s) => (s || "").replace(/\\s+/g, " ").trim();
  const labelText = (el) => {
    const parts = [];
    for (const label of el.labels || []) {
      const copy = label.cloneNode(true);
      copy.querySelectorAll("input, select, textarea, button").forEach((n) => n.remove());
      parts.push(copy.textContent);
    }
    return clean(parts.join(" "));
  };
  const radioGroup = (el) =>
    el.name
      ? [...document.getElementsByName(el.name)].filter(
          (r) => r.type === "radio" && r.form === el.form)
      : [el];
  const isRequired = (el) => el.required || el.getAttribute("aria-required") === "true";
  window.__jobaplierSeq = window.__jobaplierSeq || 0;
  const out = [];
  for (const el of document.querySelectorAll("input, textarea, select")) {
    if (el.tagName === "INPUT" && SKIP.has(el.type)) continue;
    if (el.disabled) continue;
    if (!el.checkVisibility({ checkVisibilityCSS: true, visibilityProperty: true })) continue;
    let label = labelText(el);
    let required = isRequired(el);
    let empty;
    if (el.type === "checkbox") {
      empty = !el.checked;
    } else if (el.type === "radio") {
      const group = radioGroup(el);
      empty = !group.some((r) => r.checked);
      required = group.some(isRequired);
      const legend = el.closest("fieldset")?.querySelector("legend");
      label = legend ? clean(legend.textContent) : el.name || label;
    } else if (el.type === "file") {
      empty = el.files.length === 0;
    } else {
      empty = el.value.trim() === "";
    }
    if (!el.hasAttribute(attr)) el.setAttribute(attr, String(++window.__jobaplierSeq));
    out.push({
      key: el.getAttribute(attr),
      kind: el.type,
      label,
      name: el.getAttribute("name") || "",
      id: el.id || "",
      placeholder: el.getAttribute("placeholder") || "",
      ariaLabel: el.getAttribute("aria-label") || "",
      required,
      empty,
      readonly: el.readOnly === true,
    });
  }
  return out;
}
"""

_MARK_JS = """
({ attr, mark, keys }) => {
  for (const key of keys) {
    document.querySelector(`[${attr}="${CSS.escape(key)}"]`)?.setAttribute(mark, "");
  }
}
"""

# Reserv om sidans CSP blockerar <style>: CSSOM-ändringar blockeras inte av CSP.
# Försvar på djupet mot sidans egna hanterare (t.ex. onchange="this.form.submit()")
# medan fälten fylls: blockera submit-händelser och gör submit()/requestSubmit()
# till no-ops. Återställs efteråt så att användaren kan skicka själv.
_GUARD_JS = """
() => {
  if (window.__jobaplierGuard) {
    window.__jobaplierGuard.depth += 1;
    return;
  }
  const proto = HTMLFormElement.prototype;
  const guard = {
    submit: proto.submit,
    requestSubmit: proto.requestSubmit,
    depth: 1,
    listener: (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
    },
  };
  proto.submit = function () {};
  proto.requestSubmit = function () {};
  document.addEventListener("submit", guard.listener, true);
  window.__jobaplierGuard = guard;
}
"""

_UNGUARD_JS = """
() => {
  const guard = window.__jobaplierGuard;
  if (!guard) return;
  guard.depth -= 1;
  if (guard.depth > 0) return;
  const proto = HTMLFormElement.prototype;
  proto.submit = guard.submit;
  proto.requestSubmit = guard.requestSubmit;
  document.removeEventListener("submit", guard.listener, true);
  delete window.__jobaplierGuard;
}
"""

_INLINE_OUTLINE_JS = """
(selector) => {
  for (const el of document.querySelectorAll(selector)) {
    el.style.setProperty("outline", "3px solid #e11d48", "important");
  }
}
"""


@contextmanager
def submit_guard(page: Page) -> Iterator[None]:
    """Blockerar sidans egna submit-försök medan fält fylls i. Kan nästlas."""
    page.evaluate(_GUARD_JS)
    try:
        yield
    finally:
        try:
            page.evaluate(_UNGUARD_JS)
        except PlaywrightError:
            pass  # sidan har navigerat bort; skydden försvann med den


@dataclass
class FillReport:
    filled: list[str] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)


class FormAdapter(Protocol):
    name: str

    def matches(self, url: str, html: str) -> bool: ...

    def fill(self, page: Page, applicant: Applicant) -> FillReport: ...


def highlight(page: Page, selectors: list[str]) -> None:
    """Ger elementen en röd ram via injicerad CSS."""
    if not selectors:
        return
    selector = ", ".join(selectors)
    try:
        page.add_style_tag(content=f"{selector} {{ {OUTLINE_CSS} }}")
    except PlaywrightError:
        page.evaluate(_INLINE_OUTLINE_JS, selector)


def _clean(text: str) -> str:
    """Normaliserar blanksteg och tar bort avslutande '*'/':' (markering för obligatoriskt)."""
    return re.sub(r"[\s*:]+$", "", " ".join(text.split()))


@dataclass
class _Field:
    key: str
    kind: str
    label: str
    parts: list[str]
    required: bool
    empty: bool
    readonly: bool

    @classmethod
    def from_js(cls, raw: dict[str, Any]) -> _Field:
        label = _clean(raw["label"])
        parts = [
            p
            for p in (label, raw["name"], raw["id"], _clean(raw["placeholder"]),
                      _clean(raw["ariaLabel"]))
            if p
        ]
        return cls(
            key=raw["key"],
            kind=raw["kind"],
            label=label
            or _clean(raw["ariaLabel"])
            or _clean(raw["placeholder"])
            or raw["name"]
            or raw["id"],
            parts=parts,
            required=raw["required"],
            empty=raw["empty"],
            readonly=raw["readonly"],
        )

    @property
    def descriptor(self) -> str:
        return " ".join(self.parts)

    @property
    def classification(self) -> str | None:
        # Hela beskrivningen först; sedan varje del för sig så att förankrade
        # mönster som "^namn$" kan träffa en ensam etikett.
        return classify_field(self.descriptor) or next(
            (c for p in self.parts if (c := classify_field(p))), None
        )

    @property
    def consent(self) -> bool:
        return self.kind == "checkbox" and CONSENT_RE.search(self.descriptor.casefold()) is not None


def _discover(page: Page) -> list[_Field]:
    return [_Field.from_js(raw) for raw in page.evaluate(_DISCOVER_JS, FIELD_ATTR)]


def _value_for(f: _Field, applicant: Applicant) -> str | Path | None:
    kind = f.classification
    if f.kind == "file":
        return {"cv": applicant.cv_path, "letter": applicant.letter_pdf}.get(kind or "")
    if f.kind in TEXT_KINDS:
        values = {
            "first_name": applicant.first_name,
            "last_name": applicant.last_name,
            "full_name": applicant.full_name,
            "email": applicant.email,
            "phone": applicant.phone,
            "linkedin": applicant.linkedin_url,
            "letter": applicant.letter_text,
        }
        return values.get(kind or "") or None
    return None  # select, kryssrutor, radioknappar m.m. väljs aldrig automatiskt


class GenericAdapter:
    name = "generic"

    def matches(self, url: str, html: str) -> bool:
        return True

    def fill(self, page: Page, applicant: Applicant) -> FillReport:
        with submit_guard(page):
            fields = _discover(page)
            if not fields:
                return FillReport(unanswered=[NO_FIELDS_NOTE])
            filled = self._fill_fields(page, fields, applicant)
        return FillReport(filled=filled, unanswered=self._mark_unanswered(page))

    def _fill_fields(
        self, page: Page, fields: list[_Field], applicant: Applicant
    ) -> list[str]:
        filled: list[str] = []
        for f in fields:
            if f.consent or f.readonly or not f.empty:
                continue
            value = _value_for(f, applicant)
            if value is None:
                continue
            locator = page.locator(f'[{FIELD_ATTR}="{f.key}"]')
            try:
                if f.kind == "file":
                    locator.set_input_files(value, timeout=FILL_TIMEOUT_MS)
                else:
                    locator.fill(str(value), timeout=FILL_TIMEOUT_MS)
            except PlaywrightError:
                continue  # fältet blir kvar tomt och rapporteras om det behövs
            filled.append(f.label)
        return filled

    def _mark_unanswered(self, page: Page) -> list[str]:
        keys: list[str] = []
        unanswered: list[str] = []
        for f in _discover(page):
            if f.consent or (f.empty and (f.required or f.classification is None)):
                keys.append(f.key)
                if f.label not in unanswered:
                    unanswered.append(f.label)
        if keys:
            page.evaluate(
                _MARK_JS, {"attr": FIELD_ATTR, "mark": UNANSWERED_ATTR, "keys": keys}
            )
            highlight(page, [UNANSWERED_SELECTOR])
        return unanswered
