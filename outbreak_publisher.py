#!/usr/bin/env python3
"""
outbreak_publisher.py — The Brink World Health & Outbreak Radar ingestion (v1)

Same decoupled pattern as hazard_publisher.py: collectors pull official sources,
a validation gate rejects anything unsourced/stale/malformed, and only clean
rows reach Supabase. epidemic.html reads `outbreak_signals` and never calls an
agency directly.

Sources (all official; tier = confirmed unless noted)
  who   WHO Disease Outbreak News            run daily
  ecdc  ECDC SARS-CoV-2 variant classification run daily (page changes ~monthly)
  cdc   CDC NWSS wastewater viral activity   run weekly (data.cdc.gov, Fridays)
                                             tier = early_signal

Usage
  export SUPABASE_URL=https://xxxx.supabase.co
  export SUPABASE_SERVICE_KEY=...            # service-role key, never in the browser
  python outbreak_publisher.py --dry-run     # fetch + validate, print, write nothing
  python outbreak_publisher.py               # all sources
  python outbreak_publisher.py --only ecdc,who
  python outbreak_publisher.py --probe cdc   # show the CDC dataset's real columns

Exit code is non-zero if any source failed, so a scheduler (GitHub Actions cron)
will alert you instead of failing silently.

Requires: pip install requests beautifulsoup4
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("outbreak")

UA = "TheBrinkWorld-OutbreakRadar/1.0 (+https://thebrinkworld.com)"
TIMEOUT = 30

SRC_WHO = "WHO Disease Outbreak News"
SRC_WHO_ACTIVE = "WHO Ongoing Health Emergencies"
SRC_WHO_SEAR = "WHO South-East Asia Epidemiological Bulletin"
SRC_WHO_WPRO = "WHO Western Pacific Surveillance"
SRC_WHO_EMRO = "WHO Eastern Mediterranean Outbreaks"
SRC_AFRICA_CDC = "Africa CDC Epidemic Intelligence"
SRC_ECDC = "ECDC"
SRC_ECDC_CDTR = "ECDC Communicable Disease Threats Report"
SRC_CDC = "US CDC NWSS"
SRC_CDC_HAN = "US CDC Health Alert Network"
SRC_PAHO = "PAHO Epidemiological Alerts"

OFFICIAL_SOURCES = {
    SRC_WHO, SRC_WHO_ACTIVE, SRC_WHO_SEAR, SRC_WHO_WPRO, SRC_WHO_EMRO, SRC_AFRICA_CDC,
    SRC_ECDC, SRC_ECDC_CDTR, SRC_CDC, SRC_CDC_HAN, SRC_PAHO, "India NCDC / IDSP Weekly Outbreaks",
}

KINDS = {
    "variant_status", "variant_share", "wastewater", "outbreak_notice",
    "system_stress", "active_emergency", "regional_bulletin", "health_alert",
}
TRENDS = {"rising", "falling", "flat", "unknown"}
TIERS = {"confirmed", "reported", "early_signal"}

WHO_TTL_DAYS = 60
ECDC_TTL_DAYS = 45
CDC_TTL_DAYS = 14
REGIONAL_TTL_DAYS = 75
ACTIVE_EMERGENCY_TTL_DAYS = 10
CHANGE_BADGE_DAYS = 14


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.strip().replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class Signal:
    signal_key: str
    kind: str
    entity: str
    headline: str
    tier: str
    confidence: float
    source_name: str
    source_url: str
    as_of: date
    expires_at: datetime
    entity_group: str | None = None
    geo_scope: str = "global"
    geo_code: str | None = None
    geo_name: str | None = None
    metric: str | None = None
    value: float | None = None
    unit: str | None = None
    value_text: str | None = None
    trend: str = "unknown"
    detail: dict = field(default_factory=dict)
    published_at: datetime | None = None
    change_type: str | None = None

    def to_row(self, fetched_at: datetime) -> dict:
        return {
            "signal_key": self.signal_key, "kind": self.kind, "entity": self.entity,
            "entity_group": self.entity_group, "geo_scope": self.geo_scope,
            "geo_code": self.geo_code, "geo_name": self.geo_name, "metric": self.metric,
            "value": self.value, "unit": self.unit, "value_text": self.value_text,
            "trend": self.trend, "tier": self.tier, "confidence": round(self.confidence, 2),
            "headline": self.headline, "detail": self.detail,
            "source_name": self.source_name, "source_url": self.source_url,
            "as_of": self.as_of.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "fetched_at": fetched_at.isoformat(), "expires_at": self.expires_at.isoformat(),
            "change_type": self.change_type, "is_active": True,
        }

    def to_history_row(self) -> dict:
        return {
            "signal_key": self.signal_key, "as_of": self.as_of.isoformat(), "kind": self.kind,
            "entity": self.entity, "geo_code": self.geo_code, "value": self.value,
            "value_text": self.value_text, "trend": self.trend, "tier": self.tier,
            "detail": self.detail, "source_name": self.source_name,
        }


def validate(s: Signal, now: datetime) -> list[str]:
    """The quality gate. A signal that fails is dropped and counted, never shown."""
    errs = []
    if s.kind not in KINDS:
        errs.append(f"unknown kind {s.kind!r}")
    if s.tier not in TIERS:
        errs.append(f"unknown tier {s.tier!r}")
    if s.tier == "confirmed" and s.source_name not in OFFICIAL_SOURCES:
        errs.append("confirmed tier requires an official source")
    if not s.source_url.startswith("https://"):
        errs.append("source_url must be https")
    if not (5 <= len(s.headline) <= 300):
        errs.append("headline length out of range")
    if not s.entity.strip():
        errs.append("empty entity")
    if s.as_of > now.date():
        errs.append("as_of is in the future")
    if s.expires_at <= now:
        errs.append("already expired (stale source data)")
    if s.value is not None and not math.isfinite(s.value):
        errs.append("non-finite value")
    if not (0 <= s.confidence <= 1):
        errs.append("confidence out of range")
    if s.trend not in TRENDS:
        errs.append(f"unknown trend {s.trend!r}")
    return errs


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def http_get(url: str, **kw) -> requests.Response:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    headers.update(kw.pop("headers", {}))
    r = requests.get(url, headers=headers, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


def strip_html(s: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# --------------------------------------------------------------------------
# Collector 1 — WHO Disease Outbreak News
# --------------------------------------------------------------------------
WHO_API = "https://www.who.int/api/news/diseaseoutbreaknews"
WHO_ITEM = "https://www.who.int/emergencies/disease-outbreak-news/item/"


def split_who_title(title: str) -> tuple[str, str | None]:
    """WHO titles look like 'Disease – Country'. Returns (disease, geography|None)."""
    parts = re.split(r"\s+[–—-]\s+", title, maxsplit=1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (title.strip(), None)


def parse_who(payload: dict, now: datetime) -> list[Signal]:
    out = []
    cutoff = now - timedelta(days=WHO_TTL_DAYS)
    for it in payload.get("value", []):
        pub = parse_iso(it.get("PublicationDate"))
        title = (it.get("OverrideTitle") or it.get("Title") or "").strip()
        slug = (it.get("UrlName") or "").strip()
        if not (pub and title and slug) or pub < cutoff:
            continue
        disease, geo = split_who_title(title)
        multi = geo is None or re.search(r"global|multi-?country|worldwide", geo, re.I)
        out.append(Signal(
            signal_key=f"who_don|{it.get('DonId') or slug}",
            kind="outbreak_notice", entity=disease, entity_group=None,
            geo_scope="global" if multi else "country",
            geo_name=geo, headline=title, tier="confirmed", confidence=0.95,
            source_name=SRC_WHO, source_url=WHO_ITEM + slug,
            as_of=pub.date(), published_at=pub, expires_at=pub + timedelta(days=WHO_TTL_DAYS),
            detail={"don_id": it.get("DonId"),
                    "summary_excerpt": strip_html(it.get("Summary", ""))[:500]},
        ))
    return out


def collect_who_don() -> list[Signal]:
    r = http_get(WHO_API, params={"$orderby": "PublicationDate desc", "$top": 40})
    return parse_who(r.json(), now_utc())


# --------------------------------------------------------------------------
# Collector 2 — ECDC variant classification (HTML page, monthly cadence)
# --------------------------------------------------------------------------
ECDC_URL = "https://www.ecdc.europa.eu/en/covid-19/variants-concern"
ECDC_SECTIONS = [("variants of concern", "VOC"), ("variants of interest", "VOI"),
                 ("variants under monitoring", "VUM")]
ECDC_CAT_NAME = {"VOC": "variant of concern", "VOI": "variant of interest",
                 "VUM": "variant under monitoring"}


def _table_rows(table) -> list[dict]:
    first = table.find("tr")
    if first is None:
        return []
    headers = [c.get_text(" ", strip=True).lower() for c in first.find_all(["th", "td"])]
    rows = []
    for tr in table.find_all("tr")[1:]:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
    return rows


def _col(row: dict, prefix: str) -> str:
    for k, v in row.items():
        if k.startswith(prefix):
            return v.strip()
    return ""


def parse_ecdc(html_text: str, today: date) -> tuple[date, list[dict]]:
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"as of (\d{1,2} [A-Za-z]+ \d{4})", text)
    as_of = today
    if m:
        try:
            as_of = datetime.strptime(m.group(1), "%d %B %Y").date()
        except ValueError:
            pass
    found = []
    for h in soup.find_all("h2"):
        title = h.get_text(" ", strip=True).lower()
        cat = next((c for k, c in ECDC_SECTIONS if title.startswith(k)), None)
        if not cat:
            continue
        # the first <table> or <h2> after this heading; if it is an h2 the section is empty
        nxt = h.find_next(["table", "h2"])
        if nxt is None or nxt.name != "table":
            continue
        for row in _table_rows(nxt):
            lineage = re.sub(r"\s*\([a-z]\)\s*$", "", _col(row, "lineage")).strip()
            if lineage:
                found.append({"category": cat, "lineage": lineage, "row": row})
    return as_of, found


def ecdc_signals(as_of: date, found: list[dict]) -> list[Signal]:
    out = []
    for f in found:
        row, cat, lin = f["row"], f["category"], f["lineage"]
        who = _col(row, "who label")
        who_txt = "" if who.lower() in ("", "n/a") else f" ({who})"
        transmission = _col(row, "transmission in eu") or "not stated"
        out.append(Signal(
            signal_key=f"ecdc|variant_status|{lin}",
            kind="variant_status", entity=lin, entity_group="SARS-CoV-2",
            geo_scope="region", geo_code="EU/EEA", geo_name="EU/EEA",
            metric="ecdc_category", value_text=cat,
            headline=f"{lin}{who_txt}: ECDC {ECDC_CAT_NAME[cat]}; EU/EEA circulation: {transmission}",
            tier="confirmed", confidence=0.95,
            source_name=SRC_ECDC, source_url=ECDC_URL, as_of=as_of,
            expires_at=datetime.combine(as_of, datetime.min.time(), timezone.utc)
                       + timedelta(days=ECDC_TTL_DAYS),
            detail={
                "ecdc_category": cat,
                "transmission_in_eu_eea": transmission,
                "evidence": {
                    "transmissibility": _col(row, "impact on transmissibility"),
                    "immunity": _col(row, "impact on immunity"),
                    "severity": _col(row, "impact on severity"),
                },
                "spike_mutations_of_interest": _col(row, "spike mutations"),
                "country_first_detected": _col(row, "country first detected"),
                "first_detected": _col(row, "year and month first detected"),
                "who_label": who,
            },
        ))
    return out


def collect_ecdc_variants() -> list[Signal]:
    as_of, found = parse_ecdc(http_get(ECDC_URL).text, date.today())
    if not found:
        raise RuntimeError("ECDC page parsed but no variant rows found — page layout may have changed")
    return ecdc_signals(as_of, found)


def apply_variant_deltas(sigs: list[Signal], existing: list[dict], today: date) -> list[str]:
    """Tag newly listed / reclassified variants; return keys ECDC no longer lists."""
    if not existing:                       # first ever run: nothing to compare against
        return []
    prev = {r["signal_key"]: r for r in existing}
    for s in sigs:
        old = prev.get(s.signal_key)
        if old is None:
            s.change_type, s.detail["change_detected_at"] = "new", today.isoformat()
        elif old.get("value_text") != s.value_text:
            s.change_type, s.detail["change_detected_at"] = "reclassified", today.isoformat()
            s.detail["previous_category"] = old.get("value_text")
        else:                              # unchanged: keep the badge for a while, then drop it
            d = (old.get("detail") or {}).get("change_detected_at")
            if d and old.get("change_type") and (today - date.fromisoformat(d)).days <= CHANGE_BADGE_DAYS:
                s.change_type, s.detail["change_detected_at"] = old["change_type"], d
    return [k for k in prev if k not in {s.signal_key for s in sigs}]


# --------------------------------------------------------------------------
# Collector 3 — CDC NWSS wastewater viral activity level (early signal)
# --------------------------------------------------------------------------
CDC_DATASET = os.getenv("CDC_NWSS_DATASET", "atcp-73re")
CDC_URL = f"https://data.cdc.gov/resource/{CDC_DATASET}.json"
CDC_LANDING = "https://www.cdc.gov/nwss/"

# Column names are matched from candidates because the dataset schema is not pinned here.
# Run `--probe cdc` once and edit these lists if a column is not detected.
COLS = {
    "date": ["week_end", "week_ending", "date_end", "end_date", "week_end_date", "date"],
    "geo": ["site", "geography", "state_territory", "state", "jurisdiction", "location", "geo_name"],
    "geo_type": ["geography_type", "geo_type", "geography_level", "geolevel"],
    "pathogen": ["pathogen_target", "pathogen", "virus", "target", "disease"],
    "wval": ["site_wval", "wval", "wastewater_viral_activity_level_value", "viral_activity_level_value", "value"],
    "category": ["site_wval_category", "category", "wval_category", "viral_activity_level", "activity_level", "level"],
}
LEVELS = ["Minimal", "Low", "Moderate", "High", "Very High"]
# CDC's published WVAL bands (as documented for the SARS-CoV-2 map); verify against cdc.gov/nwss
LEVEL_CUTS = [(1.5, "Minimal"), (3.0, "Low"), (4.5, "Moderate"), (8.0, "High")]
NATIONAL_NAMES = {"national", "united states", "us", "usa", "u.s."}


def wval_level(v: float) -> str:
    for cut, name in LEVEL_CUTS:
        if v <= cut:
            return name
    return "Very High"


def pathogen_label(text: str) -> str | None:
    t = text.lower()
    if "sars" in t or "covid" in t:
        return "SARS-CoV-2"
    if "influenza" in t or re.search(r"\bflu\b", t):
        return "Influenza A"
    if "rsv" in t or "syncytial" in t:
        return "RSV"
    return None


def trend_of(vals: list[float]) -> str:
    if len(vals) < 3:
        return "unknown"
    latest, prior = vals[-1], vals[-3:-1]
    base = sum(prior) / len(prior)
    if latest - base >= 0.5 and latest >= base * 1.25:
        return "rising"
    if base - latest >= 0.5 and latest <= base * 0.8:
        return "falling"
    return "flat"


def _pick(cols: set[str], cands: list[str]) -> str | None:
    low = {c.lower(): c for c in cols}
    return next((low[c] for c in cands if c in low), None)


def _f(x) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def normalize_cdc(rows: list[dict]) -> list[dict]:
    """Return [{pathogen, geo, is_national, date, wval, category}] from long OR wide layouts."""
    cols = set().union(*[r.keys() for r in rows]) if rows else set()
    dcol, gcol = _pick(cols, COLS["date"]), _pick(cols, COLS["geo"])
    tcol, pcol = _pick(cols, COLS["geo_type"]), _pick(cols, COLS["pathogen"])
    if not (dcol and gcol):
        raise RuntimeError(f"CDC: could not find date/geography columns. Columns: {sorted(cols)}")
    wcol = next((c for c in [_pick(cols, COLS["wval"])] if c), None)
    ccol = _pick(cols, COLS["category"])
    wide = {}
    if not pcol:  # wide layout: one WVAL column per pathogen
        for c in cols:
            lab = pathogen_label(c)
            if lab and ("wval" in c.lower() or "activity" in c.lower()):
                wide[c] = lab
        if not wide:
            raise RuntimeError(f"CDC: no pathogen column and no wide WVAL columns. Columns: {sorted(cols)}")
    out = []
    for r in rows:
        d = parse_iso(r.get(dcol))
        geo = (r.get(gcol) or "").strip()
        if gcol == "site" and r.get("state_territory"):
            state = str(r.get("state_territory") or "").strip()
            if state and state.lower() not in geo.lower():
                geo = f"{state} · {geo}"
        if not (d and geo):
            continue
        is_nat = geo.lower() in NATIONAL_NAMES or (tcol and str(r.get(tcol, "")).lower() == "national")
        cat_raw = str(r.get(ccol, "")).strip().title() if ccol else ""
        cat = cat_raw if cat_raw in LEVELS else None
        if pcol:
            lab = pathogen_label(str(r.get(pcol, "")))
            v = _f(r.get(wcol)) if wcol else None
            if lab and v is not None:
                out.append({"pathogen": lab, "geo": geo, "is_national": bool(is_nat),
                            "date": d.date(), "wval": v, "category": cat})
        else:
            for c, lab in wide.items():
                v = _f(r.get(c))
                if v is not None:
                    out.append({"pathogen": lab, "geo": geo, "is_national": bool(is_nat),
                                "date": d.date(), "wval": v, "category": None})
    return out


def cdc_signals(norm: list[dict], today: date) -> list[Signal]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in norm:
        groups.setdefault((r["pathogen"], r["geo"]), []).append(r)
    out = []
    for (path, geo), rs in groups.items():
        rs.sort(key=lambda r: r["date"])
        latest = rs[-1]
        vals = [r["wval"] for r in rs]
        trend = trend_of(vals)
        level = latest["category"] or wval_level(latest["wval"])
        # noise control: national always; a state only when High+ or Moderate+ and rising
        if not latest["is_national"]:
            if not (level in ("High", "Very High") or (trend == "rising" and level in ("Moderate", "High", "Very High"))):
                continue
        as_of = latest["date"]
        out.append(Signal(
            signal_key=f"cdc_nwss|wastewater|{path}|{geo}",
            kind="wastewater", entity=path, entity_group=path,
            geo_scope="country" if latest["is_national"] else "subnational",
            geo_code=geo, geo_name=geo, metric="wval", value=latest["wval"],
            unit="WVAL", value_text=level, trend=trend,
            headline=f"{path} wastewater activity {level.lower()} in {geo} (trend: {trend})",
            tier="early_signal", confidence=0.70,   # placeholder: calibrate after backtesting
            source_name=SRC_CDC, source_url=CDC_LANDING, as_of=as_of,
            expires_at=datetime.combine(as_of, datetime.min.time(), timezone.utc)
                       + timedelta(days=CDC_TTL_DAYS),
            detail={"category_source": "source" if latest["category"] else "computed_from_wval",
                    "recent_series": [[r["date"].isoformat(), r["wval"]] for r in rs[-5:]],
                    "note": "Wastewater is an early indicator of community spread, not a case count."},
        ))
    return out


def cdc_fetch_recent(days: int = 42) -> list[dict]:
    probe = http_get(CDC_URL, params={"$limit": 200}).json()
    if not probe:
        raise RuntimeError("CDC dataset returned no rows")
    cols = set().union(*[r.keys() for r in probe])
    dcol = _pick(cols, COLS["date"])
    if not dcol:
        raise RuntimeError(f"CDC: no date column found. Columns: {sorted(cols)}")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        return http_get(CDC_URL, params={"$where": f"{dcol} >= '{cutoff}'", "$limit": 50000}).json()
    except requests.HTTPError:
        return http_get(CDC_URL, params={"$order": f"{dcol} DESC", "$limit": 20000}).json()


def collect_cdc_wastewater() -> list[Signal]:
    return cdc_signals(normalize_cdc(cdc_fetch_recent()), date.today())



# --------------------------------------------------------------------------
# Collector 4+ — regional official-source intelligence
# --------------------------------------------------------------------------
MONTHS = "January February March April May June July August September October November December"


def _abs_url(base: str, href: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, href)


def _date_from_text(text: str, fallback: date | None = None) -> date:
    text = re.sub(r"\s+", " ", text or "")
    patterns = [
        r"\b(\d{1,2}\s+(?:" + "|".join(MONTHS.split()) + r")\s+\d{4})\b",
        r"\b((?:" + "|".join(MONTHS.split()) + r")\s+\d{1,2},\s+\d{4})\b",
    ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if not m:
            continue
        for fmt in ("%d %B %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(m.group(1), fmt).date()
            except ValueError:
                pass
    return fallback or date.today()


def _signal_from_link(
    *,
    key_prefix: str,
    kind: str,
    entity: str,
    headline: str,
    source_name: str,
    source_url: str,
    as_of: date,
    geo_scope: str,
    geo_name: str,
    detail: dict | None = None,
    ttl_days: int = REGIONAL_TTL_DAYS,
    confidence: float = 0.95,
) -> Signal:
    slug = re.sub(r"[^a-z0-9]+", "-", source_url.lower()).strip("-")[-140:]
    logical_kind = kind
    storage_kind = "outbreak_notice" if logical_kind in {"active_emergency", "regional_bulletin", "health_alert"} else logical_kind
    detail_payload = dict(detail or {})
    if storage_kind != logical_kind:
        detail_payload["signal_class"] = logical_kind
    return Signal(
        signal_key=f"{key_prefix}|{slug}",
        kind=storage_kind,
        entity=entity[:180],
        headline=headline[:300],
        tier="confirmed",
        confidence=confidence,
        source_name=source_name,
        source_url=source_url,
        as_of=as_of,
        expires_at=datetime.combine(as_of, datetime.min.time(), timezone.utc) + timedelta(days=ttl_days),
        geo_scope=geo_scope,
        geo_name=geo_name,
        detail=detail_payload,
        published_at=datetime.combine(as_of, datetime.min.time(), timezone.utc),
    )


WHO_ACTIVE_URL = "https://www.who.int/emergencies/situations"


def collect_who_active_emergencies() -> list[Signal]:
    soup = BeautifulSoup(http_get(WHO_ACTIVE_URL).text, "html.parser")
    out, seen = [], set()

    outbreak_heading = next(
        (h for h in soup.find_all(["h2", "h3"]) if h.get_text(" ", strip=True).lower() == "outbreaks"),
        None,
    )
    if outbreak_heading is None:
        raise RuntimeError("WHO ongoing-emergencies page parsed but the Outbreaks section was not found")

    for node in outbreak_heading.find_all_next(["a", "h2", "h3"]):
        if node.name in {"h2", "h3"} and node is not outbreak_heading:
            break
        if node.name != "a":
            continue
        href = str(node.get("href") or "")
        title = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
        if "/emergencies/situations/" not in href or not title:
            continue
        url = _abs_url(WHO_ACTIVE_URL, href)
        if url in seen:
            continue
        seen.add(url)
        out.append(_signal_from_link(
            key_prefix="who_active",
            kind="active_emergency",
            entity=title,
            headline=f"WHO ongoing disease outbreak: {title}",
            source_name=SRC_WHO_ACTIVE,
            source_url=url,
            as_of=date.today(),
            geo_scope="global",
            geo_name="Global / affected countries",
            detail={
                "coverage_note": "Listed by WHO in the current Outbreaks section of its ongoing health-emergencies page. Follow the linked WHO situation page for event-specific geography and updates."
            },
            ttl_days=ACTIVE_EMERGENCY_TTL_DAYS,
        ))
    if not out:
        raise RuntimeError("WHO Outbreaks section parsed but no current disease-outbreak links were found")
    return out[:20]


WHO_SEAR_URL = "https://www.who.int/southeastasia/outbreaks-and-emergencies/health-emergency-information-risk-assessment/sear-epi-bulletins"


def collect_who_sear_bulletins() -> list[Signal]:
    soup = BeautifulSoup(http_get(WHO_SEAR_URL).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if "epidemiological bulletin" not in title.lower():
            continue
        if not (href.startswith("http") or href.startswith("/")):
            continue
        url = _abs_url(WHO_SEAR_URL, href)
        if url in seen:
            continue
        seen.add(url)
        parent_text = a.parent.get_text(" ", strip=True) if a.parent else title
        as_of = _date_from_text(parent_text)
        out.append(_signal_from_link(
            key_prefix="who_sear",
            kind="regional_bulletin",
            entity="South-East Asia regional epidemiological bulletin",
            headline=title,
            source_name=SRC_WHO_SEAR,
            source_url=url,
            as_of=as_of,
            geo_scope="region",
            geo_name="WHO South-East Asia Region",
            detail={
                "coverage_note": "Official WHO South-East Asia regional bulletin; includes India and other SEAR Member States when relevant."
            },
        ))
    if not out:
        raise RuntimeError("WHO SEAR bulletin page parsed but no bulletin links were found")
    return sorted(out, key=lambda x: x.as_of, reverse=True)[:4]


AFRICA_CDC_URL = "https://africacdc.org/pillar/surveillance/"


def collect_africa_cdc_intelligence() -> list[Signal]:
    soup = BeautifulSoup(http_get(AFRICA_CDC_URL).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if "epidemic intelligence weekly report" not in title.lower():
            continue
        url = _abs_url(AFRICA_CDC_URL, href)
        if url in seen:
            continue
        seen.add(url)
        as_of = _date_from_text((a.parent.get_text(" ", strip=True) if a.parent else title))
        out.append(_signal_from_link(
            key_prefix="africa_cdc",
            kind="regional_bulletin",
            entity="Africa CDC epidemic intelligence",
            headline=title,
            source_name=SRC_AFRICA_CDC,
            source_url=url,
            as_of=as_of,
            geo_scope="region",
            geo_name="Africa",
            detail={
                "coverage_note": "Africa CDC event-based surveillance highlights moderate to very high-risk public-health events with new updates."
            },
        ))
    if not out:
        raise RuntimeError("Africa CDC surveillance page parsed but no epidemic-intelligence reports were found")
    return out[:4]


ECDC_CDTR_URL = "https://www.ecdc.europa.eu/en/publications-and-data/monitoring/weekly-threats-reports"


def collect_ecdc_cdtr() -> list[Signal]:
    soup = BeautifulSoup(http_get(ECDC_CDTR_URL).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if "communicable disease threats report" not in title.lower():
            continue
        url = _abs_url(ECDC_CDTR_URL, href)
        if url in seen:
            continue
        seen.add(url)
        as_of = _date_from_text(a.parent.get_text(" ", strip=True) if a.parent else title)
        out.append(_signal_from_link(
            key_prefix="ecdc_cdtr",
            kind="regional_bulletin",
            entity="EU/EEA communicable disease threats",
            headline=title,
            source_name=SRC_ECDC_CDTR,
            source_url=url,
            as_of=as_of,
            geo_scope="region",
            geo_name="EU/EEA",
            detail={
                "coverage_note": "ECDC weekly epidemic-intelligence summary of communicable-disease threats relevant to the EU/EEA, including global events with European relevance."
            },
        ))
    if not out:
        raise RuntimeError("ECDC CDTR page parsed but no weekly reports were found")
    return sorted(out, key=lambda x: x.as_of, reverse=True)[:4]


CDC_HAN_URL = "https://www.cdc.gov/han/site.html"


def collect_cdc_han() -> list[Signal]:
    soup = BeautifulSoup(http_get(CDC_HAN_URL).text, "html.parser")
    candidates, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        url = _abs_url(CDC_HAN_URL, href)
        if "/han/php/notices/han" not in url:
            continue
        if url in seen:
            continue
        seen.add(url)
        candidates.append((label, url))

    out = []
    for label, url in candidates[:12]:
        detail_soup = BeautifulSoup(http_get(url).text, "html.parser")
        page_text = detail_soup.get_text(" ", strip=True)
        as_of = _date_from_text(page_text)
        h1 = detail_soup.find("h1")
        headline = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)) if h1 else (label or "CDC Health Alert Network notice")
        out.append(_signal_from_link(
            key_prefix="cdc_han",
            kind="health_alert",
            entity=headline,
            headline=headline,
            source_name=SRC_CDC_HAN,
            source_url=url,
            as_of=as_of,
            geo_scope="country",
            geo_name="United States / international relevance where stated",
            detail={
                "coverage_note": "CDC Health Alert Network message for clinicians and public-health authorities."
            },
        ))
    if not out:
        raise RuntimeError("CDC HAN site index parsed but no HAN notice pages were found")
    return sorted(out, key=lambda x: x.as_of, reverse=True)[:12]


PAHO_ALERTS_URL = "https://www.paho.org/en/epidemiological-alerts-and-updates"


def collect_paho_alerts() -> list[Signal]:
    soup = BeautifulSoup(http_get(PAHO_ALERTS_URL).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        href = str(a.get("href") or "")
        if not title or not re.search(r"epidemiological (alert|update)", title, re.I):
            continue
        url = _abs_url(PAHO_ALERTS_URL, href)
        if url in seen:
            continue
        seen.add(url)
        as_of = _date_from_text(a.parent.get_text(" ", strip=True) if a.parent else title)
        out.append(_signal_from_link(
            key_prefix="paho",
            kind="regional_bulletin",
            entity="PAHO epidemiological alert/update",
            headline=title,
            source_name=SRC_PAHO,
            source_url=url,
            as_of=as_of,
            geo_scope="region",
            geo_name="Americas",
            detail={
                "coverage_note": "Official PAHO epidemiological alert or update for the Region of the Americas."
            },
        ))
    if not out:
        raise RuntimeError("PAHO epidemiological-alerts page parsed but no alerts were found")
    return sorted(out, key=lambda x: x.as_of, reverse=True)[:12]


WHO_WPRO_URL = "https://www.who.int/westernpacific/wpro-emergencies/surveillance/pacific-islands"


def collect_who_wpro_surveillance() -> list[Signal]:
    soup = BeautifulSoup(http_get(WHO_WPRO_URL).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        href = str(a.get("href") or "")
        if "pacific syndromic surveillance system weekly bulletin" not in title.lower():
            continue
        url = _abs_url(WHO_WPRO_URL, href)
        if url in seen:
            continue
        seen.add(url)
        as_of = _date_from_text(a.parent.get_text(" ", strip=True) if a.parent else title)
        out.append(_signal_from_link(
            key_prefix="who_wpro",
            kind="regional_bulletin",
            entity="Western Pacific syndromic surveillance",
            headline=title,
            source_name=SRC_WHO_WPRO,
            source_url=url,
            as_of=as_of,
            geo_scope="region",
            geo_name="Western Pacific / Pacific island countries and areas",
            detail={
                "coverage_note": "WHO Pacific Syndromic Surveillance System weekly bulletin; an early-warning layer for participating Pacific island countries and areas."
            },
        ))
    if not out:
        raise RuntimeError("WHO Western Pacific surveillance page parsed but no weekly bulletins were found")
    return sorted(out, key=lambda x: x.as_of, reverse=True)[:6]



WHO_EMRO_URL = "https://www.emro.who.int/home-page/outbreaks/"


def collect_who_emro_outbreaks() -> list[Signal]:
    soup = BeautifulSoup(http_get(WHO_EMRO_URL).text, "html.parser")
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        href = str(a.get("href") or "")
        if not title or len(title) < 5:
            continue
        if not re.search(r"(outbreak|cholera|dengue|measles|polio|mpox|mers|fever|disease|emergency)", title, re.I):
            continue
        url = _abs_url(WHO_EMRO_URL, href)
        if "emro.who.int" not in url or url in seen:
            continue
        seen.add(url)
        as_of = _date_from_text(a.parent.get_text(" ", strip=True) if a.parent else title)
        out.append(_signal_from_link(
            key_prefix="who_emro",
            kind="regional_bulletin",
            entity="Eastern Mediterranean public-health event",
            headline=title,
            source_name=SRC_WHO_EMRO,
            source_url=url,
            as_of=as_of,
            geo_scope="region",
            geo_name="WHO Eastern Mediterranean Region",
            detail={
                "coverage_note": "WHO Eastern Mediterranean regional outbreak/emergency information covering Member States across the Middle East, North Africa and parts of South/Central Asia."
            },
        ))
    if not out:
        raise RuntimeError("WHO EMRO outbreak page parsed but no outbreak links were found")
    return sorted(out, key=lambda x: x.as_of, reverse=True)[:12]


INDIA_IDSP_WEEKLY_URL = "https://ncdc.mohfw.gov.in/includes/WeeklyOutbreaks.php"


def collect_india_ncdc_alerts() -> list[Signal]:
    soup = BeautifulSoup(http_get(INDIA_IDSP_WEEKLY_URL).text, "html.parser")
    out, seen = [], set()
    current_year = date.today().year

    for a in soup.find_all("a", href=True):
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        href = str(a.get("href") or "")
        m = re.fullmatch(r"Week\s+(\d{1,2})", title, re.I)
        if not m:
            continue
        week = int(m.group(1))
        url = _abs_url(INDIA_IDSP_WEEKLY_URL, href)
        if str(current_year) not in url or url in seen:
            continue
        seen.add(url)
        try:
            as_of = date.fromisocalendar(current_year, week, 7)
        except ValueError:
            continue
        out.append(_signal_from_link(
            key_prefix="india_idsp_weekly",
            kind="regional_bulletin",
            entity=f"India IDSP weekly outbreak report · Week {week}",
            headline=f"India IDSP Weekly Outbreak Report · Week {week}, {current_year}",
            source_name="India NCDC / IDSP Weekly Outbreaks",
            source_url=url,
            as_of=as_of,
            geo_scope="country",
            geo_name="India",
            detail={
                "coverage_note": "Official weekly district-level disease alerts/outbreaks reported and responded to through India's IDSP/NCDC."
            },
            ttl_days=35,
        ))

    if not out:
        raise RuntimeError("India NCDC Weekly Outbreaks page parsed but no current-year weekly reports were found")
    return sorted(out, key=lambda x: x.as_of, reverse=True)[:6]


# --------------------------------------------------------------------------
# Storage (Supabase PostgREST) + dry-run
# --------------------------------------------------------------------------
class Store:
    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.s = requests.Session()
        self.s.headers.update({"apikey": key, "Authorization": f"Bearer {key}",
                               "Content-Type": "application/json"})

    def _send(self, method: str, path: str, body, prefer: str, params: dict | None = None):
        r = self.s.request(method, f"{self.base}/{path}", params=params,
                           data=json.dumps(body), headers={"Prefer": prefer}, timeout=TIMEOUT)
        if r.status_code >= 300:
            raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:300]}")

    def existing(self, kind: str, source: str) -> list[dict]:
        r = self.s.get(f"{self.base}/outbreak_signals", timeout=TIMEOUT, params={
            "select": "signal_key,value_text,change_type,detail",
            "kind": f"eq.{kind}", "source_name": f"eq.{source}", "is_active": "eq.true"})
        r.raise_for_status()
        return r.json()

    def upsert(self, rows: list[dict]):
        for i in range(0, len(rows), 200):
            self._send("POST", "outbreak_signals", rows[i:i + 200],
                       "resolution=merge-duplicates,return=minimal", {"on_conflict": "signal_key"})

    def history(self, rows: list[dict]):
        for i in range(0, len(rows), 200):
            self._send("POST", "outbreak_signal_history", rows[i:i + 200],
                       "resolution=ignore-duplicates,return=minimal",
                       {"on_conflict": "signal_key,as_of"})

    def deactivate(self, keys: list[str]):
        if keys:
            quoted = ",".join('"' + k.replace('"', '""') + '"' for k in keys)
            self._send("PATCH", "outbreak_signals", {"is_active": False}, "return=minimal",
                       {"signal_key": f"in.({quoted})"})

    def log_run(self, row: dict):
        self._send("POST", "outbreak_source_runs", [row], "return=minimal")


class DryStore:
    def existing(self, kind, source): return []
    def upsert(self, rows): print(f"  [dry-run] would upsert {len(rows)} rows")
    def history(self, rows): print(f"  [dry-run] would append {len(rows)} history rows")
    def deactivate(self, keys): print(f"  [dry-run] would deactivate {keys}" if keys else "")
    def log_run(self, row): print(f"  [dry-run] run log: {json.dumps(row, default=str)}")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
COLLECTORS = {
    "who": (collect_who_don, "outbreak_notice", SRC_WHO),
    "who_active": (collect_who_active_emergencies, "outbreak_notice", SRC_WHO_ACTIVE),
    "who_sear": (collect_who_sear_bulletins, "outbreak_notice", SRC_WHO_SEAR),
    "who_wpro": (collect_who_wpro_surveillance, "outbreak_notice", SRC_WHO_WPRO),
    "who_emro": (collect_who_emro_outbreaks, "outbreak_notice", SRC_WHO_EMRO),
    "africa_cdc": (collect_africa_cdc_intelligence, "outbreak_notice", SRC_AFRICA_CDC),
    "ecdc": (collect_ecdc_variants, "variant_status", SRC_ECDC),
    "ecdc_cdtr": (collect_ecdc_cdtr, "outbreak_notice", SRC_ECDC_CDTR),
    "cdc": (collect_cdc_wastewater, "wastewater", SRC_CDC),
    "cdc_han": (collect_cdc_han, "outbreak_notice", SRC_CDC_HAN),
    "paho": (collect_paho_alerts, "outbreak_notice", SRC_PAHO),
    "india_ncdc": (collect_india_ncdc_alerts, "outbreak_notice", "India NCDC / IDSP Weekly Outbreaks"),
}


def run_source(name: str, store, dry: bool) -> bool:
    fn, kind, source = COLLECTORS[name]
    started, now = now_utc(), now_utc()
    fetched = published = rejected = 0
    status, error = "ok", None
    try:
        sigs = fn()
        fetched = len(sigs)
        good = []
        for s in sigs:
            errs = validate(s, now)
            if errs:
                rejected += 1
                log.warning("rejected %s: %s", s.signal_key, "; ".join(errs))
            else:
                good.append(s)
        existing_rows = store.existing(kind, source)
        gone: list[str] = []
        if name == "ecdc" and good:
            gone = apply_variant_deltas(good, existing_rows, date.today())
        elif good:
            current_keys = {sig.signal_key for sig in good}
            gone = [row["signal_key"] for row in existing_rows if row.get("signal_key") not in current_keys]
        if good:
            store.upsert([sig.to_row(now) for sig in good])
            store.history([sig.to_history_row() for sig in good])
        store.deactivate(gone)
        published = len(good)
        if dry:
            for s in good[:8]:
                print(f"  • [{s.tier}] {s.headline}  (as of {s.as_of})")
    except Exception as e:  # one broken source must not stop the others
        status, error = "failed", f"{type(e).__name__}: {e}"[:500]
        log.error("%s failed: %s", name, error)
    try:
        store.log_run({"source": name, "started_at": started.isoformat(), "status": status,
                       "fetched": fetched, "published": published, "rejected": rejected, "error": error})
    except Exception as e:
        log.error("could not write run log: %s", e)
    print(f"{name}: {status} — fetched {fetched}, published {published}, rejected {rejected}")
    return status == "ok"


def probe_cdc():
    rows = http_get(CDC_URL, params={"$limit": 5}).json()
    cols = sorted(set().union(*[r.keys() for r in rows])) if rows else []
    print("dataset:", CDC_DATASET, "\ncolumns:", cols)
    for r in rows[:3]:
        print(json.dumps(r))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--only",
        default="who,who_active,who_sear,who_wpro,who_emro,africa_cdc,ecdc,ecdc_cdtr,cdc,cdc_han,paho,india_ncdc",
        help="comma list of configured official health-intelligence collectors",
    )
    ap.add_argument("--dry-run", action="store_true", help="fetch and validate; write nothing")
    ap.add_argument("--probe", choices=["cdc"], help="print the source dataset's real columns")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    if args.probe:
        probe_cdc()
        return 0
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    bad = [n for n in names if n not in COLLECTORS]
    if bad:
        ap.error(f"unknown source(s): {bad}")
    if args.dry_run:
        store = DryStore()
    else:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not (url and key):
            ap.error("set SUPABASE_URL and SUPABASE_SERVICE_KEY/SUPABASE_SERVICE_ROLE_KEY (or use --dry-run)")
        store = Store(url, key)
    results = [run_source(n, store, args.dry_run) for n in names]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
