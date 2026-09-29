import sys
import os
import json
import threading
import time
import random
import re # Added for the smart renamer context peek

import keyboard
from PIL import ImageGrab, Image, ImageChops, ImageStat, ImageDraw, ImageFont

from PySide6.QtCore import Qt, QObject, QTimer, Signal, QRect, QPoint, QSize
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen, QPixmap, QImage, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QWidget, QDialog, QTabWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QCheckBox, QRadioButton, QButtonGroup, QLineEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QListWidget, QAbstractItemView, QFrame,
    QPlainTextEdit, QScrollArea, QStackedWidget, QMessageBox, QInputDialog,
    QFileDialog, QColorDialog,
)

APP_TITLE = "Auto-Scapture Test Build (1.6.2)"


# ==========================================
# --- SMALL QT HELPERS ---
# ==========================================
class UiBridge(QObject):
    """Runs callables on the GUI thread (Qt equivalent of root.after(0, fn))."""
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(lambda fn: fn())


class PopupComboBox(QComboBox):
    """Combobox that emits a signal right before its dropdown opens (<<ComboboxPost>>)."""
    aboutToShowPopup = Signal()

    def showPopup(self):
        self.aboutToShowPopup.emit()
        super().showPopup()


def make_button(text, slot=None, bg=None, fg=None, bold=False, point_size=None):
    btn = QPushButton(text.replace("&", "&&"))  # '&' would otherwise become a shortcut underline
    if slot: btn.clicked.connect(slot)
    style = []
    if bg: style.append(f"background-color: {bg};")
    if fg or bg: style.append(f"color: {fg or 'black'};")
    if style:
        btn.setStyleSheet("QPushButton {" + " ".join(style) + " padding: 4px 10px; border: 1px solid #9a9a9a; border-radius: 3px; }"
                          "QPushButton:hover { border: 1px solid #555; }"
                          "QPushButton:disabled { color: #8a8a8a; }")
    if bold or point_size:
        f = btn.font()
        if bold: f.setBold(True)
        if point_size: f.setPointSize(point_size)
        btn.setFont(f)
    return btn


def set_combo_items(combo, items):
    """Replace a combobox's dropdown values without clobbering typed text."""
    text = combo.currentText()
    combo.blockSignals(True)
    combo.clear()
    combo.addItems(list(items))
    if combo.isEditable():
        combo.setEditText(text)
    else:
        combo.setCurrentIndex(combo.findText(text))
    combo.blockSignals(False)


def pil_to_pixmap(img):
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, img.width * 4, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


def h_separator():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


def info(parent, title, text): QMessageBox.information(parent, title, text)
def warn(parent, title, text): QMessageBox.warning(parent, title, text)
def error(parent, title, text): QMessageBox.critical(parent, title, text)
def ask_yes_no(parent, title, text):
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes


def apply_light_theme(app):
    """Classic light look (like the original Tk build), even when Windows is in dark mode."""
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in [(QPalette.Window, "#f0f0f0"), (QPalette.WindowText, "#000000"),
                        (QPalette.Base, "#ffffff"), (QPalette.AlternateBase, "#f7f7f7"),
                        (QPalette.Text, "#000000"), (QPalette.Button, "#e1e1e1"),
                        (QPalette.ButtonText, "#000000"), (QPalette.BrightText, "#ff0000"),
                        (QPalette.Highlight, "#0078d7"), (QPalette.HighlightedText, "#ffffff"),
                        (QPalette.ToolTipBase, "#ffffdc"), (QPalette.ToolTipText, "#000000"),
                        (QPalette.PlaceholderText, "#808080"), (QPalette.Link, "#0000ff")]:
        pal.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#a0a0a0"))
    app.setPalette(pal)


DARK_PANEL_QSS = """
QFrame#darkPanel { background-color: #1e1e1e; }
QFrame#darkPanel QLabel { color: white; background: transparent; }
QFrame#darkPanel QGroupBox { color: white; border: 1px solid #555; margin-top: 10px; padding-top: 6px; }
QFrame#darkPanel QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
"""


# ==========================================
# --- SNIPPING OVERLAY ---
# ==========================================
class SnipOverlay(QWidget):
    """Translucent full-screen overlay for dragging out a capture region.
    Emits the region in physical screen pixels so it lines up with ImageGrab."""
    regionSelected = Signal(tuple)

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowOpacity(0.3)
        self.setCursor(Qt.CrossCursor)
        self.screen_obj = QGuiApplication.primaryScreen()
        self.setGeometry(self.screen_obj.geometry())
        self.start = None
        self.end = None

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("gray"))
        if self.start and self.end:
            p.setPen(QPen(QColor("red"), 3))
            p.setBrush(QColor("black"))
            p.drawRect(QRect(self.start, self.end).normalized())

    def mousePressEvent(self, event):
        self.start = self.end = event.position().toPoint()
        self.update()

    def mouseMoveEvent(self, event):
        if self.start:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if not self.start: return
        self.end = event.position().toPoint()
        dpr = self.screen_obj.devicePixelRatio()
        x1, x2 = sorted((self.start.x(), self.end.x()))
        y1, y2 = sorted((self.start.y(), self.end.y()))
        region = (round(x1 * dpr), round(y1 * dpr), round(x2 * dpr), round(y2 * dpr))
        self.close()
        self.regionSelected.emit(region)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()


