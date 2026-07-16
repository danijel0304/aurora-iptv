from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from PyQt6.QtCore import QThread, pyqtSignal

from core import normalize_mac, parse_xtream_url


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
