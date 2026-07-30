# -*- coding: utf-8 -*-
"""
IPTV Group & M3U Exporter (PySide6)

- Tabs: Live / VOD / TV Shows
- Per-tab live filter
- Groups table shows count of links/items per category (best-effort, async)
- Top info shows detected playlist expiry (date+time) after connect (best-effort)
- Export in background thread + FAST export option (no resolve/create_link)

New in this build:
- "Obriši sve" button
- Auto-clear groups/cache/status on every new "Poveži i povuci grupe"
- Checkbox text clarifies: fast export works ONLY for Live TV channels; for VOD/TV Shows it is ignored (normal resolve is used).

Patch (po zahtjevu):
- NE učitava programe kad samo označiš grupu (checkbox). Učitava se tek na DVOKLIK.
- U prozoru grupe: live filter + checkboxi po programu (odabir za export)
- Export poštuje odabrane programe i pokušava popraviti "localhost" streamove na /play/live.php
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from urllib.parse import urlparse, urlunparse

import requests
from requests.exceptions import ConnectTimeout, ConnectionError, ReadTimeout, Timeout

# CachyOS/KDE Qt integracija zna rušiti PySide6 kroz KIO/Breeze native pluginove.
# Koristimo generičniji Qt setup, osim ako korisnik ručno ne postavi drugačije.
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "gtk3")
    os.environ.setdefault("QT_STYLE_OVERRIDE", "Fusion")

from PySide6 import QtCore, QtGui, QtWidgets
from concurrent.futures import ThreadPoolExecutor, as_completed

COMMON_ADULT_PINS = ("0000", "1234", "1111", "9999", "12345", "00000", "2580", "4321")
PORTAL_CONNECT_TIMEOUT = 12


# -----------------------------
# Link expiry parsing
# -----------------------------

def _try_parse_unix_ts(value: str) -> Optional[datetime]:
    try:
        v = (value or "").strip()
        if not v.isdigit():
            return None
        ts = int(v)
        if ts > 10**11:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


def _try_parse_iso(value: str) -> Optional[datetime]:
    try:
        v = (value or "").strip()
        if v.endswith("Z"):
            v = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def extract_expiry_from_url(url: str) -> Optional[datetime]:
    if not url:
        return None

    keys = ("exp", "expires", "expiry", "expire", "end", "validto", "valid_to", "token_exp")

    qs = url.split("?", 1)[1] if "?" in url else ""
    qs = qs.split("#", 1)[0]
    for part in qs.split("&") if qs else []:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k.strip().lower() in keys:
            dt = _try_parse_unix_ts(v) or _try_parse_iso(v)
            if dt:
                return dt

    mm = re.search(r"(?<!\d)(1[6-9]\d{8}|2\d{9})(?!\d)", url)
    if mm:
        return _try_parse_unix_ts(mm.group(1))

    return None


def format_expiry(dt: Optional[datetime]) -> str:
    if not dt:
        return "Nepoznato"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def looks_adult_category(name: str) -> bool:
    low = f" {(name or '').strip().lower()} "
    words = (
        " adult ", " adults ", " xxx ", " 18+ ", "+18", " 18 ",
        " porn ", " porno ", " sex ", " erotic ", " erotica ",
        " erotika ", " odrasli ", " za odrasle ", " hot ",
        " 18plus ", " 18 plus ", " playboy ", " hustler ",
        " brazzers ", " redlight ", " onlyfans ", " xhamster ",
    )
    return any(w in low for w in words)


def _m3u_attr(value: Any) -> str:
    return str(value or "").replace('"', "'").strip()


def build_extinf_line(item: "Item", group_title: str) -> str:
    raw = item.raw or {}
    name = _m3u_attr(item.name)
    attrs = {
        "group-title": group_title,
        "tvg-name": name,
    }
    for key in ("id", "channel_id", "movie_id", "episode_id", "video_id"):
        value = raw.get(key)
        if value not in (None, ""):
            attrs["tvg-id"] = _m3u_attr(value)
            break
    for key in ("logo", "logo_url", "tvg_logo", "icon", "screenshot_uri"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            attrs["tvg-logo"] = _m3u_attr(value)
            break
    attr_text = " ".join(f'{k}="{v}"' for k, v in attrs.items() if v)
    return f"#EXTINF:-1 {attr_text},{name}"


def build_generated_item_details(
    item: "Item",
    category: "Category",
    base_url: str,
    mac: str,
    generated_url: str,
) -> str:
    type_label = category_type_label(category.category_type)
    group_title = f"{type_label} | {category.name}"
    m3u_entry = f"{build_extinf_line(item, group_title)}\n{generated_url}"
    metadata = json.dumps(
        item.raw or {},
        ensure_ascii=False,
        indent=2,
        default=str,
        sort_keys=True,
    )
    return (
        f"Naziv: {item.name}\n"
        f"Vrsta: {type_label}\n"
        f"Grupa: {category.name}\n"
        f"Portal: {base_url}\n"
        f"MAC: {mac}\n\n"
        f"GENERIRANI LINK S TOKENOM\n{generated_url}\n\n"
        f"M3U ZAPIS\n{m3u_entry}\n\n"
        f"IZVORNA NAREDBA / LINK\n{item.url}\n\n"
        f"SVI METAPODACI\n{metadata}"
    )


def default_export_filename(base_url: str) -> str:
    host = urlparse(base_url if "://" in base_url else f"http://{base_url}").netloc or "portal"
    host = re.sub(r"[^A-Za-z0-9_.-]+", "_", host).strip("_") or "portal"
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"playlist_{stamp}_{host}.m3u"


def category_type_label(category_type: str) -> str:
    return {
        "IPTV": "Live",
        "VOD": "VOD",
        "Series": "TV Shows",
    }.get(category_type, category_type or "Unknown")


def format_portal_error(err: Any) -> str:
    text = str(err or "").strip()
    if isinstance(err, (ConnectTimeout, ReadTimeout, Timeout)) or "timed out" in text.lower():
        return (
            "Portal nije odgovorio na vrijeme.\n\n"
            "Provjeri Portal URL, internet vezu i radi li server trenutno.\n"
            "Ako URL ima http://, probaj ručno upisati https:// ili obrnuto.\n"
            "Za spor portal probaj ponovno za par minuta."
        )
    if isinstance(err, ConnectionError):
        return (
            "Nije moguće spojiti se na portal.\n\n"
            "Provjeri Portal URL, MAC adresu, internet vezu i radi li server trenutno."
        )
    return text or "Nepoznata greška."


# -----------------------------
# Helpers: ffmpeg cmd -> url
# -----------------------------

def extract_http_from_text(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    i = low.find("http://")
    j = low.find("https://")
    pos = min([p for p in (i, j) if p >= 0], default=-1)
    if pos < 0:
        return ""
    return t[pos:].split()[0]


def normalize_cmd_or_url(cmd_or_url: str) -> str:
    raw = (cmd_or_url or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("ffmpeg"):
        u = extract_http_from_text(raw)
        return u or raw
    return raw


def item_uid(item: 'Item') -> str:
    """Return a stable-ish unique id for an item.

    Different portals/types use different id keys.
    We fall back to URL (cmd/url) or name.
    """
    try:
        raw = item.raw or {}
        for k in ("id", "movie_id", "channel_id", "episode_id", "video_id"):
            v = raw.get(k)
            if v is not None and str(v).strip() and str(v).strip().lower() != "none":
                return str(v).strip()

        # Some portals keep unique id only in cmd/url.
        u = (item.url or "").strip()
        if u:
            return u
    except Exception:
        pass

    return (getattr(item, "name", "") or "").strip() or "_"


def maybe_fix_localhost_stream(url: str, base_url: str, mac: str) -> str:
    """If a portal returns a localhost stream URL, try to transform to /play/live.php.

    Logic copied/adapted from the 2nd provided script where export works well.
    """
    try:
        u = (url or "").strip()
        if not u:
            return ""

        # If this isn't localhost, just return.
        if "localhost" not in u.lower():
            return u

        # Many portals expose channels like http://localhost/ch/123_ ...
        m = re.search(r"/ch/(\d+)", u)
        if not m:
            return u

        ch_id = m.group(1)
        mac_q = (mac or "").strip()
        bu = (base_url or "").strip().rstrip("/")
        if not bu or not mac_q:
            return u

        return f"{bu}/play/live.php?mac={mac_q}&stream={ch_id}&extension=ts"
    except Exception:
        return (url or "").strip()


# -----------------------------
# Data structures
# -----------------------------

@dataclass
class Category:
    name: str
    category_type: str  # IPTV, VOD, Series
    category_id: str


@dataclass
class Item:
    name: str
    item_type: str  # channel / vod / series
    raw: Dict[str, Any]

    @property
    def url(self) -> str:
        for key in ("cmd", "url", "stream_url", "link", "play_url"):
            v = self.raw.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""



def _parse_expiry_value(value: Any) -> Optional[datetime]:
    """Parsira expiry iz int/float/string u tz-aware datetime (UTC default).

    Podržava:
    - unix timestamp (sekunde ili milisekunde) kao int/float ili digit-string
    - ISO (YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, +00:00, Z)
    - 'YYYY-MM-DD HH:MM:SS' / 'YYYY-MM-DD HH:MM'
    - 'DD.MM.YYYY' i 'DD.MM.YYYY HH:MM:SS'
    - varijante s / ili -
    """
    if value is None:
        return None

    # brojevi (timestamp)
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 10_000_000_000:  # ms
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None

    if isinstance(value, str):
        s = (value or "").strip()
        if not s or s.lower() in {"n/a", "na", "none", "null", "-"}:
            return None

        # digit string timestamp
        if s.isdigit():
            dt = _try_parse_unix_ts(s)
            if dt:
                return dt

        # ISO pokušaj
        dt = _try_parse_iso(s)
        if dt:
            return dt

        # Normalizacija (T -> razmak)
        s2 = s.replace("T", " ").replace("Z", "").strip()

        # Ručni formati
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%d.%m.%Y %H:%M:%S",
            "%d.%m.%Y %H:%M",
            "%d.%m.%Y",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
            "%d-%m-%Y",
        ):
            try:
                dt2 = datetime.strptime(s2, fmt)
                return dt2.replace(tzinfo=timezone.utc)
            except Exception:
                continue

        # Ako je datum dio dužeg teksta, izvuci prvi razumljiv datum.
        m = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?", s2)
        if m:
            dt = _parse_expiry_value(" ".join(p for p in m.groups() if p))
            if dt:
                return dt
        m = re.search(r"(\d{1,2}[./-]\d{1,2}[./-]\d{4})(?:[ T](\d{1,2}:\d{2}(?::\d{2})?))?", s2)
        if m:
            dt = _parse_expiry_value(" ".join(p for p in m.groups() if p))
            if dt:
                return dt

    return None


def _extract_expiry_from_js(js_data: Dict[str, Any]) -> Tuple[Optional[datetime], str]:
    """Pokušava izvući expiry iz poznatih ključeva; vraća (dt, source_key)."""
    if not isinstance(js_data, dict):
        return None, ""

    candidate_keys = (
        "end_date",
        "expire_billing_date",
        "expire_date",
        "expiry_date",
        "expires",
        "expires_at",
        "expiration_date",
        "expire",
        "expiry",
        "exp_date",
        "exp",
        "valid_to",
        "validto",
        "valid_until",
        "valid_till",
        "validity",
        "account_expiration",
        "account_expire",
        "account_expiry",
        "subscription_end",
        "subscription_expire",
        "subscription_expiry",
        "sub_end",
        "sub_expire",
        "tariff_plan_expire",
        "tariff_plan_expiry",
        "billing_expire",
        "billing_expiry",
        "end",
        "to",
        # neki portali (nažalost) vraćaju datum u 'phone'
        "phone",
    )

    for k in candidate_keys:
        if k in js_data:
            dt = _parse_expiry_value(js_data.get(k))
            if dt:
                return dt, k

    # ugniježđeni objekti (fallback)
    for nest_key in ("tariff_plan", "tariff", "account", "profile", "user", "data", "subscriber", "subscription"):
        sub = js_data.get(nest_key)
        if isinstance(sub, dict):
            for k in candidate_keys:
                if k in sub:
                    dt = _parse_expiry_value(sub.get(k))
                    if dt:
                        return dt, f"{nest_key}.{k}"

    return None, ""

# -----------------------------
# Portal clients
# -----------------------------

class BasePortalClient:
    def __init__(self, base_url: str, mac: str, timeout: int = 12, adult_pin: str = "0000"):
        self.base_url = base_url.rstrip("/")
        self.mac = mac
        self.timeout = timeout
        self.adult_pin = (adult_pin or "0000").strip() or "0000"
        self.session = requests.Session()
        self.session.cookies.set("mac", mac)
        self.session.cookies.set("stb_lang", "en")
        self.session.cookies.set("parent_password", self.adult_pin)
        self.session.cookies.set("timezone", "Europe/Zagreb")
        self._adult_pin_candidates = tuple(dict.fromkeys((self.adult_pin, *COMMON_ADULT_PINS)))

        self.headers: Dict[str, str] = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
            "X-User-Agent": "Model: MAG254; Link: Ethernet",
            "Referer": f"{self.base_url}/c/",
        }
        self._link_cache: Dict[str, str] = {}

    def set_adult_pin(self, pin: str) -> None:
        self.adult_pin = (pin or "0000").strip() or "0000"
        self.session.cookies.set("parent_password", self.adult_pin)

    def iter_adult_pins(self) -> Tuple[str, ...]:
        return self._adult_pin_candidates

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def get_account_expiry(self) -> Optional[datetime]:
        return None

    def get_account_expiry_info(self) -> Tuple[Optional[datetime], str]:
        return self.get_account_expiry(), ""

    def get_categories(self, kind: str) -> List[Category]:
        raise NotImplementedError

    def get_items(self, category: Category, num_threads: int = 10, progress_cb=None) -> List[Item]:
        """Fetch items for a category. Concrete clients override this."""
        # Optional progress callback: progress_cb(done, total_or_label)
        try:
            if callable(progress_cb):
                progress_cb(0, 'Start')
        except Exception:
            pass
        raise NotImplementedError

    def get_items_count(self, category: Category) -> Optional[int]:
        raise NotImplementedError

    def _create_link(self, item: Item) -> str:
        raise NotImplementedError

    def resolve_play_url(self, item: Item) -> str:
        raw = (item.url or "").strip()
        if not raw:
            return ""

        cache_key = f"{item.item_type}|{raw}"
        if cache_key in self._link_cache:
            return self._link_cache[cache_key]

        low = raw.lower()
        if low.startswith("http://") or low.startswith("https://"):
            self._link_cache[cache_key] = raw
            return raw

        if low.startswith("ffmpeg"):
            url = extract_http_from_text(raw) or raw
            self._link_cache[cache_key] = url
            return url

        try:
            created = (self._create_link(item) or "").strip()
            created = normalize_cmd_or_url(created)
            if created:
                self._link_cache[cache_key] = created
                return created
        except Exception:
            pass

        self._link_cache[cache_key] = raw
        return raw


class PortalPhpClient(BasePortalClient):
    """Classic /portal.php portals"""

    def _get_json(self, url: str) -> Any:
        r = self.session.get(url, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_account_expiry(self) -> Optional[datetime]:
        dt, _src = self.get_account_expiry_info()
        return dt

    def get_account_expiry_info(self) -> Tuple[Optional[datetime], str]:
        """Best-effort detekcija valjanosti liste (datum+vrijeme).

        Poboljšano:
        - prihvaća expiry kao int/float (unix timestamp) ili string
        - podržava više formata i više ključeva
        - prvo account_info/get_main_info, zatim fallback na stb/get_profile
        """
        # 1) account_info/get_main_info
        try:
            j = self._get_json(f"{self.base_url}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml")
            js = j.get("js", {}) if isinstance(j, dict) else {}
            dt, src = _extract_expiry_from_js(js)
            if dt:
                return dt, f"account_info.{src}"
        except Exception:
            pass

        # 2) Fallback: get_profile
        try:
            j = self._get_json(f"{self.base_url}/portal.php?type=stb&action=get_profile&JsHttpRequest=1-xml")
            js = j.get("js", {}) if isinstance(j, dict) else {}
            dt, src = _extract_expiry_from_js(js)
            if dt:
                return dt, f"stb.profile.{src}"
        except Exception:
            return None, ""

        return None, ""
    def _create_link(self, item: Item) -> str:
        cmd = (item.url or "").strip()
        if not cmd:
            return ""
        from requests.utils import quote

        if item.item_type == "channel":
            u = f"{self.base_url}/portal.php?type=itv&action=create_link&cmd={quote(cmd, safe='')}&JsHttpRequest=1-xml"
        else:
            u = f"{self.base_url}/portal.php?type=vod&action=create_link&cmd={quote(cmd, safe='')}&JsHttpRequest=1-xml"

        j = self._get_json(u)
        js = j.get("js", {}) if isinstance(j, dict) else {}
        out = (js.get("cmd") or js.get("url") or "")
        return str(out).strip()

    def get_categories(self, kind: str) -> List[Category]:
        out: List[Category] = []
        if kind == "IPTV":
            url = f"{self.base_url}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
            data = self._get_json(url).get("js", []) or []
            for i in data:
                if isinstance(i, dict) and i.get("title") and i.get("id"):
                    out.append(Category(str(i["title"]), "IPTV", str(i["id"])))
        elif kind == "VOD":
            url = f"{self.base_url}/portal.php?type=vod&action=get_categories&JsHttpRequest=1-xml"
            data = self._get_json(url).get("js", []) or []
            for i in data:
                if isinstance(i, dict):
                    name = i.get("title") or i.get("name")
                    cid = i.get("id")
                    if name and cid:
                        out.append(Category(str(name), "VOD", str(cid)))
        elif kind == "Series":
            url = f"{self.base_url}/portal.php?type=series&action=get_categories&JsHttpRequest=1-xml"
            data = self._get_json(url).get("js", []) or []
            for i in data:
                if isinstance(i, dict):
                    name = i.get("title") or i.get("name")
                    cid = i.get("id")
                    if name and cid:
                        out.append(Category(str(name), "Series", str(cid)))

        out = [c for c in out if c.name and c.category_id and c.category_id != "None"]
        out.sort(key=lambda c: c.name.lower())
        return out

    def _initial_list_url(self, category: Category, p: int = 0) -> Tuple[str, str]:
        if category.category_type == "IPTV":
            return (
                f"{self.base_url}/portal.php?type=itv&action=get_ordered_list&genre={category.category_id}&JsHttpRequest=1-xml&p={p}",
                "channel",
            )
        if category.category_type == "VOD":
            return (
                f"{self.base_url}/portal.php?type=vod&action=get_ordered_list&category={category.category_id}&JsHttpRequest=1-xml&p={p}",
                "vod",
            )
        if category.category_type == "Series":
            return (
                f"{self.base_url}/portal.php?type=series&action=get_ordered_list&category={category.category_id}&JsHttpRequest=1-xml&p={p}",
                "series",
            )
        return ("", "")

    def get_items_count(self, category: Category) -> Optional[int]:
        url, _ = self._initial_list_url(category, p=0)
        if not url:
            return None
        try:
            j = self._get_json(url)
            js = j.get("js", {}) if isinstance(j, dict) else {}
            if "total_items" in js:
                return int(js.get("total_items") or 0)
        except Exception:
            return None
        return None

    def get_items(self, category: Category, num_threads: int = 10, progress_cb=None) -> List[Item]:
        initial_url, item_type = self._initial_list_url(category, p=0)
        if not initial_url:
            return []

        r0 = self.session.get(initial_url, headers=self.headers, timeout=self.timeout)
        r0.raise_for_status()
        j0 = r0.json()
        js = j0.get("js", {}) if isinstance(j0, dict) else {}
        total_items = int(js.get("total_items", 0) or 0)
        first_data = js.get("data", []) or []
        per = len(first_data)
        total_pages = (total_items + per - 1) // per if per else 1

        all_data: List[Dict[str, Any]] = []
        all_data.extend(first_data)

        # progress after first page
        try:
            if callable(progress_cb):
                progress_cb(int(1 * 100 / max(1, int(total_pages))), f"Stranica 1/{int(total_pages)} | linkova: {len(all_data)}")
        except Exception:
            pass

        def fetch_page(p: int) -> List[Dict[str, Any]]:
            u, _ = self._initial_list_url(category, p=p)
            rr = self.session.get(u, headers=self.headers, timeout=self.timeout)
            rr.raise_for_status()
            jj = rr.json()
            return (jj.get("js", {}) or {}).get("data", []) or []

        if total_pages > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=max(2, int(num_threads))) as ex:
                futs = {ex.submit(fetch_page, p): p for p in range(1, total_pages)}
                done = 1
                for fut in as_completed(futs):
                    try:
                        all_data.extend(fut.result())
                    except Exception:
                        pass
                    finally:
                        done += 1
                        try:
                            if callable(progress_cb):
                                progress_cb(int(done * 100 / max(1, int(total_pages))), f"Stranica {done}/{int(total_pages)} | linkova: {len(all_data)}")
                        except Exception:
                            pass

        uniq: Dict[str, Dict[str, Any]] = {}
        for d in all_data:
            cid = d.get("id")
            if cid is None:
                continue
            sid = str(cid)
            if sid not in uniq:
                d["item_type"] = item_type
                uniq[sid] = d

        items = [Item(name=str(v.get("name") or v.get("title") or ""), item_type=item_type, raw=v) for v in uniq.values()]
        items = [i for i in items if i.name]
        items.sort(key=lambda x: x.name.lower())
        try:
            if callable(progress_cb):
                progress_cb(100, 'Gotovo')
        except Exception:
            pass
        return items

class StalkerLoadClient(BasePortalClient):
    """Stalker portals via /stalker_portal/server/load.php handshake token."""

    def __init__(self, base_url: str, mac: str, timeout: int = 12, adult_pin: str = "0000"):
        super().__init__(base_url, mac, timeout, adult_pin=adult_pin)
        self.token: Optional[str] = None
        self._load_endpoint: Optional[str] = None

    def _candidate_load_urls(self) -> List[str]:
        root = self.base_url.rstrip("/")
        return [
            f"{root}/stalker_portal/server/load.php",
            f"{root}/c/stalker_portal/server/load.php",
            f"{root}/server/load.php",
            f"{root}/stalker_portal/load.php",
            f"{root}/c/server/load.php",
        ]

    def _set_referer_for_url(self, load_url: str) -> None:
        root = self.base_url.rstrip("/")
        lu = load_url.lower()
        if "/stalker_portal/" in lu:
            self.headers["Referer"] = f"{root}/stalker_portal/c/"
        elif "/c/" in lu:
            self.headers["Referer"] = f"{root}/c/"
        else:
            self.headers["Referer"] = f"{root}/c/"

    def handshake(self) -> None:
        params = {"type": "stb", "action": "handshake", "JsHttpRequest": "1-xml"}
        last_err: Optional[Exception] = None

        for url in self._candidate_load_urls():
            try:
                self._set_referer_for_url(url)
                r = self.session.get(url, params=params, headers=self.headers, timeout=self.timeout)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                j = r.json()
                token = (j.get("js", {}) or {}).get("token")
                if token:
                    self._load_endpoint = url
                    self.token = str(token)
                    self.headers["Authorization"] = f"Bearer {self.token}"
                    return
            except Exception as e:
                last_err = e
                continue

        if last_err:
            raise last_err
        raise RuntimeError("Handshake nije uspio (load.php nije pronađen).")

    def _get_json(self, params: Dict[str, Any]) -> Any:
        if not self.token:
            self.handshake()
        url = self._load_endpoint or f"{self.base_url}/stalker_portal/server/load.php"
        try:
            self._set_referer_for_url(url)
        except Exception:
            pass
        r = self.session.get(url, params=params, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def get_account_expiry(self) -> Optional[datetime]:
        dt, _src = self.get_account_expiry_info()
        return dt

    def get_account_expiry_info(self) -> Tuple[Optional[datetime], str]:
        """Best-effort detekcija valjanosti liste (datum+vrijeme)."""

        # 1) account_info/get_main_info
        try:
            j = self._get_json({"type": "account_info", "action": "get_main_info", "JsHttpRequest": "1-xml"})
            js = j.get("js", {}) if isinstance(j, dict) else {}
            dt, src = _extract_expiry_from_js(js)
            if dt:
                return dt, f"account_info.{src}"
        except Exception:
            pass

        # 2) fallback: stb/get_profile
        try:
            j = self._get_json({"type": "stb", "action": "get_profile", "JsHttpRequest": "1-xml"})
            js = j.get("js", {}) if isinstance(j, dict) else {}
            dt, src = _extract_expiry_from_js(js)
            if dt:
                return dt, f"stb.profile.{src}"
        except Exception:
            return None, ""

        return None, ""
    def _create_link(self, item: Item) -> str:
        cmd = (item.url or "").strip()
        if not cmd:
            return ""
        if item.item_type == "channel":
            params = {"type": "itv", "action": "create_link", "cmd": cmd, "JsHttpRequest": "1-xml"}
        else:
            params = {"type": "vod", "action": "create_link", "cmd": cmd, "JsHttpRequest": "1-xml"}
        j = self._get_json(params)
        js = j.get("js", {}) if isinstance(j, dict) else {}
        out = (js.get("cmd") or js.get("url") or "")
        return str(out).strip()

    def get_categories(self, kind: str) -> List[Category]:
        out: List[Category] = []

        if kind == "IPTV":
            j = self._get_json({"type": "itv", "action": "get_genres", "JsHttpRequest": "1-xml"})
            raw = j.get("js", []) if isinstance(j, dict) else []
            for cat in raw:
                if isinstance(cat, dict) and cat.get("title") and cat.get("id"):
                    out.append(Category(str(cat["title"]), "IPTV", str(cat["id"])))

        elif kind == "Series":
            # Prefer dedicated series categories if the portal supports it
            series_raw: List[dict] = []
            try:
                j = self._get_json({"type": "series", "action": "get_categories", "JsHttpRequest": "1-xml"})
                series_raw = j.get("js", []) if isinstance(j, dict) else []
            except Exception:
                series_raw = []

            if series_raw:
                for cat in series_raw:
                    if not isinstance(cat, dict):
                        continue
                    name = cat.get("title") or cat.get("name") or cat.get("category_name")
                    cid = cat.get("id") or cat.get("category_id")
                    if name and cid:
                        out.append(Category(str(name), "Series", str(cid)))
            else:
                # Fallback: some portals expose series inside vod categories; use heuristic
                try:
                    j = self._get_json({"type": "vod", "action": "get_categories", "JsHttpRequest": "1-xml"})
                    raw = j.get("js", []) if isinstance(j, dict) else []
                except Exception:
                    raw = []

                def is_series(name: str) -> bool:
                    low = name.lower()
                    return any(k in low for k in ("tv", "series", "show", "serije", "tvshows", "shows"))

                for cat in raw:
                    if not isinstance(cat, dict):
                        continue
                    name = cat.get("title") or cat.get("name") or cat.get("category_name")
                    cid = cat.get("id") or cat.get("category_id")
                    if name and cid and is_series(str(name)):
                        out.append(Category(str(name), "Series", str(cid)))

        elif kind == "VOD":
            # Fetch vod categories (and optionally exclude ones that are clearly series if we also have series categories)
            try:
                j = self._get_json({"type": "vod", "action": "get_categories", "JsHttpRequest": "1-xml"})
                raw = j.get("js", []) if isinstance(j, dict) else []
            except Exception:
                raw = []

            # Try to fetch series categories to exclude duplicates if portal supports series
            series_ids = set()
            try:
                j2 = self._get_json({"type": "series", "action": "get_categories", "JsHttpRequest": "1-xml"})
                raw2 = j2.get("js", []) if isinstance(j2, dict) else []
                for cat in raw2:
                    if isinstance(cat, dict):
                        cid = cat.get("id") or cat.get("category_id")
                        if cid is not None:
                            series_ids.add(str(cid))
            except Exception:
                pass

            def is_series_name(name: str) -> bool:
                low = name.lower()
                return any(k in low for k in ("tv", "series", "show", "serije", "tvshows", "shows"))

            for cat in raw:
                if not isinstance(cat, dict):
                    continue
                name = cat.get("title") or cat.get("name") or cat.get("category_name")
                cid = cat.get("id") or cat.get("category_id")
                if not (name and cid):
                    continue
                scid = str(cid)
                if scid in series_ids:
                    continue
                # If we DON'T have dedicated series, avoid heuristic split: include everything as VOD
                if series_ids:
                    if is_series_name(str(name)):
                        continue
                out.append(Category(str(name), "VOD", scid))

        out = [c for c in out if c.name and c.category_id and c.category_id != "None"]
        out.sort(key=lambda c: c.name.lower())
        return out


    def get_items_count(self, category: Category) -> Optional[int]:
        base_params = {"action": "get_ordered_list", "JsHttpRequest": "1-xml"}
        if category.category_type == "IPTV":
            type_param = "itv"
            param_key = "genre"
        elif category.category_type in ("VOD", "Series"):
            type_param = "vod"
            param_key = "category"
        else:
            return None

        for p_try in (0, 1):
            try:
                p0 = dict(base_params)
                p0.update({"type": type_param, param_key: category.category_id, "p": p_try})
                j0 = self._get_json(p0)
                js = j0.get("js", {}) if isinstance(j0, dict) else {}
                if "total_items" in js:
                    return int(js.get("total_items", 0) or 0)
                for alt in ("total", "total_count", "count"):
                    if alt in js:
                        return int(js.get(alt, 0) or 0)
            except Exception:
                continue
        return None

    def get_items(self, category: Category, num_threads: int = 10, progress_cb=None) -> List[Item]:
        base_params = {"action": "get_ordered_list", "JsHttpRequest": "1-xml"}

        if category.category_type == "IPTV":
            type_param = "itv"
            param_key = "genre"
            item_type = "channel"
        elif category.category_type == "VOD":
            type_param = "vod"
            param_key = "category"
            item_type = "vod"
        elif category.category_type == "Series":
            type_param = "vod"
            param_key = "category"
            item_type = "series"
        else:
            return []

        def fetch_js(p: int) -> Dict[str, Any]:
            pp = dict(base_params)
            pp.update({"type": type_param, param_key: category.category_id, "p": p})
            jj = self._get_json(pp)
            return (jj.get("js", {}) or {}) if isinstance(jj, dict) else {}

        js0 = fetch_js(1)
        total_raw = js0.get("total_items", "0")
        try:
            total_items = int(total_raw)
        except Exception:
            total_items = len(js0.get("data", []) or [])

        first_data = js0.get("data", []) or []
        per = len(first_data)
        total_pages = (total_items + per - 1) // per if per else 1

        all_data: List[Dict[str, Any]] = []
        all_data.extend(first_data)

        # progress after first page
        try:
            if callable(progress_cb):
                progress_cb(int(1 * 100 / max(1, int(total_pages))), f"Stranica 1/{int(total_pages)} | linkova: {len(all_data)}")
        except Exception:
            pass

        if total_pages > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=max(2, int(num_threads))) as ex:
                futs = {ex.submit(fetch_js, p): p for p in range(2, total_pages + 1)}
                done = 1
                for fut in as_completed(futs):
                    try:
                        jsd = fut.result()
                        all_data.extend(jsd.get("data", []) or [])
                    except Exception:
                        pass
                    finally:
                        done += 1
                        try:
                            if callable(progress_cb):
                                progress_cb(int(done * 100 / max(1, int(total_pages))), f"Stranica {done}/{int(total_pages)} | linkova: {len(all_data)}")
                        except Exception:
                            pass

        uniq: Dict[str, Dict[str, Any]] = {}
        for d in all_data:
            cid = d.get("id") or d.get("movie_id") or d.get("channel_id")
            if cid is None:
                continue
            sid = str(cid)
            if sid not in uniq:
                d["item_type"] = item_type
                uniq[sid] = d

        items = [Item(name=str(v.get("name") or v.get("title") or ""), item_type=item_type, raw=v) for v in uniq.values()]
        items = [i for i in items if i.name]
        items.sort(key=lambda x: x.name.lower())
        return items


def _normalize_portal_base(url: str) -> str:
    """Normalize portal base URL.

    - strips known suffixes (/c, /portal.php, /stalker_portal/...)
    - keeps scheme/host if provided
    - if scheme is missing, we default to https:// (most portals require TLS)
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""

    # If user entered without scheme (e.g. synciptv.org), prefer HTTPS.
    if not re.match(r"^https?://", u, flags=re.I):
        u = "https://" + u

    suffixes = [
        "/c", "/c/",
        "/stalker_portal", "/stalker_portal/",
        "/stalker_portal/server", "/stalker_portal/server/",
        "/stalker_portal/server/load.php", "/stalker_portal/server/load.php/",
        "/portal.php", "/portal.php/",
        "/c/portal.php", "/c/portal.php/",
    ]
    changed = True
    while changed:
        changed = False
        for s in suffixes:
            if u.lower().endswith(s):
                u = u[:-len(s)].rstrip("/")
                changed = True
                break
    return u


