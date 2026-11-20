"""
Full end-to-end vehicle coverage scraper — IndusInd Smartzone.

Flow:
  1.  Launch Playwright browser
  2.  Login via LoginPage (CAPTCHA entered manually in the browser)
  3.  Navigate: Motor icon → Two Wheeler (top-navbar dropdown)
  4.  Fill Vehicle Details form (Model/Variant autocomplete, RTO, Mfg Year/Month, IDV …)
  5.  Click "Get Coverage Details" → Cover Details section appears
  6.  Scrape + configure Cover Details (OD Package, Liability, AddOns)
  7.  Click "Calculate Premium" → scrape premium values
  8.  Fill Customer Details (Name, DOB, PAN, Mobile, Gender …)
  9.  Fill Communication + Permanent Address
  10. Click "Validate Customer"
  11. Scrape final Premium Summary (IDV, OD, TP, GST, Net, Total, Proposal No)
  12. Save everything to PostgreSQL

All form values are dynamic — read from API request params or .env fallbacks.
Dates default to today, year/month default to current date when not provided.

Usage (standalone):
    python -m scrapers.vehicle_scraper

Usage (via API):  set SCRAPER_CALLABLE=scrapers.vehicle_scraper:scrape_vehicle_coverage
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from datetime import date, datetime

from dotenv import load_dotenv
from playwright.sync_api import Page, sync_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()  # ensure .env is loaded when running standalone

from config.settings import Settings
from db.postgres_client import ensure_table, save_coverage
from pages.login_page import LoginPage
from pages.vehicle_details_page import (
    AddressInput,
    CoverageDetails,
    CoverDetailsInput,
    CustomerInput,
    VehicleFormInput,
)
from utils.logger import configure_logger


SCREENSHOTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _snap(page: Page, label: str, logger: logging.Logger) -> None:
    """Save a full-page screenshot for debugging."""
    ts = datetime.now().strftime("%H%M%S")
    path = SCREENSHOTS_DIR / f"{label}-{ts}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        logger.info("Screenshot saved: %s", path.name)
    except Exception as exc:
        logger.warning("Could not save screenshot %s: %s", path.name, exc)


def _discover_inputs(page: Page, logger: logging.Logger) -> dict:
    """Dump all visible input/select/textarea IDs from the page for debugging."""
    try:
        elements = page.evaluate("""() => {
            const all = [];
            document.querySelectorAll('input, select, textarea, button').forEach(el => {
                all.push({
                    tag: el.tagName,
                    id: el.id || '',
                    name: el.name || '',
                    type: el.type || '',
                    value: el.value || '',
                    placeholder: el.placeholder || '',
                    visible: el.offsetParent !== null,
                    optionCount: el.tagName === 'SELECT' ? el.options.length : 0,
                    selectedText: el.tagName === 'SELECT' && el.selectedIndex >= 0
                        ? (el.options[el.selectedIndex]?.text || '') : ''
                });
            });
            return all;
        }""")
        logger.info("Form elements discovered: %d total", len(elements))
        for el in elements:
            if el.get("visible") and el.get("id"):
                logger.info(
                    "  [%s] id=%s name=%s type=%s value=%s",
                    el["tag"], el["id"], el["name"], el["type"], el["value"]
                )
            # Also log hidden selects (Kendo wraps them)
            if el["tag"] == "SELECT" and el.get("id") and not el.get("visible"):
                logger.info(
                    "  [HIDDEN SELECT] id=%s name=%s options=%d selected='%s'",
                    el["id"], el["name"], el.get("optionCount", 0),
                    el.get("selectedText", "")
                )
        return {el["id"]: el for el in elements if el.get("id")}
    except Exception as exc:
        logger.warning("Could not discover inputs: %s", exc)
        return {}


def _text_of(page: Page, *selectors: str) -> str | None:
    """Return inner text of the first visible matching element."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() and el.is_visible():
                t = el.inner_text().strip()
                if t:
                    return t
        except Exception:
            continue
    return None


def _get_field_value(page: Page, *selectors: str) -> str | None:
    """Return value or inner text of the first matching element (handles input, select, and text)."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if not el.count():
                continue
            val = el.evaluate("""e => {
                if (!e) return '';
                if (e.tagName === 'SELECT') {
                    return e.options && e.selectedIndex >= 0 ? e.options[e.selectedIndex].text.trim() : (e.value || '');
                }
                if (e.tagName === 'INPUT' || e.tagName === 'TEXTAREA') {
                    return (e.value || '').trim();
                }
                return (e.innerText || e.textContent || '').trim();
            }""")
            if val:
                return str(val).strip()
        except Exception:
            continue
    return None


def _fill(page: Page, logger: logging.Logger, label: str, value: str, *selectors: str) -> bool:
    """Fill matching element, with automatic fallback for Kendo UI / styled / hidden elements."""
    if value is None or str(value).strip() == "":
        return False
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if not el.count():
                continue
            # 1. Try standard Playwright interaction if element is visible
            if el.is_visible():
                tag = el.evaluate("e => e.tagName")
                itype = el.evaluate("e => e.type") if tag == "INPUT" else ""
                if tag == "SELECT":
                    try:
                        el.select_option(label=str(value))
                    except Exception:
                        el.select_option(value=str(value))
                elif itype in ("radio", "checkbox"):
                    el.check()
                else:
                    el.fill(str(value))
                logger.info("Filled '%s' = '%s' via %s", label, value, sel)
                return True

            # 2. JS fallback for elements wrapped/hidden by Kendo UI or CSS
            filled = page.evaluate("""({sel, val}) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                if (!val || !val.trim()) return false;
                if (el.tagName === 'SELECT') {
                    for (let i = 0; i < el.options.length; i++) {
                        const opt = el.options[i];
                        if (opt.text.toLowerCase().includes(val.toLowerCase()) || opt.value === val) {
                            el.selectedIndex = i;
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            if (window.$) {
                                $(el).trigger('change');
                                if ($(el).data('kendoDropDownList')) {
                                    $(el).data('kendoDropDownList').value(el.value);
                                    $(el).data('kendoDropDownList').trigger('change');
                                }
                            }
                            return true;
                        }
                    }
                } else if (el.type === 'radio' || el.type === 'checkbox') {
                    el.checked = true;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('click', { bubbles: true }));
                    if (window.$) $(el).trigger('change').trigger('click');
                    return true;
                } else {
                    el.value = val;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    if (window.$) {
                        $(el).trigger('input').trigger('change');
                        if ($(el).data('kendoAutoComplete')) {
                            $(el).data('kendoAutoComplete').value(val);
                        }
                    }
                    return true;
                }
                return false;
            }""", {"sel": sel, "val": str(value)})

            if filled:
                logger.info("Filled '%s' = '%s' via JS fallback %s", label, value, sel)
                return True
        except Exception:
            continue
    logger.warning("Could not fill '%s' with any selector: %s", label, list(selectors))
    return False


def _select_kendo_dropdown(page: Page, logger: logging.Logger, label: str, value: str, *id_hints: str) -> bool:
    """Select a Kendo-enhanced dropdown by finding the hidden <select> via id/name
    substring hints, then using the Kendo widget API to pick the option.

    ``id_hints`` are plain keyword fragments matched against the select element's
    id and name (e.g. "MfgYear", "FuelType", "Title").  They are **not** CSS
    selectors.
    """
    if not value:
        return False

    selected = page.evaluate("""({value, hints}) => {
        const needle = String(value).trim().toLowerCase();
        const hintLower = hints.map(h => h.toLowerCase());
        const selects = Array.from(document.querySelectorAll('select'));

        for (const select of selects) {
            const identity = [select.id, select.name, select.className]
                .filter(Boolean).join(' ').toLowerCase();

            // Check if any hint matches the select's identity
            const hintMatch = hintLower.length === 0 || hintLower.some(h => identity.includes(h));
            if (!hintMatch) continue;

            const options = Array.from(select.options);
            const option = options.find(o =>
                o.text.trim().toLowerCase() === needle ||
                o.text.trim().toLowerCase().includes(needle) ||
                String(o.value).trim().toLowerCase() === needle
            );
            if (!option) continue;

            // Set native value
            select.value = option.value;
            select.dispatchEvent(new Event('input', {bubbles: true}));
            select.dispatchEvent(new Event('change', {bubbles: true}));

            // Kendo widget sync
            if (window.jQuery) {
                const $sel = window.jQuery(select);
                $sel.trigger('change');
                const kw = $sel.data('kendoDropDownList');
                if (kw) {
                    kw.value(option.value);
                    kw.trigger('change');
                }
            }
            return {id: select.id, text: option.text};
        }
        return null;
    }""", {"value": str(value), "hints": list(id_hints)})

    if selected:
        logger.info("Filled '%s' = '%s' via Kendo dropdown (select#%s → '%s')",
                     label, value, selected.get("id", "?"), selected.get("text", "?"))
        return True
    return False


def _select_kendo_by_row_label(page: Page, logger: logging.Logger, label: str, value: str, *row_terms: str) -> bool:
    """Find a Kendo dropdown by row/label text and set it via pure DOM (no Kendo API)."""
    if not value:
        return False

    selected = page.evaluate("""({value, terms}) => {
        const needle = String(value).trim().toLowerCase();
        const termLower = terms.map(t => t.toLowerCase());

        for (const select of Array.from(document.querySelectorAll('select'))) {
            const identity = [select.id, select.name, select.className]
                .filter(Boolean).join(' ').toLowerCase();
            const row = select.closest('tr') || select.closest('div') || select.parentElement;
            const rowText = (row ? row.innerText : '').toLowerCase();
            const labelText = Array.from(document.querySelectorAll('label'))
                .filter(l => l.htmlFor === select.id)
                .map(l => l.innerText).join(' ').toLowerCase();

            const match = termLower.some(t =>
                identity.includes(t) || rowText.includes(t) || labelText.includes(t)
            );
            if (!match) continue;

            const opts = Array.from(select.options);
            const idx = opts.findIndex(o => o.text.trim().toLowerCase() === needle)
                     ?? opts.findIndex(o => o.text.trim().toLowerCase().includes(needle))
                     ?? opts.findIndex(o => o.value.toLowerCase() === needle);
            const option = idx >= 0 ? opts[idx] : null;
            if (!option) continue;

            // Pure DOM set — NO Kendo API, NO jQuery events (avoids AJAX)
            select.selectedIndex = idx;
            select.value = option.value;
            select.dispatchEvent(new Event('change', {bubbles: false}));
            return {id: select.id, text: option.text};
        }
        return null;
    }""", {"value": value, "terms": list(row_terms)})

    if selected:
        logger.info("Set '%s' = '%s' via row-label pure DOM (select#%s)",
                     label, selected.get("text", value), selected.get("id", "?"))
        return True
    logger.warning("Could not set '%s' = '%s' via row-label search", label, value)
    return False

def _select_dropdown_option(page: Page, logger: logging.Logger, label: str, value: str, *hints: str) -> bool:
    """Select a dropdown option by text, including Kendo-enhanced or unknown-ID controls.

    ``hints`` are keyword fragments matched against select id/name/className —
    they are NOT CSS selectors.
    """
    if not value:
        return False

    # Strip CSS selector syntax from hints so they work as plain id-substring matches
    clean_hints = []
    for h in hints:
        # e.g. "select[id*='MfgYear']" → "MfgYear", "#ddlFuelType" → "ddlFuelType"
        import re as _re
        m = _re.search(r"['\"]([^'\"]+)['\"]", h)
        if m:
            clean_hints.append(m.group(1))
        elif h.startswith("#"):
            clean_hints.append(h.lstrip("#"))
        else:
            clean_hints.append(h)

    return _select_kendo_dropdown(page, logger, label, value, *clean_hints)


def _select_by_surrounding_label(page: Page, logger: logging.Logger, label: str, value: str, *terms: str) -> bool:
    """Select a native dropdown using visible label/row text when its ID is unknown.
    Delegates to the robust Kendo-aware row-label search."""
    return _select_kendo_by_row_label(page, logger, label, value, label, *terms)


def _verify_mandatory_vehicle_fields(page: Page, vehicle: VehicleFormInput, logger: logging.Logger) -> None:
    """Verify key vehicle fields and retry business-critical dropdowns before submission.
    Checks the visible Kendo text first; only re-clicks if still showing placeholder."""

    def _retry_if_blank(field_label: str, listbox_hint: str, value: str, upper: bool = False) -> None:
        """Re-select a Kendo dropdown only when it still shows the placeholder."""
        current = page.evaluate("""({hint}) => {
            const sp = document.querySelector('[aria-owns="' + hint + '"]');
            if (!sp) return '';
            const inp = sp.querySelector('.k-input, span.k-input');
            return inp ? (inp.innerText || inp.textContent || '').trim() : '';
        }""", {"hint": f"{listbox_hint}_listbox"})
        needle = value.strip().lower()
        if current and current.lower() not in ("select month", "select year", "select", "select type", "") \
                and needle in current.lower():
            logger.info("'%s' already shows '%s' — skipping verify.", field_label, current)
            return
        v = value.upper() if upper else value
        logger.info("Re-selecting '%s' = '%s' (currently '%s')", field_label, v, current)
        # For month: wait for items to exist first (listbox repopulated by AJAX)
        if upper:
            for _ in range(10):
                count = page.evaluate("""({lid}) => {
                    const ul = document.getElementById(lid);
                    return ul ? ul.querySelectorAll('li.k-item').length : 0;
                }""", {"lid": f"{listbox_hint}_listbox"})
                if count > 1:
                    break
                page.wait_for_timeout(500)
        _click_kendo_dropdown(page, logger, field_label, v, listbox_hint.lower())

    _retry_if_blank("manufacturing_year",  "Manufacturing_YearVehicle",  vehicle.manufacturing_year)
    _retry_if_blank("manufacturing_month", "Manufacturing_MonthVehicle", vehicle.manufacturing_month, upper=True)
    _retry_if_blank("fuel_type",           "TypeOfFuelVehicle",          vehicle.fuel_type)

    missing: list[str] = []
    checks = {
        "make_model_variant": ("#VehicleDetailsMakeModel", "input[name*='Make_Model']"),
        "rto_location": ("#RTOCityLocation", "input[name*='Rto_State_City']"),
        "policy_start_date": ("#PolicyStart_Date",),
        "purchase_date": ("#Date_PurchaseVehicle",),
    }
    for field_name, selectors in checks.items():
        value = _get_field_value(page, *selectors)
        expected = getattr(vehicle, field_name if field_name != "purchase_date" else "purchase_registration_date", "")
        if expected and not value:
            missing.append(field_name)
    if missing:
        logger.warning("Vehicle fields still empty after fill attempt: %s", ", ".join(missing))
    else:
        logger.info("Mandatory vehicle fields verification completed.")


def _check_visible_radio(page: Page, logger: logging.Logger, label: str,
                         name_part: str, option_text: str) -> bool:
    """Tick the visible radio (name contains ``name_part``) whose label reads ``option_text``."""
    if not option_text:
        return False
    try:
        radios = page.locator(f"input[type='radio'][name*='{name_part}']")
        visible = [radios.nth(i) for i in range(radios.count()) if radios.nth(i).is_visible()]
        wanted = option_text.strip().lower()
        for radio in visible:
            text = radio.evaluate("""r => {
                const own = r.labels && r.labels.length ? r.labels[0].innerText : '';
                const next = r.nextSibling ? (r.nextSibling.textContent || '') : '';
                const nextEl = r.nextElementSibling ? r.nextElementSibling.innerText : '';
                return (own || next.trim() || nextEl || '').trim();
            }""").lower()
            if text == wanted or (len(visible) == 1 and wanted in (text or wanted)):
                radio.check(timeout=3000)
                logger.info("Selected '%s' = '%s' via visible radio (value=%s).",
                            label, option_text, radio.get_attribute("value"))
                return True
        logger.warning("'%s': no visible radio labelled '%s' (%d visible).", label, option_text, len(visible))
    except Exception as exc:
        logger.warning("'%s': could not tick radio '%s': %s", label, option_text, exc)
    return False


def _log_site_messages(page: Page, logger: logging.Logger, stage: str, watch_ms: int = 3000) -> list[str]:
    """Log the validation messages the site shows in its red message band."""
    seen: list[str] = []
    for _ in range(max(1, watch_ms // 300)):
        try:
            text = page.evaluate("""() => {
                const box = document.getElementById('containerMain');
                return box && box.getClientRects().length ? (box.innerText || '').trim() : '';
            }""")
        except Exception:
            text = ""
        for line in (ln.strip() for ln in text.splitlines()):
            if line and line not in seen:
                seen.append(line)
                logger.warning("Site message (%s): %s", stage, line)
        page.wait_for_timeout(300)
    if not seen:
        logger.info("No site message shown (%s).", stage)
    return seen


class SiteRejectedQuote(RuntimeError):
    """The insurer's site refused to price the quote (a business rule, not a scraper fault)."""


