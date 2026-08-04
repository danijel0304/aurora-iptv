from __future__ import annotations

import csv
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import webbrowser
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

from PyQt6.QtCore import QPoint, QRect, QSize, QSettings, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QSplashScreen,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core import (
    Vault,
    extract_playlist_urls,
    format_mac_groups,
    group_macs_by_url,
    normalize_url,
    parse_xtream_url,
)
from workers import (
    MacHttpWorker,
    PlaylistWorker,
    StalkerBalkanMacWorker,
    StalkerProfileCheckWorker,
    XtreamScanWorker,
    parse_mac_lines,
)

def app_data_dir() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    path = base / "Aurora IPTV"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


RESOURCE_DIR = resource_dir()
APP_DIR = app_data_dir()
APP_ICON_PATH = RESOURCE_DIR / "packaging" / "aurora-iptv.png"
DEFAULT_APP_VERSION = "v1.1.13"


def app_version() -> str:
    for path in (
        RESOURCE_DIR / "aurora_version.txt",
        Path(__file__).resolve().parent / "aurora_version.txt",
    ):
        try:
            version = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if version:
            return version
    return DEFAULT_APP_VERSION


APP_VERSION = app_version()
GITHUB_REPO = "danijel0304/aurora-iptv"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
GITHUB_LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
PAYPAL_DONATION_URL = "https://www.paypal.me/danijel0304"
STALKER_STUDIO_FILENAME = "IPTV_List_Generator_3.0_FULL_FIXED_v3_EXPIRY_PATCHED_v14_AUTO_THREADS.py"
BALKAN_IPTV_DIRNAME = "balkan_iptv"


def stalker_studio_source_path() -> Path:
    candidates: list[Path] = []
    source_dir = Path(__file__).resolve().parent

    def add(path: Path) -> None:
        resolved = path.expanduser()
        if resolved not in candidates:
            candidates.append(resolved)

    for base in (RESOURCE_DIR, source_dir, APP_DIR):
        add(base / "vendor" / "stalker_studio" / STALKER_STUDIO_FILENAME)
        add(base.parent / "iPTV_List_Generetor_New" / STALKER_STUDIO_FILENAME)

    for desktop_root in (Path.home() / "Desktop" / "Projekti", Path.home() / "Desktop" / "test"):
        add(desktop_root / "iPTV_List_Generetor_New" / STALKER_STUDIO_FILENAME)

    for path in candidates:
        if path.is_file():
            return path

    searched = "\n".join(f"- {path}" for path in candidates)
    raise FileNotFoundError(
        "Nedostaje Stalker Studio izvorna datoteka. Tražene lokacije:\n" + searched
    )


def balkan_iptv_source_dir() -> Path:
    candidates: list[Path] = []
    source_dir = Path(__file__).resolve().parent

    def add(path: Path) -> None:
        resolved = path.expanduser()
        if resolved not in candidates:
            candidates.append(resolved)

    for base in (RESOURCE_DIR, source_dir, APP_DIR):
        add(base / "vendor" / BALKAN_IPTV_DIRNAME)
        add(base.parent / "Fusion_IPTV")

    for desktop_root in (Path.home() / "Desktop" / "Projekti", Path.home() / "Desktop" / "test"):
        add(desktop_root / "Fusion_IPTV")

    for path in candidates:
        if (path / "main.py").is_file():
            return path

    searched = "\n".join(f"- {path}" for path in candidates)
    raise FileNotFoundError("Nedostaje Balkan IPTV modul. Tražene lokacije:\n" + searched)


def version_tuple(value: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", value or "")]
    return tuple(numbers or [0])


def is_newer_version(latest: str, current: str) -> bool:
    latest_parts = version_tuple(latest)
    current_parts = version_tuple(current)
    size = max(len(latest_parts), len(current_parts))
    return latest_parts + (0,) * (size - len(latest_parts)) > current_parts + (
        0,
    ) * (size - len(current_parts))


class UpdateCheckWorker(QThread):
    checked = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def run(self) -> None:
        try:
            request = Request(
                GITHUB_LATEST_RELEASE_API,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "Aurora-IPTV/update-check",
                },
            )
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            latest = str(payload.get("tag_name") or "")
            if not latest:
                raise RuntimeError("GitHub nije vratio oznaku verzije.")
            assets = []
            for asset in payload.get("assets", []):
                if not isinstance(asset, dict):
                    continue
                name = str(asset.get("name") or "")
                download_url = str(asset.get("browser_download_url") or "")
                if not name or not download_url:
                    continue
                assets.append(
                    {
                        "name": name,
                        "download_url": download_url,
                        "size": int(asset.get("size") or 0),
                    }
                )
            self.checked.emit(
                {
                    "latest": latest,
                    "current": APP_VERSION,
                    "url": str(payload.get("html_url") or GITHUB_RELEASES_URL),
                    "is_newer": is_newer_version(latest, APP_VERSION),
                    "assets": assets,
                }
            )
        except Exception as error:
            self.failed.emit(str(error) or type(error).__name__)


class UpdateDownloadWorker(QThread):
    progress = pyqtSignal(int, str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, asset: dict, parent=None):
        super().__init__(parent)
        self.asset = asset

    def run(self) -> None:
        partial = None
        try:
            name = str(self.asset.get("name") or "")
            download_url = str(self.asset.get("download_url") or "")
            if not name or not download_url:
                raise RuntimeError("Nedostaje release asset za update.")

            update_dir = Path(tempfile.gettempdir()) / "aurora-iptv-updates"
            update_dir.mkdir(parents=True, exist_ok=True)
            target = update_dir / name
            partial = target.with_name(f"{target.name}.part")
            if partial.exists():
                partial.unlink()

            request = Request(
                download_url,
                headers={"User-Agent": "Aurora-IPTV/self-updater"},
            )
            with urlopen(request, timeout=20) as response:
                total = int(response.headers.get("Content-Length") or self.asset.get("size") or 0)
                downloaded = 0
                with open(partial, "wb") as handle:
                    while True:
                        if self.isInterruptionRequested():
                            raise RuntimeError("Preuzimanje updatea je prekinuto.")
                        chunk = response.read(1024 * 512)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            percent = max(0, min(100, int(downloaded * 100 / total)))
                            self.progress.emit(percent, f"Preuzimam update... {percent}%")
            partial.replace(target)
            if target.suffix.lower() in {".appimage", ".exe"}:
                try:
                    target.chmod(target.stat().st_mode | 0o755)
                except OSError:
                    pass
            result = dict(self.asset)
            result["path"] = str(target)
            self.progress.emit(100, "Update je preuzet.")
            self.succeeded.emit(result)
        except Exception as error:
            if partial:
                try:
                    partial.unlink()
                except Exception:
                    pass
            self.failed.emit(str(error) or type(error).__name__)


STYLE = """
* { font-family: "Segoe UI", "Inter", sans-serif; font-size: 13px; }
QMainWindow, QWidget { background: #0b1020; color: #e8ecf6; }
QTabWidget::pane { border: 1px solid #27314c; border-radius: 12px; background: #10172a; top: -1px; }
QTabBar::tab { background: transparent; color: #8f9bb6; padding: 11px 18px; margin-right: 4px; border-bottom: 2px solid transparent; }
QTabBar::tab:hover { color: #dce6ff; background: #151e34; }
QTabBar::tab:selected { color: #78a6ff; border-bottom-color: #5d8cff; font-weight: 700; }
QFrame#Card { background: #111a2f; border: 1px solid #263250; border-radius: 14px; }
QLabel#Title { font-size: 27px; font-weight: 800; color: #f5f7ff; }
QLabel#Subtitle { color: #8491ad; font-size: 13px; }
QLabel#GuideText { color: #b9c4da; font-size: 13px; font-weight: 600; }
QLabel#ToolDescription { color: #cdd8f0; font-size: 13px; font-weight: 500; }
QLabel#Metric { font-size: 25px; font-weight: 800; color: #70a1ff; }
QLabel#MetricName { color: #8491ad; font-weight: 600; }
QLineEdit, QTextEdit, QComboBox, QSpinBox {
    background: #0b1223; border: 1px solid #2a3656; border-radius: 8px;
    padding: 8px; color: #e8ecf6; selection-background-color: #4169b3;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #5d8cff; }
QPushButton { background: #202c47; color: #e8ecf6; border: 1px solid #32415f; border-radius: 8px; padding: 8px 14px; font-weight: 600; }
QPushButton:hover { background: #293957; border-color: #4e628b; }
QPushButton#Primary { background: #4c7df0; border-color: #6592fa; color: white; }
QPushButton#Primary:hover { background: #5b89f2; }
QPushButton#Danger { background: #402137; border-color: #6e304c; color: #ff9eb9; }
QTableWidget { background: #0c1325; border: 1px solid #263250; border-radius: 9px; gridline-color: #222d47; alternate-background-color: #101a30; }
QTableWidget::item { padding: 6px; }
QTableWidget::item:selected { background: #284778; }
QHeaderView::section { background: #17213a; color: #9eb5e5; border: 0; border-right: 1px solid #263250; padding: 8px; font-weight: 700; }
QProgressBar { background: #0b1223; border: 1px solid #2a3656; border-radius: 7px; text-align: center; min-height: 17px; }
QProgressBar::chunk { background: #4c7df0; border-radius: 6px; }
QCheckBox { spacing: 8px; color: #c9d2e8; }
QStatusBar { background: #0a0f1c; color: #8390aa; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: #0b1020; }
QScrollBar:vertical, QScrollBar:horizontal {
    background: #0a0f1c; border: 0; margin: 0; width: 12px; height: 12px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #32415f; border-radius: 6px; min-height: 28px; min-width: 28px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #4e628b; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip {
    background: #17213a; color: #f3f6ff; border: 1px solid #4e628b;
    padding: 7px; border-radius: 6px;
}
"""

LIGHT_STYLE = """
* { font-family: "Segoe UI", "Inter", sans-serif; font-size: 13px; }
QMainWindow, QWidget { background: #e9eef6; color: #0f172a; }
QTabWidget::pane { border: 1px solid #aebbd0; border-radius: 12px; background: #f6f8fc; top: -1px; }
QTabBar::tab { background: transparent; color: #42526b; padding: 11px 18px; margin-right: 4px; border-bottom: 2px solid transparent; font-weight: 600; }
QTabBar::tab:hover { color: #0f172a; background: #dfe7f3; }
QTabBar::tab:selected { color: #174bbd; border-bottom-color: #2f66df; font-weight: 800; }
QFrame#Card { background: #f8fafd; border: 1px solid #b8c4d8; border-radius: 14px; }
QLabel#Title { font-size: 27px; font-weight: 900; color: #0b1220; }
QLabel#Subtitle { color: #34445c; font-size: 13px; font-weight: 600; }
QLabel#GuideText { color: #172033; font-size: 13px; font-weight: 650; }
QLabel#ToolDescription { color: #1f2f46; font-size: 13px; font-weight: 700; }
QLabel#Metric { font-size: 25px; font-weight: 900; color: #174bbd; }
QLabel#MetricName { color: #34445c; font-weight: 700; }
QLineEdit, QTextEdit, QComboBox, QSpinBox {
    background: #fdfefe; border: 1px solid #aebbd0; border-radius: 8px;
    padding: 8px; color: #0f172a; selection-background-color: #adc7ff;
}
QLineEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: #2f66df; }
QPushButton { background: #dfe7f3; color: #0f172a; border: 1px solid #aebbd0; border-radius: 8px; padding: 8px 14px; font-weight: 700; }
QPushButton:hover { background: #d2ddec; border-color: #8293ad; }
QPushButton#Primary { background: #2f66df; border-color: #174bbd; color: white; }
QPushButton#Primary:hover { background: #245bd1; }
QPushButton#Danger { background: #f8dde5; border-color: #d58aa0; color: #8f1838; }
QTableWidget { background: #fdfefe; border: 1px solid #b8c4d8; border-radius: 9px; gridline-color: #cfd8e6; alternate-background-color: #eef3f9; color: #0f172a; }
QTableWidget::item { padding: 6px; }
QTableWidget::item:selected { background: #b9d0ff; color: #08111f; }
QHeaderView::section { background: #dfe7f3; color: #1f365a; border: 0; border-right: 1px solid #b8c4d8; padding: 8px; font-weight: 800; }
QProgressBar { background: #f8fafd; border: 1px solid #aebbd0; border-radius: 7px; text-align: center; min-height: 17px; color: #0f172a; }
QProgressBar::chunk { background: #2f66df; border-radius: 6px; }
QCheckBox { spacing: 8px; color: #1f2f46; font-weight: 600; }
QStatusBar { background: #dfe7f3; color: #34445c; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: #e9eef6; }
QScrollBar:vertical, QScrollBar:horizontal {
    background: #dfe7f3; border: 0; margin: 0; width: 12px; height: 12px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #8293ad; border-radius: 6px; min-height: 28px; min-width: 28px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #64748b; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip {
    background: #f8fafd; color: #0f172a; border: 1px solid #8293ad;
    padding: 7px; border-radius: 6px;
}
"""

UI_TEXT = {
    "en": {
        "subtitle": "Unified tool for IPTV list analysis, checking, building and archive",
        "ready": "● Ready",
        "status_ready": "Aurora IPTV is ready.",
        "home": "Home",
        "archive": "Archive",
        "settings": "Settings",
        "dashboard_heading": "Control center",
        "dashboard_description": "All existing tools are connected in one interface. Use only portals and accounts you are allowed to access.",
        "quick_start": "Quick start",
        "guide": (
            "1. Home\n"
            "- Shows basic work statistics and this short guide.\n\n"
            "2. Xtream Studio\n"
            "- Analysis: load TXT, log, JSON or M3U files, extract IPTV/M3U links, filter by server and remove duplicates.\n"
            "- Account check: checks get.php Xtream links and shows status, expiry, connections, content and ping. Active accounts can be archived or sent to the generator.\n"
            "- Live/VOD/Series: loads channels, movies and series from an Xtream account. Filter content, select programs or groups and export a new M3U list.\n"
            "- Balkan IPTV: finds Balkan/Ex-YU content, scores results and tests streams. Use it for regional export of domestic channels.\n\n"
            "3. Stalker Studio\n"
            "- Profiles: load or paste portal + MAC profiles and save them for later work. A selected profile can be opened directly in Stalker Studio.\n"
            "- URL -> MAC grouping: turns messy text into portals with matching MAC addresses. Useful before checking or sending to profiles.\n"
            "- Portal check: checks whether portal + MAC pairs work and shows status/ping. Broken profiles can be removed quickly.\n"
            "- Balkan MAC test: uses the working portal + MAC pairs to find Balkan channels, create tokenized links and test random live samples.\n"
            "- Studio · Live / VOD / Series: loads content from Stalker/MAG portals. Select groups, programs and create an M3U export.\n\n"
            "4. Archive\n"
            "- Stores active Xtream accounts, saved M3U lists and MAC/Stalker profiles. You can send them back to checking, generator or Stalker Studio.\n\n"
            "5. Settings\n"
            "- Network: User-Agent and proxy settings. VLC / Player: path to the external player for opening streams."
        ),
    },
    "hr": {
        "subtitle": "Jedinstveni alat za analizu, provjeru, izradu i arhivu IPTV lista",
        "ready": "● Spremno",
        "status_ready": "Aurora IPTV je spremna.",
        "home": "Početna",
        "archive": "Arhiva",
        "settings": "Postavke",
        "dashboard_heading": "Kontrolni centar",
        "dashboard_description": "Svi postojeći alati spojeni su u jedno sučelje. Koristi samo portale i račune za koje imaš dopuštenje.",
        "quick_start": "Brzi početak",
        "guide": (
            "1. Početna\n"
            "- Prikazuje osnovnu statistiku rada i ovaj kratki vodič kroz program.\n\n"
            "2. Xtream Studio\n"
            "- Analiza: prvi korak za rad s listama. Učitaj TXT, log, JSON ili M3U, izvuci IPTV/M3U linkove, filtriraj ih po serveru i ukloni duplikate.\n"
            "- Provjera računa: provjerava get.php Xtream linkove i prikazuje status, istek, veze, sadržaj i ping. Aktivne račune možeš spremiti u arhivu ili poslati u generator.\n"
            "- Live/VOD/Series: učitava kanale, filmove i serije iz Xtream računa. Filtriraj sadržaj, označi programe ili grupe i izvezi novu M3U listu.\n"
            "- Balkan IPTV: traži Balkan/Ex-YU sadržaj, ocjenjuje rezultate i testira streamove. Koristi ga za regionalni export domaćih kanala.\n\n"
            "3. Stalker Studio\n"
            "- Profili: učitaj ili zalijepi portal + MAC profile i spremi ih za daljnji rad. Odabrani profil možeš otvoriti direktno u Stalker Studiju.\n"
            "- URL -> MAC grupiranje: iz neurednog teksta složi portale i pripadajuće MAC adrese. Korisno je prije provjere ili slanja u profile.\n"
            "- Provjera portala: provjerava rade li portal + MAC parovi i prikazuje status/ping. Neispravne profile možeš brzo ukloniti.\n"
            "- Balkan MAC test: iz ispravnih portal + MAC parova traži Balkan kanale, radi tokenizirane linkove i testira nasumične live uzorke.\n"
            "- Studio · Live / VOD / Series: učitava sadržaj iz Stalker/MAG portala. Možeš odabrati grupe, programe i napraviti M3U export.\n\n"
            "4. Arhiva\n"
            "- Čuva aktivne Xtream račune, spremljene M3U liste i MAC/Stalker profile. Iz arhive ih možeš vratiti u provjeru, generator ili Stalker Studio.\n\n"
            "5. Postavke\n"
            "- Mreža: User-Agent i proxy postavke. VLC / Player: putanja do vanjskog playera za pokretanje streamova."
        ),
    },
}

EN_TRANSLATIONS = {
    "Početna": "Home",
    "Hrvatski": "Croatian",
    "Arhiva": "Archive",
    "Postavke": "Settings",
    "Analiza": "Analysis",
    "Arhiva": "Archive",
    "Mreža": "Network",
    "Datoteka": "File",
    "Otvori datoteku u URL analizatoru": "Open file in URL analyzer",
    "Učitaj liste u Xtream skener": "Load lists into Xtream scanner",
    "Pokreni Stalker Studio": "Open Stalker Studio",
    "Izlaz": "Exit",
    "Analizirani URL-ovi": "Analyzed URLs",
    "Aktivni računi": "Active accounts",
    "Obrađene MAC adrese": "Processed MAC addresses",
    "Zapisi u arhivi": "Archive records",
    "Balkan IPTV modul nije moguće ugraditi": "Balkan IPTV module cannot be embedded",
    "Balkan arhiva - spremljene liste i ponovna provjera.": "Balkan archive - saved lists and re-checking.",
    "Balkan skener": "Balkan scanner",
    "Rezultati": "Results",
    "Uređivač sadržaja": "Content editor",
    "Super-lista": "Super list",
    "Postavke": "Settings",
    "O aplikaciji": "About",
    "Test streamova": "Test streams",
    "Pokreni Balkan provjeru": "Start Balkan check",
    "Export odabranih u M3U": "Export selected to M3U",
    "Export odabranih": "Export selected",
    "Live TV kanali": "Live TV channels",
    "Filmovi (VOD)": "Movies (VOD)",
    "Serije": "Series",
    "Otvori": "Open",
    "Dodaj datoteke": "Add files",
    "Izvuci URL-ove": "Extract URLs",
    "Filtriraj rezultate po URL-u ili serveru...": "Filter results by URL or server...",
    "Izvještaj": "Report",
    "Otvori označeni link": "Open selected link",
    "Očisti": "Clear",
    "Zalijepi tekst, log, JSON ili M3U sadržaj...": "Paste text, log, JSON or M3U content...",
    "Očišćeni jedinstveni URL-ovi pojavit će se ovdje.": "Clean unique URLs will appear here.",
    "URL-ovi: 0 · Duplikati: 0 · Serveri: 0": "URLs: 0 · Duplicates: 0 · Servers: 0",
    "Kopiraj": "Copy",
    "Spremi TXT": "Save TXT",
    "Export čistih URL-ova": "Export clean URLs",
    "Spremi listu u arhivu": "Save list to archive",
    "Učitaj TXT": "Load TXT",
    "Dodaj TXT": "Add TXT",
    "Grupiraj": "Group",
    "Provjeri URL format": "Check URL format",
    "URL pa pripadajuće MAC adrese, redak po redak...": "URL followed by matching MAC addresses, line by line...",
    "Grupe: 0 · MAC: 0": "Groups: 0 · MAC: 0",
    "Spremi izlaz": "Save output",
    "Spremi grupe u arhivu": "Save groups to archive",
    "Pošalji u Provjeru portala": "Send to portal check",
    "Dodaj sve u Provjeru portala": "Add all to portal check",
    "Dodaj sve URL/MAC parove iz grupiranja u tab Provjera portala bez ručnog kopiranja.": "Add all grouped URL/MAC pairs to the Portal check tab without manual copying.",
    "Paralelno:": "Parallel:",
    "Timeout:": "Timeout:",
    "Učitaj TXT/M3U": "Load TXT/M3U",
    "Pokreni provjeru": "Start check",
    "Jedan ili više get.php URL-ova s username i password parametrima...": "One or more get.php URLs with username and password parameters...",
    "Filtriraj server, korisnika, status ili sadržaj...": "Filter server, user, status or content...",
    "Svi statusi": "All statuses",
    "Samo aktivni": "Only active",
    "Samo neaktivni": "Only inactive",
    "Desni klik na red za spremanje u arhivu.": "Right-click a row to save it to the archive.",
    "Export aktivnih M3U": "Export active M3U",
    "Spremi aktivne u arhivu": "Save active to archive",
    "Pošalji u Generator": "Send to Generator",
    "Učitaj u Balkan IPTV": "Load into Balkan IPTV",
    "Ukloni neaktivne": "Remove inactive",
    "Ukloni duplikate": "Remove duplicates",
    "Očisti rezultate": "Clear results",
    "MAC adrese, jedna po retku...": "MAC addresses, one per line...",
    "Namijenjeno isključivo endpointima za koje imaš ovlaštenje.": "Use only with endpoints you are authorized to access.",
    "Učitaj MAC TXT": "Load MAC TXT",
    "Dodaj MAC TXT": "Add MAC TXT",
    "Pokreni MAC provjeru": "Start MAC check",
    "Export rezultata CSV": "Export results CSV",
    "Spremi rezultate u arhivu": "Save results to archive",
    "Zalijepi cijeli get.php link s username i password parametrima...": "Paste the full get.php link with username and password parameters...",
    "Iščitaj link": "Parse link",
    "Učitaj M3U": "Load M3U",
    "Očisti sve": "Clear all",
    "Učitaj sadržaj": "Load content",
    "Učitaj sve": "Load all",
    "Server": "Server",
    "Korisnik": "User",
    "Lozinka": "Password",
    "Cijeli link": "Full link",
    "Filtriraj po nazivu kanala ili kategoriji...": "Filter by channel name or category...",
    "Export označeno": "Export selected",
    "Spremi prikazano": "Save visible",
    "Spremi označeno": "Save selected",
    "Spremi sve": "Save all",
    "Označi sve": "Select all",
    "Odznači sve": "Unselect all",
    "Grupe": "Groups",
    "Označi sve grupe": "Select all groups",
    "Odznači sve grupe": "Unselect all groups",
    "Kanali: 0": "Channels: 0",
    "Provjera računa": "Account check",
    "Napredni Stalker / MAG portal studio": "Advanced Stalker / MAG portal studio",
    "Učitaj TXT listu URL/MAC profila": "Load TXT URL/MAC profile list",
    "Dodaj TXT profile": "Add TXT profiles",
    "Pokreni odabrani profil u Stalker Studiju": "Open selected profile in Stalker Studio",
    "Otvori Stalker Studio": "Open Stalker Studio",
    "Export profila TXT": "Export profiles TXT",
    "Spremi profile u arhivu": "Save profiles to archive",
    "Profili": "Profiles",
    "URL → MAC grupiranje": "URL -> MAC grouping",
    "Provjera portala": "Portal check",
    "Balkan MAC test": "Balkan MAC test",
    "Provjeri valjanost": "Check validity",
    "Učitaj iz profila": "Load from profiles",
    "Zalijepi i prepoznaj": "Paste and detect",
    "Provjeri URL/MAC": "Check URL/MAC",
    "Zaustavi provjeru": "Stop check",
    "Ukloni koji ne rade": "Remove broken",
    "Ukloni odabrano": "Remove selected",
    "Pošalji odabrano u Studio": "Send selected to Studio",
    "Export ispravnih": "Export valid",
    "Učitaj ispravne iz Provjere portala": "Load valid from Portal check",
    "Prebaci samo redove gdje je Provjera portala označila Radi = DA.": "Load only rows where Portal check marked Works = YES.",
    "Očisti tablicu": "Clear table",
    "Provjeri Balkan MAC": "Check Balkan MAC",
    "Zaustavi test": "Stop test",
    "Nasumičnih streamova po MAC-u": "Random streams per MAC",
    "Za svaki portal/MAC učitava Live grupe, pronalazi Balkan programe, radi create_link/token i nasumično proba nekoliko streamova.": "For each portal/MAC, loads Live groups, finds Balkan programs, creates tokenized links and randomly tests a few streams.",
    "Zalijepi portal URL i MAC adrese ako ne učitavaš iz drugih Stalker tabova.": "Paste portal URL and MAC addresses if you are not loading from other Stalker tabs.",
    "Balkan": "Balkan",
    "Radi Balkan": "Balkan works",
    "Testirano": "Tested",
    "Uzorci": "Samples",
    "Aktivni računi spremljeni bez duplikata": "Active accounts saved without duplicates",
    "Pretraži račune po serveru, korisniku, statusu ili isteku...": "Search accounts by server, user, status or expiry...",
    "Osvježi": "Refresh",
    "Povuci u Generator": "Pull to Generator",
    "Pošalji u provjeru": "Send to check",
    "Predloži čišćenje": "Suggest cleanup",
    "Obriši označeno": "Delete selected",
    "Obriši sve račune": "Delete all accounts",
    "Backup sve": "Backup all",
    "Restore backup": "Restore backup",
    "Import JSON": "Import JSON",
    "Xtream liste spremljene iz Generatora i provjere": "Xtream lists saved from Generator and checks",
    "MAC i Stalker liste/profili": "MAC and Stalker lists/profiles",
    "Pretraži spremljene liste po nazivu, tipu ili izvoru...": "Search saved lists by name, type or source...",
    "Otvori listu": "Open list",
    "Export liste": "Export list",
    "Kopiraj listu": "Copy list",
    "Vrati u Generator": "Return to Generator",
    "Obriši listu": "Delete list",
    "Obriši sve liste": "Delete all lists",
    "Mreža": "Network",
    "Opcionalni proxyji, jedan po retku": "Optional proxies, one per line",
    "Odaberi folder": "Choose folder",
    "Automatski spremi aktivne račune u arhivu": "Automatically save active accounts to archive",
    "Traži potvrdu prije masovnog brisanja/čišćenja": "Ask for confirmation before bulk delete/cleanup",
    "Zapamti zadnji otvoreni tab": "Remember last opened tab",
    "Pronađi player": "Find player",
    "Spremi postavke": "Save settings",
    "ID": "ID",
    "Status": "Status",
    "Server": "Server",
    "Korisnik": "User",
    "Lozinka": "Password",
    "Ističe": "Expires",
    "Veze": "Connections",
    "Sadržaj": "Content",
    "Provjereno": "Checked",
    "Naziv": "Name",
    "Kategorija": "Category",
    "EPG ID": "EPG ID",
    "Tip": "Type",
    "Izvor": "Source",
    "Stavki": "Items",
    "Spremljeno": "Saved",
    "Portal URL": "Portal URL",
    "MAC adresa": "MAC address",
    "Radi": "Works",
    "Vrijeme": "Time",
    "Balkan arhiva": "Balkan archive",
    "Izvlači IPTV i M3U URL-ove iz teksta ili datoteka, uklanja duplikate i pomaže brzo pronaći servere.": "Extracts IPTV and M3U URLs from text or files, removes duplicates and helps quickly find servers.",
    "Samo IPTV/M3U": "Only IPTV/M3U",
    "Uključi M3U8": "Include M3U8",
    "Prepoznaj query parametre": "Detect query parameters",
    "Sortiraj po serveru": "Sort by server",
    "Samo serveri": "Servers only",
    "Grupiraj po serveru": "Group by server",
    "Prikaže sažetak pronađenih URL-ova, duplikata, servera i M3U stavki.": "Show a summary of found URLs, duplicates, servers and M3U items.",
    "Otvori URL koji je označen ili je u redu gdje stoji kursor.": "Open the selected URL or the URL on the cursor line.",
    "Spremi samo očišćene jedinstvene URL-ove, bez dodatnog teksta iz ulaza.": "Save only cleaned unique URLs, without extra input text.",
    "Spremi trenutni rezultat u bazu kako bi ga kasnije mogao otvoriti iz Arhive.": "Save the current result to the database so it can be opened later from the Archive.",
    "Grupira MAC adrese po pripadajućem portalu kako bi se profili lakše pregledali, kopirali ili poslali u Stalker Studio.": "Groups MAC addresses by matching portal so profiles are easier to review, copy or send to Stalker Studio.",
    "Globalno ukloni duplikate": "Remove duplicates globally",
    "Sortiraj URL-ove": "Sort URLs",
    "Sortiraj MAC adrese": "Sort MAC addresses",
    "Spremi grupirane URL/MAC profile u bazu za kasnije korištenje.": "Save grouped URL/MAC profiles to the database for later use.",
    "Provjerava Xtream račune iz get.php URL-ova i prikazuje status, istek, broj veza, sadržaj i ping.": "Checks Xtream accounts from get.php URLs and shows status, expiry, connection count, content and ping.",
    "Napravi M3U listu samo od računa koji su u provjeri označeni kao aktivni.": "Create an M3U list only from accounts marked active in the check.",
    "Spremi M3U listu aktivnih računa u bazu bez pisanja datoteke.": "Save the active account M3U list to the database without writing a file.",
    "Prebaci označeni aktivni račun u Live/VOD/Series generator.": "Send the selected active account to the Live/VOD/Series generator.",
    "Prebaci vidljive URL-ove u Balkan IPTV skener bez kopiranja.": "Send visible URLs into the Balkan IPTV scanner without copying.",
    "Šalje MAC adrese na ovlašteni HTTP endpoint i bilježi koje adrese dobivaju uspješan odgovor.": "Sends MAC addresses to an authorized HTTP endpoint and records which addresses receive a successful response.",
    "Način slanja:": "Send method:",
    "Naziv polja:": "Field name:",
    "Tekst uspjeha:": "Success text:",
    "Opcionalni tekst koji odgovor mora sadržavati": "Optional text the response must contain",
    "Spremi tablicu MAC provjere u bazu za kasniji pregled ili export.": "Save the MAC check table to the database for later review or export.",
    "Učitava Live, VOD i serije s Xtream računa, filtrira sadržaj i izvozi odabrane stavke u M3U listu.": "Loads Live, VOD and series from an Xtream account, filters content and exports selected items to an M3U list.",
    "Iz cijelog Xtream get.php linka popuni server, korisnika i lozinku.": "Fill server, username and password from the full Xtream get.php link.",
    "Učitaj lokalnu M3U/M3U8 listu i zadrži originalne nazive grupa.": "Load a local M3U/M3U8 list and keep original group names.",
    "Učitaj Live, VOD i Serije redom iz istog Xtream računa.": "Load Live, VOD and Series in sequence from the same Xtream account.",
    "Izvezi samo stavke koje su trenutno vidljive nakon filtera i odabira grupe.": "Export only items currently visible after filtering and group selection.",
    "Izvezi samo programe označene checkboxom.": "Export only programs selected by checkbox.",
    "Spremi trenutno filtriranu listu u bazu bez exporta u datoteku.": "Save the currently filtered list to the database without exporting a file.",
    "Spremi samo programe označene checkboxom.": "Save only programs selected by checkbox.",
    "Spremi sve učitane stavke iz trenutnog Live/VOD/Serije taba.": "Save all loaded items from the current Live/VOD/Series tab.",
    "Prikaži pregled M3U zapisa za trenutno prikazane stavke.": "Show an M3U preview for the currently visible items.",
    "Stream URL": "Stream URL",
    "Označi sve trenutno prikazane programe u tablici.": "Select all currently visible programs in the table.",
    "Odznači sve trenutno prikazane programe u tablici.": "Unselect all currently visible programs in the table.",
    "Upravlja Stalker portal/MAC profilima i otvara ih u ugrađenom Studiju za učitavanje kanala i M3U export.": "Manages Stalker portal/MAC profiles and opens them in the embedded Studio to load channels and export M3U.",
    "Puni generator iz postojećeg projekta: automatski prepoznaje portal.php i stalker_portal/server/load.php, učitava Live, VOD i TV Shows, podržava kategorije, pojedinačni odabir, Adult PIN, auto-threads, brzi M3U export, resolve linkova i provjeru linkova nakon exporta.": "Full generator from the existing project: automatically detects portal.php and stalker_portal/server/load.php, loads Live, VOD and TV Shows, supports categories, individual selection, Adult PIN, auto-threads, quick M3U export, link resolving and link checks after export.",
    "Spremi sve prikazane Stalker portal/MAC profile u bazu.": "Save all displayed Stalker portal/MAC profiles to the database.",
    "Stalker Studio nije moguće ugraditi:": "Stalker Studio cannot be embedded:",
    "Provjerava odgovara li Stalker/MAG portal za URL i pripadajuću MAC adresu.": "Checks whether a Stalker/MAG portal responds for the URL and matching MAC address.",
    "Čuva provjerene aktivne račune bez duplikata i omogućuje import, export i brzo brisanje zapisa.": "Stores checked active accounts without duplicates and supports import, export and quick record deletion.",
    "Popuni Xtream Generator serverom, korisnikom i lozinkom iz označenog računa.": "Fill Xtream Generator with server, username and password from the selected account.",
    "Pretvori označeni arhivirani račun u get.php URL i pošalji ga u Xtream provjeru.": "Convert the selected archived account to a get.php URL and send it to Xtream check.",
    "Pronađe istekle ili neaktivne račune i pita prije brisanja iz arhive.": "Find expired or inactive accounts and ask before deleting them from the archive.",
    "Učita spremljenu listu natrag u odgovarajući alat za pregled ili daljnji rad.": "Load the saved list back into the matching tool for review or further work.",
    "Iz spremljene liste izvuče Xtream URL-ove i pošalje ih u provjeru.": "Extract Xtream URLs from the saved list and send them to check.",
    "Spremi označenu arhiviranu listu kao datoteku.": "Save the selected archived list as a file.",
    "Učita spremljenu M3U/listu u Xtream Generator za daljnje uređivanje.": "Load the saved M3U/list into Xtream Generator for further editing.",
    "Podešava mrežne opcije i vanjski player koje koriste alati u Aurori.": "Configures network options and the external player used by Aurora tools.",
    "Proxy lista:": "Proxy list:",
    "VLC / vanjski player:": "VLC / external player:",
    "Export folder:": "Export folder:",
    "Ažuriranja": "Updates",
    "Trenutna verzija:": "Current version:",
    "Automatski provjeri update pri pokretanju": "Automatically check for updates on startup",
    "Provjeri update": "Check for updates",
    "Otvori GitHub release": "Open GitHub release",
    "Podrška": "Support",
    "PayPal donacija za danijel0304.": "PayPal donation for danijel0304.",
    "Doniraj preko PayPala": "Donate with PayPal",
    "Otvori stranicu za preuzimanje najnovije verzije.": "Open the download page for the latest version.",
    "Otvori PayPal.me stranicu za donaciju.": "Open the PayPal.me donation page.",
    "Nije još provjereno.": "Not checked yet.",
    "Otvori datoteke": "Open files",
    "Podržano (*.txt *.log *.csv *.json *.m3u *.m3u8);;Sve datoteke (*)": "Supported (*.txt *.log *.csv *.json *.m3u *.m3u8);;All files (*)",
    "Sve datoteke (*)": "All files (*)",
    "Tekst (*.txt)": "Text (*.txt)",
    "Spremi / ažuriraj u arhivi": "Save / update in archive",
    "Kopiraj M3U URL": "Copy M3U URL",
    "Kopiraj server / korisnik / lozinka": "Copy server / username / password",
    "Otvori račun u Xtream Generatoru": "Open account in Xtream Generator",
    "Kopiraj naziv": "Copy name",
    "Kopiraj stream URL": "Copy stream URL",
    "Kopiraj M3U zapis": "Copy M3U entry",
    "Pokreni stream u VLC playeru": "Play stream in VLC player",
    "Kopiraj sve": "Copy all",
    "Pošalji sve u Xtream skener": "Send all to Xtream scanner",
    "Pošalji sve u Balkan IPTV": "Send all to Balkan IPTV",
    "Pošalji M3U u Balkan IPTV": "Send M3U to Balkan IPTV",
    "Pošalji označeni Xtream URL u generator": "Send selected Xtream URL to generator",
    "Pošalji URL/MAC profile u Stalker": "Send URL/MAC profiles to Stalker",
    "Pošalji grupe u Stalker tab": "Send groups to Stalker tab",
    "Kopiraj URL i MAC": "Copy URL and MAC",
    "Pošalji u Stalker profile": "Send to Stalker profiles",
    "Zalijepi URL/MAC profile": "Paste URL/MAC profiles",
    "Kopiraj portal": "Copy portal",
    "Kopiraj MAC": "Copy MAC",
    "Kopiraj portal i MAC": "Copy portal and MAC",
    "Otvori u Stalker Studiju": "Open in Stalker Studio",
    "Ukloni profil": "Remove profile",
    "Zaustavi": "Stop",
    "Učitavanje...": "Loading...",
    "Provjeri streamove u svim listama.": "Check streams in all lists.",
    "Provjeri Xtream/M3U liste i označi gdje je pronađen Balkan/Ex-YU sadržaj.": "Check Xtream/M3U lists and mark where Balkan/Ex-YU content was found.",
    "Učitano datoteka:": "Loaded files:",
    "Prvo pokreni izvlačenje URL-ova.": "Run URL extraction first.",
    "Znakova u ulazu:": "Input characters:",
    "Ukupno URL-ova:": "Total URLs:",
    "Odbačeno:": "Discarded:",
    "Jedinstveni serveri:": "Unique servers:",
    "M3U sadržaj:": "M3U content:",
    "Označi URL ili postavi kursor u njegov redak.": "Select a URL or place the cursor on its line.",
    "Nema URL/MAC parova za slanje.": "No URL/MAC pairs to send.",
    "Dodano profila u Provjeru portala:": "Profiles added to Portal check:",
    "Ukupno u provjeri:": "Total in check:",
    "Prepoznato URL-ova:": "Detected URLs:",
    "Prepoznato MAC adresa:": "Detected MAC addresses:",
    "Nevaljanih URL-ova:": "Invalid URLs:",
    "Nema URL-ova": "No URLs",
    "Nisu pronađeni Xtream URL-ovi s podacima.": "No Xtream URLs with credentials were found.",
    "Označi račun u tablici provjere.": "Select an account in the check table.",
    "Označeni račun nije aktivan. Želiš ga svejedno poslati u Generator?": "The selected account is not active. Send it to Generator anyway?",
    "Nema vidljivih rezultata.": "No visible results.",
    "Export rezultata": "Export results",
    "Ukloni neaktivne": "Remove inactive",
    "Želiš ukloniti sve neaktivne rezultate iz tablice?": "Remove all inactive results from the table?",
    "Nema aktivnih vidljivih računa.": "No visible active accounts.",
    "Export aktivnih računa": "Export active accounts",
    "Nedostaju podaci": "Missing data",
    "Unesi valjan endpoint i MAC adrese.": "Enter a valid endpoint and MAC addresses.",
    "Export MAC rezultata": "Export MAC results",
    "Učitaj M3U listu": "Load M3U list",
    "M3U liste (*.m3u *.m3u8 *.txt);;Sve datoteke (*)": "M3U lists (*.m3u *.m3u8 *.txt);;All files (*)",
    "Datoteka ne sadrži prepoznatljive M3U stavke.": "The file does not contain recognizable M3U items.",
    "Cijeli link": "Full link",
    "Link mora sadržavati server te username i password parametre.": "The link must contain a server plus username and password parameters.",
    "Unesi server, korisnika i lozinku.": "Enter server, username and password.",
    "Učitavanje nije uspjelo": "Loading failed",
    "Nema označenih kanala za export.": "No selected channels to export.",
    "Nema prikazanih kanala za export.": "No visible channels to export.",
    "Nema kanala": "No channels",
    "Spremi M3U": "Save M3U",
    "Nema prikazanih kanala za pregled.": "No visible channels to preview.",
    "... prikazano prvih 50 od": "... showing first 50 of",
    "Spremi listu": "Save list",
    "Naziv liste:": "List name:",
    "Nema sadržaja za spremanje.": "No content to save.",
    "Lista je spremljena u arhivu:": "List saved to archive:",
    "Nema označenih stavki za spremanje.": "No selected items to save.",
    "Nema prikazanih stavki za spremanje.": "No visible items to save.",
    "označeno": "selected",
    "prikazano": "visible",
    "sve": "all",
    "Nema učitanih stavki za spremanje.": "No loaded items to save.",
    "Nema aktivnih vidljivih računa za spremanje.": "No visible active accounts to save.",
    "Nema MAC rezultata za spremanje.": "No MAC results to save.",
    "Nema Stalker profila za spremanje.": "No Stalker profiles to save.",
    "Nema URL/MAC profila za provjeru.": "No URL/MAC profiles to check.",
    "Označi URL/MAC profil.": "Select a URL/MAC profile.",
    "Nema ispravnih profila za export.": "No valid profiles to export.",
    "Nema URL-ova za Balkan IPTV.": "No URLs for Balkan IPTV.",
    "Balkan IPTV nije učitan.": "Balkan IPTV is not loaded.",
    "Balkan provjera je već u tijeku.": "Balkan check is already running.",
    "Označi račun u arhivi.": "Select an account in the archive.",
    "Račun je povučen iz arhive u Xtream Generator.": "Account pulled from archive into Xtream Generator.",
    "Račun je poslan iz arhive u Xtream provjeru.": "Account sent from archive to Xtream check.",
    "Nema isteklih ili neaktivnih računa za micanje.": "No expired or inactive accounts to remove.",
    "Predloženo čišćenje": "Suggested cleanup",
    "isteklih ili neaktivnih računa. Želiš ih obrisati iz arhive?": "expired or inactive accounts. Delete them from the archive?",
    "Označi spremljenu listu.": "Select a saved list.",
    "Lista više ne postoji u bazi.": "The list no longer exists in the database.",
    "Lista je otvorena iz arhive:": "List opened from archive:",
    "U listi nema Xtream URL-ova za provjeru.": "The list contains no Xtream URLs to check.",
    "Poslano URL-ova u provjeru:": "URLs sent to check:",
    "Lista je vraćena u Generator:": "List returned to Generator:",
    "Obriši sve liste": "Delete all lists",
    "Želiš obrisati sve spremljene liste iz arhive?": "Delete all saved lists from the archive?",
    "Obriši sve račune": "Delete all accounts",
    "Želiš obrisati sve račune iz arhive?": "Delete all accounts from the archive?",
    "Export arhive": "Export archive",
    "Backup arhive": "Archive backup",
    "Import arhive": "Import archive",
    "Backup lista": "Backup list",
    "Lista": "List",
    "JSON mora sadržavati listu zapisa.": "JSON must contain a list of records.",
    "Odaberi player": "Choose player",
    "Odaberi export folder": "Choose export folder",
    "Postavke su spremljene.": "Settings saved.",
    "Provjera updatea je već pokrenuta.": "Update check is already running.",
    "Provjeravam GitHub release...": "Checking GitHub release...",
    "Nova verzija dostupna:": "New version available:",
    "Koristiš najnoviju verziju:": "You are using the latest version:",
    "Update provjera nije uspjela:": "Update check failed:",
    "Update je već u tijeku.": "Update is already in progress.",
    "Update nije uspio:": "Update failed:",
    "Update je preuzet. Aurora će se zatvoriti, zamijeniti aplikaciju i ponovno pokrenuti.": "Update downloaded. Aurora will close, replace the application and restart.",
    "Update je preuzet. Aurora će se zatvoriti i pokrenuti novu verziju.": "Update downloaded. Aurora will close and start the new version.",
    "Update se instalira...": "Installing update...",
    "Nova verzija pokrenut će se nakon zatvaranja Aurore.": "The new version will start after Aurora closes.",
    "Instalacija .deb updatea je pokrenuta.": ".deb update installation has started.",
    "Instalacija updatea je pokrenuta. Aurora će se zatvoriti i ponovno pokrenuti nakon uspješne instalacije. Ako sustav zatraži lozinku, potvrdi instalaciju.": "Update installation has started. Aurora will close and restart after successful installation. If the system asks for a password, confirm the installation.",
    "Instalacija updatea je pokrenuta. Ako sustav zatraži lozinku, potvrdi instalaciju i zatim ponovno pokreni Auroru.": "Update installation has started. If the system asks for a password, confirm the installation and then restart Aurora.",
    "Nije pronađen pkexec ni xdg-open za instalaciju .deb paketa.": "Neither pkexec nor xdg-open was found for .deb package installation.",
    "Učitano URL-ova u Balkan IPTV:": "URLs loaded into Balkan IPTV:",
    "Spremi": "Save",
    "Obriši označene": "Delete selected",
    "Obriši sve": "Delete all",
    "Odznači vidljive": "Unselect visible",
    "Osvježi Trezor": "Refresh Vault",
    "Osvježi log": "Refresh log",
    "Označi vidljive": "Select visible",
    "Očisti filtere": "Clear filters",
    "Očisti log": "Clear log",
    "Poveži i povuci grupe": "Connect and pull groups",
    "Pretraži sve servere": "Search all servers",
    "Pronađi": "Find",
    "Provjeri loše": "Check bad",
    "Ukloni Offline": "Remove offline",
    "Ukloni bez Balkana": "Remove without Balkan",
    "Ukloni neispravne": "Remove invalid",
    "Izbriši streamove koji ne rade": "Delete broken streams",
    "Učitaj liste": "Load lists",
    "Prikaži broj stavki (sporo)": "Show item count (slow)",
    "Provjeri linkove nakon exporta": "Check links after export",
    "✨ Smart Merge (Ukloni duplikate, zadrži najbrži ping)": "✨ Smart Merge (remove duplicates, keep fastest ping)",
    "Balkan Pronađen": "Balkan Found",
    "Popis sadržaja (Dvoklik za reprodukciju):": "Content list (double-click to play):",
    "Super-Lista (Skeniraj sve online servere za određeni kanal)": "Super List (scan all online servers for a specific channel)",
    "User-Agent (Emulacija uređaja):": "User-Agent (device emulation):",
    "Mrežne Postavke (Skeniranje)": "Network Settings (Scanning)",
    "Live": "Live",
    "TV Shows": "TV Shows",
    "Skeniraj besplatne Proxyje": "Scan free proxies",
    "Sav sadržaj": "All content",
    "Ističe ≤ 7 dana": "Expires ≤ 7 days",
    "Ističe ≤ 30 dana": "Expires ≤ 30 days",
    "http://host:port  (može i /c)": "http://host:port  (/c is allowed)",
    "Upiši pojam (npr. Arena Sport)": "Enter a term (e.g. Arena Sport)",
    "🔍 Filtriraj pronađene kanale...": "🔍 Filter found channels...",
    "🔍 Pretraži kanale...": "🔍 Search channels...",
    "🔍 Pretraži grupe...": "🔍 Search groups...",
    "Pretraži server, korisnika, Ex-Yu info ili EPG...": "Search server, user, Ex-YU info or EPG...",
    "Test portala": "Test portal",
    "Prekini": "Stop",
    "Makni sve": "Remove all",
    "Valjanost liste": "List validity",
    "Valjanost liste: —": "List validity: —",
    "Valjanost liste: (provjeravam...)": "List validity: (checking...)",
    "Brzina Skeniranja (Broj niti):": "Scan speed (thread count):",
    "Auto adult PIN": "Auto adult PIN",
    "Brzi export (samo Live TV kanali)": "Quick export (Live TV channels only)",
    "Grupa/Kategorija": "Group/Category",
    "Linkova": "Links",
    "Prikaži Balkan grupe/programe": "Show Balkan groups/programs",
    "Balkan grupe i programi": "Balkan groups and programs",
    "Status: Spreman": "Status: Ready",
    "Nema Logotipa": "No logo",
    "Bilješke": "Notes",
    "Zadnja Provjera": "Last check",
    "Pronađeno na Serveru": "Found on server",
    "Sadržaj (L|V|S)": "Content (L|V|S)",
    "Čeka test...": "Waiting for test...",
    "Nema Stalker profila za učitavanje.": "No Stalker profiles to load.",
    "Nema ispravnih profila u tabu Provjera portala.": "No valid profiles in the Portal check tab.",
    "Nema portal/MAC profila za Balkan test.": "No portal/MAC profiles for the Balkan test.",
    "Zaustavljanje Balkan MAC testa...": "Stopping Balkan MAC test...",
    "Balkan MAC test je pokrenut.": "Balkan MAC test has started.",
    "Balkan MAC test nije pokrenut.": "Balkan MAC test is not running.",
    "Balkan MAC test je završen.": "Balkan MAC test has finished.",
    "Dodano Balkan MAC profila:": "Balkan MAC profiles added:",
    "ignorirano MAC adresa bez portala:": "ignored MAC addresses without a portal:",
    "Uklonjeno odabranih Balkan MAC profila:": "Selected Balkan MAC profiles removed:",
    "Nema rezultata za export.": "No results to export.",
}

