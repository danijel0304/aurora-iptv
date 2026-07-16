from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse


URL_PATTERN = re.compile(r"(?i)\b((?:https?://|www\.)[^\s<>\"\]\)]+)")
DOMAIN_PATTERN = re.compile(
    r"\b(?:(?:https?://)|(?:www\.))?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(?:\:\d{1,5})?"
    r"(?:/[^\s<>\"\]\)]*)?",
    re.IGNORECASE,
)
MAC_PATTERN = re.compile(
    r"\b(?:(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}|"
    r"(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}|[0-9A-Fa-f]{12})\b"
)
NON_PORTAL_EXTENSIONS = {
    ".m3u",
    ".m3u8",
    ".ts",
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".mp3",
    ".aac",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".srt",
}


def normalize_url(value: str) -> str:
    value = value.strip().rstrip('.,;:!?)"]}')
    if value.lower().startswith("www."):
        return "http://" + value
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.IGNORECASE):
        return "http://" + value
    return value


def normalize_mac(value: str) -> str:
    compact = re.sub(r"[^0-9A-Fa-f]", "", value)
    if len(compact) != 12:
        return value.strip().upper()
    return ":".join(compact[i : i + 2] for i in range(0, 12, 2)).upper()


def is_portal_candidate(url: str) -> bool:
    try:
        parsed = urlparse(normalize_url(url))
    except ValueError:
        return False
    host = parsed.hostname or ""
    if not host or "." not in host:
        return False
    path = parsed.path.lower()
    if any(path.endswith(extension) for extension in NON_PORTAL_EXTENSIONS):
        return False
    if any(part in path for part in ("/live/", "/movie/", "/series/")):
        return False
    if path.endswith("/get.php") or path.endswith("/player_api.php"):
        return False
    return True


def extract_urls(text: str) -> list[str]:
    urls = [normalize_url(match.group(1)) for match in URL_PATTERN.finditer(text)]
    seen = set(urls)
    for match in DOMAIN_PATTERN.finditer(text):
        url = normalize_url(match.group(0))
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def is_playlist_url(url: str, include_m3u8: bool = True, query_markers: bool = True) -> bool:
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        query = parse_qs(parsed.query)
        if path.endswith(".m3u") or path.endswith("/m3u") or path.endswith("/m3u_plus"):
            return True
        if include_m3u8 and (path.endswith(".m3u8") or path.endswith("/m3u8")):
            return True
        if "get.php" in path and {"username", "password"}.issubset(query):
            return True
        if {"username", "password"}.issubset(query):
            type_values = query.get("type", []) + query.get("output", [])
            if not type_values or any("m3u" in value.lower() for value in type_values):
                return True
        if any(marker in path for marker in ("/playlist/", "/playlists/", "/m3u/", "/m3u8/")):
            return True
        if query_markers:
            for key, values in query.items():
                if "m3u" in key.lower():
                    return True
                if any("m3u" in unquote(value).lower() for value in values):
                    return True
    except ValueError:
        return False
    return False


@dataclass(slots=True)
class UrlExtraction:
    urls: list[str]
    total_found: int
    duplicates: int
    hosts: int
    discarded: int = 0
    channels: int = 0
    is_m3u: bool = False


def extract_playlist_urls(
    text: str,
    include_m3u8: bool = True,
    query_markers: bool = True,
    playlists_only: bool = True,
    sort_by_host: bool = False,
    dedupe: bool = True,
) -> UrlExtraction:
    found = extract_urls(text)
    filtered = [
        url
        for url in found
        if not playlists_only or is_playlist_url(url, include_m3u8, query_markers)
    ]
    result = list(dict.fromkeys(filtered)) if dedupe else list(filtered)
    if sort_by_host:
        result.sort(key=lambda url: ((urlparse(url).hostname or "").lower(), url.lower()))
    hosts = len({urlparse(url).hostname for url in result if urlparse(url).hostname})
    return UrlExtraction(
        result,
        len(found),
        len(filtered) - len(result),
        hosts,
        discarded=len(found) - len(filtered),
        channels=sum(1 for line in text.splitlines() if line.strip().upper().startswith("#EXTINF")),
        is_m3u=text.lstrip().upper().startswith("#EXTM3U"),
    )


@dataclass(slots=True)
class MacGroups:
    groups: list[tuple[str, list[str]]]
    ignored: int
    duplicates: int

    @property
    def mac_count(self) -> int:
        return sum(len(macs) for _, macs in self.groups)


