from PyQt6.QtWidgets import QPushButton, QFrame, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt

STYLE_SHEET = """
    QMainWindow { background-color: #0d1117; }
    #SideBar { background-color: #161b22; border-right: 1px solid #30363d; min-width: 180px; }
    #StatCard { background-color: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 15px; }
    QTableWidget { background-color: #0d1117; color: #c9d1d9; gridline-color: #30363d; border: none; font-family: 'Segoe UI'; }
    QHeaderView::section { background-color: #161b22; color: #58a6ff; font-weight: bold; border: none; padding: 10px; border-right: 1px solid #30363d; }
    QPushButton#MenuBtn { color: #8b949e; text-align: left; padding: 12px 20px; border: none; font-size: 14px; background: transparent; }
    QPushButton#MenuBtn:hover { background-color: #21262d; color: white; border-radius: 8px; }
    QPushButton#ActionBtn { background-color: #238636; color: white; border-radius: 6px; font-weight: bold; padding: 15px; border: none; font-size: 14px; }
    QPushButton#ActionBtn:hover { background-color: #2ea043; }
    QPushButton#StopBtn { background-color: #da3633; color: white; border-radius: 6px; font-weight: bold; padding: 15px; border: none; font-size: 14px; }
    QPushButton#StopBtn:hover { background-color: #b32d2a; }
    QTextEdit, QLineEdit, QCheckBox { background-color: #0d1117; border: 1px solid #30363d; color: #c9d1d9; border-radius: 8px; padding: 10px; }
    QProgressBar { border: 1px solid #30363d; border-radius: 5px; text-align: center; color: white; background-color: #161b22; height: 20px;}
    QProgressBar::chunk { background-color: #58a6ff; border-radius: 4px; }

    /* DEBLJI I VIDLJIVIJI RAZDJELNICI PROZORA (Splitteri) */
    QSplitter::handle {
        background-color: #21262d;
        border: 1px solid #30363d;
    }
    QSplitter::handle:horizontal {
        width: 8px;
    }
    QSplitter::handle:hover {
        background-color: #58a6ff;
    }
    QSplitter::handle:pressed {
        background-color: #3d5afe;
    }
"""

class StatCard(QFrame):
    def __init__(self, title, color):
        super().__init__()
        self.setObjectName("StatCard")
        layout = QVBoxLayout(self)
        self.lbl_title = QLabel(title)
        self.lbl_title.setStyleSheet("color: #8b949e; font-size: 11px; font-weight: bold; text-transform: uppercase;")
        self.lbl_val = QLabel("0")
        self.lbl_val.setStyleSheet(f"color: {color}; font-size: 28px; font-weight: bold;")
        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_val)