HR_TRANSLATIONS = {
    "Theme": "Tema",
    "Language": "Jezik",
    "Dark": "Tamna",
    "Light": "Svijetla",
    "English": "Engleski",
    "Hrvatski": "Hrvatski",
    "Export folder:": "Mapa za export:",
    "Content": "Sadržaj",
    "Category": "Kategorija",
    "Time": "Vrijeme",
    "Works": "Radi",
    "Timeout": "Istek vremena",
    "Timeout:": "Istek vremena:",
    "Status: Ready": "Status: Spreman",
    "No logo": "Nema logotipa",
    "Notes": "Bilješke",
    "Last check": "Zadnja provjera",
    "Found on server": "Pronađeno na serveru",
    "Content (L|V|S)": "Sadržaj (L|V|S)",
    "Balkan groups and programs": "Balkan grupe i programi",
    "Show Balkan groups/programs": "Prikaži Balkan grupe/programe",
    "Balkan works": "Radi Balkan",
    "Tested": "Testirano",
    "Samples": "Uzorci",
    "Waiting for test...": "Čeka test...",
    "Status: Spreman": "Status: Spreman",
    "Nema Logotipa": "Nema logotipa",
    "Pronađeno na Serveru": "Pronađeno na serveru",
    "Zadnja Provjera": "Zadnja provjera",
}

BALKAN_EMBED_STYLE = """
QWidget { background: #0b1020; color: #e8ecf6; }
QStackedWidget, QFrame, QGroupBox { background: #0b1020; color: #e8ecf6; }
QFrame#SideBar {
    background: #10172a; border-right: 1px solid #263250;
    min-width: 180px; max-width: 240px;
}
QFrame#StatCard, QGroupBox {
    background: #111a2f; border: 1px solid #263250; border-radius: 14px;
}
QGroupBox {
    margin-top: 12px; padding: 14px 10px 10px 10px; font-weight: 800;
}
QGroupBox::title {
    subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #9eb5e5;
}
QLabel { color: #e8ecf6; }
QListWidget {
    background: #0c1325; border: 1px solid #263250; border-radius: 9px;
    alternate-background-color: #101a30; padding: 4px;
}
QListWidget::item { padding: 6px; border-radius: 5px; }
QListWidget::item:selected { background: #284778; color: #ffffff; }
QPushButton#MenuBtn {
    background: transparent; border: 1px solid transparent; text-align: left;
    min-height: 38px; padding: 9px 14px; border-radius: 8px;
    margin: 3px 8px; color: #dbe6ff; font-weight: 700;
}
QPushButton#MenuBtn:hover { background: #1a2742; border-color: #32415f; color: #ffffff; }
QPushButton#ActionBtn {
    background: #4c7df0; border-color: #6592fa; color: white;
    min-height: 40px; border-radius: 8px; font-weight: 800;
}
QPushButton#ActionBtn:hover { background: #5b89f2; }
QPushButton#StopBtn {
    background: #402137; border-color: #6e304c; color: #ff9eb9;
    min-height: 40px; border-radius: 8px; font-weight: 800;
}
QLineEdit, QTextEdit, QComboBox, QSpinBox {
    min-height: 30px; border-radius: 8px;
}
QTableWidget { border-radius: 9px; }
QSplitter::handle {
    background: #17213a; border: 1px solid #263250; border-radius: 3px;
}
QSplitter::handle:hover { background: #2a3656; }
"""


class FlowLayout(QLayout):
    """Responsive toolbar layout that moves actions onto additional rows."""

    def __init__(
        self,
        parent=None,
        spacing: int = 8,
        align_right: bool = False,
    ):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self._align_right = align_right
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def addStretch(self, _stretch: int = 0) -> None:
        # Flow rows use the remaining horizontal space naturally.
        return

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        if not self._items:
            return QSize()
        widths = [item.sizeHint().width() for item in self._items]
        height = max(item.sizeHint().height() for item in self._items)
        margins = self.contentsMargins()
        return QSize(
            sum(widths)
            + self._spacing * (len(widths) - 1)
            + margins.left()
            + margins.right(),
            height + margins.top() + margins.bottom(),
        )

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(),
            margins.top(),
            -margins.right(),
            -margins.bottom(),
        )
        rows: list[tuple[list[tuple[QLayoutItem, QSize]], int, int]] = []
        row: list[tuple[QLayoutItem, QSize]] = []
        row_width = 0
        row_height = 0
        for item in self._items:
            hint = item.sizeHint().expandedTo(item.minimumSize())
            projected_width = (
                hint.width()
                if not row
                else row_width + self._spacing + hint.width()
            )
            if row and projected_width > effective.width():
                rows.append((row, row_width, row_height))
                row = []
                row_width = 0
                row_height = 0
            if row:
                row_width += self._spacing
            row.append((item, hint))
            row_width += hint.width()
            row_height = max(row_height, hint.height())
        if row:
            rows.append((row, row_width, row_height))

        y = effective.y()
        for row_items, current_width, current_height in rows:
            x = effective.x()
            if self._align_right:
                x += max(0, effective.width() - current_width)
            for index, (item, hint) in enumerate(row_items):
                if not test_only:
                    item.setGeometry(QRect(QPoint(x, y), hint))
                x += hint.width()
                if index + 1 < len(row_items):
                    x += self._spacing
            y += current_height + self._spacing
        if rows:
            y -= self._spacing
        return y - rect.y() + margins.bottom()


def fit_button_text(widget: QPushButton) -> None:
    """Keep the full caption visible instead of allowing layouts to clip it."""

    text_width = widget.fontMetrics().horizontalAdvance(widget.text().replace("&", ""))
    widget.setMinimumWidth(max(104, text_width + 34))
    widget.setMaximumWidth(16777215)
    widget.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
    if widget.text() and not widget.toolTip():
        widget.setToolTip(widget.text())


def button(
    text: str,
    primary: bool = False,
    danger: bool = False,
    tooltip: str = "",
) -> QPushButton:
    widget = QPushButton(text)
    widget.setMinimumHeight(40)
    fit_button_text(widget)
    if tooltip:
        widget.setToolTip(tooltip)
    elif len(text) > 18:
        widget.setToolTip(text)
    if primary:
        widget.setObjectName("Primary")
    if danger:
        widget.setObjectName("Danger")
    return widget


def table(headers: list[str]) -> QTableWidget:
    widget = QTableWidget(0, len(headers))
    widget.setHorizontalHeaderLabels(headers)
    widget.setAlternatingRowColors(True)
    widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    widget.horizontalHeader().setStretchLastSection(True)
    widget.horizontalHeader().setSectionsClickable(True)
    widget.setSortingEnabled(True)
    return widget


@contextmanager
def table_sorting_paused(widget: QTableWidget):
    sorting_enabled = widget.isSortingEnabled()
    if sorting_enabled:
        widget.setSortingEnabled(False)
    try:
        yield
    finally:
        if sorting_enabled:
            widget.setSortingEnabled(True)


def tool_description(text: str) -> QLabel:
    widget = QLabel(text)
    widget.setObjectName("ToolDescription")
    widget.setWordWrap(True)
    return widget


def restyle_long_button(widget: QPushButton, minimum_width: int = 150) -> None:
    widget.setMinimumHeight(36)
    widget.setMinimumWidth(max(minimum_width, widget.minimumWidth()))
    fit_button_text(widget)