def _scheme_variants(base_url: str) -> list[str]:
    """Return URL variants to try (prefer https).

    NOTE about ports:
    - If we switch a URL from http -> https we must NOT keep an explicit :80
      (otherwise requests will try HTTPS on port 80 and fail/refuse).
    - Likewise, if we switch from https -> http we should NOT keep :443.

    The goal is: try the same host/path on the appropriate default port first.
    """
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return []

    def _swap_scheme_keep_path(url: str, scheme: str) -> str:
        p = urlparse(url)
        host = p.hostname or ""
        port = p.port
        # Drop scheme-mismatched explicit default ports.
        if scheme.lower() == "https" and port == 80:
            port = None
        if scheme.lower() == "http" and port == 443:
            port = None
        # Rebuild netloc.
        netloc = host
        if port is not None:
            netloc = f"{host}:{port}"
        # Preserve path (and anything already in it).
        return urlunparse((scheme, netloc, p.path or "", "", "", ""))

    if re.match(r"^https?://", u, flags=re.I):
        https_u = _swap_scheme_keep_path(u, "https")
        http_u  = _swap_scheme_keep_path(u, "http")

        if u.lower().startswith("https://"):
            return [https_u, http_u] if http_u != https_u else [https_u]
        else:
            out = [https_u]
            if http_u not in out:
                out.append(http_u)
            if u not in out:
                out.append(u)
            return out

    # no scheme (should not happen after _normalize_portal_base, but just in case)
    # Prefer https by default.
    return ["https://" + u, "http://" + u]




