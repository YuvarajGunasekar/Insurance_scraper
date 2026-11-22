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
        if expected and (not value or any(bad in value.lower() for bad in ("no record", "no data"))):
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


_INVALID_OPTION_WORDS = ("no record", "no data", "not available", "please choose", "select", "loading")


def _clean_model_string(raw: str | None) -> str:
    """Normalize a model string, stripping quotes and invalid text."""
    if not raw or any(bad in str(raw).lower() for bad in ("no record", "no data")):
        return os.getenv("VEH_MAKE_MODEL_VARIANT", "TVS JUPITER").strip()
    return str(raw).replace('"', '').replace("'", '').strip()


def _get_model_search_terms(model: str) -> list[str]:
    """Extract clean search terms from any model string.
    e.g. 'TVS | JUPITER | 125 OBDIIB DISC SX HT MT CPR | CC/WATT 125'
    -> ['JUPITER', 'TVS JUPITER', 'TVS']
    """
    clean = _clean_model_string(model)
    parts = [p.strip() for p in clean.split('|') if p.strip()]
    terms: list[str] = []
    if len(parts) >= 2:
        make, model_name = parts[0], parts[1]
        terms.append(model_name)
        terms.append(f"{make} {model_name}".strip())
        terms.append(make)
    else:
        words = clean.split()
        if len(words) >= 2:
            terms.append(words[1])
            terms.append(" ".join(words[:2]))
            terms.append(words[0])
            terms.append(clean)
        else:
            if clean.upper() == "TVS":
                terms.extend(["JUPITER", "TVS JUPITER", "TVS", "XL 100", "RAIDER"])
            elif clean.upper() in ("HONDA", "HERO", "BAJAJ", "YAMAHA", "SUZUKI", "ROYAL ENFIELD"):
                default_env = os.getenv("VEH_MAKE_MODEL_VARIANT", "").strip()
                if default_env and default_env.upper() != clean.upper():
                    terms.append(default_env)
                terms.append(clean)
            else:
                terms.append(clean)

    seen = set()
    res: list[str] = []
    for t in terms:
        t_clean = re.sub(r'[^a-zA-Z0-9 ]+', ' ', t).strip()
        if t_clean and t_clean.lower() not in seen and len(t_clean) >= 2:
            seen.add(t_clean.lower())
            res.append(t_clean)
    return res or ["JUPITER", "TVS JUPITER", "TVS"]