def patch_fusion_balkan_detection(fusion_module) -> None:
    scanner_class = getattr(fusion_module, "IPTVScanner", None)
    if not scanner_class or getattr(scanner_class, "_aurora_strict_balkan_detection", False):
        return

    app_class = getattr(fusion_module, "BalkanFusionApp", None)
    original_check_portal = scanner_class.check_portal
    original_score_text = scanner_class.score_text_for_balkan
    original_is_balkan_detected = scanner_class.is_balkan_detected
    original_likely_category_ids = scanner_class.likely_balkan_category_ids

    non_balkan_context_markers = (
        "argentina",
        "arabic",
        "australia",
        "belgium",
        "bolivia",
        "brasil",
        "brazil",
        "canada",
        "chile",
        "colombia",
        "costa rica",
        "ecu",
        "ecuador",
        "el salvador",
        "guatemala",
        "honduras",
        "india",
        "france",
        "french",
        "germany",
        "german",
        "deutsch",
        "deutschland",
        "greece",
        "greek",
        "italia",
        "italy",
        "latam",
        "latin",
        "latino",
        "mexico",
        "nicaragua",
        "norway",
        "panama",
        "paraguay",
        "peru",
        "poland",
        "polska",
        "portugal",
        "puerto rico",
        "republica dominicana",
        "dominicana",
        "romania",
        "russia",
        "spain",
        "sweden",
        "turkey",
        "turkish",
        "turkiye",
        "ukraine",
        "uk",
        "united states",
        "uruguay",
        "usa",
        "venezuela",
    )
    ambiguous_balkan_markers = {
        "SRB": ("rts", "pink", "happy", "b92"),
        "BIH": ("bn tv", "face tv", "hayat"),
        "SLO": ("pop tv",),
        "CG": ("vijesti",),
    }
    ambiguous_marker_set = {
        marker
        for markers in ambiguous_balkan_markers.values()
        for marker in markers
    }
    country_context_markers = {
        "HR": ("hrvatska", "croatia", "croatian", "hrvatski"),
        "SRB": ("srbija", "serbia", "serbian", "srpski"),
        "BIH": ("bosna", "bosnia", "bosnian", "bih"),
        "SLO": ("slovenija", "slovenia", "slovenian", "slovenski"),
        "MKD": ("makedonija", "macedonia", "macedonian"),
        "CG": ("crna gora", "montenegro", "montenegrin"),
        "EXYU": ("ex yu", "exyu", "ex-yu", "balkan", "balkanski", "domaci kanali", "domaci tv"),
    }

    def zero_stats(scanner):
        return {key: 0 for key in scanner.balkan_signals.keys()}

    def has_any_phrase(scanner, normalized: str, phrases: tuple[str, ...] | list[str]) -> bool:
        return any(scanner.has_phrase(normalized, phrase) for phrase in phrases)

    def has_explicit_balkan_signal(scanner, normalized: str) -> bool:
        for signals in scanner.balkan_signals.values():
            for phrase in signals.get("strong", []):
                if scanner.normalize_text(phrase) in ambiguous_marker_set:
                    continue
                if scanner.has_phrase(normalized, phrase):
                    return True
        return scanner.has_exyu_marker(normalized)

    def has_balkan_region_context(scanner, normalized: str) -> bool:
        for phrases in country_context_markers.values():
            if has_any_phrase(scanner, normalized, phrases):
                return True
        return scanner.has_exyu_marker(normalized)

    def has_non_balkan_context(scanner, normalized: str) -> bool:
        return has_any_phrase(scanner, normalized, non_balkan_context_markers)

    def has_ambiguous_balkan_signal(scanner, normalized: str) -> bool:
        return has_any_phrase(scanner, normalized, tuple(ambiguous_marker_set))

    def has_country_context(scanner, normalized: str, country: str) -> bool:
        if scanner.has_exyu_marker(normalized):
            return True
        if has_any_phrase(scanner, normalized, country_context_markers.get("EXYU", ())):
            return True
        return has_any_phrase(scanner, normalized, country_context_markers.get(country, ()))

    def has_clear_country_strong_signal(scanner, normalized: str, country: str) -> bool:
        signals = scanner.balkan_signals.get(country, {})
        for phrase in signals.get("strong", []):
            if scanner.normalize_text(phrase) in ambiguous_marker_set:
                continue
            if scanner.has_phrase(normalized, phrase):
                return True
        return False

    def strict_score_text_for_balkan(self, text, source="category"):
        normalized = self.normalize_text(text)
        stats = original_score_text(self, text, source)
        if not normalized:
            return stats

        if has_non_balkan_context(self, normalized) and not has_balkan_region_context(self, normalized):
            return zero_stats(self)

        for country, signals in self.balkan_signals.items():
            if country == "SPORT":
                continue
            weak_hit = any(self.has_phrase(normalized, phrase) for phrase in signals.get("weak", []))
            if weak_hit and stats.get(country, 0) <= 1:
                stats[country] = 0

        for country, markers in ambiguous_balkan_markers.items():
            if not has_any_phrase(self, normalized, markers):
                continue
            if has_clear_country_strong_signal(self, normalized, country):
                continue
            if has_country_context(self, normalized, country):
                continue
            stats[country] = 0
        return stats

    def strict_is_balkan_detected(self, stats):
        if not isinstance(stats, dict):
            return False
        country_hits = sum(
            1
            for country, score in stats.items()
            if country != "SPORT" and score >= 3
        )
        return country_hits > 0 or stats.get("SPORT", 0) >= 4

    def strict_explain_balkan_match(self, text, source="category"):
        normalized = self.normalize_text(text)
        stats = self.score_text_for_balkan(text, source)
        if not normalized or not self.is_balkan_detected(stats):
            return []

        reasons = []
        if stats.get("EXYU", 0) > 0 and self.has_exyu_marker(normalized):
            reasons.append("EXYU marker")

        for country, signals in self.balkan_signals.items():
            if stats.get(country, 0) <= 0:
                continue
            matches = []
            for phrase in signals.get("strong", []):
                if self.has_phrase(normalized, phrase):
                    matches.append(phrase)
            for phrase in signals.get("weak", []):
                if self.has_phrase(normalized, phrase):
                    matches.append(phrase)
            if matches:
                unique_matches = list(dict.fromkeys(matches))
                reasons.append(f"{country}: {', '.join(unique_matches[:4])}")
            elif country == "SPORT":
                reasons.append("SPORT: regionalni sportski kanal")
        return reasons

    def strict_is_suspicious_balkan_text(self, text):
        normalized = self.normalize_text(text)
        return (
            bool(normalized)
            and has_non_balkan_context(self, normalized)
            and has_ambiguous_balkan_signal(self, normalized)
            and not has_balkan_region_context(self, normalized)
        )

    def strict_likely_balkan_category_ids(self, categories):
        if not isinstance(categories, list):
            return []

        ranked = []
        for category in categories:
            if not isinstance(category, dict):
                continue
            stats = self.score_text_for_balkan(category.get("category_name", ""), source="category")
            if self.is_balkan_detected(stats):
                ranked.append((sum(stats.values()), category.get("category_id")))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [cat_id for _, cat_id in ranked[:4] if cat_id not in (None, "")]

    async def strict_check_portal(self, client, url):
        result = await original_check_portal(self, client, url)
        if (
            not result
            or result.get("status") != "Online"
            or str(result.get("exyu", "")) != "NE"
            or str(result.get("pass", "")).upper() == "MAC"
        ):
            return result

        user_match = re.search(r"username=([^&]+)", str(url))
        pass_match = re.search(r"password=([^&]+)", str(url))
        if not user_match or not pass_match:
            return result

        user, pw = user_match.group(1), pass_match.group(1)
        api_url = f"{self.extract_base_url(url)}/player_api.php?username={user}&password={pw}"
        stats = zero_stats(self)
        live_categories = []
        category_names = {}
        suspicious = []
        for action in ("get_live_categories", "get_vod_categories", "get_series_categories"):
            categories = await self.fetch_json(client, f"{api_url}&action={action}", timeout=10.0)
            if isinstance(categories, list):
                self.merge_stats(stats, self.detect_balkan_from_categories(categories))
                if action == "get_live_categories":
                    live_categories = categories
                for category in categories:
                    if not isinstance(category, dict):
                        continue
                    name = str(category.get("category_name", ""))
                    cat_id = str(category.get("category_id", ""))
                    if cat_id:
                        category_names[cat_id] = name
                    if self.is_suspicious_balkan_text(name):
                        suspicious.append(f"grupa: {name}")

        if self.is_balkan_detected(stats):
            details = [f"{key}:{value}" for key, value in stats.items() if value > 0]
            result["exyu"] = f"DA ({', '.join(details)})" if details else "DA"
            return result

        suspicious_category_ids = []
        for category in live_categories:
            if not isinstance(category, dict):
                continue
            name = str(category.get("category_name", ""))
            normalized = self.normalize_text(name)
            if has_non_balkan_context(self, normalized) or self.is_suspicious_balkan_text(name):
                cat_id = category.get("category_id")
                if cat_id not in (None, ""):
                    suspicious_category_ids.append(str(cat_id))

        for cat_id in suspicious_category_ids[:8]:
            streams = await self.fetch_json(
                client,
                f"{api_url}&action=get_live_streams&category_id={cat_id}",
                timeout=8.0,
            )
            if not isinstance(streams, list):
                continue
            group_name = category_names.get(str(cat_id), "")
            for stream in streams[:350]:
                if not isinstance(stream, dict):
                    continue
                name = str(stream.get("name", stream.get("title", "")))
                text = " ".join(
                    [
                        name,
                        str(stream.get("epg_channel_id", "")),
                        str(stream.get("tvg_id", "")),
                        group_name,
                    ]
                )
                if self.is_suspicious_balkan_text(text):
                    suspicious.append(f"{group_name}: {name}".strip(": "))
                    break

        if suspicious:
            sample = "; ".join(list(dict.fromkeys(suspicious))[:3])
            result["exyu"] = f"SUMNJIVO ({sample})"
        return result

    scanner_class.check_portal = strict_check_portal
    scanner_class.score_text_for_balkan = strict_score_text_for_balkan
    scanner_class.is_balkan_detected = strict_is_balkan_detected
    scanner_class.explain_balkan_match = strict_explain_balkan_match
    scanner_class.is_suspicious_balkan_text = strict_is_suspicious_balkan_text
    scanner_class.likely_balkan_category_ids = strict_likely_balkan_category_ids
    scanner_class._aurora_strict_balkan_detection = True
    scanner_class._aurora_original_check_portal = original_check_portal
    scanner_class._aurora_original_score_text_for_balkan = original_score_text
    scanner_class._aurora_original_is_balkan_detected = original_is_balkan_detected
    scanner_class._aurora_original_likely_category_ids = original_likely_category_ids

    if not app_class or getattr(app_class, "_aurora_strict_balkan_ui", False):
        return

    original_init_load_groups = app_class.init_load_groups
    original_reload_groups_for_type = app_class.reload_groups_for_type
    original_show_first_balkan_program = app_class.show_first_balkan_program
    original_setup_ui = app_class.setup_ui
    original_add_res = app_class.add_res
    original_scan_finished = app_class.scan_finished
    original_close_event = app_class.closeEvent

    def balkan_stats_summary(stats: dict[str, int]) -> str:
        details = [f"{key}:{value}" for key, value in stats.items() if value > 0]
        return ", ".join(details) if details else "DA"

    def add_balkan_evidence_row(
        rows: list[dict[str, str]],
        seen: set[tuple[str, str, str]],
        scanner,
        item_type: str,
        name: str,
        group_name: str,
        text: str,
        source: str,
        stream_url: str = "",
    ) -> bool:
        stats = scanner.score_text_for_balkan(text, source=source)
        if not scanner.is_balkan_detected(stats):
            return False
        key = (item_type, name.strip().lower(), group_name.strip().lower())
        if key in seen:
            return False
        seen.add(key)
        reasons = scanner.explain_balkan_match(text, source)
        rows.append(
            {
                "type": item_type,
                "name": name,
                "group": group_name,
                "score": str(sum(stats.values())),
                "stats": balkan_stats_summary(stats),
                "signals": "; ".join(reasons) if reasons else balkan_stats_summary(stats),
                "stream_url": stream_url,
            }
        )
        return True

    def add_suspicious_evidence_row(
        rows: list[dict[str, str]],
        seen: set[tuple[str, str, str]],
        scanner,
        item_type: str,
        name: str,
        group_name: str,
        text: str,
        stream_url: str = "",
    ) -> bool:
        if not scanner.is_suspicious_balkan_text(text):
            return False
        key = (item_type, name.strip().lower(), group_name.strip().lower())
        if key in seen:
            return False
        seen.add(key)
        normalized = scanner.normalize_text(text)
        markers = [
            marker
            for marker in sorted(ambiguous_marker_set)
            if scanner.has_phrase(normalized, marker)
        ]
        contexts = [
            marker
            for marker in non_balkan_context_markers
            if scanner.has_phrase(normalized, marker)
        ]
        rows.append(
            {
                "type": item_type,
                "name": name,
                "group": group_name,
                "score": "0",
                "stats": "SUMNJIVO",
                "signals": (
                    f"Dvosmisleno: {', '.join(markers[:4])}; "
                    f"ne-Balkan kontekst: {', '.join(contexts[:4])}"
                ),
                "stream_url": stream_url,
            }
        )
        return True

    def fetch_fusion_json(client, url: str, timeout: float = 15.0):
        try:
            response = client.get(url, timeout=timeout, follow_redirects=True)
            if response.status_code != 200:
                return None
            return response.json()
        except Exception:
            return None

    def install_balkan_evidence_button(self) -> None:
        if getattr(self, "_aurora_balkan_evidence_button_installed", False):
            return
        if not hasattr(self, "table") or not self.table.parentWidget():
            return
        parent_layout = self.table.parentWidget().layout()
        if not parent_layout:
            return

        button_row = QHBoxLayout()
        button = QPushButton("Prikaži Balkan grupe/programe")
        button.setToolTip(
            "Za označenu online Xtream listu prikaži kategorije i programe koji su označeni kao Balkan/Ex-YU."
        )
        button.clicked.connect(self.show_balkan_detection_evidence)
        restyle_long_button(button, 250)
        button_row.addWidget(button)
        button_row.addStretch()

        table_index = parent_layout.indexOf(self.table)
        if table_index >= 0:
            parent_layout.insertLayout(table_index, button_row)
        else:
            parent_layout.addLayout(button_row)
        self.btn_balkan_evidence = button
        self._aurora_balkan_evidence_button_installed = True

    def configure_balkan_results_table(self) -> None:
        table = getattr(self, "table", None)
        if not table:
            return

        header = table.horizontalHeader()
        header.setCascadingSectionResizes(False)
        header.setMinimumSectionSize(44)
        header.setMaximumSectionSize(20000)
        header.setSectionsMovable(True)
        header.setStretchLastSection(True)
        for column in range(table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)

        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        widths = {
            0: 96,
            1: 150,
            2: 112,
            3: 112,
            4: 90,
            5: 150,
            6: 135,
            7: 105,
            8: 90,
            9: 95,
            10: 155,
            11: 70,
            12: 140,
            13: 72,
        }
        viewport_width = max(0, table.viewport().width())
        visible_columns = [column for column in range(table.columnCount()) if not table.isColumnHidden(column)]
        base_total = sum(widths.get(column, 100) for column in visible_columns)
        extra = max(0, viewport_width - base_total - 24)
        expansion_columns = [0, 1, 2, 3, 5, 6, 10, 12]
        share = extra // len(expansion_columns) if expansion_columns else 0
        for column in visible_columns:
            width = widths.get(column, 100)
            if column in expansion_columns:
                width += share
            table.setColumnWidth(column, width)

    def strict_add_res(self, d):
        result = original_add_res(self, d)
        if str(d.get("exyu", "")).startswith("SUMNJIVO"):
            row = self.find_result_row(d.get("server", ""), d.get("user", ""), d.get("pass", ""))
            if row >= 0 and self.table.item(row, 5):
                item = self.table.item(row, 5)
                item.setForeground(QColor("#d29922"))
                item.setToolTip("Sumnjiv Balkan signal u ne-Balkan kontekstu. Potvrdi nakon završetka skeniranja.")
        configure_balkan_results_table(self)
        return result

    def confirm_suspicious_balkan_rows(self) -> None:
        suspicious_rows = []
        for row in range(self.table.rowCount()):
            exyu_item = self.table.item(row, 5)
            if exyu_item and exyu_item.text().startswith("SUMNJIVO"):
                suspicious_rows.append(row)
        if not suspicious_rows:
            return

        for row in suspicious_rows:
            server = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
            user = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
            exyu_item = self.table.item(row, 5)
            if not exyu_item or not exyu_item.text().startswith("SUMNJIVO"):
                continue

            answer = QMessageBox.question(
                self,
                "Sumnjiva Balkan provjera",
                (
                    f"Lista je sumnjiva, ali nije automatski označena kao Balkan.\n\n"
                    f"Server: {server}\n"
                    f"Korisnik: {user}\n"
                    f"Signal: {exyu_item.text()}\n\n"
                    "Je li ovo Balkan/Ex-YU lista?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )

            if answer == QMessageBox.StandardButton.Yes:
                exyu_item.setText("DA (ručno potvrđeno)")
                exyu_item.setForeground(QColor("#58a6ff"))
                bg = QColor("#1f2e1f")
                try:
                    self.card_exyu.lbl_val.setText(str(int(self.card_exyu.lbl_val.text()) + 1))
                except Exception:
                    pass
            else:
                exyu_item.setText("NE")
                exyu_item.setForeground(QColor("#c9d1d9"))
                bg = QColor("#2e2e1f")

            for column in range(self.table.columnCount()):
                cell = self.table.item(row, column)
                if cell:
                    cell.setBackground(bg)
            self.update_row_quality(row)

        self.apply_result_filters()
        configure_balkan_results_table(self)

    def strict_scan_finished(self):
        return original_scan_finished(self)

    def stop_fusion_thread(worker, timeout: int = 800) -> None:
        if not worker or not hasattr(worker, "isRunning") or not worker.isRunning():
            return
        if hasattr(worker, "is_running"):
            worker.is_running = False
        if hasattr(worker, "quit"):
            worker.quit()
        if hasattr(worker, "wait"):
            worker.wait(timeout)
        if hasattr(worker, "isRunning") and worker.isRunning() and hasattr(worker, "terminate"):
            worker.terminate()
            worker.wait(300)

    def stop_fusion_background_work(self) -> None:
        self.bulk_stream_queue = []
        self.bulk_stream_active = 0
        for worker_name in ("worker", "vault_worker", "super_thread", "proxy_thread"):
            stop_fusion_thread(getattr(self, worker_name, None))
        for thread in list(getattr(self, "stream_threads", [])):
            stop_fusion_thread(thread)
        self.stream_threads = []
        stalker_window = getattr(self, "stalker_window", None)
        if stalker_window:
            stop_fusion_thread(getattr(stalker_window, "worker", None))
            try:
                stalker_window.close()
            except Exception:
                pass

    def strict_close_event(self, event):
        try:
            stop_fusion_background_work(self)
            original_close_event(self, event)
        except Exception:
            event.accept()

    def strict_setup_ui(self, *args, **kwargs):
        result = original_setup_ui(self, *args, **kwargs)
        install_balkan_evidence_button(self)
        configure_balkan_results_table(self)
        return result

    def show_balkan_detection_evidence(self):
        if not hasattr(self, "table"):
            return
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Balkan provjera", "Odaberi online Xtream listu u rezultatima.")
            return

        status = self.table.item(row, 4).text() if self.table.item(row, 4) else ""
        password = self.table.item(row, 3).text() if self.table.item(row, 3) else ""
        exyu = self.table.item(row, 5).text() if self.table.item(row, 5) else ""
        if status != "Online":
            QMessageBox.warning(self, "Balkan provjera", "Dokazi se mogu dohvatiti samo za online liste.")
            return
        if password.upper() == "MAC" or exyu == "STALKER":
            QMessageBox.warning(self, "Balkan provjera", "Ovaj prikaz trenutno radi za Xtream user/pass liste.")
            return

        server = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
        username = self.table.item(row, 2).text() if self.table.item(row, 2) else ""
        if not server or not username or not password:
            QMessageBox.warning(self, "Balkan provjera", "Nedostaju server, korisnik ili lozinka za odabranu listu.")
            return

        scanner = scanner_class()
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        category_names: dict[str, str] = {}
        suspicious_category_ids: list[str] = []
        api_base = f"{server}/player_api.php?username={username}&password={password}"
        ua = self.combo_ua.currentText().strip() if hasattr(self, "combo_ua") else ""
        headers = {"User-Agent": ua or "Mozilla/5.0"}
        cursor_set = False

        def build_evidence_stream_url(stream: dict) -> str:
            direct_source = str(stream.get("direct_source", "")).strip()
            if direct_source.lower().startswith("ffmpeg "):
                direct_source = direct_source.split(" ", 1)[1].strip()
            if direct_source.startswith(("http://", "https://")):
                return direct_source

            stream_id = stream.get("stream_id") or stream.get("id")
            if not stream_id:
                return ""

            container_ext = str(stream.get("container_extension", "") or "ts").strip().lstrip(".")
            if not container_ext or container_ext.lower() == "none":
                container_ext = "ts"

            user_path = quote(str(username), safe="%")
            password_path = quote(str(password), safe="%")
            stream_path = quote(str(stream_id), safe="%")
            ext_path = quote(container_ext, safe="")
            return f"{server.rstrip('/')}/live/{user_path}/{password_path}/{stream_path}.{ext_path}"

        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            cursor_set = True
            with fusion_module.httpx.Client(verify=False, headers=headers) as client:
                category_actions = (
                    ("Live grupa", "get_live_categories"),
                    ("VOD grupa", "get_vod_categories"),
                    ("Serije grupa", "get_series_categories"),
                )
                live_categories = []
                for item_type, action in category_actions:
                    categories = fetch_fusion_json(client, f"{api_base}&action={action}", timeout=12.0)
                    if not isinstance(categories, list):
                        continue
                    if action == "get_live_categories":
                        live_categories = categories
                    for category in categories:
                        if not isinstance(category, dict):
                            continue
                        name = str(category.get("category_name", "Nepoznato"))
                        cat_id = str(category.get("category_id", ""))
                        if action == "get_live_categories" and cat_id:
                            category_names[cat_id] = name
                        add_balkan_evidence_row(rows, seen, scanner, item_type, name, "", name, "category")
                        if add_suspicious_evidence_row(
                            rows,
                            seen,
                            scanner,
                            f"Sumnjiva {item_type.lower()}",
                            name,
                            "",
                            name,
                        ) and action == "get_live_categories" and cat_id:
                            suspicious_category_ids.append(cat_id)

                candidate_ids = scanner.likely_balkan_category_ids(live_categories)
                stream_sources = [
                    (f"{api_base}&action=get_live_streams&category_id={cat_id}", category_names.get(str(cat_id), ""))
                    for cat_id in suspicious_category_ids[:8]
                ]
                stream_sources.extend(
                    [
                    (f"{api_base}&action=get_live_streams&category_id={cat_id}", category_names.get(str(cat_id), ""))
                    for cat_id in candidate_ids[:6]
                    ]
                )
                stream_sources.append((f"{api_base}&action=get_live_streams", ""))

                for stream_url, fallback_group in stream_sources:
                    streams = fetch_fusion_json(client, stream_url, timeout=15.0)
                    if not isinstance(streams, list):
                        continue
                    for stream in streams[:1500]:
                        if not isinstance(stream, dict):
                            continue
                        name = str(stream.get("name", stream.get("title", "Nepoznato")))
                        category_id = str(stream.get("category_id", ""))
                        group_name = (
                            category_names.get(category_id)
                            or str(stream.get("category_name", ""))
                            or fallback_group
                        )
                        text = " ".join(
                            [
                                name,
                                str(stream.get("epg_channel_id", "")),
                                str(stream.get("tvg_id", "")),
                                group_name,
                            ]
                        )
                        stream_play_url = build_evidence_stream_url(stream)
                        add_balkan_evidence_row(
                            rows,
                            seen,
                            scanner,
                            "Program",
                            name,
                            group_name,
                            text,
                            "stream",
                            stream_play_url,
                        )
                        add_suspicious_evidence_row(
                            rows,
                            seen,
                            scanner,
                            "Sumnjiv program",
                            name,
                            group_name,
                            text,
                            stream_play_url,
                        )
                        if len(rows) >= 200:
                            break
                    if len(rows) >= 200:
                        break
        finally:
            if cursor_set:
                QApplication.restoreOverrideCursor()

        rows.sort(
            key=lambda item: (
                0 if "grupa" in item["type"].lower() else 1,
                -int(item["score"]),
                item["name"].lower(),
            )
        )

        dialog = QDialog(self)
        dialog.setWindowTitle("Balkan grupe i programi")
        dialog.resize(980, 620)
        layout = QVBoxLayout(dialog)
        title = QLabel(
            f"{server} · pronađeno {len(rows)} Balkan/Ex-YU grupa/programa"
            if rows
            else f"{server} · nema jasnih Balkan/Ex-YU grupa/programa"
        )
        title.setWordWrap(True)
        layout.addWidget(title)

        evidence_table = QTableWidget(len(rows), 6)
        evidence_table.setHorizontalHeaderLabels(["Tip", "Naziv", "Grupa", "Score", "Stats", "Signali"])
        evidence_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        evidence_table.verticalHeader().setVisible(False)
        evidence_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        evidence_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for index, item in enumerate(rows):
            values = [item["type"], item["name"], item["group"], item["score"], item["stats"], item["signals"]]
            stream_url = item.get("stream_url", "")
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(Qt.ItemDataRole.UserRole, stream_url)
                if stream_url:
                    cell.setToolTip("Dvoklik pokreće stream u VLC playeru.")
                if column in (0, 3, 4):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                evidence_table.setItem(index, column, cell)

        def play_evidence_stream(table_item: QTableWidgetItem) -> None:
            stream_url = table_item.data(Qt.ItemDataRole.UserRole)
            if not stream_url:
                QMessageBox.information(dialog, "VLC player", "Dvoklik radi samo na redovima programa.")
                return

            player_field = getattr(
                self,
                "txt_player_win" if sys.platform.startswith("win") else "txt_player_lin",
                None,
            )
            player_path = player_field.text().strip() if player_field else ""
            if not player_path:
                player_path = shutil.which("vlc") or ""
                if player_path and player_field:
                    player_field.setText(player_path)

            if not player_path:
                QMessageBox.warning(self, "VLC player", "Podesite putanju do Playera u Balkan IPTV postavkama.")
                return

            try:
                if not sys.platform.startswith("win") and " " in player_path:
                    command = player_path.split()
                    command.append(str(stream_url))
                    subprocess.Popen(command)
                else:
                    subprocess.Popen([player_path, str(stream_url)])
                self.statusBar().showMessage("Stream je poslan u VLC player.", 5000)
            except Exception as error:
                QMessageBox.critical(self, "VLC player", f"VLC nije moguće pokrenuti:\n{error}")

        evidence_table.itemDoubleClicked.connect(play_evidence_stream)
        layout.addWidget(evidence_table)

        def set_suspicious_row_decision(is_balkan: bool) -> None:
            exyu_item = self.table.item(row, 5)
            if not exyu_item or not exyu_item.text().startswith("SUMNJIVO"):
                dialog.accept()
                return

            if is_balkan:
                exyu_item.setText("DA (ručno potvrđeno)")
                exyu_item.setForeground(QColor("#58a6ff"))
                bg = QColor("#1f2e1f")
                try:
                    self.card_exyu.lbl_val.setText(str(int(self.card_exyu.lbl_val.text()) + 1))
                except Exception:
                    pass
            else:
                exyu_item.setText("NE")
                exyu_item.setForeground(QColor("#c9d1d9"))
                bg = QColor("#2e2e1f")

            for column in range(self.table.columnCount()):
                cell = self.table.item(row, column)
                if cell:
                    cell.setBackground(bg)
            self.update_row_quality(row)
            self.apply_result_filters()
            configure_balkan_results_table(self)
            dialog.accept()

        close_row = QHBoxLayout()
        if exyu.startswith("SUMNJIVO"):
            mark_balkan_button = QPushButton("Označi kao Balkan")
            mark_balkan_button.clicked.connect(lambda: set_suspicious_row_decision(True))
            mark_non_balkan_button = QPushButton("Označi kao nije Balkan")
            mark_non_balkan_button.clicked.connect(lambda: set_suspicious_row_decision(False))
            close_row.addWidget(mark_balkan_button)
            close_row.addWidget(mark_non_balkan_button)
        close_row.addStretch()
        close_button = QPushButton("Zatvori")
        close_button.clicked.connect(dialog.accept)
        close_row.addWidget(close_button)
        layout.addLayout(close_row)
        dialog.exec()

    def is_balkan_group_name(text: str) -> bool:
        scanner = scanner_class()
        stats = scanner.score_text_for_balkan(text, source="category")
        return scanner.is_balkan_detected(stats)

    def strict_init_load_groups(self, *args, show_balkan_program=False):
        self._aurora_balkan_group_filter = bool(show_balkan_program)
        return original_init_load_groups(self, *args, show_balkan_program=show_balkan_program)

    def apply_balkan_group_filter(self):
        if not getattr(self, "_aurora_balkan_group_filter", False):
            return

        any_visible = False
        for index in range(self.group_list.count()):
            item = self.group_list.item(index)
            if not item:
                continue
            is_match = is_balkan_group_name(item.text())
            item.setHidden(not is_match)
            any_visible = any_visible or is_match

        if not any_visible:
            self.pbar.setFormat("Balkan/Ex-YU kategorije nisu pronađene u ovoj listi")

    def strict_reload_groups_for_type(self, *args):
        result = original_reload_groups_for_type(self, *args)
        apply_balkan_group_filter(self)
        return result

    def strict_show_first_balkan_program(self):
        scanner = scanner_class()
        ranked_groups = []
        for index in range(self.group_list.count()):
            item = self.group_list.item(index)
            if not item or item.isHidden():
                continue
            stats = scanner.score_text_for_balkan(item.text(), source="category")
            if scanner.is_balkan_detected(stats):
                ranked_groups.append((sum(stats.values()), index))

        if not ranked_groups:
            self.chan_list.clear()
            self.pbar.setFormat("Balkan/Ex-YU kategorije nisu pronađene u ovoj listi")
            return

        ranked_groups.sort(reverse=True)
        group_item = self.group_list.item(ranked_groups[0][1])
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

        if selected_channel:
            self.chan_list.setCurrentItem(selected_channel)
            self.chan_list.scrollToItem(selected_channel)
            self.pbar.setFormat(f"Prikazan Ex-Yu program: {selected_channel.text()}")
        else:
            self.pbar.setFormat("Ex-Yu program nije pronađen u odabranoj Balkan kategoriji")

    app_class.setup_ui = strict_setup_ui
    app_class.add_res = strict_add_res
    app_class.scan_finished = strict_scan_finished
    app_class.closeEvent = strict_close_event
    app_class.stop_background_work = stop_fusion_background_work
    app_class.init_load_groups = strict_init_load_groups
    app_class.reload_groups_for_type = strict_reload_groups_for_type
    app_class.show_first_balkan_program = strict_show_first_balkan_program
    app_class.show_balkan_detection_evidence = show_balkan_detection_evidence
    app_class.confirm_suspicious_balkan_rows = confirm_suspicious_balkan_rows
    app_class._aurora_strict_balkan_ui = True
    app_class._aurora_original_setup_ui = original_setup_ui


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "0"):
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        self.value = QLabel(value)
        self.value.setObjectName("Metric")
        name = QLabel(title)
        name.setObjectName("MetricName")
        layout.addWidget(self.value)
        layout.addWidget(name)


class AuroraWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aurora IPTV")
        self.resize(1440, 900)
        self.setMinimumSize(760, 520)
        self.settings = QSettings("Aurora", "Aurora IPTV")
        self.language = str(self.settings.value("language", "en"))
        if self.language not in UI_TEXT:
            self.language = "en"
        self.theme = str(self.settings.value("theme", "dark"))
        self.default_export_dir = str(self.settings.value("export_dir", str(APP_DIR)))
        self.auto_save_active = str(self.settings.value("auto_save_active", "true")).lower() != "false"
        self.confirm_bulk_actions = str(self.settings.value("confirm_bulk_actions", "true")).lower() != "false"
        self.remember_last_tab = str(self.settings.value("remember_last_tab", "true")).lower() != "false"
        self.check_updates_on_startup = (
            str(self.settings.value("check_updates_on_startup", "true")).lower() != "false"
        )
        self.vault = Vault(APP_DIR / "aurora_vault.db")
        self.scan_worker: XtreamScanWorker | None = None
        self.mac_worker: MacHttpWorker | None = None
        self.stalker_check_worker: StalkerProfileCheckWorker | None = None
        self.stalker_balkan_worker: StalkerBalkanMacWorker | None = None
        self.playlist_worker: PlaylistWorker | None = None
        self.update_check_worker: UpdateCheckWorker | None = None
        self.update_download_worker: UpdateDownloadWorker | None = None
        self.latest_release_url = GITHUB_RELEASES_URL
        self.latest_update_payload: dict | None = None
        self._prompted_update_versions: set[str] = set()
        self.playlist_rows: dict[str, list[dict[str, str]]] = {
            "Live": [],
            "VOD": [],
            "Serije": [],
        }
        self.url_result = None
        self.fusion_window = None
        self.fusion_module = None
        self.stalker_embedded_window = None
        self.stalker_embedded_module = None
        self._build_ui()
        self._load_settings()
        if self.check_updates_on_startup:
            QTimer.singleShot(1200, lambda: self.start_update_check(manual=False))

    def tr_ui(self, key: str) -> str:
        return UI_TEXT.get(self.language, UI_TEXT["en"]).get(key, UI_TEXT["en"].get(key, key))

    def translate_static_text(self, text: str) -> str:
        if not text:
            return text
        if self.language == "en":
            return EN_TRANSLATIONS.get(text, text)
        if self.language == "hr":
            return HR_TRANSLATIONS.get(text, text)
        return text

    def translated_tab_match(self, current: str, target: str) -> bool:
        return current == target or current == self.translate_static_text(target)

    def translate_menu(self, menu: QMenu) -> None:
        for action in menu.actions():
            original = self.original_property(action, "aurora_original_text", action.text())
            action.setText(self.translate_static_text(original))
            submenu = action.menu()
            if submenu:
                self.translate_menu(submenu)

    @staticmethod
    def original_property(widget, name: str, value):
        stored = widget.property(name)
        if stored is None:
            widget.setProperty(name, value)
            return value
        return stored

    def apply_static_translations(self) -> None:
        for widget in self.findChildren(QLabel):
            if widget in {self.subtitle_label, self.connection_label, self.dashboard_heading, self.dashboard_description, self.quick_title, self.quick_text}:
                continue
            original = self.original_property(widget, "aurora_original_text", widget.text())
            widget.setText(self.translate_static_text(original))

        for widget in self.findChildren(QPushButton):
            original = self.original_property(widget, "aurora_original_text", widget.text())
            widget.setText(self.translate_static_text(original))
            tooltip = widget.toolTip()
            if tooltip:
                original_tooltip = self.original_property(widget, "aurora_original_tooltip", tooltip)
                widget.setToolTip(self.translate_static_text(original_tooltip))

        for widget in self.findChildren(QCheckBox):
            original = self.original_property(widget, "aurora_original_text", widget.text())
            widget.setText(self.translate_static_text(original))

        for widget in self.findChildren(QGroupBox):
            original = self.original_property(widget, "aurora_original_title", widget.title())
            widget.setTitle(self.translate_static_text(original))

        for widget_type in (QLineEdit, QTextEdit):
            for widget in self.findChildren(widget_type):
                placeholder = widget.placeholderText()
                original = self.original_property(widget, "aurora_original_placeholder", placeholder)
                widget.setPlaceholderText(self.translate_static_text(original))

        for widget in self.findChildren(QComboBox):
            if widget in {self.setting_theme, self.setting_language}:
                continue
            originals = widget.property("aurora_original_items")
            if originals is None or len(originals) != widget.count():
                originals = [widget.itemText(index) for index in range(widget.count())]
                widget.setProperty("aurora_original_items", originals)
            for index, original in enumerate(originals):
                widget.setItemText(index, self.translate_static_text(original))

        for widget in self.findChildren(QTabWidget):
            if widget is self.tabs:
                continue
            originals = widget.property("aurora_original_tabs")
            if originals is None or len(originals) != widget.count():
                originals = [widget.tabText(index) for index in range(widget.count())]
                widget.setProperty("aurora_original_tabs", originals)
            for index, original in enumerate(originals):
                widget.setTabText(index, self.translate_static_text(original))

        for widget in self.findChildren(QTableWidget):
            originals = widget.property("aurora_original_headers")
            if originals is None or len(originals) != widget.columnCount():
                originals = [
                    widget.horizontalHeaderItem(index).text()
                    if widget.horizontalHeaderItem(index)
                    else ""
                    for index in range(widget.columnCount())
                ]
                widget.setProperty("aurora_original_headers", originals)
            for index, original in enumerate(originals):
                item = widget.horizontalHeaderItem(index)
                if item:
                    item.setText(self.translate_static_text(original))

        for widget in self.findChildren(QAbstractItemView):
            if isinstance(widget, QTableWidget):
                continue
            model = widget.model()
            if not model or not hasattr(model, "setHeaderData"):
                continue
            originals = widget.property("aurora_original_model_headers")
            try:
                column_count = model.columnCount()
            except TypeError:
                continue
            if originals is None or len(originals) != column_count:
                originals = [
                    str(model.headerData(index, Qt.Orientation.Horizontal) or "")
                    for index in range(column_count)
                ]
                widget.setProperty("aurora_original_model_headers", originals)
            for index, original in enumerate(originals):
                model.setHeaderData(
                    index,
                    Qt.Orientation.Horizontal,
                    self.translate_static_text(original),
                )

        for action in self.findChildren(QAction):
            original = self.original_property(action, "aurora_original_text", action.text())
            action.setText(self.translate_static_text(original))

        self.polish_responsive_controls()

    def polish_responsive_controls(self, root: QWidget | None = None) -> None:
        root_widget = root or self
        for button_widget in root_widget.findChildren(QPushButton):
            fit_button_text(button_widget)
        for tabs in root_widget.findChildren(QTabWidget):
            tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)
            tabs.tabBar().setUsesScrollButtons(True)

    def apply_theme(self) -> None:
        QApplication.instance().setStyleSheet(LIGHT_STYLE if self.theme == "light" else STYLE)

    def guide_html(self) -> str:
        if self.theme == "light":
            title_color = "#0b1220"
            accent = "#174bbd"
            text = "#172033"
            muted = "#2d3b50"
        else:
            title_color = "#f5f7ff"
            accent = "#78a6ff"
            text = "#d6def0"
            muted = "#b9c4da"

        if self.language == "hr":
            sections = [
                (
                    "1. Početna",
                    ["Prikazuje osnovnu statistiku rada i ovaj kratki vodič kroz program."],
                ),
                (
                    "2. Xtream Studio",
                    [
                        "<b>Analiza:</b> učitaj TXT, log, JSON ili M3U, izvuci IPTV/M3U linkove, filtriraj ih po serveru i ukloni duplikate.",
                        "<b>Provjera računa:</b> provjerava get.php Xtream linkove i prikazuje status, istek, veze, sadržaj i ping.",
                        "<b>Live/VOD/Series:</b> učitava kanale, filmove i serije. Filtriraj sadržaj, označi programe ili grupe i izvezi M3U.",
                        "<b>Balkan IPTV:</b> traži Balkan/Ex-YU sadržaj, ocjenjuje rezultate i testira streamove za regionalni export.",
                    ],
                ),
                (
                    "3. Stalker Studio",
                    [
                        "<b>Profili:</b> učitaj ili zalijepi portal + MAC profile i otvori odabrani profil u Stalker Studiju.",
                        "<b>URL -> MAC grupiranje:</b> iz neurednog teksta složi portale i pripadajuće MAC adrese.",
                        "<b>Provjera portala:</b> provjerava rade li portal + MAC parovi i prikazuje status/ping.",
                        "<b>Studio · Live / VOD / Series:</b> učitava sadržaj iz Stalker/MAG portala, radi M3U export i dvoklikom odmah pokreće program u VLC-u.",
                    ],
                ),
                (
                    "4. Arhiva",
                    ["Čuva aktivne račune, M3U liste i MAC/Stalker profile te ih vraća u provjeru, generator ili studio."],
                ),
                (
                    "5. Postavke",
                    ["Mreža drži User-Agent i proxy opcije, a VLC / Player putanju do vanjskog playera."],
                ),
            ]
        else:
            sections = [
                (
                    "1. Home",
                    ["Shows basic work statistics and this short guide."],
                ),
                (
                    "2. Xtream Studio",
                    [
                        "<b>Analysis:</b> load TXT, log, JSON or M3U files, extract IPTV/M3U links, filter by server and remove duplicates.",
                        "<b>Account check:</b> checks get.php Xtream links and shows status, expiry, connections, content and ping.",
                        "<b>Live/VOD/Series:</b> loads channels, movies and series. Filter content, select programs or groups and export M3U.",
                        "<b>Balkan IPTV:</b> finds Balkan/Ex-YU content, scores results and tests streams for regional export.",
                    ],
                ),
                (
                    "3. Stalker Studio",
                    [
                        "<b>Profiles:</b> load or paste portal + MAC profiles and open the selected profile in Stalker Studio.",
                        "<b>URL -> MAC grouping:</b> turns messy text into portals with matching MAC addresses.",
                        "<b>Portal check:</b> checks whether portal + MAC pairs work and shows status/ping.",
                        "<b>Studio · Live / VOD / Series:</b> loads content from Stalker/MAG portals, creates M3U exports and opens a program directly in VLC on double-click.",
                    ],
                ),
                (
                    "4. Archive",
                    ["Stores active accounts, M3U lists and MAC/Stalker profiles, then sends them back to checking, generator or studio."],
                ),
                (
                    "5. Settings",
                    ["Network keeps User-Agent and proxy options, while VLC / Player stores the external player path."],
                ),
            ]

        blocks = []
        for title, bullets in sections:
            bullet_html = "".join(
                f"<div style='margin: 3px 0 0 14px; color: {text};'>"
                f"<span style='color: {accent}; font-weight: 800;'>&bull;</span> {bullet}"
                "</div>"
                for bullet in bullets
            )
            blocks.append(
                f"<div style='margin: 0 0 13px 0;'>"
                f"<div style='color: {accent}; font-size: 15px; font-weight: 900;'>{title}</div>"
                f"<div style='color: {muted}; margin-top: 3px;'>{bullet_html}</div>"
                "</div>"
            )
        return (
            f"<div style='color: {title_color}; font-size: 13px; line-height: 1.45;'>"
            + "".join(blocks)
            + "</div>"
        )

    def _build_ui(self) -> None:
        self.apply_theme()
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        title_row = QHBoxLayout()
        title_row.setSpacing(18)
        brand = QVBoxLayout()
        title = QLabel("Aurora IPTV")
        title.setObjectName("Title")
        self.subtitle_label = QLabel(self.tr_ui("subtitle"))
        self.subtitle_label.setObjectName("Subtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setMaximumWidth(300)
        brand.addWidget(title)
        brand.addWidget(self.subtitle_label)
        title_row.addLayout(brand)
        title_row.addStretch(1)

        header_controls = QVBoxLayout()
        header_controls.setSpacing(6)
        header_actions = FlowLayout(spacing=8, align_right=True)
        self.setting_check_updates_startup = QCheckBox(
            "Automatski provjeri update pri pokretanju"
        )
        self.setting_check_updates_startup.setChecked(self.check_updates_on_startup)
        self.setting_check_updates_startup.toggled.connect(
            self.update_startup_preference_changed
        )
        self.header_update_button = button("Provjeri update", primary=True)
        self.header_update_button.clicked.connect(lambda: self.start_update_check(manual=True))
        self.header_donate_button = button(
            "Doniraj preko PayPala",
            tooltip="Otvori PayPal.me stranicu za donaciju.",
        )
        self.header_donate_button.clicked.connect(self.open_paypal_donation)
        header_actions.addWidget(self.setting_check_updates_startup)
        header_actions.addWidget(self.header_update_button)
        header_actions.addWidget(self.header_donate_button)

        preference_row = QHBoxLayout()
        preference_row.setSpacing(8)
        self.setting_theme = QComboBox()
        self.setting_theme.addItem("Dark", "dark")
        self.setting_theme.addItem("Light", "light")
        self.setting_theme.setMaximumWidth(110)
        self.setting_language = QComboBox()
        self.setting_language.addItem("English", "en")
        self.setting_language.addItem("Hrvatski", "hr")
        self.setting_language.setMaximumWidth(120)
        preference_row.addStretch(1)
        preference_row.addWidget(QLabel("Theme"))
        preference_row.addWidget(self.setting_theme)
        preference_row.addWidget(QLabel("Language"))
        preference_row.addWidget(self.setting_language)
        self.connection_label = QLabel(self.tr_ui("ready"))
        self.connection_label.setStyleSheet("color: #62d6a7; font-weight: 700;")
        self.update_status_label = QLabel("Nije još provjereno.")
        self.update_status_label.setObjectName("Subtitle")
        preference_row.addWidget(self.connection_label)
        header_controls.addLayout(header_actions)
        header_controls.addLayout(preference_row)
        header_controls.addWidget(
            self.update_status_label,
            alignment=Qt.AlignmentFlag.AlignRight,
        )
        title_row.addLayout(header_controls)
        root.addLayout(title_row)

        self.tabs = QTabWidget()
        self.add_scrollable_tab(self._dashboard_tab(), self.tr_ui("home"))
        self.add_scrollable_tab(self._xtream_tab(), "Xtream Studio")
        self.add_scrollable_tab(self._stalker_tab(), "Stalker Studio")
        self.add_scrollable_tab(self._vault_tab(), self.tr_ui("archive"))
        self.add_scrollable_tab(self._settings_tab(), self.tr_ui("settings"))
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(self.tr_ui("status_ready"))
        self.setting_theme.currentIndexChanged.connect(self.preview_app_preferences)
        self.setting_language.currentIndexChanged.connect(self.preview_app_preferences)

        file_menu = self.menuBar().addMenu("Datoteka")
        open_action = QAction("Otvori datoteku u URL analizatoru", self)
        open_action.triggered.connect(self.open_analysis_files)
        file_menu.addAction(open_action)
        scan_action = QAction("Učitaj liste u Xtream skener", self)
        scan_action.triggered.connect(self.load_scanner_files)
        file_menu.addAction(scan_action)
        stalker_action = QAction("Pokreni Stalker Studio", self)
        stalker_action.triggered.connect(self.launch_stalker_studio)
        file_menu.addAction(stalker_action)
        file_menu.addSeparator()
        exit_action = QAction("Izlaz", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def add_scrollable_tab(self, page: QWidget, title: str) -> None:
        page.setMinimumWidth(0)
        page.setMinimumHeight(page.sizeHint().height())
        page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(page)
        self.tabs.addTab(scroll, title)

    def select_main_tab(self, title: str) -> None:
        for index in range(self.tabs.count()):
            if self.translated_tab_match(self.tabs.tabText(index), title):
                self.tabs.setCurrentIndex(index)
                return

    def select_xtream_tab(self, title: str) -> None:
        self.select_main_tab("Xtream Studio")
        if not hasattr(self, "xtream_tabs"):
            return
        for index in range(self.xtream_tabs.count()):
            if self.translated_tab_match(self.xtream_tabs.tabText(index), title):
                self.xtream_tabs.setCurrentIndex(index)
                return

    def open_xtream_studio_with_account(self, server: str, username: str, password: str) -> None:
        self.gen_server.setText(server)
        self.gen_user.setText(username)
        self.gen_password.setText(password)
        self.select_xtream_tab("Studio · Live / VOD / Series")

    @staticmethod
    def balkan_checkable_urls(urls: list[str]) -> list[str]:
        result = []
        seen = set()
        for url in urls:
            cleaned = url.strip()
            key = cleaned.lower()
            if not cleaned or key in seen:
                continue
            if "username=" not in key and "mac=" not in key:
                continue
            result.append(cleaned)
            seen.add(key)
        return result

    def send_analysis_to_balkan(self) -> None:
        urls: list[str]
        if self.url_result:
            needle = self.url_filter.text().strip().lower()
            urls = [
                url
                for url in self.url_result.urls
                if not needle
                or needle in url.lower()
                or needle in (urlparse(url).hostname or "").lower()
            ]
        else:
            urls = extract_playlist_urls(
                self.url_input.toPlainText(), playlists_only=False
            ).urls
        self.load_urls_into_balkan(urls)

    def send_scan_to_balkan(self) -> None:
        rows = self.scan_result_rows(visible_only=True)
        urls = [str(row.get("playlist_url", "")) for row in rows if row.get("playlist_url")]
        if not urls:
            urls = extract_playlist_urls(self.scan_input.toPlainText(), playlists_only=False).urls
        self.load_urls_into_balkan(urls)

    def load_urls_into_balkan(self, urls: list[str]) -> None:
        checkable_urls = self.balkan_checkable_urls(urls)
        if not checkable_urls:
            QMessageBox.information(
                self,
                "Balkan IPTV",
                self.translate_static_text("Nema URL-ova za Balkan IPTV."),
            )
            return
        if not self.fusion_window or not hasattr(self.fusion_window, "input_area"):
            QMessageBox.warning(
                self,
                "Balkan IPTV",
                self.translate_static_text("Balkan IPTV nije učitan."),
            )
            return
        worker = getattr(self.fusion_window, "worker", None)
        if worker and worker.isRunning():
            QMessageBox.information(
                self,
                "Balkan IPTV",
                self.translate_static_text("Balkan provjera je već u tijeku."),
            )
            return
        self.fusion_window.input_area.setPlainText("\n".join(checkable_urls))
        if hasattr(self.fusion_window, "stack"):
            self.fusion_window.stack.setCurrentIndex(0)
        self.select_xtream_tab("Balkan IPTV")
        self.statusBar().showMessage(
            f"{self.translate_static_text('Učitano URL-ova u Balkan IPTV:')} {len(checkable_urls)}",
            5000,
        )

    def start_update_check(self, manual: bool = True) -> None:
        if self.update_download_worker and self.update_download_worker.isRunning():
            message = self.translate_static_text("Update je već u tijeku.")
            if hasattr(self, "update_status_label"):
                self.update_status_label.setText(message)
            self.statusBar().showMessage(message, 5000)
            return
        if self.update_check_worker and self.update_check_worker.isRunning():
            message = self.translate_static_text("Provjera updatea je već pokrenuta.")
            if hasattr(self, "update_status_label"):
                self.update_status_label.setText(message)
            if manual:
                self.statusBar().showMessage(message, 5000)
            return
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(
                self.translate_static_text("Provjeravam GitHub release...")
            )
        self.update_check_worker = UpdateCheckWorker(self)
        self.update_check_worker.checked.connect(
            lambda payload: self.update_check_finished(payload, manual)
        )
        self.update_check_worker.failed.connect(
            lambda error: self.update_check_failed(error, manual)
        )
        self.update_check_worker.finished.connect(
            lambda: setattr(self, "update_check_worker", None)
        )
        self.update_check_worker.start()

    def update_check_finished(self, payload: dict, manual: bool) -> None:
        latest = str(payload.get("latest") or "")
        current = str(payload.get("current") or APP_VERSION)
        self.latest_release_url = str(payload.get("url") or GITHUB_RELEASES_URL)
        self.latest_update_payload = payload
        if payload.get("is_newer"):
            message = (
                f"{self.translate_static_text('Nova verzija dostupna:')} {latest} "
                f"(trenutna {current})"
            )
        else:
            message = f"{self.translate_static_text('Koristiš najnoviju verziju:')} {current}"
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(message)
        self.statusBar().showMessage(message, 7000)
        if payload.get("is_newer"):
            self.offer_self_update(payload, manual)
        elif manual:
            QMessageBox.information(self, self.translate_static_text("Ažuriranja"), message)

    def update_check_failed(self, error: str, manual: bool) -> None:
        message = f"{self.translate_static_text('Update provjera nije uspjela:')} {error}"
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(message)
        self.statusBar().showMessage(message, 7000)
        if manual:
            QMessageBox.warning(self, self.translate_static_text("Ažuriranja"), message)

    def open_latest_release(self) -> None:
        webbrowser.open(self.latest_release_url or GITHUB_RELEASES_URL)

    def update_asset_suffixes(self) -> list[str]:
        if sys.platform.startswith("win"):
            return ["windows-x86_64.exe"]
        if sys.platform.startswith("linux"):
            if os.environ.get("APPIMAGE"):
                return ["linux-x86_64.AppImage"]
            executable = Path(sys.executable).resolve()
            if getattr(sys, "frozen", False) and str(executable).startswith("/usr/"):
                return ["linux-amd64.deb", "linux-x86_64.AppImage"]
            if getattr(sys, "frozen", False):
                return ["linux-x86_64.tar.gz", "linux-x86_64.AppImage", "linux-amd64.deb"]
            return ["linux-x86_64.AppImage", "linux-x86_64.tar.gz", "linux-amd64.deb"]
        return []

    def select_update_asset(self, assets: list[dict]) -> dict | None:
        for suffix in self.update_asset_suffixes():
            for asset in assets:
                name = str(asset.get("name") or "")
                if name.lower().endswith(suffix.lower()):
                    return asset
        return None

    def offer_self_update(self, payload: dict, manual: bool) -> None:
        latest = str(payload.get("latest") or "")
        current = str(payload.get("current") or APP_VERSION)
        if not manual and latest in self._prompted_update_versions:
            return
        if latest:
            self._prompted_update_versions.add(latest)

        asset = self.select_update_asset(payload.get("assets", []))
        if not asset:
            message = (
                f"Nova verzija je dostupna: {latest} (trenutna {current}).\n\n"
                "Za ovaj sustav nije pronađen automatski paket. Otvoriti GitHub release?"
            )
            if QMessageBox.question(
                self,
                self.translate_static_text("Ažuriranja"),
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            ) == QMessageBox.StandardButton.Yes:
                self.open_latest_release()
            return

        answer = QMessageBox.question(
            self,
            self.translate_static_text("Ažuriranja"),
            (
                f"Nova verzija je dostupna: {latest}\n"
                f"Trenutna verzija: {current}\n\n"
                f"Paket: {asset.get('name')}\n\n"
                "Želiš preuzeti i instalirati update sada?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.start_self_update(asset)

    def start_self_update(self, asset: dict) -> None:
        if self.update_download_worker and self.update_download_worker.isRunning():
            return
        if hasattr(self, "header_update_button"):
            self.header_update_button.setEnabled(False)
        self.update_download_worker = UpdateDownloadWorker(asset, self)
        self.update_download_worker.progress.connect(self.update_download_progress)
        self.update_download_worker.succeeded.connect(self.update_download_finished)
        self.update_download_worker.failed.connect(self.update_download_failed)
        self.update_download_worker.finished.connect(self.update_download_worker_finished)
        self.update_download_worker.start()

    def update_download_progress(self, percent: int, message: str) -> None:
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(message)
        self.statusBar().showMessage(message, 3000)

    def update_download_worker_finished(self) -> None:
        self.update_download_worker = None
        if hasattr(self, "header_update_button"):
            self.header_update_button.setEnabled(True)

    def update_download_failed(self, error: str) -> None:
        message = f"{self.translate_static_text('Update nije uspio:')} {error}"
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(message)
        self.statusBar().showMessage(message, 7000)
        QMessageBox.critical(self, self.translate_static_text("Ažuriranja"), message)

    def update_download_finished(self, result: dict) -> None:
        try:
            self.install_downloaded_update(result)
        except Exception as error:
            path = str(result.get("path") or "")
            message = (
                f"Update je preuzet, ali automatska instalacija nije uspjela:\n{error}"
            )
            if path:
                message += f"\n\nPreuzeta datoteka:\n{path}"
            if hasattr(self, "update_status_label"):
                self.update_status_label.setText("Update je preuzet, ali nije instaliran.")
            QMessageBox.critical(self, self.translate_static_text("Ažuriranja"), message)

    @staticmethod
    def current_executable_path() -> Path | None:
        if not getattr(sys, "frozen", False):
            return None
        try:
            return Path(sys.executable).resolve()
        except OSError:
            return None

    def install_downloaded_update(self, result: dict) -> None:
        name = str(result.get("name") or "").lower()
        path = Path(str(result.get("path") or ""))
        if not path.exists():
            raise FileNotFoundError(str(path))

        if sys.platform.startswith("win") and name.endswith(".exe"):
            target = self.current_executable_path()
            if not target:
                self.launch_downloaded_update(path)
                return
            self.launch_replacement_update(path, target)
            return

        if sys.platform.startswith("linux") and name.endswith(".appimage"):
            appimage = os.environ.get("APPIMAGE")
            if appimage:
                self.launch_replacement_update(path, Path(appimage).resolve())
            else:
                path.chmod(path.stat().st_mode | 0o755)
                self.launch_downloaded_update(path)
            return

        if sys.platform.startswith("linux") and name.endswith(".tar.gz"):
            target = self.current_executable_path()
            if not target:
                self.open_downloaded_update(path)
                return
            binary = self.extract_portable_update_binary(path)
            self.launch_replacement_update(binary, target, cleanup_paths=[path])
            return

        if sys.platform.startswith("linux") and name.endswith(".deb"):
            self.install_deb_update(path)
            return

        self.open_downloaded_update(path)

    def open_downloaded_update(self, path: Path) -> None:
        message = (
            "Update je preuzet, ali ova pokrenuta verzija ne podržava potpunu "
            f"samoinstalaciju.\n\nDatoteka:\n{path}"
        )
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(f"Update je preuzet: {path}")
        QMessageBox.information(self, self.translate_static_text("Ažuriranja"), message)

    def launch_downloaded_update(self, path: Path) -> None:
        if sys.platform.startswith("win"):
            script = Path(tempfile.gettempdir()) / f"aurora-iptv-restart-{os.getpid()}.bat"
            script.write_text(
                "\n".join(
                    [
                        "@echo off",
                        "setlocal",
                        f"set \"PID={os.getpid()}\"",
                        f"set \"APP={path}\"",
                        ":wait",
                        "tasklist /FI \"PID eq %PID%\" | find \"%PID%\" >nul",
                        "if not errorlevel 1 (",
                        "  timeout /t 1 /nobreak >nul",
                        "  goto wait",
                        ")",
                        "start \"\" \"%APP%\"",
                        "del \"%~f0\" >nul 2>nul",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            self.spawn_update_script(["cmd", "/c", str(script)])
        else:
            script = Path(tempfile.gettempdir()) / f"aurora-iptv-restart-{os.getpid()}.sh"
            script.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "set -eu",
                        f"PID={os.getpid()}",
                        f"APP={shlex.quote(str(path))}",
                        "while kill -0 \"$PID\" 2>/dev/null; do sleep 0.5; done",
                        "chmod +x \"$APP\"",
                        "nohup \"$APP\" >/dev/null 2>&1 &",
                        "rm -f \"$0\"",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | 0o755)
            self.spawn_update_script(["sh", str(script)])
        message = self.translate_static_text(
            "Update je preuzet. Aurora će se zatvoriti i pokrenuti novu verziju."
        )
        QMessageBox.information(
            self,
            self.translate_static_text("Ažuriranja"),
            message,
        )
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(self.translate_static_text("Update se instalira..."))
        self.statusBar().showMessage(
            self.translate_static_text(
                "Nova verzija pokrenut će se nakon zatvaranja Aurore."
            ),
            7000,
        )
        QTimer.singleShot(300, QApplication.instance().quit)

    @staticmethod
    def spawn_update_script(command: list[str]) -> None:
        if sys.platform.startswith("win"):
            flags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
            subprocess.Popen(command, creationflags=flags, close_fds=True)
        else:
            subprocess.Popen(command, start_new_session=True, close_fds=True)

    def launch_replacement_update(
        self,
        source: Path,
        target: Path,
        cleanup_paths: list[Path] | None = None,
    ) -> None:
        cleanup_paths = cleanup_paths or []
        if sys.platform.startswith("win"):
            script = Path(tempfile.gettempdir()) / f"aurora-iptv-update-{os.getpid()}.bat"
            script.write_text(
                "\n".join(
                    [
                        "@echo off",
                        "setlocal",
                        f"set \"PID={os.getpid()}\"",
                        f"set \"SOURCE={source}\"",
                        f"set \"TARGET={target}\"",
                        ":wait",
                        "tasklist /FI \"PID eq %PID%\" | find \"%PID%\" >nul",
                        "if not errorlevel 1 (",
                        "  timeout /t 1 /nobreak >nul",
                        "  goto wait",
                        ")",
                        "copy /Y \"%SOURCE%\" \"%TARGET%\" >nul",
                        "start \"\" \"%TARGET%\"",
                        "del \"%SOURCE%\" >nul 2>nul",
                        "del \"%~f0\" >nul 2>nul",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            self.spawn_update_script(["cmd", "/c", str(script)])
        else:
            script = Path(tempfile.gettempdir()) / f"aurora-iptv-update-{os.getpid()}.sh"
            cleanup = " ".join(shlex.quote(str(path)) for path in cleanup_paths)
            script.write_text(
                "\n".join(
                    [
                        "#!/bin/sh",
                        "set -eu",
                        f"PID={os.getpid()}",
                        f"SOURCE={shlex.quote(str(source))}",
                        f"TARGET={shlex.quote(str(target))}",
                        "while kill -0 \"$PID\" 2>/dev/null; do sleep 0.5; done",
                        "cp -f \"$SOURCE\" \"$TARGET\"",
                        "chmod +x \"$TARGET\"",
                        "nohup \"$TARGET\" >/dev/null 2>&1 &",
                        f"rm -f \"$SOURCE\" {cleanup} \"$0\"",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            script.chmod(script.stat().st_mode | 0o755)
            self.spawn_update_script(["sh", str(script)])

        QMessageBox.information(
            self,
            self.translate_static_text("Ažuriranja"),
            self.translate_static_text(
                "Update je preuzet. Aurora će se zatvoriti, zamijeniti aplikaciju i ponovno pokrenuti."
            ),
        )
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(self.translate_static_text("Update se instalira..."))
        QTimer.singleShot(300, QApplication.instance().quit)

    def extract_portable_update_binary(self, archive_path: Path) -> Path:
        extract_dir = Path(tempfile.mkdtemp(prefix="aurora-iptv-update-"))
        binary_path = extract_dir / "AuroraIPTV"
        with tarfile.open(archive_path, "r:gz") as archive:
            member = next(
                (
                    item
                    for item in archive.getmembers()
                    if item.isfile() and Path(item.name).name == "AuroraIPTV"
                ),
                None,
            )
            if not member:
                raise RuntimeError("U portable arhivi nije pronađen AuroraIPTV.")
            source_file = archive.extractfile(member)
            if not source_file:
                raise RuntimeError("AuroraIPTV nije moguće pročitati iz portable arhive.")
            with open(binary_path, "wb") as handle:
                shutil.copyfileobj(source_file, handle)
        binary_path.chmod(binary_path.stat().st_mode | 0o755)
        return binary_path

    def install_deb_update(self, path: Path) -> None:
        apt = shutil.which("apt") or "/usr/bin/apt"
        app_path = self.current_executable_path() or Path("/usr/bin/AuroraIPTV")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.launch_deb_update_script(path, app_path, [apt, "install", "-y", str(path)])
        else:
            pkexec = shutil.which("pkexec")
            if pkexec:
                self.launch_deb_update_script(path, app_path, [pkexec, apt, "install", "-y", str(path)])
            else:
                opener = shutil.which("xdg-open")
                if not opener:
                    raise RuntimeError(
                        self.translate_static_text(
                            "Nije pronađen pkexec ni xdg-open za instalaciju .deb paketa."
                        )
                    )
                subprocess.Popen([opener, str(path)])
                if hasattr(self, "update_status_label"):
                    self.update_status_label.setText(
                        self.translate_static_text("Instalacija .deb updatea je pokrenuta.")
                    )
                QMessageBox.information(
                    self,
                    self.translate_static_text("Ažuriranja"),
                    self.translate_static_text(
                        "Instalacija updatea je pokrenuta. Ako sustav zatraži lozinku, potvrdi instalaciju i zatim ponovno pokreni Auroru."
                    ),
                )
                return
        if hasattr(self, "update_status_label"):
            self.update_status_label.setText(
                self.translate_static_text("Instalacija .deb updatea je pokrenuta.")
            )
        QMessageBox.information(
            self,
            self.translate_static_text("Ažuriranja"),
            self.translate_static_text(
                "Instalacija updatea je pokrenuta. Aurora će se zatvoriti i ponovno pokrenuti nakon uspješne instalacije. Ako sustav zatraži lozinku, potvrdi instalaciju."
            ),
        )
        QTimer.singleShot(300, QApplication.instance().quit)

    def launch_deb_update_script(
        self,
        package_path: Path,
        app_path: Path,
        install_command: list[str],
    ) -> None:
        script = Path(tempfile.gettempdir()) / f"aurora-iptv-deb-update-{os.getpid()}.sh"
        install_line = " ".join(shlex.quote(part) for part in install_command)
        script.write_text(
            "\n".join(
                [
                    "#!/bin/sh",
                    "set -u",
                    f"PID={os.getpid()}",
                    f"APP={shlex.quote(str(app_path))}",
                    f"PACKAGE={shlex.quote(str(package_path))}",
                    f"INSTALL_CMD={shlex.quote(install_line)}",
                    "while kill -0 \"$PID\" 2>/dev/null; do sleep 0.5; done",
                    "if sh -c \"$INSTALL_CMD\"; then",
                    "  if [ -x \"$APP\" ]; then",
                    "    nohup \"$APP\" >/dev/null 2>&1 &",
                    "  fi",
                    "  rm -f \"$PACKAGE\"",
                    "fi",
                    "rm -f \"$0\"",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | 0o755)
        self.spawn_update_script(["sh", str(script)])

    def open_paypal_donation(self) -> None:
        webbrowser.open(PAYPAL_DONATION_URL)

    def update_startup_preference_changed(self, checked: bool) -> None:
        self.check_updates_on_startup = checked
        self.settings.setValue("check_updates_on_startup", checked)
        self.settings.sync()

    def _dashboard_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 20, 18, 18)
        intro = QFrame()
        intro.setObjectName("Card")
        intro_layout = QVBoxLayout(intro)
        self.dashboard_heading = QLabel(self.tr_ui("dashboard_heading"))
        self.dashboard_heading.setStyleSheet("font-size: 20px; font-weight: 800;")
        self.dashboard_description = QLabel(self.tr_ui("dashboard_description"))
        self.dashboard_description.setWordWrap(True)
        self.dashboard_description.setObjectName("Subtitle")
        intro_layout.addWidget(self.dashboard_heading)
        intro_layout.addWidget(self.dashboard_description)
        layout.addWidget(intro)

        cards = QGridLayout()
        self.metric_urls = MetricCard("Analizirani URL-ovi")
        self.metric_online = MetricCard("Aktivni računi")
        self.metric_macs = MetricCard("Obrađene MAC adrese")
        self.metric_vault = MetricCard("Zapisi u arhivi", str(len(self.vault.rows())))
        for index, card in enumerate(
            [self.metric_urls, self.metric_online, self.metric_macs, self.metric_vault]
        ):
            cards.addWidget(card, index // 2, index % 2)
        layout.addLayout(cards)

        quick = QFrame()
        quick.setObjectName("Card")
        quick_layout = QVBoxLayout(quick)
        quick_layout.setSpacing(12)
        self.quick_title = QLabel(self.tr_ui("quick_start"))
        self.quick_title.setStyleSheet("font-size: 16px; font-weight: 800;")
        quick_layout.addWidget(self.quick_title)
        self.quick_text = QLabel(self.guide_html())
        self.quick_text.setObjectName("GuideText")
        self.quick_text.setTextFormat(Qt.TextFormat.RichText)
        self.quick_text.setWordWrap(True)
        self.quick_text.setStyleSheet("line-height: 1.7;")
        quick_layout.addWidget(self.quick_text)
        layout.addWidget(quick)
        layout.addStretch()
        return page

    def _advanced_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        try:
            fusion_dir = balkan_iptv_source_dir()
            fusion_main = fusion_dir / "main.py"
            fusion_data_dir = APP_DIR
            if str(fusion_dir) not in sys.path:
                sys.path.insert(0, str(fusion_dir))
            spec = importlib.util.spec_from_file_location("aurora_fusion_embedded", fusion_main)
            if not spec or not spec.loader:
                raise RuntimeError("Ne mogu učitati Balkan IPTV modul.")
            self.fusion_module = importlib.util.module_from_spec(spec)
            current_dir = os.getcwd()
            try:
                os.chdir(fusion_data_dir)
                spec.loader.exec_module(self.fusion_module)
            finally:
                os.chdir(current_dir)
            self.fusion_module.STYLE_SHEET = BALKAN_EMBED_STYLE
            patch_fusion_balkan_detection(self.fusion_module)
            self.configure_balkan_paths(fusion_data_dir)
            self.fusion_window = self.fusion_module.BalkanFusionApp()
            self.fusion_window.setWindowTitle("Balkan IPTV")
            self.polish_balkan_module()
            content = self.fusion_window.takeCentralWidget()
            self.polish_balkan_module(content)
            content.setStyleSheet(BALKAN_EMBED_STYLE)
            layout.addWidget(content)
        except Exception as error:
            card = QFrame()
            card.setObjectName("Card")
            card_layout = QVBoxLayout(card)
            title = QLabel("Balkan IPTV modul nije moguće ugraditi")
            title.setStyleSheet("font-size: 20px; font-weight: 800;")
            details = QLabel(str(error))
            details.setWordWrap(True)
            details.setObjectName("Subtitle")
            card_layout.addWidget(title)
            card_layout.addWidget(details)
            layout.addWidget(card)
            layout.addStretch()
        return page

    def configure_balkan_paths(self, fusion_data_dir: Path) -> None:
        if not self.fusion_module:
            return
        self.fusion_module.SETTINGS_FILE = str(fusion_data_dir / "settings.json")
        self.fusion_module.LOG_FILE = str(fusion_data_dir / "fusion.log")
        original_sqlite = self.fusion_module.sqlite3

        class BalkanSqliteProxy:
            def connect(self, database, *args, **kwargs):
                if database == "fusion_vault.db":
                    database = str(fusion_data_dir / "fusion_vault.db")
                return original_sqlite.connect(database, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(original_sqlite, name)

        self.fusion_module.sqlite3 = BalkanSqliteProxy()

    def polish_balkan_module(self, root: QWidget | None = None) -> None:
        if not self.fusion_window and root is None:
            return
        root_widget = root or self.fusion_window
        if hasattr(self.fusion_window, "btn_stalker"):
            self.fusion_window.btn_stalker.setStyleSheet("")
            self.fusion_window.btn_stalker.hide()
        if hasattr(self.fusion_window, "input_area"):
            self.fusion_window.input_area.setPlaceholderText(
                "Ovdje zalijepi M3U/Xtream linkove za Balkan/Ex-YU provjeru..."
            )
        if hasattr(self.fusion_window, "combo_content_type"):
            combo = self.fusion_window.combo_content_type
            combo.setStyleSheet("")
            current = combo.currentIndex()
            combo.clear()
            combo.addItems(["Live TV kanali", "Filmovi (VOD)", "Serije"])
            combo.setCurrentIndex(max(0, current))
        for splitter in root_widget.findChildren(QSplitter):
            splitter.setHandleWidth(6)
        for frame in root_widget.findChildren(QFrame):
            if frame.objectName() == "StatCard":
                frame.setStyleSheet("")
            elif "background-color" in frame.styleSheet() or "background:" in frame.styleSheet():
                frame.setStyleSheet("")
        for label in root_widget.findChildren(QLabel):
            if label.text() == "FUSION PRO":
                label.setText("BALKAN IPTV")
                label.setStyleSheet(
                    "color: #78a6ff; font-weight: 900; font-size: 18px; margin: 18px 14px;"
                )
            elif "Osobni Trezor" in label.text():
                label.setText("Balkan arhiva - spremljene liste i ponovna provjera.")
                label.setStyleSheet("")
            elif label.text().strip() in {"Ukupno Linija", "Online Portali", "Balkan Pronađen"}:
                label.setStyleSheet("color: #8491ad; font-weight: 700;")
            elif label.styleSheet():
                label.setStyleSheet("")
            label.setText(self.clean_balkan_text(label.text()))
        nav_labels = {
            "🏠 Skener": "Balkan skener",
            "📊 Rezultati": "Rezultati",
            "📺 Uređivač Sadržaja": "Uređivač sadržaja",
            "🚀 Globalni Alati": "Super-lista",
            "🛡️ Trezor (Baza)": "Balkan arhiva",
            "⚙️ Postavke": "Postavke",
        }
        for button_widget in root_widget.findChildren(QPushButton):
            button_text = button_widget.text().strip()
            button_text_lower = button_text.lower()
            if button_text in nav_labels:
                button_widget.setText(nav_labels[button_text])
                restyle_long_button(button_widget, 170)
            elif "najbolj" in button_text_lower and "kandidat" in button_text_lower:
                button_widget.hide()
            elif "random" in button_text_lower and "stream" in button_text_lower:
                button_widget.setText("Test streamova")
                button_widget.setToolTip("Provjeri streamove u svim listama.")
                restyle_long_button(button_widget, 170)
            elif button_text == "POKRENI PRECIZNI SKENER":
                button_widget.setText("Pokreni Balkan provjeru")
                button_widget.setToolTip("Provjeri Xtream/M3U liste i označi gdje je pronađen Balkan/Ex-YU sadržaj.")
                restyle_long_button(button_widget, 220)
            elif button_text == "💾 EXPORT ODABRANIH U M3U":
                button_widget.setText("Export odabranih u M3U")
                restyle_long_button(button_widget, 210)
            elif button_text == "💾 EXPORTAJ ODABRANE":
                button_widget.setText("Export odabranih")
                restyle_long_button(button_widget, 180)
            else:
                button_widget.setText(self.clean_balkan_text(button_text))
            if button_widget.styleSheet():
                button_widget.setStyleSheet("")
            fit_button_text(button_widget)
        for table_widget in root_widget.findChildren(QTableWidget):
            table_widget.setAlternatingRowColors(True)
            for column in range(table_widget.columnCount()):
                header = table_widget.horizontalHeaderItem(column)
                if header and header.text().strip().lower() == "odaberi":
                    table_widget.setColumnHidden(column, True)
            self.configure_balkan_table_columns(table_widget)

    @staticmethod
    def configure_balkan_table_columns(table_widget: QTableWidget) -> None:
        headers = [
            table_widget.horizontalHeaderItem(column).text().strip().lower()
            if table_widget.horizontalHeaderItem(column)
            else ""
            for column in range(table_widget.columnCount())
        ]
        if "ex-yu info" not in headers or "stream test" not in headers:
            return

        header = table_widget.horizontalHeader()
        header.setCascadingSectionResizes(False)
        header.setMinimumSectionSize(44)
        header.setMaximumSectionSize(20000)
        header.setSectionsMovable(True)
        header.setStretchLastSection(True)
        for column in range(table_widget.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)

        table_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table_widget.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table_widget.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table_widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)

        widths = {
            0: 110,
            1: 170,
            2: 130,
            3: 130,
            4: 90,
            5: 160,
            6: 145,
            7: 110,
            8: 90,
            9: 100,
            10: 175,
            11: 70,
            12: 155,
            13: 72,
        }
        visible_columns = [
            column
            for column in range(table_widget.columnCount())
            if not table_widget.isColumnHidden(column)
        ]
        base_total = sum(widths.get(column, 100) for column in visible_columns)
        extra = max(0, table_widget.viewport().width() - base_total - 24)
        expansion_columns = [column for column in (0, 1, 2, 3, 5, 6, 10, 12) if column in visible_columns]
        share = extra // len(expansion_columns) if expansion_columns else 0
        for column in visible_columns:
            width = widths.get(column, 100)
            if column in expansion_columns:
                width += share
            table_widget.setColumnWidth(column, width)

    @staticmethod
    def clean_balkan_text(text: str) -> str:
        replacements = {
            "FUSION PRO": "BALKAN IPTV",
            "Uređivač Sadržaja": "Uređivač sadržaja",
            "Globalni Alati": "Super-lista",
            "Trezor (Baza)": "Balkan arhiva",
            "POKRENI PRECIZNI SKENER": "Pokreni Balkan provjeru",
            "EXPORT ODABRANIH U M3U": "Export odabranih u M3U",
            "EXPORTAJ ODABRANE": "Export odabranih",
        }
        cleaned = re.sub(r"^[^\wČĆŽŠĐčćžšđ]+", "", text).strip()
        return replacements.get(cleaned, cleaned)

    def _analysis_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        subtabs = QTabWidget()
        subtabs.addTab(self._url_extractor(), "URL / M3U Extractor")
        layout.addWidget(subtabs)
        return page

    def _url_extractor(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Izvlači IPTV i M3U URL-ove iz teksta ili datoteka, uklanja duplikate i "
                "pomaže brzo pronaći servere."
            )
        )
        controls = FlowLayout()
        self.url_only_playlists = QCheckBox("Samo IPTV/M3U")
        self.url_only_playlists.setChecked(True)
        self.url_m3u8 = QCheckBox("Uključi M3U8")
        self.url_m3u8.setChecked(True)
        self.url_query = QCheckBox("Prepoznaj query parametre")
        self.url_query.setChecked(True)
        self.url_dedupe = QCheckBox("Ukloni duplikate")
        self.url_dedupe.setChecked(True)
        self.url_sort = QCheckBox("Sortiraj po serveru")
        self.url_hosts_only = QCheckBox("Samo serveri")
        self.url_group_hosts = QCheckBox("Grupiraj po serveru")
        for widget in [
            self.url_only_playlists,
            self.url_m3u8,
            self.url_query,
            self.url_dedupe,
            self.url_sort,
            self.url_hosts_only,
            self.url_group_hosts,
        ]:
            controls.addWidget(widget)
        self.url_hosts_only.toggled.connect(
            lambda checked: (
                self.url_group_hosts.setChecked(False) if checked else None,
                self.render_url_results(),
            )
        )
        self.url_group_hosts.toggled.connect(
            lambda checked: (
                self.url_hosts_only.setChecked(False) if checked else None,
                self.render_url_results(),
            )
        )
        controls.addStretch()
        open_btn = button("Otvori")
        open_btn.clicked.connect(self.open_analysis_files)
        add_btn = button("Dodaj datoteke")
        add_btn.clicked.connect(lambda: self.open_analysis_files(append=True))
        run_btn = button("Izvuci URL-ove", primary=True)
        run_btn.clicked.connect(self.run_url_extraction)
        controls.addWidget(open_btn)
        controls.addWidget(add_btn)
        controls.addWidget(run_btn)
        layout.addLayout(controls)
        filter_row = QHBoxLayout()
        self.url_filter = QLineEdit()
        self.url_filter.setPlaceholderText("Filtriraj rezultate po URL-u ili serveru...")
        self.url_filter.textChanged.connect(self.render_url_results)
        filter_row.addWidget(self.url_filter, 1)
        report_btn = button(
            "Izvještaj",
            tooltip="Prikaže sažetak pronađenih URL-ova, duplikata, servera i M3U stavki.",
        )
        report_btn.clicked.connect(self.show_url_report)
        open_link_btn = button(
            "Otvori označeni link",
            tooltip="Otvori URL koji je označen ili je u redu gdje stoji kursor.",
        )
        open_link_btn.clicked.connect(self.open_selected_url)
        clear_btn = button("Očisti", danger=True)
        clear_btn.clicked.connect(self.clear_url_analysis)
        filter_row.addWidget(report_btn)
        filter_row.addWidget(open_link_btn)
        filter_row.addWidget(clear_btn)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText("Zalijepi tekst, log, JSON ili M3U sadržaj...")
        self.url_output = QTextEdit()
        self.url_output.setReadOnly(True)
        self.url_output.setPlaceholderText("Očišćeni jedinstveni URL-ovi pojavit će se ovdje.")
        self.url_output.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.url_output.customContextMenuRequested.connect(self.url_output_menu)
        splitter.addWidget(self.url_input)
        splitter.addWidget(self.url_output)
        splitter.setSizes([650, 650])
        layout.addWidget(splitter, 1)
        footer = FlowLayout()
        self.url_stats = QLabel("URL-ovi: 0 · Duplikati: 0 · Serveri: 0")
        self.url_stats.setObjectName("Subtitle")
        footer.addWidget(self.url_stats)
        footer.addStretch()
        copy_btn = button("Kopiraj")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.url_output.toPlainText()))
        save_btn = button("Spremi TXT")
        save_btn.clicked.connect(lambda: self.save_text(self.url_output.toPlainText(), "urlovi.txt"))
        export_btn = button(
            "Export čistih URL-ova",
            tooltip="Spremi samo očišćene jedinstvene URL-ove, bez dodatnog teksta iz ulaza.",
        )
        export_btn.clicked.connect(self.export_clean_urls)
        footer.addWidget(copy_btn)
        footer.addWidget(save_btn)
        save_archive_btn = button(
            "Spremi listu u arhivu",
            tooltip="Spremi trenutni rezultat u bazu kako bi ga kasnije mogao otvoriti iz Arhive.",
        )
        save_archive_btn.clicked.connect(self.save_url_results_to_vault)
        send_balkan_btn = button(
            "Učitaj u Balkan IPTV",
            tooltip="Prebaci vidljive URL-ove u Balkan IPTV skener bez kopiranja.",
        )
        send_balkan_btn.clicked.connect(self.send_analysis_to_balkan)
        footer.addWidget(save_archive_btn)
        footer.addWidget(send_balkan_btn)
        footer.addWidget(export_btn)
        layout.addLayout(footer)
        return page

    def _mac_grouper(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Grupira MAC adrese po pripadajućem portalu kako bi se profili lakše "
                "pregledali, kopirali ili poslali u Stalker Studio."
            )
        )
        controls = FlowLayout()
        self.mac_global = QCheckBox("Globalno ukloni duplikate")
        self.mac_sort_urls = QCheckBox("Sortiraj URL-ove")
        self.mac_sort_values = QCheckBox("Sortiraj MAC adrese")
        controls.addWidget(self.mac_global)
        controls.addWidget(self.mac_sort_urls)
        controls.addWidget(self.mac_sort_values)
        controls.addStretch()
        open_btn = button("Učitaj TXT")
        open_btn.clicked.connect(lambda: self.load_text_into(self.mac_group_input, append=False))
        add_btn = button("Dodaj TXT")
        add_btn.clicked.connect(lambda: self.load_text_into(self.mac_group_input, append=True))
        run_btn = button("Grupiraj", primary=True)
        run_btn.clicked.connect(self.run_mac_grouping)
        check_urls_btn = button("Provjeri URL format")
        check_urls_btn.clicked.connect(self.check_mac_group_urls)
        send_check_top_btn = button(
            "Dodaj sve u Provjeru portala",
            tooltip="Dodaj sve URL/MAC parove iz grupiranja u tab Provjera portala bez ručnog kopiranja.",
        )
        send_check_top_btn.clicked.connect(self.send_mac_groups_to_stalker_check)
        controls.addWidget(open_btn)
        controls.addWidget(add_btn)
        controls.addWidget(run_btn)
        controls.addWidget(check_urls_btn)
        controls.addWidget(send_check_top_btn)
        layout.addLayout(controls)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.mac_group_input = QTextEdit()
        self.mac_group_input.setPlaceholderText("URL pa pripadajuće MAC adrese, redak po redak...")
        self.mac_group_output = QTextEdit()
        self.mac_group_output.setReadOnly(True)
        self.mac_group_output.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mac_group_output.customContextMenuRequested.connect(self.mac_group_output_menu)
        splitter.addWidget(self.mac_group_input)
        splitter.addWidget(self.mac_group_output)
        layout.addWidget(splitter, 1)
        self.mac_group_table = table(["Portal URL", "MAC adresa"])
        self.mac_group_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mac_group_table.customContextMenuRequested.connect(self.mac_group_table_menu)
        layout.addWidget(self.mac_group_table, 1)
        footer = FlowLayout()
        self.mac_group_stats = QLabel("Grupe: 0 · MAC: 0")
        self.mac_group_stats.setObjectName("Subtitle")
        footer.addWidget(self.mac_group_stats)
        footer.addStretch()
        copy_btn = button("Kopiraj")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self.mac_group_output.toPlainText())
        )
        save_btn = button("Spremi izlaz")
        save_btn.clicked.connect(
            lambda: self.save_text(self.mac_group_output.toPlainText(), "url_mac_grupe.txt")
        )
        clear_btn = button("Očisti", danger=True)
        clear_btn.clicked.connect(
            lambda: (
                self.mac_group_input.clear(),
                self.mac_group_output.clear(),
                self.mac_group_table.setRowCount(0),
            )
        )
        footer.addWidget(copy_btn)
        footer.addWidget(save_btn)
        save_archive_btn = button(
            "Spremi grupe u arhivu",
            tooltip="Spremi grupirane URL/MAC profile u bazu za kasnije korištenje.",
        )
        save_archive_btn.clicked.connect(self.save_mac_groups_to_vault)
        send_check_btn = button(
            "Dodaj sve u Provjeru portala",
            tooltip="Dodaj sve URL/MAC parove iz grupiranja u tab Provjera portala bez ručnog kopiranja.",
        )
        send_check_btn.clicked.connect(self.send_mac_groups_to_stalker_check)
        footer.addWidget(save_archive_btn)
        footer.addWidget(send_check_btn)
        footer.addWidget(clear_btn)
        layout.addLayout(footer)
        return page

    def _verification_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._xtream_scanner())
        return page

    def _xtream_scanner(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Provjerava Xtream račune iz get.php URL-ova i prikazuje status, istek, "
                "broj veza, sadržaj i ping."
            )
        )
        top = FlowLayout()
        self.scan_threads = QSpinBox()
        self.scan_threads.setRange(1, 30)
        self.scan_threads.setValue(8)
        self.scan_timeout = QSpinBox()
        self.scan_timeout.setRange(3, 60)
        self.scan_timeout.setValue(12)
        top.addWidget(QLabel("Paralelno:"))
        top.addWidget(self.scan_threads)
        top.addWidget(QLabel("Timeout:"))
        top.addWidget(self.scan_timeout)
        load_btn = button("Učitaj TXT/M3U")
        load_btn.clicked.connect(self.load_scanner_files)
        add_btn = button("Dodaj datoteke")
        add_btn.clicked.connect(lambda: self.load_scanner_files(append=True))
        top.addWidget(load_btn)
        top.addWidget(add_btn)
        top.addStretch()
        self.scan_start = button("Pokreni provjeru", primary=True)
        self.scan_start.clicked.connect(self.toggle_xtream_scan)
        top.addWidget(self.scan_start)
        layout.addLayout(top)
        self.scan_input = QTextEdit()
        self.scan_input.setMaximumHeight(135)
        self.scan_input.setPlaceholderText(
            "Jedan ili više get.php URL-ova s username i password parametrima..."
        )
        layout.addWidget(self.scan_input)
        self.scan_progress = QProgressBar()
        self.scan_progress.setValue(0)
        layout.addWidget(self.scan_progress)
        filters = QHBoxLayout()
        self.scan_filter = QLineEdit()
        self.scan_filter.setPlaceholderText("Filtriraj server, korisnika, status ili sadržaj...")
        self.scan_filter.textChanged.connect(self.filter_scan_results)
        self.scan_status_filter = QComboBox()
        self.scan_status_filter.addItems(["Svi statusi", "Samo aktivni", "Samo neaktivni"])
        self.scan_status_filter.currentIndexChanged.connect(self.filter_scan_results)
        filters.addWidget(self.scan_filter, 1)
        filters.addWidget(self.scan_status_filter)
        layout.addLayout(filters)
        self.scan_table = table(
            ["Status", "Server", "Korisnik", "Lozinka", "Ističe", "Veze", "Sadržaj", "Ping"]
        )
        self.scan_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.scan_table.customContextMenuRequested.connect(self.scan_context_menu)
        layout.addWidget(self.scan_table, 1)
        actions = QVBoxLayout()
        hint_row = QHBoxLayout()
        hint = QLabel("Desni klik na red za spremanje u arhivu.")
        hint.setObjectName("Subtitle")
        hint_row.addWidget(hint)
        hint_row.addStretch()
        actions.addLayout(hint_row)
        export_row = FlowLayout()
        cleanup_row = FlowLayout()
        export_txt = button("Export TXT")
        export_txt.clicked.connect(lambda: self.export_scan_results("txt"))
        export_csv = button("Export CSV")
        export_csv.clicked.connect(lambda: self.export_scan_results("csv"))
        export_json = button("Export JSON")
        export_json.clicked.connect(lambda: self.export_scan_results("json"))
        export_m3u = button(
            "Export aktivnih M3U",
            tooltip="Napravi M3U listu samo od računa koji su u provjeri označeni kao aktivni.",
        )
        export_m3u.clicked.connect(self.export_active_accounts_m3u)
        save_m3u = button(
            "Spremi aktivne u arhivu",
            tooltip="Spremi M3U listu aktivnih računa u bazu bez pisanja datoteke.",
        )
        save_m3u.clicked.connect(self.save_active_accounts_m3u_to_vault)
        send_generator_btn = button(
            "Pošalji u Generator",
            tooltip="Prebaci označeni aktivni račun u Live/VOD/Series generator.",
        )
        send_generator_btn.clicked.connect(self.send_selected_scan_to_generator)
        send_balkan_btn = button(
            "Učitaj u Balkan IPTV",
            tooltip="Prebaci vidljive URL-ove u Balkan IPTV skener bez kopiranja.",
        )
        send_balkan_btn.clicked.connect(self.send_scan_to_balkan)
        remove_offline = button("Ukloni neaktivne")
        remove_offline.clicked.connect(self.remove_offline_results)
        remove_duplicates = button("Ukloni duplikate")
        remove_duplicates.clicked.connect(self.remove_duplicate_scan_results)
        clear_btn = button("Očisti rezultate", danger=True)
        clear_btn.clicked.connect(lambda: self.scan_table.setRowCount(0))
        export_row.addWidget(export_txt)
        export_row.addWidget(export_csv)
        export_row.addWidget(export_json)
        export_row.addWidget(export_m3u)
        export_row.addWidget(save_m3u)
        export_row.addWidget(send_generator_btn)
        export_row.addWidget(send_balkan_btn)
        export_row.addStretch()
        cleanup_row.addWidget(remove_offline)
        cleanup_row.addWidget(remove_duplicates)
        cleanup_row.addWidget(clear_btn)
        cleanup_row.addStretch()
        actions.addLayout(export_row)
        actions.addLayout(cleanup_row)
        layout.addLayout(actions)
        return page

    def _mac_http_checker(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Šalje MAC adrese na ovlašteni HTTP endpoint i bilježi koje adrese "
                "dobivaju uspješan odgovor."
            )
        )
        form_frame = QFrame()
        form_frame.setObjectName("Card")
        form = QFormLayout(form_frame)
        self.mac_check_url = QLineEdit()
        self.mac_check_url.setPlaceholderText("https://vlastiti-server.example/check")
        self.mac_mode = QComboBox()
        self.mac_mode.addItems(["Query", "Header", "Cookie"])
        self.mac_field = QLineEdit("mac")
        self.mac_timeout = QSpinBox()
        self.mac_timeout.setRange(2, 60)
        self.mac_timeout.setValue(8)
        self.mac_success = QLineEdit()
        self.mac_success.setPlaceholderText("Opcionalni tekst koji odgovor mora sadržavati")
        form.addRow("Endpoint:", self.mac_check_url)
        form.addRow("Način slanja:", self.mac_mode)
        form.addRow("Naziv polja:", self.mac_field)
        form.addRow("Timeout:", self.mac_timeout)
        form.addRow("Tekst uspjeha:", self.mac_success)
        layout.addWidget(form_frame)
        self.mac_check_input = QTextEdit()
        self.mac_check_input.setMaximumHeight(115)
        self.mac_check_input.setPlaceholderText("MAC adrese, jedna po retku...")
        layout.addWidget(self.mac_check_input)
        controls = FlowLayout()
        note = QLabel("Namijenjeno isključivo endpointima za koje imaš ovlaštenje.")
        note.setObjectName("Subtitle")
        controls.addWidget(note)
        controls.addStretch()
        load_btn = button("Učitaj MAC TXT")
        load_btn.clicked.connect(lambda: self.load_text_into(self.mac_check_input, append=False))
        add_btn = button("Dodaj MAC TXT")
        add_btn.clicked.connect(lambda: self.load_text_into(self.mac_check_input, append=True))
        controls.addWidget(load_btn)
        controls.addWidget(add_btn)
        self.mac_start = button("Pokreni MAC provjeru", primary=True)
        self.mac_start.clicked.connect(self.toggle_mac_scan)
        controls.addWidget(self.mac_start)
        layout.addLayout(controls)
        self.mac_progress = QProgressBar()
        layout.addWidget(self.mac_progress)
        self.mac_table = table(["MAC adresa", "Radi", "Status", "Vrijeme"])
        layout.addWidget(self.mac_table, 1)
        bottom = FlowLayout()
        bottom.addStretch()
        export_btn = button("Export rezultata CSV")
        export_btn.clicked.connect(self.export_mac_results)
        save_archive_btn = button(
            "Spremi rezultate u arhivu",
            tooltip="Spremi tablicu MAC provjere u bazu za kasniji pregled ili export.",
        )
        save_archive_btn.clicked.connect(self.save_mac_results_to_vault)
        clear_btn = button("Očisti", danger=True)
        clear_btn.clicked.connect(lambda: self.mac_table.setRowCount(0))
        bottom.addWidget(export_btn)
        bottom.addWidget(save_archive_btn)
        bottom.addWidget(clear_btn)
        layout.addLayout(bottom)
        return page

    def _generator_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Učitava Live, VOD i serije s Xtream računa, filtrira sadržaj i izvozi "
                "odabrane stavke u M3U listu."
            )
        )
        credentials = QFrame()
        credentials.setObjectName("Card")
        form = QVBoxLayout(credentials)
        form.setContentsMargins(10, 10, 10, 10)
        form.setSpacing(8)
        self.gen_server = QLineEdit()
        self.gen_server.setPlaceholderText("http://server:port")
        self.gen_user = QLineEdit()
        self.gen_user.setMinimumWidth(150)
        self.gen_password = QLineEdit()
        self.gen_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.gen_password.setMinimumWidth(150)
        self.gen_full_url = QLineEdit()
        self.gen_full_url.setPlaceholderText(
            "Zalijepi cijeli get.php link s username i password parametrima..."
        )
        parse_url_btn = button(
            "Iščitaj link",
            tooltip="Iz cijelog Xtream get.php linka popuni server, korisnika i lozinku.",
        )
        parse_url_btn.clicked.connect(self.fill_generator_from_url)
        load_m3u_btn = button(
            "Učitaj M3U",
            tooltip="Učitaj lokalnu M3U/M3U8 listu i zadrži originalne nazive grupa.",
        )
        load_m3u_btn.clicked.connect(self.load_generator_m3u_file)
        clear_generator_btn = button("Očisti sve", danger=True)
        clear_generator_btn.clicked.connect(self.clear_generator)
        self.gen_load = button("Učitaj sadržaj", primary=True)
        self.gen_load.clicked.connect(self.load_playlist)
        self.gen_load_all = button(
            "Učitaj sve",
            tooltip="Učitaj Live, VOD i Serije redom iz istog Xtream računa.",
        )
        self.gen_load_all.clicked.connect(self.load_all_playlist_types)
        account_row = QGridLayout()
        account_row.setSpacing(6)
        account_row.addWidget(QLabel("Server"), 0, 0)
        account_row.addWidget(self.gen_server, 0, 1, 1, 3)
        account_row.addWidget(QLabel("Korisnik"), 1, 0)
        account_row.addWidget(self.gen_user, 1, 1)
        account_row.addWidget(QLabel("Lozinka"), 1, 2)
        account_row.addWidget(self.gen_password, 1, 3)
        account_row.setColumnStretch(1, 1)
        account_row.setColumnStretch(3, 1)
        form.addLayout(account_row)

        link_input_row = QHBoxLayout()
        link_input_row.setSpacing(6)
        link_input_row.addWidget(QLabel("Cijeli link"))
        link_input_row.addWidget(self.gen_full_url, 1)
        form.addLayout(link_input_row)
        link_actions = FlowLayout()
        link_actions.addWidget(self.gen_load)
        link_actions.addWidget(self.gen_load_all)
        link_actions.addWidget(parse_url_btn)
        link_actions.addWidget(load_m3u_btn)
        link_actions.addWidget(clear_generator_btn)
        form.addLayout(link_actions)
        layout.addWidget(credentials)
        filter_row = QHBoxLayout()
        self.gen_filter = QLineEdit()
        self.gen_filter.setPlaceholderText("Filtriraj po nazivu kanala ili kategoriji...")
        self.gen_filter.textChanged.connect(self.filter_playlist)
        filter_row.addWidget(self.gen_filter, 1)
        layout.addLayout(filter_row)
        export_actions = FlowLayout()
        export_btn = button(
            "Export M3U",
            primary=True,
            tooltip="Izvezi samo stavke koje su trenutno vidljive nakon filtera i odabira grupe.",
        )
        export_btn.clicked.connect(lambda: self.export_playlist(False))
        export_selected_btn = button(
            "Export označeno",
            tooltip="Izvezi samo programe označene checkboxom.",
        )
        export_selected_btn.clicked.connect(lambda: self.export_playlist(True))
        save_visible_btn = button(
            "Spremi prikazano",
            tooltip="Spremi trenutno filtriranu listu u bazu bez exporta u datoteku.",
        )
        save_visible_btn.clicked.connect(lambda: self.save_generator_list(False))
        save_selected_btn = button(
            "Spremi označeno",
            tooltip="Spremi samo programe označene checkboxom.",
        )
        save_selected_btn.clicked.connect(lambda: self.save_generator_list(True))
        save_all_btn = button(
            "Spremi sve",
            tooltip="Spremi sve učitane stavke iz trenutnog Live/VOD/Serije taba.",
        )
        save_all_btn.clicked.connect(self.save_all_generator_list)
        preview_btn = button(
            "Preview M3U",
            tooltip="Prikaži pregled M3U zapisa za trenutno prikazane stavke.",
        )
        preview_btn.clicked.connect(self.preview_playlist)
        export_actions.addWidget(export_btn)
        export_actions.addWidget(export_selected_btn)
        export_actions.addWidget(save_visible_btn)
        export_actions.addWidget(save_selected_btn)
        export_actions.addWidget(save_all_btn)
        export_actions.addWidget(preview_btn)
        export_actions.addStretch()
        layout.addLayout(export_actions)
        self.gen_tabs = QTabWidget()
        self.gen_tables: dict[str, QTableWidget] = {}
        self.gen_group_lists: dict[str, QListWidget] = {}
        for content_type in ("Live", "VOD", "Serije"):
            content_page = QWidget()
            content_layout = QHBoxLayout(content_page)
            content_layout.setContentsMargins(0, 6, 0, 0)
            content_splitter = QSplitter(Qt.Orientation.Horizontal)
            table_panel = QWidget()
            table_layout = QVBoxLayout(table_panel)
            table_layout.setContentsMargins(0, 0, 0, 0)
            table_controls = FlowLayout(spacing=6)
            table_controls.setSpacing(6)
            table_label = QLabel("Stream URL")
            table_label.setObjectName("Subtitle")
            check_visible_btn = button("Označi sve")
            check_visible_btn.setToolTip("Označi sve trenutno prikazane programe u tablici.")
            check_visible_btn.clicked.connect(lambda _checked=False, kind=content_type: self.set_visible_generator_checks(True, kind))
            uncheck_visible_btn = button("Odznači sve")
            uncheck_visible_btn.setToolTip("Odznači sve trenutno prikazane programe u tablici.")
            uncheck_visible_btn.clicked.connect(lambda _checked=False, kind=content_type: self.set_visible_generator_checks(False, kind))
            table_controls.addWidget(table_label)
            table_controls.addStretch()
            table_controls.addWidget(check_visible_btn)
            table_controls.addWidget(uncheck_visible_btn)
            table_layout.addLayout(table_controls)
            content_table = table(["✓", "Naziv", "Kategorija", "EPG ID", "Stream URL"])
            content_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            content_table.customContextMenuRequested.connect(
                lambda position, kind=content_type: self.generator_table_menu(kind, position)
            )
            content_table.itemDoubleClicked.connect(
                lambda item, kind=content_type: self.play_generator_item_in_vlc(kind, item)
            )
            content_table.itemChanged.connect(
                lambda item, kind=content_type: self.generator_item_changed(kind, item)
            )
            self.gen_tables[content_type] = content_table
            table_layout.addWidget(content_table, 1)

            group_panel = QFrame()
            group_panel.setObjectName("Card")
            group_layout = QVBoxLayout(group_panel)
            group_layout.setContentsMargins(10, 10, 10, 10)
            group_title = QLabel("Grupe")
            group_title.setStyleSheet("font-weight: 800;")
            group_controls = FlowLayout(spacing=6)
            check_groups_btn = button("Označi sve grupe")
            check_groups_btn.clicked.connect(lambda _checked=False, kind=content_type: self.set_generator_group_checks(True, kind))
            uncheck_groups_btn = button("Odznači sve grupe")
            uncheck_groups_btn.clicked.connect(lambda _checked=False, kind=content_type: self.set_generator_group_checks(False, kind))
            group_controls.addWidget(check_groups_btn)
            group_controls.addWidget(uncheck_groups_btn)
            group_list = QListWidget()
            group_list.setAlternatingRowColors(True)
            group_list.itemChanged.connect(
                lambda item, kind=content_type: self.generator_group_item_changed(kind, item)
            )
            self.gen_group_lists[content_type] = group_list
            group_layout.addWidget(group_title)
            group_layout.addLayout(group_controls)
            group_layout.addWidget(group_list, 1)

            content_splitter.addWidget(table_panel)
            content_splitter.addWidget(group_panel)
            content_splitter.setSizes([940, 320])
            content_layout.addWidget(content_splitter)
            self.gen_tabs.addTab(content_page, content_type)
        self.gen_tabs.currentChanged.connect(self.refresh_generator_groups)
        self.gen_tabs.currentChanged.connect(self.filter_playlist)
        layout.addWidget(self.gen_tabs, 1)
        self.gen_stats = QLabel("Kanali: 0")
        self.gen_stats.setObjectName("Subtitle")
        layout.addWidget(self.gen_stats)
        return page

    def _xtream_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.xtream_tabs = QTabWidget()
        self.xtream_tabs.setMinimumWidth(0)
        self.xtream_tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.xtream_tabs.addTab(self._analysis_tab(), "Analiza")
        self.xtream_tabs.addTab(self._xtream_scanner(), "Provjera računa")
        self.xtream_tabs.addTab(self._generator_tab(), "Studio · Live / VOD / Series")
        self.xtream_tabs.addTab(self._advanced_tab(), "Balkan IPTV")
        layout.addWidget(self.xtream_tabs)
        return page

    def _stalker_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stalker_tabs = QTabWidget()
        profiles_page = QWidget()
        profiles_layout = QVBoxLayout(profiles_page)
        profiles_layout.addWidget(
            tool_description(
                "Upravlja Stalker portal/MAC profilima i otvara ih u ugrađenom Studiju za "
                "učitavanje kanala i M3U export."
            )
        )
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        title = QLabel("Napredni Stalker / MAG portal studio")
        title.setStyleSheet("font-size: 20px; font-weight: 800;")
        description = QLabel(
            "Puni generator iz postojećeg projekta: automatski prepoznaje portal.php i "
            "stalker_portal/server/load.php, učitava Live, VOD i TV Shows, podržava "
            "kategorije, pojedinačni odabir, Adult PIN, auto-threads, brzi M3U export, "
            "resolve linkova i provjeru linkova nakon exporta."
        )
        description.setWordWrap(True)
        description.setObjectName("Subtitle")
        card_layout.addWidget(title)
        card_layout.addWidget(description)
        profiles_layout.addWidget(card)

        controls = FlowLayout()
        load_btn = button("Učitaj TXT listu URL/MAC profila")
        load_btn.clicked.connect(self.load_stalker_profiles)
        add_btn = button("Dodaj TXT profile")
        add_btn.clicked.connect(lambda: self.load_stalker_profiles(append=True))
        launch_btn = button(
            "Pokreni odabrani profil u Stalker Studiju",
            primary=True,
            tooltip="Prebaci označeni portal i MAC adresu u ugrađeni Stalker Studio.",
        )
        launch_btn.clicked.connect(self.launch_selected_stalker)
        blank_btn = button("Otvori Stalker Studio")
        blank_btn.clicked.connect(self.launch_stalker_studio)
        dedupe_btn = button("Ukloni duplikate")
        dedupe_btn.clicked.connect(self.remove_duplicate_stalker_profiles)
        controls.addWidget(load_btn)
        controls.addWidget(add_btn)
        controls.addWidget(dedupe_btn)
        controls.addStretch()
        controls.addWidget(blank_btn)
        controls.addWidget(launch_btn)
        profiles_layout.addLayout(controls)
        self.stalker_table = table(["Portal URL", "MAC adresa"])
        self.stalker_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.stalker_table.customContextMenuRequested.connect(self.stalker_table_menu)
        profiles_layout.addWidget(self.stalker_table, 1)
        export_btn = button("Export profila TXT")
        export_btn.clicked.connect(self.export_stalker_profiles)
        save_archive_btn = button(
            "Spremi profile u arhivu",
            tooltip="Spremi sve prikazane Stalker portal/MAC profile u bazu.",
        )
        save_archive_btn.clicked.connect(self.save_stalker_profiles_to_vault)
        profile_file_actions = FlowLayout()
        profile_file_actions.addWidget(save_archive_btn)
        profile_file_actions.addWidget(export_btn)
        profiles_layout.addLayout(profile_file_actions)
        self.stalker_tabs.addTab(profiles_page, "Profili")
        self.stalker_tabs.addTab(self._mac_grouper(), "URL → MAC grupiranje")
        self.stalker_tabs.addTab(self._stalker_check_tab(), "Provjera portala")
        self.stalker_tabs.addTab(self._stalker_balkan_mac_tab(), "Balkan MAC test")

        studio_page = QWidget()
        studio_layout = QVBoxLayout(studio_page)
        studio_layout.setContentsMargins(0, 0, 0, 0)
        try:
            studio_layout.addWidget(self.build_embedded_stalker())
        except Exception as error:
            error_label = QLabel(f"Stalker Studio nije moguće ugraditi:\n{error}")
            error_label.setWordWrap(True)
            studio_layout.addWidget(error_label)
        self.stalker_tabs.addTab(studio_page, "Studio · Live / VOD / Series")
        layout.addWidget(self.stalker_tabs)
        return page

    def build_embedded_stalker(self) -> QWidget:
        source_path = stalker_studio_source_path()
        source = source_path.read_text(encoding="utf-8")
        replacements = {
            "from PySide6 import QtCore, QtGui, QtWidgets": (
                "from PyQt6 import QtCore, QtGui, QtWidgets"
            ),
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
            "QtWidgets.QDialogButtonBox.Close": (
                "QtWidgets.QDialogButtonBox.StandardButton.Close"
            ),
            "QtWidgets.QHeaderView.ResizeToContents": (
                "QtWidgets.QHeaderView.ResizeMode.ResizeToContents"
            ),
            "QtWidgets.QHeaderView.Stretch": "QtWidgets.QHeaderView.ResizeMode.Stretch",
        }
        for old, new in replacements.items():
            source = source.replace(old, new)
        source = source.replace(
            "self.resize(820, 680)",
            "self.resize(1100, 850)\n        self.setMinimumSize(900, 650)",
        )
        source = source.replace(
            "dlg.resize(720, 520)",
            "dlg.resize(920, 680)\n        dlg.setMinimumSize(760, 560)",
        )
        source = source.replace(
            "root.addWidget(self.tabs, 1)",
            "self.content_log_splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)\n"
            "        self.content_log_splitter.addWidget(self.tabs)",
        )
        source = source.replace(
            "self.log.setFixedHeight(170)",
            "self.log.setMinimumHeight(90)",
        )
        source = source.replace(
            "root.addWidget(log_card)",
            "self.content_log_splitter.addWidget(log_card)\n"
            "        log_card.setMinimumHeight(150)\n"
            "        self.content_log_splitter.setChildrenCollapsible(False)\n"
            "        self.content_log_splitter.setCollapsible(0, False)\n"
            "        self.content_log_splitter.setCollapsible(1, False)\n"
            "        self.content_log_splitter.setSizes([620, 230])\n"
            "        root.addWidget(self.content_log_splitter, 1)",
        )
        source = source.replace(
            "use_fast_for_this = self.fast_export and (cat.category_type == \"IPTV\")",
            "use_fast_for_this = False",
        )
        source = source.replace(
            "low = raw.lower()\n"
            "        if low.startswith(\"http://\") or low.startswith(\"https://\"):\n"
            "            self._link_cache[cache_key] = raw\n"
            "            return raw\n\n"
            "        if low.startswith(\"ffmpeg\"):",
            "low = raw.lower()\n"
            "        if low.startswith(\"ffmpeg\"):",
        )
        source = source.replace(
            "raw = normalize_cmd_or_url((it.url or \"\").strip())\n"
            "                            url = raw if (raw.startswith(\"http://\") or raw.startswith(\"https://\")) else \"\"\n"
            "                            if not url:\n"
            "                                url = self.client.resolve_play_url(it)",
            "raw = normalize_cmd_or_url((it.url or \"\").strip())\n"
            "                            url = self.client.resolve_play_url(it)\n"
            "                            if not url and (raw.startswith(\"http://\") or raw.startswith(\"https://\")):\n"
            "                                url = raw",
        )

        module = types.ModuleType("aurora_stalker_embedded")
        module.__file__ = str(source_path)
        module.__dict__["__name__"] = "aurora_stalker_embedded"
        sys.modules[module.__name__] = module
        exec(compile(source, str(source_path), "exec"), module.__dict__)
        self.stalker_embedded_module = module
        self.stalker_embedded_window = module.MainWindow()
        self.stalker_embedded_window.play_stream_callback = self.play_stream_in_vlc
        self.patch_stalker_expiry_check()
        content = self.stalker_embedded_window.takeCentralWidget()
        content.setStyleSheet(self.stalker_embedded_window.styleSheet())
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        content.setMinimumHeight(820)
        self.configure_embedded_stalker_layout(content)
        return content

    def configure_embedded_stalker_layout(self, content: QWidget) -> None:
        module = self.stalker_embedded_module
        if not module:
            return
        for tab_name in ("tab_live", "tab_vod", "tab_tv"):
            tab_widget = getattr(self.stalker_embedded_window, tab_name, None)
            table_view = getattr(tab_widget, "table", None)
            if not table_view:
                continue
            header = table_view.horizontalHeader()
            header.setCascadingSectionResizes(False)
            header.setMinimumSectionSize(24)
            header.setMaximumSectionSize(20000)
            for column in range(header.count()):
                header.setSectionResizeMode(column, module.QtWidgets.QHeaderView.ResizeMode.Interactive)
            header.setStretchLastSection(False)
            header.setSectionsMovable(True)
            table_view.setHorizontalScrollBarPolicy(module.QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            table_view.setVerticalScrollBarPolicy(module.QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            table_view.setHorizontalScrollMode(module.QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
            table_view.setVerticalScrollMode(module.QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
            table_view.setColumnWidth(0, 44)
            table_view.setColumnWidth(1, 760)
            table_view.setColumnWidth(2, 110)
            table_view.setColumnWidth(3, 90)
            table_view.setMinimumHeight(260)

        splitter = getattr(self.stalker_embedded_window, "content_log_splitter", None)
        if splitter:
            splitter.setChildrenCollapsible(False)
            splitter.setCollapsible(0, False)
            splitter.setCollapsible(1, False)
            splitter.setSizes([620, 230])

    def patch_stalker_expiry_check(self) -> None:
        if not self.stalker_embedded_window or not self.stalker_embedded_module:
            return
        window = self.stalker_embedded_window
        module = self.stalker_embedded_module

        def collect_categories():
            categories = []
            for tab_name in ("tab_live", "tab_vod", "tab_tv"):
                tab_widget = getattr(window, tab_name, None)
                model = getattr(tab_widget, "model", None)
                if not model:
                    continue
                for row in range(model.rowCount()):
                    item = model.item(row, 1)
                    category = item.data(module.QtCore.Qt.ItemDataRole.UserRole) if item else None
                    if isinstance(category, module.Category):
                        categories.append(category)
            return categories

        def run_expiry_check():
            if not getattr(window, "client", None):
                module.QtWidgets.QMessageBox.information(
                    window,
                    "Valjanost liste",
                    "Prvo poveži portal i učitaj Live/VOD/Series grupe.",
                )
                return
            categories = collect_categories()
            samples = []
            for category_type in ("IPTV", "VOD", "Series"):
                samples.extend([c for c in categories if c.category_type == category_type][:3])
            window.expiry_lbl.setText(self.translate_static_text("Valjanost liste: (provjeravam...)"))
            window.append_log("Ručno pokrećem provjeru valjanosti liste...")
            worker = module.AccountExpiryWorker(window.client)
            worker.signals.error.connect(
                lambda error: (
                    window.append_log(f"Valjanost profil greška: {error}"),
                    window._start_sample_expiry_detection(samples),
                )
            )
            worker.signals.finished.connect(
                lambda payload: window._on_profile_expiry_detected(payload, samples)
            )
            window.thread_pool.start(worker)

        window.btn_check_expiry = module.QtWidgets.QPushButton("Provjeri valjanost")
        window.btn_check_expiry.setToolTip("Ponovno provjeri datum isteka liste.")
        window.btn_check_expiry.clicked.connect(run_expiry_check)
        window.statusBar().addPermanentWidget(window.btn_check_expiry)
        if hasattr(window, "fast_export_chk"):
            window.fast_export_chk.setChecked(False)
            window.fast_export_chk.setEnabled(False)
            window.fast_export_chk.setToolTip(
                "Isključeno u Aurori: export mora resolve/create_link da bi linkovi imali token."
            )

    def _stalker_check_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Provjerava odgovara li Stalker/MAG portal za URL i pripadajuću MAC adresu."
            )
        )
        controls = FlowLayout()
        load_profiles = button("Učitaj iz profila")
        load_profiles.clicked.connect(self.load_stalker_check_from_profiles)
        paste_profiles = button("Zalijepi i prepoznaj")
        paste_profiles.clicked.connect(
            lambda: self.add_stalker_check_profiles_from_text(QApplication.clipboard().text())
        )
        run_check = button("Provjeri URL/MAC", primary=True)
        run_check.clicked.connect(self.toggle_stalker_profile_check)
        stop_check = button("Zaustavi provjeru")
        stop_check.clicked.connect(self.stop_stalker_profile_check)
        remove_bad = button("Ukloni koji ne rade")
        remove_bad.clicked.connect(self.remove_bad_stalker_check_rows)
        remove_selected = button("Ukloni odabrano", danger=True)
        remove_selected.clicked.connect(self.remove_selected_stalker_check_rows)
        open_studio = button("Pošalji odabrano u Studio")
        open_studio.clicked.connect(self.open_selected_stalker_check_in_studio)
        export_valid = button("Export ispravnih")
        export_valid.clicked.connect(self.export_valid_stalker_check_profiles)
        controls.addWidget(load_profiles)
        controls.addWidget(paste_profiles)
        controls.addStretch()
        controls.addWidget(stop_check)
        controls.addWidget(remove_bad)
        controls.addWidget(remove_selected)
        controls.addWidget(export_valid)
        controls.addWidget(open_studio)
        controls.addWidget(run_check)
        layout.addLayout(controls)
        self.stalker_check_progress = QProgressBar()
        layout.addWidget(self.stalker_check_progress)
        self.stalker_check_table = table(["Portal URL", "MAC adresa", "Radi", "Status", "Vrijeme"])
        self.stalker_check_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.stalker_check_table.customContextMenuRequested.connect(
            self.stalker_check_table_menu
        )
        self.stalker_check_table.itemDoubleClicked.connect(
            lambda _item: self.open_selected_stalker_check_in_studio()
        )
        layout.addWidget(self.stalker_check_table, 1)
        return page

    def _stalker_balkan_mac_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Za svaki portal/MAC učitava Live grupe, pronalazi Balkan programe, "
                "radi create_link/token i nasumično proba nekoliko streamova."
            )
        )

        controls = FlowLayout()
        load_valid = button(
            "Učitaj ispravne iz Provjere portala",
            tooltip="Prebaci samo redove gdje je Provjera portala označila Radi = DA.",
        )
        load_valid.clicked.connect(self.load_stalker_balkan_from_check)
        load_profiles = button("Učitaj iz profila")
        load_profiles.clicked.connect(self.load_stalker_balkan_from_profiles)
        paste_profiles = button("Zalijepi i prepoznaj")
        paste_profiles.clicked.connect(
            lambda: self.add_stalker_balkan_profiles_from_text(QApplication.clipboard().text())
        )
        clear_rows = button("Očisti tablicu")
        clear_rows.clicked.connect(lambda: self.stalker_balkan_table.setRowCount(0))
        remove_selected = button("Ukloni odabrano", danger=True)
        remove_selected.clicked.connect(self.remove_selected_stalker_balkan_rows)
        export_results = button("Export rezultata")
        export_results.clicked.connect(self.export_stalker_balkan_results)
        stop_check = button("Zaustavi test")
        stop_check.clicked.connect(self.stop_stalker_balkan_check)
        run_check = button("Provjeri Balkan MAC", primary=True)
        run_check.clicked.connect(self.toggle_stalker_balkan_check)

        controls.addWidget(load_valid)
        controls.addWidget(load_profiles)
        controls.addWidget(paste_profiles)
        controls.addStretch()
        controls.addWidget(clear_rows)
        controls.addWidget(remove_selected)
        controls.addWidget(export_results)
        controls.addWidget(stop_check)
        controls.addWidget(run_check)
        layout.addLayout(controls)

        options = QHBoxLayout()
        options.addWidget(QLabel("Nasumičnih streamova po MAC-u"))
        self.stalker_balkan_sample_size = QSpinBox()
        self.stalker_balkan_sample_size.setRange(1, 8)
        self.stalker_balkan_sample_size.setValue(4)
        options.addWidget(self.stalker_balkan_sample_size)
        options.addWidget(QLabel("Timeout"))
        self.stalker_balkan_timeout = QSpinBox()
        self.stalker_balkan_timeout.setRange(3, 30)
        self.stalker_balkan_timeout.setValue(10)
        self.stalker_balkan_timeout.setSuffix(" s")
        options.addWidget(self.stalker_balkan_timeout)
        options.addStretch()
        layout.addLayout(options)

        self.stalker_balkan_input = QTextEdit()
        self.stalker_balkan_input.setPlaceholderText(
            "Zalijepi portal URL i MAC adrese ako ne učitavaš iz drugih Stalker tabova."
        )
        self.stalker_balkan_input.setMaximumHeight(110)
        layout.addWidget(self.stalker_balkan_input)

        self.stalker_balkan_progress = QProgressBar()
        layout.addWidget(self.stalker_balkan_progress)
        self.stalker_balkan_table = table(
            [
                "Portal URL",
                "MAC adresa",
                "Balkan",
                "Radi Balkan",
                "Testirano",
                "Status",
                "Uzorci",
                "Vrijeme",
            ]
        )
        header = self.stalker_balkan_table.horizontalHeader()
        for column in range(header.count()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.stalker_balkan_table.setColumnWidth(0, 230)
        self.stalker_balkan_table.setColumnWidth(1, 130)
        self.stalker_balkan_table.setColumnWidth(2, 80)
        self.stalker_balkan_table.setColumnWidth(3, 100)
        self.stalker_balkan_table.setColumnWidth(4, 95)
        self.stalker_balkan_table.setColumnWidth(5, 360)
        self.stalker_balkan_table.setColumnWidth(6, 520)
        self.stalker_balkan_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.stalker_balkan_table.customContextMenuRequested.connect(
            self.stalker_balkan_table_menu
        )
        layout.addWidget(self.stalker_balkan_table, 1)
        return page

    def _vault_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Čuva provjerene aktivne račune bez duplikata i omogućuje import, export "
                "i brzo brisanje zapisa."
            )
        )

        archive_tabs = QTabWidget()

        accounts_page = QWidget()
        accounts_layout = QVBoxLayout(accounts_page)
        controls = QVBoxLayout()
        controls_header = QHBoxLayout()
        account_actions = FlowLayout()
        account_file_actions = FlowLayout()
        info = QLabel("Aktivni računi spremljeni bez duplikata")
        info.setObjectName("Subtitle")
        controls_header.addWidget(info)
        controls_header.addStretch()
        self.vault_account_filter = QLineEdit()
        self.vault_account_filter.setPlaceholderText("Pretraži račune po serveru, korisniku, statusu ili isteku...")
        self.vault_account_filter.textChanged.connect(self.filter_vault_tables)
        refresh = button("Osvježi")
        refresh.clicked.connect(self.refresh_vault)
        pull_generator = button(
            "Povuci u Generator",
            tooltip="Popuni Xtream Generator serverom, korisnikom i lozinkom iz označenog računa.",
        )
        pull_generator.clicked.connect(self.load_vault_account_to_generator)
        pull_scan = button(
            "Pošalji u provjeru",
            tooltip="Pretvori označeni arhivirani račun u get.php URL i pošalji ga u Xtream provjeru.",
        )
        pull_scan.clicked.connect(self.send_vault_account_to_scan)
        cleanup = button(
            "Predloži čišćenje",
            tooltip="Pronađe istekle ili neaktivne račune i pita prije brisanja iz arhive.",
        )
        cleanup.clicked.connect(self.suggest_vault_cleanup)
        delete = button("Obriši označeno", danger=True)
        delete.clicked.connect(self.delete_vault_row)
        delete_all_accounts = button("Obriši sve račune", danger=True)
        delete_all_accounts.clicked.connect(self.delete_all_vault_accounts)
        export = button("Export JSON")
        export.clicked.connect(self.export_vault)
        backup = button("Backup sve")
        backup.clicked.connect(self.backup_full_vault)
        restore = button("Restore backup")
        restore.clicked.connect(self.restore_full_vault)
        import_btn = button("Import JSON")
        import_btn.clicked.connect(self.import_vault)
        export_csv = button("Export CSV")
        export_csv.clicked.connect(self.export_vault_csv)
        account_actions.addWidget(refresh)
        account_actions.addWidget(pull_generator)
        account_actions.addWidget(pull_scan)
        account_actions.addWidget(cleanup)
        account_actions.addStretch()
        account_file_actions.addWidget(export)
        account_file_actions.addWidget(backup)
        account_file_actions.addWidget(restore)
        account_file_actions.addWidget(export_csv)
        account_file_actions.addWidget(import_btn)
        account_file_actions.addWidget(delete)
        account_file_actions.addWidget(delete_all_accounts)
        account_file_actions.addStretch()
        controls.addLayout(controls_header)
        controls.addWidget(self.vault_account_filter)
        controls.addLayout(account_actions)
        controls.addLayout(account_file_actions)
        accounts_layout.addLayout(controls)
        self.vault_table = table(
            ["ID", "Status", "Server", "Korisnik", "Lozinka", "Ističe", "Veze", "Sadržaj", "Provjereno"]
        )
        self.vault_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.vault_table.customContextMenuRequested.connect(self.vault_account_menu)
        accounts_layout.addWidget(self.vault_table)
        xtream_lists_label = QLabel("Xtream liste spremljene iz Generatora i provjere")
        xtream_lists_label.setObjectName("Subtitle")
        accounts_layout.addWidget(xtream_lists_label)
        self.xtream_saved_lists_table = table(["ID", "Naziv", "Tip", "Izvor", "Stavki", "Spremljeno"])
        self.xtream_saved_lists_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.xtream_saved_lists_table.customContextMenuRequested.connect(
            lambda position: self.saved_list_menu(self.xtream_saved_lists_table, position)
        )
        accounts_layout.addWidget(self.xtream_saved_lists_table)
        archive_tabs.addTab(accounts_page, "Xtream")

        lists_page = QWidget()
        lists_layout = QVBoxLayout(lists_page)
        list_controls = QVBoxLayout()
        list_header = QHBoxLayout()
        list_actions = FlowLayout()
        list_info = QLabel("MAC i Stalker liste/profili")
        list_info.setObjectName("Subtitle")
        list_header.addWidget(list_info)
        list_header.addStretch()
        self.saved_list_filter = QLineEdit()
        self.saved_list_filter.setPlaceholderText("Pretraži spremljene liste po nazivu, tipu ili izvoru...")
        self.saved_list_filter.textChanged.connect(self.filter_vault_tables)
        refresh_lists = button("Osvježi")
        refresh_lists.clicked.connect(self.refresh_vault)
        open_list = button(
            "Otvori listu",
            tooltip="Učita spremljenu listu natrag u odgovarajući alat za pregled ili daljnji rad.",
        )
        open_list.clicked.connect(self.open_saved_list)
        scan_list = button(
            "Pošalji u provjeru",
            tooltip="Iz spremljene liste izvuče Xtream URL-ove i pošalje ih u provjeru.",
        )
        scan_list.clicked.connect(self.send_saved_list_to_scan)
        export_list = button(
            "Export liste",
            tooltip="Spremi označenu arhiviranu listu kao datoteku.",
        )
        export_list.clicked.connect(self.export_saved_list)
        copy_list = button("Kopiraj listu")
        copy_list.clicked.connect(self.copy_saved_list)
        generator_list = button(
            "Vrati u Generator",
            tooltip="Učita spremljenu M3U/listu u Xtream Generator za daljnje uređivanje.",
        )
        generator_list.clicked.connect(self.open_saved_list_in_generator)
        delete_list = button("Obriši listu", danger=True)
        delete_list.clicked.connect(self.delete_saved_list_row)
        delete_all_lists = button("Obriši sve liste", danger=True)
        delete_all_lists.clicked.connect(self.delete_all_saved_lists)
        list_actions.addWidget(refresh_lists)
        list_actions.addWidget(open_list)
        list_actions.addWidget(scan_list)
        list_actions.addWidget(export_list)
        list_actions.addWidget(copy_list)
        list_actions.addWidget(generator_list)
        list_actions.addWidget(delete_list)
        list_actions.addWidget(delete_all_lists)
        list_actions.addStretch()
        list_controls.addLayout(list_header)
        list_controls.addWidget(self.saved_list_filter)
        list_controls.addLayout(list_actions)
        lists_layout.addLayout(list_controls)
        self.saved_lists_table = table(["ID", "Naziv", "Tip", "Izvor", "Stavki", "Spremljeno"])
        self.saved_lists_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.saved_lists_table.customContextMenuRequested.connect(
            lambda position: self.saved_list_menu(self.saved_lists_table, position)
        )
        lists_layout.addWidget(self.saved_lists_table)
        archive_tabs.addTab(lists_page, "MAC")

        layout.addWidget(archive_tabs)
        self.refresh_vault()
        return page

    def _settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(
            tool_description(
                "Podešava mrežne opcije i vanjski player koje koriste alati u Aurori."
            )
        )
        settings_tabs = QTabWidget()

        network_page = QWidget()
        network_layout = QVBoxLayout(network_page)
        network = QFrame()
        network.setObjectName("Card")
        network_form = QFormLayout(network)
        self.setting_user_agent = QComboBox()
        self.setting_user_agent.setEditable(True)
        self.setting_user_agent.addItems(
            [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "VLC/3.0.20 LibVLC/3.0.20",
                "IPTVSmartersPro",
                "SmartTV",
            ]
        )
        self.setting_proxy = QTextEdit()
        self.setting_proxy.setMaximumHeight(90)
        self.setting_proxy.setPlaceholderText("Opcionalni proxyji, jedan po retku")
        network_form.addRow("User-Agent:", self.setting_user_agent)
        network_form.addRow("Proxy lista:", self.setting_proxy)
        network_layout.addWidget(network)
        network_layout.addStretch()
        settings_tabs.addTab(network_page, "Mreža")

        workflow_page = QWidget()
        workflow_layout = QVBoxLayout(workflow_page)
        workflow = QFrame()
        workflow.setObjectName("Card")
        workflow_form = QFormLayout(workflow)
        self.setting_export_dir = QLineEdit(self.default_export_dir)
        browse_export_dir = button("Odaberi folder")
        browse_export_dir.clicked.connect(self.browse_export_dir)
        export_dir_row = QHBoxLayout()
        export_dir_row.addWidget(self.setting_export_dir)
        export_dir_row.addWidget(browse_export_dir)
        self.setting_auto_save_active = QCheckBox("Automatski spremi aktivne račune u arhivu")
        self.setting_auto_save_active.setChecked(self.auto_save_active)
        self.setting_confirm_bulk = QCheckBox("Traži potvrdu prije masovnog brisanja/čišćenja")
        self.setting_confirm_bulk.setChecked(self.confirm_bulk_actions)
        self.setting_remember_tab = QCheckBox("Zapamti zadnji otvoreni tab")
        self.setting_remember_tab.setChecked(self.remember_last_tab)
        workflow_form.addRow("Export folder:", export_dir_row)
        workflow_form.addRow("", self.setting_auto_save_active)
        workflow_form.addRow("", self.setting_confirm_bulk)
        workflow_form.addRow("", self.setting_remember_tab)
        workflow_layout.addWidget(workflow)
        workflow_layout.addStretch()
        settings_tabs.addTab(workflow_page, "Workflow")

        player_page = QWidget()
        player_layout = QVBoxLayout(player_page)
        player = QFrame()
        player.setObjectName("Card")
        player_form = QFormLayout(player)
        self.setting_player = QLineEdit("/usr/bin/vlc")
        browse = button("Pronađi player")
        browse.clicked.connect(self.browse_player)
        player_row = QHBoxLayout()
        player_row.addWidget(self.setting_player)
        player_row.addWidget(browse)
        player_form.addRow("VLC / vanjski player:", player_row)
        player_layout.addWidget(player)
        player_layout.addStretch()
        settings_tabs.addTab(player_page, "VLC / Player")

        layout.addWidget(settings_tabs, 1)
        save = button("Spremi postavke", primary=True)
        save.clicked.connect(self.save_app_settings)
        layout.addWidget(save, alignment=Qt.AlignmentFlag.AlignLeft)
        return page

    def choose_text_files(self) -> list[str]:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            self.translate_static_text("Otvori datoteke"),
            self.default_export_dir,
            self.translate_static_text(
                "Podržano (*.txt *.log *.csv *.json *.m3u *.m3u8);;Sve datoteke (*)"
            ),
        )
        return paths

    def read_text_files(self, paths: list[str]) -> str:
        chunks = []
        errors = []
        for path in paths:
            try:
                chunks.append(Path(path).read_text(encoding="utf-8", errors="replace"))
            except OSError as error:
                errors.append(f"{path}: {error}")
        if errors:
            QMessageBox.warning(self, "Čitanje nije uspjelo", "\n".join(errors[:8]))
        return "\n".join(chunks)

    def load_text_into(self, widget: QTextEdit, append: bool = False) -> None:
        paths = self.choose_text_files()
        if not paths:
            return
        content = self.read_text_files(paths)
        if append and widget.toPlainText().strip():
            widget.append(content)
        else:
            widget.setPlainText(content)
        self.statusBar().showMessage(f"Učitano datoteka: {len(paths)}", 5000)

    def open_analysis_files(self, append: bool = False) -> None:
        self.load_text_into(self.url_input, append=append)
        self.select_xtream_tab("Analiza")

    def run_url_extraction(self) -> None:
        self.url_result = extract_playlist_urls(
            self.url_input.toPlainText(),
            self.url_m3u8.isChecked(),
            self.url_query.isChecked(),
            self.url_only_playlists.isChecked(),
            self.url_sort.isChecked(),
            self.url_dedupe.isChecked(),
        )
        result = self.url_result
        self.render_url_results()
        self.url_stats.setText(
            f"Pronađeno: {result.total_found} · Rezultat: {len(result.urls)} · "
            f"Odbačeno: {result.discarded} · Duplikati: {result.duplicates} · "
            f"Serveri: {result.hosts} · #EXTINF: {result.channels}"
        )
        self.metric_urls.value.setText(str(result.total_found))
        self.statusBar().showMessage("URL analiza je završena.", 5000)

    def render_url_results(self) -> None:
        if not self.url_result:
            return
        needle = self.url_filter.text().strip().lower()
        urls = [
            url
            for url in self.url_result.urls
            if not needle
            or needle in url.lower()
            or needle in (urlparse(url).hostname or "").lower()
        ]
        if self.url_hosts_only.isChecked():
            output = "\n".join(sorted({urlparse(url).hostname or "" for url in urls}))
        elif self.url_group_hosts.isChecked():
            grouped: dict[str, list[str]] = {}
            for url in urls:
                grouped.setdefault(urlparse(url).hostname or "(bez servera)", []).append(url)
            output = "\n\n".join(
                f"[{host}]\n" + "\n".join(grouped[host]) for host in sorted(grouped)
            )
        else:
            output = "\n".join(urls)
        self.url_output.setPlainText(output)

    def show_url_report(self) -> None:
        if not self.url_result:
            return QMessageBox.information(self, "Izvještaj", "Prvo pokreni izvlačenje URL-ova.")
        result = self.url_result
        QMessageBox.information(
            self,
            "Izvještaj",
            f"Znakova u ulazu: {len(self.url_input.toPlainText())}\n"
            f"Ukupno URL-ova: {result.total_found}\n"
            f"Rezultat: {len(result.urls)}\n"
            f"Odbačeno: {result.discarded}\n"
            f"Duplikati: {result.duplicates}\n"
            f"Jedinstveni serveri: {result.hosts}\n"
            f"M3U sadržaj: {'DA' if result.is_m3u else 'NE'}\n"
            f"#EXTINF kanali: {result.channels}",
        )

    def open_selected_url(self) -> None:
        selected = self.url_output.textCursor().selectedText().strip()
        candidate = selected.splitlines()[0] if selected else ""
        if not candidate:
            candidate = self.url_output.textCursor().block().text().strip()
        if candidate.startswith("[") or not candidate.startswith(("http://", "https://")):
            return QMessageBox.information(self, "URL", "Označi URL ili postavi kursor u njegov redak.")
        webbrowser.open(candidate)

    def clear_url_analysis(self) -> None:
        self.url_input.clear()
        self.url_output.clear()
        self.url_filter.clear()
        self.url_result = None
        self.url_stats.setText("URL-ovi: 0 · Duplikati: 0 · Serveri: 0")

    def export_clean_urls(self) -> None:
        if not self.url_result:
            return
        self.save_text("\n".join(self.url_result.urls), "iptv_urlovi.txt")

    def url_output_menu(self, position) -> None:
        menu = self.url_output.createStandardContextMenu()
        menu.addSeparator()
        copy_all = menu.addAction("Kopiraj sve")
        send_scanner = menu.addAction("Pošalji sve u Xtream skener")
        send_balkan = menu.addAction("Pošalji sve u Balkan IPTV")
        send_generator = menu.addAction("Pošalji označeni Xtream URL u generator")
        send_stalker = menu.addAction("Pošalji URL/MAC profile u Stalker")
        self.translate_menu(menu)
        chosen = menu.exec(self.url_output.mapToGlobal(position))
        if chosen == copy_all:
            QApplication.clipboard().setText(self.url_output.toPlainText())
        elif chosen == send_scanner:
            self.append_text(self.scan_input, self.url_output.toPlainText())
            self.select_xtream_tab("Provjera računa")
        elif chosen == send_balkan:
            self.send_analysis_to_balkan()
        elif chosen == send_generator:
            selected = self.url_output.textCursor().selectedText().strip()
            candidate = selected.splitlines()[0] if selected else self.url_output.textCursor().block().text().strip()
            parsed = parse_xtream_url(candidate)
            if not parsed:
                QMessageBox.information(self, "Generator", "Označeni red nije Xtream URL.")
            else:
                server, username, password = parsed
                self.open_xtream_studio_with_account(server, username, password)
        elif chosen == send_stalker:
            self.add_stalker_profiles_from_text(self.url_input.toPlainText())
            self.stalker_tabs.setCurrentIndex(0)
            self.select_main_tab("Stalker Studio")

    def mac_group_output_menu(self, position) -> None:
        menu = self.mac_group_output.createStandardContextMenu()
        menu.addSeparator()
        send_stalker = menu.addAction("Pošalji grupe u Stalker tab")
        copy_all = menu.addAction("Kopiraj sve")
        self.translate_menu(menu)
        chosen = menu.exec(self.mac_group_output.mapToGlobal(position))
        if chosen == send_stalker:
            self.add_stalker_profiles_from_text(self.mac_group_output.toPlainText())
            self.stalker_tabs.setCurrentIndex(0)
            self.select_main_tab("Stalker Studio")
        elif chosen == copy_all:
            QApplication.clipboard().setText(self.mac_group_output.toPlainText())

    @staticmethod
    def append_text(widget: QTextEdit, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if widget.toPlainText().strip():
            widget.append(text)
        else:
            widget.setPlainText(text)

    def run_mac_grouping(self) -> None:
        result = group_macs_by_url(
            self.mac_group_input.toPlainText(),
            self.mac_global.isChecked(),
            self.mac_sort_urls.isChecked(),
            self.mac_sort_values.isChecked(),
        )
        self.mac_group_output.setPlainText(format_mac_groups(result.groups))
        self.mac_group_table.setRowCount(0)
        with table_sorting_paused(self.mac_group_table):
            for portal, macs in result.groups:
                for mac in macs:
                    row = self.mac_group_table.rowCount()
                    self.mac_group_table.insertRow(row)
                    self.mac_group_table.setItem(row, 0, QTableWidgetItem(portal))
                    self.mac_group_table.setItem(row, 1, QTableWidgetItem(mac))
        self.mac_group_stats.setText(
            f"Grupe: {len(result.groups)} · MAC: {result.mac_count} · "
            f"Duplikati: {result.duplicates} · Ignorirano prije URL-a: {result.ignored}"
        )
        self.metric_macs.value.setText(str(result.mac_count))

    def mac_group_table_menu(self, position) -> None:
        row = self.mac_group_table.rowAt(position.y())
        if row < 0:
            return
        portal = self.mac_group_table.item(row, 0).text()
        mac = self.mac_group_table.item(row, 1).text()
        menu = QMenu(self)
        copy_both = menu.addAction("Kopiraj URL i MAC")
        send_stalker = menu.addAction("Pošalji u Stalker profile")
        send_check = menu.addAction("Pošalji u Provjeru portala")
        self.translate_menu(menu)
        chosen = menu.exec(self.mac_group_table.viewport().mapToGlobal(position))
        if chosen == copy_both:
            QApplication.clipboard().setText(f"{portal}\n{mac}")
        elif chosen == send_stalker:
            self.add_stalker_profiles_from_text(f"{portal}\n{mac}")
            self.stalker_tabs.setCurrentIndex(0)
        elif chosen == send_check:
            self.add_stalker_check_profiles_from_text(f"{portal}\n{mac}")
            self.select_main_tab("Stalker Studio")
            self.stalker_tabs.setCurrentIndex(2)

    def send_mac_groups_to_stalker_check(self) -> None:
        content = self.mac_group_output.toPlainText().strip()
        if not content:
            self.run_mac_grouping()
            content = self.mac_group_output.toPlainText().strip()
        if not content:
            QMessageBox.information(self, "Provjera portala", "Nema URL/MAC parova za slanje.")
            return
        before = self.stalker_check_table.rowCount()
        self.add_stalker_check_profiles_from_text(content)
        after = self.stalker_check_table.rowCount()
        self.select_main_tab("Stalker Studio")
        self.stalker_tabs.setCurrentIndex(2)
        self.statusBar().showMessage(
            (
                f"{self.translate_static_text('Dodano profila u Provjeru portala:')} "
                f"{after - before} · "
                f"{self.translate_static_text('Ukupno u provjeri:')} {after}"
            ),
            5000,
        )

    def check_mac_group_urls(self) -> None:
        result = group_macs_by_url(self.mac_group_input.toPlainText())
        invalid = [
            url
            for url, _macs in result.groups
            if not urlparse(normalize_url(url)).netloc
        ]
        QMessageBox.information(
            self,
            "URL → MAC",
            f"Prepoznato URL-ova: {len(result.groups)}\n"
            f"Prepoznato MAC adresa: {result.mac_count}\n"
            f"Nevaljanih URL-ova: {len(invalid)}",
        )

    def toggle_xtream_scan(self) -> None:
        if self.scan_worker and self.scan_worker.isRunning():
            self.scan_worker.stop()
            self.scan_start.setText(self.translate_static_text("Pokreni provjeru"))
            return
        candidates = extract_playlist_urls(
            self.scan_input.toPlainText(), playlists_only=False
        ).urls
        candidates = [url for url in candidates if "username=" in url and "password=" in url]
        deduped_candidates = []
        seen_accounts = set()
        for url in candidates:
            parsed = parse_xtream_url(url)
            key = tuple(part.lower() for part in parsed) if parsed else (url.lower(),)
            if key in seen_accounts:
                continue
            seen_accounts.add(key)
            deduped_candidates.append(url)
        candidates = deduped_candidates
        if not candidates:
            QMessageBox.warning(self, "Nema URL-ova", "Nisu pronađeni Xtream URL-ovi s podacima.")
            return
        self.scan_table.setRowCount(0)
        self.scan_progress.setMaximum(len(candidates))
        self.scan_progress.setValue(0)
        self.scan_worker = XtreamScanWorker(
            candidates, self.scan_threads.value(), self.scan_timeout.value()
        )
        self.scan_worker.result.connect(self.add_scan_result)
        self.scan_worker.progress.connect(lambda done, total: self.scan_progress.setValue(done))
        self.scan_worker.finished_scan.connect(self.scan_finished)
        self.scan_worker.start()
        self.scan_start.setText(self.translate_static_text("Zaustavi"))
        self.connection_label.setText(
            "● Check in progress" if self.language == "en" else "● Provjera u tijeku"
        )

    def load_scanner_files(self, append: bool = False) -> None:
        self.load_text_into(self.scan_input, append=append)
        self.select_xtream_tab("Provjera računa")

    def add_scan_result(self, result: dict[str, str]) -> None:
        with table_sorting_paused(self.scan_table):
            row = self.scan_table.rowCount()
            self.scan_table.insertRow(row)
            values = [
                result["status"],
                result["server"],
                result["username"],
                result["password"],
                result["expiry"],
                result["connections"],
                result["content"],
                result["ping"],
            ]
            online = result["status"].lower() in {"active", "online"}
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setForeground(QColor("#62d6a7" if online else "#ff839f"))
                item.setData(Qt.ItemDataRole.UserRole, result)
                self.scan_table.setItem(row, column, item)
        if online:
            self.metric_online.value.setText(str(int(self.metric_online.value.text()) + 1))
            if self.auto_save_active:
                self.vault.save(result)
                self.refresh_vault()

    def scan_finished(self) -> None:
        self.scan_start.setText(self.translate_static_text("Pokreni provjeru"))
        self.connection_label.setText(self.tr_ui("ready"))
        self.scan_worker = None
        self.statusBar().showMessage("Xtream provjera je završena.", 5000)

    def filter_scan_results(self) -> None:
        needle = self.scan_filter.text().strip().lower()
        mode = self.scan_status_filter.currentIndex()
        for row in range(self.scan_table.rowCount()):
            values = [
                self.scan_table.item(row, column).text().lower()
                for column in range(self.scan_table.columnCount())
                if self.scan_table.item(row, column)
            ]
            status = values[0] if values else ""
            active = status in {"active", "online"}
            visible = (not needle or any(needle in value for value in values))
            if mode == 1:
                visible = visible and active
            elif mode == 2:
                visible = visible and not active
            self.scan_table.setRowHidden(row, not visible)

    def scan_result_rows(self, visible_only: bool = False) -> list[dict[str, str]]:
        rows = []
        for row in range(self.scan_table.rowCount()):
            if visible_only and self.scan_table.isRowHidden(row):
                continue
            item = self.scan_table.item(row, 0)
            result = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(result, dict):
                rows.append(result)
        return rows

    def send_selected_scan_to_generator(self) -> None:
        row = self.scan_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Generator", "Označi račun u tablici provjere.")
            return
        item = self.scan_table.item(row, 0)
        result = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(result, dict):
            return
        if result.get("status", "").lower() not in {"active", "online"}:
            answer = QMessageBox.question(
                self,
                "Generator",
                "Označeni račun nije aktivan. Želiš ga svejedno poslati u Generator?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.open_xtream_studio_with_account(
            result["server"],
            result["username"],
            result["password"],
        )

    def export_scan_results(self, kind: str) -> None:
        rows = self.scan_result_rows(visible_only=True)
        if not rows:
            return QMessageBox.information(self, "Export", "Nema vidljivih rezultata.")
        filters = {
            "txt": ("Tekst (*.txt)", "aurora_rezultati.txt"),
            "csv": ("CSV (*.csv)", "aurora_rezultati.csv"),
            "json": ("JSON (*.json)", "aurora_rezultati.json"),
        }
        file_filter, default = filters[kind]
        path = self.get_save_path("Export rezultata", default, file_filter)
        if not path:
            return
        if kind == "json":
            Path(path).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        elif kind == "csv":
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        else:
            blocks = []
            for result in rows:
                blocks.append(
                    "\n".join(
                        [
                            f"Status: {result['status']}",
                            f"Server: {result['server']}",
                            f"Korisnik: {result['username']}",
                            f"Lozinka: {result['password']}",
                            f"Ističe: {result['expiry']}",
                            f"Veze: {result['connections']}",
                            f"Sadržaj: {result['content']}",
                            f"Ping: {result['ping']}",
                            f"M3U: {result['playlist_url']}",
                        ]
                    )
                )
            Path(path).write_text("\n\n---\n\n".join(blocks) + "\n", encoding="utf-8")

    def remove_offline_results(self) -> None:
        if self.confirm_bulk_actions:
            answer = QMessageBox.question(
                self,
                "Ukloni neaktivne",
                "Želiš ukloniti sve neaktivne rezultate iz tablice?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        for row in range(self.scan_table.rowCount() - 1, -1, -1):
            status = self.scan_table.item(row, 0).text().lower()
            if status not in {"active", "online"}:
                self.scan_table.removeRow(row)

    def remove_duplicate_scan_results(self) -> None:
        seen = set()
        for row in range(self.scan_table.rowCount() - 1, -1, -1):
            key = tuple(
                self.scan_table.item(row, column).text().lower()
                for column in (1, 2, 3)
            )
            if key in seen:
                self.scan_table.removeRow(row)
            else:
                seen.add(key)

    def scan_context_menu(self, position) -> None:
        row = self.scan_table.rowAt(position.y())
        if row < 0:
            return
        result = self.scan_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        save_action = menu.addAction("Spremi / ažuriraj u arhivi")
        copy_action = menu.addAction("Kopiraj M3U URL")
        copy_account = menu.addAction("Kopiraj server / korisnik / lozinka")
        generator_action = menu.addAction("Otvori račun u Xtream Generatoru")
        balkan_action = menu.addAction("Pošalji M3U u Balkan IPTV")
        self.translate_menu(menu)
        chosen = menu.exec(self.scan_table.viewport().mapToGlobal(position))
        if chosen == save_action:
            self.vault.save(result)
            self.refresh_vault()
        elif chosen == copy_action:
            QApplication.clipboard().setText(result["playlist_url"])
        elif chosen == copy_account:
            QApplication.clipboard().setText(
                f"{result['server']}\n{result['username']}\n{result['password']}"
            )
        elif chosen == generator_action:
            self.open_xtream_studio_with_account(
                result["server"],
                result["username"],
                result["password"],
            )
        elif chosen == balkan_action:
            self.load_urls_into_balkan([result["playlist_url"]])

    def generator_table_menu(self, content_type: str, position) -> None:
        content_table = self.gen_tables[content_type]
        row = content_table.rowAt(position.y())
        if row < 0:
            return
        stream = content_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        copy_name = menu.addAction("Kopiraj naziv")
        copy_url = menu.addAction("Kopiraj stream URL")
        copy_entry = menu.addAction("Kopiraj M3U zapis")
        play_vlc = menu.addAction("Pokreni stream u VLC playeru")
        self.translate_menu(menu)
        chosen = menu.exec(content_table.viewport().mapToGlobal(position))
        if chosen == copy_name:
            QApplication.clipboard().setText(stream["name"])
        elif chosen == copy_url:
            QApplication.clipboard().setText(stream["url"])
        elif chosen == copy_entry:
            QApplication.clipboard().setText(
                f'#EXTINF:-1 group-title="{stream["category"]}",{stream["name"]}\n'
                f'{stream["url"]}'
            )
        elif chosen == play_vlc:
            self.play_stream_in_vlc(stream["url"])

    def play_generator_item_in_vlc(
        self,
        content_type: str,
        item: QTableWidgetItem,
    ) -> None:
        stream = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(stream, dict):
            return
        stream_url = str(stream.get("url") or "").strip()
        if not stream_url:
            self.statusBar().showMessage(
                f"{content_type}: odabrani zapis nema stream URL.",
                4000,
            )
            return
        self.play_stream_in_vlc(stream_url)

    def detect_vlc_path(self) -> str:
        configured = self.setting_player.text().strip() if hasattr(self, "setting_player") else ""
        if configured and Path(configured).exists():
            return configured
        candidates = []
        found = shutil.which("vlc")
        if found:
            candidates.append(found)
        if sys.platform.startswith("win"):
            candidates.extend(
                [
                    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
                    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
                ]
            )
        elif sys.platform == "darwin":
            candidates.append("/Applications/VLC.app/Contents/MacOS/VLC")
        else:
            candidates.extend(["/usr/bin/vlc", "/snap/bin/vlc", "/usr/local/bin/vlc"])
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                if hasattr(self, "setting_player"):
                    self.setting_player.setText(candidate)
                    self.settings.setValue("player", candidate)
                return candidate
        return ""

    def offer_vlc_install(self) -> None:
        if sys.platform.startswith("win"):
            message = (
                "VLC nije pronađen. Instaliraj VLC s videolan.org, zatim u Postavkama "
                "odaberi vlc.exe ako ga Aurora ne pronađe automatski."
            )
        elif sys.platform == "darwin":
            message = "VLC nije pronađen. Instaliraj VLC za macOS i pokreni Auroru ponovno."
        else:
            message = (
                "VLC nije pronađen. Na Debian/Ubuntu sustavu možeš ga instalirati naredbom:\n"
                "sudo apt install vlc\n\n"
                "Na Fedora sustavu: sudo dnf install vlc"
            )
        QMessageBox.information(self, "VLC player", message)

    def play_stream_in_vlc(self, stream_url: str) -> None:
        player = self.detect_vlc_path()
        if not player:
            self.offer_vlc_install()
            self.browse_player()
            player = self.detect_vlc_path()
            if not player:
                return
        try:
            subprocess.Popen([player, stream_url])
            self.statusBar().showMessage("Stream je poslan u VLC player.", 5000)
        except OSError as error:
            QMessageBox.critical(self, "VLC player", f"VLC nije moguće pokrenuti:\n{error}")

    def active_account_m3u_lines(self) -> list[str]:
        rows = [
            row
            for row in self.scan_result_rows(visible_only=True)
            if row["status"].lower() in {"active", "online"}
        ]
        lines = ["#EXTM3U"]
        for row in rows:
            lines.extend([f"#EXTINF:-1,{row['server']} · {row['username']}", row["playlist_url"]])
        return lines

    def export_active_accounts_m3u(self) -> None:
        lines = self.active_account_m3u_lines()
        if len(lines) == 1:
            return QMessageBox.information(self, "Export", "Nema aktivnih vidljivih računa.")
        path = self.get_save_path("Export aktivnih računa", "aktivne_liste.m3u", "M3U (*.m3u)")
        if not path:
            return
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def toggle_mac_scan(self) -> None:
        if self.mac_worker and self.mac_worker.isRunning():
            self.mac_worker.stop()
            self.mac_start.setText(self.translate_static_text("Pokreni MAC provjeru"))
            return
        url = self.mac_check_url.text().strip()
        macs = parse_mac_lines(self.mac_check_input.toPlainText())
        if not urlparse(normalize_url(url)).netloc or not macs:
            QMessageBox.warning(self, "Nedostaju podaci", "Unesi valjan endpoint i MAC adrese.")
            return
        self.mac_table.setRowCount(0)
        self.mac_progress.setMaximum(len(macs))
        self.mac_worker = MacHttpWorker(
            normalize_url(url),
            macs,
            self.mac_mode.currentText(),
            self.mac_field.text().strip() or "mac",
            self.mac_timeout.value(),
            self.mac_success.text().strip(),
        )
        self.mac_worker.result.connect(self.add_mac_result)
        self.mac_worker.progress.connect(lambda done, total: self.mac_progress.setValue(done))
        self.mac_worker.finished_scan.connect(self.mac_finished)
        self.mac_worker.start()
        self.mac_start.setText(self.translate_static_text("Zaustavi"))

    def add_mac_result(self, result: dict[str, str]) -> None:
        with table_sorting_paused(self.mac_table):
            row = self.mac_table.rowCount()
            self.mac_table.insertRow(row)
            for column, key in enumerate(["mac", "works", "status", "ping"]):
                item = QTableWidgetItem(result[key])
                if column == 1:
                    item.setForeground(QColor("#62d6a7" if result[key] == "DA" else "#ff839f"))
                self.mac_table.setItem(row, column, item)

    def mac_finished(self) -> None:
        self.mac_start.setText(self.translate_static_text("Pokreni MAC provjeru"))
        self.mac_worker = None

    def export_mac_results(self) -> None:
        if not self.mac_table.rowCount():
            return
        path = self.get_save_path("Export MAC rezultata", "mac_provjera.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["MAC", "Radi", "Status", "Vrijeme"])
            for row in range(self.mac_table.rowCount()):
                writer.writerow(
                    [self.mac_table.item(row, column).text() for column in range(4)]
                )

    def load_playlist(self) -> None:
        self._load_playlist_type(self.current_generator_type())

    def current_generator_type(self) -> str:
        return self.gen_tabs.tabText(self.gen_tabs.currentIndex())

    def fill_generator_from_url(self) -> None:
        value = self.gen_full_url.text().strip()
        parsed = parse_xtream_url(value)
        if not parsed:
            QMessageBox.warning(
                self,
                "Cijeli link",
                "Link mora sadržavati server te username i password parametre.",
            )
            return
        server, username, password = parsed
        self.gen_server.setText(server)
        self.gen_user.setText(username)
        self.gen_password.setText(password)
        self.statusBar().showMessage("Xtream podaci su iščitani iz linka.", 5000)

    def load_generator_m3u_file(self) -> None:
        path = self.get_open_path("Učitaj M3U listu", "M3U liste (*.m3u *.m3u8 *.txt);;Sve datoteke (*)")
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            QMessageBox.critical(self, "M3U", str(error))
            return
        rows = self.parse_m3u_content(content)
        if not rows:
            QMessageBox.information(self, "M3U", "Datoteka ne sadrži prepoznatljive M3U stavke.")
            return
        self.playlist_rows["Live"] = rows
        self.playlist_rows["VOD"] = []
        self.playlist_rows["Serije"] = []
        self.gen_tabs.setCurrentIndex(0)
        self.refresh_generator_groups()
        self.filter_playlist()
        self.statusBar().showMessage(f"Učitano M3U stavki: {len(rows)}", 5000)

    @staticmethod
    def parse_m3u_content(content: str) -> list[dict[str, str]]:
        rows = []
        current = None
        attr_pattern = re.compile(r'([\w-]+)="([^"]*)"')
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.upper().startswith("#EXTINF"):
                attrs = dict(attr_pattern.findall(line))
                name = line.rsplit(",", 1)[-1].strip() if "," in line else "Bez naziva"
                current = {
                    "name": name,
                    "category": attrs.get("group-title", "Ostalo") or "Ostalo",
                    "logo": attrs.get("tvg-logo", ""),
                    "epg_id": attrs.get("tvg-id", ""),
                    "type": "Live",
                    "url": "",
                    "_checked": True,
                }
            elif line.startswith(("http://", "https://")):
                if current:
                    current["url"] = line
                    rows.append(current)
                    current = None
                else:
                    rows.append(
                        {
                            "name": line,
                            "category": "Ostalo",
                            "logo": "",
                            "epg_id": "",
                            "type": "Live",
                            "url": line,
                            "_checked": True,
                        }
                    )
        return rows

    def clear_generator(self) -> None:
        for key in self.playlist_rows:
            self.playlist_rows[key] = []
        for table_widget in self.gen_tables.values():
            table_widget.setRowCount(0)
        for group_list in self.gen_group_lists.values():
            group_list.clear()
        self.gen_filter.clear()
        self.gen_full_url.clear()
        self.gen_stats.setText("Kanali: 0")
        self.statusBar().showMessage("Xtream Generator je očišćen.", 4000)

    def _load_playlist_type(self, content_type: str) -> None:
        server = normalize_url(self.gen_server.text())
        if not urlparse(server).netloc or not self.gen_user.text() or not self.gen_password.text():
            QMessageBox.warning(self, "Nedostaju podaci", "Unesi server, korisnika i lozinku.")
            return
        self.gen_load.setEnabled(False)
        self.gen_load_all.setEnabled(False)
        self.gen_load.setText(self.translate_static_text("Učitavanje..."))
        self.playlist_worker = PlaylistWorker(
            server,
            self.gen_user.text().strip(),
            self.gen_password.text(),
            content_type,
        )
        self.playlist_worker.loaded.connect(
            lambda rows, kind=content_type: self.playlist_loaded(kind, rows)
        )
        self.playlist_worker.failed.connect(self.playlist_failed)
        self.playlist_worker.finished.connect(self.playlist_worker_finished)
        self.playlist_worker.start()

    def load_all_playlist_types(self) -> None:
        self._playlist_load_queue = ["Live", "VOD", "Serije"]
        self._load_next_playlist_type()

    def _load_next_playlist_type(self) -> None:
        if not getattr(self, "_playlist_load_queue", []):
            self.gen_load.setEnabled(True)
            self.gen_load_all.setEnabled(True)
            self.gen_load.setText(self.translate_static_text("Učitaj sadržaj"))
            return
        content_type = self._playlist_load_queue.pop(0)
        self._load_playlist_type(content_type)

    def playlist_loaded(self, content_type: str, rows: list[dict[str, str]]) -> None:
        self.playlist_rows[content_type] = rows
        for index in range(self.gen_tabs.count()):
            if self.gen_tabs.tabText(index) == content_type:
                self.gen_tabs.setCurrentIndex(index)
                break
        else:
            index = -1
        if index >= 0:
            self.gen_tabs.setCurrentIndex(index)
        self.refresh_generator_groups()
        self.filter_playlist()
        self.statusBar().showMessage(
            f"Učitano je {len(rows)} stavki ({content_type}).", 5000
        )

    def playlist_worker_finished(self) -> None:
        if getattr(self, "_playlist_load_queue", []):
            self.playlist_worker = None
            self._load_next_playlist_type()
            return
        self.playlist_worker = None
        self.gen_load.setEnabled(True)
        self.gen_load_all.setEnabled(True)
        self.gen_load.setText(self.translate_static_text("Učitaj sadržaj"))

    def playlist_failed(self, message: str) -> None:
        self._playlist_load_queue = []
        QMessageBox.critical(self, "Učitavanje nije uspjelo", message)

    def refresh_generator_groups(self, *_args) -> None:
        if not hasattr(self, "gen_group_lists"):
            return
        group_list = self.gen_group_lists[self.current_generator_type()]
        groups = sorted(
            {
                row["category"]
                for row in self.playlist_rows.get(self.current_generator_type(), [])
                if row.get("category")
            }
        )
        group_list.blockSignals(True)
        group_list.clear()
        for group in groups:
            item = QListWidgetItem(group)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            item.setCheckState(self.generator_group_check_state(group))
            group_list.addItem(item)
        group_list.blockSignals(False)

    def checked_generator_groups(self) -> set[str]:
        if not hasattr(self, "gen_group_lists"):
            return set()
        checked = set()
        group_list = self.gen_group_lists[self.current_generator_type()]
        for index in range(group_list.count()):
            item = group_list.item(index)
            if item and item.checkState() in {
                Qt.CheckState.Checked,
                Qt.CheckState.PartiallyChecked,
            }:
                checked.add(item.text())
        return checked

    def generator_group_check_state(self, group: str) -> Qt.CheckState:
        rows = [
            stream
            for stream in self.playlist_rows.get(self.current_generator_type(), [])
            if stream.get("category") == group
        ]
        if not rows:
            return Qt.CheckState.Unchecked
        checked = sum(1 for stream in rows if stream.get("_checked", True))
        if checked == 0:
            return Qt.CheckState.Unchecked
        if checked == len(rows):
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    def refresh_generator_group_check_states(self) -> None:
        if not hasattr(self, "gen_group_lists"):
            return
        group_list = self.gen_group_lists[self.current_generator_type()]
        group_list.blockSignals(True)
        for row in range(group_list.count()):
            item = group_list.item(row)
            if item:
                item.setCheckState(self.generator_group_check_state(item.text()))
        group_list.blockSignals(False)

    def generator_group_item_changed(self, content_type: str, item: QListWidgetItem) -> None:
        if not item:
            return
        if content_type != self.current_generator_type():
            return
        checked = item.checkState() == Qt.CheckState.Checked
        for stream in self.playlist_rows.get(self.current_generator_type(), []):
            if stream.get("category") == item.text():
                stream["_checked"] = checked
        self.refresh_generator_group_check_states()
        self.filter_playlist()

    def filter_playlist(self, *_args) -> None:
        content_type = self.current_generator_type()
        content_table = self.gen_tables[content_type]
        needle = self.gen_filter.text().strip().lower()
        checked_groups = self.checked_generator_groups()
        visible = [
            row
            for row in self.playlist_rows[content_type]
            if (not checked_groups or row["category"] in checked_groups)
            and (not needle or needle in row["name"].lower() or needle in row["category"].lower())
        ]
        content_table.blockSignals(True)
        with table_sorting_paused(content_table):
            content_table.setRowCount(0)
            for stream in visible:
                row = content_table.rowCount()
                content_table.insertRow(row)
                check_item = QTableWidgetItem("")
                check_item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                check_item.setCheckState(
                    Qt.CheckState.Checked
                    if stream.get("_checked", True)
                    else Qt.CheckState.Unchecked
                )
                check_item.setData(Qt.ItemDataRole.UserRole, stream)
                content_table.setItem(row, 0, check_item)
                for column, key in enumerate(["name", "category", "epg_id", "url"], start=1):
                    item = QTableWidgetItem(stream[key])
                    item.setData(Qt.ItemDataRole.UserRole, stream)
                    content_table.setItem(row, column, item)
        content_table.blockSignals(False)
        checked = sum(1 for row in self.playlist_rows[content_type] if row.get("_checked", True))
        self.gen_stats.setText(
            f"{content_type} · Prikazano: {len(visible)} · "
            f"Označeno: {checked} · Ukupno: {len(self.playlist_rows[content_type])}"
        )

    def generator_item_changed(self, content_type: str, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        stream = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(stream, dict):
            stream["_checked"] = item.checkState() == Qt.CheckState.Checked
            self.refresh_generator_group_check_states()

    def set_visible_generator_checks(self, checked: bool, content_type: str | None = None) -> None:
        content_type = content_type or self.current_generator_type()
        content_table = self.gen_tables[content_type]
        content_table.blockSignals(True)
        for row in range(content_table.rowCount()):
            item = content_table.item(row, 0)
            if not item:
                continue
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            stream = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(stream, dict):
                stream["_checked"] = checked
        content_table.blockSignals(False)
        self.refresh_generator_group_check_states()
        self.statusBar().showMessage(
            f"Prikazani programi ({content_type}) su označeni."
            if checked
            else f"Prikazani programi ({content_type}) su odznačeni.",
            4000,
        )

    def set_generator_group_checks(self, checked: bool, content_type: str | None = None) -> None:
        content_type = content_type or self.current_generator_type()
        group_list = self.gen_group_lists[content_type]
        categories = {
            group_list.item(index).text()
            for index in range(group_list.count())
            if group_list.item(index)
        }
        for stream in self.playlist_rows.get(content_type, []):
            if stream.get("category") in categories:
                stream["_checked"] = checked
        group_list.blockSignals(True)
        for index in range(group_list.count()):
            item = group_list.item(index)
            if item:
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        group_list.blockSignals(False)
        self.filter_playlist()
        self.statusBar().showMessage(
            f"Grupe ({content_type}) su označene." if checked else f"Grupe ({content_type}) su odznačene.",
            4000,
        )

    def set_all_generator_checks(self, checked: bool) -> None:
        content_type = self.current_generator_type()
        for stream in self.playlist_rows.get(content_type, []):
            stream["_checked"] = checked
        self.refresh_generator_group_check_states()
        self.filter_playlist()
        self.statusBar().showMessage(
            f"Sve stavke ({content_type}) su označene."
            if checked
            else f"Sve stavke ({content_type}) su odznačene.",
            4000,
        )

    def generator_streams(self, selected_only: bool = False) -> list[dict[str, str]]:
        content_type = self.current_generator_type()
        content_table = self.gen_tables[content_type]
        if selected_only:
            rows = [
                row
                for row in range(content_table.rowCount())
                if content_table.item(row, 0)
                and content_table.item(row, 0).checkState() == Qt.CheckState.Checked
            ]
        else:
            rows = list(range(content_table.rowCount()))
        streams = []
        for row in rows:
            item = content_table.item(row, 0)
            stream = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(stream, dict):
                streams.append(stream)
        return streams

    def playlist_m3u_lines(self, streams: list[dict[str, str]]) -> list[str]:
        lines = ["#EXTM3U"]
        for stream in streams:
            name = stream["name"].replace('"', "'")
            group = stream["category"].replace('"', "'")
            logo = stream["logo"].replace('"', "%22")
            epg_id = stream["epg_id"].replace('"', "'")
            lines.append(
                f'#EXTINF:-1 tvg-id="{epg_id}" tvg-logo="{logo}" group-title="{group}",{name}'
            )
            lines.append(stream["url"])
        return lines

    def export_playlist(self, selected_only: bool = False) -> None:
        content_type = self.current_generator_type()
        streams = self.generator_streams(selected_only)
        if not streams:
            message = "Nema označenih kanala za export." if selected_only else "Nema prikazanih kanala za export."
            QMessageBox.information(self, "Nema kanala", message)
            return
        default_name = f"aurora_{content_type.lower()}.m3u"
        path = self.get_save_path("Spremi M3U", default_name, "M3U (*.m3u)")
        if not path:
            return
        lines = self.playlist_m3u_lines(streams)
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.statusBar().showMessage(f"M3U lista je spremljena: {path}", 7000)

    def preview_playlist(self) -> None:
        streams = self.generator_streams(selected_only=False)
        if not streams:
            QMessageBox.information(self, "Preview M3U", "Nema prikazanih kanala za pregled.")
            return
        preview = "\n".join(self.playlist_m3u_lines(streams[:50]))
        if len(streams) > 50:
            preview += f"\n\n... prikazano prvih 50 od {len(streams)} stavki"
        QMessageBox.information(self, "Preview M3U", preview[:6000])

    def ask_list_name(self, default_name: str) -> str | None:
        name, accepted = QInputDialog.getText(
            self, "Spremi listu", "Naziv liste:", text=default_name
        )
        name = name.strip()
        return name if accepted and name else None

    def save_list_to_vault(
        self,
        default_name: str,
        kind: str,
        source: str,
        content: str,
        item_count: int,
    ) -> None:
        if not content.strip():
            QMessageBox.information(self, "Arhiva", "Nema sadržaja za spremanje.")
            return
        name = self.ask_list_name(default_name)
        if not name:
            return
        self.vault.save_list(name, kind, source, content, item_count)
        self.refresh_vault()
        self.statusBar().showMessage(f"Lista je spremljena u arhivu: {name}", 5000)

    def save_generator_list(self, selected_only: bool = False) -> None:
        content_type = self.current_generator_type()
        streams = self.generator_streams(selected_only)
        if not streams:
            message = "Nema označenih stavki za spremanje." if selected_only else "Nema prikazanih stavki za spremanje."
            QMessageBox.information(self, "Arhiva", message)
            return
        suffix = "označeno" if selected_only else "prikazano"
        self.save_list_to_vault(
            f"{content_type} {suffix}",
            f"Xtream {content_type}",
            self.gen_server.text().strip(),
            "\n".join(self.playlist_m3u_lines(streams)) + "\n",
            len(streams),
        )

    def save_all_generator_list(self) -> None:
        content_type = self.current_generator_type()
        streams = self.playlist_rows.get(content_type, [])
        if not streams:
            QMessageBox.information(self, "Arhiva", "Nema učitanih stavki za spremanje.")
            return
        self.save_list_to_vault(
            f"{content_type} sve",
            f"Xtream {content_type}",
            self.gen_server.text().strip() or "Xtream Generator",
            "\n".join(self.playlist_m3u_lines(streams)) + "\n",
            len(streams),
        )

    def save_url_results_to_vault(self) -> None:
        content = self.url_output.toPlainText().strip()
        item_count = len([line for line in content.splitlines() if line.strip()])
        self.save_list_to_vault("URL extractor rezultat", "URL lista", "Analiza", content + "\n", item_count)

    def save_mac_groups_to_vault(self) -> None:
        content = self.mac_group_output.toPlainText().strip()
        item_count = len(parse_mac_lines(content))
        self.save_list_to_vault("URL MAC grupe", "MAC grupe", "Stalker Studio", content + "\n", item_count)

    def save_active_accounts_m3u_to_vault(self) -> None:
        lines = self.active_account_m3u_lines()
        if len(lines) == 1:
            QMessageBox.information(self, "Arhiva", "Nema aktivnih vidljivih računa za spremanje.")
            return
        self.save_list_to_vault(
            "Aktivni Xtream računi",
            "M3U računi",
            "Provjera",
            "\n".join(lines) + "\n",
            (len(lines) - 1) // 2,
        )

    def save_mac_results_to_vault(self) -> None:
        if not self.mac_table.rowCount():
            QMessageBox.information(self, "Arhiva", "Nema MAC rezultata za spremanje.")
            return
        lines = ["MAC,Radi,Status,Vrijeme"]
        for row in range(self.mac_table.rowCount()):
            lines.append(
                ",".join(
                    self.mac_table.item(row, column).text()
                    for column in range(self.mac_table.columnCount())
                )
            )
        self.save_list_to_vault(
            "MAC provjera rezultati",
            "MAC rezultati",
            self.mac_check_url.text().strip(),
            "\n".join(lines) + "\n",
            self.mac_table.rowCount(),
        )

    def save_stalker_profiles_to_vault(self) -> None:
        if not self.stalker_table.rowCount():
            QMessageBox.information(self, "Arhiva", "Nema Stalker profila za spremanje.")
            return
        blocks = []
        for row in range(self.stalker_table.rowCount()):
            blocks.append(
                f"{self.stalker_table.item(row, 0).text()}\n"
                f"{self.stalker_table.item(row, 1).text()}"
            )
        self.save_list_to_vault(
            "Stalker profili",
            "Stalker profili",
            "Stalker Studio",
            "\n\n".join(blocks) + "\n",
            self.stalker_table.rowCount(),
        )

    def load_stalker_profiles(self, append: bool = False) -> None:
        paths = self.choose_text_files()
        if not paths:
            return
        if not append:
            self.stalker_table.setRowCount(0)
        self.add_stalker_profiles_from_text(self.read_text_files(paths))
        self.statusBar().showMessage(
            f"Učitano Stalker profila: {self.stalker_table.rowCount()}", 5000
        )

    def add_stalker_profiles_from_text(self, text: str) -> None:
        parsed = group_macs_by_url(text, global_dedupe=False)
        existing = set()
        for row in range(self.stalker_table.rowCount()):
            existing.add(
                (
                    self.stalker_table.item(row, 0).text(),
                    self.stalker_table.item(row, 1).text(),
                )
            )
        for portal, macs in parsed.groups:
            for mac in macs:
                if (portal, mac) in existing:
                    continue
                with table_sorting_paused(self.stalker_table):
                    row = self.stalker_table.rowCount()
                    self.stalker_table.insertRow(row)
                    self.stalker_table.setItem(row, 0, QTableWidgetItem(portal))
                    self.stalker_table.setItem(row, 1, QTableWidgetItem(mac))
                existing.add((portal, mac))

    def stalker_check_profiles(self) -> list[tuple[str, str]]:
        profiles = []
        for row in range(self.stalker_check_table.rowCount()):
            profiles.append(
                (
                    self.stalker_check_table.item(row, 0).text(),
                    self.stalker_check_table.item(row, 1).text(),
                )
            )
        return profiles

    def add_stalker_check_profiles_from_text(self, text: str) -> None:
        parsed = group_macs_by_url(text, global_dedupe=False)
        existing = set(self.stalker_check_profiles())
        for portal, macs in parsed.groups:
            for mac in macs:
                if (portal, mac) in existing:
                    continue
                with table_sorting_paused(self.stalker_check_table):
                    row = self.stalker_check_table.rowCount()
                    self.stalker_check_table.insertRow(row)
                    for column, value in enumerate([portal, mac, "—", "—", "—"]):
                        self.stalker_check_table.setItem(row, column, QTableWidgetItem(value))
                existing.add((portal, mac))

    def load_stalker_check_from_profiles(self) -> None:
        rows = []
        for row in range(self.stalker_table.rowCount()):
            rows.append(
                f"{self.stalker_table.item(row, 0).text()}\n"
                f"{self.stalker_table.item(row, 1).text()}"
            )
        self.stalker_check_table.setRowCount(0)
        self.add_stalker_check_profiles_from_text("\n\n".join(rows))

    def toggle_stalker_profile_check(self) -> None:
        if self.stalker_check_worker and self.stalker_check_worker.isRunning():
            self.stalker_check_worker.stop()
            return
        profiles = self.stalker_check_profiles()
        if not profiles:
            QMessageBox.information(self, "Provjera portala", "Nema URL/MAC profila za provjeru.")
            return
        self.stalker_check_progress.setMaximum(len(profiles))
        self.stalker_check_progress.setValue(0)
        self.stalker_check_worker = StalkerProfileCheckWorker(profiles)
        self.stalker_check_worker.result.connect(self.add_stalker_check_result)
        self.stalker_check_worker.progress.connect(
            lambda done, total: self.stalker_check_progress.setValue(done)
        )
        self.stalker_check_worker.finished_scan.connect(self.stalker_check_finished)
        self.stalker_check_worker.start()

    def stop_stalker_profile_check(self) -> None:
        if self.stalker_check_worker and self.stalker_check_worker.isRunning():
            self.stalker_check_worker.stop()
            self.statusBar().showMessage("Zaustavljanje provjere portala...", 4000)
        else:
            self.statusBar().showMessage("Provjera portala nije pokrenuta.", 3000)

    def add_stalker_check_result(self, result: dict[str, str]) -> None:
        for row in range(self.stalker_check_table.rowCount()):
            if (
                self.stalker_check_table.item(row, 0).text() == result["portal"]
                and self.stalker_check_table.item(row, 1).text() == result["mac"]
            ):
                values = [result["works"], result["status"], result["ping"]]
                with table_sorting_paused(self.stalker_check_table):
                    for offset, value in enumerate(values, start=2):
                        item = QTableWidgetItem(value)
                        if offset == 2:
                            item.setForeground(QColor("#62d6a7" if value == "DA" else "#ff839f"))
                        self.stalker_check_table.setItem(row, offset, item)
                return

    def stalker_check_finished(self) -> None:
        self.stalker_check_worker = None
        self.statusBar().showMessage("Provjera URL/MAC profila je završena.", 5000)

    def remove_bad_stalker_check_rows(self) -> None:
        removed = 0
        for row in range(self.stalker_check_table.rowCount() - 1, -1, -1):
            works_item = self.stalker_check_table.item(row, 2)
            status_item = self.stalker_check_table.item(row, 3)
            works = works_item.text() if works_item else "—"
            status = status_item.text() if status_item else "—"
            if works != "DA" and status != "—":
                self.stalker_check_table.removeRow(row)
                removed += 1
        self.statusBar().showMessage(f"Uklonjeno neispravnih profila: {removed}", 5000)

    def remove_selected_stalker_check_rows(self) -> None:
        rows = sorted({index.row() for index in self.stalker_check_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.stalker_check_table.removeRow(row)
        self.statusBar().showMessage(f"Uklonjeno odabranih profila: {len(rows)}", 5000)

    def open_selected_stalker_check_in_studio(self) -> None:
        row = self.stalker_check_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Provjera portala", "Označi URL/MAC profil.")
            return
        self.open_profile_in_stalker(
            self.stalker_check_table.item(row, 0).text(),
            self.stalker_check_table.item(row, 1).text(),
        )

    def stalker_table_menu(self, position) -> None:
        row = self.stalker_table.rowAt(position.y())
        menu = QMenu(self)
        paste_profiles = menu.addAction("Zalijepi URL/MAC profile")
        copy_portal = copy_mac = copy_both = launch = remove = None
        if row >= 0:
            portal = self.stalker_table.item(row, 0).text()
            mac = self.stalker_table.item(row, 1).text()
            menu.addSeparator()
            copy_portal = menu.addAction("Kopiraj portal")
            copy_mac = menu.addAction("Kopiraj MAC")
            copy_both = menu.addAction("Kopiraj portal i MAC")
            menu.addSeparator()
            launch = menu.addAction("Otvori u Stalker Studiju")
            remove = menu.addAction("Ukloni profil")
        self.translate_menu(menu)
        chosen = menu.exec(self.stalker_table.viewport().mapToGlobal(position))
        if chosen == paste_profiles:
            self.add_stalker_profiles_from_text(QApplication.clipboard().text())
        elif chosen == copy_portal:
            QApplication.clipboard().setText(portal)
        elif chosen == copy_mac:
            QApplication.clipboard().setText(mac)
        elif chosen == copy_both:
            QApplication.clipboard().setText(f"{portal}\n{mac}")
        elif chosen == launch:
            self.open_profile_in_stalker(portal, mac)
        elif chosen == remove:
            self.stalker_table.removeRow(row)

    def stalker_check_table_menu(self, position) -> None:
        row = self.stalker_check_table.rowAt(position.y())
        if row < 0:
            return
        self.stalker_check_table.setCurrentCell(row, 0)
        portal = self.stalker_check_table.item(row, 0).text()
        mac = self.stalker_check_table.item(row, 1).text()
        menu = QMenu(self)
        open_studio = menu.addAction("Otvori u Stalker Studiju")
        copy_portal = menu.addAction("Kopiraj portal")
        copy_mac = menu.addAction("Kopiraj MAC")
        copy_both = menu.addAction("Kopiraj portal i MAC")
        menu.addSeparator()
        remove = menu.addAction("Ukloni profil")
        self.translate_menu(menu)
        chosen = menu.exec(
            self.stalker_check_table.viewport().mapToGlobal(position)
        )
        if chosen == open_studio:
            self.open_selected_stalker_check_in_studio()
        elif chosen == copy_portal:
            QApplication.clipboard().setText(portal)
        elif chosen == copy_mac:
            QApplication.clipboard().setText(mac)
        elif chosen == copy_both:
            QApplication.clipboard().setText(f"{portal}\n{mac}")
        elif chosen == remove:
            self.stalker_check_table.removeRow(row)

    def launch_selected_stalker(self) -> None:
        row = self.stalker_table.currentRow()
        if row < 0:
            return QMessageBox.information(self, "Stalker Studio", "Označi URL/MAC profil.")
        self.open_profile_in_stalker(
            self.stalker_table.item(row, 0).text(),
            self.stalker_table.item(row, 1).text(),
        )

    def remove_duplicate_stalker_profiles(self) -> None:
        seen = set()
        removed = 0
        for row in range(self.stalker_table.rowCount() - 1, -1, -1):
            key = (
                self.stalker_table.item(row, 0).text().strip().lower(),
                self.stalker_table.item(row, 1).text().strip().upper(),
            )
            if key in seen:
                self.stalker_table.removeRow(row)
                removed += 1
            else:
                seen.add(key)
        self.statusBar().showMessage(f"Uklonjeno duplih Stalker profila: {removed}", 5000)

    def open_profile_in_stalker(self, portal: str, mac: str) -> None:
        if not self.stalker_embedded_window:
            return self.launch_stalker_studio(portal=portal, mac=mac)
        self.stalker_embedded_window.url_edit.setText(portal)
        self.stalker_embedded_window.mac_edit.setText(mac)
        self.stalker_tabs.setCurrentIndex(self.stalker_tabs.count() - 1)
        self.select_main_tab("Stalker Studio")
        self.statusBar().showMessage("Profil je prebačen u ugrađeni Stalker Studio.", 5000)

    def launch_stalker_studio(
        self, checked: bool = False, portal: str = "", mac: str = ""
    ) -> None:
        if self.stalker_embedded_window:
            if portal:
                self.stalker_embedded_window.url_edit.setText(portal)
            if mac:
                self.stalker_embedded_window.mac_edit.setText(mac)
            self.stalker_tabs.setCurrentIndex(self.stalker_tabs.count() - 1)
            self.select_main_tab("Stalker Studio")
            return
        QMessageBox.critical(self, "Stalker Studio", "Ugrađeni Studio nije učitan.")

    def export_stalker_profiles(self) -> None:
        if not self.stalker_table.rowCount():
            return
        blocks = []
        for row in range(self.stalker_table.rowCount()):
            blocks.append(
                f"{self.stalker_table.item(row, 0).text()}\n"
                f"{self.stalker_table.item(row, 1).text()}"
            )
        self.save_text("\n\n".join(blocks), "stalker_profili.txt")

    def export_valid_stalker_check_profiles(self) -> None:
        blocks = []
        for row in range(self.stalker_check_table.rowCount()):
            works_item = self.stalker_check_table.item(row, 2)
            if not works_item or works_item.text() != "DA":
                continue
            blocks.append(
                f"{self.stalker_check_table.item(row, 0).text()}\n"
                f"{self.stalker_check_table.item(row, 1).text()}"
            )
        if not blocks:
            QMessageBox.information(self, "Export ispravnih", "Nema ispravnih profila za export.")
            return
        self.save_text("\n\n".join(blocks), "stalker_ispravni_profili.txt")

    def stalker_balkan_profiles(self) -> list[tuple[str, str]]:
        profiles = []
        for row in range(self.stalker_balkan_table.rowCount()):
            portal_item = self.stalker_balkan_table.item(row, 0)
            mac_item = self.stalker_balkan_table.item(row, 1)
            if not portal_item or not mac_item:
                continue
            portal = portal_item.text().strip()
            mac = mac_item.text().strip()
            if portal and mac:
                profiles.append((portal, mac))
        return profiles

    def add_stalker_balkan_profiles_from_text(self, text: str) -> None:
        parsed = group_macs_by_url(text, global_dedupe=False)
        existing = set(self.stalker_balkan_profiles())
        added = 0
        with table_sorting_paused(self.stalker_balkan_table):
            for portal, macs in parsed.groups:
                for mac in macs:
                    if (portal, mac) in existing:
                        continue
                    row = self.stalker_balkan_table.rowCount()
                    self.stalker_balkan_table.insertRow(row)
                    values = [portal, mac, "—", "—", "—", "—", "—", "—"]
                    for column, value in enumerate(values):
                        self.stalker_balkan_table.setItem(row, column, QTableWidgetItem(value))
                    existing.add((portal, mac))
                    added += 1
        if parsed.ignored:
            self.statusBar().showMessage(
                (
                    f"{self.translate_static_text('Dodano Balkan MAC profila:')} {added}; "
                    f"{self.translate_static_text('ignorirano MAC adresa bez portala:')} "
                    f"{parsed.ignored}"
                ),
                6000,
            )
        else:
            self.statusBar().showMessage(
                f"{self.translate_static_text('Dodano Balkan MAC profila:')} {added}",
                4000,
            )

    def load_stalker_balkan_from_profiles(self) -> None:
        rows = []
        for row in range(self.stalker_table.rowCount()):
            rows.append(
                f"{self.stalker_table.item(row, 0).text()}\n"
                f"{self.stalker_table.item(row, 1).text()}"
            )
        if not rows:
            QMessageBox.information(
                self,
                self.translate_static_text("Balkan MAC test"),
                self.translate_static_text("Nema Stalker profila za učitavanje."),
            )
            return
        self.stalker_balkan_table.setRowCount(0)
        self.add_stalker_balkan_profiles_from_text("\n\n".join(rows))

    def load_stalker_balkan_from_check(self) -> None:
        rows = []
        for row in range(self.stalker_check_table.rowCount()):
            works_item = self.stalker_check_table.item(row, 2)
            if not works_item or works_item.text() != "DA":
                continue
            rows.append(
                f"{self.stalker_check_table.item(row, 0).text()}\n"
                f"{self.stalker_check_table.item(row, 1).text()}"
            )
        if not rows:
            QMessageBox.information(
                self,
                self.translate_static_text("Balkan MAC test"),
                self.translate_static_text("Nema ispravnih profila u tabu Provjera portala."),
            )
            return
        self.stalker_balkan_table.setRowCount(0)
        self.add_stalker_balkan_profiles_from_text("\n\n".join(rows))

    def toggle_stalker_balkan_check(self) -> None:
        if self.stalker_balkan_worker and self.stalker_balkan_worker.isRunning():
            self.stalker_balkan_worker.stop()
            self.statusBar().showMessage(
                self.translate_static_text("Zaustavljanje Balkan MAC testa..."),
                4000,
            )
            return

        pasted = self.stalker_balkan_input.toPlainText().strip()
        if pasted:
            self.add_stalker_balkan_profiles_from_text(pasted)
            self.stalker_balkan_input.clear()

        profiles = self.stalker_balkan_profiles()
        if not profiles:
            QMessageBox.information(
                self,
                self.translate_static_text("Balkan MAC test"),
                self.translate_static_text("Nema portal/MAC profila za Balkan test."),
            )
            return

        with table_sorting_paused(self.stalker_balkan_table):
            for row in range(self.stalker_balkan_table.rowCount()):
                pending = self.translate_static_text("Čeka test...")
                for column, value in enumerate(["—", "—", "—", pending, "—", "—"], start=2):
                    self.stalker_balkan_table.setItem(row, column, QTableWidgetItem(value))

        self.stalker_balkan_progress.setMaximum(len(profiles))
        self.stalker_balkan_progress.setValue(0)
        self.stalker_balkan_worker = StalkerBalkanMacWorker(
            profiles,
            sample_size=self.stalker_balkan_sample_size.value(),
            timeout=self.stalker_balkan_timeout.value(),
            language=self.language,
        )
        self.stalker_balkan_worker.result.connect(self.add_stalker_balkan_result)
        self.stalker_balkan_worker.progress.connect(self.stalker_balkan_progress_changed)
        self.stalker_balkan_worker.log.connect(lambda message: self.statusBar().showMessage(message, 3000))
        self.stalker_balkan_worker.finished_scan.connect(self.stalker_balkan_finished)
        self.stalker_balkan_worker.start()
        self.statusBar().showMessage(
            self.translate_static_text("Balkan MAC test je pokrenut."),
            4000,
        )

    def stop_stalker_balkan_check(self) -> None:
        if self.stalker_balkan_worker and self.stalker_balkan_worker.isRunning():
            self.stalker_balkan_worker.stop()
            self.statusBar().showMessage(
                self.translate_static_text("Zaustavljanje Balkan MAC testa..."),
                4000,
            )
        else:
            self.statusBar().showMessage(
                self.translate_static_text("Balkan MAC test nije pokrenut."),
                3000,
            )

    def stalker_balkan_progress_changed(self, done: int, total: int) -> None:
        self.stalker_balkan_progress.setMaximum(total)
        self.stalker_balkan_progress.setValue(done)

    def add_stalker_balkan_result(self, result: dict[str, str]) -> None:
        for row in range(self.stalker_balkan_table.rowCount()):
            portal_item = self.stalker_balkan_table.item(row, 0)
            mac_item = self.stalker_balkan_table.item(row, 1)
            if not portal_item or not mac_item:
                continue
            if portal_item.text() == result["portal"] and mac_item.text() == result["mac"]:
                values = [
                    result["balkan"],
                    result["works"],
                    result["tested"],
                    result["status"],
                    result["samples"],
                    result["ping"],
                ]
                with table_sorting_paused(self.stalker_balkan_table):
                    for offset, value in enumerate(values, start=2):
                        item = QTableWidgetItem(value)
                        if offset in {2, 3}:
                            item.setForeground(
                                QColor("#62d6a7" if value in {"DA", "YES"} else "#ff839f")
                            )
                        self.stalker_balkan_table.setItem(row, offset, item)
                return

    def stalker_balkan_finished(self) -> None:
        self.stalker_balkan_worker = None
        self.statusBar().showMessage(
            self.translate_static_text("Balkan MAC test je završen."),
            5000,
        )

    def remove_selected_stalker_balkan_rows(self) -> None:
        rows = sorted({index.row() for index in self.stalker_balkan_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.stalker_balkan_table.removeRow(row)
        self.statusBar().showMessage(
            f"{self.translate_static_text('Uklonjeno odabranih Balkan MAC profila:')} {len(rows)}",
            5000,
        )

    def stalker_balkan_table_menu(self, position) -> None:
        row = self.stalker_balkan_table.rowAt(position.y())
        if row < 0:
            return
        self.stalker_balkan_table.setCurrentCell(row, 0)
        menu = QMenu(self)
        send_studio = menu.addAction("Pošalji odabrano u Studio")
        self.translate_menu(menu)
        chosen = menu.exec(
            self.stalker_balkan_table.viewport().mapToGlobal(position)
        )
        if chosen == send_studio:
            self.open_stalker_balkan_row_in_studio(row)

    def open_stalker_balkan_row_in_studio(self, row: int) -> None:
        portal_item = self.stalker_balkan_table.item(row, 0)
        mac_item = self.stalker_balkan_table.item(row, 1)
        if not portal_item or not mac_item:
            return
        self.open_profile_in_stalker(portal_item.text(), mac_item.text())

    def export_stalker_balkan_results(self) -> None:
        if not self.stalker_balkan_table.rowCount():
            QMessageBox.information(
                self,
                self.translate_static_text("Balkan MAC test"),
                self.translate_static_text("Nema rezultata za export."),
            )
            return
        headers = [
            "Portal URL",
            "MAC adresa",
            "Balkan",
            "Radi Balkan",
            "Testirano",
            "Status",
            "Uzorci",
            "Vrijeme",
        ]
        lines = ["\t".join(headers)]
        for row in range(self.stalker_balkan_table.rowCount()):
            values = []
            for column in range(len(headers)):
                item = self.stalker_balkan_table.item(row, column)
                values.append((item.text() if item else "").replace("\t", " "))
            lines.append("\t".join(values))
        self.save_text("\n".join(lines), "stalker_balkan_mac_rezultati.txt")

    def refresh_vault(self) -> None:
        rows = self.vault.rows()
        with table_sorting_paused(self.vault_table):
            self.vault_table.setRowCount(0)
            for record in rows:
                row = self.vault_table.rowCount()
                self.vault_table.insertRow(row)
                values = [
                    record["id"],
                    record["status"],
                    record["server"],
                    record["username"],
                    record["password"],
                    record["expiry"],
                    record["connections"],
                    record["content"],
                    record["checked_at"],
                ]
                state = self.account_state(dict(record))
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    if state == "active":
                        item.setForeground(QColor("#62d6a7" if column == 1 else "#e8ecf6"))
                    elif state == "expired":
                        item.setForeground(QColor("#ffcc66"))
                    else:
                        item.setForeground(QColor("#ff839f"))
                    self.vault_table.setItem(row, column, item)
        if hasattr(self, "metric_vault"):
            self.metric_vault.value.setText(str(len(rows)))
        if hasattr(self, "saved_lists_table"):
            self.refresh_saved_lists()

    @staticmethod
    def account_state(record: dict[str, object]) -> str:
        status = str(record.get("status", "")).strip().lower()
        if status not in {"active", "online"}:
            return "inactive"
        expiry = str(record.get("expiry", "")).strip()
        if expiry in {"", "—", "Bez isteka"}:
            return "active"
        for pattern in ("%d.%m.%Y.", "%d.%m.%Y"):
            try:
                if datetime.strptime(expiry, pattern).date() < datetime.now().date():
                    return "expired"
                return "active"
            except ValueError:
                continue
        return "active"

    def selected_vault_account(self) -> dict[str, str] | None:
        row = self.vault_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Arhiva", "Označi račun u arhivi.")
            return None
        return {
            "id": self.vault_table.item(row, 0).text(),
            "status": self.vault_table.item(row, 1).text(),
            "server": self.vault_table.item(row, 2).text(),
            "username": self.vault_table.item(row, 3).text(),
            "password": self.vault_table.item(row, 4).text(),
            "expiry": self.vault_table.item(row, 5).text(),
        }

    def vault_account_menu(self, position) -> None:
        row = self.vault_table.rowAt(position.y())
        if row < 0:
            return
        self.vault_table.setCurrentCell(row, 0)
        menu = QMenu(self)
        generator = menu.addAction("Povuci u Generator")
        scan = menu.addAction("Pošalji u provjeru")
        copy_login = menu.addAction("Kopiraj server / korisnik / lozinka")
        menu.addSeparator()
        delete = menu.addAction("Obriši označeno")
        self.translate_menu(menu)
        chosen = menu.exec(self.vault_table.viewport().mapToGlobal(position))
        if chosen == generator:
            self.load_vault_account_to_generator()
        elif chosen == scan:
            self.send_vault_account_to_scan()
        elif chosen == copy_login:
            QApplication.clipboard().setText(
                "\n".join(
                    self.vault_table.item(row, column).text()
                    for column in (2, 3, 4)
                )
            )
        elif chosen == delete:
            self.delete_vault_row()

    def load_vault_account_to_generator(self) -> None:
        account = self.selected_vault_account()
        if not account:
            return
        self.open_xtream_studio_with_account(
            account["server"],
            account["username"],
            account["password"],
        )
        self.statusBar().showMessage("Račun je povučen iz arhive u Xtream Generator.", 5000)

    def send_vault_account_to_scan(self) -> None:
        account = self.selected_vault_account()
        if not account:
            return
        playlist_url = (
            f"{account['server'].rstrip('/')}/get.php?"
            f"username={account['username']}&password={account['password']}&type=m3u_plus"
        )
        self.append_text(self.scan_input, playlist_url)
        self.select_xtream_tab("Provjera računa")
        self.statusBar().showMessage("Račun je poslan iz arhive u Xtream provjeru.", 5000)

    def suggest_vault_cleanup(self) -> None:
        rows = [dict(row) for row in self.vault.rows()]
        removable = [row for row in rows if self.account_state(row) != "active"]
        if not removable:
            QMessageBox.information(self, "Arhiva", "Nema isteklih ili neaktivnih računa za micanje.")
            return
        answer = QMessageBox.question(
            self,
            "Predloženo čišćenje",
            f"Pronađeno je {len(removable)} isteklih ili neaktivnih računa. Želiš ih obrisati iz arhive?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for record in removable:
            self.vault.delete(int(record["id"]))
        self.refresh_vault()
        self.statusBar().showMessage(f"Uklonjeno zapisa: {len(removable)}", 5000)

    def refresh_saved_lists(self) -> None:
        rows = self.vault.saved_lists()
        tables = []
        if hasattr(self, "xtream_saved_lists_table"):
            tables.append(self.xtream_saved_lists_table)
        if hasattr(self, "saved_lists_table"):
            tables.append(self.saved_lists_table)
        for table_widget in tables:
            table_widget.setRowCount(0)
        for record in rows:
            kind = str(record["kind"])
            is_mac = any(marker in kind for marker in ("MAC", "Stalker"))
            target = self.saved_lists_table if is_mac else self.xtream_saved_lists_table
            with table_sorting_paused(target):
                row = target.rowCount()
                target.insertRow(row)
                values = [
                    record["id"],
                    record["name"],
                    kind,
                    record["source"],
                    record["item_count"],
                    record["created_at"],
                ]
                for column, value in enumerate(values):
                    target.setItem(row, column, QTableWidgetItem(str(value)))
        self.filter_vault_tables()

    def filter_vault_tables(self) -> None:
        if hasattr(self, "vault_table") and hasattr(self, "vault_account_filter"):
            needle = self.vault_account_filter.text().strip().lower()
            for row in range(self.vault_table.rowCount()):
                values = [
                    self.vault_table.item(row, column).text().lower()
                    for column in range(self.vault_table.columnCount())
                    if self.vault_table.item(row, column)
                ]
                self.vault_table.setRowHidden(row, bool(needle) and not any(needle in value for value in values))
        if hasattr(self, "saved_list_filter"):
            needle = self.saved_list_filter.text().strip().lower()
            for table_widget in (
                getattr(self, "xtream_saved_lists_table", None),
                getattr(self, "saved_lists_table", None),
            ):
                if not table_widget:
                    continue
                for row in range(table_widget.rowCount()):
                    values = [
                        table_widget.item(row, column).text().lower()
                        for column in range(table_widget.columnCount())
                        if table_widget.item(row, column)
                    ]
                    table_widget.setRowHidden(row, bool(needle) and not any(needle in value for value in values))

    def selected_saved_list(self):
        table_widget = getattr(self, "_active_saved_lists_table", self.saved_lists_table)
        if table_widget.currentRow() < 0 and hasattr(self, "xtream_saved_lists_table"):
            table_widget = self.xtream_saved_lists_table
        row = table_widget.currentRow()
        if row < 0:
            QMessageBox.information(self, "Arhiva", "Označi spremljenu listu.")
            return None
        list_id = int(table_widget.item(row, 0).text())
        record = self.vault.saved_list(list_id)
        if not record:
            QMessageBox.information(self, "Arhiva", "Lista više ne postoji u bazi.")
            self.refresh_vault()
            return None
        return record

    def saved_list_menu(self, table_widget: QTableWidget, position) -> None:
        row = table_widget.rowAt(position.y())
        if row < 0:
            return
        table_widget.setCurrentCell(row, 0)
        self._active_saved_lists_table = table_widget
        menu = QMenu(self)
        open_list = menu.addAction("Otvori listu")
        open_generator = menu.addAction("Vrati u Generator")
        send_scan = menu.addAction("Pošalji u provjeru")
        copy_list = menu.addAction("Kopiraj listu")
        export_list = menu.addAction("Export liste")
        menu.addSeparator()
        delete_list = menu.addAction("Obriši listu")
        self.translate_menu(menu)
        chosen = menu.exec(table_widget.viewport().mapToGlobal(position))
        if chosen == open_list:
            self.open_saved_list()
        elif chosen == open_generator:
            self.open_saved_list_in_generator()
        elif chosen == send_scan:
            self.send_saved_list_to_scan()
        elif chosen == copy_list:
            self.copy_saved_list()
        elif chosen == export_list:
            self.export_saved_list()
        elif chosen == delete_list:
            self.delete_saved_list_row()

    def open_saved_list(self) -> None:
        record = self.selected_saved_list()
        if not record:
            return
        content = str(record["content"])
        kind = str(record["kind"])
        if "MAC" in kind or "Stalker" in kind:
            self.mac_group_output.setPlainText(content)
            if "Stalker" in kind:
                self.add_stalker_profiles_from_text(content)
                self.stalker_tabs.setCurrentIndex(0)
                self.select_main_tab("Stalker Studio")
            else:
                self.stalker_tabs.setCurrentIndex(1)
                self.select_main_tab("Stalker Studio")
        else:
            self.url_output.setPlainText(content)
            self.select_xtream_tab("Analiza")
        self.statusBar().showMessage(f"Lista je otvorena iz arhive: {record['name']}", 5000)

    def send_saved_list_to_scan(self) -> None:
        record = self.selected_saved_list()
        if not record:
            return
        urls = extract_playlist_urls(str(record["content"]), playlists_only=False).urls
        xtream_urls = [url for url in urls if "username=" in url and "password=" in url]
        if not xtream_urls:
            QMessageBox.information(self, "Arhiva", "U listi nema Xtream URL-ova za provjeru.")
            return
        self.append_text(self.scan_input, "\n".join(xtream_urls))
        self.select_xtream_tab("Provjera računa")
        self.statusBar().showMessage(f"Poslano URL-ova u provjeru: {len(xtream_urls)}", 5000)

    def export_saved_list(self) -> None:
        record = self.selected_saved_list()
        if not record:
            return
        default_name = f"{str(record['name']).replace('/', '_')}.m3u"
        path = self.get_save_path("Export liste", default_name, "Sve datoteke (*)")
        if path:
            Path(path).write_text(str(record["content"]), encoding="utf-8")

    def copy_saved_list(self) -> None:
        record = self.selected_saved_list()
        if record:
            QApplication.clipboard().setText(str(record["content"]))

    def open_saved_list_in_generator(self) -> None:
        record = self.selected_saved_list()
        if not record:
            return
        rows = self.parse_m3u_content(str(record["content"]))
        if not rows:
            QMessageBox.information(self, "Generator", "Spremljena lista nije M3U format.")
            return
        self.playlist_rows["Live"] = rows
        self.playlist_rows["VOD"] = []
        self.playlist_rows["Serije"] = []
        self.select_xtream_tab("Studio · Live / VOD / Series")
        self.gen_tabs.setCurrentIndex(0)
        self.refresh_generator_groups()
        self.filter_playlist()
        self.statusBar().showMessage(f"Lista je vraćena u Generator: {record['name']}", 5000)

    def delete_saved_list_row(self) -> None:
        record = self.selected_saved_list()
        if not record:
            return
        self.vault.delete_saved_list(int(record["id"]))
        self.refresh_vault()

    def delete_all_saved_lists(self) -> None:
        if not self.vault.saved_lists():
            return
        if self.confirm_bulk_actions:
            answer = QMessageBox.question(
                self,
                "Obriši sve liste",
                "Želiš obrisati sve spremljene liste iz arhive?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.vault.delete_all_saved_lists()
        self.refresh_vault()

    def delete_vault_row(self) -> None:
        row = self.vault_table.currentRow()
        if row < 0:
            return
        self.vault.delete(int(self.vault_table.item(row, 0).text()))
        self.refresh_vault()

    def delete_all_vault_accounts(self) -> None:
        if not self.vault.rows():
            return
        if self.confirm_bulk_actions:
            answer = QMessageBox.question(
                self,
                "Obriši sve račune",
                "Želiš obrisati sve račune iz arhive?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.vault.delete_all_accounts()
        self.refresh_vault()

    def export_vault(self) -> None:
        path = self.get_save_path("Export arhive", "aurora_arhiva.json", "JSON (*.json)")
        if not path:
            return
        data = [dict(row) for row in self.vault.rows()]
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def export_vault_csv(self) -> None:
        rows = [dict(row) for row in self.vault.rows()]
        if not rows:
            return
        path = self.get_save_path("Export arhive", "aurora_arhiva.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def backup_full_vault(self) -> None:
        path = self.get_save_path("Backup arhive", "aurora_backup.json", "JSON (*.json)")
        if not path:
            return
        data = {
            "accounts": [dict(row) for row in self.vault.rows()],
            "lists": [
                dict(self.vault.saved_list(int(row["id"])))
                for row in self.vault.saved_lists()
                if self.vault.saved_list(int(row["id"]))
            ],
        }
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.statusBar().showMessage(f"Backup spremljen: {path}", 7000)

    def restore_full_vault(self) -> None:
        path = self.get_open_path("Restore backup", "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            accounts = data.get("accounts", []) if isinstance(data, dict) else []
            lists = data.get("lists", []) if isinstance(data, dict) else []
            imported_accounts = 0
            imported_lists = 0
            for row in accounts:
                if not isinstance(row, dict):
                    continue
                if row.get("server") and row.get("username"):
                    self.vault.save(
                        {
                            "server": str(row.get("server", "")),
                            "username": str(row.get("username", "")),
                            "password": str(row.get("password", "")),
                            "status": str(row.get("status", "")),
                            "expiry": str(row.get("expiry", "")),
                            "connections": str(row.get("connections", "")),
                            "content": str(row.get("content", "")),
                            "playlist_url": str(row.get("playlist_url", "")),
                        }
                    )
                    imported_accounts += 1
            for row in lists:
                if not isinstance(row, dict) or not row.get("content"):
                    continue
                self.vault.save_list(
                    str(row.get("name", "Backup lista")),
                    str(row.get("kind", "Lista")),
                    str(row.get("source", "Backup")),
                    str(row.get("content", "")),
                    int(row.get("item_count", 0) or 0),
                )
                imported_lists += 1
            self.refresh_vault()
            QMessageBox.information(
                self,
                "Restore backup",
                f"Uvezeno računa: {imported_accounts}\nUvezeno lista: {imported_lists}",
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.critical(self, "Restore backup", str(error))

    def import_vault(self) -> None:
        path = self.get_open_path("Import arhive", "JSON (*.json)")
        if not path:
            return
        try:
            rows = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError("JSON mora sadržavati listu zapisa.")
            imported = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                normalized = {
                    "server": str(row.get("server", "")),
                    "username": str(row.get("username", row.get("user", ""))),
                    "password": str(row.get("password", row.get("pass", ""))),
                    "status": str(row.get("status", "")),
                    "expiry": str(row.get("expiry", "")),
                    "connections": str(row.get("connections", "")),
                    "content": str(row.get("content", "")),
                    "playlist_url": str(row.get("playlist_url", row.get("url", ""))),
                }
                if normalized["server"] and normalized["username"]:
                    self.vault.save(normalized)
                    imported += 1
            self.refresh_vault()
            QMessageBox.information(self, "Import", f"Uvezeno zapisa: {imported}")
        except (OSError, ValueError, json.JSONDecodeError) as error:
            QMessageBox.critical(self, "Import", str(error))

    def browse_player(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Odaberi player", "", "Sve datoteke (*)")
        if path:
            self.setting_player.setText(path)

    def browse_export_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Odaberi export folder", self.default_export_dir)
        if path:
            self.setting_export_dir.setText(path)

    def default_path(self, filename: str) -> str:
        directory = Path(getattr(self, "default_export_dir", str(APP_DIR))).expanduser()
        return str(directory / filename)

    def get_save_path(self, title: str, default_name: str, file_filter: str) -> str:
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.translate_static_text(title),
            self.default_path(default_name),
            self.translate_static_text(file_filter),
        )
        return path

    def get_open_path(self, title: str, file_filter: str) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.translate_static_text(title),
            self.default_export_dir,
            self.translate_static_text(file_filter),
        )
        return path

    def preview_app_preferences(self) -> None:
        if hasattr(self, "setting_theme"):
            self.theme = self.setting_theme.currentData() or "dark"
        if hasattr(self, "setting_language"):
            self.language = self.setting_language.currentData() or "en"
        self.apply_theme()
        self.refresh_ui_language()
        self.settings.setValue("theme", self.theme)
        self.settings.setValue("language", self.language)
        self.settings.sync()

    def refresh_ui_language(self) -> None:
        if hasattr(self, "setting_theme"):
            theme_originals = ["Dark", "Light"]
            for index, original in enumerate(theme_originals):
                if index < self.setting_theme.count():
                    self.setting_theme.setItemText(index, self.translate_static_text(original))
        if hasattr(self, "setting_language"):
            language_originals = ["English", "Hrvatski"]
            for index, original in enumerate(language_originals):
                if index < self.setting_language.count():
                    self.setting_language.setItemText(index, self.translate_static_text(original))
        self.subtitle_label.setText(self.tr_ui("subtitle"))
        self.connection_label.setText(self.tr_ui("ready"))
        self.dashboard_heading.setText(self.tr_ui("dashboard_heading"))
        self.dashboard_description.setText(self.tr_ui("dashboard_description"))
        self.quick_title.setText(self.tr_ui("quick_start"))
        self.quick_text.setText(self.guide_html())
        main_tabs = {
            self.tr_ui("home"): 0,
            "Xtream Studio": 1,
            "Stalker Studio": 2,
            self.tr_ui("archive"): 3,
            self.tr_ui("settings"): 4,
        }
        for title, index in main_tabs.items():
            if index < self.tabs.count():
                self.tabs.setTabText(index, title)
        self.apply_static_translations()
        self.statusBar().showMessage(self.tr_ui("status_ready"), 3000)

    def save_app_settings(self) -> None:
        self.preview_app_preferences()
        self.default_export_dir = self.setting_export_dir.text().strip() or str(APP_DIR)
        self.auto_save_active = self.setting_auto_save_active.isChecked()
        self.confirm_bulk_actions = self.setting_confirm_bulk.isChecked()
        self.remember_last_tab = self.setting_remember_tab.isChecked()
        self.check_updates_on_startup = self.setting_check_updates_startup.isChecked()
        self.settings.setValue("theme", self.theme)
        self.settings.setValue("language", self.language)
        self.settings.setValue("export_dir", self.default_export_dir)
        self.settings.setValue("auto_save_active", self.auto_save_active)
        self.settings.setValue("confirm_bulk_actions", self.confirm_bulk_actions)
        self.settings.setValue("remember_last_tab", self.remember_last_tab)
        self.settings.setValue("check_updates_on_startup", self.check_updates_on_startup)
        self.settings.setValue("user_agent", self.setting_user_agent.currentText())
        self.settings.setValue("proxies", self.setting_proxy.toPlainText())
        self.settings.setValue("player", self.setting_player.text())
        self.settings.sync()
        self.statusBar().showMessage("Postavke su spremljene.", 5000)

    def save_text(self, content: str, default_name: str) -> None:
        path = self.get_save_path("Spremi", default_name, "Tekst (*.txt)")
        if path:
            Path(path).write_text(content, encoding="utf-8")

    def _load_settings(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.theme = str(self.settings.value("theme", self.theme))
        self.language = str(self.settings.value("language", self.language))
        if self.language not in UI_TEXT:
            self.language = "en"
        if self.theme not in {"dark", "light"}:
            self.theme = "dark"
        self.default_export_dir = str(self.settings.value("export_dir", self.default_export_dir))
        self.auto_save_active = str(self.settings.value("auto_save_active", self.auto_save_active)).lower() not in {"false", "0"}
        self.confirm_bulk_actions = str(self.settings.value("confirm_bulk_actions", self.confirm_bulk_actions)).lower() not in {"false", "0"}
        self.remember_last_tab = str(self.settings.value("remember_last_tab", self.remember_last_tab)).lower() not in {"false", "0"}
        self.check_updates_on_startup = str(
            self.settings.value("check_updates_on_startup", self.check_updates_on_startup)
        ).lower() not in {"false", "0"}
        if hasattr(self, "setting_theme"):
            theme_index = self.setting_theme.findData(self.theme)
            if theme_index >= 0:
                self.setting_theme.setCurrentIndex(theme_index)
        if hasattr(self, "setting_language"):
            language_index = self.setting_language.findData(self.language)
            if language_index >= 0:
                self.setting_language.setCurrentIndex(language_index)
        self.apply_theme()
        self.refresh_ui_language()
        self.scan_threads.setValue(int(self.settings.value("scan_threads", 8)))
        self.scan_timeout.setValue(int(self.settings.value("scan_timeout", 12)))
        self.setting_user_agent.setCurrentText(
            str(self.settings.value("user_agent", self.setting_user_agent.currentText()))
        )
        self.setting_proxy.setPlainText(str(self.settings.value("proxies", "")))
        self.setting_player.setText(str(self.settings.value("player", "/usr/bin/vlc")))
        self.setting_export_dir.setText(self.default_export_dir)
        self.setting_auto_save_active.setChecked(self.auto_save_active)
        self.setting_confirm_bulk.setChecked(self.confirm_bulk_actions)
        self.setting_remember_tab.setChecked(self.remember_last_tab)
        self.setting_check_updates_startup.setChecked(self.check_updates_on_startup)
        if self.remember_last_tab:
            self.select_main_tab(str(self.settings.value("last_main_tab", self.tr_ui("home"))))

    def closeEvent(self, event) -> None:
        for worker in [
            self.scan_worker,
            self.mac_worker,
            self.stalker_check_worker,
            self.stalker_balkan_worker,
            self.playlist_worker,
            self.update_check_worker,
            self.update_download_worker,
        ]:
            if worker and worker.isRunning():
                if hasattr(worker, "stop"):
                    worker.stop()
                else:
                    worker.requestInterruption()
                    worker.quit()
                worker.wait(1500)
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("scan_threads", self.scan_threads.value())
        self.settings.setValue("scan_timeout", self.scan_timeout.value())
        if self.remember_last_tab:
            self.settings.setValue("last_main_tab", self.tabs.tabText(self.tabs.currentIndex()))
        if self.fusion_window:
            try:
                self.fusion_window.save_settings()
                if hasattr(self.fusion_window, "stop_background_work"):
                    self.fusion_window.stop_background_work()
                for worker_name in ("worker", "vault_worker", "super_thread", "proxy_thread"):
                    worker = getattr(self.fusion_window, worker_name, None)
                    if worker and hasattr(worker, "isRunning") and worker.isRunning():
                        if hasattr(worker, "is_running"):
                            worker.is_running = False
                        worker.quit()
                        worker.wait(1000)
                for thread in list(getattr(self.fusion_window, "stream_threads", [])):
                    if thread and hasattr(thread, "isRunning") and thread.isRunning():
                        if hasattr(thread, "is_running"):
                            thread.is_running = False
                        thread.quit()
                        thread.wait(800)
                        if thread.isRunning() and hasattr(thread, "terminate"):
                            thread.terminate()
                            thread.wait(300)
                self.fusion_window.stream_threads = []
                stalker_window = getattr(self.fusion_window, "stalker_window", None)
                if stalker_window:
                    worker = getattr(stalker_window, "worker", None)
                    if worker and hasattr(worker, "isRunning") and worker.isRunning():
                        if hasattr(worker, "is_running"):
                            worker.is_running = False
                        worker.quit()
                        worker.wait(800)
                    stalker_window.close()
            except Exception:
                pass
        if self.stalker_embedded_window:
            try:
                self.stalker_embedded_window._save_settings()
                client = getattr(self.stalker_embedded_window, "client", None)
                if client:
                    client.close()
            except Exception:
                pass
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Aurora IPTV")
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setStyleSheet(STYLE)
    splash_image = QPixmap(str(APP_ICON_PATH)).scaled(
        256,
        256,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    splash = QSplashScreen(splash_image)
    splash.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    splash.showMessage("Pokrećem aplikaciju…", Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, QColor("#e5e7eb"))
    splash.show()
    app.processEvents()
    window = AuroraWindow()
    window.setWindowIcon(QIcon(str(APP_ICON_PATH)))

    def show_main_window() -> None:
        window.show()
        splash.finish(window)

    QTimer.singleShot(3000, show_main_window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
