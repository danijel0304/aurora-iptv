from __future__ import annotations

import asyncio
import importlib.util
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
import types
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

from core import normalize_mac, normalize_url, parse_xtream_url


STALKER_STUDIO_FILENAME = "IPTV_List_Generator_3.0_FULL_FIXED_v3_EXPIRY_PATCHED_v14_AUTO_THREADS.py"
_STALKER_STUDIO_MODULE: types.ModuleType | None = None
_BALKAN_SCANNER_MODULE: types.ModuleType | None = None


def _resource_bases() -> list[Path]:
    bases: list[Path] = []
    frozen_base = getattr(sys, "_MEIPASS", "")
    if frozen_base:
        bases.append(Path(frozen_base))
    bases.append(Path(__file__).resolve().parent)
    bases.append(Path.cwd())
    unique: list[Path] = []
    for base in bases:
        resolved = base.expanduser()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _find_resource(*parts: str) -> Path:
    candidates = [base.joinpath(*parts) for base in _resource_bases()]
    for path in candidates:
        if path.is_file():
            return path
    searched = "\n".join(str(path) for path in candidates)
    raise FileNotFoundError(f"Nedostaje datoteka resursa:\n{searched}")


def _load_stalker_studio_module() -> types.ModuleType:
    global _STALKER_STUDIO_MODULE
    if _STALKER_STUDIO_MODULE:
        return _STALKER_STUDIO_MODULE

    source_path = _find_resource("vendor", "stalker_studio", STALKER_STUDIO_FILENAME)
    source = source_path.read_text(encoding="utf-8")
    replacements = {
        "from PySide6 import QtCore, QtGui, QtWidgets": "from PyQt6 import QtCore, QtGui, QtWidgets",
        "QtCore.Signal": "QtCore.pyqtSignal",
        "QtCore.Slot": "QtCore.pyqtSlot",
        "QtCore.Qt.Unchecked": "QtCore.Qt.CheckState.Unchecked",
        "QtCore.Qt.Checked": "QtCore.Qt.CheckState.Checked",
        "QtCore.Qt.AlignRight": "QtCore.Qt.AlignmentFlag.AlignRight",
        "QtCore.Qt.AlignVCenter": "QtCore.Qt.AlignmentFlag.AlignVCenter",
        "QtCore.Qt.CustomContextMenu": "QtCore.Qt.ContextMenuPolicy.CustomContextMenu",
        "QtCore.Qt.ItemIsEnabled": "QtCore.Qt.ItemFlag.ItemIsEnabled",
        "QtCore.Qt.ItemIsUserCheckable": "QtCore.Qt.ItemFlag.ItemIsUserCheckable",
        "QtCore.Qt.NoItemFlags": "QtCore.Qt.ItemFlag.NoItemFlags",
        "QtCore.Qt.PointingHandCursor": "QtCore.Qt.CursorShape.PointingHandCursor",
        "QtCore.Qt.UserRole": "QtCore.Qt.ItemDataRole.UserRole",
        "QtCore.Qt.WindowModal": "QtCore.Qt.WindowModality.WindowModal",
        "QtWidgets.QAbstractItemView.ExtendedSelection": (
            "QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection"
        ),
        "QtWidgets.QAbstractItemView.NoEditTriggers": (
            "QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers"
        ),
        "QtWidgets.QAbstractItemView.SelectRows": (
            "QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows"
        ),
        "QtWidgets.QDialogButtonBox.Close": "QtWidgets.QDialogButtonBox.StandardButton.Close",
        "QtWidgets.QHeaderView.ResizeToContents": "QtWidgets.QHeaderView.ResizeMode.ResizeToContents",
        "QtWidgets.QHeaderView.Stretch": "QtWidgets.QHeaderView.ResizeMode.Stretch",
    }
    for old, new in replacements.items():
        source = source.replace(old, new)

    module = types.ModuleType("aurora_stalker_worker_embedded")
    module.__file__ = str(source_path)
    module.__dict__["__name__"] = module.__name__
    sys.modules[module.__name__] = module
    exec(compile(source, str(source_path), "exec"), module.__dict__)
    _STALKER_STUDIO_MODULE = module
    return module


