import asyncio
import httpx
import re
import time
import logging
import unicodedata
from datetime import datetime

class IPTVScanner:
    def __init__(self, timeout=15.0):
        self.timeout = timeout

        # Short codes are weak evidence. Channel/category names are stronger.
        # Broad words like "regional", "lokalni", "arena" and "eurosport" are
        # intentionally excluded because they caused many false Balkan hits.
        self.balkan_signals = {
            "HR": {
                "strong": ["hrvatska", "croatia", "croatian", "hrvatski", "hrt", "doma tv", "rtl hr", "nova tv hr", "maxsport"],
                "weak": ["hr", "hrv", "cro"]
            },
            "SRB": {
                "strong": ["srbija", "serbia", "serbian", "srpski", "rts", "pink", "b92", "happy", "prva sr", "superstar tv"],
                "weak": ["srb"]
            },
            "BIH": {
                "strong": ["bosna", "bosnia", "bosnian", "bih", "hayat", "obn", "bn tv", "ftv bih", "face tv"],
                "weak": []
            },
            "SLO": {
                "strong": ["slovenija", "slovenia", "slovenian", "slovenski", "pop tv", "kanal a"],
                "weak": ["slo"]
            },
            "MKD": {
                "strong": ["makedonija", "macedonia", "macedonian", "sitel", "kanal 5 mk", "telma"],
                "weak": ["mkd"]
            },
            "CG": {
                "strong": ["crna gora", "montenegro", "montenegrin", "rtcg", "vijesti"],
                "weak": ["mne", "cg"]
            },
            "EXYU": {
                "strong": ["ex yu", "exyu", "ex-yu", "balkan", "balkanski", "domaci kanali", "domaci tv"],
                "weak": ["domaci"]
            },
            "SPORT": {
                "strong": ["arena sport hr", "arena sport srb", "arena sport serbia", "sport klub hr", "sport klub srb", "sportklub hr", "sportklub srb"],
                "weak": []
            }
        }

    async def check_portal(self, client, url):
        try:
            base_url = self.extract_base_url(url)
            start_time = time.time()

            user_match = re.search(r'username=([^&]+)', url)
            pass_match = re.search(r'password=([^&]+)', url)
            mac_match = re.search(r'mac=([0-9a-fA-F:]+)', url)

            # --- STALKER PORTAL LOGIKA ---
            if mac_match and not user_match:
                mac = mac_match.group(1).strip().upper()
                headers = {
                    "Cookie": f"mac={mac}",
                    "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
                    "X-User-Agent": "Model: MAG200; Link: WiFi",
                    "Referer": f"{base_url}/c/",
                    "Accept-Language": "en-US,en;q=0.9"
                }
                api_base = base_url.replace('/c', '') + '/server/load.php'
                stalker_url = f"{api_base}?type=stb&action=handshake&JsHttpRequest=1-xml"

                resp = await client.get(stalker_url, headers=headers, timeout=self.timeout, follow_redirects=True)
                ping = f"{int((time.time() - start_time) * 1000)}ms"

                token = ""
                if resp.status_code == 200:
                    try:
                        token = resp.json().get('js', {}).get('token', '') or resp.json().get('token', '')
                    except:
                        if 'token' in resp.text.lower():
                            token = "present"

                if resp.status_code == 200 and token:
                    try:
                        if token != "present":
                            headers["Authorization"] = f"Bearer {token}"
                    except:
                        pass

                    try:
                        prof_resp = await client.get(
                            f"{api_base}?type=stb&action=get_profile&JsHttpRequest=1-xml",
                            headers=headers,
                            timeout=self.timeout,
                            follow_redirects=True
                        )
                        prof_data = prof_resp.json().get('js', {}) if prof_resp.status_code == 200 else {}
                        expiry = "Unlimited"
                        expire_ts = prof_data.get('expire_billing_date')
                        if expire_ts and str(expire_ts) != "0":
                            expiry = datetime.fromtimestamp(int(expire_ts)).strftime('%d.%m.%Y.')
                    except:
                        expiry = "Unlimited"

                    return {
                        "server": base_url, "user": mac, "pass": "MAC",
                        "status": "Online", "ping": ping, "exyu": "STALKER",
                        "expiry": expiry, "conns": "1/1",
                        "ch_count": "MAC Portal", "epg_link": "Stalker API",
                        "url": base_url
                    }
                return None

            # --- XTREAM CODES LOGIKA ---
            if not user_match or not pass_match: return None
            user, pw = user_match.group(1), pass_match.group(1)

            api_url = f"{base_url}/player_api.php?username={user}&password={pw}"

            resp = await client.get(api_url, timeout=self.timeout, follow_redirects=True)
            ping = f"{int((time.time() - start_time) * 1000)}ms"

            try: data = resp.json()
            except ValueError: return None

            user_info = data.get('user_info', {})
            auth_status = str(data.get('auth', 0))
            status_str = str(user_info.get('status', '')).lower()

            if status_str == 'active' or auth_status == '1' or user_info.get('username') == user:
                live_count, vod_count, series_count = 0, 0, 0
                exyu_stats = {k: 0 for k in self.balkan_signals.keys()}
                cats = []

                await asyncio.sleep(0.1)

                # Brzo skeniranje svega
                vod_cats = await self.fetch_json(client, f"{api_url}&action=get_vod_categories", timeout=10.0)
                if isinstance(vod_cats, list):
                    vod_count = len(vod_cats)

                series_cats = await self.fetch_json(client, f"{api_url}&action=get_series_categories", timeout=10.0)
                if isinstance(series_cats, list):
                    series_count = len(series_cats)

                live_cats = await self.fetch_json(client, f"{api_url}&action=get_live_categories", timeout=12.0)
                if isinstance(live_cats, list):
                    cats = live_cats
                    live_count = len(cats)
                    exyu_stats = self.detect_balkan_from_categories(cats)

                if not self.is_balkan_detected(exyu_stats) and self.should_sample_channels(exyu_stats, cats):
                    stream_stats = await self.detect_balkan_from_stream_sample(client, api_url, cats)
                    self.merge_stats(exyu_stats, stream_stats)

                exyu_result = "NE"
                if self.is_balkan_detected(exyu_stats):
                    details = [f"{k}:{v}" for k, v in exyu_stats.items() if v > 0]
                    exyu_result = f"DA ({', '.join(details)})"

                content_info = f"L:{live_count} | V:{vod_count} | S:{series_count}"
                epg_url = user_info.get('xmltv_api_url') or f"{base_url}/xmltv.php?username={user}&password={pw}"

                return {
                    "server": base_url, "user": user, "pass": pw,
                    "status": "Online", "ping": ping, "exyu": exyu_result,
                    "expiry": self.format_date(user_info.get('exp_date')),
                    "conns": f"{user_info.get('active_cons','0')}/{user_info.get('max_connections','1')}",
                    "ch_count": content_info,
                    "epg_link": epg_url,
                    "url": base_url
                }

        except Exception as e:
            logging.warning("Provjera portala nije uspjela: %s | %s", self.safe_url(url), self.describe_error(e))
        return None

    async def fetch_json(self, client, url, timeout=None):
        try:
            resp = await client.get(url, timeout=timeout or self.timeout, follow_redirects=True)
            if resp.status_code != 200:
                logging.info("Xtream API nije vratio 200 za %s: HTTP %s", self.safe_url(url), resp.status_code)
                return None
            return resp.json()
        except ValueError:
            logging.info("Xtream API nije vratio JSON za %s", self.safe_url(url))
        except Exception as e:
            logging.info("Xtream API dohvat nije uspio za %s: %s", self.safe_url(url), self.describe_error(e))
        return None

    def format_date(self, ts):
        if not ts or str(ts).lower() in ["0", "none", "null"]: return "Unlimited"
        try: return datetime.fromtimestamp(int(ts)).strftime('%d.%m.%Y.')
        except: return "N/A"

    def extract_base_url(self, url):
        clean_url = str(url or "").strip()
        base = re.split(r"/(?:get|player_api|xmltv)\.php", clean_url, maxsplit=1)[0]
        base = re.split(r"/c(?:/|\?|$)", base, maxsplit=1)[0]
        return base.rstrip("/")

    def safe_url(self, url):
        text = str(url or "")
        text = re.sub(r"(password=)[^&\s]+", r"\1***", text, flags=re.IGNORECASE)
        return text

    def describe_error(self, error):
        name = error.__class__.__name__
        msg = str(error)
        low = msg.lower()
        if "temporary failure in name resolution" in low or "name resolution" in low:
            return "DNS greska"
        if "timeout" in low:
            return "Timeout"
        if "403" in low or "forbidden" in low:
            return "403 blokada"
        if "connection refused" in low:
            return "Server odbija vezu"
        if "network is unreachable" in low:
            return "Mreza nedostupna"
        return f"{name}: {msg}" if msg else name

    def normalize_text(self, text):
        text = unicodedata.normalize("NFKD", str(text or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def has_phrase(self, normalized_text, phrase):
        phrase = self.normalize_text(phrase)
        return bool(re.search(rf"(^|\s){re.escape(phrase)}($|\s)", normalized_text))

    def score_text_for_balkan(self, text, source="category"):
        normalized = self.normalize_text(text)
        stats = {k: 0 for k in self.balkan_signals.keys()}
        if not normalized:
            return stats

        if self.has_exyu_marker(normalized):
            stats["EXYU"] += 5 if source == "category" else 6

        source_bonus = 1 if source == "stream" else 0
        for country, signals in self.balkan_signals.items():
            for phrase in signals["strong"]:
                if self.has_phrase(normalized, phrase):
                    stats[country] += 3 + source_bonus
            for phrase in signals["weak"]:
                if self.has_phrase(normalized, phrase):
                    stats[country] += 1
        self.score_regional_sports_channel(normalized, stats, source)
        return stats

    def has_exyu_marker(self, normalized):
        return bool(re.search(r"(^|\s)ex\s*yu($|\s)", normalized) or re.search(r"(^|\s)exyu($|\s)", normalized))

    def score_regional_sports_channel(self, normalized, stats, source):
        if source != "stream":
            return
        if not (
            self.has_phrase(normalized, "arena sport")
            or self.has_phrase(normalized, "sport klub")
            or self.has_phrase(normalized, "sportklub")
        ):
            return

        regional_tokens = {
            "HR": ["hr", "hrv", "cro"],
            "SRB": ["sr", "srb"],
            "BIH": ["bih", "ba", "bosna"],
            "SLO": ["slo", "si"],
            "MKD": ["mk", "mkd"],
            "CG": ["cg", "mne"],
        }
        for region, tokens in regional_tokens.items():
            if any(self.has_phrase(normalized, token) for token in tokens):
                stats[region] += 3
                stats["SPORT"] += 1

    def merge_stats(self, base, extra):
        for country, value in extra.items():
            base[country] = base.get(country, 0) + value

    def detect_balkan_from_categories(self, categories):
        stats = {k: 0 for k in self.balkan_signals.keys()}
        if not isinstance(categories, list):
            return stats

        for category in categories:
            if not isinstance(category, dict):
                continue
            name = category.get("category_name", "")
            self.merge_stats(stats, self.score_text_for_balkan(name, source="category"))
        return stats

    def is_balkan_detected(self, stats):
        country_hits = sum(1 for country, score in stats.items() if country != "SPORT" and score >= 2)
        return country_hits > 0 or stats.get("SPORT", 0) >= 4

    def should_sample_channels(self, stats, categories):
        if self.is_balkan_detected(stats):
            return True
        # Category APIs are often incomplete or blocked differently than stream APIs.
        # Sample channels even without category hits to avoid false "NE" results.
        return True

    def likely_balkan_category_ids(self, categories):
        if not isinstance(categories, list):
            return []

        ranked = []
        for category in categories:
            if not isinstance(category, dict):
                continue
            score = sum(self.score_text_for_balkan(category.get("category_name", "")).values())
            if score > 0:
                ranked.append((score, category.get("category_id")))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [cat_id for _, cat_id in ranked[:4] if cat_id not in (None, "")]

    async def detect_balkan_from_stream_sample(self, client, api_url, categories):
        stats = {k: 0 for k in self.balkan_signals.keys()}
        category_ids = self.likely_balkan_category_ids(categories)
        category_names = self.category_name_map(categories)
        urls = [
            f"{api_url}&action=get_live_streams&category_id={cat_id}"
            for cat_id in category_ids
        ]
        stream_limit = 250

        if not urls:
            urls = [f"{api_url}&action=get_live_streams"]
            stream_limit = 1500

        for url in urls[:4]:
            try:
                resp = await client.get(url, timeout=8.0)
                if resp.status_code != 200:
                    continue
                streams = resp.json()
                if not isinstance(streams, list):
                    continue

                for stream in streams[:stream_limit]:
                    if not isinstance(stream, dict):
                        continue
                    text = " ".join([
                        str(stream.get("name", "")),
                        str(stream.get("epg_channel_id", "")),
                        str(stream.get("category_name", "")),
                        category_names.get(str(stream.get("category_id", "")), "")
                    ])
                    self.merge_stats(stats, self.score_text_for_balkan(text, source="stream"))
                if self.is_balkan_detected(stats):
                    break
            except Exception:
                continue
        return stats

    def category_name_map(self, categories):
        if not isinstance(categories, list):
            return {}
        names = {}
        for category in categories:
            if not isinstance(category, dict):
                continue
            cat_id = str(category.get("category_id", ""))
            if cat_id:
                names[cat_id] = str(category.get("category_name", ""))
        return names