def group_macs_by_url(
    text: str,
    global_dedupe: bool = False,
    sort_urls: bool = False,
    sort_macs: bool = False,
) -> MacGroups:
    groups: list[tuple[str, list[str]]] = []
    positions: dict[str, int] = {}
    per_url: dict[str, set[str]] = {}
    globally_seen: set[str] = set()
    current_url: str | None = None
    ignored = 0
    duplicates = 0

    def add_url(url_value: str) -> str:
        normalized = normalize_url(url_value)
        if normalized not in positions:
            positions[normalized] = len(groups)
            groups.append((normalized, []))
            per_url[normalized] = set()
        return normalized

    def add_mac(url_value: str, mac_value: str) -> None:
        nonlocal duplicates
        mac = normalize_mac(mac_value)
        if mac in per_url[url_value] or (global_dedupe and mac in globally_seen):
            duplicates += 1
            return
        groups[positions[url_value]][1].append(mac)
        per_url[url_value].add(mac)
        globally_seen.add(mac)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        mac_matches = list(MAC_PATTERN.finditer(line))
        macs = [normalize_mac(match.group(0)) for match in mac_matches]
        url_scan_line = line
        for match in reversed(mac_matches):
            url_scan_line = url_scan_line[: match.start()] + " " + url_scan_line[match.end() :]
        raw_url_matches = list(DOMAIN_PATTERN.finditer(url_scan_line))
        url_matches = [
            match
            for match in raw_url_matches
            if is_portal_candidate(match.group(0))
        ]
        if raw_url_matches and not url_matches:
            ignored += len(macs)
            current_url = None
            continue
        if url_matches:
            # Svaki URL otvara novi blok. Ako je u istoj liniji vise URL-ova,
            # MAC adrese iz te linije pripisuju se prvom URL-u jer je to jedini
            # format koji se moze pouzdano procitati bez dodatnih oznaka.
            current_url = add_url(url_matches[0].group(0))
            for extra_url in url_matches[1:]:
                add_url(extra_url.group(0))
        if macs and not current_url:
            ignored += len(macs)
            continue
        for mac in macs:
            add_mac(current_url, mac)

    if sort_macs:
        groups = [(url, sorted(macs)) for url, macs in groups]
    if sort_urls:
        groups.sort(key=lambda item: item[0].lower())
    return MacGroups(groups, ignored, duplicates)


def format_mac_groups(groups: Iterable[tuple[str, list[str]]]) -> str:
    blocks = ["\n".join([url, *macs]) for url, macs in groups]
    return "\n\n".join(blocks)


def parse_xtream_url(url: str) -> tuple[str, str, str] | None:
    try:
        parsed = urlparse(normalize_url(url))
        query = parse_qs(parsed.query)
        username = query.get("username", [""])[0]
        password = query.get("password", [""])[0]
        if not parsed.netloc or not username or not password:
            return None
        return f"{parsed.scheme}://{parsed.netloc}", username, password
    except ValueError:
        return None


class Vault:
    def __init__(self, path: Path):
        self.path = path
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expiry TEXT,
                    connections TEXT,
                    content TEXT,
                    playlist_url TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    UNIQUE(server, username, password)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_lists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT,
                    item_count INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def save(self, result: dict[str, str]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (
                    server, username, password, status, expiry, connections,
                    content, playlist_url, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(server, username, password) DO UPDATE SET
                    status=excluded.status,
                    expiry=excluded.expiry,
                    connections=excluded.connections,
                    content=excluded.content,
                    playlist_url=excluded.playlist_url,
                    checked_at=excluded.checked_at
                """,
                (
                    result["server"],
                    result["username"],
                    result["password"],
                    result["status"],
                    result.get("expiry", ""),
                    result.get("connections", ""),
                    result.get("content", ""),
                    result["playlist_url"],
                ),
            )

    def rows(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM accounts ORDER BY checked_at DESC"))

    def delete(self, account_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def delete_all_accounts(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM accounts")

    def save_list(
        self,
        name: str,
        kind: str,
        source: str,
        content: str,
        item_count: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO saved_lists (
                    name, kind, source, item_count, content, created_at
                ) VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
                """,
                (name, kind, source, item_count, content),
            )

    def saved_lists(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT id, name, kind, source, item_count, created_at
                    FROM saved_lists
                    ORDER BY created_at DESC
                    """
                )
            )

    def saved_list(self, list_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM saved_lists WHERE id = ?", (list_id,)
            ).fetchone()

    def delete_saved_list(self, list_id: int) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM saved_lists WHERE id = ?", (list_id,))

    def delete_all_saved_lists(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM saved_lists")