def build_auto_client(base_url: str, mac: str, adult_pin: str = "0000") -> BasePortalClient:
    base_url = _normalize_portal_base(base_url)

    # Try https fallback automatically to avoid timeouts on port 80.
    last_err: Exception | None = None
    for bu in _scheme_variants(base_url) or [base_url]:
        s = requests.Session()
        try:
            s.cookies.set("mac", mac)
            s.cookies.set("stb_lang", "en")
            s.cookies.set("parent_password", (adult_pin or COMMON_ADULT_PINS[0]).strip() or COMMON_ADULT_PINS[0])
            s.cookies.set("timezone", "Europe/Zagreb")
            headers = {
                "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
                "X-User-Agent": "Model: MAG254; Link: Ethernet",
                "Referer": f"{bu}/c/",
            }
            test_urls = [
                f"{bu}/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml",
                        f"{bu}/c/portal.php?type=itv&action=get_genres&JsHttpRequest=1-xml",
            ]
            for test_url in test_urls:
                try:
                    r = s.get(test_url, headers=headers, timeout=PORTAL_CONNECT_TIMEOUT)
                    if r.status_code < 400:
                        _ = r.json()
                        return PortalPhpClient(bu, mac, adult_pin=adult_pin)
                except Exception as e:
                    last_err = e
                    continue
            # If portal.php probing didn't work, validate that the Stalker handshake is reachable
            # before committing to this base URL; otherwise try the next scheme variant.
            try:
                probe = StalkerLoadClient(bu, mac, timeout=PORTAL_CONNECT_TIMEOUT)
                probe.set_adult_pin(adult_pin)
                probe.handshake()
                return StalkerLoadClient(bu, mac, adult_pin=adult_pin)
            except Exception as e:
                last_err = e
                continue
        except Exception as e:
            last_err = e
        finally:
            try:
                s.close()
            except Exception:
                pass

    if last_err:
        raise last_err
    return StalkerLoadClient(base_url, mac, adult_pin=adult_pin)

class WorkerSignals(QtCore.QObject):
    finished = QtCore.Signal(object)
    error = QtCore.Signal(str)
    log = QtCore.Signal(str)
    progress = QtCore.Signal(int, str)  # percent, message


class ResolveItemLinkWorker(QtCore.QRunnable):
    def __init__(
        self,
        client: BasePortalClient,
        category: Category,
        item: Item,
    ):
        super().__init__()
        self.client = client
        self.category = category
        self.item = item
        self.signals = WorkerSignals()

    @QtCore.Slot()
    def run(self):
        try:
            url = (self.client.resolve_play_url(self.item) or "").strip()
            if self.category.category_type == "IPTV":
                url = maybe_fix_localhost_stream(
                    url,
                    self.client.base_url,
                    self.client.mac,
                )
            if not url:
                raise RuntimeError("Portal nije vratio link za reprodukciju.")
            self.signals.finished.emit(
                {
                    "item": self.item,
                    "category": self.category,
                    "url": url,
                    "details": build_generated_item_details(
                        self.item,
                        self.category,
                        self.client.base_url,
                        self.client.mac,
                        url,
                    ),
                }
            )
        except Exception as error:
            self.signals.error.emit(str(error) or type(error).__name__)


def fetch_categories_with_progress(
    client: BasePortalClient,
    auto_adult_pins: bool,
    progress_cb=None,
    log_cb=None,
    cancel_cb=None,
) -> List[Category]:
    cats: List[Category] = []
    kinds = ("IPTV", "VOD", "Series")
    seen: set[Tuple[str, str]] = set()
    adult_found = False
    original_pin = client.adult_pin
    pins = client.iter_adult_pins() if auto_adult_pins else (original_pin,)
    total_steps = max(1, len(pins) * len(kinds))
    step = 0

    for pin_idx, pin in enumerate(pins, start=1):
        if callable(cancel_cb) and cancel_cb():
            break
        client.set_adult_pin(pin)
        if auto_adult_pins and pin_idx == 1 and callable(log_cb):
            log_cb(f"Adult PIN: pokušavam {pin}")
        elif auto_adult_pins and callable(log_cb):
            log_cb(f"Adult PIN: pokušavam dodatni PIN {pin}")

        pin_cats: List[Category] = []
        for kind in kinds:
            if callable(cancel_cb) and cancel_cb():
                break
            if callable(log_cb):
                log_cb(f"Povlačim kategorije: {kind}...")
            if callable(progress_cb):
                progress_cb(int(step * 100 / total_steps), f"{kind}... ({step + 1}/{total_steps})")

            pin_cats.extend(client.get_categories(kind))
            step += 1

            if callable(progress_cb):
                progress_cb(int(step * 100 / total_steps), f"Gotovo: {kind} ({step}/{total_steps})")

        for c in pin_cats:
            key = (c.category_type, c.category_id)
            if key in seen:
                continue
            seen.add(key)
            cats.append(c)

        if any(looks_adult_category(c.name) for c in pin_cats):
            adult_found = True
            if auto_adult_pins and callable(log_cb):
                log_cb(f"Adult kategorije pronađene s PIN-om {pin}.")
            break

    if auto_adult_pins and not adult_found:
        client.set_adult_pin(original_pin)
    return cats


class FetchCategoriesWorker(QtCore.QRunnable):
    def __init__(self, client: BasePortalClient, auto_adult_pins: bool = False):
        super().__init__()
        self.client = client
        self.auto_adult_pins = bool(auto_adult_pins)
        self.signals = WorkerSignals()
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @QtCore.Slot()
    def run(self):
        try:
            self.signals.progress.emit(0, "Start")
            cats = fetch_categories_with_progress(
                self.client,
                self.auto_adult_pins,
                progress_cb=self.signals.progress.emit,
                log_cb=self.signals.log.emit,
                cancel_cb=lambda: self._cancel,
            )
            self.signals.progress.emit(100, "Učitavanje grupa završeno")
            self.signals.finished.emit(cats)
        except Exception as e:
            self.signals.error.emit(str(e))