def _load_balkan_scanner_module() -> types.ModuleType:
    global _BALKAN_SCANNER_MODULE
    if _BALKAN_SCANNER_MODULE:
        return _BALKAN_SCANNER_MODULE

    source_path = _find_resource("vendor", "balkan_iptv", "scanner.py")
    spec = importlib.util.spec_from_file_location("aurora_balkan_scanner_worker", source_path)
    if not spec or not spec.loader:
        raise ImportError(f"Ne mogu učitati Balkan scanner: {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _BALKAN_SCANNER_MODULE = module
    return module


def _merge_balkan_stats(base: dict[str, int], extra: dict[str, int]) -> dict[str, int]:
    for key, value in extra.items():
        base[key] = base.get(key, 0) + int(value or 0)
    return base


def _balkan_stats_summary(stats: dict[str, int]) -> str:
    parts = [f"{key}:{value}" for key, value in stats.items() if value > 0]
    return ", ".join(parts[:5])


def _payload_list(payload: object, keys: tuple[str, ...] = ()) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


def _first_value(mapping: dict, *keys: str) -> object:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return ""


def _category_id(value: object) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _stream_key(stream: dict, content_type: str) -> str:
    if content_type == "VOD":
        return str(_first_value(stream, "stream_id", "movie_id", "id"))
    if content_type == "Serije":
        return str(_first_value(stream, "series_id", "id", "stream_id"))
    return str(_first_value(stream, "stream_id", "id"))


def _redact(text: str, values: tuple[str, ...]) -> str:
    for value in values:
        if value:
            text = text.replace(value, "***")
    return text


async def _json_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    timeout: int | float | None = None,
) -> object:
    response = await client.get(url, params=params, timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: server nije vratio Xtream API odgovor.")
    try:
        return response.json()
    except ValueError as error:
        preview = response.text.strip().replace("\n", " ")[:120]
        preview = _redact(
            preview,
            (
                str(params.get("username", "")),
                str(params.get("password", "")),
            ),
        )
        if preview:
            raise RuntimeError(f"Server nije vratio JSON odgovor: {preview}") from error
        raise RuntimeError("Server je vratio prazan odgovor.") from error


def _expiry(value: object) -> str:
    if value in (None, "", "0", 0):
        return "Bez isteka"
    try:
        return datetime.fromtimestamp(int(str(value))).strftime("%d.%m.%Y.")
    except (TypeError, ValueError, OSError):
        return str(value)


class XtreamScanWorker(QThread):
    result = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    finished_scan = pyqtSignal()

    def __init__(self, urls: list[str], concurrency: int = 8, timeout: int = 12):
        super().__init__()
        self.urls = urls
        self.concurrency = concurrency
        self.timeout = timeout
        self.running = True

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        limits = httpx.Limits(
            max_connections=max(10, self.concurrency * 2),
            max_keepalive_connections=max(5, self.concurrency),
        )
        semaphore = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(verify=False, follow_redirects=True, limits=limits) as client:
            tasks = [self._check(client, semaphore, url) for url in self.urls]
            completed = 0
            for future in asyncio.as_completed(tasks):
                if not self.running:
                    break
                result = await future
                completed += 1
                if result:
                    self.result.emit(result)
                self.progress.emit(completed, len(tasks))
        self.finished_scan.emit()

    async def _check(
        self, client: httpx.AsyncClient, semaphore: asyncio.Semaphore, playlist_url: str
    ) -> dict[str, str] | None:
        parsed = parse_xtream_url(playlist_url)
        if not parsed:
            return None
        server, username, password = parsed
        api_url = f"{server}/player_api.php"
        params = {"username": username, "password": password}
        started = time.monotonic()
        result = {
            "server": server,
            "username": username,
            "password": password,
            "status": "Offline",
            "expiry": "—",
            "connections": "—",
            "content": "—",
            "ping": "—",
            "playlist_url": playlist_url,
        }
        try:
            async with semaphore:
                response = await client.get(api_url, params=params, timeout=self.timeout)
                result["ping"] = f"{int((time.monotonic() - started) * 1000)} ms"
                data = response.json()
                user_info = data.get("user_info", {}) if isinstance(data, dict) else {}
                authenticated = str(data.get("auth", "0")) == "1" or str(
                    user_info.get("status", "")
                ).lower() == "active"
                if not authenticated:
                    return result

                async def count(action: str) -> int:
                    try:
                        response = await client.get(
                            api_url,
                            params={**params, "action": action},
                            timeout=min(self.timeout, 10),
                        )
                        payload = response.json()
                        return len(payload) if isinstance(payload, list) else 0
                    except Exception:
                        return 0

                live, vod, series = await asyncio.gather(
                    count("get_live_streams"),
                    count("get_vod_streams"),
                    count("get_series"),
                )
                result.update(
                    status=str(user_info.get("status") or "Active"),
                    expiry=_expiry(user_info.get("exp_date")),
                    connections=(
                        f"{user_info.get('active_cons', '0')}/"
                        f"{user_info.get('max_connections', '1')}"
                    ),
                    content=f"Live {live} · VOD {vod} · Serije {series}",
                )
        except Exception as error:
            result["status"] = type(error).__name__.replace("Exception", "") or "Greška"
        return result


class MacHttpWorker(QThread):
    result = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    finished_scan = pyqtSignal()

    def __init__(
        self,
        url: str,
        macs: list[str],
        mode: str,
        field: str,
        timeout: int,
        success_text: str,
    ):
        super().__init__()
        self.url = url
        self.macs = macs
        self.mode = mode
        self.field = field
        self.timeout = timeout
        self.success_text = success_text
        self.running = True

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
            for index, mac in enumerate(self.macs, 1):
                if not self.running:
                    break
                started = time.monotonic()
                request_url = self.url
                headers = {"User-Agent": "Aurora-IPTV/1.0", "Accept": "*/*"}
                if self.mode == "Query":
                    parsed = urlsplit(request_url)
                    from urllib.parse import parse_qsl

                    query = [(key, value) for key, value in parse_qsl(parsed.query) if key != self.field]
                    query.append((self.field, mac))
                    request_url = urlunsplit(
                        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
                    )
                elif self.mode == "Header":
                    headers[self.field] = mac
                else:
                    headers["Cookie"] = f"{self.field}={mac}"
                status = "Greška"
                works = False
                try:
                    response = await client.get(request_url, headers=headers, timeout=self.timeout)
                    works = 200 <= response.status_code < 300
                    if self.success_text:
                        works = self.success_text.lower() in response.text[:8192].lower()
                    status = f"HTTP {response.status_code}"
                except Exception as error:
                    status = type(error).__name__.replace("Exception", "") or "Greška"
                self.result.emit(
                    {
                        "mac": normalize_mac(mac),
                        "works": "DA" if works else "NE",
                        "status": status,
                        "ping": f"{int((time.monotonic() - started) * 1000)} ms",
                    }
                )
                self.progress.emit(index, len(self.macs))
        self.finished_scan.emit()


class StalkerProfileCheckWorker(QThread):
    result = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    finished_scan = pyqtSignal()

    def __init__(self, profiles: list[tuple[str, str]], timeout: int = 4):
        super().__init__()
        self.profiles = profiles
        self.timeout = min(4, max(1, int(timeout)))
        self.running = True

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        headers = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; MAG250)",
            "Accept": "*/*",
        }
        async with httpx.AsyncClient(verify=False, follow_redirects=True, headers=headers) as client:
            for index, (portal, mac) in enumerate(self.profiles, 1):
                if not self.running:
                    break
                started = time.monotonic()
                status = "Greška"
                works = False
                try:
                    response = await client.get(
                        portal,
                        headers={**headers, "Cookie": f"mac={normalize_mac(mac)}"},
                        timeout=self.timeout,
                    )
                    status = f"HTTP {response.status_code}"
                    works = 200 <= response.status_code < 400
                except Exception as error:
                    status = type(error).__name__.replace("Exception", "") or "Greška"
                self.result.emit(
                    {
                        "portal": portal,
                        "mac": normalize_mac(mac),
                        "works": "DA" if works else "NE",
                        "status": status,
                        "ping": f"{int((time.monotonic() - started) * 1000)} ms",
                    }
                )
                self.progress.emit(index, len(self.profiles))
        self.finished_scan.emit()