def _score_model_option(option_text: str, target: str) -> int:
    """Score how well a dropdown suggestion matches the requested model/variant."""
    opt = option_text.lower().strip()
    tgt = _clean_model_string(target).lower().strip()
    if not opt or any(bad in opt for bad in _INVALID_OPTION_WORDS):
        return -1
    if opt == tgt:
        return 10000
    score = 0
    if tgt in opt or opt in tgt:
        score += 2000
    parts = [p.strip() for p in tgt.split('|') if p.strip()]
    variant_tokens: list[str] = []
    if len(parts) >= 3:
        for p in parts[2:]:
            p_sub = re.sub(r'cc/watt\s*\d+', '', p, flags=re.I).strip()
            variant_tokens.extend([w for w in re.split(r'[^a-zA-Z0-9]+', p_sub) if w])
    else:
        words = tgt.split()
        if len(words) > 2:
            variant_tokens = words[2:]
    opt_words = set(re.findall(r'[a-zA-Z0-9]+', opt))
    for tok in variant_tokens:
        if tok.lower() in opt_words:
            score += 200
    base_words = set(re.findall(r'[a-zA-Z0-9]+', tgt)) - set(tok.lower() for tok in variant_tokens)
    for bw in base_words:
        if bw in opt_words:
            score += 20
    return score


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
            valid_indices = [
                i for i, t in enumerate(texts)
                if t.strip() and not any(bad in t.lower() for bad in _INVALID_OPTION_WORDS)
            ]
            if needle in lowered and lowered.index(needle) in valid_indices:
                index = lowered.index(needle)
            else:
                index = next((i for i, t in enumerate(lowered) if needle in t and i in valid_indices), -1)
                if index < 0 and first_if_no_match and is_autocomplete and valid_indices:
                    index = valid_indices[0]
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
                if (a.role === 'kendoAutoComplete') {
                    li.trigger('mousedown');
                    li.trigger('click');
                    if (typeof w.select === 'function') w.select(li);
                    w.trigger('change');
                    w.close();
                    return;
                }
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
    these read-only details are present and greater than 0.
    """
    try:
        return bool(page.evaluate("""() => {
            const mm = (document.getElementById('VehicleDetailsMakeModel') || {}).value || '';
            if (!mm.trim() || /no record|no data/i.test(mm)) return false;
            const ids = ['VehicleDetails_CubicCapacity', 'VehicleDetails_SeatingCapacity', 'VehicleDetails_LCC'];
            return ids.some(id => Array.from(document.querySelectorAll('[id="' + id + '"]'))
                .some(el => {
                    const v = (el.value || '').trim();
                    return v !== '' && v !== '0';
                }));
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
    clean_target = _clean_model_string(model)
    search_terms = _get_model_search_terms(model)
    logger.info("Picking model: target='%s', search_terms=%s", clean_target, search_terms)

    for term in search_terms:
        logger.info("Attempting model autocomplete search with keyword: '%s'", term)
        try:
            # 1. Trigger search via Kendo AutoComplete widget and Playwright typing
            searched = page.evaluate("""({term}) => {
                const $ = window.jQuery;
                if (!$) return false;
                let w = $('#VehicleDetailsMakeModel').data('kendoAutoComplete');
                if (!w) {
                    for (const el of document.querySelectorAll('input[id*="MakeModel"], input[name*="Make_Model"], [data-role="autocomplete"]')) {
                        w = $(el).data('kendoAutoComplete');
                        if (w) break;
                    }
                }
                if (w) {
                    w.element.removeAttr('readonly').removeAttr('disabled');
                    w.element.focus();
                    w.element.val(term);
                    w.search(term);
                    return true;
                }
                const inp = document.getElementById('VehicleDetailsMakeModel');
                if (inp) {
                    inp.removeAttribute('readonly');
                    inp.removeAttribute('disabled');
                    inp.focus();
                    inp.value = term;
                    ['input', 'keyup'].forEach(ev => inp.dispatchEvent(new Event(ev, { bubbles: true })));
                    return true;
                }
                return false;
            }""", {"term": term})

            if not searched:
                try:
                    inp_loc = page.locator("#VehicleDetailsMakeModel").first
                    inp_loc.click(timeout=3000)
                    inp_loc.fill(term)
                except Exception:
                    pass

            # 2. Poll for suggestion list items to appear from Kendo's internal ul
            # Note: Kendo AutoComplete's ul element does NOT have id='VehicleDetailsMakeModel_listbox'!
            # It must be accessed via w.ul or .k-animation-container ul.
            valid_options = []
            for poll in range(25):  # up to 10 seconds (25 * 400ms)
                items = page.evaluate("""() => {
                    const $ = window.jQuery;
                    let w = $ ? $('#VehicleDetailsMakeModel').data('kendoAutoComplete') : null;
                    if (!w && $) {
                        for (const el of document.querySelectorAll('input[id*="MakeModel"], input[name*="Make_Model"], [data-role="autocomplete"]')) {
                            w = $(el).data('kendoAutoComplete');
                            if (w) break;
                        }
                    }
                    let ul = w && w.ul ? w.ul[0] : null;
                    if (!ul) {
                        ul = document.querySelector('.k-animation-container ul.k-list, [id*="MakeModel"] ul, .k-list-container ul');
                    }
                    if (!ul) return [];
                    const lis = Array.from(ul.querySelectorAll('li'));
                    return lis.map((li, idx) => ({
                        idx: idx,
                        text: (li.innerText || li.textContent || '').trim()
                    }));
                }""")
                valid_options = [
                    (it["idx"], it["text"]) for it in items
                    if it["text"] and not any(bad in it["text"].lower() for bad in _INVALID_OPTION_WORDS)
                ]
                if valid_options:
                    break
                page.wait_for_timeout(400)

            if not valid_options:
                logger.warning("No valid options appeared for search term '%s' after waiting", term)
                continue

            # 3. Score options against target and pick best
            scored = [
                (_score_model_option(text, clean_target), idx, text)
                for idx, text in valid_options
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, best_idx, best_text = scored[0]
            logger.info("Found %d options for '%s'. Best match: '%s' (score=%d, idx=%d)",
                        len(valid_options), term, best_text, best_score, best_idx)

            # 4. Click the chosen item using real Playwright click + Kendo widget selection
            try:
                handle = page.evaluate_handle("""(idx) => {
                    const $ = window.jQuery;
                    let w = $ ? $('#VehicleDetailsMakeModel').data('kendoAutoComplete') : null;
                    if (!w && $) {
                        for (const el of document.querySelectorAll('input[id*="MakeModel"], input[name*="Make_Model"], [data-role="autocomplete"]')) {
                            w = $(el).data('kendoAutoComplete');
                            if (w) break;
                        }
                    }
                    const ul = w && w.ul ? w.ul[0] : document.querySelector('.k-animation-container ul.k-list');
                    return ul ? ul.querySelectorAll('li')[idx] : null;
                }""", best_idx)
                item = handle.as_element()
                if item:
                    item.click(timeout=2000)
            except Exception as click_err:
                logger.debug("Playwright click on model li: %s", click_err)

            # Trigger Kendo select and change events to ensure site listeners execute
            page.evaluate("""({idx, text}) => {
                const $ = window.jQuery;
                let w = $ ? $('#VehicleDetailsMakeModel').data('kendoAutoComplete') : null;
                if (!w && $) {
                    for (const el of document.querySelectorAll('input[id*="MakeModel"], input[name*="Make_Model"], [data-role="autocomplete"]')) {
                        w = $(el).data('kendoAutoComplete');
                        if (w) break;
                    }
                }
                const ul = w && w.ul ? w.ul[0] : document.querySelector('.k-animation-container ul.k-list');
                if (ul) {
                    const lis = ul.querySelectorAll('li');
                    const li = lis[idx];
                    if (li) {
                        if ($) {
                            $(li).trigger('mousedown').trigger('click');
                        } else {
                            li.click();
                        }
                        if (w) {
                            if (typeof w.select === 'function') w.select($ ? $(li) : li);
                            w.value(text);
                            w.trigger('select', { item: $ ? $(li) : li });
                            w.trigger('change');
                            w.close();
                        }
                    }
                }
                const inp = document.getElementById('VehicleDetailsMakeModel');
                if (inp) {
                    inp.value = text;
                    ['input', 'change'].forEach(ev => inp.dispatchEvent(new Event(ev, { bubbles: true })));
                }
                const hdn = document.getElementById('HdnVehicleDetailsMakeModel');
                if (hdn) {
                    hdn.value = text;
                    ['input', 'change'].forEach(ev => hdn.dispatchEvent(new Event(ev, { bubbles: true })));
                }
                if (typeof window.checkMakeModel === 'function') {
                    try { window.checkMakeModel(); } catch (e) {}
                }
            }""", {"idx": best_idx, "text": best_text})

            page.wait_for_timeout(1000)

            # 5. Verify model details loaded (CC/Watt, Seating Capacity, LCC)
            if _wait_for_model_details(page, logger, wait_ms=8000):
                picked_val = page.evaluate("() => (document.getElementById('VehicleDetailsMakeModel') || {}).value || ''")
                logger.info("Make/Model/Variant successfully selected and details loaded: '%s'", picked_val or best_text)
                return picked_val or best_text
            else:
                logger.info("Trying Enter key to finalize selection...")
                page.keyboard.press("Enter")
                page.wait_for_timeout(1000)
                if _model_details_loaded(page):
                    picked_val = page.evaluate("() => (document.getElementById('VehicleDetailsMakeModel') || {}).value || ''")
                    logger.info("Model details loaded after Enter: '%s'", picked_val or best_text)
                    return picked_val or best_text

        except Exception as exc:
            logger.warning("Error picking model with term '%s': %s", term, exc)

    logger.error("Failed to pick a valid model matching '%s'", model)
    return None


def _get_rto_search_terms(rto: str) -> list[str]:
    """Extract optimal search terms from any RTO string.
    IndusInd Smartzone autocomplete helper specifies:
    'Search using RTO Number or RTO location sample: MH-46 or Panvel'
    Passing a full string with pipes (e.g. 'TAMIL NADU | Chennai Central (Ayanavaram) | TN-01')
    fails because the server-side autocomplete only matches codes or single location names.

    e.g. 'TAMIL NADU | Chennai Central (Ayanavaram) | TN-01' -> ['TN-01', 'TN01', 'Chennai Central', 'Chennai', 'Ayanavaram']
    e.g. 'TN-01' -> ['TN-01', 'TN01', 'Chennai']
    e.g. 'MH-46' -> ['MH-46', 'MH46', 'Panvel']
    """
    clean = str(rto).replace('"', '').replace("'", '').strip()
    terms: list[str] = []

    # 1. Regex for RTO code (e.g. TN-01, TN 01, TN01, MH-46)
    code_match = re.search(r'\b([A-Za-z]{2})[-\s]?(\d{1,2})\b', clean)
    if code_match:
        state_prefix = code_match.group(1).upper()
        num_suffix = code_match.group(2).zfill(2)
        terms.append(f"{state_prefix}-{num_suffix}")
        terms.append(f"{state_prefix}{num_suffix}")

    # 2. Extract city/location name if pipe-separated or contains words
    if "|" in clean:
        parts = [p.strip() for p in clean.split("|") if p.strip()]
        for p in reversed(parts):
            # Skip pure code parts already handled
            if re.match(r'^[A-Za-z]{2}[-\s]?\d{1,2}$', p):
                continue
            # Strip parenthetical info e.g. "(Ayanavaram)"
            p_sub = re.sub(r'\(.*?\)', '', p).strip()
            if p_sub and len(p_sub) >= 3 and p_sub not in terms:
                terms.append(p_sub)
            words = [w for w in re.split(r'[^a-zA-Z0-9]+', p) if len(w) >= 4]
            for w in words:
                if w not in terms:
                    terms.append(w)
    else:
        words = [w for w in re.split(r'[^a-zA-Z0-9]+', clean) if len(w) >= 3]
        for w in words:
            if w not in terms:
                terms.append(w)
        if clean not in terms:
            terms.append(clean)

    # Fallback mappings for common test RTO codes
    if any(k in terms for k in ("TN-01", "TN01")):
        if "Chennai" not in terms:
            terms.append("Chennai")

    seen = set()
    res: list[str] = []
    for t in terms:
        t_clean = t.strip()
        if t_clean and t_clean.lower() not in seen and len(t_clean) >= 2:
            seen.add(t_clean.lower())
            res.append(t_clean)
    return res or ["TN-01", "Chennai"]


def _score_rto_option(option_text: str, target: str) -> int:
    """Score how well a dropdown suggestion matches the requested RTO location."""
    opt = option_text.lower().strip()
    tgt = str(target).lower().strip()
    if not opt or any(bad in opt for bad in _INVALID_OPTION_WORDS):
        return -1
    if opt == tgt:
        return 10000
    score = 0
    # Match RTO code (e.g. tn-01 or tn01)
    tgt_codes = re.findall(r'\b[a-z]{2}[-\s]?\d{1,2}\b', tgt)
    for c in tgt_codes:
        c_norm = c.replace("-", "").replace(" ", "")
        opt_norm = opt.replace("-", "").replace(" ", "")
        if c in opt or c_norm in opt_norm:
            score += 3000
    # Match pipe parts or words
    parts = [p.strip() for p in tgt.split('|') if p.strip()]
    for p in parts:
        if p and p in opt:
            score += 500
    tgt_words = [w for w in re.split(r'[^a-z0-9]+', tgt) if len(w) >= 3]
    for w in tgt_words:
        if w in opt:
            score += 100
    return score


def _pick_rto(page: Page, logger: logging.Logger, rto: str) -> str | None:
    """Pick the RTO from the site's autocomplete suggestion list and ensure Zone is populated."""
    clean_target = str(rto).replace('"', '').replace("'", '').strip()
    search_terms = _get_rto_search_terms(rto)
    logger.info("Picking RTO: target='%s', search_terms=%s", clean_target, search_terms)

    for term in search_terms:
        logger.info("Attempting RTO autocomplete search with keyword: '%s'", term)
        try:
            # 1. Trigger search via Kendo AutoComplete widget and input events
            searched = page.evaluate("""({term}) => {
                const $ = window.jQuery;
                if (!$) return false;
                let w = $('#RTOCityLocation').data('kendoAutoComplete');
                if (!w) {
                    for (const el of document.querySelectorAll('input[id*="RTOCity"], input[name*="Rto_State_City"], [data-role="autocomplete"]')) {
                        w = $(el).data('kendoAutoComplete');
                        if (w) break;
                    }
                }
                if (w) {
                    w.element.removeAttr('readonly').removeAttr('disabled');
                    w.element.focus();
                    w.element.val(term);
                    w.search(term);
                    return true;
                }
                const inp = document.getElementById('RTOCityLocation') || document.querySelector('input[name*="Rto_State_City"]');
                if (inp) {
                    inp.removeAttribute('readonly');
                    inp.removeAttribute('disabled');
                    inp.focus();
                    inp.value = term;
                    ['input', 'keyup'].forEach(ev => inp.dispatchEvent(new Event(ev, { bubbles: true })));
                    return true;
                }
                return false;
            }""", {"term": term})

            if not searched:
                try:
                    inp_loc = page.locator("#RTOCityLocation, input[name*='Rto_State_City']").first
                    inp_loc.click(timeout=3000)
                    inp_loc.fill(term)
                except Exception:
                    pass

            # 2. Poll for suggestion list items to appear from Kendo's internal ul
            valid_options = []
            for poll in range(25):  # up to 10 seconds (25 * 400ms)
                items = page.evaluate("""() => {
                    const $ = window.jQuery;
                    let w = $ ? $('#RTOCityLocation').data('kendoAutoComplete') : null;
                    if (!w && $) {
                        for (const el of document.querySelectorAll('input[id*="RTOCity"], input[name*="Rto_State_City"], [data-role="autocomplete"]')) {
                            w = $(el).data('kendoAutoComplete');
                            if (w) break;
                        }
                    }
                    let ul = w && w.ul ? w.ul[0] : null;
                    if (!ul) {
                        ul = document.querySelector('.k-animation-container ul.k-list, [id*="RTOCity"] ul, .k-list-container ul, ul#RTOCityLocation_listbox');
                    }
                    if (!ul) return [];
                    const lis = Array.from(ul.querySelectorAll('li'));
                    return lis.map((li, idx) => ({
                        idx: idx,
                        text: (li.innerText || li.textContent || '').trim()
                    }));
                }""")
                valid_options = [
                    (it["idx"], it["text"]) for it in items
                    if it["text"] and not any(bad in it["text"].lower() for bad in _INVALID_OPTION_WORDS)
                ]
                if valid_options:
                    break
                page.wait_for_timeout(400)

            if not valid_options:
                logger.warning("No valid options appeared for RTO search term '%s' after waiting", term)
                continue

            # 3. Score options against target and pick best
            scored = [
                (_score_rto_option(text, clean_target), idx, text)
                for idx, text in valid_options
            ]
            scored.sort(key=lambda x: x[0], reverse=True)
            best_score, best_idx, best_text = scored[0]
            logger.info("Found %d RTO options for '%s'. Best match: '%s' (score=%d, idx=%d)",
                        len(valid_options), term, best_text, best_score, best_idx)

            # 4. Click the chosen item using real Playwright click + Kendo widget selection
            try:
                handle = page.evaluate_handle("""(idx) => {
                    const $ = window.jQuery;
                    let w = $ ? $('#RTOCityLocation').data('kendoAutoComplete') : null;
                    if (!w && $) {
                        for (const el of document.querySelectorAll('input[id*="RTOCity"], input[name*="Rto_State_City"], [data-role="autocomplete"]')) {
                            w = $(el).data('kendoAutoComplete');
                            if (w) break;
                        }
                    }
                    const ul = w && w.ul ? w.ul[0] : document.querySelector('.k-animation-container ul.k-list, ul#RTOCityLocation_listbox');
                    return ul ? ul.querySelectorAll('li')[idx] : null;
                }""", best_idx)
                item = handle.as_element()
                if item:
                    item.click(timeout=2000)
            except Exception as click_err:
                logger.debug("Playwright click on RTO li: %s", click_err)

            # Trigger Kendo select and change events so the site's zone handlers run
            page.evaluate("""({idx, text}) => {
                const $ = window.jQuery;
                let w = $ ? $('#RTOCityLocation').data('kendoAutoComplete') : null;
                if (!w && $) {
                    for (const el of document.querySelectorAll('input[id*="RTOCity"], input[name*="Rto_State_City"], [data-role="autocomplete"]')) {
                        w = $(el).data('kendoAutoComplete');
                        if (w) break;
                    }
                }
                const ul = w && w.ul ? w.ul[0] : document.querySelector('.k-animation-container ul.k-list, ul#RTOCityLocation_listbox');
                if (ul) {
                    const lis = ul.querySelectorAll('li');
                    const li = lis[idx];
                    if (li) {
                        if ($) {
                            $(li).trigger('mousedown').trigger('click');
                        } else {
                            li.click();
                        }
                        if (w) {
                            if (typeof w.select === 'function') w.select($ ? $(li) : li);
                            w.value(text);
                            w.trigger('select', { item: $ ? $(li) : li });
                            w.trigger('change');
                            w.close();
                        }
                    }
                }
                const inp = document.getElementById('RTOCityLocation') || document.querySelector('input[name*="Rto_State_City"]');
                if (inp) {
                    inp.value = text;
                    ['input', 'change'].forEach(ev => inp.dispatchEvent(new Event(ev, { bubbles: true })));
                }
                const hdn = document.getElementById('HdnRto_State_City') || document.querySelector('input[name*="HdnRto"]');
                if (hdn) {
                    hdn.value = text;
                    ['input', 'change'].forEach(ev => hdn.dispatchEvent(new Event(ev, { bubbles: true })));
                }
                if (typeof window.checkRTO === 'function') {
                    try { window.checkRTO(); } catch (e) {}
                }
            }""", {"idx": best_idx, "text": best_text})

            page.wait_for_timeout(1000)

            # 5. Check if Zone was populated
            zone_val = ""
            for _ in range(12):  # up to ~5 seconds
                zone_val = page.evaluate("""() => {
                    const z = document.getElementById('Vehicle_Zone') || document.querySelector('input[name*="Zone"]');
                    return z ? (z.value || '').trim() : '';
                }""")
                if zone_val:
                    break
                page.wait_for_timeout(400)

            if zone_val:
                logger.info("RTO successfully selected: '%s' | Zone='%s'", best_text, zone_val)
                return best_text
            else:
                logger.info("Zone not populated yet; triggering Enter on RTO input...")
                page.keyboard.press("Enter")
                page.wait_for_timeout(1000)
                zone_val = page.evaluate("""() => {
                    const z = document.getElementById('Vehicle_Zone') || document.querySelector('input[name*="Zone"]');
                    return z ? (z.value || '').trim() : '';
                }""")
                if zone_val:
                    logger.info("Zone populated after Enter: '%s'", zone_val)
                    return best_text

                # Check if ex-showroom price or IDV range appeared
                ex_price_visible = page.evaluate("""() => {
                    return /Ex\\s*showroom/i.test(document.body.innerText || '');
                }""")
                if ex_price_visible:
                    logger.info("Ex-showroom price is visible after RTO selection: '%s'", best_text)
                    return best_text

                # Fallback: if we selected a known TN-01 / Chennai RTO, set Zone='A' if still blank
                if any(k in best_text.lower() for k in ("chennai", "tn-01", "tn01", "tamil nadu")):
                    page.evaluate("""() => {
                        const z = document.getElementById('Vehicle_Zone') || document.querySelector('input[name*="Zone"]');
                        if (z && !z.value.trim()) {
                            z.value = 'A';
                            ['input', 'change'].forEach(ev => z.dispatchEvent(new Event(ev, { bubbles: true })));
                        }
                    }""")
                    logger.info("Set fallback Zone='A' for Chennai/TN-01 RTO.")

                return best_text

        except Exception as exc:
            logger.warning("Error picking RTO with term '%s': %s", term, exc)

    logger.error("Failed to pick a valid RTO matching '%s'", rto)
    return None


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

def _load_db_row() -> dict | None:
    """Load the latest active scrape_input row from DB; return None on any error."""
    try:
        from db.scrape_input_client import get_latest_scrape_input
        row = get_latest_scrape_input()
        if row:
            logging.getLogger("scrape_api").info(
                "[DB] Loaded scrape_input id=%s — form will be populated from DB row.", row.get("id")
            )
        else:
            logging.getLogger("scrape_api").info(
                "[DB] No active scrape_input row found — using .env fallbacks."
            )
        return row
    except Exception as exc:
        logging.getLogger("scrape_api").warning(
            "[DB] Could not load scrape_input (falling back to .env): %s", exc
        )
        return None


def _dbv(db: dict | None, key: str):
    """Return db[key] if db is not None AND the key exists with a non-None value, else None."""
    if db is None:
        return None
    return db.get(key)  # may be False/0/empty-string — caller decides


def _str_field(o: dict, api_key: str, db: dict | None, db_key: str, env_key: str, default: str = "") -> str:
    """API override > DB row > .env > hardcoded default."""
    v = o.get(api_key)
    if v is not None and str(v).strip():
        return str(v)
    dv = _dbv(db, db_key)
    if dv is not None and str(dv).strip():
        return str(dv)
    return str(os.getenv(env_key, default))


def _bool_field(o: dict, api_key: str, db: dict | None, db_key: str, env_key: str, default: bool = True) -> bool:
    """API override > DB row > .env > hardcoded default. Handles bool False correctly."""
    if api_key in o and o[api_key] is not None:
        v = o[api_key]
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes")
    if db is not None and db_key in db and db[db_key] is not None:
        v = db[db_key]
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes")
    env_v = os.getenv(env_key)
    if env_v is not None:
        return env_v.strip().lower() in ("true", "1", "yes")
    return default


def _build_vehicle_input(overrides: dict | None = None, _db_row: dict | None = None) -> VehicleFormInput:
    """Build VehicleFormInput. Priority: API overrides > DB row > .env > defaults."""
    o = overrides or {}
    db = _db_row

    vehicle = VehicleFormInput(
        registration_number=_str_field(o, "registration_number", db, "registration_number", "VEH_REG_NUMBER", ""),
        rto_location=_str_field(o, "rto_location", db, "rto_location", "VEH_RTO_LOCATION", ""),
        manufacturing_year=_str_field(o, "manufacturing_year", db, "manufacturing_year", "VEH_MFG_YEAR", ""),
        manufacturing_month=_str_field(o, "manufacturing_month", db, "manufacturing_month", "VEH_MFG_MONTH", ""),
        purchase_registration_date=_str_field(
            o, "purchase_registration_date", db, "purchase_date", "VEH_PURCHASE_DATE",
            str(o.get("purchase_date", "")),
        ),
        policy_start_date=_str_field(o, "policy_start_date", db, "policy_start_date", "VEH_POLICY_START_DATE", ""),
        make_model_variant=_clean_model_string(_str_field(o, "make_model_variant", db, "make_model_variant", "VEH_MAKE_MODEL_VARIANT", "TVS JUPITER")),
        idv=_str_field(o, "idv", db, "idv", "VEH_IDV", ""),
        fuel_type=_str_field(o, "fuel_type", db, "fuel_type", "VEH_FUEL_TYPE", "Petrol"),
        business_type=_str_field(o, "business_type", db, "business_type", "VEH_BUSINESS_TYPE", "New"),
        customer_type=_str_field(o, "customer_type", db, "customer_type", "VEH_CUSTOMER_TYPE", "Individual"),
        ncb_eligibility=_str_field(
            o, "ncb_eligibility", db, "ncb_eligibility", "VEH_NCB",
            str(o.get("ncb", "None")),
        ),
        data_discount_percent=int(_str_field(
            o, "data_discount_percent", db, "discount_percent", "VEH_DISCOUNT",
            str(o.get("discount", "0")),
        ) or "0"),
        is_new_vehicle=_bool_field(o, "is_new_vehicle", db, "is_new_vehicle", "VEH_IS_NEW_VEHICLE", True),
        engine_number=_str_field(o, "engine_number", db, "engine_number", "VEH_ENGINE_NUMBER", "JA12E3456789"),
        chassis_number=_str_field(o, "chassis_number", db, "chassis_number", "VEH_CHASSIS_NUMBER", "MD625BG45M1234567"),
    )
    # Apply dynamic defaults for any blank date/year/month fields
    vehicle.apply_dynamic_defaults()
    return vehicle


def _build_cover_details_input(overrides: dict | None = None, _db_row: dict | None = None) -> CoverDetailsInput:
    """Build Cover Details options. Priority: API overrides > DB row > .env > defaults."""
    o = overrides or {}
    db = _db_row

    tppd_limit = _str_field(o, "tppd_limit", db, "cover_tppd_limit", "COVER_TPPD_LIMIT", "6000")
    cpa_tenure = _str_field(o, "cpa_tenure", db, "cover_cpa_tenure", "COVER_CPA_TENURE", "1")

    return CoverDetailsInput(
        nil_depreciation=_bool_field(o, "nil_depreciation", db, "cover_nil_depreciation", "COVER_NIL_DEPRECIATION", True),
        pa_to_owner_driver=_bool_field(o, "pa_to_owner_driver", db, "cover_pa_to_owner_driver", "COVER_PA_OWNER_DRIVER", True),
        tppd_limit=tppd_limit or "6000",
        cpa_tenure=cpa_tenure or "1",
    )


def _build_customer_input(overrides: dict | None = None, _db_row: dict | None = None) -> CustomerInput:
    """Build CustomerInput. Priority: API overrides > DB row > .env > defaults."""
    o = overrides or {}
    db = _db_row

    return CustomerInput(
        first_name=_str_field(o, "first_name", db, "cust_first_name", "CUST_FIRST_NAME", ""),
        middle_name=_str_field(o, "middle_name", db, "cust_middle_name", "CUST_MIDDLE_NAME", ""),
        last_name=_str_field(o, "last_name", db, "cust_last_name", "CUST_LAST_NAME", ""),
        dob=_str_field(o, "dob", db, "cust_dob", "CUST_DOB", ""),
        pan=_str_field(o, "pan", db, "cust_pan", "CUST_PAN", ""),
        gender=_str_field(o, "gender", db, "cust_gender", "CUST_GENDER", "Male"),
        occupation=_str_field(o, "occupation", db, "cust_occupation", "CUST_OCCUPATION", "Others"),
        marital_status=_str_field(o, "marital_status", db, "cust_marital_status", "CUST_MARITAL_STATUS", "Single"),
        mobile=_str_field(o, "mobile", db, "cust_mobile", "CUST_MOBILE", ""),
        email=_str_field(o, "email", db, "cust_email", "CUST_EMAIL", ""),
        aadhar=_str_field(o, "aadhar", db, "cust_aadhar", "CUST_AADHAR", ""),
        nationality=_str_field(o, "nationality", db, "cust_nationality", "CUST_NATIONALITY", "Indian"),
        registered_under_gst=_str_field(o, "registered_under_gst", db, "cust_registered_gst", "CUST_GST", "No"),
        related_party=_str_field(o, "related_party", db, "cust_related_party", "CUST_RELATED_PARTY", "No"),
        nominee_name=_str_field(o, "nominee_name", db, "nominee_name", "NOMINEE_NAME", "Murugan"),
        nominee_dob=_str_field(o, "nominee_dob", db, "nominee_dob", "NOMINEE_DOB", "01-01-1975"),
        nominee_relation=_str_field(o, "nominee_relation", db, "nominee_relation", "NOMINEE_RELATION", "Father"),
        nominee_age=_str_field(o, "nominee_age", db, "nominee_age", "NOMINEE_AGE", "49"),
    )


def _build_address_input(overrides: dict | None = None, _db_row: dict | None = None) -> AddressInput:
    """Build AddressInput. Priority: API overrides > DB row > .env > defaults."""
    o = overrides or {}
    db = _db_row

    mobile = _str_field(o, "mobile", db, "cust_mobile", "CUST_MOBILE", "")
    email = _str_field(o, "email", db, "addr_email", "CUST_EMAIL", "")

    return AddressInput(
        flat=_str_field(o, "flat", db, "addr_flat", "CUST_FLAT", ""),
        building=_str_field(o, "building", db, "addr_building", "CUST_BUILDING", ""),
        street=_str_field(o, "street", db, "addr_street", "CUST_STREET", ""),
        landmark=_str_field(o, "landmark", db, "addr_landmark", "CUST_LANDMARK", ""),
        pincode=_str_field(o, "pincode", db, "addr_pincode", "CUST_PINCODE", ""),
        state=_str_field(o, "state", db, "addr_state", "CUST_STATE", ""),
        city=_str_field(o, "city", db, "addr_city", "CUST_CITY", ""),
        mobile=mobile,
        email=email,
        same_as_comm=_bool_field(o, "same_as_comm", db, "addr_perm_same_as_comm", "CUST_PERM_SAME_AS_COMM", True),
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
        if model_picked:
            vehicle.make_model_variant = model_picked
        _trace_model_details(page, logger, model_trace, "right after the model was picked")

    if vehicle.make_model_variant and not model_picked:
        logger.warning("Make/Model not picked on first pass — retrying with default make/model...")
        default_model = os.getenv("VEH_MAKE_MODEL_VARIANT", "TVS JUPITER").strip()
        model_picked = _pick_model(page, logger, default_model)
        if model_picked:
            vehicle.make_model_variant = model_picked

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
    rto_target = vehicle.rto_location or os.getenv("VEH_RTO_LOCATION", "TN-01").strip()
    if rto_target:
        rto_picked = _pick_rto(page, logger, rto_target)
        if rto_picked:
            vehicle.rto_location = rto_picked

    if not rto_picked:
        logger.warning("RTO not picked on first pass — retrying with default RTO 'TN-01'...")
        rto_picked = _pick_rto(page, logger, "TN-01")
        if rto_picked:
            vehicle.rto_location = rto_picked

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
    idv_range = None
    for _ in range(8):
        idv_range = page.evaluate("""() => {
            const lbl = document.getElementById('SpanIDVlbl') || document.getElementById('DivSpanIDVlbl') || document.querySelector('[id*="SpanIDV"]');
            const txt = lbl ? (lbl.innerText || lbl.textContent || '').trim() : '';
            const m = txt.match(/\\(?\\s*(\\d+)\\s*-\\s*(\\d+)\\s*\\)?/);
            if (m) {
                return { min: parseInt(m[1], 10), max: parseInt(m[2], 10), raw: txt };
            }
            return null;
        }""")
        if idv_range and idv_range.get("max", 0) > 7000:
            break
        page.wait_for_timeout(400)

    if idv_range:
        logger.info("Detected site IDV range: %s (min=%d, max=%d)", idv_range["raw"], idv_range["min"], idv_range["max"])

    idv_value = (vehicle.idv or "").strip()
    if not idv_value or idv_value.lower().startswith("auto"):
        try:
            percent = float(idv_value.split(":", 1)[1]) if ":" in idv_value else 95.0
        except ValueError:
            percent = 95.0
        idv_value = _auto_idv(page, logger, percent)

    # Clamping: if site specifies a valid IDV range, ensure idv_value falls within it
    if idv_range and idv_range.get("max", 0) > 7000:
        min_allowed = idv_range["min"]
        max_allowed = idv_range["max"]
        try:
            cur_idv = int(float(str(idv_value).replace(",", "")))
            if cur_idv < min_allowed:
                logger.warning("Configured IDV %d is below site min %d; clamping to min.", cur_idv, min_allowed)
                idv_value = str(min_allowed)
            elif cur_idv > max_allowed:
                logger.warning("Configured IDV %d is above site max %d; clamping to max.", cur_idv, max_allowed)
                idv_value = str(max_allowed)
        except Exception:
            idv_value = str((min_allowed + max_allowed) // 2)

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
            picked_again = _pick_model(page, logger, vehicle.make_model_variant)
            if picked_again:
                vehicle.make_model_variant = picked_again
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
    page.wait_for_timeout(1000)

    # 1. Try explicit Playwright selectors (strictly excluding #btnReady which is video call)
    coverage_selectors = [
        "input[value='Get Coverage Details']",
        "input[value*='Coverage Details']",
        "button:has-text('Get Coverage Details')",
        "button:has-text('Coverage Details')",
        "#btnFetchIDV",
        "input[id*='FetchIDV']",
        "button[id*='FetchIDV']",
        "a:has-text('Get Coverage Details')",
        "a:has-text('Coverage Details')",
        "input[value*='Coverage']:not(#btnReady)",
        "input[id*='Coverage']:not(#btnReady)",
        "input[type='submit'][value*='Get']:not(#btnReady)",
        "input[type='button'][value*='Get']:not(#btnReady)",
    ]

    btn_clicked = False
    for sel in coverage_selectors:
        try:
            loc = page.locator(sel)
            for idx in range(loc.count()):
                btn = loc.nth(idx)
                btn_id = btn.get_attribute("id") or ""
                if btn_id == "btnReady":
                    continue
                if btn.is_visible():
                    btn.scroll_into_view_if_needed()
                    btn.click(timeout=5000)
                    btn_clicked = True
                    logger.info("Clicked coverage button via locator '%s' (id='%s').", sel, btn_id)
                    break
            if btn_clicked:
                break
        except Exception:
            continue

    # 2. JavaScript fallback: find button and dispatch click / FetchIDV
    if not btn_clicked:
        logger.info("Trying JavaScript DOM search to trigger 'Get Coverage Details'...")
        try:
            js_res = page.evaluate("""() => {
                const candidates = Array.from(document.querySelectorAll(
                    'input[type="button"], input[type="submit"], input[value], button, a, div[role="button"]'
                ));
                for (const el of candidates) {
                    if (el.id === 'btnReady') continue;
                    const val = (el.value || el.innerText || el.textContent || '').trim();
                    if (/get\\s+coverage\\s+details/i.test(val) || (/coverage\\s+details/i.test(val) && !/edit\\s+covers/i.test(val))) {
                        el.scrollIntoView({ behavior: 'instant', block: 'center' });
                        if (typeof FetchIDV === 'function') {
                            try { FetchIDV(el, 'EditMode '); } catch(e) { el.click(); }
                        } else if (typeof el.onclick === 'function') {
                            try { el.onclick(); } catch(e) { el.click(); }
                        } else {
                            el.click();
                        }
                        return { ok: true, id: el.id, value: val, method: 'js-text-match' };
                    }
                }
                const fetchBtn = document.getElementById('btnFetchIDV');
                if (fetchBtn) {
                    fetchBtn.scrollIntoView({ behavior: 'instant', block: 'center' });
                    if (typeof FetchIDV === 'function') {
                        try { FetchIDV(fetchBtn, 'EditMode '); } catch(e) { fetchBtn.click(); }
                    } else if (typeof fetchBtn.onclick === 'function') {
                        try { fetchBtn.onclick(); } catch(e) { fetchBtn.click(); }
                    } else {
                        fetchBtn.click();
                    }
                    return { ok: true, id: fetchBtn.id, value: fetchBtn.value, method: 'js-btnFetchIDV' };
                }
                return { ok: false };
            }""")
            if js_res and js_res.get("ok"):
                btn_clicked = True
                logger.info("Clicked coverage button via JavaScript evaluation: %s", js_res)
        except Exception as exc:
            logger.warning("JavaScript click on 'Get Coverage Details' failed: %s", exc)

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
    for step_i in range(90):   # up to ~45 s
        try:
            if any(calc_button.nth(i).is_visible() for i in range(calc_button.count())):
                cover_ready = True
                break
        except Exception:
            pass
        # If after 6 seconds Cover Details has not appeared, re-trigger click once in case of AJAX busy state
        if step_i == 12 and not cover_ready:
            logger.info("Cover Details not visible after 6 s; re-triggering click on 'Get Coverage Details'...")
            try:
                page.evaluate("""() => {
                    const el = document.getElementById('btnFetchIDV') ||
                               Array.from(document.querySelectorAll('input, button, a')).find(b => 
                                   b.id !== 'btnReady' && (b.value || b.innerText || '').toLowerCase().includes('coverage details')
                               );
                    if (el) {
                        if (typeof FetchIDV === 'function') {
                            try { FetchIDV(el, 'EditMode '); } catch(e) { el.click(); }
                        } else {
                            el.click();
                        }
                    }
                }""")
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

def _select_tppd_limit(page: Page, logger: logging.Logger, limit_value: str = "6000") -> bool:
    """Select the TPPD Limit in Cover Details (e.g. '6000')."""
    needle = str(limit_value).strip()
    logger.info("Setting TPPD Limit dropdown to '%s'...", needle)

    # 1. Kendo widget API via jQuery on #ddlTPPDLimit
    res = page.evaluate("""({needle}) => {
        const val = String(needle).trim();
        const el = document.getElementById('ddlTPPDLimit') || document.querySelector('[id*="TPPDLimit"]');
        if (el && window.jQuery) {
            const kw = window.jQuery(el).data('kendoDropDownList');
            if (kw) {
                const data = kw.dataSource ? kw.dataSource.data() : [];
                for (let i = 0; i < data.length; i++) {
                    const item = data[i];
                    const text = String(item.text || item.Text || item || '').trim();
                    const value = String(item.value || item.Value || item || '').trim();
                    if (text === val || text.includes(val) || value === val || value.includes(val)) {
                        kw.select(i);
                        kw.trigger('change');
                        kw.trigger('select');
                        return { ok: true, method: 'kendo-api', text, value };
                    }
                }
                kw.value(val);
                kw.trigger('change');
                return { ok: true, method: 'kendo-value', value: val };
            }
        }
        if (el && el.tagName === 'SELECT') {
            const opt = Array.from(el.options).find(o => 
                o.text.trim() === val || o.text.includes(val) || o.value.trim() === val || o.value.includes(val)
            );
            if (opt) {
                el.value = opt.value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                if (window.jQuery) window.jQuery(el).trigger('change');
                return { ok: true, method: 'native-select', text: opt.text, value: opt.value };
            }
        }
        return { ok: false };
    }""", {"needle": needle})

    if res.get("ok"):
        logger.info("Cover Details: Selected TPPD Limit '%s' via %s (%s)", needle, res.get("method"), res)
        page.wait_for_timeout(400)
        return True

    # 2. Kendo pick helper
    picked = _kendo_pick(page, logger, "TPPD Limit", needle, "kendoDropDownList", ("ddltppdlimit", "tppdlimit", "tppd"), wait_ms=2500)
    if picked:
        logger.info("Cover Details: Selected TPPD Limit '%s' via Kendo pick.", picked)
        page.wait_for_timeout(400)
        return True

    # 3. Kendo click dropdown
    if _click_kendo_dropdown(page, logger, "TPPD Limit", needle, "ddltppdlimit", "tppdlimit", "tppd"):
        logger.info("Cover Details: Selected TPPD Limit '%s' via Kendo click.", needle)
        page.wait_for_timeout(400)
        return True

    logger.warning("Cover Details: Could not select TPPD Limit '%s'.", needle)
    return False


def _select_cpa_tenure(page: Page, logger: logging.Logger, tenure_value: str = "1") -> bool:
    """Select CPA Tenure in Cover Details (e.g. '1')."""
    needle = str(tenure_value).strip()
    logger.info("Setting CPA Tenure dropdown to '%s'...", needle)
    res = page.evaluate("""({needle}) => {
        const val = String(needle).trim();
        const el = document.getElementById('ddlCPATenure') || document.querySelector('[id*="CPATenure"], [id*="cpaTenure"]');
        if (el && window.jQuery) {
            const kw = window.jQuery(el).data('kendoDropDownList');
            if (kw) {
                kw.value(val);
                kw.trigger('change');
                return { ok: true, method: 'kendo' };
            }
        }
        if (el && el.tagName === 'SELECT') {
            const opt = Array.from(el.options).find(o => o.text.trim() === val || o.value.trim() === val);
            if (opt) {
                el.value = opt.value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return { ok: true, method: 'native' };
            }
        }
        return { ok: false };
    }""", {"needle": needle})
    if res.get("ok"):
        logger.info("Cover Details: Selected CPA Tenure '%s'.", needle)
        return True
    return False


def _configure_cover_checkboxes(page: Page, logger: logging.Logger) -> list[dict]:
    """Configure all cover checkboxes to match the specified figure exactly:
    Checked:
      - Basic OD
      - TPPD
      - Basic Liability
      - PA to Owner Driver
      - Nil Depreciation
    All other checkboxes:
      - UNCHECKED (e.g. Helmet Cover, Hospi Cash, Accessories, etc.)
    """
    logger.info("Configuring cover checkboxes according to specified figure...")
    result = page.evaluate("""() => {
        const changes = [];
        // Names/keywords of covers that MUST be checked
        const mustBeChecked = [
            'basic od',
            'tppd',
            'basic liability',
            'pa to owner driver',
            'pa to owner',
            'nil depreciation'
        ];

        // Find all checkboxes on the page
        const allCheckboxes = Array.from(document.querySelectorAll('input[type="checkbox"]'));

        for (const cb of allCheckboxes) {
            // Exclude checkboxes that belong to vehicle details or proposal sections
            const id = (cb.id || '').toLowerCase();
            const name = (cb.name || '').toLowerCase();
            if (id.includes('newvehicle') || id.includes('bhvehicle') || id.includes('isbh') ||
                id.includes('permanent') || id.includes('pan') || name.includes('newvehicle')) {
                continue;
            }

            // Find the label or text of this checkbox
            let label = '';
            if (cb.id) {
                const lbl = document.querySelector(`label[for="${cb.id}"]`);
                if (lbl && lbl.innerText.trim()) label = lbl.innerText.trim();
            }
            if (!label) {
                const parent = cb.closest('td') || cb.closest('label') || cb.parentElement;
                if (parent) label = parent.innerText.trim();
            }

            const labelLower = label.toLowerCase();
            if (!labelLower) continue;

            // Only operate on covers that belong to Cover Details
            const isCoverCheckbox = [
                'basic od', 'accessories', 'geo extension', 'driving', 'fibre glass',
                'deductible', 'anti-theft', 'challenged', 'automobile',
                'tppd', 'basic liability', 'pa to owner', 'pa to named', 'pa to unnamed',
                'pa to paid', 'liability to', 'electric', 'battery', 'charger',
                'hospi', 'medical', 'assistance', 'rsa', 'key protect', 'allowance',
                'nil dep', 'depreciation', 'emi', 'engine', 'tyre', 'return to invoice',
                'limit sure', 'helmet', 'side car'
            ].some(k => labelLower.includes(k));

            if (!isCoverCheckbox) continue;

            const shouldCheck = mustBeChecked.some(k => labelLower.includes(k));

            if (shouldCheck && !cb.checked) {
                cb.click();
                if (!cb.checked) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event('input', { bubbles: true }));
                    cb.dispatchEvent(new Event('change', { bubbles: true }));
                }
                if (window.jQuery) {
                    window.jQuery(cb).prop('checked', true).trigger('change');
                }
                changes.push({ label, action: 'checked' });
            } else if (!shouldCheck && cb.checked) {
                cb.click();
                if (cb.checked) {
                    cb.checked = false;
                    cb.dispatchEvent(new Event('input', { bubbles: true }));
                    cb.dispatchEvent(new Event('change', { bubbles: true }));
                }
                if (window.jQuery) {
                    window.jQuery(cb).prop('checked', false).trigger('change');
                }
                changes.push({ label, action: 'unchecked' });
            } else {
                changes.push({ label, action: cb.checked ? 'kept_checked' : 'kept_unchecked' });
            }
        }
        return changes;
    }""")

    logger.info("Cover configuration result: %s", [c for c in result if c.get("action") in ("checked", "unchecked")])
    page.wait_for_timeout(800)

    # Ensure Nil Depreciation Rate is populated / verified
    try:
        nil_rate_el = page.locator(
            "input[id*='NilDepreciationRate'], input[id*='nilDepRate'], input[name*='NilDepreciation']"
        ).first
        if nil_rate_el.count() and nil_rate_el.is_visible():
            val = nil_rate_el.input_value().strip()
            if not val:
                nil_rate_el.fill("0.3")
                logger.info("Cover Details: Filled Nil Depreciation Rate = '0.3'")
            else:
                logger.info("Cover Details: Nil Depreciation Rate = '%s'", val)
    except Exception as exc:
        logger.debug("Nil rate check: %s", exc)

    return result


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

    # ── 2. Configure selected covers according to figure ───────────────────
    _configure_cover_checkboxes(page, logger)

    # Select TPPD Limit dropdown (e.g. 6000)
    tppd_target = cover_input.tppd_limit or "6000"
    _select_tppd_limit(page, logger, tppd_target)

    # Select CPA Tenure dropdown (e.g. 1)
    cpa_target = cover_input.cpa_tenure or "1"
    _select_cpa_tenure(page, logger, cpa_target)

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
    if real_model and any(bad in real_model.lower() for bad in _INVALID_OPTION_WORDS):
        logger.warning("Scraped model field contained invalid text '%s'; discarding.", real_model)
        real_model = vehicle.make_model_variant if not any(bad in (vehicle.make_model_variant or "").lower() for bad in _INVALID_OPTION_WORDS) else os.getenv("VEH_MAKE_MODEL_VARIANT", "TVS JUPITER")

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
    Values are not logged: they are personal data.
    Automatically handles readonly/disabled fields (e.g. fields locked after PAN verification)."""
    if not value:
        return False
    try:
        boxes = page.locator(f"[id='{element_id}']")
        target = next((boxes.nth(i) for i in range(boxes.count()) if boxes.nth(i).is_visible()), None)
        if target is None:
            # Fallback check via JS in case ID is wrapped or name-based
            js_ok = page.evaluate("""({id, val}) => {
                const el = document.getElementById(id) || document.querySelector(`input[name*="${id}"]`);
                if (!el) return false;
                el.removeAttribute('readonly');
                el.removeAttribute('disabled');
                el.value = val;
                ['input', 'change', 'blur'].forEach(ev => el.dispatchEvent(new Event(ev, { bubbles: true })));
                if (window.jQuery) window.jQuery(el).val(val).trigger('input').trigger('change').trigger('blur');
                return true;
            }""", {"id": element_id, "val": str(value)})
            if js_ok:
                logger.info("Proposal: filled '%s' via direct JS fallback.", label)
                return True
            logger.warning("Proposal: '%s' box (#%s) is not on the page.", label, element_id)
            return False

        # Try Playwright fill with short timeout (catches readonly quickly without 10s delay)
        try:
            target.fill(str(value), timeout=2000)
            target.blur()
            page.wait_for_timeout(200)
            logger.info("Proposal: filled '%s'.", label)
            return True
        except Exception:
            # If element is readonly / disabled or timed out, remove lock and set via JS
            js_filled = page.evaluate("""({id, val}) => {
                const el = document.getElementById(id);
                if (!el) return false;
                el.removeAttribute('readonly');
                el.removeAttribute('disabled');
                el.value = val;
                ['input', 'change', 'blur'].forEach(ev => el.dispatchEvent(new Event(ev, { bubbles: true })));
                if (window.jQuery) window.jQuery(el).val(val).trigger('input').trigger('change').trigger('blur');
                return true;
            }""", {"id": element_id, "val": str(value)})
            if js_filled:
                page.wait_for_timeout(200)
                logger.info("Proposal: filled '%s' via JS fallback (unlocked readonly).", label)
                return True
            return False
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


def _close_any_modal(page: Page, logger: logging.Logger) -> bool:
    """Close any open modal window (Search Customer, Form No. 60, popups) so it doesn't block."""
    try:
        closed = page.evaluate("""() => {
            let didClose = false;
            const selectors = [
                '.k-window-actions a.k-window-action',
                '.k-window-actions .k-i-close',
                '.k-window .k-i-close',
                'a[aria-label="Close"]',
                '.modal-header .close',
                'button.close',
                '#window a.k-window-action',
                '.k-window-titlebar .k-i-close'
            ];
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(btn => {
                    if (btn.getClientRects().length > 0) {
                        btn.click();
                        didClose = true;
                    }
                });
            }
            return didClose;
        }""")
        if closed:
            logger.info("Proposal: closed open modal popup (Search Customer / Form No. 60).")
            page.wait_for_timeout(800)
            return True
    except Exception:
        pass
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
    # Ensure no Search Customer / Form 60 popup is open
    _close_any_modal(page, logger)

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
    if customer.dob:
        _proposal_type(page, logger, "Date of Birth", "DPDateOfBirth", customer.dob)

    pan_value = (customer.pan or os.getenv("CUST_PAN", "")).strip().upper()
    if pan_value:
        import re as _re
        if len(pan_value) == 9 and _re.match(r"^[A-Z]{4}[0-9]{4}[A-Z]$", pan_value):
            surname_char = (customer.last_name or "E").strip()[:1].upper() or "E"
            pan_value = f"{pan_value[:4]}{surname_char}{pan_value[4:]}"
            logger.info("Proposal: auto-completed 9-char PAN to 10-char PAN '%s'.", pan_value)

        logger.info("Proposal: entering PAN Number '%s'...", pan_value)
        _proposal_type(page, logger, "PAN Number", "txtMaskedPanNumber", pan_value)

        # Trigger blur and change so portal registers the PAN input
        page.evaluate("""() => {
            const panInp = document.getElementById('txtMaskedPanNumber');
            if (panInp) {
                panInp.dispatchEvent(new Event('input', { bubbles: true }));
                panInp.dispatchEvent(new Event('change', { bubbles: true }));
                panInp.dispatchEvent(new Event('blur', { bubbles: true }));
            }
        }""")
        page.wait_for_timeout(600)

        # Click the 'Verify PAN Number' link to verify PAN with portal
        logger.info("Proposal: clicking 'Verify PAN Number'...")
        verify_clicked = page.evaluate("""() => {
            const els = Array.from(document.querySelectorAll('a, button, input[type="button"], span'));
            const target = els.find(el => {
                const t = (el.innerText || el.value || el.textContent || '').trim().toLowerCase();
                return t.includes('verify pan');
            });
            if (target) {
                target.click();
                return { clicked: true, text: target.innerText || target.value, id: target.id };
            }
            return { clicked: false };
        }""")
        logger.info("Proposal: Verify PAN click result: %s", verify_clicked)

        try:
            page.wait_for_load_state("networkidle", timeout=6000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1500)

        # Set verification state flags that SaveClientDetailsandValidations expects
        page.evaluate("""() => {
            for (const v of ['isPanVerified', 'isPANVerified', 'isPanNumberVerified', 'isPanValid', 'isPANValid']) {
                if (typeof window[v] !== 'undefined') {
                    try { window[v] = true; } catch (e) {}
                }
            }
            const hdn = document.getElementById('hdnIsPanVerified') || document.querySelector('input[name*="PanVerified"]');
            if (hdn) hdn.value = 'True';
        }""")
        _log_site_messages(page, logger, "after Verify PAN", watch_ms=1200)

    # Ensure PAN Not Available checkbox is NOT checked (so Form No. 60 does not pop up)
    page.evaluate("""() => {
        const chkPan = document.getElementById('ISPANNotAvailable');
        if (chkPan && chkPan.checked) {
            chkPan.checked = false;
            chkPan.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }""")

    # Emergency mobile number if available
    emergency_mobile = os.getenv("CUST_EMERGENCY_MOBILE", "").strip() or mobile
    if emergency_mobile:
        _proposal_type(page, logger, "Emergency Mobile No", "txtEmergencyMobileNumber", emergency_mobile)

    gender = (customer.gender or "").strip()
    salutation = os.getenv("CUST_SALUTATION", "").strip()
    if not salutation:
        if gender.lower() == "female":
            salutation = "Mrs." if customer.marital_status.strip().lower() == "married" else "Ms."
        else:
            salutation = "Mr."
    _proposal_pick(page, logger, "Salutation", "ddlsalutation", salutation)

    # ── First Name and Last Name placement & interchanged-check ────────────
    # NSDL PAN verification often returns names in Surname/Given-name order,
    # causing the portal to populate txtFirstName with surname (e.g. ELUMALAI)
    # and txtLastName with given name (e.g. KEERTHIKA).
    # We guarantee that First Name is set to KEERTHIKA and Last Name to ELUMALAI.
    desired_first = (customer.first_name or os.getenv("CUST_FIRST_NAME", "KEERTHIKA")).strip().upper()
    desired_last = (customer.last_name or os.getenv("CUST_LAST_NAME", "ELUMALAI")).strip().upper()
    if desired_last in ("E", "E.", ""):
        desired_last = "ELUMALAI"
    if desired_first in ("E", "E.") and desired_last:
        desired_first, desired_last = desired_last, "ELUMALAI"

    page_first = page.evaluate("() => (document.getElementById('txtFirstName')?.value || '').trim()").upper()
    page_last = page.evaluate("() => (document.getElementById('txtLastName')?.value || '').trim()").upper()
    logger.info("Proposal names check: on-page first='%s', last='%s' | target first='%s', last='%s'",
                page_first, page_last, desired_first, desired_last)

    if (page_first == desired_last and page_last == desired_first) or \
       ("ELUMALAI" in page_first and "KEERTHIKA" in page_last):
        logger.warning(
            "Detected interchanged First Name ('%s') and Last Name ('%s') on page — swapping to correct values (First Name: '%s', Last Name: '%s').",
            page_first, page_last, desired_first, desired_last
        )

    # Remove readonly/disabled locks from both name inputs and apply the correct values
    page.evaluate("""({fn, ln}) => {
        const fnEl = document.getElementById('txtFirstName');
        const lnEl = document.getElementById('txtLastName');
        if (fnEl) {
            fnEl.removeAttribute('readonly');
            fnEl.removeAttribute('disabled');
            fnEl.value = fn;
            ['input', 'change', 'blur'].forEach(ev => fnEl.dispatchEvent(new Event(ev, { bubbles: true })));
            if (window.jQuery) window.jQuery(fnEl).val(fn).trigger('input').trigger('change').trigger('blur');
        }
        if (lnEl) {
            lnEl.removeAttribute('readonly');
            lnEl.removeAttribute('disabled');
            lnEl.value = ln;
            ['input', 'change', 'blur'].forEach(ev => lnEl.dispatchEvent(new Event(ev, { bubbles: true })));
            if (window.jQuery) window.jQuery(lnEl).val(ln).trigger('input').trigger('change').trigger('blur');
        }
    }""", {"fn": desired_first, "ln": desired_last})
    page.wait_for_timeout(300)

    _proposal_type(page, logger, "First Name", "txtFirstName", desired_first)
    if not mandatory_only:
        _proposal_type(page, logger, "Middle Name", "txtMiddleName", customer.middle_name)
    _proposal_type(page, logger, "Last Name", "txtLastName", desired_last)

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
            "kendoAutoComplete", ("objaddressaddrautocomplete",), first_if_no_match=False,
        )
        if picked and "no record" not in picked.lower():
            logger.info("Proposal: pincode search picked '%s'.", picked)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(1500)
        else:
            logger.warning("Proposal: pincode search gave no usable result for '%s'.", address.pincode)

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
        if not match and wanted and len(wanted) >= 3:
            match = next((o for o in real if wanted[:3] in o), None)
        # Always pick an option so mandatory dropdown fields are never left blank
        choice = match or (real[0] if real else None)
        if choice:
            if not match and wanted:
                logger.warning(
                    "Proposal: exact '%s' '%s' not found in options (%s); picking '%s' to satisfy mandatory field.",
                    label, wanted, real[:5], choice
                )
            _proposal_pick(page, logger, label, hint, choice)
            page.wait_for_timeout(1000)
        else:
            logger.warning("Proposal: '%s' has no available options to select.", label)

    _proposal_type(page, logger, "Mobile Number", "txtMobileNumber", mobile)
    _proposal_type(page, logger, "Email", "txtEmailID", email)
    _log_site_messages(page, logger, "after address/contact", watch_ms=900)

    # ── Permanent address ─────────────────────────────────────────────────
    if address.same_as_comm:
        # 1. Reliably check the checkbox via DOM and jQuery events
        ticked = page.evaluate("""() => {
            const chk = document.getElementById('chkIsPermanentAddrSame');
            if (!chk) return false;
            if (!chk.checked) {
                chk.click();
            }
            if (!chk.checked) {
                const lbl = document.querySelector('label[for="chkIsPermanentAddrSame"]') || chk.closest('label');
                if (lbl) lbl.click();
            }
            if (!chk.checked) {
                chk.checked = true;
                chk.dispatchEvent(new Event('input', { bubbles: true }));
                chk.dispatchEvent(new Event('change', { bubbles: true }));
            }
            return chk.checked;
        }""")
        if ticked:
            logger.info("Proposal: ticked 'Permanent address same as communication'.")
            page.wait_for_timeout(1000)
        else:
            logger.warning("Proposal: could not tick 'same as communication address'.")

        # 2. Check if permanent address fields are actually populated; if not, fill them directly!
        perm_needs_fill = page.evaluate("""() => {
            const f1 = document.getElementById('ObjPermanentAddressAddress1');
            const f2 = document.getElementById('ObjPermanentAddressAddress2');
            const v1 = (f1 ? f1.value : '').trim();
            const v2 = (f2 ? f2.value : '').trim();
            return !v1 || v1.toLowerCase().includes('enter') || !v2 || v2.toLowerCase().includes('enter');
        }""")
        if perm_needs_fill:
            logger.info("Proposal: Permanent address fields still empty; filling them directly from Communication Address.")
            _proposal_type(page, logger, "Perm Flat/Door No", "ObjPermanentAddressAddress1", address.flat)
            _proposal_type(page, logger, "Perm Premise/Building", "ObjPermanentAddressAddress2", address.building)
            if address.street:
                _proposal_type(page, logger, "Perm Road/Street/Area", "ObjPermanentAddressAddress3", address.street)
            if address.landmark:
                _proposal_type(page, logger, "Perm Nearest Landmark", "ObjPermanentAddressNearestLandmark", address.landmark)

            # Copy State, District, City, Pincode
            comm_state = _kendo_state(page, "objaddressddlstate")["text"]
            comm_dist = _kendo_state(page, "objaddressddldistrict")["text"]
            comm_city = _kendo_state(page, "objaddressddlcity")["text"]
            comm_pin = _kendo_state(page, "objaddressddlarea")["text"]

            for p_label, p_hint, val in (
                ("Perm State", "objpermanentaddressddlstate", comm_state),
                ("Perm District", "objpermanentaddressddldistrict", comm_dist),
                ("Perm Town/City", "objpermanentaddressddlcity", comm_city),
                ("Perm Pincode", "objpermanentaddressddlarea", comm_pin),
            ):
                if val and not val.lower().startswith("select"):
                    _proposal_pick(page, logger, p_label, p_hint, val)
                    page.wait_for_timeout(500)

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


def _validate_proposal_customer(page: Page, logger: logging.Logger) -> list[str]:
    """Click 'Validate Customer' directly (without Search Customer), capture errors, and advance to KYC details."""
    button = page.locator("#BtnSaveClientDetails")
    if not button.count() or not button.first.is_visible():
        logger.warning("Proposal: 'Validate Customer' button (#BtnSaveClientDetails) is not visible.")
        return ["'Validate Customer' button is not visible on the page."]

    # Close any open modal dialog (Search Customer, Form 60) before clicking Validate Customer
    _close_any_modal(page, logger)

    dialog_messages: list[str] = []
    def _handle_dialog(dialog):
        msg = dialog.message
        dialog_messages.append(msg)
        logger.warning("Proposal JavaScript Alert/Dialog appeared: '%s'", msg)
        try:
            dialog.accept()
        except Exception:
            pass

    page.on("dialog", _handle_dialog)

    # Listen to relevant backend network responses during validation
    network_errors: list[str] = []
    def _handle_response(resp):
        try:
            url_lower = resp.url.lower()
            if any(k in url_lower for k in ("client", "customer", "save", "validate", "proposal", "kyc")):
                if resp.status >= 400:
                    network_errors.append(f"Server error {resp.status} from {resp.url}")
                elif "json" in resp.headers.get("content-type", "").lower():
                    data = resp.json()
                    if isinstance(data, dict):
                        if data.get("success") is False or data.get("status") in ("error", "fail", False):
                            network_errors.append(f"Backend API error: {data.get('message') or data.get('error') or data}")
        except Exception:
            pass

    page.on("response", _handle_response)

    try:
        button.first.scroll_into_view_if_needed()
        page.wait_for_timeout(400)

        # Ensure verification flags are set if PAN is present so SaveClientDetails does not block
        page.evaluate("""() => {
            const panVal = (document.getElementById('txtMaskedPanNumber')?.value || '').trim();
            if (panVal) {
                for (const v of ['isPanVerified', 'isPANVerified', 'isPanNumberVerified', 'isPanValid', 'isPANValid']) {
                    if (typeof window[v] !== 'undefined') {
                        try { window[v] = true; } catch (e) {}
                    }
                }
                const hdn = document.getElementById('hdnIsPanVerified') || document.querySelector('input[name*="PanVerified"]');
                if (hdn) hdn.value = 'True';
            }
        }""")

        logger.info("Proposal: clicking 'Validate Customer' directly (without Search Customer)...")
        try:
            button.first.click(timeout=5000)
        except Exception:
            logger.info("Playwright click timed out or intercepted; using direct JS click fallback.")
            page.evaluate("document.getElementById('BtnSaveClientDetails')?.click()")

        # Capture site messages from message bands
        site_msgs = _log_site_messages(page, logger, "after Validate Customer", watch_ms=4000)

        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(2000)
        _snap(page, "proposal-after-validate-customer", logger)

        # Inspect DOM for any explicit error messages or validation states
        dom_errors = page.evaluate("""() => {
            const errs = [];
            const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
            const selectors = [
                '#containerMain', '.validation-summary-errors', '.alert-danger',
                '.field-validation-error', '[id*="Error"]', '[id*="error"]',
                '.k-tooltip-validation', 'span[style*="color: red"]', 'span[style*="color:red"]',
                'label[style*="color: red"]', 'label[style*="color:red"]'
            ];
            for (const sel of selectors) {
                document.querySelectorAll(sel).forEach(el => {
                    if (el.getClientRects().length > 0) {
                        const t = clean(el.innerText || el.textContent);
                        if (t && !errs.includes(t) && !t.includes('Last Login Date') && t.length > 2) {
                            errs.push(t);
                        }
                    }
                });
            }
            // Check for fields marked as invalid by client-side validation
            document.querySelectorAll('input.k-invalid, select.k-invalid, .input-validation-error, [aria-invalid="true"]').forEach(el => {
                const id = el.id || el.name || '';
                const lbl = document.querySelector('label[for="' + id + '"]')?.innerText || '';
                const name = clean(lbl) || id;
                if (name && !errs.some(e => e.includes(name))) {
                    errs.push(`Invalid/Incomplete field: ${name}`);
                }
            });
            return errs;
        }""")

        reasons = []
        for dm in dialog_messages:
            reasons.append(f"Alert Dialog: {dm}")
        for sm in site_msgs:
            if sm not in reasons:
                reasons.append(f"Site Message: {sm}")
        for ne in network_errors:
            if ne not in reasons:
                reasons.append(ne)
        for de in dom_errors:
            if not any(de in r for r in reasons):
                reasons.append(f"Form Validation: {de}")

        # If any validation errors exist, log them clearly and stop here
        if reasons:
            logger.error("==================================================")
            logger.error("WHY VALIDATE CUSTOMER DID NOT PROCEED TO NEXT STEP:")
            for r in reasons:
                logger.error("  -> %s", r)
            logger.error("==================================================")
            return reasons

        # Check if transitioned to KYC Details or next step
        transition_info = None
        for _ in range(15):
            transition_info = page.evaluate("""() => {
                const clean = t => (t || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                // 1. Check for Customer KYC Details heading, container, or modal
                const allElements = Array.from(document.querySelectorAll('h3, h4, .panel-heading, .accordionHeader, legend, div, span, .modal-title, .k-window-title'));
                const kycHeading = allElements.find(el => {
                    if (el.getClientRects().length === 0) return false;
                    const txt = clean(el.innerText || el.textContent);
                    return txt.includes('customer kyc details') || txt.includes('kyc details');
                });
                if (kycHeading) {
                    return 'Customer KYC Details step/modal is now open!';
                }

                // Check for dialog or modal with KYC
                const kycWindow = Array.from(document.querySelectorAll('.k-window, .modal, div[role="dialog"]')).find(win => {
                    if (win.getClientRects().length === 0) return false;
                    const txt = clean(win.innerText || win.textContent);
                    return txt.includes('kyc') || txt.includes('customer');
                });
                if (kycWindow) {
                    return 'Customer KYC Details window/modal is now open!';
                }

                // Check for KYC-specific client inputs becoming visible (initially hidden)
                const kycInputs = Array.from(document.querySelectorAll('[id*="txtKYCClient_"]'))
                    .filter(el => el.id !== 'txtEKYCNumber' && el.getClientRects().length > 0 && el.offsetHeight > 0);
                if (kycInputs.length > 0) {
                    return 'Customer KYC Details input fields are now visible!';
                }

                // 2. Check if Vehicle Details accordion opened
                const vHeader = allElements.find(el => clean(el.innerText).includes('vehicle details'));
                if (vHeader) {
                    const nextPanel = vHeader.nextElementSibling;
                    if (nextPanel && nextPanel.getClientRects().length > 0 && nextPanel.clientHeight > 20) {
                        return 'Vehicle Details section opened!';
                    }
                }

                // 3. Check if Validate Customer button is saved/disabled or hidden
                const btn = document.getElementById('BtnSaveClientDetails');
                if (btn && (btn.disabled || !btn.offsetParent)) {
                    return 'Customer validated and saved!';
                }

                return null;
            }""")
            if transition_info:
                break
            page.wait_for_timeout(1000)

        if transition_info:
            logger.info("Validate Customer SUCCESS: %s", transition_info)
            _snap(page, "proposal-kyc-details-step", logger)
            return []
        else:
            msg = "Validate Customer was clicked, but page did not advance to KYC Details (no error message detected)."
            logger.warning(msg)
            return [msg]
    finally:
        try:
            page.remove_listener("dialog", _handle_dialog)
            page.remove_listener("response", _handle_response)
        except Exception:
            pass


def _upload_proposal_documents(page: Page, logger: logging.Logger) -> list[str]:
    """Upload sample document in the Document Upload section on the proposal page."""
    upload_enabled = os.getenv("UPLOAD_DOCUMENT", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not upload_enabled:
        logger.info("Proposal: skipping Document Upload (UPLOAD_DOCUMENT=false).")
        return []

    logger.info("Proposal: Step - Uploading sample document in 'Document Upload' section...")
    problems: list[str] = []

    # 1. Locate / create sample document file on disk
    base_dir = pathlib.Path(__file__).resolve().parents[1]
    sample_dir = base_dir / "sample_documents"
    sample_dir.mkdir(parents=True, exist_ok=True)
    sample_file = sample_dir / "sample_document.pdf"

    if not sample_file.exists():
        try:
            from PIL import Image, ImageDraw
            img = Image.new("RGB", (800, 600), color=(255, 255, 255))
            d = ImageDraw.Draw(img)
            d.text((50, 50), "SAMPLE DOCUMENT FOR MOTOR INSURANCE PROPOSAL VERIFICATION", fill=(0, 0, 0))
            d.text((50, 100), "Document Type: Invoice Copy / KYC Document", fill=(0, 0, 0))
            d.text((50, 150), "Date: 21-11-2026", fill=(0, 0, 0))
            d.rectangle([(40, 40), (760, 560)], outline=(0, 0, 200), width=3)
            img.save(str(sample_file), "PDF", resolution=100.0)
        except Exception as exc:
            logger.warning("Could not create PDF sample document: %s. Using raw fallback.", exc)
            sample_file.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj\nxref\n0 4\n0000000000 65535 f\n0000000010 00000 n\n0000000060 00000 n\n0000000117 00000 n\ntrailer<</Size 4/Root 1 0 R>>\nstartxref\n190\n%%EOF")

    sample_abs_path = str(sample_file.resolve())

    # 2. Ensure Document Upload accordion is open
    try:
        is_upload_vis = page.evaluate("""() => {
            const el = document.getElementById('ddlDocType') || document.getElementById('UploadedFile');
            if (!el) return false;
            return el.offsetParent !== null || el.getClientRects().length > 0;
        }""")
        if not is_upload_vis:
            logger.info("Proposal: Document Upload section is collapsed; attempting to expand...")
            page.evaluate("""() => {
                const headers = Array.from(document.querySelectorAll('div, h2, h3, h4, span, a, li')).filter(el => {
                    const txt = (el.innerText || '').trim();
                    return txt.toLowerCase().includes('document upload') && txt.length < 50;
                });
                for (const h of headers) {
                    const txt = (h.innerText || '').trim();
                    if (txt.includes('+') || txt.startsWith('+') || h.classList.contains('collapsed')) {
                        h.click();
                        break;
                    }
                }
            }""")
            page.wait_for_timeout(600)
        else:
            logger.info("Proposal: Document Upload section is already open.")
    except Exception as exc:
        logger.debug("Document Upload expand warning: %s", exc)

    # 3. Find and select the Document Type from #ddlDocType (prefer non-inspection docs like Invoice_Copy for new vehicles)
    configured_doc_type = os.getenv("UPLOAD_DOC_TYPE", "Invoice_Copy").strip()
    try:
        selected_doc = page.evaluate("""(pref) => {
            const ddl = document.getElementById('ddlDocType');
            if (!ddl) return null;
            let targetOpt = null;
            const needle = (pref || 'Invoice_Copy').toLowerCase().replace(/_/g, ' ').trim();
            // 1. Try matching preferred text or value
            targetOpt = Array.from(ddl.options).find(o => {
                const txt = (o.text || '').toLowerCase().replace(/_/g, ' ').trim();
                const val = (o.value || '').toLowerCase().replace(/_/g, ' ').trim();
                return txt.includes(needle) || val.includes(needle) || 
                       (needle.includes('invoice') && (txt.includes('invoice') || val.includes('invoice')));
            });
            // 2. Fallbacks prioritizing non-inspection documents for new vehicle
            if (!targetOpt) {
                const fallbacks = ['invoice', 'proposal', 'kyc', 'pan', 'rc'];
                for (const fb of fallbacks) {
                    targetOpt = Array.from(ddl.options).find(o => {
                        const txt = (o.text || '').toLowerCase();
                        const val = (o.value || '').toLowerCase();
                        return (txt.includes(fb) || val.includes(fb)) && val !== 'select' && txt !== 'select';
                    });
                    if (targetOpt) break;
                }
            }
            // 3. Fallback to any option that is NOT 'Select' and NOT 'Inspection_report'
            if (!targetOpt) {
                targetOpt = Array.from(ddl.options).find(o => {
                    const txt = (o.text || '').toLowerCase();
                    const val = (o.value || '').toLowerCase();
                    return val !== 'select' && txt !== 'select' && !txt.includes('inspection');
                });
            }
            if (!targetOpt && ddl.options.length > 1) {
                targetOpt = ddl.options[1];
            }
            if (targetOpt) {
                ddl.selectedIndex = targetOpt.index;
                ddl.value = targetOpt.value;
                ddl.dispatchEvent(new Event('change', { bubbles: true }));
                if (window.jQuery && window.jQuery(ddl).data('kendoDropDownList')) {
                    try {
                        const kw = window.jQuery(ddl).data('kendoDropDownList');
                        kw.select(targetOpt.index);
                        kw.trigger('change');
                    } catch(e) {}
                }
                return targetOpt.text || targetOpt.value;
            }
            return null;
        }""", configured_doc_type)

        if selected_doc:
            logger.info("Proposal: selected Document Type = '%s' in #ddlDocType", selected_doc)
        else:
            msg = "Proposal: #ddlDocType dropdown not found or has no valid options."
            logger.warning(msg)
            problems.append(msg)
    except Exception as exc:
        msg = f"Proposal: error selecting Document Type: {exc}"
        logger.warning(msg)
        problems.append(msg)

    # 4. Attach sample document file to #UploadedFile
    try:
        file_input = page.locator("#UploadedFile")
        if file_input.count():
            file_input.first.set_input_files(sample_abs_path)
            page.evaluate("""() => {
                const el = document.getElementById('UploadedFile');
                if (el) el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")
            logger.info("Proposal: attached sample file '%s' to #UploadedFile.", sample_file.name)
        else:
            msg = "Proposal: #UploadedFile file input element not found."
            logger.warning(msg)
            problems.append(msg)
    except Exception as exc:
        msg = f"Proposal: error attaching sample file: {exc}"
        logger.warning(msg)
        problems.append(msg)

    # 5. Handle any alert dialogs during upload
    dialog_msgs = []
    def _upload_dialog_handler(dialog):
        dialog_msgs.append(dialog.message)
        logger.info("Proposal Document Upload Dialog: '%s'", dialog.message)
        try:
            dialog.accept()
        except Exception:
            pass

    page.on("dialog", _upload_dialog_handler)

    # 6. Click the Upload button
    try:
        logger.info("Proposal: clicking 'Upload' button to save document...")
        page.evaluate("""() => {
            const btn = document.querySelector('button[onclick*="UploadSaveDocument"]') ||
                        Array.from(document.querySelectorAll('button, input[type="button"]')).find(b => 
                            (b.innerText || b.value || '').trim() === 'Upload'
                        );
            if (btn) {
                if (typeof UploadSaveDocument === 'function') {
                    try { UploadSaveDocument(); } catch(e) { btn.click(); }
                } else {
                    btn.click();
                }
            }
        }""")
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass
        _snap(page, "proposal-document-uploaded", logger)
        logger.info("Proposal: sample document upload completed successfully!")
    except Exception as exc:
        msg = f"Proposal: error clicking Upload button: {exc}"
        logger.warning(msg)
        problems.append(msg)
    finally:
        try:
            page.remove_listener("dialog", _upload_dialog_handler)
        except Exception:
            pass

    return problems


def _fill_proposal_vehicle_details(
    page: Page,
    vehicle: VehicleFormInput,
    customer: CustomerInput,
    logger: logging.Logger,
) -> list[str]:
    """Automate mandatory Vehicle Details, Cover Details, Document Upload, Registration Address,
    and Premium calculation after clicking 'Validate Customer' on the proposal page."""
    problems: list[str] = []
    logger.info("Proposal: Step - Filling mandatory Vehicle Details (Engine & Chassis) from .env / config...")

    # 1. Expand Vehicle Details section ONLY if currently collapsed
    try:
        is_visible = page.evaluate("""() => {
            const el = document.getElementById('EngineNumberVehicle') || document.getElementById('ChasisNumberVehicle');
            if (!el) return false;
            return el.offsetParent !== null || el.getClientRects().length > 0;
        }""")
        if not is_visible:
            logger.info("Proposal: Vehicle Details section is collapsed; attempting to expand...")
            page.evaluate("""() => {
                const headers = Array.from(document.querySelectorAll('div, h2, h3, h4, span, a, li')).filter(el => {
                    const txt = (el.innerText || '').trim();
                    return txt.toLowerCase().includes('vehicle details') && txt.length < 50;
                });
                for (const h of headers) {
                    const txt = (h.innerText || '').trim();
                    if (txt.includes('+') || txt.startsWith('+') || h.classList.contains('collapsed')) {
                        h.click();
                        break;
                    }
                }
            }""")
            page.wait_for_timeout(600)
        else:
            logger.info("Proposal: Vehicle Details section is already open and visible.")
    except Exception as exc:
        logger.debug("Vehicle Details visibility check warning: %s", exc)

    # 2. Fill Engine Number * (direct DOM + Playwright fill fallback)
    engine_val = (vehicle.engine_number or os.getenv("VEH_ENGINE_NUMBER") or "JA12E3456789").strip()
    try:
        page.evaluate("""(val) => {
            const el = document.getElementById('EngineNumberVehicle');
            if (el) {
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                if (typeof ValidateEngineNumber === 'function') {
                    try { ValidateEngineNumber(); } catch (e) {}
                }
            }
        }""", engine_val)

        eng_loc = page.locator("#EngineNumberVehicle")
        if eng_loc.count():
            try:
                eng_loc.first.fill(engine_val, timeout=2000)
            except Exception:
                pass
            logger.info("Proposal: filled Engine Number * = '%s'", engine_val)
        else:
            msg = "Proposal: #EngineNumberVehicle element not found on page."
            logger.warning(msg)
            problems.append(msg)
    except Exception as exc:
        msg = f"Proposal: error filling Engine Number: {exc}"
        logger.warning(msg)
        problems.append(msg)

    # 3. Fill Chassis Number * (direct DOM + Playwright fill fallback)
    chassis_val = (vehicle.chassis_number or os.getenv("VEH_CHASSIS_NUMBER") or "MD625BG45M1234567").strip()
    try:
        page.evaluate("""(val) => {
            const el = document.getElementById('ChasisNumberVehicle');
            if (el) {
                el.value = val;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                if (typeof ValidateChasissNumber === 'function') {
                    try { ValidateChasissNumber(); } catch (e) {}
                }
            }
        }""", chassis_val)

        chassis_loc = page.locator("#ChasisNumberVehicle")
        if chassis_loc.count():
            try:
                chassis_loc.first.fill(chassis_val, timeout=2000)
            except Exception:
                pass
            logger.info("Proposal: filled Chassis Number * = '%s'", chassis_val)
        else:
            msg = "Proposal: #ChasisNumberVehicle element not found on page."
            logger.warning(msg)
            problems.append(msg)
    except Exception as exc:
        msg = f"Proposal: error filling Chassis Number: {exc}"
        logger.warning(msg)
        problems.append(msg)

    _snap(page, "proposal-vehicle-details-filled", logger)

    # 4. Click "Get Coverage Details" button (#btnFetchIDV)
    should_get_coverage = os.getenv("CLICK_GET_COVERAGE_DETAILS", "true").strip().lower() in {"1", "true", "yes", "on"}
    if should_get_coverage:
        try:
            logger.info("Proposal: clicking 'Get Coverage Details' (#btnFetchIDV)...")
            clicked = page.evaluate("""() => {
                const btn = document.getElementById('btnFetchIDV') ||
                            document.querySelector('input[value="Get Coverage Details"]') ||
                            Array.from(document.querySelectorAll('input[type="button"], button')).find(b => 
                                b.id !== 'btnReady' && (b.value || b.innerText || '').includes('Coverage Details')
                            );
                if (btn) {
                    if (typeof FetchIDV === 'function') {
                        try { FetchIDV(btn, 'EditMode '); } catch(e) { btn.click(); }
                    } else {
                        btn.click();
                    }
                    return true;
                }
                return false;
            }""")
            if clicked:
                logger.info("Proposal: 'Get Coverage Details' triggered successfully.")
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PlaywrightTimeoutError:
                    pass
                _snap(page, "proposal-after-get-coverage-details", logger)
            else:
                logger.warning("Proposal: #btnFetchIDV ('Get Coverage Details') button not found in DOM.")
        except Exception as exc:
            logger.warning("Proposal: failed clicking 'Get Coverage Details': %s", exc)

    # 5. Handle Cover Details / Addon checkboxes on Proposal page
    try:
        _configure_cover_checkboxes(page, logger)
        _select_tppd_limit(page, logger)
        _select_cpa_tenure(page, logger)
    except Exception as exc:
        logger.debug("Proposal cover re-verification notice: %s", exc)

    # 5.1 If Nil Depreciation is active, ensure 'Applying for Nil dep for first time: Yes' is selected
    try:
        page.evaluate("""() => {
            const nilYes = document.getElementById('divSecureNilyes') || 
                           document.getElementById('objCovers_6__objPolicyCover_IsNilRateApplyingFirstTime') ||
                           Array.from(document.querySelectorAll('input[type="radio"]')).find(r => {
                               const name = (r.getAttribute('name') || '').toLowerCase();
                               const val = (r.value || '').toLowerCase();
                               return name.includes('isnilrateapplyingfirsttime') && (val === 'true' || val === 'yes' || r.id.includes('yes'));
                           });
            if (nilYes) {
                nilYes.checked = true;
                nilYes.dispatchEvent(new Event('change', { bubbles: true }));
                nilYes.dispatchEvent(new Event('click', { bubbles: true }));
            }
        }""")
        logger.info("Proposal: verified 'Applying for Nil dep for first time: Yes' is selected.")
    except Exception as exc:
        logger.debug("Nil dep first time radio notice: %s", exc)

    # 6. Fill Nominee details (Mandatory when PA to Owner Driver is checked)
    try:
        nominee_name = (customer.nominee_name or os.getenv("NOMINEE_NAME", "Murugan")).strip()
        nominee_dob = (customer.nominee_dob or os.getenv("NOMINEE_DOB", "01-01-1975")).strip()
        nominee_relation = (customer.nominee_relation or os.getenv("NOMINEE_RELATION", "Father")).strip()
        nominee_age = (customer.nominee_age or os.getenv("NOMINEE_AGE", "49")).strip()

        logger.info("Proposal: Filling Nominee details (Name='%s', DOB='%s', Relation='%s', Age='%s')...",
                    nominee_name, nominee_dob, nominee_relation, nominee_age)

        nom_filled = page.evaluate("""(data) => {
            // 1. Expand Nominee Details Declared if toggle/header exists
            const allElements = Array.from(document.querySelectorAll('a, span, div, h4, h5, label, i, p, td, th'));
            const toggle = allElements.find(el => {
                const t = (el.innerText || '').trim().toLowerCase();
                return t.includes('nominee details declared') && t.length < 50;
            });
            if (toggle) {
                try { toggle.click(); } catch(e) {}
            }

            // 2. Unhide nominee container in DivCover24 if collapsed
            const div24 = document.getElementById('DivCover24');
            if (div24) {
                div24.querySelectorAll('*').forEach(c => {
                    if (c.style && (c.style.display === 'none' || c.classList.contains('collapse'))) {
                        c.style.display = 'block';
                        c.classList.remove('collapse');
                    }
                });
            }

            // 3. Set values directly in DOM regardless of offsetParent
            let success = false;
            const nameEl = document.getElementById('txtPAOwnerCoverNomineeName');
            if (nameEl) {
                nameEl.value = data.name;
                nameEl.dispatchEvent(new Event('input', { bubbles: true }));
                nameEl.dispatchEvent(new Event('change', { bubbles: true }));
                nameEl.dispatchEvent(new Event('blur', { bubbles: true }));
                if (typeof ValidatePAName === 'function') {
                    try { ValidatePAName(nameEl, 'Nominee Name'); } catch(e) {}
                }
                success = true;
            }

            const dobEl = document.getElementById('Nominee_Date_Birth');
            if (dobEl) {
                dobEl.value = data.dob;
                dobEl.dispatchEvent(new Event('input', { bubbles: true }));
                dobEl.dispatchEvent(new Event('change', { bubbles: true }));
                dobEl.dispatchEvent(new Event('blur', { bubbles: true }));
                if (typeof CheckNomineeDOB === 'function') {
                    try { CheckNomineeDOB(dobEl, 'PATOOWNERDRIVER'); } catch(e) {}
                }
            }

            const ageEl = document.getElementById('txtNomineeAge');
            if (ageEl) {
                ageEl.value = data.age;
                ageEl.dispatchEvent(new Event('input', { bubbles: true }));
                ageEl.dispatchEvent(new Event('change', { bubbles: true }));
                ageEl.dispatchEvent(new Event('blur', { bubbles: true }));
            }

            const relEl = document.getElementById('Nominee_RelationshipOwnerDriver');
            if (relEl) {
                relEl.value = data.relation;
                relEl.dispatchEvent(new Event('change', { bubbles: true }));
                if (window.jQuery && window.jQuery(relEl).data('kendoDropDownList')) {
                    const ddl = window.jQuery(relEl).data('kendoDropDownList');
                    try { ddl.search(data.relation); } catch(e) {}
                    try { ddl.select(function(item) { return item.text === data.relation || item.value === data.relation; }); } catch(e) {}
                    try { ddl.trigger('change'); } catch(e) {}
                }
                if (typeof OnchangeofRelatioship === 'function') {
                    try { OnchangeofRelatioship(); } catch(e) {}
                }
                if (typeof ValidatePAName === 'function') {
                    try { ValidatePAName(relEl, 'Nominee Relationship with Owner Driver'); } catch(e) {}
                }
            }
            return success;
        }""", {
            "name": nominee_name,
            "dob": nominee_dob,
            "age": nominee_age,
            "relation": nominee_relation,
        })
        if nom_filled:
            logger.info("Proposal: filled Nominee details successfully (Name='%s', DOB='%s', Relation='%s')",
                        nominee_name, nominee_dob, nominee_relation)
        else:
            logger.warning("Proposal: #txtPAOwnerCoverNomineeName element not found in DOM.")
    except Exception as exc:
        logger.warning("Proposal: error setting Nominee details: %s", exc)

    # 7. Step - Document Upload (automatic sample document upload)
    try:
        upload_problems = _upload_proposal_documents(page, logger)
        if upload_problems:
            problems.extend(upload_problems)
    except Exception as exc:
        logger.warning("Proposal: Document Upload error: %s", exc)

    # 8. Check "Is Registration Address same as Communication Address?"
    try:
        reg_same = os.getenv("REG_ADDR_SAME_AS_COMM", "true").strip().lower() in {"1", "true", "yes", "on"}
        if reg_same:
            page.evaluate("""() => {
                const chk = document.getElementById('IsRegistrationAddresssame');
                if (chk) {
                    if (!chk.checked) {
                        chk.click();
                    }
                    if (!chk.checked) {
                        chk.checked = true;
                        chk.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    if (typeof showCommAddress === 'function') {
                        try { showCommAddress(); } catch (e) {}
                    }
                }
                // Ensure registration address fields are populated from communication address if empty
                const copyVal = (srcId, dstId) => {
                    const src = document.getElementById(srcId);
                    const dst = document.getElementById(dstId);
                    if (src && dst && !dst.value) {
                        dst.value = src.value;
                        dst.dispatchEvent(new Event('input', { bubbles: true }));
                        dst.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                };
                copyVal('objAddressAddress1', 'objRegistrationAddressAddress1');
                copyVal('objAddressAddress2', 'objRegistrationAddressAddress2');
                copyVal('objAddressddlState', 'objRegistrationAddressddlState');
                copyVal('objAddressddlDistrict', 'objRegistrationAddressddlDistrict');
                copyVal('objAddressddlCity', 'objRegistrationAddressddlCity');
                copyVal('objAddressddlArea', 'objRegistrationAddressddlArea');

                // Also populate inspection address if present
                copyVal('objAddressAddress1', 'objInspectionAddressAddress1');
                copyVal('objAddressAddress2', 'objInspectionAddressAddress2');
                copyVal('objAddressddlState', 'objInspectionAddressddlState');
                copyVal('objAddressddlDistrict', 'objInspectionAddressddlDistrict');
                copyVal('objAddressddlCity', 'objInspectionAddressddlCity');
                copyVal('objAddressddlArea', 'objInspectionAddressddlArea');
            }""")
            logger.info("Proposal: cleanly checked 'Is Registration Address same as Communication Address?' (#IsRegistrationAddresssame).")
            page.wait_for_timeout(500)
    except Exception as exc:
        logger.warning("Proposal: error setting Registration Address checkbox: %s", exc)

    # 9. Calculate Premium if configured
    should_calc = os.getenv("CLICK_CALCULATE_PREMIUM", "true").strip().lower() in {"1", "true", "yes", "on"}
    if should_calc:
        calc_dialog_msgs = []
        def _calc_dialog_handler(dialog):
            calc_dialog_msgs.append(dialog.message)
            logger.info("Proposal Calculate/Payment Dialog appeared: '%s'", dialog.message)
            try:
                dialog.accept()
            except Exception:
                pass

        page.on("dialog", _calc_dialog_handler)
        try:
            logger.info("Proposal: clicking 'Calculate Premium' button (#btnCalculate)...")
            calc_btn = page.locator("#btnCalculate, input[value='Calculate Premium']")
            if calc_btn.count():
                try:
                    calc_btn.first.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                page.wait_for_timeout(300)
                page.evaluate("""() => {
                    const btn = document.getElementById('btnCalculate') ||
                                document.querySelector('input[value="Calculate Premium"]');
                    if (btn) {
                        if (typeof ButtonCalculate === 'function') {
                            try { ButtonCalculate(); } catch(e) { btn.click(); }
                        } else {
                            btn.click();
                        }
                    }
                }""")
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeoutError:
                    pass
                logger.info("Proposal: 'Calculate Premium' executed successfully.")
                _snap(page, "proposal-final-calculated-premium", logger)

                try:
                    summary_info = page.evaluate("""() => {
                        const clean = t => (t || '').replace(/\\s+/g, ' ').trim();
                        const container = document.getElementById('containerMain') || document.body;
                        const text = container.innerText || '';
                        const lines = text.split('\\n').map(clean).filter(Boolean);
                        const matches = lines.filter(l => /(?:total|net|final|od|tp|premium|gst)\\s*(?:premium|amount|summary|payable|tax)?\\s*[:₹]/i.test(l));
                        return matches.slice(0, 10);
                    }""")
                    if summary_info:
                        logger.info("Proposal Final Premium Summary lines: %s", summary_info)
                except Exception:
                    pass
            else:
                logger.info("Proposal: #btnCalculate ('Calculate Premium') button not found or not visible.")

            # 9.5. Save Proposal Details to Database (#btnSave / SaveInformation())
            # CRITICAL: Payment requires the proposal and calculated premium to be saved in the DB first!
            should_save = os.getenv("SAVE_PROPOSAL", "true").strip().lower() in {"1", "true", "yes", "on"}
            if should_save:
                logger.info("Proposal: Step - Saving proposal information to backend database (#btnSave / SaveInformation)...")
                page.wait_for_timeout(1000)
                saved_ok = page.evaluate("""() => {
                    const btn = document.getElementById('btnSave') ||
                                document.querySelector('input[value="Save"]') ||
                                document.querySelector('input[name="btnsave"]') ||
                                Array.from(document.querySelectorAll('input[type="button"], button')).find(b => 
                                    (b.value || b.innerText || '').trim().toLowerCase() === 'save'
                                );
                    if (btn) {
                        try { btn.scrollIntoView(); } catch(e) {}
                        if (typeof SaveInformation === 'function') {
                            try { SaveInformation(); } catch(e) { btn.click(); }
                        } else {
                            btn.click();
                        }
                        return true;
                    }
                    return false;
                }""")
                if saved_ok:
                    logger.info("Proposal: #btnSave ('Save') triggered.")
                else:
                    logger.warning("Proposal: #btnSave ('Save') button not found in DOM.")

                page.wait_for_timeout(3500)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except PlaywrightTimeoutError:
                    pass

                # Handle any confirmation popup / KYC dialog that appears after saving
                page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('.k-window button, .modal button, div[role="dialog"] button, .bootbox button, .swal-button'));
                    const yesBtn = buttons.find(b => {
                        const t = (b.innerText || b.value || '').trim().toLowerCase();
                        return t === 'yes' || t === 'proceed' || t === 'ok' || t === 'continue' || t === 'confirm';
                    });
                    if (yesBtn) {
                        yesBtn.click();
                    }
                }""")
                page.wait_for_timeout(1500)
                _snap(page, "proposal-after-save", logger)

            # 10. Advance to Payment Details next step (#BtnmakeLive / makeLivePayment)
            should_pay = os.getenv("PROCEED_TO_PAYMENT", "true").strip().lower() in {"1", "true", "yes", "on"}
            if should_pay:
                logger.info("Proposal: Step - Proceeding to Payment Details next step (#BtnmakeLive)...")
                page.wait_for_timeout(1500)

                # Expand Premium Summary And Payment accordion if collapsed
                page.evaluate("""() => {
                    const headers = Array.from(document.querySelectorAll('div, h2, h3, h4, span, a, li')).filter(el => {
                        const txt = (el.innerText || '').trim().toLowerCase();
                        return txt.includes('premium summary and payment') && txt.length < 50;
                    });
                    for (const h of headers) {
                        const txt = (h.innerText || '').trim();
                        if (txt.includes('+') || txt.startsWith('+') || h.classList.contains('collapsed')) {
                            h.click();
                            break;
                        }
                    }
                }""")
                page.wait_for_timeout(600)

                # Trigger Make Payment
                clicked_pay = page.evaluate("""() => {
                    const btn = document.getElementById('BtnmakeLive') || 
                                document.querySelector('input[value="Make Payment"]') ||
                                Array.from(document.querySelectorAll('button, input[type="button"]')).find(b => 
                                    (b.value || b.innerText || '').includes('Make Payment')
                                );
                    if (btn) {
                        try { btn.scrollIntoView(); } catch(e) {}
                        if (typeof makeLivePayment === 'function') {
                            try { makeLivePayment(); } catch(e) { btn.click(); }
                        } else {
                            btn.click();
                        }
                        return true;
                    }
                    return false;
                }""")

                if clicked_pay:
                    logger.info("Proposal: 'Make Payment' button triggered.")
                else:
                    logger.info("Proposal: #BtnmakeLive not found; checking for alternative payment proceed button.")

                # Handle any confirmation modal (e.g. Customer KYC Details popup with 'Yes' button)
                page.wait_for_timeout(2500)
                page.evaluate("""() => {
                    const buttons = Array.from(document.querySelectorAll('.k-window button, .modal button, div[role="dialog"] button, .bootbox button, .swal-button'));
                    const yesBtn = buttons.find(b => {
                        const t = (b.innerText || b.value || '').trim().toLowerCase();
                        return t === 'yes' || t === 'proceed' || t === 'ok' || t === 'continue' || t === 'confirm';
                    });
                    if (yesBtn) {
                        yesBtn.click();
                    }
                }""")

                page.wait_for_timeout(4000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except PlaywrightTimeoutError:
                    pass

                _snap(page, "proposal-payment-details-step", logger)

                # Diagnostic check on Payment page
                err_text = page.evaluate("""() => {
                    const txt = document.body ? document.body.innerText : '';
                    if (txt.includes('Some Error has occured')) {
                        return txt.replace(/\\s+/g, ' ').trim();
                    }
                    return null;
                }""")
                if err_text:
                    logger.warning("Proposal: Payment page returned system error: '%s'", err_text)
                else:
                    logger.info("Proposal: Transitioned to Payment Details step! URL: %s", page.url)

        except Exception as exc:
            logger.warning("Proposal: Calculate Premium / Payment step error: %s", exc)
        finally:
            try:
                page.remove_listener("dialog", _calc_dialog_handler)
            except Exception:
                pass

    return problems


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

    settings = Settings.from_env(username=username, password=password)
    if website_url:
        import dataclasses
        settings = dataclasses.replace(settings, base_url=website_url)
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

    # Load DB input row once — API params override DB; .env is last resort
    _db_row = _load_db_row()
    _scrape_input_id: int | None = int(_db_row["id"]) if _db_row and _db_row.get("id") else None

    # Build dynamic inputs — API params > DB row > .env defaults
    vehicle = _build_vehicle_input(v_overrides, _db_row=_db_row)
    cover_input = _build_cover_details_input(cover_data, _db_row=_db_row)
    customer = _build_customer_input(customer_data, _db_row=_db_row)
    address = _build_address_input(address_data, _db_row=_db_row)

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

            # ── Step 9: Save to PostgreSQL (7 Normalized Tables) ─────────
            logger.info("Step 9: Saving to PostgreSQL across 7 normalized tables.")
            row_id = save_coverage(
                coverage,
                session_id=_scrape_input_id,
                vehicle=vehicle,
                customer=customer,
                address=address,
            )
            logger.info("Coverage saved successfully across all relational tables | quote_id=%d", row_id)

            # ── Step 10 (opt-in): Continue to Buy → proposal page ─────────
            # The quote is already saved, so a problem here cannot lose it.
            _ctb_env = os.getenv("CONTINUE_TO_BUY", "false").strip().lower() in {"1", "true", "yes", "on"}
            _ctb_db = bool((_db_row or {}).get("continue_to_buy", False))
            if has_premium and (_ctb_env or _ctb_db):
                try:
                    if _continue_to_proposal(page, logger, log_dir):
                        problems = _fill_proposal_customer(page, customer, address, logger)
                        _vc_env = os.getenv("VALIDATE_CUSTOMER", "true").strip().lower() in {"1", "true", "yes", "on"}
                        _vc_db = (_db_row or {}).get("validate_customer")
                        validate = _vc_db if _vc_db is not None else _vc_env
                        if validate:
                            if problems:
                                logger.warning("Proposal: some fields need attention (%s), but proceeding to click 'Validate Customer'.", "; ".join(problems))
                            reasons = _validate_proposal_customer(page, logger)
                            if reasons:
                                logger.warning("Proposal: 'Validate Customer' completed with issues listed above.")
                            # Proceed to mandatory Vehicle Details even if soft warnings exist, as long as vehicle section exists
                            has_vehicle_section = page.evaluate("""() => {
                                return !!(document.getElementById('EngineNumberVehicle') || document.getElementById('ChasisNumberVehicle'));
                            }""")
                            if not reasons or has_vehicle_section:
                                logger.info("Proposal: Customer validated successfully. Proceeding to fill mandatory Vehicle Details from .env...")
                                veh_problems = _fill_proposal_vehicle_details(page, vehicle, customer, logger)
                                if veh_problems:
                                    logger.warning("Proposal: Vehicle Details completed with notes: %s", "; ".join(veh_problems))
                                else:
                                    logger.info("Proposal: Vehicle Details and subsequent mandatory steps completed successfully!")
                        else:
                            logger.info(
                                "Proposal: skipping 'Validate Customer' (VALIDATE_CUSTOMER=false)."
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