class ConnectCategoriesWorker(QtCore.QRunnable):
    def __init__(self, base_url: str, mac: str, adult_pin: str, auto_adult_pins: bool):
        super().__init__()
        self.base_url = base_url
        self.mac = mac
        self.adult_pin = adult_pin
        self.auto_adult_pins = bool(auto_adult_pins)
        self.signals = WorkerSignals()
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @QtCore.Slot()
    def run(self):
        client: Optional[BasePortalClient] = None
        try:
            self.signals.progress.emit(0, "Spajam se na portal...")
            client = build_auto_client(self.base_url, self.mac, adult_pin=self.adult_pin)
            if self._cancel:
                client.close()
                self.signals.finished.emit({"client": None, "categories": [], "canceled": True})
                return

            self.signals.progress.emit(10, "Portal pronađen, učitavam grupe...")

            def progress(pct: int, msg: str):
                self.signals.progress.emit(10 + int(max(0, min(100, pct))) * 90 // 100, msg)

            cats = fetch_categories_with_progress(
                client,
                self.auto_adult_pins,
                progress_cb=progress,
                log_cb=self.signals.log.emit,
                cancel_cb=lambda: self._cancel,
            )
            self.signals.progress.emit(100, "Učitavanje grupa završeno")
            self.signals.finished.emit({"client": client, "categories": cats, "canceled": self._cancel})
        except Exception as e:
            if client:
                client.close()
            self.signals.error.emit(str(e))


class TestPortalWorker(QtCore.QRunnable):
    def __init__(self, base_url: str, mac: str, adult_pin: str):
        super().__init__()
        self.base_url = base_url
        self.mac = mac
        self.adult_pin = adult_pin
        self.signals = WorkerSignals()

    @QtCore.Slot()
    def run(self):
        client: Optional[BasePortalClient] = None
        try:
            self.signals.progress.emit(10, "Testiram portal...")
            client = build_auto_client(self.base_url, self.mac, adult_pin=self.adult_pin)
            self.signals.progress.emit(50, "Portal odgovara, provjeravam kategorije...")
            cats = client.get_categories("IPTV")
            self.signals.finished.emit({
                "ok": True,
                "client": type(client).__name__,
                "live_categories": len(cats),
                "base_url": client.base_url,
            })
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            if client:
                client.close()


class FetchCountsWorker(QtCore.QRunnable):
    def __init__(self, client: BasePortalClient, categories: List[Category], num_threads: int, max_per_run: int = 5000):
        super().__init__()
        self.client = client
        self.categories = categories
        self.num_threads = max(2, int(num_threads))
        self.max_per_run = max_per_run
        self.signals = WorkerSignals()

    @QtCore.Slot()
    def run(self):
        try:
            out: List[Tuple[str, str, Optional[int]]] = []
            categories = list(self.categories[: int(self.max_per_run)])
            total = len(categories)
            try:
                self.signals.progress.emit(0, f"Brojim sadržaj: 0/{total}")
            except Exception:
                pass

            def fetch_count(c: Category) -> Tuple[str, str, Optional[int]]:
                try:
                    cnt = self.client.get_items_count(c)
                    return (c.category_type, c.category_id, cnt)
                except Exception:
                    return (c.category_type, c.category_id, None)

            max_workers = max(2, min(self.num_threads, 12))
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = [ex.submit(fetch_count, c) for c in categories]
                for n, fut in enumerate(as_completed(futs), start=1):
                    out.append(fut.result())
                    try:
                        pct = int(n * 100 / max(1, total))
                        self.signals.progress.emit(pct, f"Brojim sadržaj: {n}/{total}")
                    except Exception:
                        pass
            self.signals.finished.emit(out)
        except Exception as e:
            self.signals.error.emit(str(e))


class AccountExpiryWorker(QtCore.QRunnable):
    def __init__(self, client: BasePortalClient):
        super().__init__()
        self.client = client
        self.signals = WorkerSignals()

    @QtCore.Slot()
    def run(self):
        try:
            self.signals.finished.emit(self.client.get_account_expiry_info())
        except Exception as e:
            self.signals.error.emit(str(e))


class DetectExpiryWorker(QtCore.QRunnable):
    def __init__(self, client: BasePortalClient, sample_categories: List[Category], num_threads: int, items_per_category: int = 25):
        super().__init__()
        self.client = client
        self.sample_categories = sample_categories
        self.num_threads = num_threads
        self.items_per_category = items_per_category
        self.signals = WorkerSignals()

    @QtCore.Slot()
    def run(self):
        try:
            best: Optional[datetime] = None
            checked = 0
            total = max(1, len(self.sample_categories))
            for cat_idx, cat in enumerate(self.sample_categories, start=1):
                self.signals.progress.emit(
                    int((cat_idx - 1) * 100 / total),
                    f"Valjanost: grupa {cat_idx}/{total}",
                )
                try:
                    items = self.client.get_items(cat, num_threads=max(2, int(self.num_threads)))
                except Exception:
                    continue
                for it in items[: self.items_per_category]:
                    url = self.client.resolve_play_url(it)
                    dt = extract_expiry_from_url(url)
                    if dt:
                        best = dt if (best is None or dt > best) else best
                    checked += 1
                    if checked >= 120:
                        break
                if checked >= 120:
                    break
            self.signals.progress.emit(100, f"Valjanost: provjereno {checked} linkova")
            self.signals.finished.emit(best)
        except Exception as e:
            self.signals.error.emit(str(e))


class ExportWorker(QtCore.QRunnable):
    """Background export + faster IO via chunked writes.

    fast_export applies ONLY for IPTV (Live TV).
    For VOD/TV Shows, fast_export is ignored and normal resolve is used.
    """

    def __init__(
        self,
        client: BasePortalClient,
        categories: List[Category],
        path: str,
        num_threads: int,
        fast_export: bool,
        cache: Dict[Tuple[str, str], List[Item]],
        item_selection: Dict[Tuple[str, str, str], bool],
        check_links: bool = False,
    ):
        super().__init__()
        self.client = client
        self.categories = categories
        self.path = path
        self.num_threads = max(2, int(num_threads))
        self.fast_export = bool(fast_export)
        self.check_links = bool(check_links)
        self.cache = cache
        # Snapshot of selection state at the time export starts.
        self.item_selection = dict(item_selection or {})
        self.signals = WorkerSignals()
        self._cancel = False

    def cancel(self):
        self._cancel = True

    @QtCore.Slot()
    def run(self):
        try:
            lines_written = 0
            links_written = 0
            links_by_type: Dict[str, int] = {"IPTV": 0, "VOD": 0, "Series": 0}
            groups_by_type: Dict[str, int] = {"IPTV": 0, "VOD": 0, "Series": 0}
            exported_urls: List[str] = []
            best_exp: Optional[datetime] = None

            def emit_progress(pct: int, msg: str):
                try:
                    self.signals.progress.emit(int(max(0, min(100, pct))), str(msg or ""))
                except Exception:
                    pass

            emit_progress(0, "Priprema exporta...")

            with open(self.path, "w", encoding="utf-8", newline="\n") as f:
                f.write("#EXTM3U\n")
                lines_written += 1

                total_groups = len(self.categories)
                for g_idx, cat in enumerate(self.categories, start=1):
                    if self._cancel:
                        break

                    self.signals.log.emit(f"Export: {cat.category_type} / {cat.name}")
                    groups_by_type[cat.category_type] = groups_by_type.get(cat.category_type, 0) + 1

                    key = (cat.category_type, cat.category_id)
                    items = self.cache.get(key)
                    if items is None:
                        items = self.client.get_items(cat, num_threads=self.num_threads)
                        self.cache[key] = items

                    type_label = category_type_label(cat.category_type)
                    group_name = cat.name.replace('"', "'")
                    group_title = f"{type_label} | {group_name}"
                    chunk: List[str] = []
                    chunk.append(f'# --- {type_label} / {group_name} ---')
                    chunk.append(f'#EXTGRP:{group_title}')
                    lines_written += 2

                    # Snapshot selekcije za ovu grupu
                    selected_items: List[Item] = []
                    for it in items:
                        uid = item_uid(it)
                        if self.item_selection.get((cat.category_type, cat.category_id, uid), True):
                            selected_items.append(it)

                    total_sel = len(selected_items)
                    done_sel = 0
                    before_group_links = links_written

                    use_fast_for_this = self.fast_export and (cat.category_type == "IPTV")

                    if use_fast_for_this:
                        for it in selected_items:
                            if self._cancel:
                                break
                            url = normalize_cmd_or_url((it.url or "").strip())
                            url = maybe_fix_localhost_stream(url, self.client.base_url, self.client.mac)
                            if not url:
                                continue

                            dt = extract_expiry_from_url(url)
                            if dt:
                                best_exp = dt if (best_exp is None or dt > best_exp) else best_exp

                            chunk.append(build_extinf_line(it, group_title))
                            chunk.append(url)
                            exported_urls.append(url)
                            lines_written += 2
                            links_written += 1
                            links_by_type[cat.category_type] = links_by_type.get(cat.category_type, 0) + 1
                            done_sel += 1

                            if done_sel % 50 == 0 or done_sel == total_sel:
                                pct = int(done_sel * 100 / max(1, total_sel))
                                emit_progress(pct, f"{cat.category_type} {cat.name}: {done_sel}/{total_sel} linkova (grupa {g_idx}/{total_groups})")
                                if done_sel % 200 == 0:
                                    self.signals.log.emit(f"Exportirano linkova: {links_written}")

                    else:
                        # Paralelno resolve-anje za VOD/TV Shows (i IPTV kad fast_export nije upaljen)
                        def build_entry(it: Item):
                            if self._cancel:
                                return None
                            raw = normalize_cmd_or_url((it.url or "").strip())
                            url = raw if (raw.startswith("http://") or raw.startswith("https://")) else ""
                            if not url:
                                url = self.client.resolve_play_url(it)

                            if cat.category_type == "IPTV":
                                url = maybe_fix_localhost_stream(url, self.client.base_url, self.client.mac)

                            if not url:
                                return None

                            dt = extract_expiry_from_url(url)
                            lines = [build_extinf_line(it, group_title), url]
                            return lines, dt

                        local_best: Optional[datetime] = None
                        max_workers = max(2, min(self.num_threads, 20))

                        with ThreadPoolExecutor(max_workers=max_workers) as ex:
                            futures = [ex.submit(build_entry, it) for it in selected_items]
                            for fut in as_completed(futures):
                                if self._cancel:
                                    break
                                out = None
                                try:
                                    out = fut.result()
                                except Exception:
                                    out = None
                                if not out:
                                    # i dalje brojimo kao 'obrađeno' da progress ide naprijed
                                    done_sel += 1
                                else:
                                    lines2, dt = out
                                    chunk.extend(lines2)
                                    if len(lines2) >= 2:
                                        exported_urls.append(lines2[1])
                                    lines_written += len(lines2)
                                    links_written += 1
                                    done_sel += 1
                                    if dt:
                                        local_best = dt if (local_best is None or dt > local_best) else local_best
                                    links_by_type[cat.category_type] = links_by_type.get(cat.category_type, 0) + 1

                                if done_sel % 50 == 0 or done_sel == total_sel:
                                    pct = int(done_sel * 100 / max(1, total_sel))
                                    emit_progress(pct, f"{cat.category_type} {cat.name}: {done_sel}/{total_sel} linkova (grupa {g_idx}/{total_groups})")
                                    if done_sel % 200 == 0:
                                        self.signals.log.emit(f"Exportirano linkova: {links_written}")

                        if local_best:
                            best_exp = local_best if (best_exp is None or local_best > best_exp) else best_exp

                    # Write chunk at end of group (single IO)
                    if chunk:
                        f.write("\n".join(chunk) + "\n")
                    group_written = links_written - before_group_links
                    self.signals.log.emit(
                        f"Grupa export: {category_type_label(cat.category_type)} / {cat.name} | "
                        f"učitano: {len(items)}, odabrano: {total_sel}, zapisano: {group_written}"
                    )

                    # After each group: bump progress to show group boundary
                    emit_progress(100, f"Gotovo: {cat.category_type} {cat.name} ({g_idx}/{total_groups}) | ukupno linkova: {links_written}")

            dead_links = 0
            checked_links = 0
            if self.check_links and exported_urls and not self._cancel:
                emit_progress(0, "Provjeravam linkove...")

                def check_url(url: str) -> bool:
                    try:
                        r = self.client.session.get(url, headers=self.client.headers, timeout=5, stream=True)
                        try:
                            return r.status_code < 400
                        finally:
                            r.close()
                    except Exception:
                        return False

                max_workers = max(2, min(self.num_threads, 16))
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = [ex.submit(check_url, u) for u in exported_urls]
                    total = len(futs)
                    for n, fut in enumerate(as_completed(futs), start=1):
                        if self._cancel:
                            break
                        checked_links += 1
                        if not fut.result():
                            dead_links += 1
                        if n % 25 == 0 or n == total:
                            emit_progress(int(n * 100 / max(1, total)), f"Provjera linkova: {n}/{total} | neuspjelo: {dead_links}")

            # Final signal (done)
            self.signals.finished.emit({
                "done": True,
                "canceled": self._cancel,
                "best_exp": best_exp,
                "lines": lines_written,
                "links": links_written,
                "links_by_type": links_by_type,
                "groups_by_type": groups_by_type,
                "checked_links": checked_links,
                "dead_links": dead_links,
            })
        except Exception as e:
            self.signals.error.emit(str(e))


# -----------------------------
# Models / filtering
# -----------------------------

COL_CHECK = 0
COL_NAME = 1
COL_COUNT = 2
COL_ID = 3


class CategoryModel(QtGui.QStandardItemModel):
    def __init__(self):
        super().__init__(0, 4)
        self.setHorizontalHeaderLabels(["", "Grupa/Kategorija", "Linkova", "ID"])

    def add_categories(self, categories: List[Category]):
        self.setRowCount(0)
        for c in categories:
            check_item = QtGui.QStandardItem()
            check_item.setCheckable(True)
            check_item.setCheckState(QtCore.Qt.Unchecked)
            check_item.setEditable(False)

            name_item = QtGui.QStandardItem(c.name)
            name_item.setEditable(False)

            count_item = QtGui.QStandardItem("…")
            count_item.setEditable(False)
            count_item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

            id_item = QtGui.QStandardItem(c.category_id)
            id_item.setEditable(False)

            name_item.setData(c, QtCore.Qt.UserRole)
            self.appendRow([check_item, name_item, count_item, id_item])

    def iter_checked_categories(self) -> List[Category]:
        out: List[Category] = []
        for r in range(self.rowCount()):
            it = self.item(r, COL_CHECK)
            if it and it.checkState() == QtCore.Qt.Checked:
                c = self.item(r, COL_NAME).data(QtCore.Qt.UserRole)
                if isinstance(c, Category):
                    out.append(c)
        return out

    def set_count_for_category_id(self, category_id: str, count: Optional[int]):
        for r in range(self.rowCount()):
            id_it = self.item(r, COL_ID)
            if id_it and id_it.text() == str(category_id):
                cnt_it = self.item(r, COL_COUNT)
                if cnt_it:
                    cnt_it.setText(str(count) if isinstance(count, int) else "?")
                return


class CategoryFilterProxy(QtCore.QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self._needle = ""

    def set_filter_text(self, text: str):
        self._needle = (text or "").strip().lower()
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QtCore.QModelIndex) -> bool:
        if not self._needle:
            return True
        m = self.sourceModel()
        if m is None:
            return True
        name_idx = m.index(source_row, COL_NAME, source_parent)
        name = str(m.data(name_idx) or "").lower()
        return self._needle in name


# -----------------------------
# Tab widget
# -----------------------------

class GroupsTab(QtWidgets.QWidget):
    category_double_clicked = QtCore.Signal(object)
    def __init__(self, title: str, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.title = title

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        actions = QtWidgets.QHBoxLayout()
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Live filter...")
        self.filter_edit.setClearButtonEnabled(True)

        self.btn_check_all = QtWidgets.QPushButton("Označi sve")
        self.btn_check_visible = QtWidgets.QPushButton("Označi vidljive")
        self.btn_uncheck_all = QtWidgets.QPushButton("Makni sve")
        for b in (self.btn_check_all, self.btn_check_visible, self.btn_uncheck_all):
            b.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        actions.addWidget(self.filter_edit, 1)
        actions.addWidget(self.btn_check_all)
        actions.addWidget(self.btn_check_visible)
        actions.addWidget(self.btn_uncheck_all)
        root.addLayout(actions)

        self.table = QtWidgets.QTableView()
        self.table.setObjectName("MainTable")
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        root.addWidget(self.table, 1)

        self.model = CategoryModel()
        self.proxy = CategoryFilterProxy()
        self.proxy.setSourceModel(self.model)
        self.table.setModel(self.proxy)

        self.table.horizontalHeader().setSectionResizeMode(COL_CHECK, QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QtWidgets.QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_COUNT, QtWidgets.QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(COL_ID, QtWidgets.QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)

        self.filter_edit.textChanged.connect(self.proxy.set_filter_text)
        self.btn_check_all.clicked.connect(lambda: self.set_all_checks(True))
        self.btn_check_visible.clicked.connect(lambda: self.set_visible_checks(True))
        self.btn_uncheck_all.clicked.connect(lambda: self.set_all_checks(False))
        # Dvoklik na grupu otvara prikaz sadržaja grupe
        self.table.doubleClicked.connect(self._on_double_clicked)

    def _on_double_clicked(self, proxy_index: QtCore.QModelIndex):
        try:
            if not proxy_index.isValid():
                return
            src = self.proxy.mapToSource(proxy_index)
            row = src.row()
            it = self.model.item(row, COL_NAME)
            c = it.data(QtCore.Qt.UserRole) if it else None
            if isinstance(c, Category):
                self.category_double_clicked.emit(c)
        except Exception:
            pass


    def set_all_checks(self, checked: bool):
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        for r in range(self.model.rowCount()):
            it = self.model.item(r, COL_CHECK)
            if it:
                it.setCheckState(state)

    def set_visible_checks(self, checked: bool):
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        for pr in range(self.proxy.rowCount()):
            src = self.proxy.mapToSource(self.proxy.index(pr, COL_CHECK))
            it = self.model.item(src.row(), COL_CHECK)
            if it:
                it.setCheckState(state)

    def iter_checked_categories(self) -> List[Category]:
        return self.model.iter_checked_categories()



# -----------------------------
# Group contents dialog
# -----------------------------

class FetchItemsForDialogWorker(QtCore.QRunnable):
    def __init__(self, client: BasePortalClient, category: Category, num_threads: int):
        super().__init__()
        self.client = client
        self.category = category
        self.num_threads = max(2, int(num_threads))
        self.signals = WorkerSignals()

    @QtCore.Slot()
    def run(self):
        # Neki portali znaju vratiti prazno ili timeout na prvu.
        # Napravimo 2 pokušaja s malim delayem.
        last_err = None
        for attempt in (1, 2):
            try:
                def _pcb(pct, msg=''):
                    try:
                        self.signals.progress.emit(int(pct), str(msg or ''))
                    except Exception:
                        pass

                items = self.client.get_items(
                    self.category,
                    num_threads=self.num_threads,
                    progress_cb=_pcb
                )

                # Ako je prazan rezultat, još jednom pokušaj (često je transient).
                if attempt == 1 and isinstance(items, list) and len(items) == 0:
                    QtCore.QThread.msleep(250)
                    continue

                self.signals.finished.emit(items if isinstance(items, list) else [])
                return
            except Exception as e:
                last_err = e
                if attempt == 1:
                    QtCore.QThread.msleep(250)
                    continue

        self.signals.error.emit(str(last_err) if last_err else "Nepoznata greška")


class GroupContentsDialog(QtWidgets.QDialog):
    retry_requested = QtCore.Signal()
    """Dialog with list of items in a group.

    - Live filter (as you type)
    - Per-program checkbox selection (used during export)

    Note: selection_store keeps only unchecked items (False). Missing key => selected.
    """

    def __init__(
        self,
        parent: QtWidgets.QWidget,
        category: Category,
        selection_store: Dict[Tuple[str, str, str], bool],
        client: BasePortalClient,
        thread_pool: QtCore.QThreadPool,
    ):
        super().__init__(parent)
        self.category = category
        self.selection_store = selection_store
        self.client = client
        self.thread_pool = thread_pool
        self._resolving_link = False

        self.setWindowTitle(f"{category.category_type} – {category.name}")
        self.resize(820, 680)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        top = QtWidgets.QHBoxLayout()
        self.filter_edit = QtWidgets.QLineEdit()
        self.filter_edit.setPlaceholderText("Live filter (traži po nazivu)...")
        self.filter_edit.setClearButtonEnabled(True)

        self.btn_check_all = QtWidgets.QPushButton("Označi sve")
        self.btn_uncheck_all = QtWidgets.QPushButton("Makni sve")
        self.btn_retry = QtWidgets.QPushButton("Retry")
        for b in (self.btn_check_all, self.btn_uncheck_all, self.btn_retry):
            b.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.count_lbl = QtWidgets.QLabel("—")
        self.count_lbl.setStyleSheet("color: #a9b4c3;")

        top.addWidget(self.filter_edit, 1)
        top.addWidget(self.btn_check_all)
        top.addWidget(self.btn_uncheck_all)
        top.addWidget(self.btn_retry)
        top.addWidget(self.count_lbl)
        root.addLayout(top)

        hint = QtWidgets.QLabel(
            "Dvoklik na kanal, film ili epizodu generira tokenizirani link i odmah "
            "pokreće sadržaj u VLC playeru. Dodatne opcije dostupne su desnim klikom."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #a9b4c3;")
        root.addWidget(hint)

        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("Učitavam programe: %p%")
        self.progress.setVisible(False)
        root.addWidget(self.progress)

        self.listw = QtWidgets.QListWidget()
        self.listw.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.listw.setAlternatingRowColors(True)
        self.listw.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        root.addWidget(self.listw, 1)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        root.addWidget(btns)

        self._all_items: List[Item] = []

        self.filter_edit.textChanged.connect(self._apply_filter)
        self.listw.itemChanged.connect(self._on_item_changed)
        self.btn_check_all.clicked.connect(lambda: self._set_all_visible(True))
        self.btn_uncheck_all.clicked.connect(lambda: self._set_all_visible(False))
        self.btn_retry.clicked.connect(lambda: self.retry_requested.emit())
        self.listw.itemDoubleClicked.connect(self._generate_item_link)
        self.listw.customContextMenuRequested.connect(self._item_context_menu)

    def set_loading(self, msg: str = "Učitavam..."):
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.listw.clear()
        it = QtWidgets.QListWidgetItem(msg)
        it.setFlags(QtCore.Qt.NoItemFlags)
        self.listw.addItem(it)
        self.count_lbl.setText("")

    def set_progress(self, percent: int, msg: str = ''):
        try:
            self.progress.setVisible(True)
            self.progress.setValue(max(0, min(100, int(percent))))
            if msg:
                self.progress.setFormat(f"{msg}: %p%")
            else:
                self.progress.setFormat("Učitavam programe: %p%")
        except Exception:
            pass

    def set_items(self, items: List[Item]):
        self.progress.setVisible(False)
        self._all_items = list(items or [])
        self._all_items.sort(key=lambda x: (x.name or "").lower())
        self._apply_filter()

    def _apply_filter(self):
        needle = (self.filter_edit.text() or "").strip().lower()

        self.listw.blockSignals(True)
        self.listw.clear()

        shown = 0
        for it in self._all_items:
            name = (it.name or "").strip()
            if not name:
                continue
            if needle and needle not in name.lower():
                continue

            uid = item_uid(it)
            key = (self.category.category_type, self.category.category_id, uid)
            selected = bool(self.selection_store.get(key, True))

            li = QtWidgets.QListWidgetItem(name)
            li.setFlags(li.flags() | QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            li.setCheckState(QtCore.Qt.Checked if selected else QtCore.Qt.Unchecked)
            li.setData(QtCore.Qt.UserRole, uid)
            li.setToolTip(
                "Dvoklik generira tokenizirani link i odmah pokreće sadržaj u VLC-u."
            )

            # URL (ako postoji) kao tooltip
            try:
                u = it.url
                if u:
                    li.setToolTip(
                        "Dvoklik generira tokenizirani link i odmah pokreće sadržaj u VLC-u.\n\n"
                        f"Izvorno: {u}"
                    )
            except Exception:
                pass

            self.listw.addItem(li)
            shown += 1

        self.listw.blockSignals(False)
        self._update_count_label(shown=shown)

    def _update_count_label(self, shown: Optional[int] = None):
        try:
            total = len(self._all_items)
            if shown is None:
                shown = self.listw.count()
            selected_total = 0
            for it in self._all_items:
                uid = item_uid(it)
                key = (self.category.category_type, self.category.category_id, uid)
                if bool(self.selection_store.get(key, True)):
                    selected_total += 1
            self.count_lbl.setText(f"Prikaz: {shown}/{total} | Označeno: {selected_total}/{total}")
        except Exception:
            self.count_lbl.setText("—")

    def _on_item_changed(self, li: QtWidgets.QListWidgetItem):
        try:
            uid = li.data(QtCore.Qt.UserRole)
            if not uid:
                return
            key = (self.category.category_type, self.category.category_id, str(uid))
            selected = (li.checkState() == QtCore.Qt.Checked)
            # Keep the store small: missing key => selected.
            if selected:
                self.selection_store.pop(key, None)
            else:
                self.selection_store[key] = False
            self._update_count_label()
        except Exception:
            pass

    def _item_for_list_entry(self, li: QtWidgets.QListWidgetItem) -> Optional[Item]:
        uid = str(li.data(QtCore.Qt.UserRole) or "")
        if not uid:
            return None
        return next((item for item in self._all_items if item_uid(item) == uid), None)

    def _generate_item_link(
        self,
        li: QtWidgets.QListWidgetItem,
        show_details: bool = False,
    ):
        if self._resolving_link:
            return
        item = self._item_for_list_entry(li)
        if not item:
            return
        self._resolving_link = True
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.progress.setFormat("Generiram tokenizirani link...")
        worker = ResolveItemLinkWorker(self.client, self.category, item)

        def failed(error: str):
            self._resolving_link = False
            self.progress.setRange(0, 100)
            self.progress.setVisible(False)
            QtWidgets.QMessageBox.critical(
                self,
                "Generiranje linka",
                f"Link nije moguće generirati:\n{error}",
            )

        def finished(payload: object):
            self._resolving_link = False
            self.progress.setRange(0, 100)
            self.progress.setVisible(False)
            if isinstance(payload, dict):
                if show_details:
                    self._show_generated_link(payload)
                else:
                    generated_url = str(payload.get("url") or "").strip()
                    if generated_url:
                        self._play_generated_stream(generated_url)
                    else:
                        QtWidgets.QMessageBox.warning(
                            self,
                            "Pokretanje streama",
                            "Portal nije vratio valjan stream URL.",
                        )

        worker.signals.error.connect(failed)
        worker.signals.finished.connect(finished)
        self.thread_pool.start(worker)

    def _item_context_menu(self, position):
        li = self.listw.itemAt(position)
        if not li:
            return
        menu = QtWidgets.QMenu(self)
        play = menu.addAction("Pokreni u VLC playeru")
        details = menu.addAction("Prikaži tokenizirani link i detalje")
        copy_name = menu.addAction("Kopiraj naziv")
        menu.addSeparator()
        toggle = menu.addAction(
            "Odznači program"
            if li.checkState() == QtCore.Qt.Checked
            else "Označi program"
        )
        chosen = menu.exec(self.listw.viewport().mapToGlobal(position))
        if chosen == play:
            self._generate_item_link(li)
        elif chosen == details:
            self._generate_item_link(li, show_details=True)
        elif chosen == copy_name:
            QtWidgets.QApplication.clipboard().setText(li.text())
        elif chosen == toggle:
            li.setCheckState(
                QtCore.Qt.Unchecked
                if li.checkState() == QtCore.Qt.Checked
                else QtCore.Qt.Checked
            )

    def _show_generated_link(self, payload: Dict[str, Any]):
        generated_url = str(payload.get("url") or "")
        details = str(payload.get("details") or generated_url)
        item = payload.get("item")
        title = item.name if isinstance(item, Item) else "Odabrani kanal"

        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(f"Tokenizirani link – {title}")
        dlg.resize(920, 720)
        root = QtWidgets.QVBoxLayout(dlg)
        info = QtWidgets.QLabel(
            "Link je generiran kroz portal create_link. Prikazani su M3U zapis "
            "i svi dostupni metapodaci."
        )
        info.setWordWrap(True)
        root.addWidget(info)

        output = QtWidgets.QTextEdit()
        output.setReadOnly(True)
        output.setPlainText(details)
        root.addWidget(output, 1)

        actions = QtWidgets.QHBoxLayout()
        copy_link = QtWidgets.QPushButton("Kopiraj link")
        copy_all = QtWidgets.QPushButton("Kopiraj sve")
        play = QtWidgets.QPushButton("Pokreni stream")
        close = QtWidgets.QPushButton("Zatvori")
        copy_link.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(generated_url)
        )
        copy_all.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(details)
        )
        play.clicked.connect(lambda: self._play_generated_stream(generated_url))
        close.clicked.connect(dlg.accept)
        actions.addWidget(copy_link)
        actions.addWidget(copy_all)
        actions.addWidget(play)
        actions.addStretch()
        actions.addWidget(close)
        root.addLayout(actions)
        dlg.exec()

    def _play_generated_stream(self, generated_url: str):
        owner = self.parent()
        while owner is not None:
            callback = getattr(owner, "play_stream_callback", None)
            if callable(callback):
                callback(generated_url)
                return
            owner = owner.parent()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(generated_url))

    def _set_all_visible(self, selected: bool):
        # Select/deselect ONLY the items currently visible (i.e. after filtering)
        try:
            self.listw.blockSignals(True)
            for i in range(self.listw.count()):
                li = self.listw.item(i)
                if not li:
                    continue
                uid = li.data(QtCore.Qt.UserRole)
                if not uid:
                    continue
                key = (self.category.category_type, self.category.category_id, str(uid))
                li.setCheckState(QtCore.Qt.Checked if selected else QtCore.Qt.Unchecked)
                if selected:
                    self.selection_store.pop(key, None)
                else:
                    self.selection_store[key] = False
        finally:
            self.listw.blockSignals(False)
            self._update_count_label()

# -----------------------------
# Main window
# -----------------------------

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPTV List Generator 3.0")
        self.resize(1180, 820)

        self.thread_pool = QtCore.QThreadPool.globalInstance()
        self.client: Optional[BasePortalClient] = None
        self._categories_worker: Optional[FetchCategoriesWorker] = None
        self.items_cache: Dict[Tuple[str, str], List[Item]] = {}
        # Per-program selection state (checked/unchecked u prozoru grupe)
        # Key: (category_type, category_id, item_uid)
        self.item_selection_state: Dict[Tuple[str, str, str], bool] = {}

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        # Connection card
        conn_card = QtWidgets.QFrame()
        conn_card.setObjectName("Card")
        conn_l = QtWidgets.QGridLayout(conn_card)
        conn_l.setHorizontalSpacing(10)
        conn_l.setVerticalSpacing(8)

        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText("http://host:port  (može i /c)")

        self.mac_edit = QtWidgets.QLineEdit()
        self.mac_edit.setPlaceholderText("00:1A:79:AA:BB:CC")

        self.adult_pin_edit = QtWidgets.QLineEdit()
        self.adult_pin_edit.setPlaceholderText("0000")
        self.adult_pin_edit.setMaxLength(8)

        self.threads_spin = QtWidgets.QSpinBox()
        self.threads_spin.setRange(2, 64)
        self.threads_spin.setValue(6)
        self.auto_threads_chk = QtWidgets.QCheckBox("Auto Threads")
        self.auto_threads_chk.setToolTip(
            "Automatski prilagođava broj threadova (paralelnih zahtjeva) po portalu.\n"
            "Kad portal vraća prazno/timeout, aplikacija spušta threads (stabilnije i često brže)."
        )
        self.auto_threads_chk.setChecked(True)

        self.counts_chk = QtWidgets.QCheckBox("Prikaži broj stavki (sporo)")
        self.counts_chk.setToolTip("Ako je uključeno, aplikacija će nakon učitavanja grupa brojati sadržaj po grupama.\n"
                                   "To može biti vrlo sporo na velikim listama i nekim portalima.")
        self.counts_chk.setChecked(False)

        self.auto_adult_pins_chk = QtWidgets.QCheckBox("Auto adult PIN")
        self.auto_adult_pins_chk.setToolTip(
            "Ako je uključeno, program će probati više čestih adult PIN-ova.\n"
            "To može usporiti učitavanje grupa. Za brzinu ostavi isključeno i koristi Adult PIN polje."
        )
        self.auto_adult_pins_chk.setChecked(False)

        self.btn_connect = QtWidgets.QPushButton("Poveži i povuci grupe")
        self.btn_connect.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.btn_test = QtWidgets.QPushButton("Test portala")
        self.btn_test.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.btn_cancel_load = QtWidgets.QPushButton("Prekini")
        self.btn_cancel_load.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        self.btn_cancel_load.setEnabled(False)

        self.btn_clear_all = QtWidgets.QPushButton("Obriši sve")
        self.btn_clear_all.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.btn_info = QtWidgets.QToolButton()
        self.btn_info.setText("ℹ Info")
        self.btn_info.setToolTip("Upute i informacije")
        self.btn_info.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.btn_donate = QtWidgets.QToolButton()
        self.btn_donate.setText("❤️ Donate")
        self.btn_donate.setToolTip("Podrži razvoj (PayPal)")
        self.btn_donate.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))

        self.expiry_lbl = QtWidgets.QLabel("Valjanost liste: —")
        self.expiry_lbl.setStyleSheet("color: #a9b4c3;")

        self.groups_progress = QtWidgets.QProgressBar()
        self.groups_progress.setRange(0, 100)
        self.groups_progress.setValue(0)
        self.groups_progress.setFormat("Učitavanje grupa: %p%")
        self.groups_progress.setVisible(False)

        self.lbl_portal_url = QtWidgets.QLabel("Portal URL")
        self.lbl_mac = QtWidgets.QLabel("MAC")
        self.lbl_threads = QtWidgets.QLabel("Threads")
        self.lbl_adult_pin = QtWidgets.QLabel("Adult PIN")
        self.lbl_language = QtWidgets.QLabel("Jezik")
        self.lang_combo = QtWidgets.QComboBox()
        self.lang_combo.addItem("Hrvatski", "hr")
        self.lang_combo.addItem("English", "en")

        conn_l.addWidget(self.lbl_portal_url, 0, 0)
        conn_l.addWidget(self.url_edit, 0, 1, 1, 3)

        conn_l.addWidget(self.lbl_mac, 1, 0)
        conn_l.addWidget(self.mac_edit, 1, 1)
        conn_l.addWidget(self.lbl_threads, 1, 2)
        conn_l.addWidget(self.threads_spin, 1, 3)
        conn_l.addWidget(self.lbl_adult_pin, 2, 0)
        conn_l.addWidget(self.adult_pin_edit, 2, 1)
        conn_l.addWidget(self.auto_threads_chk, 2, 2, 1, 2)
        conn_l.addWidget(self.counts_chk, 2, 4, 1, 3)
        conn_l.addWidget(self.auto_adult_pins_chk, 3, 0, 1, 2)

        conn_l.addWidget(self.btn_connect, 0, 4, 2, 1)
        conn_l.addWidget(self.btn_test, 0, 5, 1, 1)
        conn_l.addWidget(self.btn_cancel_load, 1, 5, 1, 1)
        conn_l.addWidget(self.btn_clear_all, 0, 6, 2, 1)
        conn_l.addWidget(self.expiry_lbl, 4, 0, 1, 7)
        conn_l.addWidget(self.groups_progress, 5, 0, 1, 7)

        root.addWidget(conn_card)
        # Status bar (donate/info) – ne smeta u formi
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        sb.addPermanentWidget(self.lbl_language)
        sb.addPermanentWidget(self.lang_combo)
        sb.addPermanentWidget(self.btn_info)
        sb.addPermanentWidget(self.btn_donate)


        # Tabs
        self.tabs = QtWidgets.QTabWidget()
        self.tab_live = GroupsTab("Live")
        self.tab_vod = GroupsTab("VOD")
        self.tab_tv = GroupsTab("TV Shows")
        self.tabs.addTab(self.tab_live, "Live")
        self.tabs.addTab(self.tab_vod, "VOD")
        self.tabs.addTab(self.tab_tv, "TV Shows")
        root.addWidget(self.tabs, 1)

        # NE učitavaj programe kad označim grupu (checkbox).
        # Programe učitavamo TEK kad dvokliknem na grupu.
        self.tab_live.category_double_clicked.connect(lambda c: self.open_group_contents(c))
        self.tab_vod.category_double_clicked.connect(lambda c: self.open_group_contents(c))
        self.tab_tv.category_double_clicked.connect(lambda c: self.open_group_contents(c))

        # Export bar
        bottom_actions = QtWidgets.QHBoxLayout()
        bottom_actions.addStretch(1)

        self.fast_export_chk = QtWidgets.QCheckBox("Brzi export (samo Live TV kanali)")
        self.fast_export_chk.setToolTip(
            "Brzi export radi samo za Live TV (IPTV) grupe.\n"
            "Za VOD i TV Shows automatski se koristi normalni export (resolve linkova)."

        )
        bottom_actions.addWidget(self.fast_export_chk)

        self.check_links_chk = QtWidgets.QCheckBox("Provjeri linkove nakon exporta")
        self.check_links_chk.setToolTip("Opcionalno i sporije: nakon exporta kratko provjeri rade li generirani linkovi.")
        bottom_actions.addWidget(self.check_links_chk)

        self.btn_export = QtWidgets.QPushButton("Export M3U")
        self.btn_export.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        bottom_actions.addWidget(self.btn_export)
        root.addLayout(bottom_actions)

        # Log
        log_card = QtWidgets.QFrame()
        log_card.setObjectName("Card")
        log_l = QtWidgets.QVBoxLayout(log_card)
        log_l.setContentsMargins(10, 10, 10, 10)

        log_top = QtWidgets.QHBoxLayout()
        self.lbl_log = QtWidgets.QLabel("Log")
        log_top.addWidget(self.lbl_log)
        log_top.addStretch(1)
        self.btn_clear_log = QtWidgets.QToolButton()
        self.btn_clear_log.setText("✕")
        self.btn_clear_log.setToolTip("Očisti log")
        self.btn_clear_log.setCursor(QtGui.QCursor(QtCore.Qt.PointingHandCursor))
        log_top.addWidget(self.btn_clear_log)
        log_l.addLayout(log_top)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setFixedHeight(170)
        log_l.addWidget(self.log)

        root.addWidget(log_card)

        # Signals
        self.btn_connect.clicked.connect(self.on_connect)
        self.btn_test.clicked.connect(self.on_test_portal)
        self.btn_cancel_load.clicked.connect(self.on_cancel_loading)
        self.btn_clear_all.clicked.connect(self.clear_all)
        self.btn_export.clicked.connect(self.on_export)
        self.btn_clear_log.clicked.connect(self.log.clear)
        self.btn_info.clicked.connect(self.show_info_dialog)
        self.btn_donate.clicked.connect(self.open_donate)
        self.lang_combo.currentIndexChanged.connect(lambda _idx: (self._apply_language(), self._save_settings()))

        self._apply_dark_theme()
        self._load_settings()
        self._apply_language()

    def _apply_dark_theme(self):
        self.setStyleSheet(
            """
            QMainWindow { background: #12161d; }
            QLabel { color: #d8dee9; }

            QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {
                background: #1a2130;
                color: #e5e9f0;
                border: 1px solid #2a3650;
                border-radius: 10px;
                padding: 8px;
                selection-background-color: #3b82f6;
            }
            QPlainTextEdit { padding: 10px; }

            QPushButton {
                background: #24314a;
                color: #e5e9f0;
                border: 1px solid #2a3650;
                border-radius: 12px;
                padding: 10px 12px;
            }
            QPushButton:hover { background: #2a3a58; }
            QPushButton:pressed { background: #1f2a40; }

            QToolButton {
                background: transparent;
                color: #a9b4c3;
                border: 1px solid #2a3650;
                border-radius: 10px;
                padding: 6px 10px;
            }
            QToolButton:hover { color: #e5e9f0; }

            QFrame#Card {
                background: #161c26;
                border: 1px solid #232d42;
                border-radius: 16px;
            }

            QTableView#MainTable {
                background: #161c26;
                alternate-background-color: #141a23;
                color: #e5e9f0;
                gridline-color: #22304a;
                border: 1px solid #232d42;
                border-radius: 16px;
                padding: 6px;
            }

            QHeaderView::section {
                background: #141a23;
                color: #c9d1df;
                border: 0px;
                border-bottom: 1px solid #232d42;
                padding: 8px;
            }

            QTableView::item:selected { background: #2b4b7d; }
            """
        )

    def append_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{ts}] {msg}")
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _settings(self) -> QtCore.QSettings:
        return QtCore.QSettings("danijel", "IPTVListGenerator2")

    def _lang(self) -> str:
        return str(self.lang_combo.currentData() or "hr")

    def _tr(self, key: str) -> str:
        texts = {
            "hr": {
                "portal_url": "Portal URL",
                "mac": "MAC",
                "threads": "Threads",
                "adult_pin": "Adult PIN",
                "language": "Jezik",
                "url_placeholder": "http://host:port  (može i /c)",
                "auto_threads": "Auto Threads",
                "counts": "Prikaži broj stavki (sporo)",
                "auto_adult": "Auto adult PIN",
                "connect": "Poveži i povuci grupe",
                "test": "Test portala",
                "cancel": "Prekini",
                "clear": "Obriši sve",
                "expiry": "Valjanost liste: —",
                "progress_groups": "Učitavanje grupa: %p%",
                "fast_export": "Brzi export (samo Live TV kanali)",
                "check_links": "Provjeri linkove nakon exporta",
                "export": "Export M3U",
                "log": "Log",
                "clear_log": "Očisti log",
                "info_tip": "Upute i informacije",
                "donate_tip": "Podrži razvoj (PayPal)",
                "filter": "Live filter...",
                "check_all": "Označi sve",
                "check_visible": "Označi vidljive",
                "uncheck_all": "Makni sve",
                "tab_live": "Live",
                "tab_vod": "VOD",
                "tab_series": "TV Shows",
            },
            "en": {
                "portal_url": "Portal URL",
                "mac": "MAC",
                "threads": "Threads",
                "adult_pin": "Adult PIN",
                "language": "Language",
                "url_placeholder": "http://host:port  (/c allowed)",
                "auto_threads": "Auto Threads",
                "counts": "Show item count (slow)",
                "auto_adult": "Auto adult PIN",
                "connect": "Connect and load groups",
                "test": "Portal Test",
                "cancel": "Cancel",
                "clear": "Clear all",
                "expiry": "List validity: —",
                "progress_groups": "Loading groups: %p%",
                "fast_export": "Fast export (Live TV only)",
                "check_links": "Check links after export",
                "export": "Export M3U",
                "log": "Log",
                "clear_log": "Clear log",
                "info_tip": "Instructions and information",
                "donate_tip": "Support development (PayPal)",
                "filter": "Live filter...",
                "check_all": "Select all",
                "check_visible": "Select visible",
                "uncheck_all": "Unselect all",
                "tab_live": "Live",
                "tab_vod": "VOD",
                "tab_series": "TV Shows",
            },
        }
        lang = self._lang()
        return texts.get(lang, texts["hr"]).get(key, texts["hr"].get(key, key))

    def _apply_language(self):
        self.lbl_portal_url.setText(self._tr("portal_url"))
        self.lbl_mac.setText(self._tr("mac"))
        self.lbl_threads.setText(self._tr("threads"))
        self.lbl_adult_pin.setText(self._tr("adult_pin"))
        self.lbl_language.setText(self._tr("language"))
        self.url_edit.setPlaceholderText(self._tr("url_placeholder"))
        self.auto_threads_chk.setText(self._tr("auto_threads"))
        self.counts_chk.setText(self._tr("counts"))
        self.auto_adult_pins_chk.setText(self._tr("auto_adult"))
        self.btn_connect.setText(self._tr("connect"))
        self.btn_test.setText(self._tr("test"))
        self.btn_cancel_load.setText(self._tr("cancel"))
        self.btn_clear_all.setText(self._tr("clear"))
        if self.expiry_lbl.text().endswith("—"):
            self.expiry_lbl.setText(self._tr("expiry"))
        self.groups_progress.setFormat(self._tr("progress_groups"))
        self.fast_export_chk.setText(self._tr("fast_export"))
        self.check_links_chk.setText(self._tr("check_links"))
        self.btn_export.setText(self._tr("export"))
        self.lbl_log.setText(self._tr("log"))
        self.btn_clear_log.setToolTip(self._tr("clear_log"))
        self.btn_info.setToolTip(self._tr("info_tip"))
        self.btn_donate.setToolTip(self._tr("donate_tip"))
        for tab in (self.tab_live, self.tab_vod, self.tab_tv):
            tab.filter_edit.setPlaceholderText(self._tr("filter"))
            tab.btn_check_all.setText(self._tr("check_all"))
            tab.btn_check_visible.setText(self._tr("check_visible"))
            tab.btn_uncheck_all.setText(self._tr("uncheck_all"))
        self.tabs.setTabText(0, self._tr("tab_live"))
        self.tabs.setTabText(1, self._tr("tab_vod"))
        self.tabs.setTabText(2, self._tr("tab_series"))

    def _load_settings(self):
        s = self._settings()
        self.url_edit.setText(str(s.value("portal_url", "") or ""))
        self.mac_edit.setText(str(s.value("mac", "") or ""))
        self.adult_pin_edit.setText(str(s.value("adult_pin", COMMON_ADULT_PINS[0]) or COMMON_ADULT_PINS[0]))
        self.threads_spin.setValue(int(s.value("threads", 6) or 6))
        self.auto_threads_chk.setChecked(str(s.value("auto_threads", "true")).lower() in ("1", "true", "yes"))
        self.counts_chk.setChecked(str(s.value("show_counts", "false")).lower() in ("1", "true", "yes"))
        self.auto_adult_pins_chk.setChecked(str(s.value("auto_adult_pins", "false")).lower() in ("1", "true", "yes"))
        self.fast_export_chk.setChecked(str(s.value("fast_export", "false")).lower() in ("1", "true", "yes"))
        self.check_links_chk.setChecked(str(s.value("check_links", "false")).lower() in ("1", "true", "yes"))
        lang = str(s.value("language", "hr") or "hr")
        idx = self.lang_combo.findData(lang)
        self.lang_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _save_settings(self):
        s = self._settings()
        s.setValue("portal_url", self.url_edit.text().strip())
        s.setValue("mac", self.mac_edit.text().strip())
        s.setValue("adult_pin", self.adult_pin_edit.text().strip() or COMMON_ADULT_PINS[0])
        s.setValue("threads", int(self.threads_spin.value()))
        s.setValue("auto_threads", self.auto_threads_chk.isChecked())
        s.setValue("show_counts", self.counts_chk.isChecked())
        s.setValue("auto_adult_pins", self.auto_adult_pins_chk.isChecked())
        s.setValue("fast_export", self.fast_export_chk.isChecked())
        s.setValue("check_links", self.check_links_chk.isChecked())
        s.setValue("language", self._lang())

    def closeEvent(self, event: QtGui.QCloseEvent):
        self._save_settings()
        super().closeEvent(event)

    def _effective_threads(self) -> int:
        """Vrati stvarni broj threadova koji ćemo koristiti.
        Auto Threads: spušta vrijednost kad portal pokazuje throttling (prazno/timeout).
        """
        base = int(self.threads_spin.value())
        # cap je praktičan (portali često ne vole > 10)
        base = max(2, min(base, 32))
        if not getattr(self, "auto_threads_chk", None) or not self.auto_threads_chk.isChecked():
            return base
        fails = int(getattr(self, "_adaptive_failures", 0))
        # svaka greška/prazno spušta za 2, ali ne ispod 2
        eff = max(2, base - (fails * 2))
        # dodatno: ne idi preko 10 u auto modu
        eff = min(eff, 10)
        return eff

    def _note_portal_result(self, ok: bool, empty: bool = False):
        """Ažurira adaptivni signal za auto threads."""
        cur = int(getattr(self, "_adaptive_failures", 0))
        if ok and not empty:
            # polako oporavljaj (decay)
            self._adaptive_failures = max(0, cur - 1)
            return
        # greška ili prazno
        self._adaptive_failures = min(6, cur + 1)


    def _on_groups_progress(self, pct: int, msg: str = ""):
        try:
            self.groups_progress.setVisible(True)
            self.groups_progress.setValue(int(pct))
            if msg:
                self.groups_progress.setFormat(f"Učitavanje grupa: {int(pct)}% — {msg}")
            else:
                self.groups_progress.setFormat(f"Učitavanje grupa: {int(pct)}%")
            if int(pct) >= 100:
                # kratko ostavi pa sakrij (da se vidi 100%)
                QtCore.QTimer.singleShot(400, lambda: self.groups_progress.setVisible(False))
        except Exception:
            pass

    # NOTE:
    # Ranije je aplikacija radila "prefetch" programa na klik na grupu.
    # To je stvaralo problem: kad korisnik samo označi grupu (checkbox),
    # aplikacija bi odmah povlačila programe.
    # Sada programe učitavamo isključivo na DVOKLIK (otvaranje prozora grupe).

    def open_donate(self):
        try:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://www.paypal.com/paypalme/danijel0304"))
        except Exception:
            pass

    def show_info_dialog(self):
        if self._lang() == "en":
            html = (
                "<h2>IPTV List Generator 3.0</h2>"
                "<p><b>What it does:</b> connects to a Stalker/Portal URL with a MAC address and creates an M3U playlist for <b>Live</b>, <b>VOD</b>, and <b>TV Shows</b>.</p>"
                "<h3>Quick Start</h3>"
                "<ol>"
                "<li>Enter <b>Portal URL</b> and <b>MAC</b>. The URL may include <code>/c</code>.</li>"
                "<li>Use <b>Portal Test</b> to quickly check whether the URL/MAC responds before loading everything.</li>"
                "<li>Click <b>Connect and load groups</b>. Wait while the program works in the background; avoid clicking other actions until loading finishes.</li>"
                "<li>Tick the groups you want to export. Double-click a group to load its channels/movies/episodes and adjust per-item selection. Double-click an item to generate its tokenized play URL, M3U entry, and metadata.</li>"
                "<li>Click <b>Export M3U</b>. The log shows how many Live, VOD, and TV Shows links were exported.</li>"
                "</ol>"
                "<h3>Selection</h3>"
                "<ul>"
                "<li><b>Select all</b> selects all groups in the current tab.</li>"
                "<li><b>Select visible</b> selects only the currently filtered groups, useful for terms like country names or adult group names.</li>"
                "<li><b>Unselect all</b> clears the current tab selection.</li>"
                "</ul>"
                "<h3>Adult PIN</h3>"
                "<p><b>Adult PIN</b> is sent as the portal parental password. The usual default is <code>0000</code>.</p>"
                "<p><b>Auto adult PIN</b> tries several common PINs. It can make loading slower, so keep it off unless adult categories are missing.</p>"
                "<h3>Speed Options</h3>"
                "<ul>"
                "<li><b>Auto Threads</b> adjusts parallel requests when a portal starts timing out or returning empty results.</li>"
                "<li><b>Show item count</b> can be slow because every group must be queried.</li>"
                "<li><b>Fast export</b> applies only to Live TV. VOD and TV Shows usually need resolved play links.</li>"
                "<li><b>Check links after export</b> is optional and can be slow because generated URLs are tested.</li>"
                "</ul>"
                "<h3>List Validity</h3>"
                "<p>The program tries to read expiry from profile fields and then from sample stream URLs. Some portals do not expose this information.</p>"
                "<h3>CachyOS/KDE Notes</h3>"
                "<p>The app avoids KDE native file dialogs and KDE/Breeze Qt theme integration because they can trigger KIO crashes on some systems.</p>"
                "<h3>Donate</h3>"
                "<p>Support development: <a href='https://www.paypal.com/paypalme/danijel0304'>PayPal</a></p>"
            )
        else:
            html = (
                "<h2>IPTV List Generator 3.0</h2>"
                "<p><b>Što radi:</b> spaja se na Stalker/Portal URL uz MAC i generira M3U listu za <b>Live</b>, <b>VOD</b> i <b>TV Shows</b>.</p>"
                "<h3>Brzi početak</h3>"
                "<ol>"
                "<li>Upiši <b>Portal URL</b> i <b>MAC</b>. URL može sadržavati <code>/c</code>.</li>"
                "<li>Koristi <b>Test portala</b> za brzu provjeru radi li URL/MAC prije punog učitavanja.</li>"
                "<li>Klikni <b>Poveži i povuci grupe</b>. Pričekaj dok program radi u pozadini i nemoj klikati druge akcije dok učitavanje ne završi.</li>"
                "<li>Označi grupe koje želiš exportati. Dvoklik na grupu učitava kanale/filmove/epizode i omogućuje izbor pojedinačnih stavki. Dvoklik na stavku generira tokenizirani play URL, M3U zapis i metapodatke.</li>"
                "<li>Klikni <b>Export M3U</b>. Log prikazuje koliko je Live, VOD i TV Shows linkova exportano.</li>"
                "</ol>"
                "<h3>Označavanje</h3>"
                "<ul>"
                "<li><b>Označi sve</b> označava sve grupe u trenutnom tabu.</li>"
                "<li><b>Označi vidljive</b> označava samo trenutno filtrirane grupe, korisno za države ili adult nazive grupa.</li>"
                "<li><b>Makni sve</b> briše odabir u trenutnom tabu.</li>"
                "</ul>"
                "<h3>Adult PIN</h3>"
                "<p><b>Adult PIN</b> šalje se portalu kao parental password. Uobičajeni default je <code>0000</code>.</p>"
                "<p><b>Auto adult PIN</b> proba više čestih PIN-ova. Može usporiti učitavanje, zato ga uključi samo ako adult kategorije nedostaju.</p>"
                "<h3>Opcije za brzinu</h3>"
                "<ul>"
                "<li><b>Auto Threads</b> prilagođava paralelne zahtjeve kad portal timeouta ili vraća prazno.</li>"
                "<li><b>Prikaži broj stavki</b> može biti sporo jer se svaka grupa dodatno provjerava.</li>"
                "<li><b>Brzi export</b> vrijedi samo za Live TV. VOD i TV Shows obično moraju razriješiti play link.</li>"
                "<li><b>Provjeri linkove nakon exporta</b> je opcionalno i može biti sporo jer testira generirane URL-ove.</li>"
                "</ul>"
                "<h3>Valjanost liste</h3>"
                "<p>Program pokušava pročitati datum isteka iz profila, a zatim iz uzorka stream linkova. Neki portali tu informaciju ne šalju.</p>"
                "<h3>CachyOS/KDE napomena</h3>"
                "<p>Program zaobilazi KDE native file dialoge i KDE/Breeze Qt integraciju jer na nekim sustavima mogu izazvati KIO rušenja.</p>"
                "<h3>Donate</h3>"
                "<p>Podrška razvoju: <a href='https://www.paypal.com/paypalme/danijel0304'>PayPal</a></p>"
            )
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Info")
        dlg.resize(720, 520)
        lay = QtWidgets.QVBoxLayout(dlg)

        browser = QtWidgets.QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(html)
        lay.addWidget(browser, 1)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def open_group_contents(self, category: Category):
        """Otvori dialog s popisom sadržaja odabrane grupe (dvoklik)."""
        if not self.client:
            QtWidgets.QMessageBox.information(self, 'Info', 'Prvo povuci kategorije (Poveži).')
            return

        dlg = GroupContentsDialog(
            self,
            category,
            self.item_selection_state,
            self.client,
            self.thread_pool,
        )
        dlg.set_loading('Učitavam sadržaj grupe...')
        dlg.show()

        # Ako je već u cacheu, prikaži odmah
        key = (category.category_type, category.category_id)
        cached = self.items_cache.get(key)
        if cached is not None and len(cached) > 0:
            dlg.set_items(cached)

        def start_fetch():
            # Retry / initial fetch: očisti cache za ovu grupu i ponovno povuci
            self.items_cache.pop(key, None)
            dlg.set_loading('Učitavam sadržaj grupe...')

            worker = FetchItemsForDialogWorker(self.client, category, num_threads=self._effective_threads())
            worker.signals.progress.connect(lambda pct, msg: dlg.set_progress(pct, msg))
            worker.signals.error.connect(lambda e: (self._note_portal_result(ok=False), self.items_cache.pop(key, None), self.append_log(f"Sadržaj grupe greška: {e}"), dlg.set_loading(f"Greška: {e}")))

            def _done(items):
                try:
                    if isinstance(items, list):
                        # Auto Threads feedback
                        self._note_portal_result(ok=True, empty=(len(items) == 0))
                        if len(items) > 0:
                            self.items_cache[key] = items
                    else:
                        self._note_portal_result(ok=False)
                    dlg.set_items(items if isinstance(items, list) else [])
                except Exception as e:
                    self.append_log(f"Dialog greška: {e}")

            worker.signals.finished.connect(_done)
            self.thread_pool.start(worker)

        # Retry gumb u dialogu
        try:
            dlg.retry_requested.connect(start_fetch)
        except Exception:
            pass

        # Ako nije bilo cachea, pokreni fetch odmah
        if cached is None or len(cached) == 0:
            start_fetch()


    def clear_all(self):
        # Manual clear + used automatically on connect
        self.tab_live.model.add_categories([])
        self.tab_vod.model.add_categories([])
        self.tab_tv.model.add_categories([])

        self.tab_live.filter_edit.clear()
        self.tab_vod.filter_edit.clear()
        self.tab_tv.filter_edit.clear()

        self.items_cache.clear()
        self.item_selection_state.clear()
        self.expiry_lbl.setText("Valjanost liste: —")
        self.append_log("Obrisano: grupe, cache, selekcija programa i status.")

    def on_test_portal(self):
        base_url = self.url_edit.text().strip()
        mac = self.mac_edit.text().strip()
        adult_pin = self.adult_pin_edit.text().strip() or COMMON_ADULT_PINS[0]
        if not base_url or not mac:
            QtWidgets.QMessageBox.warning(self, "Greška", "Unesi Portal URL i MAC.")
            return
        self._save_settings()
        self.btn_test.setEnabled(False)
        self.append_log("Testiram portal...")
        worker = TestPortalWorker(base_url, mac, adult_pin)
        worker.signals.progress.connect(self._on_groups_progress)
        worker.signals.error.connect(lambda e: (self.btn_test.setEnabled(True), self.append_log(f"Test greška: {e}"), QtWidgets.QMessageBox.critical(self, "Test", e)))

        def _done(payload):
            self.btn_test.setEnabled(True)
            self.groups_progress.setVisible(False)
            if isinstance(payload, dict) and payload.get("ok"):
                msg = f"Portal radi.\nTip: {payload.get('client')}\nBase URL: {payload.get('base_url')}\nLive grupa: {payload.get('live_categories')}"
                self.append_log(f"Test OK: {payload.get('client')} / Live grupa: {payload.get('live_categories')}")
                QtWidgets.QMessageBox.information(self, "Test", msg)

        worker.signals.finished.connect(_done)
        self.thread_pool.start(worker)

    def on_cancel_loading(self):
        if self._categories_worker:
            self._categories_worker.cancel()
            self.append_log("Prekid učitavanja zatražen. Čekam da trenutni mrežni zahtjev završi.")
            self.btn_cancel_load.setEnabled(False)

    def on_connect(self):
        base_url = self.url_edit.text().strip()
        mac = self.mac_edit.text().strip()
        adult_pin = self.adult_pin_edit.text().strip() or COMMON_ADULT_PINS[0]
        if not base_url or not mac:
            QtWidgets.QMessageBox.warning(self, "Greška", "Unesi Portal URL i MAC.")
            return
        self._save_settings()

        # Auto-clear when connecting with new URL/MAC
        self.clear_all()
        self._adaptive_failures = 0

        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
            self.client = None

        self.expiry_lbl.setText("Valjanost liste: (detektiram...)")
        self.append_log("Spajam se i povlačim kategorije...")
        self.append_log("Pričekaj dok program radi u pozadini. Nemoj ponovno klikati gumbe dok učitavanje ne završi.")
        self.btn_connect.setEnabled(False)
        self.btn_cancel_load.setEnabled(True)

        worker = ConnectCategoriesWorker(
            base_url,
            mac,
            adult_pin,
            auto_adult_pins=self.auto_adult_pins_chk.isChecked(),
        )
        self._categories_worker = worker
        worker.signals.log.connect(self.append_log)
        worker.signals.progress.connect(self._on_groups_progress)
        worker.signals.error.connect(self._on_worker_error)
        worker.signals.finished.connect(self._on_connect_loaded)
        self.groups_progress.setVisible(True)
        self.groups_progress.setValue(0)
        self.groups_progress.setFormat("Molim pričekaj, program radi u pozadini...")
        self.thread_pool.start(worker)

    def _on_worker_error(self, err: str):
        msg = format_portal_error(err)
        self.append_log(f"Greška: {msg}")
        self.btn_connect.setEnabled(True)
        self.btn_cancel_load.setEnabled(False)
        self._categories_worker = None
        QtWidgets.QMessageBox.critical(self, "Greška", msg)

    def _on_connect_loaded(self, payload: object):
        if not isinstance(payload, dict):
            payload = {}
        self.btn_connect.setEnabled(True)
        self.btn_cancel_load.setEnabled(False)
        self._categories_worker = None

        if payload.get("canceled"):
            self.append_log("Učitavanje prekinuto.")
            self.groups_progress.setVisible(False)
            return

        client = payload.get("client")
        if isinstance(client, BasePortalClient):
            self.client = client
        categories = payload.get("categories", [])
        self._on_categories_loaded(categories if isinstance(categories, list) else [])

    def _on_categories_loaded(self, categories: List[Category]):
        self.btn_connect.setEnabled(True)
        self.btn_cancel_load.setEnabled(False)
        self._categories_worker = None

        live = [c for c in categories if c.category_type == "IPTV"]
        vod = [c for c in categories if c.category_type == "VOD"]
        tv = [c for c in categories if c.category_type == "Series"]

        self.tab_live.model.add_categories(live)
        self.tab_vod.model.add_categories(vod)
        self.tab_tv.model.add_categories(tv)

        self.append_log(f"Učitano kategorija: {len(categories)} (Live={len(live)}, VOD={len(vod)}, TV={len(tv)})")

        if not self.client:
            return
        # Brojanje stavki po grupama je opcionalno jer može biti sporo
        if self.counts_chk.isChecked():
            cnt_worker = FetchCountsWorker(self.client, list(categories), num_threads=self._effective_threads())
            cnt_worker.signals.error.connect(lambda e: self.append_log(f"Count greška: {e}"))
            cnt_worker.signals.progress.connect(self._on_groups_progress)
            cnt_worker.signals.finished.connect(self._on_counts_loaded)
            # pokaži progress i za brojanje (da se vidi da aplikacija radi)
            self.groups_progress.setVisible(True)
            self.groups_progress.setValue(0)
            self.groups_progress.setFormat("Brojim sadržaj: %p%")
            self.thread_pool.start(cnt_worker)
        else:
            self.append_log("Preskačem brojanje stavki (isključeno).")
            try:
                self.groups_progress.setValue(100)
                self.groups_progress.setFormat("Učitavanje grupa završeno")
                QtCore.QTimer.singleShot(500, lambda: self.groups_progress.setVisible(False))
            except Exception:
                pass

        samples: List[Category] = []
        for arr in (live[:3], vod[:3], tv[:3]):
            samples.extend(arr)
        exp_worker = AccountExpiryWorker(self.client)
        exp_worker.signals.error.connect(lambda e: (self.append_log(f"Expiry profil greška: {e}"), self._start_sample_expiry_detection(samples)))
        exp_worker.signals.finished.connect(lambda payload: self._on_profile_expiry_detected(payload, samples))
        self.thread_pool.start(exp_worker)

    def _on_counts_loaded(self, payload: List[Tuple[str, str, Optional[int]]]):
        for typ, cid, cnt in payload:
            if typ == "IPTV":
                self.tab_live.model.set_count_for_category_id(cid, cnt)
            elif typ == "VOD":
                self.tab_vod.model.set_count_for_category_id(cid, cnt)
            elif typ == "Series":
                self.tab_tv.model.set_count_for_category_id(cid, cnt)

        # sakrij progress nakon što završi brojanje
        try:
            self.groups_progress.setValue(100)
            QtCore.QTimer.singleShot(500, lambda: self.groups_progress.setVisible(False))
        except Exception:
            pass

    def _start_sample_expiry_detection(self, samples: List[Category]):
        if not self.client:
            return
        self.append_log("Valjanost nije pronađena u profilu, provjeravam uzorke linkova...")
        exp_worker = DetectExpiryWorker(self.client, samples, num_threads=self._effective_threads())
        exp_worker.signals.error.connect(lambda e: self.append_log(f"Expiry greška: {e}"))
        exp_worker.signals.progress.connect(lambda pct, msg: self.append_log(msg) if int(pct) in (0, 100) and msg else None)
        exp_worker.signals.finished.connect(self._on_expiry_detected)
        self.thread_pool.start(exp_worker)

    def _on_profile_expiry_detected(self, payload: object, samples: List[Category]):
        dt: Optional[datetime] = None
        src = ""
        if isinstance(payload, tuple):
            dt = payload[0] if isinstance(payload[0], datetime) else None
            src = str(payload[1] or "") if len(payload) > 1 else ""
        elif isinstance(payload, datetime):
            dt = payload
        if dt:
            self.expiry_lbl.setText(f"Valjanost liste: {format_expiry(dt)}")
            suffix = f" ({src})" if src else ""
            self.append_log(f"Valjanost liste (profil{suffix}): {format_expiry(dt)}")
        else:
            self._start_sample_expiry_detection(samples)

    def _on_expiry_detected(self, dt: Optional[datetime]):
        self.expiry_lbl.setText(f"Valjanost liste: {format_expiry(dt)}")
        if dt:
            self.append_log(f"Detektirana valjanost (uzorak): {format_expiry(dt)}")
        else:
            self.append_log("Valjanost liste nije moguće pouzdano iščitati (nema exp parametara u linkovima).")

    def on_export(self):
        if not self.client:
            QtWidgets.QMessageBox.information(self, "Info", "Prvo povuci kategorije (Poveži).")
            return
        self._save_settings()

        checked: List[Category] = []
        checked.extend(self.tab_live.iter_checked_categories())
        checked.extend(self.tab_vod.iter_checked_categories())
        checked.extend(self.tab_tv.iter_checked_categories())

        if not checked:
            QtWidgets.QMessageBox.information(self, "Info", "Nisi odabrao nijednu grupu.")
            return
        adult_checked = [c for c in checked if looks_adult_category(c.name)]
        if adult_checked:
            self.append_log(f"Adult grupe odabrane za export: {len(adult_checked)}")
        else:
            self.append_log("Adult grupe nisu odabrane za export. Ako ih želiš, filtriraj adult grupe i označi ih ručno ili s 'Označi vidljive'.")

        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Spremi M3U",
            default_export_filename(self.url_edit.text().strip()),
            "M3U (*.m3u)",
            options=QtWidgets.QFileDialog.Option.DontUseNativeDialog,
        )
        if not path:
            return

        prog = QtWidgets.QProgressDialog("Exportam...", "Prekini", 0, 100, self)
        prog.setWindowTitle("Export")
        prog.setWindowModality(QtCore.Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setValue(0)

        worker = ExportWorker(
            client=self.client,
            categories=checked,
            path=path,
            num_threads=self._effective_threads(),
            fast_export=self.fast_export_chk.isChecked(),
            cache=self.items_cache,
            item_selection=self.item_selection_state,
            check_links=self.check_links_chk.isChecked(),
        )

        def on_progress(pct: int, msg: str):
            try:
                prog.setValue(int(pct))
                if msg:
                    prog.setLabelText(msg)
            except Exception:
                pass

        def on_done(payload: dict):
            # payload from ExportWorker.finished
            if not isinstance(payload, dict):
                payload = {}
            prog.close()

            if payload.get("canceled"):
                self.append_log("Export prekinut.")
                return

            links = int(payload.get("links", 0) or 0)
            lines = int(payload.get("lines", 0) or 0)
            checked_links = int(payload.get("checked_links", 0) or 0)
            dead_links = int(payload.get("dead_links", 0) or 0)
            links_by_type = payload.get("links_by_type", {}) if isinstance(payload.get("links_by_type"), dict) else {}
            groups_by_type = payload.get("groups_by_type", {}) if isinstance(payload.get("groups_by_type"), dict) else {}
            live_links = int(links_by_type.get("IPTV", 0) or 0)
            vod_links = int(links_by_type.get("VOD", 0) or 0)
            series_links = int(links_by_type.get("Series", 0) or 0)
            live_groups = int(groups_by_type.get("IPTV", 0) or 0)
            vod_groups = int(groups_by_type.get("VOD", 0) or 0)
            series_groups = int(groups_by_type.get("Series", 0) or 0)
            self.append_log(f"M3U spremljen: {path} ({links} linkova / {lines} linija)")
            self.append_log(
                f"Export sažetak: Live={live_links} linkova ({live_groups} grupa), "
                f"VOD={vod_links} linkova ({vod_groups} grupa), "
                f"TV Shows={series_links} linkova ({series_groups} grupa)"
            )
            if checked_links:
                self.append_log(f"Provjera linkova: {checked_links} provjereno, neuspjelo: {dead_links}")

            best = payload.get("best_exp")
            if isinstance(best, datetime):
                self.expiry_lbl.setText(f"Valjanost liste: {format_expiry(best)}")

            extra = f"\nProvjereno linkova: {checked_links}\nNeuspjelo: {dead_links}" if checked_links else ""
            QtWidgets.QMessageBox.information(
                self,
                "OK",
                f"Spremljeno u:\n{path}\n\n"
                f"Ukupno linkova: {links}\n"
                f"Live: {live_links} ({live_groups} grupa)\n"
                f"VOD: {vod_links} ({vod_groups} grupa)\n"
                f"TV Shows: {series_links} ({series_groups} grupa)"
                f"{extra}",
            )

        worker.signals.log.connect(self.append_log)
        worker.signals.progress.connect(on_progress)
        worker.signals.error.connect(self._on_worker_error)
        worker.signals.finished.connect(on_done)

        prog.canceled.connect(worker.cancel)
        self.thread_pool.start(worker)


# -----------------------------
# App entry
# -----------------------------

def main():
    app = QtWidgets.QApplication(sys.argv)
    font = app.font()
    font.setPointSize(10)
    app.setFont(font)

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