class StalkerBalkanMacWorker(QThread):
    result = pyqtSignal(dict)
    progress = pyqtSignal(int, int)
    log = pyqtSignal(str)
    finished_scan = pyqtSignal()

    def __init__(
        self,
        profiles: list[tuple[str, str]],
        sample_size: int = 4,
        timeout: int = 10,
        category_limit: int = 8,
    ):
        super().__init__()
        self.profiles = profiles
        self.sample_size = min(8, max(1, int(sample_size)))
        self.timeout = min(30, max(3, int(timeout)))
        self.category_limit = min(16, max(2, int(category_limit)))
        self.running = True
        self._random = random.SystemRandom()

    def stop(self) -> None:
        self.running = False

    def run(self) -> None:
        total = len(self.profiles)
        for index, (portal, mac) in enumerate(self.profiles, 1):
            if not self.running:
                break
            self.result.emit(self._check_profile(portal, mac))
            self.progress.emit(index, total)
        self.finished_scan.emit()

    def _check_profile(self, portal: str, mac: str) -> dict[str, str]:
        started = time.monotonic()
        portal = normalize_url(portal)
        mac = normalize_mac(mac)
        result = {
            "portal": portal,
            "mac": mac,
            "balkan": "NE",
            "works": "NE",
            "tested": "0/0",
            "status": "Greška",
            "samples": "—",
            "ping": "—",
        }
        client = None
        try:
            stalker_module = _load_stalker_studio_module()
            scanner_module = _load_balkan_scanner_module()
            scanner = scanner_module.IPTVScanner(timeout=self.timeout)

            self.log.emit(f"Provjeravam Balkan kanale za {portal} / {mac}")
            if hasattr(stalker_module, "PORTAL_CONNECT_TIMEOUT"):
                stalker_module.PORTAL_CONNECT_TIMEOUT = self.timeout
            client = stalker_module.build_auto_client(portal, mac, adult_pin="0000")
            if hasattr(client, "timeout"):
                client.timeout = self.timeout

            categories = client.get_categories("IPTV")
            if not categories:
                result["status"] = "Nema Live grupa ili portal ne vraća popis."
                return result

            candidates, balkan_stats, checked_categories = self._collect_balkan_candidates(
                client,
                scanner,
                categories,
            )
            stats_text = _balkan_stats_summary(balkan_stats)
            if not candidates:
                result["status"] = (
                    f"Nema Balkan kanala u {len(categories)} Live grupa."
                    if not stats_text
                    else f"Balkan signal ({stats_text}), ali nema programa za test."
                )
                return result

            result["balkan"] = "DA"
            tested_samples = self._choose_samples(candidates)
            sample_results = []
            working_count = 0
            for sample in tested_samples:
                if not self.running:
                    break
                item = sample["item"]
                try:
                    play_url = client.resolve_play_url(item)
                    play_url = self._clean_stream_url(stalker_module, play_url)
                except Exception as error:
                    sample_results.append(f"{item.name}: link {self._short_error(error)}")
                    continue
                if not play_url:
                    sample_results.append(f"{item.name}: bez linka")
                    continue
                works, status = self._probe_stream(client, play_url)
                if works:
                    working_count += 1
                sample_results.append(f"{item.name}: {'DA' if works else 'NE'} ({status})")

            tested_count = len(sample_results)
            result["works"] = "DA" if working_count else "NE"
            result["tested"] = f"{working_count}/{tested_count}"
            result["samples"] = "; ".join(sample_results[:6]) or "—"
            result["status"] = self._status_text(
                working_count,
                tested_count,
                len(candidates),
                checked_categories,
                stats_text,
            )
        except Exception as error:
            result["status"] = self._short_error(error)
        finally:
            result["ping"] = f"{int((time.monotonic() - started) * 1000)} ms"
            if client:
                try:
                    client.close()
                except Exception:
                    pass
        return result

    def _collect_balkan_candidates(
        self,
        client: Any,
        scanner: Any,
        categories: list[Any],
    ) -> tuple[list[dict[str, Any]], dict[str, int], int]:
        ranked_categories = []
        balkan_stats = {key: 0 for key in scanner.balkan_signals.keys()}
        for category in categories:
            stats = scanner.score_text_for_balkan(category.name, source="category")
            score = sum(int(value or 0) for value in stats.values())
            ranked_categories.append((score, category, stats))
            if score:
                _merge_balkan_stats(balkan_stats, stats)
        ranked_categories.sort(key=lambda item: item[0], reverse=True)

        category_pool = [item for item in ranked_categories if item[0] > 0][: self.category_limit]
        if not category_pool:
            category_pool = ranked_categories[: self.category_limit]

        candidates: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        checked_categories = 0
        target_pool_size = max(self.sample_size * 8, 24)
        for category_score, category, category_stats in category_pool:
            if not self.running:
                break
            checked_categories += 1
            try:
                items = client.get_items(category, num_threads=2)
            except Exception as error:
                self.log.emit(f"Preskačem grupu {category.name}: {self._short_error(error)}")
                continue

            category_candidates = []
            for item in items:
                combined_text = f"{category.name} {item.name}"
                stream_stats = scanner.score_text_for_balkan(combined_text, source="stream")
                stream_score = sum(int(value or 0) for value in stream_stats.values())
                score = category_score + stream_score
                if score <= 0:
                    continue
                stats = dict(category_stats)
                _merge_balkan_stats(stats, stream_stats)
                key = (item.name.strip().lower(), (item.url or "").strip().lower())
                if key in seen:
                    continue
                seen.add(key)
                _merge_balkan_stats(balkan_stats, stats)
                category_candidates.append(
                    {
                        "category": category,
                        "item": item,
                        "score": score,
                        "stats": stats,
                    }
                )

            if not category_candidates and category_score > 0:
                fallback_items = list(items)
                self._random.shuffle(fallback_items)
                for item in fallback_items[:target_pool_size]:
                    key = (item.name.strip().lower(), (item.url or "").strip().lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    category_candidates.append(
                        {
                            "category": category,
                            "item": item,
                            "score": category_score,
                            "stats": dict(category_stats),
                        }
                    )

            candidates.extend(category_candidates)
            if len(candidates) >= target_pool_size and category_score > 0:
                break

        candidates.sort(key=lambda item: int(item["score"]), reverse=True)
        return candidates, balkan_stats, checked_categories

    def _choose_samples(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(candidates) <= self.sample_size:
            return list(candidates)
        strong_pool = candidates[: max(self.sample_size * 6, self.sample_size)]
        return self._random.sample(strong_pool, self.sample_size)

    def _clean_stream_url(self, stalker_module: types.ModuleType, play_url: str) -> str:
        url = (play_url or "").strip()
        if not url:
            return ""
        try:
            extracted = stalker_module.extract_http_from_text(url)
            if extracted:
                return extracted
        except Exception:
            pass
        try:
            url = stalker_module.normalize_cmd_or_url(url)
        except Exception:
            pass
        if not re.match(r"^https?://", url, flags=re.IGNORECASE):
            match = re.search(r"https?://\S+", url)
            url = match.group(0) if match else ""
        return url.strip()

    def _probe_stream(self, client: Any, play_url: str) -> tuple[bool, str]:
        headers = dict(getattr(client, "headers", {}) or {})
        headers.update(
            {
                "User-Agent": "VLC/3.0.20 LibVLC/3.0.20",
                "Accept": "*/*",
                "Connection": "close",
            }
        )
        response = None
        try:
            response = client.session.get(
                play_url,
                headers=headers,
                timeout=(min(5, self.timeout), self.timeout),
                stream=True,
                allow_redirects=True,
            )
            status = f"HTTP {response.status_code}"
            if response.status_code not in {200, 206}:
                return False, status

            content_type = response.headers.get("content-type", "").lower()
            chunk = b""
            for part in response.iter_content(chunk_size=4096):
                if part:
                    chunk = part
                    break

            preview = chunk[:256].lstrip().lower()
            if preview.startswith(b"#extm3u"):
                return True, f"{status} HLS"
            if preview.startswith(b"<html") or preview.startswith(b"{") or preview.startswith(b"["):
                return False, f"{status} nije stream"
            if b"not found" in preview[:160] or b"forbidden" in preview[:160]:
                return False, f"{status} odbijeno"
            if "html" in content_type or "json" in content_type:
                return False, f"{status} {content_type or 'tekst'}"
            if chunk:
                return True, status
            if any(marker in content_type for marker in ("video", "audio", "mpegurl", "octet-stream")):
                return True, status
            return False, f"{status} prazan odgovor"
        except Exception as error:
            return False, self._short_error(error)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _status_text(
        self,
        working_count: int,
        tested_count: int,
        candidate_count: int,
        checked_categories: int,
        stats_text: str,
    ) -> str:
        prefix = (
            f"Balkan signal: {stats_text}. "
            if stats_text
            else "Balkan signal pronađen po nazivima. "
        )
        if tested_count == 0:
            return prefix + f"Kandidata {candidate_count}, ali nije testiran nijedan stream."
        if working_count:
            return (
                prefix
                + f"Radi {working_count}/{tested_count}; kandidata {candidate_count}; "
                + f"grupa provjereno {checked_categories}."
            )
        return (
            prefix
            + f"Balkan postoji, ali 0/{tested_count} testiranih streamova radi; "
            + f"kandidata {candidate_count}; grupa provjereno {checked_categories}."
        )

    def _short_error(self, error: Exception) -> str:
        name = error.__class__.__name__.replace("Exception", "") or "Greška"
        message = str(error).strip()
        if len(message) > 120:
            message = message[:117] + "..."
        return f"{name}: {message}" if message else name


class PlaylistWorker(QThread):
    loaded = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, server: str, username: str, password: str, content_type: str = "Live"):
        super().__init__()
        self.server = server.rstrip("/")
        self.username = username
        self.password = password
        self.content_type = content_type

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        api_url = f"{self.server}/player_api.php"
        base_params = {"username": self.username, "password": self.password}
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            }
            async with httpx.AsyncClient(
                verify=False,
                follow_redirects=True,
                timeout=25,
                headers=headers,
            ) as client:
                auth = await _json_get(client, api_url, base_params)
                info = auth.get("user_info", {}) if isinstance(auth, dict) else {}
                if str(auth.get("auth", "0")) != "1" and str(info.get("status", "")).lower() != "active":
                    raise RuntimeError("Račun nije aktivan ili podaci nisu ispravni.")

                actions = {
                    "Live": ("get_live_categories", "get_live_streams"),
                    "VOD": ("get_vod_categories", "get_vod_streams"),
                    "Serije": ("get_series_categories", "get_series"),
                }
                category_action, stream_action = actions.get(
                    self.content_type, actions["Live"]
                )
                categories_payload, streams_payload = await asyncio.gather(
                    _json_get(client, api_url, {**base_params, "action": category_action}),
                    _json_get(client, api_url, {**base_params, "action": stream_action}),
                )
                categories = {
                    str(_first_value(item, "category_id", "id")): str(
                        _first_value(item, "category_name", "name", "title") or "Ostalo"
                    )
                    for item in _payload_list(
                        categories_payload, ("categories", "data", "items")
                    )
                    if isinstance(item, dict)
                }
                streams = _payload_list(
                    streams_payload,
                    ("streams", "channels", "movies", "series", "data", "items"),
                )
                if not streams and categories:
                    fetched_by_category = []
                    seen_streams = set()
                    for category_id in categories:
                        payload = await _json_get(
                            client,
                            api_url,
                            {
                                **base_params,
                                "action": stream_action,
                                "category_id": category_id,
                            },
                        )
                        for stream in _payload_list(
                            payload,
                            ("streams", "channels", "movies", "series", "data", "items"),
                        ):
                            if not isinstance(stream, dict):
                                continue
                            row = dict(stream)
                            row.setdefault("category_id", category_id)
                            key = _stream_key(row, self.content_type)
                            if not key or key in seen_streams:
                                continue
                            seen_streams.add(key)
                            fetched_by_category.append(row)
                    streams = fetched_by_category
                rows = []
                for stream in streams:
                    if not isinstance(stream, dict):
                        continue
                    if self.content_type == "Live":
                        prefix = "live"
                        stream_id = _first_value(stream, "stream_id", "id")
                        extension = stream.get("container_extension") or "ts"
                        name = str(_first_value(stream, "name", "title") or "Bez naziva")
                        category_id = _category_id(_first_value(stream, "category_id"))
                        logo = _first_value(stream, "stream_icon", "cover", "logo")
                    elif self.content_type == "VOD":
                        prefix = "movie"
                        stream_id = _first_value(stream, "stream_id", "movie_id", "id")
                        extension = stream.get("container_extension") or "mp4"
                        name = str(_first_value(stream, "name", "title") or "Bez naziva")
                        category_id = _category_id(_first_value(stream, "category_id"))
                        logo = _first_value(
                            stream, "stream_icon", "cover", "movie_image", "logo"
                        )
                    else:
                        stream_id = _first_value(stream, "series_id", "id", "stream_id")
                        extension = ""
                        name = str(_first_value(stream, "name", "title") or "Serija")
                        category_id = _category_id(_first_value(stream, "category_id"))
                        logo = _first_value(
                            stream, "cover", "stream_icon", "movie_image", "logo"
                        )
                    if not stream_id:
                        continue
                    if self.content_type == "Serije":
                        stream_url = (
                            f"{api_url}?username={self.username}&password={self.password}"
                            f"&action=get_series_info&series_id={stream_id}"
                        )
                    else:
                        stream_url = (
                            f"{self.server}/{prefix}/{self.username}/{self.password}/"
                            f"{stream_id}.{extension}"
                        )
                    rows.append(
                        {
                            "name": name,
                            "category": categories.get(str(category_id), "Ostalo"),
                            "logo": str(logo or ""),
                            "epg_id": str(_first_value(stream, "epg_channel_id", "tvg_id")),
                            "type": self.content_type,
                            "url": stream_url,
                        }
                    )
                self.loaded.emit(rows)
        except Exception as error:
            self.failed.emit(str(error))


def parse_mac_lines(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in re.finditer(
        r"(?i)(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|[0-9a-f]{12}", text
    ):
        mac = normalize_mac(match.group(0))
        if mac not in seen:
            seen.add(mac)
            result.append(mac)
    return result