def _site_error_lines(page: Page) -> list[str]:
    """Business errors the site prints on the page, e.g.
    'No Discount available for mentioned Details. Please Contact Corporate Motor Underwriter ErrorCode - …'."""
    try:
        lines = page.evaluate("""() => {
            const band = document.getElementById('containerMain');
            const text = (document.body.innerText || '') + '\\n' +
                (band && band.getClientRects().length ? band.innerText || '' : '');
            return text.split('\\n').map(l => l.trim())
                .filter(l => /ErrorCode|Contact Corporate|Underwriter/i.test(l));
        }""")
    except Exception:
        return []
    unique: list[str] = []
    for line in lines:
        # The same error is repeated with a different reference after "ErrorCode".
        key = re.split(r"ErrorCode", line, flags=re.IGNORECASE)[0].strip()
        if all(re.split(r"ErrorCode", seen, flags=re.IGNORECASE)[0].strip() != key for seen in unique):
            unique.append(line)
    return unique[:5]


_MONEY_RE = re.compile(r"^\s*(?:₹|Rs\.?|INR)?\s*-?\d[\d,]*(?:\.\d+)?\s*$", re.IGNORECASE)


def _money_or_none(value: str | None) -> str | None:
    """Keep a scraped premium only if it is actually an amount (drops 'No', 'NaN', labels …)."""
    if value is None:
        return None
    text = str(value).strip()
    return text if _MONEY_RE.match(text) else None


def _log_month_diagnostics(page: Page, logger: logging.Logger, stage: str) -> None:
    """Log why the Manufacturing Month may have been reset: its current value,
    any message the site is showing, and the site's own month-change handler."""
    try:
        info = page.evaluate("""() => {
            const $ = window.jQuery;
            const el = document.getElementById('Manufacturing_MonthVehicle');
            const widget = el && $ ? $(el).data('kendoDropDownList') : null;
            const shown = document.querySelector('[aria-owns="Manufacturing_MonthVehicle_listbox"] .k-input');
            const visible = e => {
                const r = e.getBoundingClientRect();
                const s = getComputedStyle(e);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            };
            // Floating layers (popups, message bands, dialogs) currently on screen.
            const overlays = [];
            for (const e of document.querySelectorAll('body *')) {
                const s = getComputedStyle(e);
                if (!['fixed', 'absolute'].includes(s.position)) continue;
                if ((parseInt(s.zIndex, 10) || 0) < 100 || !visible(e)) continue;
                if (e.closest('.k-animation-container, .k-list-container')) continue;
                overlays.push({
                    id: e.id, cls: String(e.className).slice(0, 80),
                    text: (e.innerText || '').trim().slice(0, 400),
                    html: e.outerHTML.slice(0, 500),
                });
                if (overlays.length >= 6) break;
            }
            const src = n => typeof window[n] === 'function' ? window[n].toString().slice(0, 5000) : null;
            return {
                shownText: shown ? shown.innerText.trim() : null,
                widgetText: widget ? widget.text() : null,
                widgetValue: widget ? String(widget.value()) : null,
                inputValue: el ? el.value : null,
                makeModel: (document.getElementById('VehicleDetailsMakeModel') || {}).value || '',
                cc: (document.getElementById('VehicleDetails_CubicCapacity') || {}).value || '',
                purchaseDate: (document.getElementById('Date_PurchaseVehicle') || {}).value || '',
                overlays,
                monthHandler: src('onchangeofMonthQuote'),
                modelHandler: src('checkMakeModel'),
            };
        }""")
    except Exception as exc:
        logger.warning("Month diagnostics failed (%s): %s", stage, exc)
        return

    logger.info(
        "Month diagnostics (%s): shown='%s' widget_text='%s' widget_value='%s' input='%s' | "
        "model='%s' cc='%s' purchase_date='%s'",
        stage, info["shownText"], info["widgetText"], info["widgetValue"], info["inputValue"],
        info["makeModel"], info["cc"], info["purchaseDate"],
    )
    for overlay in info["overlays"]:
        logger.info("  Message layer on page: id=%s class=%s text=%r html=%s",
                    overlay["id"], overlay["cls"], overlay["text"], overlay["html"])
    if not getattr(_log_month_diagnostics, "_handlers_logged", False):
        _log_month_diagnostics._handlers_logged = True
        logger.info("Site handler onchangeofMonthQuote: %s", info["monthHandler"])
        logger.info("Site handler checkMakeModel: %s", info["modelHandler"])


def _select_business_type(page: Page, logger: logging.Logger, business_type: str) -> bool:
    """Select New/Rollover/Renewal business radio, including hidden markup."""
    if not business_type:
        return False

    mapping = {"new": "2", "rollover": "5", "renewal": "6"}
    needle = business_type.strip().lower()
    value = mapping.get(needle, "")

    try:
        if value:
            radio = page.locator(f"input[name='objPolicy.BusinessTypeId'][value='{value}']").first
            if radio.count():
                radio.check(force=True)
                logger.info("Selected business type '%s' via radio value %s.", business_type, value)
                return True

        selected = page.evaluate("""({needle}) => {
            const radios = Array.from(document.querySelectorAll("input[type='radio']"));
            for (const radio of radios) {
                const text = (
                    (radio.closest('label')?.innerText || '') + ' ' +
                    (radio.parentElement?.innerText || '') + ' ' +
                    (radio.labels ? Array.from(radio.labels).map(l => l.innerText).join(' ') : '')
                ).toLowerCase();
                if (text.includes(needle)) {
                    radio.checked = true;
                    radio.dispatchEvent(new Event('change', {bubbles: true}));
                    radio.dispatchEvent(new Event('click', {bubbles: true}));
                    if (window.jQuery) window.jQuery(radio).trigger('change').trigger('click');
                    return true;
                }
            }
            return false;
        }""", {"needle": needle})
        if selected:
            logger.info("Selected business type '%s' via visible radio text.", business_type)
            return True
    except Exception as exc:
        logger.debug("Business type selection failed: %s", exc)

    logger.warning("Could not select business type '%s'.", business_type)
    return False


# Finds the LIVE Kendo widget for a field. Switching Sub Product re-renders the
# form but leaves the old popups (e.g. ul#Manufacturing_MonthVehicle_listbox) in
# <body>, so document.getElementById(...) returns a dead list belonging to the
# old form. Going through the widget on the visible input always reaches the
# list that the current form actually reads.
_KENDO_FIND_JS = """
function findKendo(hints, role) {
    const $ = window.jQuery;
    if (!$) return null;
    const hl = hints.map(h => h.toLowerCase());
    let fallback = null;
    for (const el of document.querySelectorAll('[data-role]')) {
        const owns = (el.id || '').toLowerCase() + '_listbox';
        if (!el.id || !hl.some(h => owns.includes(h))) continue;
        const w = $(el).data(role);
        if (!w) continue;
        const wrap = (w.wrapper && w.wrapper[0]) || el;
        if (wrap.getClientRects().length > 0) return w;
        fallback = fallback || w;
    }
    return fallback;
}
"""


