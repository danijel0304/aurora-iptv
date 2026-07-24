import sys
import asyncio
import re
import httpx
import os
import tempfile
import subprocess
import sqlite3
import time
import random
import json
import logging
from urllib.parse import quote
from datetime import datetime
from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from scanner import IPTVScanner
from ui_components import STYLE_SHEET, StatCard

SETTINGS_FILE = "settings.json"
LOG_FILE = "fusion.log"

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# --- INICIJALIZACIJA BAZE PODATAKA ---
def init_db():
    conn = sqlite3.connect("fusion_vault.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS vault (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 server TEXT, user TEXT, pass TEXT, status TEXT,
                 exyu TEXT, expiry TEXT, notes TEXT, last_checked TEXT,
                 url TEXT)''')
    conn.commit()
    conn.close()

init_db()

class NumericTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            val1 = self.sort_value(self.text())
            val2 = self.sort_value(other.text())
            return val1 < val2
        except Exception:
            return super().__lt__(other)

    def sort_value(self, text):
        clean = text.strip()
        if clean.lower() == "unlimited":
            return float("inf")
        date_match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", clean)
        if date_match:
            day, month, year = map(int, date_match.groups())
            return year * 10000 + month * 100 + day
        if "timeout" in clean.lower() or clean.upper() == "N/A":
            return float("inf")
        return float(re.findall(r"[-+]?\d*\.\d+|\d+", clean)[0])

# --- THREADOVI (ASINKRONI RADNICI) ---
class ScannerThread(QThread):
    result_ready = pyqtSignal(dict)
    finished_signal = pyqtSignal()
    progress_update = pyqtSignal(int)

    def __init__(self, urls, max_threads=10, proxy_list=None, user_agent=None):
        super().__init__()
        self.urls = urls
        self.scanner = IPTVScanner()
        self.is_running = True
        self.max_threads = max_threads
        self.proxy_list = proxy_list if proxy_list else []
        self.user_agent = user_agent

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.process())
        except Exception as e:
            logging.exception("ScannerThread se zaustavio zbog greske: %s", e)
            self.finished_signal.emit()
        finally:
            loop.close()

    async def process(self):
        limits = httpx.Limits(
            max_keepalive_connections=max(5, self.max_threads),
            max_connections=max(10, self.max_threads * 3)
        )
        headers = {"User-Agent": self.user_agent} if self.user_agent else None
        proxy = random.choice(self.proxy_list).strip() if self.proxy_list else None
        client_kwargs = {
            "verify": False,
            "timeout": 15.0,
            "limits": limits,
            "headers": headers
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        try:
            client = httpx.AsyncClient(**client_kwargs)
        except TypeError:
            client_kwargs.pop("proxy", None)
            logging.warning("Instalirana verzija httpx ne podrzava proxy parametar.")
            client = httpx.AsyncClient(**client_kwargs)

        async with client:
            total = len(self.urls)
            self.completed_tasks = 0
            semaphore = asyncio.Semaphore(self.max_threads)
            tasks = []
            for url in self.urls:
                if not self.is_running:
                    break
                tasks.append(asyncio.create_task(self.check_single(client, url, total, semaphore)))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        logging.error(
                            "Zadatak skeniranja nije uspio.",
                            exc_info=(type(result), result, result.__traceback__)
                        )

        self.finished_signal.emit()

    async def check_single(self, client, url, total, semaphore):
        async with semaphore:
            if not self.is_running:
                return
            res = await self.scanner.check_portal(client, url)

        if not res:
             try:
                 base = self.scanner.extract_base_url(url)
                 user = re.search(r'username=([^&]+)', url).group(1) if 'username=' in url else "N/A"
                 pw = re.search(r'password=([^&]+)', url).group(1) if 'password=' in url else "N/A"
                 res = {
                     "server": base, "user": user, "pass": pw, "status": "Offline",
                     "exyu": "NE", "ch_count": "0", "expiry": "N/A", "conns": "0/0",
                     "ping": "Timeout", "epg_link": "N/A", "url": base
                 }
             except:
                 res = None

        if res and self.is_running:
            self.result_ready.emit(res)

        if self.is_running:
            self.completed_tasks += 1
            self.progress_update.emit(int((self.completed_tasks / total) * 100))

class SuperSearchThread(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(list)

    def __init__(self, servers, keyword):
        super().__init__()
        self.servers = servers
        self.keyword = keyword.lower()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        found_channels = loop.run_until_complete(self.process())
        self.finished.emit(found_channels)

    async def process(self):
        results = []
        async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
            for srv in self.servers:
                self.progress.emit(f"Pretražujem: {srv['server']} ...")
                url = f"{srv['server']}/player_api.php?username={srv['user']}&password={srv['pass']}&action=get_live_streams"
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if isinstance(data, list):
                            for ch in data:
                                if isinstance(ch, dict) and self.keyword in str(ch.get('name', '')).lower():
                                    stream_url = f"{srv['server']}/live/{srv['user']}/{srv['pass']}/{ch.get('stream_id', '')}.ts"
                                    results.append({
                                        "name": ch.get('name', 'Nepoznato'),
                                        "server": srv['server'],
                                        "url": stream_url,
                                        "ping": srv.get('ping', 999)
                                    })
                except:
                    pass
        return results

class StreamCheckThread(QThread):
    result_ready = pyqtSignal(str)

    def __init__(self, server, user, pw, mode="first", sample_size=5, region=None, user_agent=None):
        super().__init__()
        self.server = server
        self.user = user
        self.pw = pw
        self.mode = mode
        self.sample_size = sample_size
        self.region = region
        self.user_agent = user_agent
        self.scanner = IPTVScanner()
        self.selection_label = "Random"
        self.quick_mode = mode in ("exyu_random", "region_random", "random", "smart_random")

    def run(self):
        try:
            import requests
            if self.mode == "smart_random":
                streams = self.load_smart_random_streams(requests)
            elif self.mode in ("exyu_random", "region_random"):
                streams = self.load_random_exyu_streams(requests)
                self.selection_label = self.region or "Ex-YU"
            elif self.mode == "random":
                streams = self.load_random_streams(requests)
                self.selection_label = "Random"
            else:
                api_url = f"{self.server}/player_api.php?username={self.user}&password={self.pw}&action=get_live_streams"
                streams = self.get_json(requests, api_url, timeout=8)

            if not isinstance(streams, list) or not streams:
                if self.mode == "smart_random":
                    self.result_ready.emit("Nema Ex-YU ni drugih test kanala")
                    return
                label = self.region or "Ex-YU"
                self.result_ready.emit(f"Nema {label} kanala" if self.quick_mode else "Nema kanala")
                return

            checked = 0
            working = 0
            reasons = {}
            failed_names = []
            for stream in streams[:self.sample_size]:
                if not isinstance(stream, dict):
                    continue
                sid = stream.get("stream_id")
                if not sid:
                    continue
                checked += 1
                ok, reason = self.stream_works(requests, stream)
                if ok:
                    working += 1
                else:
                    reasons[reason] = reasons.get(reason, 0) + 1
                    failed_names.append(str(stream.get("name", "Nepoznato"))[:40])

            if checked == 0:
                self.result_ready.emit("Nema test kanala")
            elif working > 0:
                if self.quick_mode:
                    label = self.selection_label
                    needed = self.required_working_streams(checked)
                    if working >= needed:
                        self.result_ready.emit(f"Lista ispravna: DA | {label} radi ({working}/{checked})")
                    else:
                        reason = max(reasons, key=reasons.get) if reasons else "slab uzorak"
                        self.result_ready.emit(f"Lista ispravna: NE | {label} radi samo ({working}/{checked}) | {reason}")
                else:
                    self.result_ready.emit(f"Radi ({working}/{checked})")
            else:
                reason = max(reasons, key=reasons.get) if reasons else "nepoznato"
                detail = f" | {reason}"
                if failed_names:
                    detail += f" | npr. {', '.join(failed_names[:2])}"
                if self.quick_mode:
                    label = self.selection_label
                    self.result_ready.emit(f"Lista ispravna: NE | {label} ne radi (0/{checked}){detail}")
                else:
                    self.result_ready.emit(f"Ne radi (0/{checked}){detail}")
        except Exception as e:
            logging.exception("Stream provjera nije uspjela za %s: %s", self.server, e)
            self.result_ready.emit("Greška / Timeout")

    def required_working_streams(self, checked):
        if checked <= 2:
            return 1
        return max(2, (checked + 2) // 3)

    def api_headers(self):
        return {"User-Agent": self.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json,*/*"}

    def get_json(self, requests_module, url, timeout=10):
        try:
            resp = requests_module.get(url, headers=self.api_headers(), timeout=timeout, allow_redirects=True)
            if resp.status_code != 200:
                logging.info("Xtream API nije vratio 200 za %s: HTTP %s", self.server, resp.status_code)
                return None
            return resp.json()
        except ValueError:
            logging.info("Xtream API nije vratio JSON za %s", self.server)
            return None
        except requests_module.exceptions.RequestException as e:
            logging.info("Xtream API dohvat nije uspio za %s: %s", self.server, self.scanner.describe_error(e))
            return None

    def load_random_exyu_streams(self, requests_module):
        api_base = f"{self.server}/player_api.php?username={self.user}&password={self.pw}"
        categories = self.get_json(requests_module, f"{api_base}&action=get_live_categories", timeout=10)
        if not isinstance(categories, list):
            return []

        ranked_categories = []
        for category in categories:
            if not isinstance(category, dict):
                continue
            stats = self.scanner.score_text_for_balkan(category.get("category_name", ""), source="category")
            if self.region:
                if stats.get(self.region, 0) <= 0:
                    continue
                ranked_categories.append((stats.get(self.region, 0), category))
            elif self.scanner.is_balkan_detected(stats) or any(stats.values()):
                ranked_categories.append((sum(stats.values()), category))

        ranked_categories.sort(key=lambda item: item[0], reverse=True)
        pool = []
        category_limit = 3 if self.quick_mode else 8
        for _, category in ranked_categories[:category_limit]:
            cat_id = category.get("category_id")
            if cat_id in (None, ""):
                continue
            try:
                streams = self.get_json(requests_module, f"{api_base}&action=get_live_streams&category_id={cat_id}", timeout=6 if self.quick_mode else 12)
                if isinstance(streams, list):
                    pool.extend([s for s in streams if isinstance(s, dict) and s.get("stream_id")])
            except Exception:
                continue

        if not pool:
            streams = self.get_json(requests_module, f"{api_base}&action=get_live_streams", timeout=12)
            if isinstance(streams, list):
                for stream in streams:
                    if not isinstance(stream, dict) or not stream.get("stream_id"):
                        continue
                    text = " ".join([
                        str(stream.get("name", "")),
                        str(stream.get("epg_channel_id", "")),
                        str(stream.get("category_name", ""))
                    ])
                    stats = self.scanner.score_text_for_balkan(text, source="stream")
                    if self.region:
                        if stats.get(self.region, 0) > 0:
                            pool.append(stream)
                    elif self.scanner.is_balkan_detected(stats) or any(stats.values()):
                        pool.append(stream)

        if not pool:
            return []
        return random.sample(pool, min(self.sample_size, len(pool)))

    def load_smart_random_streams(self, requests_module):
        exyu_streams = self.load_random_exyu_streams(requests_module)
        if exyu_streams:
            self.selection_label = "Ex-YU"
            return exyu_streams

        other_streams = self.load_random_non_exyu_streams(requests_module)
        if other_streams:
            self.selection_label = "Nema Ex-YU, ostali"
            return other_streams

        self.selection_label = "Nema Ex-YU, random"
        return self.load_random_streams(requests_module)

    def load_random_streams(self, requests_module):
        api_base = f"{self.server}/player_api.php?username={self.user}&password={self.pw}"
        streams = self.get_json(requests_module, f"{api_base}&action=get_live_streams", timeout=12)
        if not isinstance(streams, list):
            return []
        pool = [s for s in streams if isinstance(s, dict) and s.get("stream_id")]
        if not pool:
            return []
        return random.sample(pool, min(self.sample_size, len(pool)))

    def load_random_non_exyu_streams(self, requests_module):
        api_base = f"{self.server}/player_api.php?username={self.user}&password={self.pw}"
        categories = self.get_json(requests_module, f"{api_base}&action=get_live_categories", timeout=10)
        category_names = {}
        if isinstance(categories, list):
            for category in categories:
                if isinstance(category, dict):
                    category_names[str(category.get("category_id", ""))] = str(category.get("category_name", ""))

        streams = self.get_json(requests_module, f"{api_base}&action=get_live_streams", timeout=12)
        if not isinstance(streams, list):
            return []

        pool = []
        for stream in streams:
            if not isinstance(stream, dict) or not stream.get("stream_id"):
                continue
            category_name = category_names.get(str(stream.get("category_id", "")), "")
            text = " ".join([
                str(stream.get("name", "")),
                str(stream.get("epg_channel_id", "")),
                str(stream.get("category_name", "")),
                category_name
            ])
            stats = self.scanner.score_text_for_balkan(text, source="stream")
            if not self.scanner.is_balkan_detected(stats) and not any(stats.values()):
                pool.append(stream)

        if not pool:
            return []
        return random.sample(pool, min(self.sample_size, len(pool)))

    def stream_works(self, requests_module, stream):
        stream_id = stream.get("stream_id") if isinstance(stream, dict) else stream
        direct_source = str(stream.get("direct_source", "")).strip() if isinstance(stream, dict) else ""
        container_ext = str(stream.get("container_extension", "") or "ts").strip().lstrip(".")
        if container_ext.lower() in ("", "None".lower()):
            container_ext = "ts"

        user = quote(str(self.user), safe="%")
        pw = quote(str(self.pw), safe="%")
        stream_id = quote(str(stream_id), safe="%")
        extensions = list(dict.fromkeys([container_ext, "ts", "m3u8"]))
        candidates = []
        if direct_source.startswith(("http://", "https://")):
            candidates.append(direct_source)
        for ext in extensions:
            candidates.append(f"{self.server}/live/{user}/{pw}/{stream_id}.{ext}")
        candidates.extend([
            f"{self.server}/live/{user}/{pw}/{stream_id}",
            f"{self.server}/{user}/{pw}/{stream_id}",
        ])

        headers_list = [
            {"User-Agent": self.user_agent or "VLC/3.0.11 LibVLC/3.0.11", "Accept": "*/*"},
            {"User-Agent": "VLC/3.0.11 LibVLC/3.0.11", "Accept": "*/*"},
            {"User-Agent": "IPTVSmartersPro", "Accept": "*/*"},
            {"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
        ]
        headers_list = list({tuple(sorted(headers.items())): headers for headers in headers_list}.values())
        if self.quick_mode:
            candidates = candidates[:4]
            headers_list = headers_list[:2]
        timeout = (3, 6) if self.quick_mode else (5, 12)
        for url in candidates:
            for headers in headers_list:
                try:
                    with requests_module.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
                        if r.status_code not in (200, 206):
                            last_reason = f"HTTP {r.status_code}"
                            continue

                        content_type = r.headers.get("Content-Type", "").lower()
                        if any(token in content_type for token in ("text/html", "application/json", "text/plain", "xml")):
                            last_reason = "error stranica"
                            continue

                        data = bytearray()
                        start_time = time.time()
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                data.extend(chunk)
                            if self.stream_payload_is_valid(bytes(data), content_type):
                                return True, "OK"
                            if len(data) >= 32768 or time.time() - start_time > 4.0:
                                break
                        if self.payload_looks_like_error(bytes(data), content_type):
                            last_reason = "error odgovor"
                        else:
                            last_reason = "bez podataka" if not data else "premalo podataka"
                except requests_module.exceptions.Timeout:
                    last_reason = "timeout"
                    continue
                except requests_module.exceptions.RequestException:
                    last_reason = "veza"
                    continue
        return False, locals().get("last_reason", "nepoznato")

    def stream_payload_is_valid(self, data, content_type):
        if not data:
            return False
        if b"#EXTM3U" in data[:2048]:
            return True
        if self.payload_looks_like_error(data, content_type):
            return False
        if any(token in content_type for token in ("video/", "audio/", "octet-stream")) and len(data) >= 4096:
            return True
        if len(data) >= 16384:
            return True
        return False

    def payload_looks_like_error(self, data, content_type):
        if not data:
            return False
        sample = data[:2048].lstrip()
        lower = sample[:512].lower()
        if lower.startswith((b"<!doctype html", b"<html", b"<?xml", b"{", b"[")):
            return True
        try:
            text = sample.decode("utf-8", errors="ignore").lower()
        except Exception:
            return False
        error_tokens = (
            "not authorized", "unauthorized", "forbidden", "not found",
            "invalid", "expired", "blocked", "bouquet", "active code",
            "username", "password", "access denied", "404", "403"
        )
        if any(token in text for token in error_tokens):
            return True
        return any(token in content_type for token in ("text/html", "application/json", "text/plain", "xml"))

class ProxyScraperThread(QThread):
    result_ready = pyqtSignal(str)

    def run(self):
        try:
            import requests
            r = requests.get("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", timeout=10)
            if r.status_code == 200:
                proxies = r.text.splitlines()[:100]
                self.result_ready.emit("\n".join([f"http://{p}" for p in proxies]))
            else:
                self.result_ready.emit("")
        except:
            self.result_ready.emit("")

class StalkerWorker(QThread):
    result_ready = pyqtSignal(dict)
    progress_update = pyqtSignal(int)
    finished_signal = pyqtSignal()

    def __init__(self, portal, macs):
        super().__init__()
        self.portal = portal.strip().rstrip('/')
        if self.portal.endswith('/c'):
            self.portal = self.portal[:-2]
        self.macs = macs
        self.is_running = True

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.process())

    async def process(self):
        total = len(self.macs)
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
        async with httpx.AsyncClient(verify=False, timeout=20.0, limits=limits) as client:
            for i, mac in enumerate(self.macs):
                if not self.is_running:
                    break
                res = await self.check_mac(client, mac)
                if res:
                    self.result_ready.emit(res)
                self.progress_update.emit(int(((i + 1) / total) * 100))

        self.finished_signal.emit()

    async def check_mac(self, client, mac):
        mac = mac.strip().upper()

        base_url = self.portal.strip().rstrip('/')
        api_base = f"{base_url}/server/load.php"
        start_t = time.time()

        headers = {
            "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
            "X-User-Agent": "Model: MAG200; Link: WiFi",
            "Accept": "*/*",
            "Referer": f"{base_url}/c/",
            "Accept-Language": "en-US,en;q=0.9"
        }

        cookies = {
            "mac": mac,
            "stb_lang": "en",
            "timezone": "Europe/Zagreb"
        }

        try:
            hs_url = f"{api_base}?type=stb&action=handshake&JsHttpRequest=1-xml"
            hs_resp = await client.get(hs_url, headers=headers, cookies=cookies, follow_redirects=True)
            ping = f"{int((time.time() - start_t) * 1000)}ms"

            for k, v in hs_resp.cookies.items():
                cookies[k] = v

            token = ""
            if hs_resp.status_code == 200:
                try:
                    hs_json = hs_resp.json()
                    token = hs_json.get("js", {}).get("token", "") or hs_json.get("token", "")
                except Exception:
                    if "token" in hs_resp.text.lower():
                        token = "present"

            if hs_resp.status_code == 200 and token:
                if token != "present":
                    headers["Authorization"] = f"Bearer {token}"

                expiry = "Unlimited"
                status = "Online"

                try:
                    prof_url = f"{api_base}?type=stb&action=get_profile&JsHttpRequest=1-xml"
                    prof_resp = await client.get(prof_url, headers=headers, cookies=cookies, follow_redirects=True)

                    if prof_resp.status_code == 200:
                        prof_json = prof_resp.json()
                        prof_data = prof_json.get("js", {})

                        if isinstance(prof_data, dict):
                            if prof_data.get("status") == 3 or prof_data.get("is_blocked"):
                                status = "Blokiran"

                            expire_ts = prof_data.get("expire_billing_date")
                            if expire_ts and str(expire_ts) != "0":
                                from PyQt6.QtCore import QDateTime
                                expiry = QDateTime.fromSecsSinceEpoch(int(expire_ts)).toString("dd.MM.yyyy")
                except Exception:
                    pass

                return {
                    "mac": mac,
                    "status": status,
                    "expiry": expiry,
                    "ping": ping,
                    "url": f"{base_url}/c/"
                }

        except Exception:
            pass

        return {
            "mac": mac,
            "status": "Offline",
            "expiry": "N/A",
            "ping": "Timeout",
            "url": f"{base_url}/c/"
        }

class StalkerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🕵️ Stalker (MAC) Portal Tester")
        self.resize(800, 600)
        self.setStyleSheet(STYLE_SHEET)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        info = QLabel("Ovaj alat isključivo provjerava radi li MAC na navedenom portalu.")
        info.setStyleSheet("color: #8b949e; font-size: 13px; margin-bottom: 10px;")
        layout.addWidget(info)

        h_url = QHBoxLayout()
        h_url.addWidget(QLabel("Portal URL:"))
        self.txt_portal = QLineEdit()
        self.txt_portal.setPlaceholderText("http://mag.portal.com:8080/c/")
        h_url.addWidget(self.txt_portal)
        layout.addLayout(h_url)

        layout.addWidget(QLabel("Unesi MAC adrese (jedna po liniji):"))
        self.txt_macs = QTextEdit()
        self.txt_macs.setPlaceholderText("00:1A:79:XX:YY:ZZ")
        layout.addWidget(self.txt_macs)

        self.pbar = QProgressBar()
        self.pbar.setValue(0)
        layout.addWidget(self.pbar)

        self.btn_start = QPushButton("POKRENI MAC SKENER")
        self.btn_start.setObjectName("ActionBtn")
        self.btn_start.clicked.connect(self.toggle_scan)
        layout.addWidget(self.btn_start)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["MAC Adresa", "Status", "Ističe", "Ping", "Portal URL"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

    def toggle_scan(self):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.is_running = False
            self.btn_start.setText("POKRENI MAC SKENER")
            self.btn_start.setObjectName("ActionBtn")
            self.setStyleSheet(STYLE_SHEET)
        else:
            portal = self.txt_portal.text().strip()
            if not portal:
                return QMessageBox.warning(self, "Greška", "Unesite Portal URL!")

            macs = re.findall(r'([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})', self.txt_macs.toPlainText())
            if not macs:
                return QMessageBox.warning(self, "Greška", "Nisu pronađene važeće MAC adrese.")

            self.table.setRowCount(0)
            self.pbar.setValue(0)
            self.btn_start.setText("ZAUSTAVI SKENIRANJE")
            self.btn_start.setObjectName("StopBtn")
            self.setStyleSheet(STYLE_SHEET)

            unique_macs = list(dict.fromkeys(macs))
            self.worker = StalkerWorker(portal, unique_macs)
            self.worker.result_ready.connect(self.add_res)
            self.worker.progress_update.connect(self.pbar.setValue)
            self.worker.finished_signal.connect(self.scan_finished)
            self.worker.start()

    def scan_finished(self):
        self.btn_start.setText("POKRENI MAC SKENER")
        self.btn_start.setObjectName("ActionBtn")
        self.setStyleSheet(STYLE_SHEET)

    def add_res(self, d):
        row = self.table.rowCount()
        self.table.insertRow(row)
        vals = [d['mac'], d['status'], d['expiry'], d['ping'], d.get('url', '')]

        for i, v in enumerate(vals):
            it = QTableWidgetItem(str(v))
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if i == 1:
                if d['status'] == "Online":
                    it.setForeground(QColor("#3fb950"))
                elif "Blokiran" in d['status']:
                    it.setForeground(QColor("#d29922"))
                else:
                    it.setForeground(QColor("#da3633"))
            self.table.setItem(row, i, it)

# --- GLAVNA APLIKACIJA ---
class BalkanFusionApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fusion IPTV List Balkan Checker v3")
        self.resize(1450, 850)
        self.current_selected_list = None
        self.channel_cache = {}
        self.group_cache = {}
        self.stalker_window = None
        self.stream_threads = []
        self.bulk_stream_queue = []
        self.bulk_stream_active = 0
        self.bulk_stream_total = 0
        self.bulk_stream_done = 0
        self.bulk_stream_max_parallel = 3
        self.best_candidates_only = False
        self.settings = self.load_settings_data()
        self.setup_ui()
        self.apply_settings()
        self.setStyleSheet(STYLE_SHEET)

    def load_settings_data(self):
        if not os.path.exists(SETTINGS_FILE):
            return {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.exception("Ne mogu ucitati settings.json: %s", e)
            return {}

    def save_settings(self):
        data = {
            "threads": self.spin_threads.value(),
            "proxy": self.txt_proxy.text(),
            "user_agent": self.combo_ua.currentText(),
            "player_win": self.txt_player_win.text(),
            "player_lin": self.txt_player_lin.text(),
            "auto_switch": self.chk_auto_switch.isChecked(),
            "filter_text": self.txt_result_filter.text(),
            "filter_status": self.combo_filter_status.currentIndex(),
            "filter_balkan": self.combo_filter_balkan.currentIndex(),
            "max_ping": self.spin_filter_ping.value(),
            "filter_expiry": self.combo_filter_expiry.currentIndex()
        }
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logging.exception("Ne mogu spremiti settings.json: %s", e)

    def apply_settings(self):
        if not self.settings:
            return
        self.spin_threads.setValue(int(self.settings.get("threads", self.spin_threads.value())))
        self.txt_proxy.setText(self.settings.get("proxy", ""))
        self.combo_ua.setCurrentText(self.settings.get("user_agent", self.combo_ua.currentText()))
        self.txt_player_win.setText(self.settings.get("player_win", ""))
        self.txt_player_lin.setText(self.settings.get("player_lin", self.txt_player_lin.text()))
        self.chk_auto_switch.setChecked(bool(self.settings.get("auto_switch", True)))
        self.txt_result_filter.setText(self.settings.get("filter_text", ""))
        self.combo_filter_status.setCurrentIndex(int(self.settings.get("filter_status", 0)))
        self.combo_filter_balkan.setCurrentIndex(int(self.settings.get("filter_balkan", 0)))
        self.spin_filter_ping.setValue(int(self.settings.get("max_ping", 0)))
        self.combo_filter_expiry.setCurrentIndex(int(self.settings.get("filter_expiry", 0)))
        self.apply_result_filters()

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    def save_settings_clicked(self, *args):
        self.save_settings()
        QMessageBox.information(self, "Postavke", "Postavke su spremljene.")

    def load_log_view(self, *args):
        if not os.path.exists(LOG_FILE):
            self.txt_log_view.setPlainText("Log je prazan.")
            return
        try:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-120:]
            self.txt_log_view.setPlainText("".join(lines))
        except Exception as e:
            self.txt_log_view.setPlainText(f"Ne mogu ucitati log: {e}")

    def clear_log_view(self, *args):
        try:
            open(LOG_FILE, "w", encoding="utf-8").close()
            self.txt_log_view.clear()
        except Exception as e:
            QMessageBox.warning(self, "Log", f"Ne mogu ocistiti log: {e}")

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setHandleWidth(8)

        # SIDEBAR
        sidebar = QFrame()
        sidebar.setObjectName("SideBar")
        side_v = QVBoxLayout(sidebar)

        lbl = QLabel("FUSION PRO")
        lbl.setStyleSheet("color: #58a6ff; font-weight: 900; font-size: 18px; margin: 25px 15px;")
        side_v.addWidget(lbl)

        self.nav_btns = [
            QPushButton("🏠 Skener"),
            QPushButton("📊 Rezultati"),
            QPushButton("📺 Uređivač Sadržaja"),
            QPushButton("🚀 Globalni Alati"),
            QPushButton("🛡️ Trezor (Baza)"),
            QPushButton("⚙️ Postavke")
        ]

        for i, b in enumerate(self.nav_btns):
            b.setObjectName("MenuBtn")
            b.clicked.connect(lambda checked, idx=i: self.stack.setCurrentIndex(idx))
            side_v.addWidget(b)

        side_v.addWidget(QLabel(""))
        self.btn_stalker = QPushButton("🕵️ MAC / Stalker Alat")
        self.btn_stalker.setStyleSheet("background-color: #21262d; color: #58a6ff; font-weight: bold; border-radius: 8px; padding: 12px; margin: 10px;")
        self.btn_stalker.clicked.connect(self.open_stalker_window)
        side_v.addWidget(self.btn_stalker)

        side_v.addStretch()
        self.main_splitter.addWidget(sidebar)

        self.stack = QStackedWidget()

        # PAGE 1: SKENER
        pg_dash = QWidget()
        dash_v = QVBoxLayout(pg_dash)

        top_h = QHBoxLayout()
        btn_load = QPushButton("📂 Učitaj liste")
        btn_load.clicked.connect(self.load_file)
        btn_clear = QPushButton("🗑️ Očisti sve")
        btn_clear.clicked.connect(self.clear_all)
        top_h.addWidget(btn_load)
        top_h.addStretch()
        top_h.addWidget(btn_clear)
        dash_v.addLayout(top_h)

        cards = QHBoxLayout()
        self.card_total = StatCard("Ukupno Linija", "#8b949e")
        self.card_online = StatCard("Online Portali", "#3fb950")
        self.card_exyu = StatCard("Balkan Pronađen", "#58a6ff")
        cards.addWidget(self.card_total)
        cards.addWidget(self.card_online)
        cards.addWidget(self.card_exyu)
        dash_v.addLayout(cards)

        self.input_area = QTextEdit()
        self.input_area.setPlaceholderText("Ovdje zalijepi M3U linkove ili MAC adrese...")
        dash_v.addWidget(self.input_area)

        self.btn_start = QPushButton("POKRENI PRECIZNI SKENER")
        self.btn_start.setObjectName("ActionBtn")
        self.btn_start.clicked.connect(self.toggle_scan)
        dash_v.addWidget(self.btn_start)

        self.stack.addWidget(pg_dash)

        # PAGE 2: REZULTATI
        pg_res = QWidget()
        res_v = QVBoxLayout(pg_res)

        tool_h = QHBoxLayout()
        btn_export_txt = QPushButton("💾 Exportaj Balkan (TXT)")
        btn_export_txt.clicked.connect(self.export_txt)
        self.combo_export_region = QComboBox()
        self.combo_export_region.addItems(["Sve Ex-YU", "HR", "SRB", "BIH", "SLO", "MKD", "CG", "SPORT"])
        btn_export_region = QPushButton("💾 Export M3U po regiji")
        btn_export_region.clicked.connect(self.export_region_m3u)
        btn_rem_off = QPushButton("🗑️ Ukloni Offline")
        btn_rem_off.clicked.connect(self.remove_offline)
        btn_rem_non_exyu = QPushButton("🗑️ Ukloni bez Balkana")
        btn_rem_non_exyu.clicked.connect(self.remove_non_balkan)
        btn_remove_duplicates = QPushButton("🧹 Ukloni duplikate")
        btn_remove_duplicates.setToolTip("Ukloni duple server/user/pass redove i zadrži najbolji rezultat.")
        btn_remove_duplicates.clicked.connect(self.remove_duplicate_results)
        btn_remove_failed_tests = QPushButton("🗑️ Ukloni neispravne")
        btn_remove_failed_tests.setToolTip("Ukloni liste koje su pale na stream testiranju programa.")
        btn_remove_failed_tests.clicked.connect(self.remove_failed_stream_tests)
        btn_remove_marked = QPushButton("🗑️ Obriši označene")
        btn_remove_marked.setToolTip("Obriši sve redove označene checkboxom. Ako nema checkbox oznaka, briše selektirane redove.")
        btn_remove_marked.clicked.connect(self.remove_marked_rows)
        btn_clear_results = QPushButton("🗑️ Očisti sve")
        btn_clear_results.clicked.connect(self.clear_all)
        self.btn_best_candidates = QPushButton("⭐ Najbolji kandidati")
        self.btn_best_candidates.setCheckable(True)
        self.btn_best_candidates.clicked.connect(self.toggle_best_candidates)
        btn_random_all_test = QPushButton("🎲 Provjeri random streamove u svim listama")
        btn_random_all_test.setToolTip("Za svaku listu prvo proba Ex-YU streamove; ako ih nema, proba ostale random streamove.")
        btn_random_all_test.clicked.connect(self.run_all_stream_checks)

        tool_h.addWidget(btn_export_txt)
        tool_h.addWidget(self.combo_export_region)
        tool_h.addWidget(btn_export_region)
        tool_h.addWidget(btn_rem_off)
        tool_h.addWidget(btn_rem_non_exyu)
        tool_h.addWidget(btn_remove_duplicates)
        tool_h.addWidget(btn_remove_failed_tests)
        tool_h.addWidget(btn_remove_marked)
        tool_h.addWidget(btn_clear_results)
        tool_h.addWidget(self.btn_best_candidates)
        tool_h.addWidget(btn_random_all_test)
        tool_h.addStretch()
        res_v.addLayout(tool_h)

        filter_h = QHBoxLayout()
        self.txt_result_filter = QLineEdit()
        self.txt_result_filter.setPlaceholderText("Pretraži server, korisnika, Ex-Yu info ili EPG...")
        self.txt_result_filter.textChanged.connect(self.apply_result_filters)

        self.combo_filter_status = QComboBox()
        self.combo_filter_status.addItems(["Svi statusi", "Samo Online", "Samo Offline"])
        self.combo_filter_status.currentIndexChanged.connect(self.apply_result_filters)

        self.combo_filter_balkan = QComboBox()
        self.combo_filter_balkan.addItems(["Sav sadržaj", "Samo Balkan/Ex-Yu", "Bez Balkana"])
        self.combo_filter_balkan.currentIndexChanged.connect(self.apply_result_filters)

        self.spin_filter_ping = QSpinBox()
        self.spin_filter_ping.setRange(0, 10000)
        self.spin_filter_ping.setSuffix(" ms max")
        self.spin_filter_ping.setSpecialValueText("Ping: bilo koji")
        self.spin_filter_ping.valueChanged.connect(self.apply_result_filters)

        self.combo_filter_expiry = QComboBox()
        self.combo_filter_expiry.addItems(["Svi rokovi", "Ističe ≤ 7 dana", "Ističe ≤ 30 dana", "Unlimited", "Isteklo"])
        self.combo_filter_expiry.currentIndexChanged.connect(self.apply_result_filters)

        btn_clear_filters = QPushButton("Očisti filtere")
        btn_clear_filters.clicked.connect(self.clear_result_filters)

        filter_h.addWidget(self.txt_result_filter)
        filter_h.addWidget(self.combo_filter_status)
        filter_h.addWidget(self.combo_filter_balkan)
        filter_h.addWidget(self.spin_filter_ping)
        filter_h.addWidget(self.combo_filter_expiry)
        filter_h.addWidget(btn_clear_filters)
        res_v.addLayout(filter_h)

        self.table = QTableWidget(0, 14)
        self.table.setHorizontalHeaderLabels(["M3U Link", "Server", "Korisnik", "Lozinka", "Status", "Ex-Yu Info", "Sadržaj (L|V|S)", "Ističe", "Veze", "Ping", "EPG Link", "Ocjena", "Stream Test", "Odaberi"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.table_menu)
        self.table.cellClicked.connect(self.open_exyu_from_result_click)
        res_v.addWidget(self.table)

        self.stack.addWidget(pg_res)

        # PAGE 3: UREĐIVAČ SADRŽAJA
        pg_chan = QWidget()
        chan_layout = QVBoxLayout(pg_chan)
        self.chan_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.chan_splitter.setHandleWidth(8)

        left_widget = QWidget()
        v1 = QVBoxLayout(left_widget)
        v1.setContentsMargins(0,0,0,0)

        self.combo_content_type = QComboBox()
        self.combo_content_type.addItems(["📺 Live TV Kanali", "🎬 Filmovi (VOD)", "🍿 Serije"])
        self.combo_content_type.setStyleSheet("padding: 8px; font-weight: bold; margin-bottom: 10px;")
        self.combo_content_type.currentIndexChanged.connect(self.reload_groups_for_type)
        v1.addWidget(self.combo_content_type)

        self.txt_filter_groups = QLineEdit()
        self.txt_filter_groups.setPlaceholderText("🔍 Pretraži grupe...")
        self.txt_filter_groups.textChanged.connect(self.filter_groups)
        v1.addWidget(self.txt_filter_groups)

        h_grp_btns = QHBoxLayout()
        btn_chk_all_grp = QPushButton("☑ Označi vidljive")
        btn_chk_none_grp = QPushButton("☐ Odznači vidljive")
        btn_chk_all_grp.clicked.connect(lambda: self.toggle_all_groups(True))
        btn_chk_none_grp.clicked.connect(lambda: self.toggle_all_groups(False))
        h_grp_btns.addWidget(btn_chk_all_grp)
        h_grp_btns.addWidget(btn_chk_none_grp)
        v1.addLayout(h_grp_btns)

        v1.addWidget(QLabel("📂 Kategorije:"))
        self.group_list = QListWidget()
        self.group_list.itemClicked.connect(self.preview_channels)
        self.group_list.itemChanged.connect(self.on_group_checked)
        v1.addWidget(self.group_list)
        self.chan_splitter.addWidget(left_widget)

        right_widget = QWidget()
        v2 = QVBoxLayout(right_widget)
        v2.setContentsMargins(0,0,0,0)

        self.txt_filter_channels = QLineEdit()
        self.txt_filter_channels.setPlaceholderText("🔍 Pretraži kanale...")
        self.txt_filter_channels.textChanged.connect(self.filter_channels)
        v2.addWidget(self.txt_filter_channels)

        h_chn_btns = QHBoxLayout()
        btn_chk_all_chn = QPushButton("☑ Označi vidljive")
        btn_chk_none_chn = QPushButton("☐ Odznači vidljive")
        btn_chk_all_chn.clicked.connect(lambda: self.toggle_all_channels(True))
        btn_chk_none_chn.clicked.connect(lambda: self.toggle_all_channels(False))
        h_chn_btns.addWidget(btn_chk_all_chn)
        h_chn_btns.addWidget(btn_chk_none_chn)
        v2.addLayout(h_chn_btns)

        v2.addWidget(QLabel("📺 Popis sadržaja (Dvoklik za reprodukciju):"))
        self.info_panel = QFrame()
        self.info_panel.setFixedHeight(60)
        self.info_panel.setStyleSheet("background-color: #21262d; border-radius: 8px; padding: 5px;")
        h_info = QHBoxLayout(self.info_panel)

        self.lbl_logo = QLabel("Nema Logotipa")
        self.lbl_logo.setFixedSize(50, 50)
        self.lbl_epg_now = QLabel("Trenutno: Nepoznato")

        h_info.addWidget(self.lbl_logo)
        h_info.addWidget(self.lbl_epg_now)
        h_info.addStretch()
        v2.addWidget(self.info_panel)

        self.chan_list = QListWidget()
        self.chan_list.itemChanged.connect(self.on_channel_checked)
        self.chan_list.itemDoubleClicked.connect(self.play_from_double_click)
        self.chan_list.itemClicked.connect(self.load_visual_epg)
        self.chan_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chan_list.customContextMenuRequested.connect(self.channel_menu)
        v2.addWidget(self.chan_list)

        btn_exp_m3u = QPushButton("💾 EXPORT ODABRANIH U M3U")
        btn_exp_m3u.setObjectName("ActionBtn")
        btn_exp_m3u.clicked.connect(self.export_m3u)
        v2.addWidget(btn_exp_m3u)

        self.chan_splitter.addWidget(right_widget)
        self.chan_splitter.setSizes([400, 800])
        chan_layout.addWidget(self.chan_splitter)

        self.stack.addWidget(pg_chan)

        # PAGE 4: GLOBALNI ALATI
        pg_tools = QWidget()
        tools_v = QVBoxLayout(pg_tools)

        tools_v.addWidget(QLabel("🚀 Super-Lista (Skeniraj sve online servere za određeni kanal)"))
        h_super = QHBoxLayout()
        self.txt_super_search = QLineEdit()
        self.txt_super_search.setPlaceholderText("Upiši pojam (npr. Arena Sport)")
        btn_super_run = QPushButton("🔍 Pretraži sve servere")
        btn_super_run.clicked.connect(self.run_super_search)
        h_super.addWidget(self.txt_super_search)
        h_super.addWidget(btn_super_run)
        tools_v.addLayout(h_super)

        self.lbl_super_status = QLabel("Status: Spreman")
        tools_v.addWidget(self.lbl_super_status)

        self.txt_super_filter = QLineEdit()
        self.txt_super_filter.setPlaceholderText("🔍 Filtriraj pronađene kanale...")
        self.txt_super_filter.textChanged.connect(self.filter_super_table)
        tools_v.addWidget(self.txt_super_filter)

        self.super_table = QTableWidget(0, 3)
        self.super_table.setHorizontalHeaderLabels(["Kanal (Odaberi)", "Pronađeno na Serveru", "Izvorni Link (URL)"])
        self.super_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.super_table.horizontalHeader().setStretchLastSection(True)
        self.super_table.setSortingEnabled(True)
        tools_v.addWidget(self.super_table)

        h_super_exp = QHBoxLayout()
        h_super_exp.addWidget(QLabel("Naziv nove liste:"))
        self.txt_super_name = QLineEdit()
        self.txt_super_name.setText("MojaSuperLista")
        self.txt_super_name.setFixedWidth(200)
        h_super_exp.addWidget(self.txt_super_name)

        self.chk_smart_merge = QCheckBox("✨ Smart Merge (Ukloni duplikate, zadrži najbrži ping)")
        self.chk_smart_merge.setChecked(True)
        h_super_exp.addWidget(self.chk_smart_merge)

        btn_select_all = QPushButton("☑ Označi vidljive")
        btn_select_all.clicked.connect(lambda: self.toggle_super_check(True))
        btn_unselect_all = QPushButton("☐ Odznači vidljive")
        btn_unselect_all.clicked.connect(lambda: self.toggle_super_check(False))
        btn_super_export = QPushButton("💾 EXPORTAJ ODABRANE")
        btn_super_export.setObjectName("ActionBtn")
        btn_super_export.clicked.connect(self.export_super_list)

        h_super_exp.addWidget(btn_select_all)
        h_super_exp.addWidget(btn_unselect_all)
        h_super_exp.addStretch()
        h_super_exp.addWidget(btn_super_export)
        tools_v.addLayout(h_super_exp)

        self.stack.addWidget(pg_tools)

        # PAGE 5: TREZOR (Ažurirano s pravim M3U Linkom)
        pg_vault = QWidget()
        vault_v = QVBoxLayout(pg_vault)
        h_v_top = QHBoxLayout()
        h_v_top.addWidget(QLabel("🛡️ Osobni Trezor - Ovdje spremate liste koje želite sačuvati zauvijek."))
        btn_v_refresh = QPushButton("🔄 Osvježi Trezor")
        btn_v_refresh.clicked.connect(self.load_vault)
        btn_v_rescan = QPushButton("Ponovno provjeri")
        btn_v_rescan.clicked.connect(self.rescan_vault)
        btn_v_rescan_bad = QPushButton("Provjeri loše")
        btn_v_rescan_bad.clicked.connect(self.rescan_bad_vault)
        btn_v_export = QPushButton("Export JSON")
        btn_v_export.clicked.connect(self.export_vault_json)
        btn_v_import = QPushButton("Import JSON")
        btn_v_import.clicked.connect(self.import_vault_json)
        h_v_top.addStretch()
        h_v_top.addWidget(btn_v_refresh)
        h_v_top.addWidget(btn_v_rescan)
        h_v_top.addWidget(btn_v_rescan_bad)
        h_v_top.addWidget(btn_v_export)
        h_v_top.addWidget(btn_v_import)
        vault_v.addLayout(h_v_top)

        self.vault_table = QTableWidget(0, 8)
        self.vault_table.setHorizontalHeaderLabels(["Server", "Korisnik", "Lozinka", "Bilješke", "Ističe", "Ex-Yu", "Zadnja Provjera", "Cijeli M3U Link"])
        self.vault_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.vault_table.horizontalHeader().setStretchLastSection(True)
        self.vault_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.vault_table.customContextMenuRequested.connect(self.vault_menu)
        vault_v.addWidget(self.vault_table)

        self.stack.addWidget(pg_vault)

        # PAGE 6: POSTAVKE
        pg_set = QWidget()
        set_v = QVBoxLayout(pg_set)

        net_grp = QGroupBox("Mrežne Postavke (Skeniranje)")
        net_v = QVBoxLayout(net_grp)
        h_thr = QHBoxLayout()
        h_thr.addWidget(QLabel("Brzina Skeniranja (Broj niti):"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 50)
        self.spin_threads.setValue(10)
        h_thr.addWidget(self.spin_threads)
        h_thr.addStretch()
        net_v.addLayout(h_thr)

        h_prx = QHBoxLayout()
        h_prx.addWidget(QLabel("Proxy (npr. http://ip:port):"))
        self.txt_proxy = QLineEdit()
        self.txt_proxy.setPlaceholderText("Ostavite prazno za direktnu vezu, ili unesite listu iz Scrapera...")
        btn_scrape = QPushButton("Skeniraj besplatne Proxyje")
        btn_scrape.clicked.connect(self.scrape_proxies)
        h_prx.addWidget(self.txt_proxy)
        h_prx.addWidget(btn_scrape)
        net_v.addLayout(h_prx)

        h_ua = QHBoxLayout()
        h_ua.addWidget(QLabel("User-Agent (Emulacija uređaja):"))
        self.combo_ua = QComboBox()
        self.combo_ua.addItems(["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "VLC/3.0.11 LibVLC/3.0.11", "IPTVSmartersPro", "SmartTV"])
        self.combo_ua.setEditable(True)
        h_ua.addWidget(self.combo_ua)
        net_v.addLayout(h_ua)
        set_v.addWidget(net_grp)

        play_grp = QGroupBox("Postavke Playera")
        play_v = QVBoxLayout(play_grp)
        h_win = QHBoxLayout()
        h_win.addWidget(QLabel("Windows (.exe):"))
        self.txt_player_win = QLineEdit()
        self.txt_player_win.setPlaceholderText("C:/Program Files/VideoLAN/VLC/vlc.exe")
        btn_browse_win = QPushButton("Pronađi")
        btn_browse_win.clicked.connect(lambda: self.browse_player(self.txt_player_win, "Executable (*.exe)"))
        h_win.addWidget(self.txt_player_win)
        h_win.addWidget(btn_browse_win)
        play_v.addLayout(h_win)

        h_lin = QHBoxLayout()
        h_lin.addWidget(QLabel("Linux (Bin/Flatpak):"))
        self.txt_player_lin = QLineEdit()
        self.txt_player_lin.setText("/usr/bin/vlc")
        btn_browse_lin = QPushButton("Pronađi")
        btn_browse_lin.clicked.connect(lambda: self.browse_player(self.txt_player_lin, "All Files (*)"))
        h_lin.addWidget(self.txt_player_lin)
        h_lin.addWidget(btn_browse_lin)
        play_v.addLayout(h_lin)
        set_v.addWidget(play_grp)

        self.chk_auto_switch = QCheckBox("Automatski prebaci na Rezultate pri pokretanju")
        self.chk_auto_switch.setChecked(True)
        set_v.addWidget(self.chk_auto_switch)

        btn_save_settings = QPushButton("Spremi postavke")
        btn_save_settings.setObjectName("ActionBtn")
        btn_save_settings.clicked.connect(self.save_settings_clicked)
        set_v.addWidget(btn_save_settings)

        log_grp = QGroupBox("Dijagnostika")
        log_v = QVBoxLayout(log_grp)
        log_btns = QHBoxLayout()
        btn_refresh_log = QPushButton("Osvježi log")
        btn_refresh_log.clicked.connect(self.load_log_view)
        btn_clear_log = QPushButton("Očisti log")
        btn_clear_log.clicked.connect(self.clear_log_view)
        log_btns.addWidget(btn_refresh_log)
        log_btns.addWidget(btn_clear_log)
        log_btns.addStretch()
        log_v.addLayout(log_btns)
        self.txt_log_view = QTextEdit()
        self.txt_log_view.setReadOnly(True)
        self.txt_log_view.setFixedHeight(140)
        log_v.addWidget(self.txt_log_view)
        set_v.addWidget(log_grp)

        set_v.addStretch()

        self.stack.addWidget(pg_set)

        self.main_splitter.addWidget(self.stack)
        self.main_splitter.setSizes([220, 1230])
        self.main_splitter.setCollapsible(0, False)
        root_layout.addWidget(self.main_splitter)

        bottom_panel = QFrame()
        bottom_panel.setStyleSheet("background-color: #161b22; border-top: 1px solid #30363d;")
        bottom_lay = QVBoxLayout(bottom_panel)
        bottom_lay.setContentsMargins(15, 10, 15, 10)
        self.pbar = QProgressBar()
        self.pbar.setValue(0)
        self.pbar.setFormat("Spreman")
        bottom_lay.addWidget(self.pbar)
        root_layout.addWidget(bottom_panel)

        self.load_vault()

    # --- NOVE FUNKCIJE ZA BRISANJE I STATISTIKU ---
    def remove_offline(self, *args):
        for r in range(self.table.rowCount() - 1, -1, -1):
            item = self.table.item(r, 4)
            if item and item.text() != "Online":
                self.table.removeRow(r)
        self.update_stats()

    def remove_non_balkan(self, *args):
        for r in range(self.table.rowCount() - 1, -1, -1):
            item = self.table.item(r, 5)
            if item and item.text() == "NE":
                self.table.removeRow(r)
        self.update_stats()

    def remove_duplicate_results(self, *args):
        if self.table.rowCount() <= 1:
            return QMessageBox.information(self, "Duplikati", "Nema dovoljno rezultata za provjeru duplikata.")

        best_by_key = {}
        remove_rows = set()
        for row in range(self.table.rowCount()):
            key = self.row_duplicate_key(row)
            if not key:
                continue

            if key not in best_by_key:
                best_by_key[key] = row
                continue

            current_best = best_by_key[key]
            if self.duplicate_row_score(row) > self.duplicate_row_score(current_best):
                remove_rows.add(current_best)
                best_by_key[key] = row
            else:
                remove_rows.add(row)

        if not remove_rows:
            return QMessageBox.information(self, "Duplikati", "Nisu pronađeni duplikati.")

        for row in sorted(remove_rows, reverse=True):
            self.table.removeRow(row)

        self.update_stats()
        self.apply_result_filters()
        self.pbar.setFormat(f"Uklonjeno duplikata: {len(remove_rows)}")

    def remove_failed_stream_tests(self, *args):
        rows = []
        for row in range(self.table.rowCount()):
            stream_text = self.table.item(row, 12).text() if self.table.item(row, 12) else ""
            if self.stream_status_is_failed_test(stream_text):
                rows.append(row)

        if not rows:
            return QMessageBox.information(self, "Stream test", "Nema lista označenih kao neispravne nakon testiranja.")

        reply = QMessageBox.question(
            self,
            "Ukloni neispravne",
            f"Ukloniti {len(rows)} lista koje su pale na testiranju?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for row in sorted(rows, reverse=True):
            self.table.removeRow(row)

        self.update_stats()
        self.apply_result_filters()
        self.pbar.setFormat(f"Uklonjeno neispravnih lista: {len(rows)}")

    def row_duplicate_key(self, row):
        server = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
        user = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
        pw = self.table.item(row, 3).text() if self.table.item(row, 3) else ""
        if not server or not user or not pw:
            return ""
        return self.result_signature(server, user, pw)

    def duplicate_row_score(self, row):
        status = self.table.item(row, 4).text() if self.table.item(row, 4) else ""
        exyu = self.table.item(row, 5).text() if self.table.item(row, 5) else ""
        expiry = self.table.item(row, 7).text() if self.table.item(row, 7) else ""
        ping = self.parse_ping_ms(self.table.item(row, 9).text() if self.table.item(row, 9) else "")
        grade = self.table.item(row, 11).text() if self.table.item(row, 11) else ""
        stream = self.table.item(row, 12).text() if self.table.item(row, 12) else ""

        score = 0
        if status == "Online":
            score += 10000
        if self.is_balkan_text(exyu):
            score += 3000
        score += {"A": 500, "B": 350, "C": 150, "D": 0}.get(grade, 0)
        if self.stream_status_is_positive(stream):
            score += 300
        elif self.stream_status_is_negative(stream):
            score -= 300
        if expiry == "Unlimited":
            score += 200
        elif not self.is_expired(expiry):
            score += 100
        if ping is not None:
            score += max(0, 1000 - min(ping, 1000))
        return score

    def remove_marked_rows(self, *args):
        rows = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                rows.append(r)

        if not rows:
            rows = sorted({index.row() for index in self.table.selectedIndexes()})

        if not rows:
            return QMessageBox.information(self, "Brisanje", "Nema označenih ili selektiranih redova za brisanje.")

        reply = QMessageBox.question(
            self,
            "Brisanje",
            f"Obrisati {len(rows)} označenih redova?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for r in sorted(rows, reverse=True):
            if 0 <= r < self.table.rowCount():
                self.table.removeRow(r)
        self.update_stats()
        self.apply_result_filters()

    def remove_single_row(self, row):
        self.table.removeRow(row)
        self.update_stats()

    def update_stats(self):
        self.card_total.lbl_val.setText(str(self.table.rowCount()))
        online_c = 0
        exyu_c = 0
        for r in range(self.table.rowCount()):
            st = self.table.item(r, 4)
            ex = self.table.item(r, 5)
            if st and st.text() == "Online":
                online_c += 1
            if ex and (ex.text().startswith("DA") or ex.text() == "STALKER"):
                exyu_c += 1
        self.card_online.lbl_val.setText(str(online_c))
        self.card_exyu.lbl_val.setText(str(exyu_c))

    def parse_ping_ms(self, text):
        try:
            return int(re.findall(r"\d+", text)[0])
        except Exception:
            return None

    def parse_expiry_date(self, text):
        clean = str(text or "").strip()
        if clean.lower() in ("", "n/a", "none", "null"):
            return None
        if clean.lower() == "unlimited":
            return "unlimited"
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", clean)
        if not m:
            return None
        day, month, year = map(int, m.groups())
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None

    def days_until_expiry(self, text):
        expiry = self.parse_expiry_date(text)
        if expiry in (None, "unlimited"):
            return None
        return (expiry - datetime.now().date()).days

    def is_expiring_within(self, text, days):
        left = self.days_until_expiry(text)
        return left is not None and 0 <= left <= days

    def is_expired(self, text):
        left = self.days_until_expiry(text)
        return left is not None and left < 0

    def is_balkan_text(self, exyu):
        return str(exyu or "").startswith("DA") or str(exyu or "") == "STALKER"

    def is_connection_full(self, text):
        match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(text or ""))
        if not match:
            return False
        active, maximum = map(int, match.groups())
        return maximum > 0 and active >= maximum

    def stream_status_is_positive(self, text):
        normalized = str(text or "").strip().lower()
        return normalized.startswith("lista ispravna: da") or normalized.startswith("radi ")

    def stream_status_is_negative(self, text):
        normalized = str(text or "").strip().lower()
        return normalized.startswith("lista ispravna: ne") or normalized.startswith("ne radi")

    def stream_status_is_failed_test(self, text):
        normalized = str(text or "").strip().lower()
        if not normalized or normalized == "nije testirano" or normalized.endswith("test..."):
            return False
        if self.stream_status_is_negative(normalized):
            return True
        failed_markers = (
            "greška",
            "greska",
            "timeout",
            "nema test kanala",
            "nema ex-yu",
            "nema exyu",
            "nema kanala",
            "ne radi",
        )
        return any(marker in normalized for marker in failed_markers)

    def is_best_candidate_row(self, row):
        status = self.table.item(row, 4).text() if self.table.item(row, 4) else ""
        exyu = self.table.item(row, 5).text() if self.table.item(row, 5) else ""
        expiry = self.table.item(row, 7).text() if self.table.item(row, 7) else ""
        grade = self.table.item(row, 11).text() if self.table.item(row, 11) else ""
        stream = self.table.item(row, 12).text() if self.table.item(row, 12) else ""
        return (
            status == "Online"
            and self.is_balkan_text(exyu)
            and grade in ("A", "B")
            and not self.is_expired(expiry)
            and self.stream_status_is_positive(stream)
        )

    def result_signature(self, server, user, pw):
        return f"{server.strip().lower()}|{user.strip()}|{pw.strip()}"

    def is_duplicate_result(self, server, user, pw):
        sig = self.result_signature(server, user, pw)
        for r in range(self.table.rowCount()):
            srv = self.table.item(r, 1).text() if self.table.item(r, 1) else ""
            usr = self.table.item(r, 2).text() if self.table.item(r, 2) else ""
            pwd = self.table.item(r, 3).text() if self.table.item(r, 3) else ""
            if self.result_signature(srv, usr, pwd) == sig:
                return True
        return False

    def quality_grade(self, d, stream_status="Nije testirano"):
        if d.get("status") != "Online":
            return "D"

        score = 0
        ping = self.parse_ping_ms(d.get("ping", ""))
        expiry = d.get("expiry", "")
        conns = str(d.get("conns", ""))

        score += 25
        if self.is_balkan_text(d.get("exyu", "")):
            score += 20
        if ping is not None:
            if ping <= 300:
                score += 20
            elif ping <= 800:
                score += 12
            else:
                score += 5
        if expiry == "Unlimited":
            score += 20
        elif self.is_expiring_within(expiry, 7) or self.is_expired(expiry):
            score += 2
        elif self.is_expiring_within(expiry, 30):
            score += 8
        else:
            score += 15
        if re.match(r"^\d+/\d+$", conns):
            active, maximum = map(int, conns.split("/"))
            if maximum > 0 and active < maximum:
                score += 10
        if self.stream_status_is_positive(stream_status):
            score += 10
        elif self.stream_status_is_negative(stream_status):
            score -= 15

        if score >= 75:
            return "A"
        if score >= 55:
            return "B"
        if score >= 35:
            return "C"
        return "D"

    def update_row_quality(self, row):
        if row < 0 or row >= self.table.rowCount():
            return
        d = {
            "status": self.table.item(row, 4).text() if self.table.item(row, 4) else "",
            "exyu": self.table.item(row, 5).text() if self.table.item(row, 5) else "",
            "expiry": self.table.item(row, 7).text() if self.table.item(row, 7) else "",
            "conns": self.table.item(row, 8).text() if self.table.item(row, 8) else "",
            "ping": self.table.item(row, 9).text() if self.table.item(row, 9) else ""
        }
        stream_status = self.table.item(row, 12).text() if self.table.item(row, 12) else "Nije testirano"
        if self.table.item(row, 11):
            grade = self.quality_grade(d, stream_status)
            self.table.item(row, 11).setText(grade)
            colors = {"A": "#3fb950", "B": "#58a6ff", "C": "#d29922", "D": "#da3633"}
            self.table.item(row, 11).setForeground(QColor(colors.get(grade, "#c9d1d9")))

    def toggle_best_candidates(self, checked):
        self.best_candidates_only = bool(checked)
        self.apply_result_filters()

    def apply_result_filters(self, *args):
        if not hasattr(self, "table"):
            return

        q = self.txt_result_filter.text().strip().lower()
        status_mode = self.combo_filter_status.currentIndex()
        balkan_mode = self.combo_filter_balkan.currentIndex()
        max_ping = self.spin_filter_ping.value()
        expiry_mode = self.combo_filter_expiry.currentIndex()
        for r in range(self.table.rowCount()):
            row_text = " ".join(
                self.table.item(r, c).text().lower()
                for c in range(self.table.columnCount())
                if self.table.item(r, c)
            )
            status = self.table.item(r, 4).text() if self.table.item(r, 4) else ""
            exyu = self.table.item(r, 5).text() if self.table.item(r, 5) else ""
            expiry = self.table.item(r, 7).text() if self.table.item(r, 7) else ""
            ping_text = self.table.item(r, 9).text() if self.table.item(r, 9) else ""
            stream_text = self.table.item(r, 12).text() if self.table.item(r, 12) else ""
            ping_ms = self.parse_ping_ms(ping_text)

            visible = True
            if q and q not in row_text:
                visible = False
            if status_mode == 1 and status != "Online":
                visible = False
            if status_mode == 2 and status == "Online":
                visible = False
            if balkan_mode == 1 and not (exyu.startswith("DA") or exyu == "STALKER"):
                visible = False
            if balkan_mode == 2 and (exyu.startswith("DA") or exyu == "STALKER"):
                visible = False
            if max_ping > 0 and (ping_ms is None or ping_ms > max_ping):
                visible = False
            if expiry_mode == 1 and not self.is_expiring_within(expiry, 7):
                visible = False
            if expiry_mode == 2 and not self.is_expiring_within(expiry, 30):
                visible = False
            if expiry_mode == 3 and expiry != "Unlimited":
                visible = False
            if expiry_mode == 4 and not self.is_expired(expiry):
                visible = False
            if self.best_candidates_only and not self.is_best_candidate_row(r):
                visible = False

            self.table.setRowHidden(r, not visible)

    def clear_result_filters(self, *args):
        self.txt_result_filter.clear()
        self.combo_filter_status.setCurrentIndex(0)
        self.combo_filter_balkan.setCurrentIndex(0)
        self.spin_filter_ping.setValue(0)
        self.combo_filter_expiry.setCurrentIndex(0)
        self.best_candidates_only = False
        self.btn_best_candidates.setChecked(False)
        self.apply_result_filters()

    # --- GLAVNE SKENER FUNKCIJE ---
    def open_stalker_window(self):
        if self.stalker_window is None:
            self.stalker_window = StalkerWindow()
        self.stalker_window.show()
        self.stalker_window.activateWindow()

    def scrape_proxies(self):
        self.txt_proxy.setText("Prikupljam proxyje... Molimo pričekajte.")
        self.proxy_thread = ProxyScraperThread()
        self.proxy_thread.result_ready.connect(lambda r: self.txt_proxy.setText(r))
        self.proxy_thread.start()

    def canonical_input_key(self, url):
        scanner = IPTVScanner()
        base = scanner.extract_base_url(url)
        user = re.search(r'username=([^&\s]+)', url, flags=re.IGNORECASE)
        pw = re.search(r'password=([^&\s]+)', url, flags=re.IGNORECASE)
        mac = re.search(r'mac=([0-9a-fA-F:]+)', url, flags=re.IGNORECASE)
        if user and pw:
            return f"xtream|{base.lower()}|{user.group(1)}|{pw.group(1)}"
        if mac:
            return f"mac|{base.lower()}|{mac.group(1).upper()}"
        return url.strip().lower()

    def toggle_scan(self, *args):
        if hasattr(self, 'worker') and self.worker.isRunning():
            self.worker.is_running = False
            self.btn_start.setText("POKRENI PRECIZNI SKENER")
            self.btn_start.setObjectName("ActionBtn")
            self.setStyleSheet(STYLE_SHEET)
        else:
            raw_text = self.input_area.toPlainText()

            # SPAJA PORTAL I MAC AKO SU ZALIJEPLJENI S RAZMAKOM
            raw_text = re.sub(r'(https?://[^\s]+(?:/c/|/c))\s+([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})', r'\1?mac=\2', raw_text)

            urls = re.findall(r'(https?://[^\s]+)', raw_text)
            unique_urls = []
            seen_keys = set()
            for u in urls:
                if 'username=' not in u.lower() and 'mac=' not in u.lower():
                    continue
                key = self.canonical_input_key(u)
                if key in seen_keys:
                    logging.info("Preskocen ulazni duplikat: %s", key)
                    continue
                seen_keys.add(key)
                unique_urls.append(u)

            if not unique_urls:
                self.pbar.setValue(0)
                self.pbar.setFormat("Nema prepoznatih M3U/MAC linkova")
                QMessageBox.warning(
                    self,
                    "Skener",
                    "Nisam pronašao link za provjeru. Zalijepi M3U/Xtream link s username/password parametrima ili MAC portal link."
                )
                return

            self.card_total.lbl_val.setText(str(len(unique_urls)))
            self.table.setSortingEnabled(False)
            self.table.setRowCount(0)
            self.card_online.lbl_val.setText("0")
            self.card_exyu.lbl_val.setText("0")
            self.pbar.setValue(0)
            self.pbar.setFormat(f"Skeniram 0/{len(unique_urls)}")

            if self.chk_auto_switch.isChecked():
                self.stack.setCurrentIndex(1)

            self.btn_start.setText("PREKINI SKENIRANJE")
            self.btn_start.setObjectName("StopBtn")
            self.setStyleSheet(STYLE_SHEET)

            proxy_input = self.txt_proxy.text().strip()
            p_list = proxy_input.splitlines() if proxy_input else None
            ua = self.combo_ua.currentText().strip()

            self.worker = ScannerThread(unique_urls, max_threads=self.spin_threads.value(), proxy_list=p_list, user_agent=ua)
            self.worker.result_ready.connect(self.add_res)
            self.worker.progress_update.connect(self.pbar.setValue)
            self.worker.finished_signal.connect(self.scan_finished)
            self.worker.start()

    def scan_finished(self):
        self.table.setSortingEnabled(True)
        self.btn_start.setText("POKRENI PRECIZNI SKENER")
        self.btn_start.setObjectName("ActionBtn")
        self.setStyleSheet(STYLE_SHEET)
        self.pbar.setValue(100 if self.table.rowCount() else 0)
        self.pbar.setFormat(f"Završeno: {self.table.rowCount()} rezultata")

    def add_res(self, d):
        if self.is_duplicate_result(d.get("server", ""), d.get("user", ""), d.get("pass", "")):
            logging.info("Preskocen duplikat: %s | %s", d.get("server", ""), d.get("user", ""))
            return

        row = self.table.rowCount()
        self.table.insertRow(row)

        # Generiranje punog M3U linka
        if d['pass'].upper() == "MAC":
            full_url = f"{d.get('url', d['server'])}"
        else:
            full_url = f"{d['server']}/get.php?username={d['user']}&password={d['pass']}&type=m3u_plus&output=ts"

        stream_status = "Nije testirano"
        quality = self.quality_grade(d, stream_status)
        vals = [full_url, d['server'], d['user'], d['pass'], d['status'], d['exyu'], d['ch_count'], d['expiry'], d['conns'], d['ping'], d['epg_link'], quality, stream_status]

        bg = QColor("#2b1111")
        if d['status'] == "Online":
            bg = QColor("#1f2e1f") if d['exyu'].startswith("DA") or d['exyu'] == "STALKER" else QColor("#2e2e1f")

        for i, v in enumerate(vals):
            it = NumericTableWidgetItem(str(v)) if i in [7, 9] else QTableWidgetItem(str(v))
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it.setBackground(bg)
            if i == 0:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Unchecked)

            # Spremi URL za Trezor na 1. element (server)
            if i == 1 and 'url' in d:
                it.setData(Qt.ItemDataRole.UserRole, d['url'])

            self.table.setItem(row, i, it)

            if i == 4 and d['status'] == "Online":
                it.setForeground(QColor("#3fb950"))
            if i == 4 and d['status'] == "Offline":
                it.setForeground(QColor("#da3633"))
            if i == 5 and (d['exyu'].startswith("DA") or d['exyu'] == "STALKER"):
                it.setForeground(QColor("#58a6ff"))
            if i == 8 and self.is_connection_full(str(v)):
                it.setForeground(QColor("#da3633"))
                it.setToolTip("Sve dozvoljene konekcije su zauzete. API može biti online, ali stream može vraćati 401.")
            if i == 11:
                colors = {"A": "#3fb950", "B": "#58a6ff", "C": "#d29922", "D": "#da3633"}
                it.setForeground(QColor(colors.get(str(v), "#c9d1d9")))

        if d['status'] == "Online":
             self.card_online.lbl_val.setText(str(int(self.card_online.lbl_val.text()) + 1))
             if d['exyu'].startswith("DA") or d['exyu'] == "STALKER":
                 self.card_exyu.lbl_val.setText(str(int(self.card_exyu.lbl_val.text()) + 1))
        self.apply_result_filters()
        self.pbar.setFormat(f"Pronađeno rezultata: {self.table.rowCount()}")
    # --- POMOĆNE ---
    def load_file(self, *args):
        paths, _ = QFileDialog.getOpenFileNames(self, "Otvori liste", "", "Liste (*.txt *.m3u);;Sve datoteke (*)")
        if not paths:
            return

        contents = []
        failed = []
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    contents.append(f.read().strip())
            except Exception as e:
                failed.append(f"{os.path.basename(path)}: {e}")

        if contents:
            self.input_area.setPlainText("\n".join(text for text in contents if text))
            self.pbar.setValue(0)
            self.pbar.setFormat(f"Učitano datoteka: {len(contents)}")

        if failed:
            QMessageBox.warning(self, "Učitavanje", "Neke datoteke nisu učitane:\n" + "\n".join(failed[:8]))

    def clear_all(self, *args):
        self.input_area.clear()
        self.table.setRowCount(0)
        self.group_list.clear()
        self.chan_list.clear()
        self.channel_cache = {}
        self.group_cache = {}
        self.current_selected_list = None
        if hasattr(self, "txt_result_filter"):
            self.txt_result_filter.clear()
        if hasattr(self, "combo_filter_status"):
            self.combo_filter_status.setCurrentIndex(0)
        if hasattr(self, "combo_filter_balkan"):
            self.combo_filter_balkan.setCurrentIndex(0)
        if hasattr(self, "combo_filter_expiry"):
            self.combo_filter_expiry.setCurrentIndex(0)
        if hasattr(self, "spin_filter_ping"):
            self.spin_filter_ping.setValue(0)
        if hasattr(self, "btn_best_candidates"):
            self.btn_best_candidates.setChecked(False)
            self.best_candidates_only = False
        self.pbar.setValue(0)
        self.pbar.setFormat("Očišćeno")
        self.card_total.lbl_val.setText("0")
        self.card_online.lbl_val.setText("0")
        self.card_exyu.lbl_val.setText("0")

    def export_txt(self, *args):
        path, _ = QFileDialog.getSaveFileName(self, "Spremi Balkan", "balkan.txt", "Text (*.txt)")
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 5).text().startswith("DA"):
                    m3u = self.table.item(r, 0).text()
                    srv = self.table.item(r, 1).text()
                    usr = self.table.item(r, 2).text()
                    pwd = self.table.item(r, 3).text()
                    exp = self.table.item(r, 7).text()
                    conn = self.table.item(r, 8).text()
                    epg = self.table.item(r, 10).text()

                    f.write(f"Server: {srv}\n"
                            f"User: {usr}\n"
                            f"Pass: {pwd}\n"
                            f"Ističe: {exp}\n"
                            f"Konekcije: {conn}\n"
                            f"M3U Link: {m3u}\n"
                            f"EPG Link: {epg}\n"
                            f"---\n")

    def export_region_m3u(self, *args):
        region = self.combo_export_region.currentText()
        filename = f"{region.lower().replace(' ', '_').replace('-', '')}.m3u"
        path, _ = QFileDialog.getSaveFileName(self, "Export M3U po regiji", filename, "M3U (*.m3u)")
        if not path:
            return

        count = 0
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for r in range(self.table.rowCount()):
                exyu = self.table.item(r, 5).text() if self.table.item(r, 5) else ""
                if not self.is_balkan_text(exyu):
                    continue
                if region != "Sve Ex-YU" and f"{region}:" not in exyu:
                    continue
                name = self.table.item(r, 1).text() if self.table.item(r, 1) else "Lista"
                m3u = self.table.item(r, 0).text() if self.table.item(r, 0) else ""
                if m3u:
                    f.write(f"#EXTINF:-1,{name}\n{m3u}\n")
                    count += 1

        QMessageBox.information(self, "Export", f"Exportirano {count} listi.")

    # --- REZULTATI TABLICA ---
    def table_menu(self, pos):
        row = self.table.currentRow()
        if row < 0:
            row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            return

        m = QMenu()
        m.addAction("▶️ Pokreni cijelu listu u Playeru").triggered.connect(lambda: self.play_full_list(row))
        m.addAction("🔍 Učitaj Sadržaj").triggered.connect(lambda: self.init_load_groups())
        m.addAction("🛡️ Spremi u Trezor").triggered.connect(lambda: self.add_to_vault(row))
        m.addAction("🔗 Kopiraj M3U Link").triggered.connect(lambda: QApplication.clipboard().setText(self.table.item(row, 0).text()))
        m.addAction("🔗 Kopiraj Server").triggered.connect(lambda: QApplication.clipboard().setText(self.table.item(row, 1).text()))
        m.addAction("❌ Ukloni ovaj link").triggered.connect(lambda: self.remove_single_row(row))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def play_full_list(self, row):
        item = self.table.item(row, 0)
        if not item:
            return

        url = item.text()
        p_path = self.txt_player_win.text().strip() if sys.platform.startswith('win') else self.txt_player_lin.text().strip()

        if not p_path:
            return QMessageBox.warning(self, "Greška", "Podesite putanju do Playera u postavkama!")

        try:
            if not sys.platform.startswith('win') and " " in p_path:
                 cmd = p_path.split()
                 cmd.append(url)
                 subprocess.Popen(cmd)
            else:
                 subprocess.Popen([p_path, url])
        except Exception as e:
            QMessageBox.critical(self, "Greška", f"Ne mogu pokrenuti player: {str(e)}")

    def run_all_stream_checks(self, *args):
        if self.bulk_stream_queue or self.bulk_stream_active:
            return QMessageBox.information(self, "Stream test", "Provjera svih lista je već pokrenuta.")

        jobs = []
        for row in range(self.table.rowCount()):
            status = self.table.item(row, 4).text() if self.table.item(row, 4) else ""
            password = self.table.item(row, 3).text() if self.table.item(row, 3) else ""
            exyu = self.table.item(row, 5).text() if self.table.item(row, 5) else ""
            if status != "Online" or password.upper() == "MAC" or exyu == "STALKER":
                continue
            jobs.append((row, "smart_random", None))

        if not jobs:
            return QMessageBox.information(self, "Stream test", "Nema online Xtream lista za provjeru.")

        self.bulk_stream_queue = jobs
        self.bulk_stream_active = 0
        self.bulk_stream_done = 0
        self.bulk_stream_total = len(jobs)
        self.pbar.setValue(0)
        self.pbar.setFormat(f"Random stream test 0/{self.bulk_stream_total}")
        self.start_next_bulk_stream_checks()

    def start_next_bulk_stream_checks(self):
        while self.bulk_stream_queue and self.bulk_stream_active < self.bulk_stream_max_parallel:
            row, mode, region = self.bulk_stream_queue.pop(0)
            if row >= self.table.rowCount():
                self.bulk_stream_done += 1
                continue
            label = "Random stream test..."
            self.table.item(row, 12).setText(label)
            self.start_stream_check_thread(row, mode=mode, region=region, bulk=True)

        self.update_bulk_stream_progress()

    def update_bulk_stream_progress(self):
        if self.bulk_stream_total:
            value = int((self.bulk_stream_done / self.bulk_stream_total) * 100)
            self.pbar.setValue(value)
            self.pbar.setFormat(f"Random stream test {self.bulk_stream_done}/{self.bulk_stream_total}")

    def start_stream_check_thread(self, row, mode="first", region=None, bulk=False):
        srv = self.table.item(row, 1).text()
        u = self.table.item(row, 2).text()
        p = self.table.item(row, 3).text()
        sample_size = 6
        thread = StreamCheckThread(
            srv,
            u,
            p,
            mode=mode,
            sample_size=sample_size,
            region=region,
            user_agent=self.combo_ua.currentText().strip()
        )
        if bulk:
            self.bulk_stream_active += 1
        thread.result_ready.connect(lambda res, target_row=row, th=thread, is_bulk=bulk: self.finish_stream_check(target_row, res, th, is_bulk))
        self.stream_threads.append(thread)
        thread.start()

    def finish_stream_check(self, row, result, thread=None, bulk=False):
        if row < self.table.rowCount() and self.table.item(row, 12):
            self.table.item(row, 12).setText(result)
            self.update_row_quality(row)
            self.apply_result_filters()
        if thread and thread in self.stream_threads:
            self.stream_threads.remove(thread)
        if bulk:
            self.bulk_stream_active = max(0, self.bulk_stream_active - 1)
            self.bulk_stream_done += 1
            self.update_bulk_stream_progress()
            if self.bulk_stream_queue:
                self.start_next_bulk_stream_checks()
            elif self.bulk_stream_active == 0:
                self.pbar.setValue(100)
                self.pbar.setFormat(f"Random stream test završen: {self.bulk_stream_done}/{self.bulk_stream_total}")

    # --- TREZOR S PRAVIM M3U LINKOM ---
    def add_to_vault(self, row):
        srv = self.table.item(row, 1).text()
        u = self.table.item(row, 2).text()
        p = self.table.item(row, 3).text()
        st = self.table.item(row, 4).text()
        ex = self.table.item(row, 5).text()
        exp = self.table.item(row, 7).text()
        full_m3u_link = self.table.item(row, 0).text()

        note, ok = QInputDialog.getText(self, "Trezor", "Unesi kratku bilješku za ovu listu (opcionalno):")
        if not ok:
            return

        conn = sqlite3.connect("fusion_vault.db")
        c = conn.cursor()
        duplicate = c.execute(
            "SELECT 1 FROM vault WHERE lower(server)=lower(?) AND user=? AND pass=? LIMIT 1",
            (srv, u, p)
        ).fetchone()
        if duplicate:
            conn.close()
            return QMessageBox.information(self, "Trezor", "Ova lista je već spremljena u Trezor.")
        c.execute("INSERT INTO vault (server, user, pass, status, exyu, expiry, notes, last_checked, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (srv, u, p, st, ex, exp, note, time.strftime("%Y-%m-%d"), full_m3u_link))
        conn.commit()
        conn.close()

        QMessageBox.information(self, "Trezor", "Lista uspješno spremljena u Trezor s cjelokupnim M3U linkom!")
        self.load_vault()

    def load_vault(self):
        self.vault_table.setRowCount(0)
        conn = sqlite3.connect("fusion_vault.db")
        c = conn.cursor()

        try:
            c.execute("SELECT id, server, user, pass, notes, expiry, exyu, last_checked, url FROM vault")
        except sqlite3.OperationalError:
            c.execute("SELECT id, server, user, pass, notes, expiry, exyu, last_checked FROM vault")

        for row_data in c.fetchall():
            row = self.vault_table.rowCount()
            self.vault_table.insertRow(row)
            for i, val in enumerate(row_data[1:]):
                it = QTableWidgetItem(str(val))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.vault_table.setItem(row, i, it)
                if i == 0:
                    it.setData(Qt.ItemDataRole.UserRole, row_data[0])
        conn.close()

    def vault_menu(self, pos):
        row = self.vault_table.currentRow()
        if row < 0:
            return

        m = QMenu()
        m.addAction("📋 Kopiraj Cijeli M3U Link").triggered.connect(lambda: QApplication.clipboard().setText(self.vault_table.item(row, 7).text()))
        m.addAction("🗑️ Obriši iz Trezora").triggered.connect(lambda: self.delete_from_vault(row))
        m.exec(self.vault_table.viewport().mapToGlobal(pos))

    def delete_from_vault(self, row):
        item = self.vault_table.item(row, 0)
        if not item:
            return

        v_id = item.data(Qt.ItemDataRole.UserRole)
        conn = sqlite3.connect("fusion_vault.db")
        c = conn.cursor()
        c.execute("DELETE FROM vault WHERE id=?", (v_id,))
        conn.commit()
        conn.close()
        self.load_vault()

    def export_vault_json(self, *args):
        path, _ = QFileDialog.getSaveFileName(self, "Export Trezora", "fusion_vault_export.json", "JSON (*.json)")
        if not path:
            return
        try:
            conn = sqlite3.connect("fusion_vault.db")
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT server, user, pass, status, exyu, expiry, notes, last_checked, url FROM vault").fetchall()
            conn.close()
            with open(path, "w", encoding="utf-8") as f:
                json.dump([dict(row) for row in rows], f, indent=2, ensure_ascii=False)
            QMessageBox.information(self, "Trezor", f"Exportirano {len(rows)} zapisa.")
        except Exception as e:
            logging.exception("Export Trezora nije uspio: %s", e)
            QMessageBox.critical(self, "Trezor", f"Export nije uspio: {e}")

    def import_vault_json(self, *args):
        path, _ = QFileDialog.getOpenFileName(self, "Import Trezora", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            if not isinstance(rows, list):
                raise ValueError("JSON mora biti lista zapisa.")

            conn = sqlite3.connect("fusion_vault.db")
            c = conn.cursor()
            count = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                c.execute(
                    "INSERT INTO vault (server, user, pass, status, exyu, expiry, notes, last_checked, url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.get("server", ""),
                        row.get("user", ""),
                        row.get("pass", ""),
                        row.get("status", ""),
                        row.get("exyu", ""),
                        row.get("expiry", ""),
                        row.get("notes", ""),
                        row.get("last_checked", ""),
                        row.get("url", "")
                    )
                )
                count += 1
            conn.commit()
            conn.close()
            self.load_vault()
            QMessageBox.information(self, "Trezor", f"Importirano {count} zapisa.")
        except Exception as e:
            logging.exception("Import Trezora nije uspio: %s", e)
            QMessageBox.critical(self, "Trezor", f"Import nije uspio: {e}")

    def rescan_vault(self, *args):
        if hasattr(self, "vault_worker") and self.vault_worker.isRunning():
            return QMessageBox.information(self, "Trezor", "Provjera Trezora je vec pokrenuta.")

        conn = sqlite3.connect("fusion_vault.db")
        rows = conn.execute("SELECT id, url FROM vault WHERE url IS NOT NULL AND url != ''").fetchall()
        conn.close()

        self.start_vault_rescan(rows, "Nema spremljenih M3U linkova za provjeru.")

    def rescan_bad_vault(self, *args):
        if hasattr(self, "vault_worker") and self.vault_worker.isRunning():
            return QMessageBox.information(self, "Trezor", "Provjera Trezora je vec pokrenuta.")

        conn = sqlite3.connect("fusion_vault.db")
        rows = conn.execute(
            "SELECT id, url, status, exyu, expiry FROM vault WHERE url IS NOT NULL AND url != ''"
        ).fetchall()
        conn.close()

        bad_rows = []
        for v_id, url, status, exyu, expiry in rows:
            if status != "Online" or not self.is_balkan_text(exyu) or self.is_expired(expiry) or self.is_expiring_within(expiry, 30):
                bad_rows.append((v_id, url))

        self.start_vault_rescan(bad_rows, "Nema loših zapisa za ponovnu provjeru.")

    def start_vault_rescan(self, rows, empty_message):
        if not rows:
            return QMessageBox.information(self, "Trezor", empty_message)

        self.vault_rescan_ids_by_url = {url: v_id for v_id, url in rows}
        urls = list(self.vault_rescan_ids_by_url.keys())
        self.pbar.setValue(0)
        self.pbar.setFormat(f"Provjeravam Trezor 0/{len(urls)}")

        proxy_input = self.txt_proxy.text().strip()
        p_list = proxy_input.splitlines() if proxy_input else None
        ua = self.combo_ua.currentText().strip()

        self.vault_worker = ScannerThread(urls, max_threads=self.spin_threads.value(), proxy_list=p_list, user_agent=ua)
        self.vault_worker.result_ready.connect(self.update_vault_from_scan)
        self.vault_worker.progress_update.connect(self.pbar.setValue)
        self.vault_worker.finished_signal.connect(self.vault_rescan_finished)
        self.vault_worker.start()

    def update_vault_from_scan(self, d):
        if d.get("pass", "").upper() == "MAC":
            full_url = d.get("url", d.get("server", ""))
        else:
            full_url = f"{d['server']}/get.php?username={d['user']}&password={d['pass']}&type=m3u_plus&output=ts"

        v_id = self.vault_rescan_ids_by_url.get(full_url)
        if not v_id:
            for url, row_id in self.vault_rescan_ids_by_url.items():
                if d.get("server", "") in url and d.get("user", "") in url:
                    v_id = row_id
                    break
        if not v_id:
            logging.warning("Nije pronaden Trezor zapis za rescan rezultat: %s", full_url)
            return

        conn = sqlite3.connect("fusion_vault.db")
        c = conn.cursor()
        c.execute(
            "UPDATE vault SET server=?, user=?, pass=?, status=?, exyu=?, expiry=?, last_checked=?, url=? WHERE id=?",
            (
                d.get("server", ""),
                d.get("user", ""),
                d.get("pass", ""),
                d.get("status", ""),
                d.get("exyu", ""),
                d.get("expiry", ""),
                time.strftime("%Y-%m-%d"),
                full_url,
                v_id
            )
        )
        conn.commit()
        conn.close()
        self.pbar.setFormat("Trezor se ažurira...")

    def vault_rescan_finished(self):
        self.load_vault()
        self.pbar.setValue(100)
        self.pbar.setFormat("Provjera Trezora završena")
        QMessageBox.information(self, "Trezor", "Ponovna provjera Trezora je zavrsena.")

    # --- UREĐIVAČ SADRŽAJA & CHECKBOX LOGIKA ---
    def get_action_names(self):
        sel = self.combo_content_type.currentIndex()
        cat_act = ["get_live_categories", "get_vod_categories", "get_series_categories"][sel]
        str_act = ["get_live_streams", "get_vod_streams", "get_series"][sel]
        prefix = ["live", "vod", "series"][sel]
        return cat_act, str_act, prefix

    def open_exyu_from_result_click(self, row, column):
        if column != 5:
            return

        item = self.table.item(row, column)
        if not item or not self.is_balkan_text(item.text()):
            return

        self.table.setCurrentCell(row, column)
        self.init_load_groups(show_balkan_program=True)

    def init_load_groups(self, *args, show_balkan_program=False):
        row = self.table.currentRow()
        if row < 0:
            return

        if self.table.item(row, 4).text() != "Online":
            return QMessageBox.warning(self, "Greška", "Samo aktivni serveri.")

        # Dodana zaštita: Uređivač radi samo s Xtream API-jem, ne s MAC portalima
        if self.table.item(row, 3).text().upper() == "MAC" or self.table.item(row, 5).text() == "STALKER":
            return QMessageBox.warning(self, "Greška", "Uređivač sadržaja trenutno podržava samo Xtream portale (User/Pass). MAC portali nisu podržani za uređivanje.")

        self.current_selected_list = {
            "server": self.table.item(row, 1).text(),
            "user": self.table.item(row, 2).text(),
            "pass": self.table.item(row, 3).text(),
            "row": row
        }
        self.channel_cache = {}
        self.group_cache = {}
        self.stack.setCurrentIndex(2)
        QApplication.processEvents()
        if show_balkan_program and self.combo_content_type.currentIndex() != 0:
            self.combo_content_type.blockSignals(True)
            self.combo_content_type.setCurrentIndex(0)
            self.combo_content_type.blockSignals(False)
        self.reload_groups_for_type()
        if show_balkan_program:
            self.show_first_balkan_program()

    def show_first_balkan_program(self):
        scanner = IPTVScanner()
        ranked_groups = []
        for index in range(self.group_list.count()):
            item = self.group_list.item(index)
            if not item:
                continue
            score = sum(scanner.score_text_for_balkan(item.text(), source="category").values())
            if score > 0:
                ranked_groups.append((score, index))

        if ranked_groups:
            ranked_groups.sort(reverse=True)
            group_item = self.group_list.item(ranked_groups[0][1])
        else:
            group_item = QListWidgetItem("Svi kanali")
            group_item.setFlags(group_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            group_item.setCheckState(Qt.CheckState.Unchecked)
            group_item.setData(Qt.ItemDataRole.UserRole, "")
            self.group_list.addItem(group_item)

        self.group_list.setCurrentItem(group_item)
        self.group_list.scrollToItem(group_item)
        self.preview_channels(group_item)

        selected_channel = None
        for index in range(self.chan_list.count()):
            channel_item = self.chan_list.item(index)
            if not channel_item:
                continue
            stats = scanner.score_text_for_balkan(channel_item.text(), source="stream")
            if scanner.is_balkan_detected(stats):
                selected_channel = channel_item
                break

        if selected_channel is None and self.chan_list.count() > 0:
            selected_channel = self.chan_list.item(0)

        if selected_channel:
            self.chan_list.setCurrentItem(selected_channel)
            self.chan_list.scrollToItem(selected_channel)
            self.pbar.setFormat(f"Prikazan Ex-Yu program: {selected_channel.text()}")
        else:
            self.pbar.setFormat("Ex-Yu program nije pronađen u dostupnim Live TV kanalima")

    def reload_groups_for_type(self, *args):
        if not self.current_selected_list:
            return

        self.group_list.clear()
        self.chan_list.clear()
        self.txt_filter_groups.clear()
        self.txt_filter_channels.clear()

        cat_act, _, prefix = self.get_action_names()

        if prefix not in self.group_cache:
            try:
                url = f"{self.current_selected_list['server']}/player_api.php?username={self.current_selected_list['user']}&password={self.current_selected_list['pass']}&action={cat_act}"

                # Dodajemo User-Agent iz postavki da izbjegnemo blokadu (403 Forbidden)
                ua = self.combo_ua.currentText().strip()
                headers = {"User-Agent": ua} if ua else {"User-Agent": "Mozilla/5.0"}

                with httpx.Client(verify=False, headers=headers) as client:
                    resp = client.get(url, timeout=15.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        self.group_cache[prefix] = data if isinstance(data, list) else []
                    else:
                        self.group_cache[prefix] = []
            except Exception as e:
                print(f"Greška kod učitavanja grupa: {e}")
                self.group_cache[prefix] = []

        self.group_list.blockSignals(True)
        groups = self.group_cache[prefix]
        self.mark_current_list_balkan_from_categories(groups)
        if not groups:
            fallback_names = {
                "live": "Svi kanali",
                "vod": "Svi filmovi",
                "series": "Sve serije"
            }
            groups = [{"category_name": fallback_names.get(prefix, "Sav sadržaj"), "category_id": ""}]

        for c in groups:
            if not isinstance(c, dict):
                continue
            name = c.get('category_name', 'Nepoznato')
            it = QListWidgetItem(name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, c.get('category_id', ''))
            self.group_list.addItem(it)
        self.group_list.blockSignals(False)
        self.mark_current_list_balkan_from_group_items()

    def preview_channels(self, item):
        cat_id = item.data(Qt.ItemDataRole.UserRole)
        _, str_act, prefix = self.get_action_names()
        cache_key = f"{prefix}_{cat_id}"

        self.chan_list.blockSignals(True)
        self.chan_list.clear()
        self.txt_filter_channels.clear()

        if cache_key not in self.channel_cache:
            try:
                url = f"{self.current_selected_list['server']}/player_api.php?username={self.current_selected_list['user']}&password={self.current_selected_list['pass']}&action={str_act}"
                if cat_id not in (None, ""):
                    url += f"&category_id={cat_id}"

                # Dodajemo User-Agent iz postavki
                ua = self.combo_ua.currentText().strip()
                headers = {"User-Agent": ua} if ua else {"User-Agent": "Mozilla/5.0"}

                with httpx.Client(verify=False, headers=headers) as client:
                    resp = client.get(url, timeout=15.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        streams = data if isinstance(data, list) else []
                    else:
                        streams = []

                for s in streams:
                    if isinstance(s, dict):
                        s['my_checked'] = item.checkState() == Qt.CheckState.Checked
                self.channel_cache[cache_key] = streams
                self.mark_current_list_balkan_from_streams(streams, item.text())
            except Exception as e:
                print(f"Greška kod učitavanja kanala: {e}")
                self.channel_cache[cache_key] = []

        self.mark_current_list_balkan_from_streams(self.channel_cache[cache_key], item.text())

        for s in self.channel_cache[cache_key]:
            if not isinstance(s, dict):
                continue
            name = s.get('name', s.get('title', 'Nepoznato'))
            s_id = s.get('stream_id', s.get('series_id', 0))
            it = QListWidgetItem(name)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if s.get('my_checked', False) else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, {"id": s_id, "icon": s.get('stream_icon', '')})
            self.chan_list.addItem(it)

        self.chan_list.blockSignals(False)

    def balkan_label_from_stats(self, stats):
        scanner = IPTVScanner()
        if not scanner.is_balkan_detected(stats):
            return ""
        details = [f"{k}:{v}" for k, v in stats.items() if v > 0]
        return f"DA ({', '.join(details)})" if details else "DA"

    def mark_current_list_balkan_from_categories(self, groups):
        scanner = IPTVScanner()
        stats = scanner.detect_balkan_from_categories(groups)
        label = self.balkan_label_from_stats(stats)
        if label:
            self.update_current_result_balkan(label)

    def mark_current_list_balkan_from_streams(self, streams, category_name=""):
        if not isinstance(streams, list):
            return
        scanner = IPTVScanner()
        stats = {k: 0 for k in scanner.balkan_signals.keys()}
        for stream in streams[:1500]:
            if not isinstance(stream, dict):
                continue
            text = " ".join([
                str(stream.get("name", "")),
                str(stream.get("title", "")),
                str(stream.get("epg_channel_id", "")),
                str(stream.get("category_name", "")),
                str(category_name or "")
            ])
            scanner.merge_stats(stats, scanner.score_text_for_balkan(text, source="stream"))
            if scanner.is_balkan_detected(stats):
                break

        label = self.balkan_label_from_stats(stats)
        if label:
            self.update_current_result_balkan(label)

    def update_current_result_balkan(self, label):
        if not self.current_selected_list:
            return

        row = self.current_selected_list.get("row", -1)
        if (
            row < 0
            or row >= self.table.rowCount()
            or self.result_signature(
                self.table.item(row, 1).text() if self.table.item(row, 1) else "",
                self.table.item(row, 2).text() if self.table.item(row, 2) else "",
                self.table.item(row, 3).text() if self.table.item(row, 3) else ""
            ) != self.result_signature(
                self.current_selected_list.get("server", ""),
                self.current_selected_list.get("user", ""),
                self.current_selected_list.get("pass", "")
            )
        ):
            row = self.find_result_row(
                self.current_selected_list.get("server", ""),
                self.current_selected_list.get("user", ""),
                self.current_selected_list.get("pass", "")
            )
        if row < 0:
            row = self.table.currentRow()
        if row < 0 or row >= self.table.rowCount():
            return

        current = self.table.item(row, 5).text() if self.table.item(row, 5) else ""
        if self.is_balkan_text(current):
            return

        item = self.table.item(row, 5)
        if item is None:
            item = QTableWidgetItem()
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 5, item)
        item.setText(label)
        item.setForeground(QColor("#58a6ff"))

        bg = QColor("#1f2e1f")
        for col in range(self.table.columnCount()):
            cell = self.table.item(row, col)
            if cell:
                cell.setBackground(bg)

        self.update_row_quality(row)
        self.update_stats()
        self.apply_result_filters()

    def find_result_row(self, server, user, pw):
        target = self.result_signature(server, user, pw)
        for row in range(self.table.rowCount()):
            srv = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            usr = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
            pwd = self.table.item(row, 3).text() if self.table.item(row, 3) else ""
            if self.result_signature(srv, usr, pwd) == target:
                return row
        return -1

    def mark_current_list_balkan_from_group_items(self):
        scanner = IPTVScanner()
        stats = {k: 0 for k in scanner.balkan_signals.keys()}
        for i in range(self.group_list.count()):
            item = self.group_list.item(i)
            if not item:
                continue
            scanner.merge_stats(stats, scanner.score_text_for_balkan(item.text(), source="category"))
        label = self.balkan_label_from_stats(stats)
        if label:
            self.update_current_result_balkan(label)

    def toggle_all_groups(self, state_bool):
        state = Qt.CheckState.Checked if state_bool else Qt.CheckState.Unchecked
        for i in range(self.group_list.count()):
            item = self.group_list.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    def toggle_all_channels(self, state_bool):
        state = Qt.CheckState.Checked if state_bool else Qt.CheckState.Unchecked
        for i in range(self.chan_list.count()):
            item = self.chan_list.item(i)
            if not item.isHidden():
                item.setCheckState(state)

    def load_visual_epg(self, item):
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data or not isinstance(data, dict):
            return

        self.lbl_logo.setText("Učitavam...")
        self.lbl_epg_now.setText("Dohvaćam EPG...")
        icon_url = data.get("icon", "")

        if icon_url and icon_url.startswith("http"):
            try:
                import requests
                img_data = requests.get(icon_url, timeout=3).content
                pixmap = QPixmap()
                pixmap.loadFromData(img_data)
                self.lbl_logo.setPixmap(pixmap.scaled(50, 50, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            except:
                self.lbl_logo.setText("Nema\nLogotipa")
        else:
            self.lbl_logo.setText("Nema\nLogotipa")

        if self.combo_content_type.currentIndex() == 0:
            try:
                import requests
                s_id = data.get("id")
                url = f"{self.current_selected_list['server']}/player_api.php?username={self.current_selected_list['user']}&password={self.current_selected_list['pass']}&action=get_short_epg&stream_id={s_id}"
                epg_data = requests.get(url, timeout=4).json()

                if isinstance(epg_data, dict) and 'epg_listings' in epg_data and len(epg_data['epg_listings']) > 0:
                    now_playing = epg_data['epg_listings'][0]
                    title = now_playing.get('title', 'Nepoznato')
                    start = now_playing.get('start', '')
                    self.lbl_epg_now.setText(f"Trenutno emitira:\n{start} - {title}")
                else:
                    self.lbl_epg_now.setText("Nema dostupnog EPG-a za ovaj kanal.")
            except:
                self.lbl_epg_now.setText("Greška pri dohvaćanju EPG-a.")
        else:
            self.lbl_epg_now.setText("EPG je dostupan samo za Live TV.")

    def filter_groups(self):
        q = self.txt_filter_groups.text().lower()
        for i in range(self.group_list.count()):
            item = self.group_list.item(i)
            item.setHidden(q not in item.text().lower())
        self.mark_current_list_balkan_from_group_items()

    def filter_channels(self):
        q = self.txt_filter_channels.text().lower()
        for i in range(self.chan_list.count()):
            item = self.chan_list.item(i)
            item.setHidden(q not in item.text().lower())

    def on_group_checked(self, item):
        cat_id = item.data(Qt.ItemDataRole.UserRole)
        is_checked = item.checkState() == Qt.CheckState.Checked
        _, _, prefix = self.get_action_names()
        cache_key = f"{prefix}_{cat_id}"

        if cache_key in self.channel_cache:
            for s in self.channel_cache[cache_key]:
                if isinstance(s, dict):
                    s['my_checked'] = is_checked

        if self.group_list.currentItem() == item:
            self.chan_list.blockSignals(True)
            for i in range(self.chan_list.count()):
                self.chan_list.item(i).setCheckState(item.checkState())
            self.chan_list.blockSignals(False)

    def on_channel_checked(self, item):
        if not self.group_list.currentItem():
            return

        cat_id = self.group_list.currentItem().data(Qt.ItemDataRole.UserRole)
        _, _, prefix = self.get_action_names()
        cache_key = f"{prefix}_{cat_id}"
        s_data = item.data(Qt.ItemDataRole.UserRole)

        if not isinstance(s_data, dict):
            return

        s_id = s_data.get("id")

        if cache_key in self.channel_cache:
            for s in self.channel_cache[cache_key]:
                if isinstance(s, dict) and s.get('stream_id', s.get('series_id', 0)) == s_id:
                    s['my_checked'] = item.checkState() == Qt.CheckState.Checked

    # --- POKRETANJE PLAYERA ---
    def play_from_double_click(self, item):
        self.trigger_play(item)

    def channel_menu(self, pos):
        item = self.chan_list.itemAt(pos)
        if not item:
            return
        m = QMenu()
        m.addAction("▶️ Pokreni u Playeru").triggered.connect(lambda: self.trigger_play(item))
        m.exec(self.chan_list.viewport().mapToGlobal(pos))

    def trigger_play(self, item):
        if not self.current_selected_list:
            return

        s_data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(s_data, dict):
            return

        s_id = s_data.get("id")
        sel = self.combo_content_type.currentIndex()
        url_type = ["live", "movie", "series"][sel]
        ext = ".mp4" if sel == 1 else ".ts"

        url = f"{self.current_selected_list['server']}/{url_type}/{self.current_selected_list['user']}/{self.current_selected_list['pass']}/{s_id}{ext}"
        p_path = self.txt_player_win.text().strip() if sys.platform.startswith('win') else self.txt_player_lin.text().strip()

        if not p_path:
            return QMessageBox.warning(self, "Greška", "Podesite putanju do Playera u postavkama!")

        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".m3u", mode='w', encoding='utf-8') as tmp:
                tmp.write(f"#EXTM3U\n#EXTINF:-1, Stream\n{url}\n")
                tmp_path = tmp.name

            if not sys.platform.startswith('win') and " " in p_path:
                 cmd = p_path.split()
                 cmd.append(tmp_path)
                 subprocess.Popen(cmd)
            else:
                 subprocess.Popen([p_path, tmp_path])
        except Exception as e:
            QMessageBox.critical(self, "Greška", str(e))

    def browse_player(self, line_edit, filter_str):
        path, _ = QFileDialog.getOpenFileName(self, "Odaberi Player", "/", filter_str)
        if path:
            line_edit.setText(path)

    def export_m3u(self, *args):
        if not self.current_selected_list:
            return

        path, _ = QFileDialog.getSaveFileName(self, "Spremi M3U", "lista.m3u", "M3U (*.m3u)")
        if not path:
            return

        sel = self.combo_content_type.currentIndex()
        ext = ".mp4" if sel == 1 else ".ts"
        url_type = ["live", "movie", "series"][sel]
        _, _, prefix = self.get_action_names()

        count = 0
        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for cache_key, streams in self.channel_cache.items():
                if cache_key.startswith(prefix):
                    for s in streams:
                        if isinstance(s, dict) and s.get('my_checked', False):
                            s_id = s.get('stream_id', s.get('series_id', 0))
                            name = s.get('name', s.get('title', 'Nepoznato'))
                            icon = s.get('stream_icon', '')
                            url = f"{self.current_selected_list['server']}/{url_type}/{self.current_selected_list['user']}/{self.current_selected_list['pass']}/{s_id}{ext}"
                            f.write(f'#EXTINF:-1 tvg-logo="{icon}", {name}\n{url}\n')
                            count += 1

        QMessageBox.information(self, "Uspjeh", f"Spremljeno {count} kanala u M3U listu!")

    # --- GLOBALNI ALATI ---
    def filter_super_table(self):
        q = self.txt_super_filter.text().lower()
        for r in range(self.super_table.rowCount()):
            item = self.super_table.item(r, 0)
            if item:
                self.super_table.setRowHidden(r, q not in item.text().lower())

    def run_super_search(self, *args):
        keyword = self.txt_super_search.text().strip()
        if not keyword:
            return QMessageBox.warning(self, "Greška", "Upiši pojam za pretragu!")

        servers = []
        for r in range(self.table.rowCount()):
            if self.table.item(r, 4).text() == "Online" and self.table.item(r, 5).text() != "STALKER":
                ping_str = self.table.item(r, 9).text().replace("ms", "")
                try:
                    p_val = int(ping_str)
                except:
                    p_val = 999
                servers.append({
                    "server": self.table.item(r, 1).text(),
                    "user": self.table.item(r, 2).text(),
                    "pass": self.table.item(r, 3).text(),
                    "ping": p_val
                })

        if not servers:
            return QMessageBox.warning(self, "Greška", "Nema dostupnih online (Xtream) servera u tablici.")

        self.super_table.setSortingEnabled(False)
        self.super_table.setRowCount(0)
        self.txt_super_filter.clear()

        self.lbl_super_status.setText("Status: Pretražujem... Molimo pričekajte.")
        self.super_thread = SuperSearchThread(servers, keyword)
        self.super_thread.progress.connect(lambda txt: self.lbl_super_status.setText(f"Status: {txt}"))
        self.super_thread.finished.connect(self.super_search_done)
        self.super_thread.start()

    def super_search_done(self, results):
        self.lbl_super_status.setText(f"Status: Završeno! Pronađeno {len(results)} kanala.")
        if not results:
            return QMessageBox.information(self, "Rezultat", "Nije pronađen niti jedan kanal s tim imenom.")

        for ch in results:
            row = self.super_table.rowCount()
            self.super_table.insertRow(row)

            it_name = QTableWidgetItem(ch['name'])
            it_name.setFlags(it_name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it_name.setCheckState(Qt.CheckState.Checked)
            it_name.setData(Qt.ItemDataRole.UserRole, ch['ping'])

            it_server = QTableWidgetItem(ch['server'])
            it_url = QTableWidgetItem(ch['url'])

            self.super_table.setItem(row, 0, it_name)
            self.super_table.setItem(row, 1, it_server)
            self.super_table.setItem(row, 2, it_url)

        self.super_table.setSortingEnabled(True)

    def toggle_super_check(self, state_bool):
        state = Qt.CheckState.Checked if state_bool else Qt.CheckState.Unchecked
        for r in range(self.super_table.rowCount()):
            if not self.super_table.isRowHidden(r):
                item = self.super_table.item(r, 0)
                if item:
                    item.setCheckState(state)

    def export_super_list(self, *args):
        if self.super_table.rowCount() == 0:
            return

        filename = self.txt_super_name.text().strip()
        if not filename:
            filename = "SuperLista"
        if not filename.endswith(".m3u"):
            filename += ".m3u"

        path, _ = QFileDialog.getSaveFileName(self, "Spremi Super-Listu", filename, "M3U (*.m3u)")
        if not path:
            return

        export_list = []
        for r in range(self.super_table.rowCount()):
            it_name = self.super_table.item(r, 0)
            if it_name and it_name.checkState() == Qt.CheckState.Checked:
                url = self.super_table.item(r, 2).text()
                ping = it_name.data(Qt.ItemDataRole.UserRole)
                export_list.append({"name": it_name.text(), "url": url, "ping": ping})

        if self.chk_smart_merge.isChecked():
            merged_dict = {}
            for item in export_list:
                key = item['name'].lower().strip()
                if key not in merged_dict or item['ping'] < merged_dict[key]['ping']:
                    merged_dict[key] = item
            export_list = list(merged_dict.values())

        with open(path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for item in export_list:
                f.write(f"#EXTINF:-1, {item['name']}\n{item['url']}\n")

        QMessageBox.information(self, "Uspjeh", f"Spremljeno {len(export_list)} kanala u {path}!")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = BalkanFusionApp()
    window.show()
    sys.exit(app.exec())