# ==========================================
# --- REDACTION CANVAS ---
# ==========================================
class RedactionCanvas(QWidget):
    def __init__(self, original_img, state, text_source):
        super().__init__()
        self.base_pixmap = pil_to_pixmap(original_img)
        self.state = state
        self.text_source = text_source
        self.scaled = None
        self.drag_rect = None
        self.setCursor(Qt.CrossCursor)
        self.refresh_canvas()

    def refresh_canvas(self):
        z = self.state['zoom']
        size = QSize(max(1, int(self.base_pixmap.width() * z)), max(1, int(self.base_pixmap.height() * z)))
        self.scaled = self.base_pixmap.scaled(size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.setFixedSize(size)
        self.update()

    def paintEvent(self, event):
        z = self.state['zoom']
        p = QPainter(self)
        p.drawPixmap(0, 0, self.scaled)
        p.setPen(QColor("black"))
        p.setBrush(QColor("black"))
        for box in self.state['rectangles']:
            p.drawRect(QRect(QPoint(int(box[0] * z), int(box[1] * z)), QPoint(int(box[2] * z), int(box[3] * z))))

        font = QFont("Arial")
        font.setPixelSize(max(1, int(24 * z)))
        p.setFont(font)
        ascent = QFontMetrics(font).ascent()
        for x, y, text_str, color in self.state['texts']:
            p.setPen(QColor(color))
            p.drawText(int(x * z), int(y * z) + ascent, text_str)

        if self.drag_rect:
            p.setPen(QPen(QColor("red"), 2))
            p.setBrush(QColor("black"))
            x1, y1, x2, y2 = self.drag_rect
            p.drawRect(QRect(QPoint(int(x1 * z), int(y1 * z)), QPoint(int(x2 * z), int(y2 * z))).normalized())

    def wheelEvent(self, event):
        if event.angleDelta().y() > 0:
            self.state['zoom'] *= 1.1
        else:
            self.state['zoom'] /= 1.1
        self.refresh_canvas()
        event.accept()

    def _img_pos(self, event):
        z = self.state['zoom']
        pos = event.position()
        return pos.x() / z, pos.y() / z

    def mousePressEvent(self, event):
        x, y = self._img_pos(event)
        if self.state['tool'] == 'box':
            self.state['start_x'], self.state['start_y'] = x, y
            self.drag_rect = (x, y, x, y)
            self.update()
        elif self.state['tool'] == 'text':
            txt_val = self.text_source().strip()
            if txt_val:
                self.state['texts'].append((x, y, txt_val, self.state['color']))
                self.state['history'].append('text')
                self.update()

    def mouseMoveEvent(self, event):
        if self.state['tool'] == 'box' and self.drag_rect:
            x, y = self._img_pos(event)
            self.drag_rect = (self.state['start_x'], self.state['start_y'], x, y)
            self.update()

    def mouseReleaseEvent(self, event):
        if self.state['tool'] == 'box' and self.drag_rect:
            cur_x, cur_y = self._img_pos(event)
            sx, sy = self.state['start_x'], self.state['start_y']
            coords = (min(sx, cur_x), min(sy, cur_y), max(sx, cur_x), max(sy, cur_y))
            if (coords[2] - coords[0]) > 2 and (coords[3] - coords[1]) > 2:
                self.state['rectangles'].append(coords)
                self.state['history'].append('box')
            self.drag_rect = None
            self.update()


# ==========================================
# --- MAIN APPLICATION ---
# ==========================================
class ScreenCaptureApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)

        self.bridge = UiBridge()

        self.counter = 1
        self.capture_region = None
        self.is_listening = False
        self.session_images = []
        self.reference_end_image = None

        self.current_session_folder = None
        self._toasts = []

        # When frozen by PyInstaller, __file__ lives in a temp folder, so keep settings next to the .exe
        if getattr(sys, "frozen", False):
            self.script_dir = os.path.dirname(sys.executable)
        else:
            self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.settings_file = os.path.join(self.script_dir, "settings.json")

        self.settings_data = self.load_settings()
        self.presets = self.settings_data.get("presets", {})

        prefs = self.settings_data.get("preferences", {})
        self.auto_open_pdf = prefs.get("auto_open_pdf", True)
        self.is_pinned = False

        self.saved_hotkeys = prefs.get("saved_hotkeys", ["ctrl+shift+a", "f5"])
        self.saved_filenames = prefs.get("saved_filenames", ["Module1_"])

        self.renamer_extensions = prefs.get("renamer_extensions", [".txt", ".cfg", ".csv", ".json", ".md", ".log"])

        # ==========================================
        # --- TABBED INTERFACE ---
        # ==========================================
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.notebook = QTabWidget()
        outer.addWidget(self.notebook)

        self.cap_tab = QWidget()
        self.rename_tab = QWidget()

        self.notebook.addTab(self.cap_tab, "📸 Auto-Scapture")
        self.notebook.addTab(self.rename_tab, "📂 Lab Renamer")

        self.setup_capture_ui()
        self.setup_renamer_ui()

        self.resize(480, self.sizeHint().height())

    def ui_call(self, fn):
        """Thread-safe: schedule fn on the GUI thread."""
        self.bridge.call.emit(fn)

    # ==========================================
    # --- TAB 1: EXACT 1.1.1 CAPTURE UI ---
    # ==========================================
    def setup_capture_ui(self):
        lay = QVBoxLayout(self.cap_tab)
        lay.setContentsMargins(10, 5, 10, 10)
        center = Qt.AlignHCenter

        top_frame = QHBoxLayout()
        self.pin_btn = QCheckBox("📌 Pin Window")
        self.pin_btn.toggled.connect(self.toggle_pin)
        top_frame.addWidget(self.pin_btn)
        top_frame.addStretch()
        self.settings_btn = make_button("⚙️ Preferences", self.open_settings_window, bg="#e0e0e0")
        top_frame.addWidget(self.settings_btn)
        lay.addLayout(top_frame)

        title_label = QLabel(APP_TITLE)
        f = QFont("Helvetica", 12); f.setBold(True)
        title_label.setFont(f)
        lay.addWidget(title_label, alignment=center)
        lay.addSpacing(10)

        lay.addWidget(QLabel("Master Save Directory:"), alignment=center)
        folder_frame = QHBoxLayout()
        folder_frame.setContentsMargins(10, 0, 10, 0)
        default_path = os.path.join(os.path.expanduser("~"), "Desktop", "AutoCaptures")
        self.folder_entry = QLineEdit(default_path)
        folder_frame.addWidget(self.folder_entry)
        self.browse_btn = make_button("Browse", self.browse_folder)
        folder_frame.addWidget(self.browse_btn)
        lay.addLayout(folder_frame)
        lay.addSpacing(10)

        def labelled_row(label_text):
            box = QVBoxLayout()
            box.setContentsMargins(10, 0, 10, 0)
            box.addWidget(QLabel(label_text))
            row = QHBoxLayout()
            box.addLayout(row)
            lay.addLayout(box)
            return row

        name_inner = labelled_row("Base File Name:")
        self.name_combo = PopupComboBox()
        self.name_combo.setEditable(True)
        self.name_combo.aboutToShowPopup.connect(self.scan_for_pdfs)
        name_inner.addWidget(self.name_combo, 1)
        self.save_name_btn = make_button("Save", self.save_filename)
        name_inner.addWidget(self.save_name_btn)
        self.del_name_btn = make_button("Delete", self.delete_filename)
        name_inner.addWidget(self.del_name_btn)

        hk_inner = labelled_row("Capture Hotkey:")
        self.hotkey_combo = QComboBox()
        self.hotkey_combo.setEditable(True)
        self.hotkey_combo.addItems(self.saved_hotkeys)
        self.hotkey_combo.setEditText(self.saved_hotkeys[0] if self.saved_hotkeys else "ctrl+shift+a")
        hk_inner.addWidget(self.hotkey_combo, 1)
        self.save_hk_btn = make_button("Save", self.save_hotkey)
        hk_inner.addWidget(self.save_hk_btn)
        self.del_hk_btn = make_button("Delete", self.delete_hotkey)
        hk_inner.addWidget(self.del_hk_btn)

        preset_inner = labelled_row("Saved Area Presets:")
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(self.presets.keys()))
        self.preset_combo.setCurrentIndex(-1)
        self.preset_combo.textActivated.connect(self.apply_preset)
        preset_inner.addWidget(self.preset_combo, 1)
        self.save_preset_btn = make_button("Save Area", self.save_preset)
        preset_inner.addWidget(self.save_preset_btn)
        self.del_preset_btn = make_button("Delete", self.delete_preset)
        preset_inner.addWidget(self.del_preset_btn)

        lay.addSpacing(10)
        self.select_btn = make_button("1. Select Screen Area", self.activate_snipping)
        lay.addWidget(self.select_btn, alignment=center)

        self.status_label = QLabel("Area: Not Selected")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: blue;")
        lay.addWidget(self.status_label, alignment=center)

        mode_frame = QGroupBox("Capture Mode")
        mode_lay = QVBoxLayout(mode_frame)
        self.mode_group = QButtonGroup(self)
        self.mode_radios = {}
        for value, text in [("Manual", "Mode 1: Manual (Hotkey)"),
                            ("Auto", "Mode 2: Auto-Capture (Fixed Slide Count)"),
                            ("Smart", "Mode 3: Smart Auto (Detect Section Title/End)")]:
            rb = QRadioButton(text)
            rb.toggled.connect(lambda checked: checked and self.update_mode_ui())
            self.mode_group.addButton(rb)
            self.mode_radios[value] = rb
            mode_lay.addWidget(rb)

        self.dynamic_settings_frame = QWidget()
        self.dyn_grid = QGridLayout(self.dynamic_settings_frame)
        self.dyn_grid.setContentsMargins(0, 5, 0, 5)
        mode_lay.addWidget(self.dynamic_settings_frame)
        lay.addWidget(mode_frame)

        self.auto_count_lbl = QLabel("Slides:")
        self.auto_count_entry = QSpinBox()
        self.auto_count_entry.setRange(1, 9999)
        self.auto_count_entry.setValue(10)

        prefs = self.settings_data.get("preferences", {})

        self.auto_delay_lbl = QLabel("Delay (s):")
        self.delay_frame = QWidget()
        delay_lay = QHBoxLayout(self.delay_frame)
        delay_lay.setContentsMargins(0, 0, 0, 0)
        self.auto_delay_min_entry = QDoubleSpinBox()
        self.auto_delay_max_entry = QDoubleSpinBox()
        for spin, val in [(self.auto_delay_min_entry, prefs.get("delay_min", 1.5)),
                          (self.auto_delay_max_entry, prefs.get("delay_max", 2.0))]:
            spin.setRange(0.0, 600.0)
            spin.setDecimals(1)
            spin.setSingleStep(0.1)
            spin.setValue(float(val))
        delay_lay.addWidget(self.auto_delay_min_entry)
        delay_lay.addWidget(QLabel("-"))
        delay_lay.addWidget(self.auto_delay_max_entry)

        self.auto_key_lbl = QLabel("Key:")
        self.auto_key_combo = QComboBox()
        self.auto_key_combo.addItems(["right", "space", "enter", "down", "page down"])

        self.set_ref_btn = make_button("Set Current Screen as Target 'End' Slide", self.capture_reference_image, bg="#ffeb99")
        self.ref_status_lbl = QLabel("Target Slide: NOT SET")
        self.ref_status_lbl.setStyleSheet("color: red;")

        self.dyn_widgets = [self.auto_count_lbl, self.auto_count_entry, self.auto_delay_lbl, self.delay_frame,
                            self.auto_key_lbl, self.auto_key_combo, self.set_ref_btn, self.ref_status_lbl]

        self.cont_cb = QCheckBox("Enable Continuous Capture (Hold key to spam)")
        self.cont_cb.setChecked(prefs.get("continuous_capture", False))
        self.cont_cb.toggled.connect(self.save_settings)
        lay.addWidget(self.cont_cb, alignment=center)

        self.start_btn = make_button("2. Start Capture Sequence", self.toggle_listening)
        self.start_btn.setEnabled(False)
        lay.addWidget(self.start_btn, alignment=center)

        lay.addSpacing(5)
        lay.addWidget(h_separator())
        lay.addSpacing(5)

        self.load_btn = make_button("📂 Load Past Images into Session", self.load_past_images, bg="#fff2cc")
        lay.addWidget(self.load_btn, alignment=center)

        self.review_btn = make_button("👁️ Review / Redact Current Session", self.open_review_window, bg="#cfe2f3")
        lay.addWidget(self.review_btn, alignment=center)

        self.clear_btn = make_button("🗑️ Clear Current Session", self.clear_session, bg="#ffcccc")
        lay.addWidget(self.clear_btn, alignment=center)

        self.pdf_btn = make_button("3. Export Session to PDF", self.export_to_pdf, bg="#d9ead3")
        lay.addWidget(self.pdf_btn, alignment=center)

        self.session_label = QLabel("Images in current session: 0")
        lay.addWidget(self.session_label, alignment=center)
        lay.addStretch()

        if self.saved_filenames:
            self.name_combo.setEditText(self.saved_filenames[0])
        else:
            self.name_combo.setEditText("Module1_")
        self.scan_for_pdfs()
        self.mode_radios["Manual"].setChecked(True)
        self.update_mode_ui()

    def capture_mode(self):
        for value, rb in self.mode_radios.items():
            if rb.isChecked(): return value
        return "Manual"

    # ==========================================
    # --- TAB 2: LAB RENAMER UI ---
    # ==========================================

    def setup_renamer_ui(self):
        self.rename_files_list = []
        lay = QVBoxLayout(self.rename_tab)
        lay.setContentsMargins(20, 10, 20, 10)

        title = QLabel("Bulk File Renamer")
        f = QFont("Helvetica", 14); f.setBold(True)
        title.setFont(f)
        lay.addWidget(title, alignment=Qt.AlignHCenter)
        lay.addSpacing(10)

        sel_btn = make_button("📂 1. Select Files to Rename", self.load_rename_files, point_size=10)
        sel_btn.setMinimumHeight(40)
        sel_row = QHBoxLayout(); sel_row.setContentsMargins(20, 0, 20, 0); sel_row.addWidget(sel_btn)
        lay.addLayout(sel_row)

        self.rename_lb_main = QListWidget()
        self.rename_lb_main.setFont(QFont("Consolas", 9))
        lay.addWidget(self.rename_lb_main, 1)

        self.rename_count_lbl = QLabel("Files Loaded: 0")
        lay.addWidget(self.rename_count_lbl, alignment=Qt.AlignHCenter)

        rev_btn = make_button("👁️ 2. Review, Convert & Rename", self.open_renamer_review_window, bg="#cfe2f3", bold=True, point_size=10)
        rev_btn.setMinimumHeight(40)
        rev_row = QHBoxLayout(); rev_row.setContentsMargins(20, 10, 20, 10); rev_row.addWidget(rev_btn)
        lay.addLayout(rev_row)
        lay.addWidget(make_button("🗑️ Clear Loaded Files", self.clear_rename_list, bg="#ffcccc"), alignment=Qt.AlignHCenter)

    def load_rename_files(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "Select Files", "", "Text Files (*.txt);;Config Files (*.cfg);;CSV Files (*.csv);;All Files (*.*)")
        if fs:
            self.rename_files_list = [os.path.normpath(p) for p in fs]
            self.refresh_renamer_main_list()

    def clear_rename_list(self):
        self.rename_files_list = []
        self.refresh_renamer_main_list()

    def refresh_renamer_main_list(self):
        self.rename_lb_main.clear()
        for f in self.rename_files_list:
            self.rename_lb_main.addItem(os.path.basename(f))
        self.rename_count_lbl.setText(f"Files Loaded: {len(self.rename_files_list)}")

    def _new_dialog(self, parent, title, w, h):
        dlg = QDialog(parent)
        dlg.setWindowTitle(title)
        dlg.resize(w, h)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.setWindowModality(Qt.ApplicationModal)
        if self.is_pinned: dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        return dlg

    def _selected_rows(self, listwidget):
        return sorted(listwidget.row(i) for i in listwidget.selectedItems())

    def open_renamer_review_window(self):
        if not self.rename_files_list:
            info(self, "Empty", "Please load some files first!")
            return

        r_win = self._new_dialog(self, "Review, Convert, Merge & Rename", 1000, 700)
        r_win.setStyleSheet(DARK_PANEL_QSS)
        root_lay = QHBoxLayout(r_win)

        left_frame = QWidget()
        left_frame.setFixedWidth(300)
        left = QVBoxLayout(left_frame)
        left.setContentsMargins(0, 0, 0, 0)
        root_lay.addWidget(left_frame)

        right_frame = QFrame()
        right_frame.setObjectName("darkPanel")
        right = QVBoxLayout(right_frame)
        root_lay.addWidget(right_frame, 1)

        hdr = QLabel("Reorder Files:")
        f = QFont("Arial", 10); f.setBold(True); hdr.setFont(f)
        left.addWidget(hdr, alignment=Qt.AlignHCenter)

        self.r_listbox = QListWidget()
        self.r_listbox.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.r_listbox.setFont(QFont("Arial", 10))
        left.addWidget(self.r_listbox, 1)

        for fp in self.rename_files_list:
            self.r_listbox.addItem(os.path.basename(fp))

        btn_grid = QGridLayout()
        btn_grid.addWidget(make_button("⬆ Up", self.move_rename_up), 0, 0)
        btn_grid.addWidget(make_button("⬇ Down", self.move_rename_down), 0, 1)
        btn_grid.addWidget(make_button("❌ Del", self.remove_rename_item, fg="red"), 0, 2)
        btn_grid.addWidget(make_button("⇈ Top", self.move_rename_top), 1, 0)
        btn_grid.addWidget(make_button("⇊ Bot", self.move_rename_bottom), 1, 1)
        btn_grid.addWidget(make_button("⇄ Rev", self.reverse_rename_list), 1, 2)
        left.addLayout(btn_grid)

        self.preview_title = QLabel("Preview")
        f = QFont("Arial", 10); f.setBold(True); self.preview_title.setFont(f)
        right.addWidget(self.preview_title, alignment=Qt.AlignHCenter)

        single_frame = QGroupBox("Manual Edit Selected File")
        single = QHBoxLayout(single_frame)
        self.single_name_entry = QLineEdit()
        single.addWidget(self.single_name_entry, 1)
        self.single_ext_combo = QComboBox()
        self.single_ext_combo.setEditable(True)
        self.single_ext_combo.addItems(self.renamer_extensions)
        self.single_ext_combo.setMinimumWidth(80)
        single.addWidget(self.single_ext_combo)
        single.addWidget(make_button("Rename File", self.rename_single_file, bg="#ffeb99"))
        right.addWidget(single_frame)

        self.preview_stack = QStackedWidget()
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setStyleSheet("QPlainTextEdit { background-color: #2d2d2d; color: #d4d4d4; }")
        self.preview_img_lbl = QLabel()
        self.preview_img_lbl.setAlignment(Qt.AlignCenter)
        self.preview_stack.addWidget(self.preview_text)
        self.preview_stack.addWidget(self.preview_img_lbl)
        right.addWidget(self.preview_stack, 1)
        self.r_listbox.itemSelectionChanged.connect(self.update_rename_preview)

        rename_frame = QGroupBox("Batch Sequential Naming && Conversion")
        rg = QGridLayout(rename_frame)
        rg.addWidget(QLabel("Base:"), 0, 0)
        self.rn_base_entry = QLineEdit("Lab1-SC_")
        rg.addWidget(self.rn_base_entry, 0, 1)
        rg.addWidget(QLabel("Idx:"), 0, 2)
        self.rn_start_spin = QSpinBox()
        self.rn_start_spin.setRange(0, 999999)
        self.rn_start_spin.setValue(1)
        rg.addWidget(self.rn_start_spin, 0, 3)
        rg.addWidget(QLabel("Ext:"), 0, 4)
        self.rn_ext_combo = QComboBox()
        self.rn_ext_combo.setEditable(True)
        self.rn_ext_combo.addItems(["Keep Original"] + self.renamer_extensions)
        self.rn_ext_combo.setMinimumWidth(120)
        rg.addWidget(self.rn_ext_combo, 0, 5)
        rg.addWidget(make_button("APPLY RENAME & CONVERT TO ALL", lambda: self.execute_batch_rename(r_win),
                                 bg="#4CAF50", fg="white", bold=True, point_size=10), 1, 0, 1, 6)
        rg.setColumnStretch(1, 1)
        right.addWidget(rename_frame)

        combine_frame = QGroupBox("File Merger")
        cl = QVBoxLayout(combine_frame)
        cl.addWidget(make_button("🔗 COMBINE ALL LISTED FILES INTO ONE", self.combine_files, bg="#337ab7", fg="white", bold=True, point_size=10))
        right.addWidget(combine_frame)

        if self.rename_files_list:
            self.r_listbox.setCurrentRow(0)
            self.update_rename_preview()
        r_win.show()

    def update_rename_preview(self):
        rows = self._selected_rows(self.r_listbox)
        if not rows: return
        idx = rows[0]
        path = self.rename_files_list[idx]

        self.preview_title.setText(f"Preview: {os.path.basename(path)}")
        base_n = os.path.splitext(os.path.basename(path))[0]
        ext = os.path.splitext(path)[1].lower()

        self.single_name_entry.setText(base_n)
        self.single_ext_combo.setEditText(ext)

        if ext in [".txt", ".cfg", ".csv", ".log", ".md", ".xml", ".json", ".ini", ".data"]:
            self.preview_stack.setCurrentWidget(self.preview_text)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    self.preview_text.setPlainText(f.read(2000))
            except Exception as e:
                self.preview_text.setPlainText(f"Could not read file:\n{e}")

        elif ext in [".png", ".jpg", ".jpeg", ".bmp"]:
            self.preview_stack.setCurrentWidget(self.preview_img_lbl)
            pix = QPixmap(path)
            if pix.isNull():
                self.preview_img_lbl.setPixmap(QPixmap())
                self.preview_img_lbl.setText("Image not readable")
                self.preview_img_lbl.setStyleSheet("color: red;")
            else:
                self.preview_img_lbl.setStyleSheet("")
                self.preview_img_lbl.setPixmap(pix.scaled(450, 450, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.preview_stack.setCurrentWidget(self.preview_text)
            self.preview_text.setPlainText(f"File format ({ext}) preview not supported.\n\nFile Location:\n{path}")

    def rename_single_file(self):
        rows = self._selected_rows(self.r_listbox)
        if not rows:
            warn(self.r_listbox.window(), "Warning", "No file selected in the list.")
            return
        idx = rows[0]
        old_path = self.rename_files_list[idx]
        dir_n = os.path.dirname(old_path)
        parent = self.r_listbox.window()

        new_name = self.single_name_entry.text().strip()
        new_ext = self.single_ext_combo.currentText().strip()

        if not new_ext.startswith(".") and new_ext: new_ext = "." + new_ext
        if not new_name:
            error(parent, "Error", "File name cannot be empty.")
            return

        new_path = os.path.join(dir_n, new_name + new_ext)
        if old_path != new_path:
            if os.path.exists(new_path) and not ask_yes_no(parent, "Overwrite?", f"'{os.path.basename(new_path)}' exists. Overwrite?"):
                return
            try:
                os.replace(old_path, new_path)
                self.rename_files_list[idx] = new_path
                self.r_listbox.item(idx).setText(os.path.basename(new_path))
                self.refresh_renamer_main_list()

                if new_ext and new_ext not in self.renamer_extensions:
                    self.renamer_extensions.append(new_ext)
                    self.save_settings()
                    set_combo_items(self.single_ext_combo, self.renamer_extensions)
                    set_combo_items(self.rn_ext_combo, ["Keep Original"] + self.renamer_extensions)

                info(parent, "Success", f"File renamed to {os.path.basename(new_path)}")
            except Exception as e:
                error(parent, "Error", f"Failed to rename:\n{e}")

    def _refresh_r_listbox(self):
        self.r_listbox.clear()
        for f in self.rename_files_list:
            self.r_listbox.addItem(os.path.basename(f))

    def _move_rename(self, dest_fn):
        rows = self._selected_rows(self.r_listbox)
        if len(rows) != 1: return
        idx = rows[0]
        new_idx = dest_fn(idx, len(self.rename_files_list))
        if new_idx != idx:
            item = self.rename_files_list.pop(idx)
            self.rename_files_list.insert(new_idx, item)
            self._refresh_r_listbox()
            self.r_listbox.setCurrentRow(new_idx)
            self.update_rename_preview()

    def move_rename_up(self): self._move_rename(lambda i, n: max(i - 1, 0))
    def move_rename_down(self): self._move_rename(lambda i, n: min(i + 1, n - 1))
    def move_rename_top(self): self._move_rename(lambda i, n: 0)
    def move_rename_bottom(self): self._move_rename(lambda i, n: n - 1)

    def reverse_rename_list(self):
        self.rename_files_list.reverse()
        self._refresh_r_listbox()

    def remove_rename_item(self):
        rows = self._selected_rows(self.r_listbox)
        if not rows: return
        for idx in reversed(rows):
            del self.rename_files_list[idx]
            self.r_listbox.takeItem(idx)
        self.preview_text.clear()
        self.preview_img_lbl.setPixmap(QPixmap())
        self.preview_title.setText("Preview")

    def execute_batch_rename(self, window):
        base = self.rn_base_entry.text()
        start_idx = self.rn_start_spin.value()
        new_ext_val = self.rn_ext_combo.currentText().strip()

        if new_ext_val != "Keep Original" and not new_ext_val.startswith("."):
            new_ext_val = "." + new_ext_val
        count = 0

        rn_smart_cb = getattr(self, 'rn_smart_cb', None)
        for i, old_path in enumerate(self.rename_files_list):
            dir_n = os.path.dirname(old_path)
            ext = os.path.splitext(old_path)[1]
            final_ext = ext if new_ext_val == "Keep Original" else new_ext_val
            new_name = f"{base}{start_idx + i}{final_ext}"

            if rn_smart_cb is not None and rn_smart_cb.isChecked() and final_ext.lower() in [".txt", ".cfg", ".log", ".md", ".csv", ".ini"]:
                try:
                    with open(old_path, 'r', encoding='utf-8', errors='ignore') as f:
                        for _ in range(5):
                            line = f.readline()
                            m = re.search(r"(?:Title|Experiment|Lab|Name):\s*(.*)", line, re.I)
                            if m:
                                clean = "".join(x for x in m.group(1).strip() if x.isalnum() or x in " -_")
                                if clean:
                                    new_name = f"{clean}{final_ext}"
                                    break
                except Exception as e:
                    print(f"Error reading file {old_path}: {e}")

            new_path = os.path.join(dir_n, new_name)
            try:
                os.rename(old_path, new_path)
                self.rename_files_list[i] = new_path
                count += 1
            except Exception as e:
                print(f"Failed to rename {old_path}: {e}")

        if new_ext_val != "Keep Original" and new_ext_val not in self.renamer_extensions:
            self.renamer_extensions.append(new_ext_val)
            self.save_settings()

        info(window, "Success", f"Successfully renamed {count} files.")
        window.close()
        self.refresh_renamer_main_list()

    def combine_files(self):
        parent = QApplication.activeWindow() or self
        if not self.rename_files_list:
            info(parent, "Empty", "No files to combine.")
            return

        save_path, _ = QFileDialog.getSaveFileName(
            parent, "Save Combined File", "Combined_Output.txt",
            "Text File (*.txt);;Config File (*.cfg);;Markdown (*.md);;All Files (*.*)"
        )

        if not save_path:
            return

        try:
            with open(save_path, 'w', encoding='utf-8') as outfile:
                for fp in self.rename_files_list:
                    filename = os.path.basename(fp)
                    outfile.write(f"{'='*50}\n--- START FILE: {filename} ---\n{'='*50}\n\n")
                    try:
                        with open(fp, 'r', encoding='utf-8', errors='ignore') as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"[Error reading file: {e}]")
                    outfile.write("\n\n")
            info(parent, "Success", f"Combined {len(self.rename_files_list)} files into:\n{save_path}")
        except Exception as e:
            error(parent, "Error", f"Failed to combine files:\n{e}")

    def open_settings_window(self):
        settings_win = self._new_dialog(self, "Auto-Scapture Preferences", 650, 350)
        lay = QVBoxLayout(settings_win)

        top_pref_frame = QHBoxLayout()
        cb = QCheckBox("Auto-open PDF after export")
        cb.setChecked(self.auto_open_pdf)
        def on_auto_open(checked):
            self.auto_open_pdf = checked
            self.save_settings()
        cb.toggled.connect(on_auto_open)
        top_pref_frame.addWidget(cb)
        top_pref_frame.addStretch()
        top_pref_frame.addWidget(make_button("📄 View Raw settings.json", self.view_settings_file))
        lay.addLayout(top_pref_frame)

        lay.addWidget(h_separator())
        hdr = QLabel("Manage Saved Settings")
        f = QFont("Arial", 10); f.setBold(True); hdr.setFont(f)
        lay.addWidget(hdr, alignment=Qt.AlignHCenter)

        lists_frame = QHBoxLayout()
        lay.addLayout(lists_frame, 1)

        def make_column(title, items, on_remove):
            col = QVBoxLayout()
            col.addWidget(QLabel(title), alignment=Qt.AlignHCenter)
            lb = QListWidget()
            lb.addItems(list(items))
            col.addWidget(lb, 1)
            def remove():
                item = lb.currentItem()
                if item is None: return
                val = item.text()
                lb.takeItem(lb.row(item))
                on_remove(val)
            col.addWidget(make_button("Remove", remove), alignment=Qt.AlignHCenter)
            lists_frame.addLayout(col)

        # 1. Base Names
        def remove_pref_name(val):
            self.saved_filenames.remove(val)
            self.save_settings()
            self.scan_for_pdfs()
            if self.name_combo.currentText() == val: self.name_combo.setEditText("")
        make_column("Base Names", self.saved_filenames, remove_pref_name)

        # 2. Hotkeys
        def remove_pref_hk(val):
            self.saved_hotkeys.remove(val)
            set_combo_items(self.hotkey_combo, self.saved_hotkeys)
            self.save_settings()
            if self.hotkey_combo.currentText() == val: self.hotkey_combo.setEditText("")
        make_column("Hotkeys", self.saved_hotkeys, remove_pref_hk)

        # 3. Area Presets
        def remove_pref_preset(val):
            was_selected = self.preset_combo.currentText() == val
            del self.presets[val]
            set_combo_items(self.preset_combo, self.presets.keys())
            self.save_settings()
            if was_selected:
                self.preset_combo.setCurrentIndex(-1)
                self.capture_region = None
                self.set_status("Area: Not Selected")
                self.start_btn.setEnabled(False)
        make_column("Area Presets", self.presets.keys(), remove_pref_preset)

        # 4. File Extensions
        def remove_pref_ext(val):
            self.renamer_extensions.remove(val)
            self.save_settings()
        make_column("File Exts", self.renamer_extensions, remove_pref_ext)

        settings_win.show()

    def view_settings_file(self):
        if not os.path.exists(self.settings_file): self.save_settings()
        os.startfile(self.settings_file)

    def load_settings(self):
        default_data = {
            "presets": {},
            "preferences": {
                "auto_open_pdf": True,
                "continuous_capture": False,
                "saved_hotkeys": ["ctrl+shift+a"],
                "saved_filenames": ["Module1_"],
                "delay_min": 1.5,
                "delay_max": 2.0,
                "renamer_extensions": [".txt", ".cfg", ".csv", ".json", ".md", ".log"]
            }
        }
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r") as f:
                    data = json.load(f)
                    if "preferences" not in data: data["preferences"] = default_data["preferences"]
                    if "renamer_extensions" not in data["preferences"]:
                        data["preferences"]["renamer_extensions"] = default_data["preferences"]["renamer_extensions"]
                    return data
            except Exception: return default_data
        return default_data

    def save_settings(self):
        data = {
            "presets": self.presets,
            "preferences": {
                "auto_open_pdf": self.auto_open_pdf,
                "continuous_capture": self.cont_cb.isChecked(),
                "saved_hotkeys": self.saved_hotkeys,
                "saved_filenames": self.saved_filenames,
                "delay_min": self.auto_delay_min_entry.value(),
                "delay_max": self.auto_delay_max_entry.value(),
                "renamer_extensions": self.renamer_extensions
            }
        }
        try:
            with open(self.settings_file, "w") as f: json.dump(data, f, indent=4)
        except OSError as e:
            print(f"Could not save settings: {e}")

    def set_status(self, text):
        self.status_label.setText(text)

    def update_mode_ui(self):
        for w in self.dyn_widgets:
            self.dyn_grid.removeWidget(w)
            w.hide()
        self.cont_cb.hide()

        def place(w, row, col, rowspan=1, colspan=1):
            self.dyn_grid.addWidget(w, row, col, rowspan, colspan)
            w.show()

        mode = self.capture_mode()

        if mode == "Manual":
            self.cont_cb.show()
            self.start_btn.setText("2. Start Listening")
            self.hotkey_combo.setEnabled(True)
            self.save_hk_btn.setEnabled(True)
            self.del_hk_btn.setEnabled(True)

        elif mode == "Auto":
            place(self.auto_count_lbl, 0, 0)
            place(self.auto_count_entry, 0, 1)
            place(self.auto_delay_lbl, 0, 2)
            place(self.delay_frame, 0, 3)
            place(self.auto_key_lbl, 0, 4)
            place(self.auto_key_combo, 0, 5)
            self.start_btn.setText("2. Start Auto-Capture (3s Delay)")
            self.hotkey_combo.setEnabled(False)
            self.save_hk_btn.setEnabled(False)
            self.del_hk_btn.setEnabled(False)

        elif mode == "Smart":
            place(self.set_ref_btn, 0, 0, 1, 3)
            place(self.ref_status_lbl, 0, 3, 1, 3)
            place(self.auto_delay_lbl, 1, 0)
            place(self.delay_frame, 1, 1, 1, 2)
            place(self.auto_key_lbl, 1, 3)
            place(self.auto_key_combo, 1, 4, 1, 2)
            self.start_btn.setText("2. Start Smart Capture (3s Delay)")
            self.hotkey_combo.setEnabled(False)
            self.save_hk_btn.setEnabled(False)
            self.del_hk_btn.setEnabled(False)

    def capture_reference_image(self):
        if not self.capture_region:
            warn(self, "No Area", "Please select a Screen Area first!")
            return

        self.setWindowOpacity(0.0)
        QApplication.processEvents()
        time.sleep(0.2)

        self.reference_end_image = ImageGrab.grab(bbox=self.capture_region)
        self.ref_status_lbl.setText("Target Slide: SAVED")
        self.ref_status_lbl.setStyleSheet("color: green;")

        self.setWindowOpacity(1.0)
        info(self, "Target Set", "The current screen has been saved as the Target End Slide.\n\nNavigate to the START of your presentation before starting the macro.")

    def scan_for_pdfs(self):
        folder = self.folder_entry.text()
        pdf_names = set(self.saved_filenames)
        if os.path.exists(folder):
            for item in os.listdir(folder):
                path = os.path.join(folder, item)
                if os.path.isfile(path) and item.lower().endswith(".pdf"):
                    base_name = item[:-4] + "_"
                    pdf_names.add(base_name)
                elif os.path.isdir(path):
                    try:
                        for sub_item in os.listdir(path):
                            if sub_item.lower().endswith(".pdf"):
                                base_name = sub_item[:-4] + "_"
                                pdf_names.add(base_name)
                    except OSError:
                        pass

        combo_values = sorted(list(pdf_names))
        if not combo_values: combo_values = ["Module1_"]
        set_combo_items(self.name_combo, combo_values)

    def save_filename(self):
        current = self.name_combo.currentText().strip()
        if current and current not in self.saved_filenames:
            self.saved_filenames.append(current)
            self.save_settings()
            self.scan_for_pdfs()
            info(self, "Saved", f"Added '{current}' to your saved file names.")

    def delete_filename(self):
        current = self.name_combo.currentText().strip()
        if current in self.saved_filenames:
            self.saved_filenames.remove(current)
            self.save_settings()
            self.name_combo.setEditText("")
            self.scan_for_pdfs()
            info(self, "Deleted", f"Removed '{current}' from saved file names.")

    def save_hotkey(self):
        current = self.hotkey_combo.currentText().lower().strip()
        if current and current not in self.saved_hotkeys:
            self.saved_hotkeys.append(current)
            set_combo_items(self.hotkey_combo, self.saved_hotkeys)
            self.save_settings()
            info(self, "Saved", f"Added '{current}' to your saved hotkeys.")

    def delete_hotkey(self):
        current = self.hotkey_combo.currentText().lower().strip()
        if current in self.saved_hotkeys:
            self.saved_hotkeys.remove(current)
            set_combo_items(self.hotkey_combo, self.saved_hotkeys)
            self.hotkey_combo.setEditText(self.saved_hotkeys[0] if self.saved_hotkeys else "")
            self.save_settings()
            info(self, "Deleted", f"Removed '{current}' from saved hotkeys.")

    def save_preset(self):
        if not self.capture_region: return
        preset_name, ok = QInputDialog.getText(self, "Save Preset", "Enter a name for this area:")
        if ok and preset_name:
            self.presets[preset_name] = self.capture_region
            self.save_settings()
            set_combo_items(self.preset_combo, self.presets.keys())
            self.preset_combo.setCurrentText(preset_name)

    def delete_preset(self):
        selected = self.preset_combo.currentText()
        if selected in self.presets:
            del self.presets[selected]
            self.save_settings()
            set_combo_items(self.preset_combo, self.presets.keys())
            self.preset_combo.setCurrentIndex(-1)
            self.capture_region = None
            self.set_status("Area: Not Selected")
            self.start_btn.setEnabled(False)

    def toggle_pin(self, checked):
        self.is_pinned = checked
        self.setWindowFlag(Qt.WindowStaysOnTopHint, checked)
        self.show()

    def apply_preset(self, selected):
        if selected in self.presets:
            self.capture_region = tuple(self.presets[selected])
            self.set_status(f"Area Captured! (Preset: {selected})\n{self.capture_region}")
            self.start_btn.setEnabled(True)

    def browse_folder(self):
        folder_selected = QFileDialog.getExistingDirectory(self, "Select Folder", self.folder_entry.text())
        if folder_selected:
            self.folder_entry.setText(os.path.normpath(folder_selected))
            self.scan_for_pdfs()

    def activate_snipping(self):
        self.overlay = SnipOverlay()
        self.overlay.regionSelected.connect(self.on_region_selected)
        self.overlay.show()
        self.overlay.activateWindow()

    def on_region_selected(self, region):
        self.capture_region = region
        self.preset_combo.setCurrentIndex(-1)
        self.set_status(f"Area Captured!\n{self.capture_region}")
        self.start_btn.setEnabled(True)
        self.reference_end_image = None
        self.ref_status_lbl.setText("Target Slide: NOT SET")
        self.ref_status_lbl.setStyleSheet("color: red;")

    def toggle_ui_lock(self, lock=True):
        enabled = not lock

        self.folder_entry.setEnabled(enabled)
        self.name_combo.setEnabled(enabled)
        self.browse_btn.setEnabled(enabled)
        self.preset_combo.setEnabled(enabled)
        self.select_btn.setEnabled(enabled)
        self.load_btn.setEnabled(enabled)

        self.notebook.setTabEnabled(1, enabled)

        mode = self.capture_mode()
        if mode == "Manual":
            self.hotkey_combo.setEnabled(enabled)
            self.cont_cb.setEnabled(enabled)
        elif mode == "Auto":
            self.auto_count_entry.setEnabled(enabled)
            self.auto_delay_min_entry.setEnabled(enabled)
            self.auto_delay_max_entry.setEnabled(enabled)
            self.auto_key_combo.setEnabled(enabled)
        elif mode == "Smart":
            self.set_ref_btn.setEnabled(enabled)
            self.auto_delay_min_entry.setEnabled(enabled)
            self.auto_delay_max_entry.setEnabled(enabled)
            self.auto_key_combo.setEnabled(enabled)

        for rb in self.mode_radios.values():
            rb.setEnabled(enabled)

    def set_start_btn(self, text, color=None):
        self.start_btn.setText(text)
        self.start_btn.setStyleSheet(f"color: {color};" if color else "")

    def toggle_listening(self):
        save_folder = self.folder_entry.text()
        if not os.path.exists(save_folder): os.makedirs(save_folder)

        if not self.is_listening:
            mode = self.capture_mode()

            if mode == "Smart" and self.reference_end_image is None:
                error(self, "Missing Target", "You must click 'Set Current Screen as Target' on the slide you want the macro to stop at before starting.")
                return

            if mode in ["Auto", "Smart"]:
                self.save_settings()

            self.is_listening = True
            self.toggle_ui_lock(lock=True)

            # Read widget values here on the GUI thread; worker threads must not touch widgets
            delay_min = self.auto_delay_min_entry.value()
            delay_max = self.auto_delay_max_entry.value()
            if delay_min > delay_max:
                delay_min, delay_max = delay_max, delay_min
            key = self.auto_key_combo.currentText()

            if mode == "Manual":
                custom_hotkey = self.hotkey_combo.currentText().lower()
                try:
                    keyboard.add_hotkey(custom_hotkey, lambda: self.ui_call(self.take_screenshot))
                    self.current_hotkey = custom_hotkey
                    self._key_locked = False
                except ValueError:
                    error(self, "Error", "Invalid key combination.")
                    self.toggle_ui_lock(lock=False)
                    self.is_listening = False
                    return
                self.set_start_btn(f"Stop Listening ({custom_hotkey})", "red")
            elif mode == "Auto":
                self.set_start_btn("Stop Auto-Capture (Abort)", "red")
                slides = self.auto_count_entry.value()
                threading.Thread(target=self.run_auto_capture_thread, args=(slides, delay_min, delay_max, key), daemon=True).start()
            elif mode == "Smart":
                self.set_start_btn("Stop Smart Capture (Abort)", "red")
                threading.Thread(target=self.run_smart_capture_thread, args=(delay_min, delay_max, key), daemon=True).start()

        else:
            self.is_listening = False
            self.toggle_ui_lock(lock=False)
            mode = self.capture_mode()
            if mode == "Manual":
                try: keyboard.remove_hotkey(self.current_hotkey)
                except (KeyError, ValueError): pass
                self.set_start_btn("2. Start Listening")
            elif mode == "Auto":
                self.set_start_btn("2. Start Auto-Capture (3s Delay)")
            elif mode == "Smart":
                self.set_start_btn("2. Start Smart Capture (3s Delay)")

    def compare_images(self, img1, img2, tolerance=2.0):
        i1 = img1.resize((100, 100)).convert("L")
        i2 = img2.resize((100, 100)).convert("L")
        diff = ImageChops.difference(i1, i2)
        stat = ImageStat.Stat(diff)
        return stat.mean[0] < tolerance

    def run_smart_capture_thread(self, delay_min, delay_max, key):
        for i in range(3, 0, -1):
            if not self.is_listening: return
            self.ui_call(lambda i=i: self.start_btn.setText(f"Starting in {i}..."))
            time.sleep(1)

        if not self.is_listening: return

        max_slides = 200
        suppress_detection = False

        for i in range(max_slides):
            if not self.is_listening: break

            self.ui_call(lambda curr=i+1: self.start_btn.setText(f"Scanning Slide {curr} - Click to Stop"))
            current_img = ImageGrab.grab(bbox=self.capture_region)

            is_match = self.compare_images(current_img, self.reference_end_image)

            if suppress_detection:
                if not is_match:
                    suppress_detection = False
            else:
                if is_match:
                    user_wants_to_stop = [None]

                    def ask_user(i=i):
                        user_wants_to_stop[0] = ask_yes_no(
                            self, "Target Detected!",
                            f"Target slide detected after {i} captures.\n\nDo you want to STOP the macro here?\n\n(Click 'No' to save this slide and continue capturing)."
                        )

                    self.ui_call(ask_user)

                    while user_wants_to_stop[0] is None:
                        if not self.is_listening:
                            break
                        time.sleep(0.1)

                    if not self.is_listening or user_wants_to_stop[0]:
                        break
                    else:
                        suppress_detection = True

            self.ui_call(lambda img=current_img: self.save_raw_image(img))
            time.sleep(0.3)

            if i < max_slides - 1:
                keyboard.send(key)
                actual_delay = random.uniform(delay_min, delay_max)
                time.sleep(actual_delay)

        if i >= max_slides - 1:
            self.ui_call(lambda: warn(self, "Max Slides Reached", "Stopped automatically after 200 slides to prevent infinite loop."))

        if self.is_listening:
            self.ui_call(self.toggle_listening)

    def run_auto_capture_thread(self, slides, delay_min, delay_max, key):
        for i in range(3, 0, -1):
            if not self.is_listening: return
            self.ui_call(lambda i=i: self.start_btn.setText(f"Starting in {i}..."))
            time.sleep(1)

        if not self.is_listening: return

        for i in range(slides):
            if not self.is_listening: break

            self.ui_call(lambda curr=i+1, total=slides: self.start_btn.setText(f"Capturing {curr}/{total} - Click to Stop"))
            self.ui_call(lambda: self.take_screenshot(is_auto=True))
            time.sleep(0.3)

            if i < slides - 1:
                keyboard.send(key)
                actual_delay = random.uniform(delay_min, delay_max)
                time.sleep(actual_delay)

        if self.is_listening:
            self.ui_call(self.toggle_listening)
            self.ui_call(lambda: info(self, "Done", f"Captured {slides} slides!"))

    def check_key_release(self):
        try:
            if keyboard.is_pressed(self.current_hotkey):
                QTimer.singleShot(50, self.check_key_release)
            else:
                self._key_locked = False
        except Exception:
            self._key_locked = False

    def save_raw_image(self, img_object):
        if not self.current_session_folder:
            base = self.name_combo.currentText().strip("_")
            if not base: base = "Session"
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self.current_session_folder = os.path.join(self.folder_entry.text(), f"{base}_{timestamp}")
            os.makedirs(self.current_session_folder, exist_ok=True)

        save_folder = self.current_session_folder
        base_name = self.name_combo.currentText()
        filename = f"{base_name}{self.counter}.png"
        filepath = os.path.join(save_folder, filename)

        img_object.save(filepath)
        self.session_images.append(filepath)
        self.show_toast(filename, len(self.session_images))
        self.session_label.setText(f"Images in current session: {len(self.session_images)}")
        self.counter += 1

    def take_screenshot(self, is_auto=False):
        if not is_auto and not self.cont_cb.isChecked():
            if getattr(self, '_key_locked', False): return
            self._key_locked = True
            self.check_key_release()

        if self.capture_region:
            img = ImageGrab.grab(bbox=self.capture_region)
            self.save_raw_image(img)

    def load_past_images(self):
        folder = self.folder_entry.text()
        if not os.path.exists(folder):
            folder = os.path.expanduser("~")

        filepaths, _ = QFileDialog.getOpenFileNames(
            self, "Select Past Captures", folder,
            "PNG Images (*.png);;JPEG Images (*.jpg *.jpeg);;All Files (*.*)"
        )

        if filepaths:
            added_count = 0
            for path in filepaths:
                path = os.path.normpath(path)
                if path not in self.session_images:
                    self.session_images.append(path)
                    added_count += 1

            self.session_label.setText(f"Images in current session: {len(self.session_images)}")

            if added_count > 0:
                info(self, "Loaded", f"Successfully loaded {added_count} image(s) into the current session.")
            else:
                info(self, "Notice", "No new images were loaded (they may already be in the session).")

    # ==========================================
    # --- CAPTURE REVIEW WINDOW ---
    # ==========================================
    def open_review_window(self):
        if not self.session_images:
            info(self, "Empty Session", "You need to take some screenshots before you can review them!")
            return

        review_win = self._new_dialog(self, "Review Session Queue", 800, 550)
        review_win.setStyleSheet(DARK_PANEL_QSS)
        root_lay = QHBoxLayout(review_win)

        left_frame = QWidget()
        left_frame.setFixedWidth(300)
        left = QVBoxLayout(left_frame)
        left.setContentsMargins(0, 0, 0, 0)
        root_lay.addWidget(left_frame)

        right_frame = QFrame()
        right_frame.setObjectName("darkPanel")
        right = QVBoxLayout(right_frame)
        root_lay.addWidget(right_frame, 1)

        hdr = QLabel("Current Queue:")
        f = QFont("Arial", 10); f.setBold(True); hdr.setFont(f)
        left.addWidget(hdr, alignment=Qt.AlignHCenter)

        listbox = QListWidget()
        listbox.setSelectionMode(QAbstractItemView.ExtendedSelection)
        listbox.setFont(QFont("Arial", 10))
        left.addWidget(listbox, 1)

        def refresh_lb():
            listbox.clear()
            for filepath in self.session_images:
                listbox.addItem(os.path.basename(filepath))

        refresh_lb()

        preview_label = QLabel("Select an image to preview")
        preview_label.setAlignment(Qt.AlignCenter)
        right.addWidget(preview_label, 1)

        def update_preview():
            rows = self._selected_rows(listbox)
            if rows:
                path = self.session_images[rows[0]]
                pix = QPixmap(path)
                if pix.isNull():
                    preview_label.setPixmap(QPixmap())
                    preview_label.setText("Image not found")
                else:
                    preview_label.setPixmap(pix.scaled(500, 500, Qt.KeepAspectRatio, Qt.SmoothTransformation))

        listbox.itemSelectionChanged.connect(update_preview)

        # REORDER BUTTONS
        def move(dest_fn):
            rows = self._selected_rows(listbox)
            if len(rows) != 1: return
            idx = rows[0]
            new_idx = dest_fn(idx, len(self.session_images))
            if new_idx != idx:
                item = self.session_images.pop(idx)
                self.session_images.insert(new_idx, item)
                refresh_lb()
                listbox.setCurrentRow(new_idx)
                update_preview()

        def reverse_list():
            self.session_images.reverse()
            refresh_lb()

        def remove_item():
            rows = self._selected_rows(listbox)
            if not rows: return
            for idx in reversed(rows):
                path = self.session_images[idx]
                try: os.remove(path)
                except OSError: pass
                del self.session_images[idx]
            refresh_lb()
            self.session_label.setText(f"Images in current session: {len(self.session_images)}")
            preview_label.setPixmap(QPixmap())
            preview_label.setText("Select an image to preview")

        btn_grid = QGridLayout()
        btn_grid.addWidget(make_button("⬆ Up", lambda: move(lambda i, n: max(i - 1, 0))), 0, 0)
        btn_grid.addWidget(make_button("⬇ Down", lambda: move(lambda i, n: min(i + 1, n - 1))), 0, 1)
        btn_grid.addWidget(make_button("❌ Del", remove_item, fg="red"), 0, 2)
        btn_grid.addWidget(make_button("⇈ Top", lambda: move(lambda i, n: 0)), 1, 0)
        btn_grid.addWidget(make_button("⇊ Bot", lambda: move(lambda i, n: n - 1)), 1, 1)
        btn_grid.addWidget(make_button("⇄ Rev", reverse_list), 1, 2)
        left.addLayout(btn_grid)

        def open_redaction_tool():
            rows = self._selected_rows(listbox)
            if not rows:
                info(review_win, "Selection", "Please select at least one image to redact.")
                return
            filepaths = [self.session_images[idx] for idx in rows]
            self.launch_manual_redaction(filepaths, review_win, update_preview)

        redact_btn = make_button("🖌️ Open Redaction Tool for Selected Image(s)", open_redaction_tool, bg="#ffd9b3")
        right.addWidget(redact_btn, alignment=Qt.AlignHCenter)

        if self.session_images:
            listbox.setCurrentRow(0)
            update_preview()
        review_win.show()

    def launch_manual_redaction(self, filepaths, parent_win, update_callback):
        try:
            original_img = Image.open(filepaths[0]).convert('RGB')
        except Exception as e:
            error(parent_win, "Error", f"Could not open image for editing:\n{e}")
            return

        title_text = f"Redaction Studio - {os.path.basename(filepaths[0])}"
        if len(filepaths) > 1:
            title_text += f" (+ {len(filepaths)-1} more)"

        w, h = original_img.size
        avail = QGuiApplication.primaryScreen().availableGeometry()
        win_w = min(w + 40, avail.width() - 100)
        win_h = min(h + 100, avail.height() - 100)
        redact_win = self._new_dialog(parent_win, title_text, win_w, win_h)
        lay = QVBoxLayout(redact_win)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        top_bar_w = QFrame()
        top_bar_w.setStyleSheet("QFrame { background-color: #e0e0e0; }")
        top_bar = QHBoxLayout(top_bar_w)
        top_bar.setContentsMargins(5, 5, 5, 5)
        lay.addWidget(top_bar_w)

        state = {'start_x': 0, 'start_y': 0, 'rectangles': [], 'texts': [], 'history': [], 'zoom': 1.0, 'tool': 'box', 'color': 'red'}

        bold9 = QFont("Arial", 9); bold9.setBold(True)
        box_rb = QRadioButton("⬛ Draw Box"); box_rb.setFont(bold9); box_rb.setChecked(True)
        text_rb = QRadioButton("🔤 Add Text"); text_rb.setFont(bold9)
        box_rb.toggled.connect(lambda c: c and state.update(tool='box'))
        text_rb.toggled.connect(lambda c: c and state.update(tool='text'))
        top_bar.addWidget(box_rb)
        top_bar.addWidget(text_rb)

        text_entry = QLineEdit("Type here...")
        text_entry.setFixedWidth(150)
        text_entry.setStyleSheet("background-color: white;")
        top_bar.addWidget(text_entry)

        color_btn = make_button("Text Color", bg="red", fg="white", bold=True, point_size=8)
        def choose_color():
            c = QColorDialog.getColor(QColor(state['color']), redact_win, "Choose Text Color")
            if c.isValid():
                state['color'] = c.name()
                color_btn.setStyleSheet(f"QPushButton {{ background-color: {c.name()}; color: white; padding: 4px 10px; border: 1px solid #9a9a9a; border-radius: 3px; }}")
        color_btn.clicked.connect(choose_color)
        top_bar.addWidget(color_btn)

        top_bar.addWidget(QLabel("|  Scroll wheel to zoom"))
        top_bar.addStretch()

        canvas = RedactionCanvas(original_img, state, text_entry.text)
        scroll = QScrollArea()
        scroll.setWidget(canvas)
        scroll.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        scroll.setStyleSheet("QScrollArea { background-color: #333; border: none; }")
        scroll.viewport().setStyleSheet("background-color: #333;")
        lay.addWidget(scroll, 1)

        def save_redactions():
            if not state['rectangles'] and not state['texts']:
                redact_win.close()
                return

            try:
                font = ImageFont.truetype("arial.ttf", 24)
            except IOError:
                font = ImageFont.load_default()

            for fp in filepaths:
                try:
                    img = Image.open(fp).convert('RGB')
                    draw = ImageDraw.Draw(img)

                    for box in state['rectangles']:
                        draw.rectangle(box, fill="black")

                    for txt in state['texts']:
                        x, y, text_str, color = txt
                        draw.text((x, y), text_str, fill=color, font=font)

                    img.save(fp)
                except Exception as e:
                    error(redact_win, "Save Error", f"Could not save changes to {os.path.basename(fp)}:\n{e}")

            update_callback()
            redact_win.close()
            info(parent_win, "Saved", f"Changes have been permanently saved to {len(filepaths)} image(s).")

        def undo_last():
            if state['history']:
                last_action = state['history'].pop()
                if last_action == 'box' and state['rectangles']:
                    state['rectangles'].pop()
                elif last_action == 'text' and state['texts']:
                    state['texts'].pop()
                canvas.update()

        top_bar.addWidget(make_button("↩️ Undo Last", undo_last))
        top_bar.addWidget(make_button(f"💾 Save to {len(filepaths)} Image(s)", save_redactions, bg="#4CAF50", fg="white", bold=True, point_size=10))

        redact_win.show()

    def show_toast(self, filename, session_index):
        # Frameless, always-on-top, and never takes focus (so auto modes keep sending keys to the slideshow)
        toast = QLabel(f"📸 [#{session_index}] Captured: {filename}")
        toast.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        toast.setAttribute(Qt.WA_ShowWithoutActivating)
        toast.setAttribute(Qt.WA_DeleteOnClose)
        toast.setStyleSheet("background-color: #2b2b2b; color: #4CAF50; padding: 10px 15px;")
        f = QFont("Arial", 11); f.setBold(True)
        toast.setFont(f)
        toast.setWordWrap(True)
        toast.setMaximumWidth(430)
        toast.adjustSize()

        geo = QGuiApplication.primaryScreen().availableGeometry()
        toast.move(geo.right() - toast.width() - 20, geo.bottom() - toast.height() - 20)
        toast.show()

        self._toasts.append(toast)
        def close_toast():
            toast.close()
            if toast in self._toasts: self._toasts.remove(toast)
        QTimer.singleShot(1500, close_toast)

    def clear_session(self):
        if not self.session_images:
            info(self, "Empty Session", "There are no images in the current session to clear.")
            return

        if ask_yes_no(self, "Confirm Clear", "Are you sure you want to permanently delete all screenshots in the current session?\n\nThis will remove the files from your computer."):
            for path in self.session_images:
                try:
                    os.remove(path)
                except OSError:
                    pass

            if self.current_session_folder and os.path.exists(self.current_session_folder):
                try:
                    if not os.listdir(self.current_session_folder):
                        os.rmdir(self.current_session_folder)
                except OSError:
                    pass

            self.session_images.clear()
            self.counter = 1
            self.current_session_folder = None
            self.session_label.setText("Images in current session: 0")
            info(self, "Session Cleared", "All images in the current session have been deleted.")

    def export_to_pdf(self):
        if not self.session_images:
            warn(self, "Empty Session", "You haven't taken any screenshots yet!")
            return

        base_name = self.name_combo.currentText()
        clean_name = base_name.strip("_") if base_name.endswith("_") else base_name
        if not clean_name: clean_name = "Compiled_Document"

        if self.current_session_folder:
            save_folder = self.current_session_folder
        else:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            save_folder = os.path.join(self.folder_entry.text(), f"{clean_name}_Export_{timestamp}")
            os.makedirs(save_folder, exist_ok=True)

        pdf_filename = f"{clean_name}.pdf"
        pdf_path = os.path.join(save_folder, pdf_filename)

        while os.path.exists(pdf_path):
            action = QMessageBox.question(
                self, "File Already Exists",
                f"The file '{pdf_filename}' already exists.\n\n"
                "• Click 'Yes' to Overwrite it.\n"
                "• Click 'No' to Rename this new export.\n"
                "• Click 'Cancel' to abort.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
            )
            if action == QMessageBox.Cancel:
                return
            elif action == QMessageBox.Yes:
                break
            else:
                new_name, ok = QInputDialog.getText(self, "Rename File", "Enter a new name for the PDF:", text=clean_name)
                if not ok or not new_name:
                    return
                clean_name = new_name
                pdf_filename = f"{clean_name}.pdf"
                pdf_path = os.path.join(save_folder, pdf_filename)

        try:
            first_image = Image.open(self.session_images[0]).convert('RGB')
            other_images = [Image.open(filepath).convert('RGB') for filepath in self.session_images[1:]]

            first_image.save(pdf_path, format="PDF", resolution=100.0, save_all=True, append_images=other_images)

            if self.auto_open_pdf:
                info(self, "Success", f"Saved {len(self.session_images)} images.\n\nFile Location:\n{pdf_path}\n\nOpening PDF now...")
            else:
                info(self, "Success", f"Saved {len(self.session_images)} images to:\n{pdf_path}")

            self.session_images.clear()
            self.counter = 1
            self.current_session_folder = None
            self.session_label.setText("Images in current session: 0")

            self.scan_for_pdfs()

            if self.auto_open_pdf:
                try:
                    os.startfile(pdf_path)
                except AttributeError:
                    import subprocess
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    subprocess.call([opener, pdf_path])

        except PermissionError:
            error(
                self, "File is Locked",
                f"Cannot save '{pdf_filename}'.\n\n"
                "Windows is blocking the save because the file is currently OPEN in another program.\n\n"
                "Please close the PDF and try exporting again!"
            )
        except Exception as e:
            error(self, "Error", f"Could not create PDF:\n{str(e)}\n\nTry ensuring all images in the session still exist in the folder.")

    def closeEvent(self, event):
        self.is_listening = False
        try: keyboard.unhook_all()
        except Exception: pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    apply_light_theme(app)
    window = ScreenCaptureApp()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