def _kendo_pick(
    page: Page,
    logger: logging.Logger,
    label: str,
    value: str,
    role: str,
    hints: tuple[str, ...],
    *,
    first_if_no_match: bool = False,
    wait_ms: int = 8000,
) -> str | None:
    """Pick an item in the live Kendo widget and return its text (None if not possible).

    ``role`` is 'kendoDropDownList' (opens the list) or 'kendoAutoComplete'
    (types ``value`` and searches). The item is chosen with a real mouse click so
    the site's own handlers run; dropdowns fall back to the widget API.
    """
    if not value:
        return None
    args = {"hints": [h.lower() for h in hints], "role": role, "text": str(value).strip()}
    is_autocomplete = role == "kendoAutoComplete"
    try:
        opened = page.evaluate("(a) => {" + _KENDO_FIND_JS + """
            const w = findKendo(a.hints, a.role);
            if (!w) return false;
            if (a.role === 'kendoAutoComplete') {
                w.element.removeAttr('readonly').removeAttr('disabled');
                w.element.focus();
                w.element.val(a.text);
                w.search(a.text);
            } else {
                w.open();
            }
            return true;
        }""", args)
        if not opened:
            return None

        # Wait for the live list to contain a usable item.
        index, texts = -1, []
        needle = args["text"].lower()
        for _ in range(max(1, wait_ms // 400)):
            texts = page.evaluate("(a) => {" + _KENDO_FIND_JS + """
                const w = findKendo(a.hints, a.role);
                return w ? w.ul.children('li').toArray().map(li => (li.innerText || li.textContent || '').trim()) : [];
            }""", args)
            lowered = [t.lower() for t in texts]
            if needle in lowered:
                index = lowered.index(needle)
            else:
                index = next((i for i, t in enumerate(lowered) if needle in t), -1)
                if index < 0 and first_if_no_match and is_autocomplete and texts:
                    index = 0
            if index >= 0:
                break
            page.wait_for_timeout(400)
        if index < 0:
            logger.warning("'%s': no option matching '%s' in the live list. Options: %s",
                           label, value, texts[:15])
            page.keyboard.press("Escape")
            return None

        args["index"] = index
        handle = page.evaluate_handle("(a) => {" + _KENDO_FIND_JS + """
            const w = findKendo(a.hints, a.role);
            return w ? w.ul.children('li')[a.index] : null;
        }""", args)
        item = handle.as_element()
        try:
            if item is None:
                raise RuntimeError("item not found")
            item.click(timeout=3000)
        except Exception as exc:
            logger.debug("'%s': mouse click on option failed (%s); using widget API.", label, exc)
            page.evaluate("(a) => {" + _KENDO_FIND_JS + """
                const w = findKendo(a.hints, a.role);
                if (!w) return;
                const li = w.ul.children('li').eq(a.index);
                if (a.role === 'kendoAutoComplete') { li[0].click(); return; }
                w.select(li); w.trigger('change'); w.close();
            }""", args)
        page.wait_for_timeout(500)

        if not is_autocomplete:
            # Confirm the widget really holds the value; force it through the API if not.
            ok = page.evaluate("(a) => {" + _KENDO_FIND_JS + """
                const w = findKendo(a.hints, a.role);
                if (!w) return false;
                const want = w.ul.children('li').eq(a.index);
                const wantText = (want.text() || '').trim();
                if ((w.text() || '').trim() !== wantText) {
                    w.select(want); w.trigger('change'); w.close();
                }
                return (w.text() || '').trim() === wantText;
            }""", args)
            if not ok:
                logger.warning("'%s': the site did not keep '%s'.", label, texts[index])
                return None
        return texts[index]
    except Exception as exc:
        logger.debug("_kendo_pick('%s') error: %s", label, exc)
        return None


def _model_details_loaded(page: Page) -> bool:
    """True when the site has filled CC/Watt or Seating Capacity for the chosen model.

    The site only accepts the model ("Please Choose Make/Model/Variant…") while
    these read-only details are present.
    """
    try:
        return bool(page.evaluate("""() => {
            const ids = ['VehicleDetails_CubicCapacity', 'VehicleDetails_SeatingCapacity'];
            return ids.some(id => Array.from(document.querySelectorAll('[id="' + id + '"]'))
                .some(el => (el.value || '').trim() !== ''));
        }"""))
    except Exception:
        return False


def _wait_for_model_details(page: Page, logger: logging.Logger, wait_ms: int = 10000) -> bool:
    """Wait for the site to load the chosen model's details (CC, seating …)."""
    for _ in range(max(1, wait_ms // 400)):
        if _model_details_loaded(page):
            return True
        page.wait_for_timeout(400)
    logger.warning("The site did not load the model's details (CC/seating) within %d s.", wait_ms // 1000)
    return False


def _pick_model(page: Page, logger: logging.Logger, model: str) -> str | None:
    """Pick the model from the site's suggestion list and wait for its details."""
    picked = _kendo_pick(
        page, logger, "make_model_variant", model,
        "kendoAutoComplete", ("vehicledetailsmakemodel",), first_if_no_match=True,
    )
    if picked:
        logger.info("Make/Model/Variant picked from the site's list: '%s'", picked)
        _wait_for_model_details(page, logger)
        page.wait_for_timeout(500)
    return picked


def _trace_model_details(page: Page, logger: logging.Logger, state: dict, stage: str) -> None:
    """Log the step at which the site wipes the model's details, if it does."""
    loaded = _model_details_loaded(page)
    if state.get("loaded") and not loaded:
        logger.warning("Model details (CC/seating) were cleared by the site %s.", stage)
    state["loaded"] = loaded


def _auto_idv(page: Page, logger: logging.Logger, percent: float = 95.0) -> str:
    """IDV for a new vehicle as a percentage of the ex-showroom price shown on the form."""
    try:
        text = page.evaluate("""() => {
            const m = (document.body.innerText || '').match(/Ex\\s*showroom price[^\\n]*?([\\d,]+(?:\\.\\d+)?)\\s*$/im);
            return m ? m[1] : '';
        }""")
        price = float(text.replace(",", "")) if text else 0.0
    except Exception:
        price = 0.0
    if price <= 0:
        logger.warning("IDV auto: ex-showroom price not shown on the form; leaving IDV blank.")
        return ""
    idv = str(int(round(price * percent / 100.0)))
    logger.info("IDV auto: %s%% of ex-showroom price %s = %s", percent, text, idv)
    return idv


def _click_kendo_dropdown(page: Page, logger: logging.Logger, label: str, value: str, *id_hints: str) -> bool:
    """Select a Kendo dropdown by physically clicking its visible wrapper span
    and then clicking the matching item in the popup list.

    This site uses <span role="listbox" aria-owns="<id>_listbox"> wrappers —
    there are NO hidden <select> elements for Year/Month/Fuel.

    ``id_hints`` — substrings matched (case-insensitive) against the aria-owns
    value, e.g. "manufacturing_monthvehicle" matches
    aria-owns="Manufacturing_MonthVehicle_listbox".
    """
    if not value:
        return False

    needle = str(value).strip().lower()

    # Preferred path: the live widget (immune to stale popups left in <body>).
    picked = _kendo_pick(page, logger, label, value, "kendoDropDownList", tuple(id_hints), wait_ms=4000)
    if picked:
        logger.info("Selected '%s' = '%s' via live Kendo widget (item='%s')", label, value, picked)
        return True

    try:
        # ── 1. Find the wrapper and its listbox id via aria-owns ──────────
        listbox_id = page.evaluate("""({hints}) => {
            const hl = hints.map(h => h.toLowerCase());
            for (const sp of document.querySelectorAll('[aria-owns]')) {
                const owns = (sp.getAttribute('aria-owns') || '').toLowerCase();
                if (hl.some(h => owns.includes(h))) {
                    return sp.getAttribute('aria-owns');
                }
            }
            return null;
        }""", {"hints": list(id_hints)})

        if not listbox_id:
            logger.debug("_click_kendo_dropdown: no aria-owns match for hints=%s", id_hints)
            return False

        # ── 2. Open the popup by clicking the wrapper span ────────────────
        page.evaluate("""({lid}) => {
            const sp = document.querySelector('[aria-owns="' + lid + '"]');
            if (sp) sp.click();
        }""", {"lid": listbox_id})
        page.wait_for_timeout(700)

        # ── 3. Wait until this specific listbox has real items ────────────
        for _ in range(10):
            count = page.evaluate("""({lid}) => {
                const ul = document.getElementById(lid);
                if (!ul) return 0;
                return Array.from(ul.querySelectorAll('li.k-item'))
                    .filter(li => li.offsetParent !== null || ul.style.display !== 'none')
                    .length;
            }""", {"lid": listbox_id})
            if count > 0:
                break
            page.wait_for_timeout(300)

        # ── 4. Click the matching li in THIS listbox via JS ──────────────
        # The Kendo popup <ul> is appended to <body> by Kendo's animation
        # container — it may not be a child of the wrapper span in the DOM.
        # Use JS to find and click it directly by text match.
        clicked = page.evaluate("""({lid, needle}) => {
            const ul = document.getElementById(lid);
            if (!ul) return {ok: false, reason: 'listbox not found'};
            const items = Array.from(ul.querySelectorAll('li.k-item'));
            if (!items.length) return {ok: false, reason: 'no items', count: 0};
            for (const li of items) {
                const t = (li.innerText || li.textContent || '').trim().toLowerCase();
                if (t === needle || t.includes(needle)) {
                    li.click();
                    return {ok: true, text: li.innerText.trim()};
                }
            }
            return {ok: false, reason: 'no match', count: items.length,
                    available: items.slice(0,12).map(l => l.innerText.trim())};
        }""", {"lid": listbox_id, "needle": needle})

        if clicked and clicked.get("ok"):
            page.wait_for_timeout(400)
            logger.info("Selected '%s' = '%s' via Kendo JS click (listbox=%s, item='%s')",
                        label, value, listbox_id, clicked.get("text", value))
            return True

        # JS click failed — close and report
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        reason = clicked.get("reason", "?") if clicked else "evaluate error"
        available = clicked.get("available", []) if clicked else []
        logger.debug("Listbox %s: %s. Available: %s", listbox_id, reason, available)

    except Exception as exc:
        logger.debug("_click_kendo_dropdown('%s') error: %s", label, exc)

    return False


def _get_kendo_current_text(page: Page, *id_hints: str) -> str:
    """Return the currently displayed text of a Kendo dropdown.
    Checks BOTH the hidden <select> value AND the visible Kendo span text."""
    try:
        return page.evaluate("""({hints}) => {
            const hintLower = hints.map(h => h.toLowerCase());
            for (const s of Array.from(document.querySelectorAll('select'))) {
                const identity = [s.id, s.name, s.className].filter(Boolean).join(' ').toLowerCase();
                if (!hintLower.some(h => identity.includes(h))) continue;

                // 1. Try the Kendo widget's displayed text first (most reliable)
                if (window.jQuery) {
                    const kw = window.jQuery(s).data('kendoDropDownList');
                    if (kw) {
                        const t = kw.text ? kw.text() : '';
                        if (t && t.trim()) return t.trim();
                    }
                }

                // 2. Try the visible Kendo wrapper span text
                let el = s.nextElementSibling;
                while (el) {
                    if (el.classList.contains('k-dropdown') || el.classList.contains('k-widget')) {
                        const span = el.querySelector('.k-input, .k-state-default span');
                        if (span && span.innerText.trim()) return span.innerText.trim();
                    }
                    el = el.nextElementSibling;
                }

                // 3. Fall back to native select option text
                if (s.selectedIndex >= 0) {
                    return (s.options[s.selectedIndex]?.text || '').trim();
                }
            }
            return '';
        }""", {"hints": list(id_hints)}) or ""
    except Exception:
        return ""


def _silent_set_select(page: Page, logger: logging.Logger, label: str,
                        value: str, *id_hints: str) -> bool:
    """Set a <select> value with ZERO events — no change, no input, no jQuery, no Kendo.

    This is the only safe approach when a site fires a server AJAX call (with
    CSRF/session validation) on the 'change' event, causing "Authentication Failed"
    full-page redirects.

    The form still picks up the correct value on submit because browsers serialize
    select.value from the DOM — no event is required for that.
    """
    if not value:
        return False

    result = page.evaluate("""({value, hints}) => {
        const needle = value.trim().toLowerCase();
        const hintLower = hints.map(h => h.toLowerCase());

        for (const s of Array.from(document.querySelectorAll('select'))) {
            const identity = [s.id, s.name, s.className].filter(Boolean).join(' ').toLowerCase();
            if (!hintLower.some(h => identity.includes(h))) continue;

            const opts = Array.from(s.options);
            const idx = opts.findIndex(o =>
                o.text.trim().toLowerCase() === needle ||
                o.text.trim().toLowerCase().includes(needle) ||
                o.value.toLowerCase() === needle
            );

            if (idx < 0) {
                return {ok: false, optCount: opts.length, selectId: s.id,
                        options: opts.map(o => o.text.trim())};
            }

            // ── Completely silent set — absolutely no events ──────────────
            s.selectedIndex = idx;
            s.value = opts[idx].value;

            // Update Kendo's visible display span directly in the DOM
            // so the UI shows the correct text, without calling any Kendo methods
            let sib = s.nextElementSibling;
            while (sib) {
                const inp = sib.querySelector('.k-input') ||
                            sib.querySelector('[class*="k-state"] span') ||
                            sib.querySelector('span[aria-label]');
                if (inp) { inp.textContent = opts[idx].text; break; }
                if (sib.classList.contains('k-dropdown') || sib.classList.contains('k-widget')) {
                    const any = sib.querySelector('span');
                    if (any) { any.textContent = opts[idx].text; break; }
                }
                sib = sib.nextElementSibling;
            }

            return {ok: true, text: opts[idx].text, selectId: s.id};
        }
        return {ok: false, optCount: -1, selectId: null, options: []};
    }""", {"value": value, "hints": list(id_hints)})

    if result and result.get("ok"):
        logger.info("Silent-set '%s' = '%s' (no events, no AJAX) on select#%s",
                     label, result.get("text", value), result.get("selectId", "?"))
        return True

    opts_available = result.get("options", []) if result else []
    logger.warning("Silent-set '%s' = '%s' failed — options in DOM: %s",
                    label, value, opts_available[:12])
    return False


def _safe_set_kendo_dropdown(page: Page, logger: logging.Logger, label: str,
                              value: str, id_hints: tuple, row_hints: tuple) -> bool:
    """Set a Kendo dropdown value WITHOUT triggering any AJAX/DataSource calls.

    Strategy:
      1. Skip entirely if the displayed value already matches (auto-filled by Make/Model).
      2. Set the native <select> selectedIndex via pure JS.
      3. Update the Kendo widget's visible display span directly in the DOM
         (avoids calling kw.select() / kw.value() which fire DataSource events).
      4. Only dispatch a minimal synthetic 'change' on the native select so the
         form picks up the new value on submit — NOT on the Kendo widget itself.
    """
    if not value:
        return False

    needle = value.strip().lower()

    # ── 1. Check displayed text — skip if already correct ─────────────────
    current = _get_kendo_current_text(page, *id_hints).lower()
    if current and current not in ("select month", "select year", "select", "") \
            and (current == needle or needle in current):
        logger.info("'%s' already shows '%s' — skipping.", label, current)
        return False   # no change made; caller skips AJAX wait

    # ── 2. Pure DOM set — zero Kendo API, zero AJAX ────────────────────────
    result = page.evaluate("""({value, hints}) => {
        const needle = value.trim().toLowerCase();
        const hintLower = hints.map(h => h.toLowerCase());

        for (const s of Array.from(document.querySelectorAll('select'))) {
            const identity = [s.id, s.name, s.className].filter(Boolean).join(' ').toLowerCase();
            if (!hintLower.some(h => identity.includes(h))) continue;

            const opts = Array.from(s.options);
            const idx = opts.findIndex(o =>
                o.text.trim().toLowerCase() === needle ||
                o.text.trim().toLowerCase().includes(needle) ||
                o.value.toLowerCase() === needle
            );

            if (idx < 0) {
                return {found: false, optCount: opts.length, selectId: s.id,
                        availableOptions: opts.map(o => o.text.trim())};
            }

            // Set native select value directly (no events yet)
            s.selectedIndex = idx;
            s.value = opts[idx].value;

            // Update the Kendo widget's visible display span WITHOUT calling
            // any Kendo methods (which would fire DataSource.read / AJAX)
            if (window.jQuery) {
                const wrapper = window.jQuery(s).closest('.k-widget').addBack()
                    .next('.k-widget, .k-dropdown');
                // Try to update the visible text span directly
                const span = document.querySelector(
                    'span.k-input, ' +
                    '[aria-owns="' + s.id + '_listbox"] .k-input'
                );
                if (!span) {
                    // Try sibling Kendo wrapper
                    let sib = s.nextElementSibling;
                    while (sib) {
                        const inp = sib.querySelector('.k-input, span[aria-label]');
                        if (inp) { inp.textContent = opts[idx].text; break; }
                        sib = sib.nextElementSibling;
                    }
                } else {
                    span.textContent = opts[idx].text;
                }
            }

            // Sync the Kendo widget's internal state so the visible span updates.
            // kw.value() sets the selection without re-fetching the DataSource,
            // which is safe (no AJAX) when the options are already loaded.
            if (window.jQuery) {
                const kw = window.jQuery(s).data('kendoDropDownList');
                if (kw) {
                    kw.value(opts[idx].value);
                    // Do NOT call kw.trigger('change') — that fires DataSource.read
                }
            }

            // Also fire ONE minimal native 'change' so the form model reads the value
            s.dispatchEvent(new Event('change', {bubbles: false}));

            return {found: true, text: opts[idx].text, selectId: s.id};
        }
        return {found: false, optCount: -1, selectId: null, availableOptions: []};
    }""", {"value": value, "hints": list(id_hints)})

    if result and result.get("found"):
        logger.info("Set '%s' = '%s' via pure DOM (no Kendo AJAX) on select#%s",
                     label, result.get("text", value), result.get("selectId", "?"))
        return True

    # Log what options ARE available to help debug
    opt_count = result.get("optCount", -1) if result else -1
    available = result.get("availableOptions", []) if result else []
    logger.warning("Could not set '%s' = '%s' — %d options in select: %s",
                    label, value, opt_count, available[:10])

    # ── 3. Last resort: row-label scan (also pure DOM) ────────────────────
    return _select_kendo_by_row_label(page, logger, label, value, *row_hints)


def _select_vehicle_dropdown(page: Page, logger: logging.Logger, label: str, value: str) -> bool:
    """Select a vehicle Kendo dropdown safely — skips if already correct,
    uses index-based API to avoid authentication-triggering DataSource refresh."""
    if not value:
        return False

    id_hint_map = {
        "fuel_type":           ("FuelType", "fueltype", "ddlFuelType", "VehicleDetails_Fuel", "Fuel"),
        "manufacturing_year":  ("MfgYear", "ManufYear", "ManufacturingYear"),
        "manufacturing_month": ("MfgMonth", "ManufMonth", "ManufacturingMonth"),
    }
    row_hint_map = {
        "fuel_type":           ("fuel",),
        "manufacturing_year":  ("manufacturing", "year", "mfg"),
        "manufacturing_month": ("month", "manufacturing"),
    }
    id_hints  = id_hint_map.get(label,  (label,))
    row_hints = row_hint_map.get(label, (label,))

    return _safe_set_kendo_dropdown(page, logger, label, value, id_hints, row_hints)


# ---------------------------------------------------------------------------
# Environment → input builders (with dynamic defaults and dictionary overrides)
# ---------------------------------------------------------------------------

def _build_vehicle_input(overrides: dict | None = None) -> VehicleFormInput:
    o = overrides or {}
    is_new = o.get("is_new_vehicle", os.getenv("VEH_IS_NEW_VEHICLE", "true"))
    if isinstance(is_new, str):
        is_new = is_new.lower() in ("true", "1", "yes")

    vehicle = VehicleFormInput(
        registration_number=str(o.get("registration_number", os.getenv("VEH_REG_NUMBER", ""))),
        rto_location=str(o.get("rto_location", os.getenv("VEH_RTO_LOCATION", ""))),
        manufacturing_year=str(o.get("manufacturing_year", os.getenv("VEH_MFG_YEAR", ""))),
        manufacturing_month=str(o.get("manufacturing_month", os.getenv("VEH_MFG_MONTH", ""))),
        purchase_registration_date=str(o.get("purchase_registration_date", o.get("purchase_date", os.getenv("VEH_PURCHASE_DATE", "")))),
        policy_start_date=str(o.get("policy_start_date", os.getenv("VEH_POLICY_START_DATE", ""))),
        make_model_variant=str(o.get("make_model_variant", os.getenv("VEH_MAKE_MODEL_VARIANT", ""))),
        idv=str(o.get("idv", os.getenv("VEH_IDV", ""))),
        fuel_type=str(o.get("fuel_type", os.getenv("VEH_FUEL_TYPE", "Petrol"))),
        business_type=str(o.get("business_type", os.getenv("VEH_BUSINESS_TYPE", "New"))),
        customer_type=str(o.get("customer_type", os.getenv("VEH_CUSTOMER_TYPE", "Individual"))),
        ncb_eligibility=str(o.get("ncb_eligibility", o.get("ncb", os.getenv("VEH_NCB", "None")))),
        data_discount_percent=int(o.get("data_discount_percent", o.get("discount", os.getenv("VEH_DISCOUNT", "0")))),
        is_new_vehicle=bool(is_new),
    )
    # Apply dynamic defaults for any blank date/year/month fields
    vehicle.apply_dynamic_defaults()
    return vehicle


def _build_cover_details_input(overrides: dict | None = None) -> CoverDetailsInput:
    """Build Cover Details options from environment/overrides with sensible defaults."""
    o = overrides or {}
    nil_dep = o.get("nil_depreciation", os.getenv("COVER_NIL_DEPRECIATION", "true"))
    if isinstance(nil_dep, str):
        nil_dep = nil_dep.lower() in ("true", "1", "yes")

    pa_owner = o.get("pa_to_owner_driver", os.getenv("COVER_PA_OWNER_DRIVER", "true"))
    if isinstance(pa_owner, str):
        pa_owner = pa_owner.lower() in ("true", "1", "yes")

    return CoverDetailsInput(
        nil_depreciation=bool(nil_dep),
        pa_to_owner_driver=bool(pa_owner),
    )


def _build_customer_input(overrides: dict | None = None) -> CustomerInput:
    o = overrides or {}
    return CustomerInput(
        first_name=str(o.get("first_name", os.getenv("CUST_FIRST_NAME", ""))),
        middle_name=str(o.get("middle_name", os.getenv("CUST_MIDDLE_NAME", ""))),
        last_name=str(o.get("last_name", os.getenv("CUST_LAST_NAME", ""))),
        dob=str(o.get("dob", os.getenv("CUST_DOB", ""))),
        pan=str(o.get("pan", os.getenv("CUST_PAN", ""))),
        gender=str(o.get("gender", os.getenv("CUST_GENDER", "Male"))),
        occupation=str(o.get("occupation", os.getenv("CUST_OCCUPATION", "Others"))),
        marital_status=str(o.get("marital_status", os.getenv("CUST_MARITAL_STATUS", "Single"))),
        mobile=str(o.get("mobile", os.getenv("CUST_MOBILE", ""))),
        email=str(o.get("email", os.getenv("CUST_EMAIL", ""))),
        aadhar=str(o.get("aadhar", os.getenv("CUST_AADHAR", ""))),
        nationality=str(o.get("nationality", os.getenv("CUST_NATIONALITY", "Indian"))),
        registered_under_gst=str(o.get("registered_under_gst", os.getenv("CUST_GST", "No"))),
        related_party=str(o.get("related_party", os.getenv("CUST_RELATED_PARTY", "No"))),
    )


def _build_address_input(overrides: dict | None = None) -> AddressInput:
    o = overrides or {}
    mobile = str(o.get("mobile", os.getenv("CUST_MOBILE", "")))
    email = str(o.get("email", os.getenv("CUST_EMAIL", "")))
    same_as_comm = o.get("same_as_comm", os.getenv("CUST_PERM_SAME_AS_COMM", "true"))
    if isinstance(same_as_comm, str):
        same_as_comm = same_as_comm.lower() in ("true", "1", "yes")

    return AddressInput(
        flat=str(o.get("flat", os.getenv("CUST_FLAT", ""))),
        building=str(o.get("building", os.getenv("CUST_BUILDING", ""))),
        street=str(o.get("street", os.getenv("CUST_STREET", ""))),
        landmark=str(o.get("landmark", os.getenv("CUST_LANDMARK", ""))),
        pincode=str(o.get("pincode", os.getenv("CUST_PINCODE", ""))),
        state=str(o.get("state", os.getenv("CUST_STATE", ""))),
        city=str(o.get("city", os.getenv("CUST_CITY", ""))),
        mobile=mobile,
        email=email,
        same_as_comm=bool(same_as_comm),
    )


# ---------------------------------------------------------------------------
# Step 1b — Navigate to Two Wheeler form via Motor icon dropdown
# ---------------------------------------------------------------------------

def _navigate_to_tw_form(page: Page, logger: logging.Logger, settings: Settings) -> bool:
    """
    Navigate to the Two-Wheeler Vehicle Details form.

    Priority:
    1. VEHICLE_DETAILS_URL env-var — instant, zero extra reloads.
    2. Motor navbar icon → hover → click "Two Wheeler" dropdown item.
    3. Dashboard link scan — read <a> hrefs already on page.
    4. Direct URL list — last resort (causes error page reloads).
    """
    base = settings.base_url.rstrip("/")

    # ── 1. Env-var shortcut ──────────────────────────────────────────────
    shortcut = os.getenv("VEHICLE_DETAILS_URL", "").strip()
    if shortcut:
        logger.info("Using VEHICLE_DETAILS_URL shortcut: %s", shortcut)
        try:
            page.goto(shortcut, wait_until="domcontentloaded",
                      timeout=settings.navigation_timeout_ms)
            page.wait_for_timeout(1500)
            if _is_form_page(page):
                logger.info("Shortcut URL loaded the form page.")
                return True
            logger.warning("Shortcut URL loaded but form not detected; continuing.")
        except Exception as exc:
            logger.debug("Shortcut URL failed: %s", exc)

    # ── 2. Motor icon → Two Wheeler dropdown (preferred, 0 extra reloads) ─
    logger.info("Trying Motor navbar icon → Two Wheeler dropdown.")
    try:
        # The Motor icon in the top-right icon bar
        motor_link = page.locator(
            "a:has-text('Motor'), "
            "li:has-text('Motor') > a, "
            ".navbar a:has-text('Motor'), "
            "td:has-text('Motor') a, "
            "[title='Motor'], img[alt*='Motor']"
        ).first
        if motor_link.count() and motor_link.is_visible():
            motor_link.hover()
            page.wait_for_timeout(700)
            # "Two Wheeler" appears in the dropdown
            tw = page.locator(
                "a:has-text('Two Wheeler'), li:has-text('Two Wheeler') > a, "
                ".dropdown-menu a:has-text('Two Wheeler'), "
                "ul a:has-text('Two Wheeler')"
            ).first
            if tw.count() and tw.is_visible():
                logger.info("Clicking 'Two Wheeler' from Motor dropdown.")
                tw.click()
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(2000)
                _snap(page, "after-motor-menu-click", logger)
                if _is_form_page(page):
                    logger.info("Motor → Two Wheeler navigation succeeded. URL: %s", page.url)
                    _log_tip(page.url, logger)
                    return True
                logger.warning("Clicked Two Wheeler but form page not detected; URL=%s", page.url)
    except Exception as exc:
        logger.debug("Motor dropdown navigation failed: %s", exc)

    # ── 3. Dashboard link scan (no extra reload) ─────────────────────────
    logger.info("Scanning dashboard for Two-Wheeler link.")
    try:
        links = page.evaluate("""() =>
            Array.from(document.querySelectorAll('a'))
                .map(a => ({text: a.innerText.trim(), href: a.href}))
                .filter(a => a.href && (
                    a.text.toLowerCase().includes('two') ||
                    a.text.toLowerCase() === 'tw' ||
                    a.href.toLowerCase().includes('partialquote') ||
                    a.href.toLowerCase().includes('twpolicy')
                ))
        """)
        logger.info("Found %d TW candidate link(s).", len(links))
        for link in links:
            href = link.get("href", "")
            text = link.get("text", "")
            if not href:
                continue
            # Prioritise exact "Two Wheeler" text
            if "two" not in text.lower() and "tw" not in text.lower():
                continue
            try:
                page.goto(href, wait_until="domcontentloaded",
                          timeout=settings.navigation_timeout_ms)
                page.wait_for_timeout(1500)
                if _is_form_page(page):
                    logger.info("Dashboard scan found working TW link: %s", href)
                    _log_tip(href, logger)
                    return True
            except Exception:
                continue
        # Second pass: any PartialQuote / motor link
        for link in links:
            href = link.get("href", "")
            if not href:
                continue
            try:
                page.goto(href, wait_until="domcontentloaded",
                          timeout=settings.navigation_timeout_ms)
                page.wait_for_timeout(1500)
                if _is_form_page(page):
                    logger.info("Dashboard scan (pass 2) found form: %s", href)
                    _log_tip(href, logger)
                    return True
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Dashboard link scan failed: %s", exc)

    # ── 4. Direct URL list — last resort ─────────────────────────────────
    for url in [
        f"{base}/IMDForms/TW/TWPolicyEntry.aspx",
        f"{base}/IMDForms/Motor/TW/NewPolicy.aspx",
        f"{base}/Motor/TW/PolicyEntry.aspx",
        f"{base}/NewBusiness/TW/PolicyEntry.aspx",
    ]:
        try:
            logger.info("Trying direct URL (last resort): %s", url)
            page.goto(url, wait_until="domcontentloaded",
                      timeout=settings.navigation_timeout_ms)
            page.wait_for_timeout(1500)
            if _is_form_page(page):
                logger.info("Direct URL worked: %s", url)
                return True
            logger.warning("URL %s returned error page.", url)
        except Exception as exc:
            logger.debug("URL %s failed: %s", url, exc)

    _snap(page, "navigation-failed", logger)
    logger.error("Could not reach the Vehicle Details form via any strategy.")
    return False


def _is_form_page(page: Page) -> bool:
    """Return True when the Vehicle Details form is visible."""
    try:
        return (
            "Some Error" not in page.content()[:500]
            and "Error has Occurred" not in page.content()[:500]
            and page.locator(
                "#VehicleDetailsMakeModel, "
                "input[name*='FastlaneRegistration'], "
                "#Registration_Number1Vehicle1, "
                "input[id*='BusinessType']"
            ).first.count() > 0
        )
    except Exception:
        return False


def _log_tip(url: str, logger: logging.Logger) -> None:
    logger.info(
        "TIP: Set VEHICLE_DETAILS_URL=%s in your .env to skip scanning next run.", url
    )


# ---------------------------------------------------------------------------
# Step 2 — Fill Vehicle Details form
# ---------------------------------------------------------------------------

def _ensure_new_vehicle_subproduct(page: Page, logger: logging.Logger) -> bool:
    """Ensure the Sub Product dropdown is set to 'Two Wheeler Package for 5 years (Only New Veh.)'."""
    logger.info("Checking Sub Product dropdown ...")
    try:
        # Check current subproduct text on page
        current_text = page.evaluate("""() => {
            const kd = Array.from(document.querySelectorAll('.k-dropdown, select')).find(el =>
                (el.innerText || el.value || '').toLowerCase().includes('two wheeler')
            );
            return kd ? (kd.innerText || kd.value || '').trim() : '';
        }""")
        logger.info("Current subproduct text: '%s'", current_text)
        if "only new veh" in current_text.lower() or "5 year" in current_text.lower():
            logger.info("Already on 'Two Wheeler Package for 5 years (Only New Veh.)'.")
            return True

        # Need to switch to "Two Wheeler Package for 5 years (Only New Veh.)"
        logger.info("Switching Sub Product to 'Two Wheeler Package for 5 years (Only New Veh.)' ...")

        switched = False
        # Method 1: Try clicking Kendo UI dropdown widget
        try:
            kd_widget = page.locator(".k-dropdown:has-text('Two Wheeler'), .k-dropdown:near(:text('Select Sub Product'))").first
            if kd_widget.count() and kd_widget.is_visible():
                kd_widget.click()
                page.wait_for_timeout(800)
                opt_locator = page.locator(
                    "ul[role='listbox'] li:has-text('5 years'), "
                    "ul[role='listbox'] li:has-text('Only New Veh'), "
                    ".k-animation-container li:has-text('5 years'), "
                    ".k-animation-container li:has-text('Only New Veh'), "
                    "li.k-item:has-text('5 years'), "
                    "li.k-item:has-text('Only New Veh')"
                ).first
                if opt_locator.count() and opt_locator.is_visible():
                    opt_locator.click()
                    switched = True
                    logger.info("Selected 'Two Wheeler Package for 5 years (Only New Veh.)' via Kendo dropdown UI.")
        except Exception as e:
            logger.debug("Kendo UI dropdown click attempt: %s", e)

        # Method 2: JS / Kendo API / select fallback
        if not switched:
            switched = page.evaluate("""() => {
                const selects = Array.from(document.querySelectorAll('select'));
                for (const s of selects) {
                    for (let i = 0; i < s.options.length; i++) {
                        const t = s.options[i].text.toLowerCase();
                        if (t.includes('5 year') || t.includes('only new veh') || (t.includes('new') && t.includes('package'))) {
                            s.selectedIndex = i;
                            s.dispatchEvent(new Event('change', { bubbles: true }));
                            if (window.$) {
                                $(s).trigger('change');
                                if ($(s).data('kendoDropDownList')) {
                                    $(s).data('kendoDropDownList').select(i);
                                    $(s).data('kendoDropDownList').trigger('change');
                                }
                            }
                            if (typeof s.onchange === 'function') {
                                s.onchange();
                            }
                            return true;
                        }
                    }
                }
                return false;
            }""")
            if switched:
                logger.info("Selected 'Two Wheeler Package for 5 years' via JS/Kendo API.")

        if switched:
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            _snap(page, "after-subproduct-switch", logger)
            logger.info("Form updated for New Vehicle (5 years). URL: %s", page.url)
            return True
        else:
            logger.warning("Could not find or switch Sub Product dropdown.")
            return False
    except Exception as exc:
        logger.warning("Error ensuring new vehicle subproduct: %s", exc)
        return False


def _fill_vehicle_details(
    page: Page,
    vehicle: VehicleFormInput,
    logger: logging.Logger,
) -> None:
    """Fill all fields on the Vehicle Details form and click Get Coverage Details."""
    _snap(page, "form-before-fill", logger)
    _discover_inputs(page, logger)

    # Ensure New Vehicle (5 years) subproduct is active if requested
    if vehicle.is_new_vehicle or vehicle.business_type.lower() == "new":
        _ensure_new_vehicle_subproduct(page, logger)
        # Re-discover inputs after subproduct switch (form reloads with new fields)
        _discover_inputs(page, logger)

    # ── Business Type ────────────────────────────────────────────────────
    logger.info("Setting business type to '%s'.", vehicle.business_type)
    # BusinessTypeId value=2 → New, value=5 → Rollover, value=6 → Renewal
    if not _select_business_type(page, logger, vehicle.business_type):
        _fill(page, logger, "business_type",
              vehicle.business_type,
              "input[name='objPolicy.BusinessTypeId'][value='2']",
              f"input[value='{vehicle.business_type}'][type='radio']",
              f"label:has-text('{vehicle.business_type}') input[type='radio']")
    page.wait_for_timeout(400)

    # ── Make / Model / Variant (autocomplete) ────────────────────────────
    model_picked = None
    model_trace: dict = {}
    if vehicle.make_model_variant:
        model_picked = _pick_model(page, logger, vehicle.make_model_variant)
        _trace_model_details(page, logger, model_trace, "right after the model was picked")

    if vehicle.make_model_variant and not model_picked:
        logger.info("Filling Make/Model/Variant: '%s'", vehicle.make_model_variant)
        try:
            # Trigger the Kendo autocomplete via JS (input is hidden to Playwright)
            page.evaluate("""({val}) => {
                const inp = document.querySelector(
                    '#VehicleDetailsMakeModel, input[name="Make_Model_Variant_CC"]'
                );
                if (!inp) return;
                inp.removeAttribute('disabled');
                inp.focus();
                inp.value = val;
                ['input', 'keyup'].forEach(ev =>
                    inp.dispatchEvent(new Event(ev, {bubbles: true}))
                );
                if (window.jQuery) {
                    const kac = window.jQuery(inp).data('kendoAutoComplete');
                    if (kac) kac.search(val);
                }
            }""", {"val": vehicle.make_model_variant})

            # Wait for the listbox to have at least one item
            listbox_appeared = False
            for _ in range(16):  # up to ~8s
                count = page.evaluate("""() => {
                    const ul = document.getElementById('VehicleDetailsMakeModel_listbox');
                    return ul ? ul.querySelectorAll('li.k-item').length : 0;
                }""")
                if count > 0:
                    listbox_appeared = True
                    break
                page.wait_for_timeout(500)

            if listbox_appeared:
                # Click the first suggestion via JS (bypasses visibility checks)
                clicked = page.evaluate("""({val}) => {
                    const ul = document.getElementById('VehicleDetailsMakeModel_listbox');
                    if (!ul) return false;
                    const items = ul.querySelectorAll('li.k-item');
                    if (!items.length) return false;
                    // Find best match, else take first
                    const needle = val.toLowerCase();
                    let best = items[0];
                    for (const li of items) {
                        if ((li.innerText || '').toLowerCase().includes(needle)) {
                            best = li; break;
                        }
                    }
                    best.click();
                    return (best.innerText || '').trim();
                }""", {"val": vehicle.make_model_variant})
                page.wait_for_timeout(800)
                logger.info("Autocomplete suggestion clicked: %s", clicked or vehicle.make_model_variant)
            else:
                logger.warning("Make/Model listbox never appeared — trying Kendo select() API")
                page.evaluate("""({val}) => {
                    const inp = document.querySelector('#VehicleDetailsMakeModel');
                    if (!inp || !window.jQuery) return;
                    const kac = window.jQuery(inp).data('kendoAutoComplete');
                    if (kac) { kac.value(val); inp.value = val;
                        inp.dispatchEvent(new Event('change', {bubbles:true})); }
                }""", {"val": vehicle.make_model_variant})
                page.wait_for_timeout(600)
        except Exception as exc:
            logger.warning("Could not fill Make/Model/Variant: %s", exc)

        # ── Wait for AJAX-populated Kendo dropdowns to appear in DOM ─────
        # After Make/Model selection the site fires an AJAX call that injects
        # the Year, Fuel Type and (after year selection) Month <select> elements.
        # Without this wait all three report "-1 options in select: []".
        logger.info("Waiting for year/fuel Kendo listboxes to appear after Make/Model AJAX...")
        for _attempt in range(20):   # up to ~10 s
            year_ready = page.evaluate("""() => {
                const ul = document.getElementById('Manufacturing_YearVehicle_listbox');
                return ul ? ul.querySelectorAll('li.k-item').length > 1 : false;
            }""")
            if year_ready:
                logger.info("Year listbox is ready after %d poll(s).", _attempt + 1)
                break
            page.wait_for_timeout(500)
        else:
            logger.warning("Year listbox never appeared after Make/Model AJAX — proceeding anyway.")
        page.wait_for_timeout(300)

    # ── New Vehicle checkbox ──────────────────────────────────────────────
    if vehicle.is_new_vehicle:
        try:
            nv_chk = page.locator("#IsNewVehicle, input[id*='IsNew'], input[id*='NewVehicle']").first
            if nv_chk.count() and nv_chk.is_visible():
                nv_chk.check()
                logger.info("Checked 'New Vehicle' checkbox.")
                page.wait_for_timeout(400)
        except Exception as exc:
            logger.debug("Could not check New Vehicle: %s", exc)
    _trace_model_details(page, logger, model_trace, "after ticking 'New Vehicle'")

    # ── Fuel Type ────────────────────────────────────────────────────────
    # Use Kendo UI click so the widget's internal state updates properly.
    fuel_selected = _click_kendo_dropdown(
        page, logger, "fuel_type", vehicle.fuel_type,
        "typeoffuelvehicle",   # aria-owns="TypeOfFuelVehicle_listbox"
    )
    if not fuel_selected:
        logger.warning("Kendo click failed for fuel_type — falling back to _select_vehicle_dropdown")
        _select_vehicle_dropdown(page, logger, "fuel_type", vehicle.fuel_type)
    page.wait_for_timeout(300)
    _trace_model_details(page, logger, model_trace, "after selecting the fuel type")

    # ── Registration Number (4 split boxes) ──────────────────────────────
    reg = vehicle.registration_number.upper().replace("-", "").replace(" ", "")
    reg_boxes = page.locator(
        "input[id^='Registration_Number'], input[name*='FastlaneRegistration']"
    ).all()
    logger.info("Found %d registration box(es).", len(reg_boxes))

    if len(reg_boxes) >= 2:
        m = re.match(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{1,4})$", reg)
        if m:
            parts = list(m.groups())
        elif vehicle.is_new_vehicle:
            # New vehicle: RTO code + empty boxes e.g. "TN51" → TN 51 blank blank
            m2 = re.match(r"^([A-Z]{2})(\d{1,2})$", reg)
            parts = list(m2.groups()) + ["", ""] if m2 else [reg, "", "", ""]
        else:
            parts = [reg, "", "", ""]
        for box, part in zip(reg_boxes, parts):
            if part:
                try:
                    box.fill(part)
                    page.wait_for_timeout(100)
                except Exception:
                    pass
        logger.info("Registration filled in %d boxes: %s", len(reg_boxes), parts)
    elif reg:
        _fill(page, logger, "registration_number", vehicle.registration_number,
          "input[id^='Registration_Number']",
          "input[id*='RegNo']", "input[name*='RegNo']",
          "input[placeholder*='Reg']")

    page.wait_for_timeout(300)
    _trace_model_details(page, logger, model_trace, "after filling the registration number")

    # ── RTO Location (autocomplete) ───────────────────────────────────────
    rto_picked = None
    if vehicle.rto_location:
        rto_picked = _kendo_pick(
            page, logger, "rto_location", vehicle.rto_location,
            "kendoAutoComplete", ("rtocitylocation",), first_if_no_match=True,
        )
        if rto_picked:
            logger.info("RTO location picked from the site's list: '%s'", rto_picked)
            page.wait_for_timeout(600)

    if vehicle.rto_location and not rto_picked:
        logger.info("Filling RTO location: '%s'", vehicle.rto_location)
        try:
            rto_el = page.locator("input#RTOCityLocation, input[name='Rto_State_City']").first
            rto_el.wait_for(state="attached", timeout=5000)
            rto_already = rto_el.evaluate("e => e.value || ''")
            if rto_already.strip():
                logger.info("RTO already filled: '%s'", rto_already.strip())
            else:
                # Inject via JS (Kendo autocomplete input is hidden to Playwright)
                page.evaluate("""({val}) => {
                    const inp = document.querySelector(
                        '#RTOCityLocation, input[name="Rto_State_City"]'
                    );
                    if (!inp) return;
                    inp.removeAttribute('readonly');
                    inp.focus();
                    inp.value = val;
                    ['input','keyup','change'].forEach(ev =>
                        inp.dispatchEvent(new Event(ev, {bubbles: true}))
                    );
                    if (window.jQuery) {
                        const kac = window.jQuery(inp).data('kendoAutoComplete');
                        if (kac) { kac.value(val); kac.search(val); }
                    }
                }""", {"val": vehicle.rto_location})
                page.wait_for_timeout(1500)
                sug = page.locator(
                    "ul#RTOCityLocation_listbox li.k-item"
                ).first
                if sug.count() and sug.is_visible():
                    sug.click()
                    logger.info("RTO autocomplete suggestion selected.")
                else:
                    page.evaluate("""() => {
                        const inp = document.querySelector('#RTOCityLocation');
                        if (inp) inp.dispatchEvent(new KeyboardEvent('keydown',
                            {bubbles:true, key:'ArrowDown', keyCode:40}));
                    }""")
                    page.wait_for_timeout(300)
                    page.evaluate("""() => {
                        const inp = document.querySelector('#RTOCityLocation');
                        if (inp) inp.dispatchEvent(new KeyboardEvent('keydown',
                            {bubbles:true, key:'Enter', keyCode:13}));
                    }""")
                    logger.info("RTO confirmed via keyboard events.")
                page.wait_for_timeout(600)
        except Exception as exc:
            logger.warning("Could not fill RTO location: %s", exc)

    _trace_model_details(page, logger, model_trace, "after choosing the RTO location")

    # ── Manufacturing Year ────────────────────────────────────────────────
    # Use Kendo UI click so Kendo's own event chain fires, which triggers
    # the AJAX call that populates the Month dropdown options.
    year_selected = _click_kendo_dropdown(
        page, logger, "manufacturing_year", vehicle.manufacturing_year,
        "manufacturing_yearvehicle",   # aria-owns="Manufacturing_YearVehicle_listbox"
    )
    year_changed = year_selected  # used below to decide whether to wait for AJAX
    if not year_selected:
        logger.warning("Kendo click failed for year — falling back to _select_vehicle_dropdown")
        year_changed = _select_vehicle_dropdown(page, logger, "manufacturing_year", vehicle.manufacturing_year)
    if year_changed:
        # Only wait for AJAX if we actually changed the year (new selection)
        logger.info("Manufacturing year changed — waiting for month options to reload...")
        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1500)

    # ── Manufacturing Month ───────────────────────────────────────────────
    # The month listbox (ul#Manufacturing_MonthVehicle_listbox) is populated
    # by AJAX after year selection.  Wait for it to have items (beyond placeholder).
    logger.info("Waiting for month listbox options to load...")
    for _attempt in range(14):   # up to ~7 s
        month_opts = page.evaluate("""() => {
            const ul = document.getElementById('Manufacturing_MonthVehicle_listbox');
            if (!ul) return [];
            return Array.from(ul.querySelectorAll('li.k-item'))
                       .map(li => li.innerText.trim())
                       .filter(t => t && t.toUpperCase() !== 'SELECT MONTH');
        }""")
        if month_opts:
            logger.info("Month options ready: %s", month_opts)
            break
        page.wait_for_timeout(500)
    else:
        logger.warning("Month listbox options never appeared after 7 s.")

    # ── Select the month by physically clicking the Kendo popup ──────────
    # The site stores month options as UPPERCASE (JANUARY, FEBRUARY …).
    # Pass the value in uppercase so the case-insensitive match in
    # _click_kendo_dropdown finds it correctly.
    month_value_upper = vehicle.manufacturing_month.upper()
    month_selected = _click_kendo_dropdown(
        page, logger, "manufacturing_month", month_value_upper,
        "manufacturing_monthvehicle",   # aria-owns="Manufacturing_MonthVehicle_listbox"
    )
    if not month_selected:
        logger.warning("Kendo click failed for month — falling back to _select_vehicle_dropdown")
        _select_vehicle_dropdown(page, logger, "manufacturing_month", vehicle.manufacturing_month)
    page.wait_for_timeout(200)
    _log_month_diagnostics(page, logger, "right after month selection")
    _trace_model_details(page, logger, model_trace, "after selecting the year and month")

    # ── Purchase / Registration Date ──────────────────────────────────────
    _fill(page, logger, "purchase_date", vehicle.purchase_registration_date,
          "#Date_PurchaseVehicle",
          "input[name*='PurchaseDate']", "input[id*='PurchaseDate']",
          "input[placeholder*='Purchase']")
    page.wait_for_timeout(200)

    # ── Registration Date (often same as purchase date for new vehicles) ──
    _fill(page, logger, "registration_date", vehicle.purchase_registration_date,
          "#Date_RegistrationVehicleQuote",
          "input[name*='RegistrationDate']", "input[id*='RegistrationDate']",
          "input[id*='RegDate']", "input[placeholder*='Registration']")
    page.wait_for_timeout(200)

    # ── Customer Type (radio: Individual / Corporate) ───────────────────────
    try:
        cust_radio = page.locator(
            "input#CustomerTypeIndividual, "
            "input[name='objClient.ClientType'][value='True']"
        ).first
        if cust_radio.count():
            cust_radio.check(force=True)
            logger.info("Selected Customer Type = 'Individual' via radio.")
        else:
            # JS fallback
            page.evaluate("""() => {
                const r = document.querySelector("input[id='CustomerTypeIndividual'], input[name*='ClientType'][value='True']");
                if (r) { r.checked = true; r.dispatchEvent(new Event('change',{bubbles:true}));
                    if (window.jQuery) window.jQuery(r).trigger('change').trigger('click'); }
            }""")
            logger.info("Selected Customer Type = 'Individual' via JS fallback.")
    except Exception as exc:
        logger.warning("Could not set Customer Type: %s", exc)
    page.wait_for_timeout(200)

    # ── IDV ───────────────────────────────────────────────────────────────
    # VEH_IDV may be a number, or "auto" / "auto:<percent>" to derive it from the
    # ex-showroom price the site shows (default 95 %, the usual new-vehicle IDV).
    idv_value = (vehicle.idv or "").strip()
    if idv_value.lower().startswith("auto"):
        try:
            percent = float(idv_value.split(":", 1)[1]) if ":" in idv_value else 95.0
        except ValueError:
            percent = 95.0
        idv_value = _auto_idv(page, logger, percent)
    if idv_value:
        _fill(page, logger, "idv", idv_value,
              "#IDVVehicle",
              "input[id*='IDV']", "input[name*='IDV']")
        page.wait_for_timeout(200)
        _log_site_messages(page, logger, "after filling IDV", watch_ms=900)

    # ── Policy Start Date ─────────────────────────────────────────────────
    _fill(page, logger, "policy_start_date", vehicle.policy_start_date,
          "#PolicyStart_Date",
          "input[id*='PolicyStart']", "input[name*='PolicyStart']",
          "input[placeholder*='Policy']")
    page.wait_for_timeout(200)

    # ── Discount ──────────────────────────────────────────────────────────
    if vehicle.data_discount_percent:
        _fill(page, logger, "discount", str(vehicle.data_discount_percent),
              "#Detariff_Discount_Rate",
              "input[id*='Discount']", "input[name*='Discount']")
        page.wait_for_timeout(200)

    # ── NCB ───────────────────────────────────────────────────────────────
    # rdbNcbEligibility value=2 → None
    # The radio's value differs between sub products (2 on the old-vehicle form,
    # 1 on the new-vehicle form), so choose the VISIBLE radio by its label text.
    if not _check_visible_radio(page, logger, "ncb", "NCBEligibility", vehicle.ncb_eligibility):
        _fill(page, logger, "ncb", vehicle.ncb_eligibility,
              "input[name*='NCBEligibility'][value='2']",   # None
              f"input[value='{vehicle.ncb_eligibility}'][type='radio']",
              f"label:has-text('{vehicle.ncb_eligibility}') input[type='radio']",
              "select[id*='NCB']")
    page.wait_for_timeout(200)

    # ── Previous Insurance Details (radio: Yes/No) ────────────────────────
    # For new vehicles, select 'No'
    if vehicle.is_new_vehicle or vehicle.business_type.lower() == "new":
        try:
            prev_no = page.locator(
                "#IdPrevInsuranceDetailsNo, "
                "input[name='objPreviousInsurance.PrevInsAvailable'][value='0']"
            ).first
            if prev_no.count() and prev_no.is_visible():
                prev_no.check(force=True)
                logger.info("Selected Previous Insurance = 'No'.")
                page.wait_for_timeout(300)
        except Exception as exc:
            logger.debug("Could not set Previous Insurance: %s", exc)

    # ── Vehicle Ownership Transfer (radio: No for new vehicles) ───────────
    if vehicle.is_new_vehicle or vehicle.business_type.lower() == "new":
        try:
            transfer_no = page.locator(
                "#rdbVehicleOwnershipTransferInLastPolicyNo, "
                "input[name='objvehicleDetails.IsVehicleOwnershipTransferInLastPolicy'][value='False']"
            ).first
            if transfer_no.count() and transfer_no.is_visible():
                transfer_no.check(force=True)
                logger.info("Selected Vehicle Ownership Transfer = 'No'.")
                page.wait_for_timeout(200)
        except Exception as exc:
            logger.debug("Could not set Vehicle Ownership Transfer: %s", exc)

    _snap(page, "form-after-fill", logger)
    _log_month_diagnostics(page, logger, "after filling the rest of the form")
    _verify_mandatory_vehicle_fields(page, vehicle, logger)

    # The site sometimes wipes the model's details (CC, seating) while the rest
    # of the form is filled, and then rejects the form with "Please Choose
    # Make/Model/Variant". Pick the model again and re-check the dropdowns.
    if vehicle.make_model_variant:
        for attempt in (1, 2):
            if _model_details_loaded(page):
                break
            logger.warning("Model details are empty before submit — picking the model again (attempt %d).", attempt)
            _pick_model(page, logger, vehicle.make_model_variant)
            _verify_mandatory_vehicle_fields(page, vehicle, logger)
        else:
            if not _model_details_loaded(page):
                logger.error("Model details are still empty; the site will reject the form.")

    # ── Ex-showroom price (read-only, scraped before submit) ──────────────
    ex_price = _text_of(page,
        "span[id*='ExShowroom']", "label[id*='ExShowroom']",
        "td:has-text('Ex showroom') + td", ".ex-showroom",
        "a:has-text('Ex showroom')")
    if ex_price:
        logger.info("Ex-showroom price detected: %s", ex_price)

    # ── Click Get Coverage Details ────────────────────────────────────────
    logger.info("Clicking 'Get Coverage Details' button.")
    btn_clicked = False
    for sel in [
        "#btnReady",
        "input[value*='Coverage']",
        "button:has-text('Get Coverage Details')",
        "input[id*='Coverage']",
        "input[type='submit'][value*='Get']",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                btn_clicked = True
                logger.info("Clicked coverage button via: %s", sel)
                break
        except Exception:
            continue

    if not btn_clicked:
        logger.error("Could not find 'Get Coverage Details' button!")
        return

    # If the site rejects the form it says why in its message band.
    _log_site_messages(page, logger, "after Get Coverage Details")

    # Wait for Cover Details section to load (stays on same page)
    logger.info("Waiting for Cover Details section to load …")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    # The section is filled in by a slow server call (10 s or more), so wait for
    # its 'Calculate Premium' button rather than a fixed delay.
    calc_button = page.locator(
        "input[value*='Calculate Premium'], button:has-text('Calculate Premium')"
    )
    cover_ready = False
    for _ in range(90):   # up to ~45 s
        try:
            if any(calc_button.nth(i).is_visible() for i in range(calc_button.count())):
                cover_ready = True
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    if cover_ready:
        logger.info("Cover Details section loaded.")
    else:
        logger.warning("Cover Details section did not load within 45 s.")
        _log_site_messages(page, logger, "Cover Details did not load", watch_ms=600)
    page.wait_for_timeout(1000)

    # The site reports business errors as plain text inside the section.
    for line in _site_error_lines(page):
        logger.warning("Site reported in Cover Details: %s", line)

    # Check if we navigated to a Proposal page or stayed on same page
    current_url = page.url
    logger.info("Post-submit page URL: %s", current_url)
    _snap(page, "after-get-coverage-details", logger)


# ---------------------------------------------------------------------------
# Step 3 — Scrape & Fill Cover Details (OD Package, Liability, AddOns)
# ---------------------------------------------------------------------------

def _scrape_and_fill_cover_details(
    page: Page,
    cover_input: CoverDetailsInput,
    logger: logging.Logger,
) -> dict:
    """
    After 'Get Coverage Details' is clicked, the Cover Details section appears.
    This function:
      1. Scrapes all available cover options from the real website
      2. Configures selected covers (Nil Depreciation, PA Owner Driver, etc.)
      3. Clicks 'Calculate Premium' and scrapes the premium result
      4. Returns a dict of all scraped cover and premium data
    """
    logger.info("--- Scraping & Filling Cover Details ---")
    _expand_section(page, "Cover Details", logger)
    page.wait_for_timeout(1000)
    _snap(page, "cover-details-section", logger)

    scraped = {
        "od_covers": [],
        "liability_covers": [],
        "addon_covers": [],
        "premium_values": {},
    }

    # ── 1. Scrape OD Package options ──────────────────────────────────────
    logger.info("Scraping OD Package options from website...")
    try:
        od_section = page.locator("text=OD Package").first
        if od_section.count():
            # Find all checkboxes in the OD Package column
            od_checkboxes = page.evaluate("""() => {
                const items = [];
                const labels = document.querySelectorAll('input[type="checkbox"]');
                labels.forEach(cb => {
                    const parent = cb.closest('td') || cb.parentElement;
                    const label = parent ? parent.innerText.trim() : '';
                    if (label && cb.id) {
                        items.push({id: cb.id, label: label, checked: cb.checked, name: cb.name || ''});
                    }
                });
                return items;
            }""")
            for item in od_checkboxes:
                label_lower = item["label"].lower()
                # Categorize covers
                if any(k in label_lower for k in ["basic od", "non electrical", "electrical acc",
                       "geo extension", "driving tu", "fibre glass", "voluntary",
                       "anti-theft", "specially designed", "automobile"]):
                    scraped["od_covers"].append({
                        "name": item["label"],
                        "id": item["id"],
                        "checked": item["checked"],
                    })
                elif any(k in label_lower for k in ["tppd", "basic liability", "pa to owner",
                         "pa to named", "pa to unnamed", "pa to paid",
                         "liability to paid", "liability to emp"]):
                    scraped["liability_covers"].append({
                        "name": item["label"],
                        "id": item["id"],
                        "checked": item["checked"],
                    })
                elif any(k in label_lower for k in ["nil depreciation", "electric motor",
                         "battery protection", "charger cover", "hospi cash",
                         "emergency medical", "assistance cover", "key protect",
                         "daily allowance", "emi", "side car"]):
                    scraped["addon_covers"].append({
                        "name": item["label"],
                        "id": item["id"],
                        "checked": item["checked"],
                    })

            logger.info("Scraped %d OD covers, %d liability covers, %d addon covers",
                       len(scraped["od_covers"]),
                       len(scraped["liability_covers"]),
                       len(scraped["addon_covers"]))
    except Exception as exc:
        logger.warning("Could not scrape cover options: %s", exc)

    # ── 2. Configure selected covers ──────────────────────────────────────

    # Nil Depreciation
    if cover_input.nil_depreciation:
        try:
            nil_dep = page.locator(
                "input[type='checkbox'][id*='NilDepreciation'], "
                "input[type='checkbox'][id*='nilDepreciation'], "
                "label:has-text('Nil Depreciation') input[type='checkbox']"
            ).first
            if nil_dep.count() and nil_dep.is_visible():
                if not nil_dep.is_checked():
                    nil_dep.check()
                    logger.info("Checked 'Nil Depreciation' addon cover.")
                    page.wait_for_timeout(500)
                else:
                    logger.info("'Nil Depreciation' already checked.")
        except Exception as exc:
            logger.debug("Could not check Nil Depreciation: %s", exc)

    # PA to Owner Driver
    if cover_input.pa_to_owner_driver:
        try:
            pa_owner = page.locator(
                "input[type='checkbox'][id*='PAOwner'], "
                "input[type='checkbox'][id*='PAtoOwner'], "
                "label:has-text('PA to Owner Driver') input[type='checkbox'], "
                "label:has-text('PA to Owner') input[type='checkbox']"
            ).first
            if pa_owner.count() and pa_owner.is_visible():
                if not pa_owner.is_checked():
                    pa_owner.check()
                    logger.info("Checked 'PA to Owner Driver'.")
                    page.wait_for_timeout(300)
                else:
                    logger.info("'PA to Owner Driver' already checked.")
        except Exception as exc:
            logger.debug("Could not check PA to Owner Driver: %s", exc)

    _snap(page, "cover-details-configured", logger)

    # ── 3. Scrape Nil Depreciation Rate if visible ────────────────────────
    try:
        nil_rate_el = page.locator(
            "input[id*='NilDepreciationRate'], "
            "input[id*='nilDepRate'], "
            "input[name*='NilDepreciation']"
        ).first
        if nil_rate_el.count() and nil_rate_el.is_visible():
            nil_rate = nil_rate_el.input_value()
            if nil_rate:
                scraped["premium_values"]["nil_depreciation_rate"] = nil_rate
                logger.info("Nil Depreciation Rate: %s", nil_rate)
    except Exception:
        pass

    # ── 4. Scrape TPPD Limit ──────────────────────────────────────────────
    try:
        tppd_el = page.locator(
            "select[id*='TPPD'], select[id*='tppd'], "
            "select[name*='TPPD']"
        ).first
        if tppd_el.count() and tppd_el.is_visible():
            tppd_val = tppd_el.input_value()
            if tppd_val:
                scraped["premium_values"]["tppd_limit"] = tppd_val
                logger.info("TPPD Limit: %s", tppd_val)
    except Exception:
        pass

    # ── 5. Scrape CPA Tenure ──────────────────────────────────────────────
    try:
        cpa_el = page.locator(
            "select[id*='CPA'], select[id*='cpaTenure'], "
            "select[name*='CPATenure']"
        ).first
        if cpa_el.count() and cpa_el.is_visible():
            cpa_val = cpa_el.input_value()
            if cpa_val:
                scraped["premium_values"]["cpa_tenure"] = cpa_val
                logger.info("CPA Tenure: %s", cpa_val)
    except Exception:
        pass

    # ── 6. Click Calculate Premium ────────────────────────────────────────
    logger.info("Clicking 'Calculate Premium' button...")
    calc_clicked = False
    for sel in [
        "input[value*='Calculate Premium']",
        "button:has-text('Calculate Premium')",
        "#btnCalculatePremium",
        "input[id*='CalculatePremium']",
        "input[type='submit'][value*='Calculate']",
        "input[type='button'][value*='Calculate']",
        "a:has-text('Calculate Premium')",
        "#btnCalculate",
        "input[value*='Calculate']",
        "button:has-text('Calculate')",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                calc_clicked = True
                logger.info("Clicked Calculate Premium via: %s", sel)
                break
        except Exception:
            continue

    if calc_clicked:
        logger.info("Waiting for premium calculation...")
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(3000)
        _snap(page, "after-calculate-premium", logger)

        # Scrape the premium values that appear after calculation
        try:
            premium_data = page.evaluate("""() => {
                const data = {};
                // Try reading from table rows
                document.querySelectorAll('table tr').forEach(row => {
                    const cells = row.querySelectorAll('td');
                    if (cells.length >= 2) {
                        const label = cells[0].innerText.trim().replace(/:$/, '').trim();
                        const value = cells[cells.length - 1].innerText.trim();
                        if (label && value) data[label] = value;
                    }
                });
                // Try reading from labeled spans/inputs
                document.querySelectorAll('span[id], label[id], input[id]').forEach(el => {
                    const id = el.id.toLowerCase();
                    const val = el.innerText || el.value || '';
                    if (val.trim()) {
                        if (id.includes('premium') || id.includes('idv') || id.includes('od') ||
                            id.includes('tp') || id.includes('gst') || id.includes('total') ||
                            id.includes('net')) {
                            data[el.id] = val.trim();
                        }
                    }
                });
                return data;
            }""")
            scraped["premium_values"].update(premium_data)
            logger.info("Premium values scraped: %d entries", len(premium_data))
            for k, v in premium_data.items():
                logger.info("  %s = %s", k, v)
        except Exception as exc:
            logger.warning("Could not scrape premium values: %s", exc)
    else:
        logger.warning("Could not find 'Calculate Premium' button!")

    # ── 7. Click View Premium Summary if available ────────────────────────
    try:
        view_btn = page.locator(
            "input[value*='View Premium Summary'], "
            "button:has-text('View Premium Summary'), "
            "#btnViewPremiumSummary"
        ).first
        if view_btn.count() and view_btn.is_visible():
            view_btn.click()
            logger.info("Clicked 'View Premium Summary'.")
            page.wait_for_timeout(2000)
            _snap(page, "premium-summary-popup", logger)

            # Scrape from popup/modal if present
            try:
                summary_data = page.evaluate("""() => {
                    const data = {};
                    const modal = document.querySelector('.modal-body, .popup, [id*="Summary"]');
                    const container = modal || document;
                    container.querySelectorAll('table tr').forEach(row => {
                        const cells = row.querySelectorAll('td');
                        if (cells.length >= 2) {
                            const label = cells[0].innerText.trim().replace(/:$/, '').trim();
                            const value = cells[cells.length - 1].innerText.trim();
                            if (label && value) data[label] = value;
                        }
                    });
                    return data;
                }""")
                scraped["premium_values"].update(summary_data)
                logger.info("Premium Summary popup data: %d entries", len(summary_data))
            except Exception:
                pass

            # Close popup
            try:
                close_btn = page.locator(
                    "button.close, .modal-footer button, button:has-text('Close'), "
                    "button:has-text('OK'), .btn-close"
                ).first
                if close_btn.count() and close_btn.is_visible():
                    close_btn.click()
                    page.wait_for_timeout(500)
            except Exception:
                pass
    except Exception:
        pass

    logger.info("Cover Details scraping complete.")
    return scraped


# ---------------------------------------------------------------------------
# Step 4 — Fill Customer Details
# ---------------------------------------------------------------------------

def _fill_customer_details(
    page: Page,
    cust: CustomerInput,
    logger: logging.Logger,
) -> None:
    """Fill the Customer Details accordion section on the Proposal page."""
    logger.info("--- Filling Customer Details ---")

    # Expand section if collapsed
    _expand_section(page, "Customer Details", logger)
    page.wait_for_timeout(500)

    # Discover what's actually on the page at this point
    _discover_inputs(page, logger)

    # Customer Type (Kendo-styled radio checkbox — use JS click to bypass visibility)
    try:
        done = page.evaluate("""() => {
            const r = document.querySelector(
                "input#CustomerTypeIndividual, input[name*='ClientType'][value='True']"
            );
            if (!r) return false;
            r.checked = true;
            r.dispatchEvent(new Event('change', {bubbles: true}));
            r.dispatchEvent(new Event('click',  {bubbles: true}));
            if (window.jQuery) window.jQuery(r).trigger('change').trigger('click');
            return true;
        }""")
        if done:
            logger.info("Selected Customer Type = 'Individual' via JS click.")
        else:
            logger.warning("Customer Type radio not found in DOM.")
    except Exception as exc:
        logger.warning("Could not set Customer Type: %s", exc)
    page.wait_for_timeout(200)

    # Title / Salutation — Kendo dropdown identified by aria-owns
    salutation = "Mr." if cust.gender.lower() == "male" else "Ms."
    if not _click_kendo_dropdown(page, logger, "salutation", salutation,
                                  "ddlobjclienttitle", "clienttitle", "ddltitle"):
        _select_kendo_dropdown(page, logger, "salutation", salutation,
                                "Title", "Salutation", "ddlTitle")
    page.wait_for_timeout(200)

    # First Name
    _fill(page, logger, "first_name", cust.first_name,
          "#txtFirstName", "input[name*='FirstName']",
          "input[id*='FirstName']", "input[placeholder*='First']")
    page.wait_for_timeout(100)

    # Middle Name
    if cust.middle_name:
        _fill(page, logger, "middle_name", cust.middle_name,
              "#txtMiddleName", "input[name*='MiddleName']",
              "input[id*='MiddleName']", "input[placeholder*='Middle']")
        page.wait_for_timeout(100)

    # Last Name
    _fill(page, logger, "last_name", cust.last_name,
          "#txtLastName", "input[name*='LastName']",
          "input[id*='LastName']", "input[placeholder*='Last']")
    page.wait_for_timeout(100)

    # Date of Birth
    if cust.dob:
        _fill(page, logger, "dob", cust.dob,
              "#txtDOB", "input[name*='DOB']",
              "input[id*='DOB']", "input[placeholder*='Birth']",
              "input[placeholder*='DD-MM']")
        page.wait_for_timeout(200)

    # PAN Number
    if cust.pan:
        _fill(page, logger, "pan", cust.pan,
              "#txtPAN", "input[name*='PAN']",
              "input[id*='PAN']", "input[placeholder*='PAN']")
        page.wait_for_timeout(200)

    # Gender (Kendo dropdown)
    if not _click_kendo_dropdown(page, logger, "gender", cust.gender,
                                  "ddlobjclientgender", "clientgender", "ddlgender"):
        if not _select_kendo_dropdown(page, logger, "gender", cust.gender,
                                       "Gender", "ddlGender"):
            _fill(page, logger, "gender", cust.gender,
                  f"input[value='{cust.gender}'][type='radio']",
                  f"label:has-text('{cust.gender}') input[type='radio']")
    page.wait_for_timeout(200)

    # Occupation (Kendo dropdown)
    if not _click_kendo_dropdown(page, logger, "occupation", cust.occupation,
                                  "ddlbundleoccupation", "occupation", "ddloccupation"):
        _select_kendo_dropdown(page, logger, "occupation", cust.occupation,
                                "Occupation", "ddlOccupation")
    page.wait_for_timeout(200)

    # Marital Status (Kendo dropdown)
    if not _click_kendo_dropdown(page, logger, "marital_status", cust.marital_status,
                                  "ddlobjclientmarital", "maritalstatus", "ddlmarital"):
        _select_kendo_dropdown(page, logger, "marital_status", cust.marital_status,
                                "Marital", "ddlMaritalStatus", "MaritalStatus")
    page.wait_for_timeout(200)

    # Nationality (Kendo dropdown)
    if not _click_kendo_dropdown(page, logger, "nationality", cust.nationality,
                                  "ddlobjclientnationality", "nationality", "ddlnationality"):
        _select_kendo_dropdown(page, logger, "nationality", cust.nationality,
                                "Nationality", "ddlNationality")
    page.wait_for_timeout(200)

    # Registered under GST (Kendo dropdown or radio)
    if not _select_kendo_dropdown(page, logger, "gst", cust.registered_under_gst,
                                   "GST", "ddlGST", "RegisteredGST"):
        _fill(page, logger, "gst", cust.registered_under_gst,
              "input[name*='GST'][value='No']",
              f"input[value='{cust.registered_under_gst}'][type='radio']")
    page.wait_for_timeout(200)

    # Aadhar
    if cust.aadhar:
        _fill(page, logger, "aadhar", cust.aadhar,
              "#txtAadhar", "input[id*='Aadhar']",
              "input[name*='Aadhar']", "input[placeholder*='Aadhar']")
        page.wait_for_timeout(200)

    _snap(page, "customer-details-filled", logger)
    logger.info("Customer Details section filled.")


# ---------------------------------------------------------------------------
# Step 4 — Fill Communication + Permanent Address
# ---------------------------------------------------------------------------

def _fill_address(
    page: Page,
    addr: AddressInput,
    logger: logging.Logger,
) -> None:
    """Fill Communication Address (and optionally Permanent Address)."""
    logger.info("--- Filling Communication Address ---")
    _expand_section(page, "Communication Address", logger)
    page.wait_for_timeout(400)

    # Flat / Door No
    _fill(page, logger, "flat", addr.flat,
          "input[id*='Flat'], input[name*='Flat']",
          "input[id*='Door'], input[placeholder*='Flat']",
          "input[placeholder*='Door']")
    page.wait_for_timeout(100)

    # Building
    _fill(page, logger, "building", addr.building,
          "input[id*='Building'], input[name*='Building']",
          "input[id*='Premises'], input[placeholder*='Building']")
    page.wait_for_timeout(100)

    # Street / Area
    _fill(page, logger, "street", addr.street,
          "input[id*='Street'], input[name*='Street']",
          "input[id*='Road'], input[placeholder*='Street']",
          "input[id*='Area']")
    page.wait_for_timeout(100)

    # Landmark
    if addr.landmark:
        _fill(page, logger, "landmark", addr.landmark,
              "input[id*='Landmark'], input[name*='Landmark']",
              "input[placeholder*='Landmark']")
        page.wait_for_timeout(100)

    # Pincode search → triggers State/City auto-fill
    if addr.pincode:
        _fill(page, logger, "pincode_search", addr.pincode,
              "input[id*='PincodeSearch'], input[id*='pincode'], "
              "input[name*='Pincode'], input[placeholder*='Pincode']")
        page.wait_for_timeout(1500)   # wait for State/District/City to auto-fill
        logger.info("Pincode entered; waiting for State/City auto-fill.")

    # State (may auto-fill from pincode)
    if addr.state:
        _fill(page, logger, "state", addr.state,
              "select[id*='State'], select[name*='State']",
              "#ddlState")
        page.wait_for_timeout(400)

    # City
    if addr.city:
        _fill(page, logger, "city", addr.city,
              "select[id*='City'], select[name*='City']",
              "select[id*='Town'], input[id*='City']",
              "#ddlCity, #ddlTown")
        page.wait_for_timeout(200)

    # Mobile
    if addr.mobile:
        _fill(page, logger, "mobile", addr.mobile,
              "input[id*='Mobile'], input[name*='Mobile']",
              "input[type='tel'], input[placeholder*='Mobile']")
        page.wait_for_timeout(100)

    # Email
    if addr.email:
        _fill(page, logger, "email", addr.email,
              "input[id*='Email'], input[name*='Email']",
              "input[type='email'], input[placeholder*='Email']")
        page.wait_for_timeout(100)

    # Permanent Address — same as Communication
    logger.info("--- Filling Permanent Address ---")
    _expand_section(page, "Permanent Address", logger)
    page.wait_for_timeout(300)

    if addr.same_as_comm:
        try:
            same_chk = page.locator(
                "input[id*='SameAsCommunication'], "
                "input[name*='SameAsCommunication'], "
                "input[id*='PermanentSame'], "
                "label:has-text('same as Communication') input[type='checkbox']"
            ).first
            if same_chk.count() and same_chk.is_visible():
                same_chk.check()
                logger.info("Ticked 'Permanent Address same as Communication'.")
                page.wait_for_timeout(400)
        except Exception as exc:
            logger.debug("Could not tick same-as-comm: %s", exc)

    _snap(page, "address-filled", logger)
    logger.info("Address section filled.")


# ---------------------------------------------------------------------------
# Step 5 — Validate Customer
# ---------------------------------------------------------------------------

def _validate_customer(page: Page, logger: logging.Logger) -> None:
    """Click the Validate Customer button and wait for confirmation."""
    logger.info("Clicking 'Validate Customer' button.")
    for sel in [
        "#btnValidateCustomer",
        "button:has-text('Validate Customer')",
        "input[value*='Validate Customer']",
        "a:has-text('Validate Customer')",
        "input[value*='Validate']",
        "button:has-text('Validate')",
        "#btnValidate",
        "input[type='submit'][value*='Validate']",
        "input[type='button'][value*='Validate']",
    ]:
        try:
            btn = page.locator(sel).first
            if btn.count() and btn.is_visible():
                btn.click()
                logger.info("Validate Customer clicked via: %s", sel)
                # Wait for confirmation popup or next section to activate
                try:
                    page.wait_for_timeout(3000)
                    # Dismiss any confirmation dialog
                    page.locator(
                        "button:has-text('OK'), button:has-text('Yes'), "
                        "button.btn-primary, .modal-footer button"
                    ).first.click()
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                _snap(page, "customer-validated", logger)
                return
        except Exception:
            continue
    logger.warning("Could not find 'Validate Customer' button.")


# ---------------------------------------------------------------------------
# Step 6 — Scrape Premium Summary
# ---------------------------------------------------------------------------

def _scrape_premium_summary(
    page: Page,
    vehicle: VehicleFormInput,
    cover_scraped: dict,
    logger: logging.Logger,
) -> CoverageDetails:
    """
    Expand 'Premium Summary and Payment' section and scrape all premium fields.
    Merges in cover details already scraped from the Cover Details step.
    Falls back to scraping any visible table on the page.
    """
    logger.info("--- Scraping Premium Summary ---")
    _expand_section(page, "Premium Summary", logger)
    page.wait_for_timeout(1500)
    _snap(page, "premium-summary", logger)

    # Scrape key-value pairs from all visible tables
    extra: dict = {}
    try:
        rows = page.locator("table tr").all()
        for row in rows:
            cells = row.locator("td").all()
            if len(cells) >= 2:
                label = cells[0].inner_text().strip().rstrip(":").strip()
                value = cells[-1].inner_text().strip()
                if label and value:
                    extra[label] = value
    except Exception as exc:
        logger.warning("Could not scrape table rows: %s", exc)

    # Merge in premium values from Cover Details step
    if cover_scraped.get("premium_values"):
        extra.update(cover_scraped["premium_values"])

    logger.info("Premium table rows scraped: %d", len(extra))

    def lookup(*keys: str) -> str | None:
        for k in keys:
            # Exact match
            if k in extra:
                return extra[k]
            # Case-insensitive partial match
            for ek, ev in extra.items():
                if k.lower() in ek.lower():
                    return ev
        return None

    # ── Scrape actual values directly from page controls ──────────────────
    # Registration number from boxes
    reg_val = ""
    try:
        boxes = page.locator("input[id^='Registration_Number'], input[name*='FastlaneRegistration']").all()
        box_vals = [b.input_value().strip() for b in boxes if b.count()]
        if any(box_vals):
            reg_val = "-".join([v for v in box_vals if v])
    except Exception:
        pass
    if not reg_val:
        reg_val = vehicle.registration_number or "NEW"

    # Real RTO Location string from website autocomplete
    real_rto = _get_field_value(
        page,
        "#RTOCityLocation",
        "input[id*='RTO']",
        "input[name*='RTO']",
        "input[placeholder*='RTO']",
    ) or vehicle.rto_location

    # Real Make / Model / Variant string selected from autocomplete
    real_model = _get_field_value(
        page,
        "#VehicleDetailsMakeModel",
        "input[name*='Make_Model']",
        "input[id*='MakeModel']",
    ) or vehicle.make_model_variant

    # Real Fuel Type selected on the website
    real_fuel = _get_field_value(
        page,
        "#ddlFuelType",
        "select[id*='FuelType']",
        "select[name*='Fuel']",
    ) or vehicle.fuel_type

    # Real Mfg Year & Month selected
    real_mfg_year = _get_field_value(
        page,
        "select[id*='MfgYear']",
        "select[id*='mfgYear']",
        "select[name*='MfgYear']",
    ) or vehicle.manufacturing_year
    real_mfg_month = _get_field_value(
        page,
        "select[id*='MfgMonth']",
        "select[id*='mfgMonth']",
        "select[name*='MfgMonth']",
    ) or vehicle.manufacturing_month

    # Real Policy Start Date
    real_policy_start = _get_field_value(
        page,
        "#PolicyStart_Date",
        "input[id*='PolicyStart']",
        "input[name*='PolicyStart']",
    ) or vehicle.policy_start_date

    # Proposal / Trace IDs from page header
    proposal_no = _get_field_value(
        page,
        "#lblProposalNumber",
        "span[id*='ProposalNo']",
        "label[id*='ProposalNo']",
        "td:has-text('Proposal') + td",
        "span[id*='Proposal']",
    )
    if not proposal_no:
        try:
            body_text = page.locator("body").inner_text()
            m = re.search(r"Proposal\s*(?:Number|No)[\s:]*([A-Za-z0-9/-]+)", body_text, re.IGNORECASE)
            if m:
                proposal_no = m.group(1).strip()
        except Exception:
            pass

    trace_id = _get_field_value(
        page,
        "span[id*='TraceId']",
        "#lblTraceId",
        ".trace-id",
        "td:has-text('TRACE') + td",
    )
    if not trace_id:
        try:
            body_text = page.locator("body").inner_text()
            m = re.search(r"TRACE\s*ID\s*:\s*([A-Za-z0-9]+)", body_text)
            if m:
                trace_id = m.group(1).strip()
        except Exception:
            pass

    # Scrape vehicle panel read-only fields directly from controls
    ex_showroom = _get_field_value(
        page,
        "span[id*='ExShowroom']",
        "label[id*='ExShowroom']",
        ".ex-showroom",
        "td:has-text('Ex showroom') + td",
        "a:has-text('Ex showroom')",
    )
    if not ex_showroom:
        try:
            body_text = page.locator("body").inner_text()
            m = re.search(r"Ex\s*showroom\s*price[^\d]*₹?\s*([0-9,]+)", body_text)
            if m:
                ex_showroom = m.group(1).replace(",", "").strip()
        except Exception:
            pass

    scraped_idv = _get_field_value(
        page,
        "#IDVVehicle",
        "input[id*='IDV']",
        "input[name*='IDV']",
        "span[id*='IDV']",
    ) or lookup("IDV", "Insured Declared Value", "IDV Value")

    scraped_cc = _get_field_value(
        page,
        "#txtCC",
        "input[id*='CC']",
        "span[id*='CC']",
        "td:has-text('CC/Watt') + td",
        "input[id*='ccWatt']",
    )
    scraped_segment = _get_field_value(
        page,
        "#txtVehicleSegment",
        "input[id*='Segment']",
        "span[id*='Segment']",
        "td:has-text('Vehicle Segment') + td",
    )
    scraped_zone = _get_field_value(
        page,
        "#txtZone",
        "input[id*='Zone']",
        "span[id*='Zone']",
        "td:has-text('Zone') + td",
    )
    scraped_lcc = _get_field_value(
        page,
        "#txtLCC",
        "input[id*='LCC']",
        "span[id*='LCC']",
        "td:has-text('LCC') + td",
    )
    scraped_seating = _get_field_value(
        page,
        "#txtSeatingCapacity",
        "input[id*='SeatingCapacity']",
        "span[id*='Seating']",
        "td:has-text('Seating') + td",
    )

    result = CoverageDetails(
        registration_number=reg_val,
        rto_location=real_rto or vehicle.rto_location,
        manufacturing_year=real_mfg_year or vehicle.manufacturing_year,
        manufacturing_month=real_mfg_month or vehicle.manufacturing_month,
        policy_start_date=real_policy_start or vehicle.policy_start_date,
        fuel_type=real_fuel or vehicle.fuel_type,
        make_model_variant=real_model or vehicle.make_model_variant,
        vehicle_make_model=real_model or vehicle.make_model_variant,
        idv=scraped_idv or lookup("IDV", "Insured Declared Value", "IDV Value"),
        ex_showroom_price=ex_showroom,
        base_od_premium=lookup("Basic OD", "Own Damage", "OD Premium", "Basic OD Premium"),
        tp_premium=lookup("TP Premium", "Third Party", "TPRD", "TP"),
        net_premium=lookup("Net Premium", "Net OD", "Net"),
        gst=lookup("GST", "Service Tax", "Tax"),
        total_premium=lookup("Total Premium", "Total", "Grand Total", "Gross Premium"),
        own_damage_premium=lookup("Own Damage", "OD Premium"),
        third_party_premium=lookup("Third Party", "TP Premium"),
        proposal_number=proposal_no,
        trace_id=trace_id,
        cc_watt=scraped_cc,
        vehicle_segment=scraped_segment,
        zone=scraped_zone,
        seating_capacity=scraped_seating,
        lcc=scraped_lcc,
        # Cover details from real website scraping
        od_covers=json.dumps(cover_scraped.get("od_covers", [])),
        liability_covers=json.dumps(cover_scraped.get("liability_covers", [])),
        addon_covers=json.dumps(cover_scraped.get("addon_covers", [])),
        raw_coverage_text="\n".join(f"{k}: {v}" for k, v in extra.items()),
        extra_fields=extra,
    )

    # The fuzzy label lookup above picks unrelated fields for two values on this
    # site ('Total' → a sum-insured field, 'TP' → the TPPD add-on), so read
    # those from where the site actually shows them.
    try:
        body_text = page.locator("body").inner_text()
        m = re.search(r"Your Premium for the vehicle is\s*:?\s*(?:₹|Rs\.?)?\s*([\d,]+(?:\.\d+)?)",
                      body_text, re.IGNORECASE)
        site_total = m.group(1) if m else None
    except Exception as exc:
        logger.debug("Could not read the total premium line: %s", exc)
        site_total = None

    def exact(*keys: str) -> str | None:
        wanted = {k.lower() for k in keys}
        for ek, ev in extra.items():
            if ek.strip().lower() in wanted:
                return ev
        return None

    if site_total:
        result.total_premium = site_total
        logger.info("Total premium shown by the site: %s", site_total)
    else:
        # Never fall back to the fuzzy match for the total: it picks a
        # sum-insured field (10000). Better empty than wrong.
        result.total_premium = exact("Total Premium", "Gross Premium", "Final Premium")
        logger.warning("The site's total premium line was not on the page; total left as %s.",
                       result.total_premium)

    basic_liability = exact("Basic Liability", "Basic TP", "Basic Third Party")
    if basic_liability:
        result.tp_premium = basic_liability
        result.third_party_premium = basic_liability
    basic_od = exact("Basic OD")
    if basic_od:
        result.base_od_premium = basic_od
        result.own_damage_premium = basic_od
    tax = exact("IGST", "GST")
    if tax:
        result.gst = tax

    # The label lookup above is fuzzy and can land on unrelated hidden fields
    # (e.g. 'TP' → "Repairatpreferredgarage = No"). Keep only real amounts.
    for premium_field in (
        "base_od_premium", "tp_premium", "net_premium", "gst", "total_premium",
        "own_damage_premium", "third_party_premium",
    ):
        raw = getattr(result, premium_field)
        cleaned = _money_or_none(raw)
        if raw and cleaned is None:
            logger.info("Ignoring non-amount value for %s: %r", premium_field, raw)
        setattr(result, premium_field, cleaned)

    logger.info(
        "Premium scraped | IDV=%s | OD=%s | TP=%s | Net=%s | GST=%s | Total=%s | Proposal=%s",
        result.idv, result.base_od_premium, result.tp_premium,
        result.net_premium, result.gst, result.total_premium,
        result.proposal_number,
    )
    return result


# ---------------------------------------------------------------------------
# Utility: expand accordion / tab section
# ---------------------------------------------------------------------------

def _expand_section(page: Page, section_name: str, logger: logging.Logger) -> None:
    """Click a collapsed accordion/tab section header to expand it."""
    try:
        header = page.locator(
            f"h3:has-text('{section_name}'), "
            f"h4:has-text('{section_name}'), "
            f"div.panel-heading:has-text('{section_name}'), "
            f"a[data-toggle='collapse']:has-text('{section_name}'), "
            f"td.heading:has-text('{section_name}'), "
            f"div.accordion-header:has-text('{section_name}')"
        ).first
        if header.count() and header.is_visible():
            aria = header.get_attribute("aria-expanded")
            if aria == "false" or aria is None:
                header.click()
                page.wait_for_timeout(600)
                logger.info("Expanded section: %s", section_name)
    except Exception as exc:
        logger.debug("Could not expand section '%s': %s", section_name, exc)


# ---------------------------------------------------------------------------
# Main scrape function
# ---------------------------------------------------------------------------

def _continue_to_proposal(page: Page, logger: logging.Logger, log_dir: pathlib.Path) -> bool:
    """Click 'Continue to Buy', wait for the proposal page and record its form.

    This only OPENS the proposal page and writes down every field on it
    (logs/proposal_form_fields.json). It does not type customer data yet: the
    field list is what the customer-filling step is written from.
    """
    quote_url = page.url
    if "proposalnumber" in quote_url.lower():
        # Someone already clicked the button in the browser window.
        logger.info("Step 10: Already on the proposal page; not clicking 'Continue to Buy'.")
        quote_url = ""
    else:
        button = page.locator(
            "#continueToBuy, input[value='Continue to Buy'], button:has-text('Continue to Buy')"
        )
        target = next((button.nth(i) for i in range(button.count()) if button.nth(i).is_visible()), None)
        if target is None:
            logger.warning("Step 10: 'Continue to Buy' button is not on the page. URL: %s", page.url)
            return False
        logger.info("Step 10: Clicking 'Continue to Buy'.")
        target.click()

    # The proposal page is a new URL (…/Motor/Motor?proposalNumber=…).
    reached = False
    for _ in range(120):   # up to ~60 s
        try:
            if page.url != quote_url and "proposalnumber" in page.url.lower():
                reached = True
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    if not reached:
        logger.warning("Step 10: the proposal page did not open within 60 s. URL: %s", page.url)
        _log_site_messages(page, logger, "after Continue to Buy", watch_ms=900)
        return False

    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2000)
    logger.info("Step 10: Proposal page opened. URL: %s", page.url)
    _snap(page, "proposal-page", logger)

    fields = page.evaluate("""() => {
        const $ = window.jQuery;
        const clean = t => (t || '').replace(/\\s+/g, ' ').trim().slice(0, 90);
        const shown = e => !!e && e.getClientRects().length > 0;
        const labelOf = el => {
            if (el.id) {
                const direct = document.querySelector('label[for="' + el.id + '"]');
                if (direct && clean(direct.innerText)) return clean(direct.innerText);
            }
            // Walk left/up through the layout until some text is found.
            let node = el;
            for (let depth = 0; depth < 6 && node; depth++) {
                let prev = node.previousElementSibling;
                while (prev) {
                    const text = clean(prev.innerText);
                    if (text && text.length < 90 && !prev.matches('input,select,textarea')
                        && !prev.querySelector('input,select,textarea')) return text;
                    prev = prev.previousElementSibling;
                }
                node = node.parentElement;
            }
            return '';
        };
        const sectionOf = el => {
            const all = Array.from(document.querySelectorAll('.accordionHeader, .panel-heading, h3, h4, legend'));
            let best = '';
            for (const h of all) {
                if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) best = clean(h.innerText) || best;
            }
            return best;
        };
        const out = [];
        for (const el of document.querySelectorAll('input, select, textarea, button')) {
            const type = (el.getAttribute('type') || el.tagName).toLowerCase();
            if (type === 'hidden') continue;
            const role = el.getAttribute('data-role') || '';
            let widget = null;
            if ($ && role) {
                const data = $(el).data() || {};
                widget = Object.keys(data).filter(k => /^kendo/.test(k)).map(k => data[k])[0] || null;
            }
            const wrapper = widget && widget.wrapper ? widget.wrapper[0] : null;
            let options = [];
            if (widget && widget.dataSource && widget.ul) {
                options = widget.ul.children('li').toArray().map(li => clean(li.innerText)).slice(0, 40);
            } else if (el.tagName === 'SELECT') {
                options = Array.from(el.options).map(o => clean(o.text)).slice(0, 40);
            }
            out.push({
                tag: el.tagName.toLowerCase(), type, id: el.id || '', name: el.getAttribute('name') || '',
                role, visible: shown(wrapper || el),
                label: labelOf(wrapper || el), section: sectionOf(wrapper || el),
                text: clean(widget && widget.text ? widget.text() : (el.tagName === 'BUTTON' ? el.innerText : '')),
                hasValue: !!(el.value || '').trim(),
                buttonValue: (type === 'button' || type === 'submit') ? clean(el.value) : '',
                readonly: el.readOnly || el.disabled || false,
                maxlength: el.getAttribute('maxlength') || '',
                onchange: clean(el.getAttribute('onchange')), onblur: clean(el.getAttribute('onblur')),
                onclick: clean(el.getAttribute('onclick')),
                options,
            });
        }
        return out;
    }""")

    out_file = log_dir / "proposal_form_fields.json"
    out_file.write_text(json.dumps({"url": page.url, "fields": fields}, indent=1), encoding="utf-8")
    visible = [f for f in fields if f.get("visible")]
    logger.info(
        "Step 10: Recorded %d fields (%d visible) from the proposal page to %s",
        len(fields), len(visible), out_file.name,
    )
    return True


# Example values that were suggested as placeholders for .env. They are not a
# real customer and must never be typed into a live proposal.
_EXAMPLE_CUSTOMER_VALUES = {
    "pan": "ABCDE1234F",
    "mobile": "9876543210",
    "email": "ravi@example.com",
}


def _proposal_type(page: Page, logger: logging.Logger, label: str, element_id: str, value: str) -> bool:
    """Type into a proposal-page text box by id and leave it, so the site's own checks run.
    Values are not logged: they are personal data."""
    if not value:
        return False
    try:
        boxes = page.locator(f"[id='{element_id}']")
        target = next((boxes.nth(i) for i in range(boxes.count()) if boxes.nth(i).is_visible()), None)
        if target is None:
            logger.warning("Proposal: '%s' box (#%s) is not on the page.", label, element_id)
            return False
        target.fill(str(value))
        target.blur()
        page.wait_for_timeout(300)
        logger.info("Proposal: filled '%s'.", label)
        return True
    except Exception as exc:
        logger.warning("Proposal: could not fill '%s': %s", label, str(exc).splitlines()[0])
        return False


def _kendo_state(page: Page, hint: str, role: str = "kendoDropDownList") -> dict:
    """Current text and options of a live Kendo dropdown."""
    try:
        return page.evaluate("(a) => {" + _KENDO_FIND_JS + """
            const w = findKendo(a.hints, a.role);
            if (!w) return {found: false, text: '', options: []};
            return {found: true, text: (w.text() || '').trim(),
                    options: w.ul.children('li').toArray().map(li => (li.innerText || '').trim())};
        }""", {"hints": [hint.lower()], "role": role})
    except Exception:
        return {"found": False, "text": "", "options": []}


def _proposal_pick(page: Page, logger: logging.Logger, label: str, hint: str, value: str) -> bool:
    """Choose a value in a proposal-page dropdown unless it already shows it."""
    if not value:
        return False
    state = _kendo_state(page, hint)
    if state["text"].lower() == value.strip().lower():
        logger.info("Proposal: '%s' already set.", label)
        return True
    picked = _kendo_pick(page, logger, label, value, "kendoDropDownList", (hint,), wait_ms=6000)
    if picked:
        logger.info("Proposal: selected '%s' = '%s'.", label, picked)
        page.wait_for_timeout(400)
        return True
    logger.warning("Proposal: could not select '%s' = '%s'. Options: %s",
                   label, value, _kendo_state(page, hint)["options"][:12])
    return False


def _fill_proposal_customer(
    page: Page,
    customer: CustomerInput,
    address: AddressInput,
    logger: logging.Logger,
) -> list[str]:
    """Fill Customer Details and Communication Address on the proposal page.

    Returns the list of things a person still has to look at. Nothing is saved
    or submitted here: 'Validate Customer' is a separate, opt-in step.
    """
    logger.info("--- Filling proposal: Customer Details ---")
    problems: list[str] = []
    # PROPOSAL_MANDATORY_ONLY=true fills only the fields the page marks with '*'.
    mandatory_only = os.getenv("PROPOSAL_MANDATORY_ONLY", "false").strip().lower() in {"1", "true", "yes", "on"}
    if mandatory_only:
        logger.info("Proposal: mandatory-only mode (skipping DOB, PAN, middle name, marital status, "
                    "nationality, street and landmark).")

    mobile = address.mobile or customer.mobile
    email = address.email or customer.email
    examples = [
        name for name, value in (("PAN", customer.pan), ("mobile", mobile), ("email", email))
        if value and value.strip().lower() == _EXAMPLE_CUSTOMER_VALUES[name.lower()].lower()
    ]
    if examples:
        message = (
            "The customer details in .env are still the example values ("
            + ", ".join(examples) + "). Replace the CUST_* values with a real customer's details."
        )
        logger.error("Proposal: not filling the customer form. %s", message)
        return [message]
    if not customer.first_name or not customer.last_name:
        message = "CUST_FIRST_NAME and CUST_LAST_NAME are required in .env."
        logger.error("Proposal: not filling the customer form. %s", message)
        return [message]

    # ── Identity ──────────────────────────────────────────────────────────
    if mandatory_only:
        pass
    elif customer.pan:
        _proposal_type(page, logger, "Date of Birth", "DPDateOfBirth", customer.dob)
        _proposal_type(page, logger, "PAN Number", "txtMaskedPanNumber", customer.pan)
    else:
        _proposal_type(page, logger, "Date of Birth", "DPDateOfBirth", customer.dob)
        # 'PAN Not Available' is a declaration about the customer, so it is
        # left for the person reviewing the proposal to tick.
        logger.warning("Proposal: CUST_PAN is empty; PAN left blank and 'PAN Not Available' not ticked.")
        problems.append("PAN is empty (tick 'PAN Not Available' by hand if that is correct)")
    _log_site_messages(page, logger, "after DOB/PAN", watch_ms=900)

    gender = (customer.gender or "").strip()
    salutation = os.getenv("CUST_SALUTATION", "").strip()
    if not salutation:
        if gender.lower() == "female":
            salutation = "Mrs." if customer.marital_status.strip().lower() == "married" else "Ms."
        else:
            salutation = "Mr."
    _proposal_pick(page, logger, "Salutation", "ddlsalutation", salutation)

    _proposal_type(page, logger, "First Name", "txtFirstName", customer.first_name)
    if not mandatory_only:
        _proposal_type(page, logger, "Middle Name", "txtMiddleName", customer.middle_name)
    _proposal_type(page, logger, "Last Name", "txtLastName", customer.last_name)

    # The salutation usually sets the gender; only choose it if it is still empty/different.
    if gender:
        _proposal_pick(page, logger, "Gender", "ddlgender", gender)
    if customer.occupation:
        _proposal_pick(page, logger, "Occupation", "ddloccupation", customer.occupation)
    if customer.marital_status and not mandatory_only:
        _proposal_pick(page, logger, "Marital Status", "ddlmaritalstatus", customer.marital_status)
    if customer.nationality and not mandatory_only:
        _proposal_pick(page, logger, "Nationality", "ddlnationality", customer.nationality)
    _log_site_messages(page, logger, "after customer details", watch_ms=900)

    # ── Communication address ─────────────────────────────────────────────
    logger.info("--- Filling proposal: Communication Address ---")
    _proposal_type(page, logger, "Flat/Door No", "objAddressAddress1", address.flat)
    _proposal_type(page, logger, "Premise/Building", "objAddressAddress2", address.building)
    if not mandatory_only:
        _proposal_type(page, logger, "Road/Street/Area", "objAddressAddress3", address.street)
        _proposal_type(page, logger, "Nearest Landmark", "objAddressNearestLandmark", address.landmark)

    if address.pincode:
        picked = _kendo_pick(
            page, logger, "Pincode Search", address.pincode,
            "kendoAutoComplete", ("objaddressaddrautocomplete",), first_if_no_match=True,
        )
        if picked:
            logger.info("Proposal: pincode search picked '%s'.", picked)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)
        else:
            logger.warning("Proposal: pincode search gave no result for the pincode in .env.")

    # State / district / city / pincode are normally filled by the pincode search.
    if not _kendo_state(page, "objaddressddlstate")["text"].lower().replace("select state", "").strip():
        if address.state:
            _proposal_pick(page, logger, "State", "objaddressddlstate", address.state.upper())
            page.wait_for_timeout(1200)
    for label, hint, wanted in (
        ("District", "objaddressddldistrict", address.city),
        ("Town/City", "objaddressddlcity", address.city),
        ("Pincode", "objaddressddlarea", address.pincode),
    ):
        state = _kendo_state(page, hint)
        if state["text"] and not state["text"].lower().startswith("select"):
            continue
        real = [o for o in state["options"] if o and not o.lower().startswith("select")]
        match = next((o for o in real if wanted and wanted.strip().lower() in o.lower()), None)
        choice = match or (real[0] if len(real) == 1 else None)
        if choice:
            _proposal_pick(page, logger, label, hint, choice)
            page.wait_for_timeout(1000)
        else:
            logger.warning("Proposal: '%s' is not set and I will not guess. Options: %s", label, real[:12])

    _proposal_type(page, logger, "Mobile Number", "txtMobileNumber", mobile)
    _proposal_type(page, logger, "Email", "txtEmailID", email)
    _log_site_messages(page, logger, "after address/contact", watch_ms=900)

    # ── Permanent address ─────────────────────────────────────────────────
    if address.same_as_comm:
        try:
            box = page.locator("#chkIsPermanentAddrSame").first
            if not box.is_checked():
                box.check(timeout=3000)
            logger.info("Proposal: ticked 'Permanent address same as communication'.")
            page.wait_for_timeout(800)
        except Exception as exc:
            logger.warning("Proposal: could not tick 'same as communication address': %s",
                           str(exc).splitlines()[0])

    # ── What is still empty? ──────────────────────────────────────────────
    for label, element_id in (
        ("First Name", "txtFirstName"), ("Last Name", "txtLastName"),
        ("Flat/Door No", "objAddressAddress1"), ("Premise/Building", "objAddressAddress2"),
        ("Mobile Number", "txtMobileNumber"), ("Email", "txtEmailID"),
    ):
        try:
            if not page.locator(f"[id='{element_id}']").first.input_value().strip():
                problems.append(f"{label} is empty")
        except Exception:
            problems.append(f"{label} could not be checked")
    for label, hint in (
        ("Gender", "ddlgender"), ("State", "objaddressddlstate"),
        ("District", "objaddressddldistrict"), ("Town/City", "objaddressddlcity"),
        ("Pincode", "objaddressddlarea"),
    ):
        text = _kendo_state(page, hint)["text"]
        if not text or text.lower().startswith("select"):
            problems.append(f"{label} is not selected")

    _snap(page, "proposal-customer-filled", logger)
    if problems:
        logger.warning("Proposal: still needs attention: %s", "; ".join(problems))
    else:
        logger.info("Proposal: customer details and address are filled.")
    return problems


def _validate_proposal_customer(page: Page, logger: logging.Logger) -> None:
    """Click the proposal page's 'Validate Customer' button and log what the site says."""
    button = page.locator("#BtnSaveClientDetails")
    if not button.count() or not button.first.is_visible():
        logger.warning("Proposal: 'Validate Customer' button is not on the page.")
        return
    logger.info("Proposal: clicking 'Validate Customer'.")
    button.first.click()
    _log_site_messages(page, logger, "after Validate Customer", watch_ms=6000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(1500)
    _snap(page, "proposal-after-validate-customer", logger)


def scrape_vehicle_coverage(
    website_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    scrape_type: str = "full",
    vehicle_data: dict | None = None,
    cover_data: dict | None = None,
    customer_data: dict | None = None,
    address_data: dict | None = None,
    **kwargs,
) -> str:
    """Login, navigate, fill form, scrape covers, fill customer, scrape premium, save to Postgres.

    All form values are dynamic — read from API request params, .env fallbacks,
    or computed defaults (today's date, current year/month).
    """
    log_dir = pathlib.Path(__file__).resolve().parents[1] / "logs"
    logger: logging.Logger = configure_logger(log_dir)

    settings = Settings.from_env()
    if website_url:
        import dataclasses
        settings = dataclasses.replace(settings, base_url=website_url)
    if username:
        import dataclasses
        settings = dataclasses.replace(settings, username=username)
    if password:
        import dataclasses
        settings = dataclasses.replace(settings, password=password)

    # Merge any vehicle-related kwargs into vehicle_data
    v_overrides = dict(vehicle_data or {})
    for k in (
        "registration_number", "rto_location", "make_model_variant",
        "manufacturing_year", "manufacturing_month", "purchase_date",
        "purchase_registration_date", "policy_start_date", "idv",
        "fuel_type", "business_type", "customer_type", "ncb",
        "ncb_eligibility", "discount", "data_discount_percent", "is_new_vehicle",
    ):
        if k in kwargs and kwargs[k] is not None:
            v_overrides[k] = kwargs[k]

    # Build dynamic inputs — blank fields get sensible defaults
    vehicle = _build_vehicle_input(v_overrides)
    cover_input = _build_cover_details_input(cover_data)
    customer = _build_customer_input(customer_data)
    address = _build_address_input(address_data)

    logger.info(
        "Vehicle scraper started | reg=%s | rto=%s | model=%s | "
        "mfg=%s/%s | policy_start=%s | fuel=%s | business=%s | new=%s",
        vehicle.registration_number or "(new)",
        vehicle.rto_location or "(not set)",
        vehicle.make_model_variant or "(not set)",
        vehicle.manufacturing_year,
        vehicle.manufacturing_month,
        vehicle.policy_start_date,
        vehicle.fuel_type,
        vehicle.business_type,
        vehicle.is_new_vehicle,
    )

    ensure_table()

    with sync_playwright() as playwright:
        browser_type = getattr(playwright, settings.browser)
        browser = browser_type.launch(headless=settings.headless)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(settings.timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)

        # Site alert()/confirm() boxes are otherwise dismissed silently, which
        # hides validation messages (e.g. why a dropdown was reset).
        def _log_dialog(dialog) -> None:
            logger.warning("Site %s box: %s", dialog.type, dialog.message)
            dialog.dismiss()  # same as Playwright's default when nothing listens

        page.on("dialog", _log_dialog)

        # The portal opens a "video calling" pop-up window by itself partway
        # through the session. It takes focus away from the form, which fires
        # the form's on-blur checks at a random moment. Nothing here needs
        # pop-ups, so blank/video-chat ones are swallowed before they open.
        context.add_init_script("""(() => {
            const realOpen = window.open;
            window.open = function (url, ...rest) {
                const u = String(url || '');
                if (u === '' || u === 'about:blank' || /videochat/i.test(u)) {
                    console.log('Blocked pop-up window:', u || '(blank)');
                    const sink = new Proxy(function () {}, {
                        get: (t, p) => (p === 'closed' ? false : (typeof p === 'symbol' ? undefined : sink)),
                        set: () => true,
                        apply: () => sink,
                    });
                    return sink;
                }
                return realOpen.call(window, url, ...rest);
            };
        })();""")
        context.on("page", lambda popup: logger.warning("The site opened another window: %s", popup.url))

        try:
            # ── Step 1: Login ─────────────────────────────────────────────
            logger.info("Step 1: Logging in via %s", settings.base_url)
            login_page = LoginPage(page, settings, logger)
            if not login_page.login(max_attempts=5):
                raise RuntimeError("Login failed — cannot proceed.")
            logger.info("Login succeeded. URL: %s", page.url)
            _snap(page, "post-login-dashboard", logger)

            # ── Step 2: Navigate to Two Wheeler form ──────────────────────
            logger.info("Step 2: Navigating to Vehicle Details form (Motor → Two Wheeler).")
            if not _navigate_to_tw_form(page, logger, settings):
                raise RuntimeError("Could not reach Vehicle Details form.")
            logger.info("Vehicle Details form reached. URL: %s", page.url)

            # ── Step 3: Fill Vehicle Details + click Get Coverage Details ─
            logger.info("Step 3: Filling Vehicle Details form.")
            _fill_vehicle_details(page, vehicle, logger)

            # Verify mandatory fields after initial fill
            logger.info("Verifying mandatory vehicle fields...")
            _verify_mandatory_vehicle_fields(page, vehicle, logger)

            # ── Step 4: Scrape & Fill Cover Details ───────────────────────
            logger.info("Step 4: Scraping & filling Cover Details (OD, Liability, AddOns).")
            cover_scraped = _scrape_and_fill_cover_details(page, cover_input, logger)

            # If the site refused to price the quote there is no customer step to do.
            site_errors = _site_error_lines(page)
            if site_errors:
                for line in site_errors:
                    logger.warning("Site refused the quote: %s", line)
                logger.info("Skipping customer steps 5-7 because the site returned an error.")
            elif page.locator(
                "#txtFirstName, input[name*='FirstName'], input[id*='FirstName']"
            ).count() == 0:
                # The customer form is on the NEXT page, after 'Continue to Buy'.
                # Filling "customer" values here would land on unrelated quote
                # fields (it overwrote the RTO location with the customer city).
                logger.info(
                    "Skipping customer steps 5-7: this page has no customer form "
                    "(it comes after 'Continue to Buy', which is not clicked)."
                )
            else:
                # ── Step 5: Fill Customer Details ─────────────────────────
                logger.info("Step 5: Filling Customer Details.")
                _fill_customer_details(page, customer, logger)

                # ── Step 6: Fill Address ──────────────────────────────────
                logger.info("Step 6: Filling Communication + Permanent Address.")
                _fill_address(page, address, logger)

                # ── Step 7: Validate Customer ─────────────────────────────
                logger.info("Step 7: Validating Customer.")
                _validate_customer(page, logger)

            # ── Step 8: Scrape Premium Summary ────────────────────────────
            logger.info("Step 8: Scraping Premium Summary.")
            coverage: CoverageDetails = _scrape_premium_summary(
                page, vehicle, cover_scraped, logger
            )

            has_premium = any((
                coverage.total_premium, coverage.net_premium,
                coverage.base_od_premium, coverage.tp_premium,
            ))
            site_errors = site_errors or _site_error_lines(page)
            if not has_premium and site_errors:
                # Report the insurer's own message instead of saving an empty row.
                _snap(page, "site-refused-quote", logger)
                raise SiteRejectedQuote(
                    "The insurer's site refused to price this quote "
                    f"(model={coverage.make_model_variant}; RTO={coverage.rto_location}; "
                    f"IDV={coverage.idv}): " + " | ".join(site_errors)
                )
            if not has_premium:
                logger.warning("No premium amounts were found on the page; saving the vehicle and cover details only.")

            # ── Step 9: Save to PostgreSQL ────────────────────────────────
            logger.info("Step 9: Saving to PostgreSQL.")
            row_id = save_coverage(coverage)
            logger.info("Coverage saved successfully | row_id=%d", row_id)

            # ── Step 10 (opt-in): Continue to Buy → proposal page ─────────
            # The quote is already saved, so a problem here cannot lose it.
            if has_premium and os.getenv("CONTINUE_TO_BUY", "false").strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    if _continue_to_proposal(page, logger, log_dir):
                        problems = _fill_proposal_customer(page, customer, address, logger)
                        validate = os.getenv("VALIDATE_CUSTOMER", "false").strip().lower() in {"1", "true", "yes", "on"}
                        if validate and not problems:
                            _validate_proposal_customer(page, logger)
                        elif validate:
                            logger.warning("Proposal: not clicking 'Validate Customer' because details are incomplete.")
                        else:
                            logger.info(
                                "Proposal: stopping before 'Validate Customer' for a person to review "
                                "(set VALIDATE_CUSTOMER=true to click it)."
                            )
                except Exception as exc:
                    logger.error("Continue to Buy step failed: %s", exc)
                    _snap(page, "continue-to-buy-failed", logger)

            return (
                f"Coverage scraped and saved | "
                f"reg={coverage.registration_number or 'NEW'} | "
                f"model={coverage.make_model_variant} | "
                f"IDV={coverage.idv} | "
                f"OD={coverage.base_od_premium} | "
                f"TP={coverage.tp_premium} | "
                f"Total={coverage.total_premium} | "
                f"Proposal={coverage.proposal_number} | "
                f"TraceID={coverage.trace_id} | "
                f"ExShowroom={coverage.ex_showroom_price} | "
                f"OD_covers={len(cover_scraped.get('od_covers', []))} | "
                f"Liability_covers={len(cover_scraped.get('liability_covers', []))} | "
                f"Addon_covers={len(cover_scraped.get('addon_covers', []))} | "
                f"DB row_id={row_id}"
            )

        finally:
            # Optionally leave the window up so the result can be looked at
            # before it closes (KEEP_BROWSER_OPEN_SECONDS in .env; 0 = close at once).
            try:
                keep_open = int(os.getenv("KEEP_BROWSER_OPEN_SECONDS", "0") or 0)
            except ValueError:
                keep_open = 0
            if keep_open > 0 and not settings.headless:
                logger.info("Keeping the browser open for %d s before closing.", keep_open)
                try:
                    page.wait_for_timeout(keep_open * 1000)
                except Exception:
                    pass  # the window was closed by hand
            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    result = scrape_vehicle_coverage()
    print(result)
