import sys
import os
import json
import threading
import time
import random
import re # Added for the smart renamer context peek
import ctypes
import ctypes.wintypes as wt
import shutil
import uuid
import datetime
from collections import OrderedDict
import array
import math
import io
import struct
import zlib

import keyboard
from PIL import ImageGrab, Image, ImageChops, ImageStat, ImageDraw, ImageFont

from PySide6.QtCore import Qt, QObject, QTimer, Signal, QRect, QRectF, QPoint, QSize, QUrl, QByteArray, QRunnable, QThreadPool, QFileSystemWatcher
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPalette, QPen, QShortcut, QPixmap, QImage, QGuiApplication, QImageReader, QRegion, QAction
from PySide6.QtWidgets import (
    QApplication, QWidget, QDialog, QTabWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QCheckBox, QRadioButton, QButtonGroup, QLineEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QListWidget, QListWidgetItem, QAbstractItemView, QFrame,
    QPlainTextEdit, QScrollArea, QStackedWidget, QMessageBox, QInputDialog,
    QFileDialog, QColorDialog, QSlider, QSplitter, QStyle, QSizePolicy, QStyleOptionSlider, QLayout,
    QListView, QSystemTrayIcon, QMenu,
)

# Qt's FFmpeg engine opens MOV/MP4/MKV/WEBM/AVI alike and seeks accurately; the Windows Media
# Foundation engine can't open MKV and is unreliable with some files, so always prefer FFmpeg.
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

try:  # Video Capture tab needs the QtMultimedia add-on (FFmpeg backend, hardware decoding where available)
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput, QMediaMetaData, QAudioDecoder, QAudioFormat
    from PySide6.QtMultimediaWidgets import QVideoWidget
    VIDEO_AVAILABLE = True
except ImportError:
    VIDEO_AVAILABLE = False

APP_VERSION = "1.7.0"  # keep in sync with version.txt (build_exe.ps1 checks this)
APP_TITLE = f"Auto-Scapture v{APP_VERSION}"


def resource_path(rel):
    """Locate bundled files both from source and inside a PyInstaller one-file build."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


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


def button_qss(bg, fg="black"):
    return ("QPushButton { background-color: %s; color: %s; padding: 5px 12px; border: 1px solid rgba(0,0,0,0.16); border-radius: 6px; }"
            "QPushButton:hover { border: 1px solid rgba(0,0,0,0.45); }"
            "QPushButton:pressed { padding-top: 6px; padding-bottom: 4px; }"
            "QPushButton:disabled { background-color: #e5e7eb; color: #9ca3af; border: 1px solid #e5e7eb; }") % (bg, fg)


PRIMARY = "#1f6feb"
DANGER = "#d93025"


def make_button(text, slot=None, bg=None, fg=None, bold=False, point_size=None):
    btn = QPushButton(text.replace("&", "&&"))  # '&' would otherwise become a shortcut underline
    if slot: btn.clicked.connect(slot)
    if bg or fg:
        btn.setStyleSheet(button_qss(bg or "#ffffff", fg or "black"))
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


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp")
FLAG_RATIO = 0.01  # captures where under 1% of pixels changed are flagged as possible duplicates


def image_difference(img1, img2):
    """Average per-pixel difference (0-255) between two images at thumbnail scale."""
    i1 = img1.resize((100, 100)).convert("L")
    i2 = img2.resize((100, 100)).convert("L")
    return ImageStat.Stat(ImageChops.difference(i1, i2)).mean[0]


def is_identical(img1, img2):
    """Pixel-identical screenshots: the slide did not change at all (safe to skip)."""
    return img1.size == img2.size and ImageChops.difference(img1.convert("RGB"), img2.convert("RGB")).getbbox() is None


def change_ratio(img1, img2, width=480, threshold=16):
    """Fraction of pixels that visibly changed (any colour channel), at reduced resolution."""
    h = max(1, round(width * img1.height / max(1, img1.width)))
    a = img1.convert("RGB").resize((width, h))
    b = img2.convert("RGB").resize((width, h))
    r, g, bl = ImageChops.difference(a, b).split()
    hist = ImageChops.lighter(ImageChops.lighter(r, g), bl).histogram()
    return sum(hist[threshold + 1:]) / (width * h)


def looks_like_duplicate(img1, img2):
    """Identical or nearly identical, e.g. captured before the slide finished changing."""
    return is_identical(img1, img2) or change_ratio(img1, img2) < FLAG_RATIO
PAGE_SIZES = {"A4": (595.28, 841.89), "Letter": (612.0, 792.0)}  # portrait, in PDF points (1/72 inch)


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_pos_physical():
    """Real cursor position in physical virtual-desktop pixels (same space ImageGrab uses)."""
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def grab_region(region):
    """Screenshot a region given in physical pixels. Works on any monitor."""
    x1, y1, x2, y2 = region
    user32 = ctypes.windll.user32
    if x1 >= 0 and y1 >= 0 and x2 <= user32.GetSystemMetrics(0) and y2 <= user32.GetSystemMetrics(1):
        return ImageGrab.grab(bbox=tuple(region))  # fast path: primary monitor only
    return ImageGrab.grab(bbox=tuple(region), all_screens=True)


def physical_to_logical_rect(region):
    """Map a physical-pixel region back to Qt's (DPI-scaled) coordinates for drawing on screen."""
    x1, y1, x2, y2 = region
    for scr in QGuiApplication.screens():
        g, dpr = scr.geometry(), scr.devicePixelRatio()
        if g.x() <= x1 < g.x() + g.width() * dpr and g.y() <= y1 < g.y() + g.height() * dpr:
            lx = lambda v: int(g.x() + (v - g.x()) / dpr)
            ly = lambda v: int(g.y() + (v - g.y()) / dpr)
            return QRect(QPoint(lx(x1), ly(y1)), QPoint(lx(x2), ly(y2)))
    return QRect(x1, y1, x2 - x1, y2 - y1)


def exclude_from_capture(widget, enabled=True):
    """Hide a window from screenshots (Windows 10 2004+). Harmless no-op elsewhere."""
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(widget.winId()), 0x11 if enabled else 0)
    except Exception:
        pass


def png_rgb_stream(path):
    """Return (width, height, zlib data) of an image as PNG-filtered 8-bit RGB.

    PDFs can embed PNG image data losslessly (FlateDecode + PNG predictor). Screenshots are
    already RGB PNGs, so their compressed data is copied straight in without re-encoding;
    anything else (JPG, RGBA, palette...) is converted to RGB PNG in memory first."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height, depth, color, _comp, _filt, interlace = struct.unpack(">IIBBBBB", data[16:29])
        if depth == 8 and color == 2 and interlace == 0:
            chunks, pos = [], 8
            while pos < len(data):
                length = struct.unpack(">I", data[pos:pos + 4])[0]
                if data[pos + 4:pos + 8] == b"IDAT":
                    chunks.append(data[pos + 8:pos + 8 + length])
                pos += 12 + length
            return width, height, b"".join(chunks)
    img = Image.open(io.BytesIO(data)).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return png_rgb_stream_from_bytes(buf.getvalue())


def png_rgb_stream_from_bytes(data):
    width, height = struct.unpack(">II", data[16:24])
    chunks, pos = [], 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        if data[pos + 4:pos + 8] == b"IDAT":
            chunks.append(data[pos + 8:pos + 8 + length])
        pos += 12 + length
    return width, height, b"".join(chunks)


class LosslessPdfWriter:
    """Minimal streaming PDF writer: one full-resolution, lossless image per page.

    (Pillow's PDF export re-compresses every page as JPEG, which blurs text in screenshots.)"""

    def __init__(self, path):
        self.f = open(path, "wb")
        self.offsets = {}
        self.page_ids = []
        self.next_id = 4  # 1 = catalog, 2 = page tree, 3 = font
        self.f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        self._obj(3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    def _new_id(self):
        self.next_id += 1
        return self.next_id - 1

    def _obj(self, num, body, stream=None):
        self.offsets[num] = self.f.tell()
        self.f.write(b"%d 0 obj\n" % num + body)
        if stream is not None:
            self.f.write(b"\nstream\n" + stream + b"\nendstream")
        self.f.write(b"\nendobj\n")

    def add_image_page(self, path, page_size="Original", footer=None):
        w, h, idat = png_rgb_stream(path)
        if page_size in PAGE_SIZES:
            page_w, page_h = PAGE_SIZES[page_size]
            if w > h: page_w, page_h = page_h, page_w
            margin, bottom = 36.0, (54.0 if footer else 36.0)
            # Never upscale beyond 1 pixel per point; the image keeps its full resolution either way
            scale = min((page_w - 2 * margin) / w, (page_h - margin - bottom) / h, 1.0)
            draw_w, draw_h = w * scale, h * scale
            x = (page_w - draw_w) / 2
            y = bottom + (page_h - margin - bottom - draw_h) / 2
            text_y = 22.0
        else:
            scale = 72.0 / 96.0  # screen pixels at 96 DPI
            draw_w, draw_h = w * scale, h * scale
            strip = 28.0 if footer else 0.0
            page_w, page_h = draw_w, draw_h + strip
            x, y = 0.0, strip
            text_y = 9.0

        img_id, content_id, page_id = self._new_id(), self._new_id(), self._new_id()
        self._obj(img_id, (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                           b"/BitsPerComponent 8 /Filter /FlateDecode "
                           b"/DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 /Columns %d >> /Length %d >>")
                  % (w, h, w, len(idat)), idat)
        content = "q %.3f 0 0 %.3f %.3f %.3f cm /Im0 Do Q\n" % (draw_w, draw_h, x, y)
        if footer:
            text_w = len(footer) * 5.56  # Helvetica digits / slash / space at 10pt
            content += "BT /F1 10 Tf 0.35 g %.3f %.3f Td (%s) Tj ET\n" % ((page_w - text_w) / 2, text_y, footer)
        content = content.encode("ascii")
        self._obj(content_id, b"<< /Length %d >>" % len(content), content)
        self._obj(page_id, (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.3f %.3f] "
                            b"/Resources << /XObject << /Im0 %d 0 R >> /Font << /F1 3 0 R >> >> /Contents %d 0 R >>")
                  % (page_w, page_h, img_id, content_id))
        self.page_ids.append(page_id)

    def close(self):
        kids = b" ".join(b"%d 0 R" % i for i in self.page_ids)
        self._obj(2, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(self.page_ids)))
        self._obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        xref_pos = self.f.tell()
        self.f.write(b"xref\n0 %d\n0000000000 65535 f \n" % self.next_id)
        for num in range(1, self.next_id):
            self.f.write(b"%010d 00000 n \n" % self.offsets[num])
        self.f.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (self.next_id, xref_pos))
        self.f.close()

    def abort(self):
        self.f.close()


class ReorderList(QListWidget):
    """List that supports drag-and-drop reordering and tells us when the order changed."""
    orderChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)

    def dropEvent(self, event):
        super().dropEvent(event)
        self.orderChanged.emit()


class AreaFlash(QWidget):
    """Briefly outlines the capture area on screen. Click-through and never focused."""
    def __init__(self, rect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setGeometry(rect.adjusted(-4, -4, 4, 4))
        QTimer.singleShot(1600, self.close)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setPen(QPen(QColor("#ff3b30"), 4))
        p.setBrush(QColor(255, 59, 48, 35))
        p.drawRect(self.rect().adjusted(2, 2, -2, -2))


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
    app.setFont(QFont("Segoe UI", 9))
    pal = QPalette()
    for role, color in [(QPalette.Window, "#f4f6f9"), (QPalette.WindowText, "#1f2937"),
                        (QPalette.Base, "#ffffff"), (QPalette.AlternateBase, "#f7f7f7"),
                        (QPalette.Text, "#000000"), (QPalette.Button, "#e1e1e1"),
                        (QPalette.ButtonText, "#000000"), (QPalette.BrightText, "#ff0000"),
                        (QPalette.Highlight, "#1f6feb"), (QPalette.HighlightedText, "#ffffff"),
                        (QPalette.ToolTipBase, "#ffffdc"), (QPalette.ToolTipText, "#000000"),
                        (QPalette.PlaceholderText, "#808080"), (QPalette.Link, "#0000ff")]:
        pal.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#a0a0a0"))
    app.setPalette(pal)
    app.setStyleSheet(APP_QSS)


APP_QSS = """
QGroupBox { background: #ffffff; border: 1px solid #dde3ea; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 4px; color: #1b4a96; }
QPushButton { background: #ffffff; border: 1px solid #c9d1db; border-radius: 6px; padding: 5px 12px; }
QPushButton:hover { background: #f0f4fa; border-color: #8fa5c0; }
QPushButton:pressed { background: #e2e9f3; }
QPushButton:disabled { color: #a0a7b1; background: #f3f4f6; border-color: #e1e5ea; }
QPushButton:checked { background: #dbe8fd; border-color: #1f6feb; color: #0b2a5b; }
QFrame#miniPanel { background: #ffffff; }
QLineEdit { border: 1px solid #c9d1db; border-radius: 5px; padding: 4px 6px; background: #ffffff; }
QLineEdit:focus { border: 1px solid #1f6feb; }
QLineEdit:disabled { background: #f3f4f6; color: #9ca3af; }
QListWidget { border: 1px solid #d5dbe3; border-radius: 6px; background: #ffffff; }
QListWidget::item { padding: 3px; }
QListWidget::item:selected { background: #dbe8fd; color: #0b2a5b; }
QTabWidget::pane { border: none; border-top: 1px solid #dde3ea; }
QTabBar::tab { background: #e9edf2; color: #4b5563; padding: 7px 16px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; }
QTabBar::tab:selected { background: #f4f6f9; color: #1b4a96; }
QTabBar::tab:disabled { color: #b0b6bf; }
"""


DARK_PANEL_QSS = """
QFrame#darkPanel { background-color: #1e1e1e; }
QFrame#darkPanel QLabel { color: white; background: transparent; }
QFrame#darkPanel { border-radius: 8px; }
QFrame#darkPanel QGroupBox { color: white; background: transparent; border: 1px solid #555; margin-top: 10px; padding-top: 6px; }
QFrame#darkPanel QGroupBox::title { color: #cfd8e3; }
QFrame#darkPanel QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 3px; }
"""


# ==========================================
# --- SNIPPING OVERLAY ---
# ==========================================
class SnipOverlay(QWidget):
    """Translucent overlay for one monitor. The region itself is read from the real cursor
    position (physical pixels) so it lines up with ImageGrab on every monitor and DPI scale."""
    pressed = Signal()
    released = Signal()
    cancelled = Signal()

    def __init__(self, screen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowOpacity(0.3)
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(screen.geometry())
        self.start = None
        self.end = None

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("gray"))
        f = QFont("Segoe UI", 16); f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(self.rect().adjusted(0, 40, 0, 0), Qt.AlignHCenter | Qt.AlignTop, "Drag to select the capture area  •  Esc to cancel")
        if self.start and self.end:
            p.setPen(QPen(QColor("red"), 3))
            p.setBrush(QColor("black"))
            p.drawRect(QRect(self.start, self.end).normalized())

    def mousePressEvent(self, event):
        self.start = self.end = event.position().toPoint()
        self.update()
        self.pressed.emit()

    def mouseMoveEvent(self, event):
        if self.start:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.start:
            self.released.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()


class SnipController(QObject):
    """Shows an overlay on every monitor and emits the selected region in physical pixels."""
    regionSelected = Signal(tuple)

    def __init__(self):
        super().__init__()
        self.start_phys = None
        self.overlays = [SnipOverlay(scr) for scr in QGuiApplication.screens()]
        for o in self.overlays:
            o.pressed.connect(self._on_press)
            o.released.connect(self._on_release)
            o.cancelled.connect(self.close_all)

    def show(self):
        for o in self.overlays:
            o.show()
        under_cursor = next((o for o in self.overlays if o.geometry().contains(QCursor.pos())), self.overlays[0])
        under_cursor.activateWindow()
        under_cursor.setFocus()

    def _on_press(self):
        self.start_phys = cursor_pos_physical()

    def _on_release(self):
        if not self.start_phys: return
        (sx, sy), (ex, ey) = self.start_phys, cursor_pos_physical()
        self.close_all()
        x1, x2 = sorted((sx, ex))
        y1, y2 = sorted((sy, ey))
        if x2 - x1 >= 5 and y2 - y1 >= 5:  # ignore accidental clicks
            self.regionSelected.emit((x1, y1, x2, y2))

    def close_all(self):
        for o in self.overlays:
            o.close()
        self.overlays = []


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
# --- VIDEO CAPTURE TAB ---
# ==========================================
STANDARD_FPS = (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0, 90.0, 100.0, 119.88, 120.0, 144.0, 240.0)
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts")


def qimage_to_pil(qimg):
    qimg = qimg.convertToFormat(QImage.Format_RGB888)
    w, h, bpl = qimg.width(), qimg.height(), qimg.bytesPerLine()
    return Image.frombuffer("RGB", (w, h), bytes(qimg.constBits()), "raw", "RGB", bpl, 1).copy()


def format_clock(ms, with_ms=False, hours=False):
    total_s, rem_ms = divmod(max(0, int(ms)), 1000)
    h, rem = divmod(total_s, 3600)
    m, sec = divmod(rem, 60)
    text = f"{h}:{m:02d}:{sec:02d}" if (hours or h) else f"{m:02d}:{sec:02d}"
    return text + (f".{rem_ms:03d}" if with_ms else "")


class ClickSlider(QSlider):
    """Slider that jumps straight to wherever you click (and then lets you drag)."""

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.maximum() > self.minimum():
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
            handle = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            span = max(1, groove.width() - handle.width())
            x = int(event.position().x()) - groove.x() - handle.width() // 2
            self.setValue(QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), x, span))
        super().mousePressEvent(event)  # the handle is now under the cursor, so dragging continues


WAVE_BIN_MS = 50  # one loudness sample per 50 ms of audio


class WaveformWidget(QWidget):
    """Loudness chart of the video's audio. Click or drag on it to seek."""
    seekRequested = Signal(int)  # absolute player position (ms)

    def __init__(self):
        super().__init__()
        self.setFixedHeight(56)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Audio loudness - click to jump there")
        self.reset()

    def reset(self, message="Analyzing audio..."):
        self.bins = []          # peak level (0-32767) per WAVE_BIN_MS, on the file's clock
        self.start_ms = 0
        self.duration_ms = 0
        self.position_ms = 0
        self.message = message
        self._cache_key = None
        self._columns = []
        self.update()

    def set_timeline(self, start_ms, duration_ms):
        if (start_ms, duration_ms) != (self.start_ms, self.duration_ms):
            self.start_ms, self.duration_ms = start_ms, duration_ms
            self.update()

    def set_position(self, pos_ms):
        self.position_ms = pos_ms
        self.update()

    def add_samples(self, start_ms, samples, sample_rate):
        per_bin = max(1, int(sample_rate * WAVE_BIN_MS / 1000))
        for i in range(0, len(samples), per_bin):
            chunk = samples[i:i + per_bin]
            if not chunk:
                continue
            peak = max(max(chunk), -min(chunk))
            idx = int((start_ms + i * 1000.0 / sample_rate) / WAVE_BIN_MS)
            if idx >= len(self.bins):
                self.bins.extend([0] * (idx + 1 - len(self.bins)))
            if peak > self.bins[idx]:
                self.bins[idx] = peak
        self.message = ""

    def finish(self):
        if not any(self.bins):
            self.message = "No audio track"
        self._cache_key = None
        self.update()

    def _level(self, peak):
        # Show loudness on a 60 dB scale so quiet speech and loud parts are both visible
        if peak <= 0:
            return 0.0
        db = 20 * math.log10(peak / 32768.0)
        return min(1.0, max(0.0, (db + 60) / 60))

    def _column_levels(self, width):
        key = (width, len(self.bins), self.start_ms, self.duration_ms, sum(self.bins[-50:]))
        if key != self._cache_key:
            self._cache_key = key
            cols = []
            if self.duration_ms > 0 and self.bins:
                for x in range(width):
                    a = int((self.start_ms + self.duration_ms * x / width) / WAVE_BIN_MS)
                    b = max(a + 1, int((self.start_ms + self.duration_ms * (x + 1) / width) / WAVE_BIN_MS))
                    seg = self.bins[a:b]
                    cols.append(self._level(max(seg)) if seg else 0.0)
            self._columns = cols
        return self._columns

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#111827"))
        p.drawRoundedRect(r, 6, 6)
        cols = self._column_levels(r.width())
        mid = r.height() / 2
        play_x = int((self.position_ms - self.start_ms) / self.duration_ms * r.width()) if self.duration_ms else -1
        played, unplayed = QPen(QColor("#60a5fa")), QPen(QColor("#4b5563"))
        for x, level in enumerate(cols):
            h = max(1.0, level * (r.height() - 8) / 2)
            p.setPen(played if x <= play_x else unplayed)
            p.drawLine(x, int(mid - h), x, int(mid + h))
        if self.message:
            p.setPen(QColor("#9ca3af"))
            p.drawText(r, Qt.AlignCenter, self.message)
        if 0 <= play_x <= r.width():
            p.setPen(QPen(QColor("white"), 2))
            p.drawLine(play_x, 2, play_x, r.height() - 2)

    def _seek_from(self, event):
        if self.duration_ms > 0:
            frac = min(1.0, max(0.0, event.position().x() / max(1, self.width())))
            self.seekRequested.emit(int(self.start_ms + frac * self.duration_ms))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._seek_from(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton:
            self._seek_from(event)


class VideoCaptureTab(QWidget):
    """Media player for grabbing native-resolution frames and building YouTube chapter timestamps."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.video_path = None
        self.fps = 30.0
        self.last_frame = None
        self.chapters = []           # [[ms, title], ...]
        self._prime_state = "done"   # waiting -> playing -> settling -> verifying -> done
        self._start_ms = 0           # timeline position of the first frame (some files don't start at 0)
        self._muted_before = False
        self._was_playing = False

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.video)
        self.video.videoSink().videoFrameChanged.connect(self._on_frame)

        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.durationChanged.connect(self._on_duration)
        self.player.positionChanged.connect(self._on_position)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 10)

        # --- top bar ---
        top = QHBoxLayout()
        self.open_btn = make_button("Open Video...", lambda: self.open_video(), bg=PRIMARY, fg="white", bold=True)
        top.addWidget(self.open_btn)
        self.file_lbl = QLabel("No video loaded")
        f = self.file_lbl.font(); f.setBold(True); self.file_lbl.setFont(f)
        top.addWidget(self.file_lbl, 1)
        self.info_lbl = QLabel("")
        self.info_lbl.setStyleSheet("color: #6b7280;")
        top.addWidget(self.info_lbl)
        root.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # --- player column ---
        player_col = QWidget()
        pc = QVBoxLayout(player_col)
        pc.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        placeholder = QLabel("Open a video (or drop one here) to scrub through it\nand capture frames at full resolution.")
        placeholder.setAlignment(Qt.AlignCenter)
        placeholder.setStyleSheet("background: #1f2937; color: #9ca3af; border-radius: 8px; font-size: 11pt;")
        self.stack.addWidget(placeholder)
        self.stack.addWidget(self.video)
        self.stack.setMinimumSize(480, 270)
        pc.addWidget(self.stack, 1)

        self.slider = ClickSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(self._slider_pressed)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.slider.sliderReleased.connect(self._slider_released)
        pc.addWidget(self.slider)

        self.wave = WaveformWidget()
        self.wave.reset("Open a video to see its audio")
        self.wave.seekRequested.connect(self.seek_to)
        pc.addWidget(self.wave)
        self.decoder = None

        self.time_lbl = QLabel("00:00.000 / 00:00.000")
        self.time_lbl.setFont(QFont("Consolas", 10))
        pc.addWidget(self.time_lbl)

        ctl = QHBoxLayout()
        icon = self.style().standardIcon
        def tool(ic, tip, slot):
            b = QPushButton()
            b.setIcon(icon(ic))
            b.setToolTip(tip)
            b.clicked.connect(slot)
            b.setMinimumWidth(40)
            ctl.addWidget(b)
            return b
        self.prev_frame_btn = tool(QStyle.SP_MediaSkipBackward, "Previous frame  (Left)", lambda: self.step_frame(-1))
        self.back_btn = tool(QStyle.SP_MediaSeekBackward, "Back 5 seconds  (Shift+Left)", lambda: self.jump(-5000))
        self.play_btn = tool(QStyle.SP_MediaPlay, "Play / Pause  (Space)", self.toggle_play)
        self.fwd_btn = tool(QStyle.SP_MediaSeekForward, "Forward 5 seconds  (Shift+Right)", lambda: self.jump(5000))
        self.next_frame_btn = tool(QStyle.SP_MediaSkipForward, "Next frame  (Right)", lambda: self.step_frame(1))

        ctl.addSpacing(8)
        ctl.addWidget(QLabel("Speed:"))
        self.speed_combo = QComboBox()
        self.speed_combo.addItems(["0.25x", "0.5x", "1x", "1.5x", "2x"])
        self.speed_combo.setCurrentText("1x")
        self.speed_combo.currentTextChanged.connect(lambda t: self.player.setPlaybackRate(float(t[:-1])))
        ctl.addWidget(self.speed_combo)

        self.mute_btn = QPushButton()
        self.mute_btn.setIcon(icon(QStyle.SP_MediaVolume))
        self.mute_btn.setToolTip("Mute / unmute")
        self.mute_btn.clicked.connect(self.toggle_mute)
        ctl.addWidget(self.mute_btn)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(80)
        self.volume.setFixedWidth(90)
        self.volume.valueChanged.connect(lambda v: self.audio.setVolume(v / 100))
        self.audio.setVolume(0.8)
        ctl.addWidget(self.volume)
        ctl.addStretch()

        self.capture_btn = make_button("Capture Frame  (C)", self.capture_frame, bg=PRIMARY, fg="white", bold=True, point_size=10)
        self.capture_btn.setMinimumHeight(34)
        self.capture_btn.setToolTip("Save the current frame at the video's native resolution into the current session")
        ctl.addWidget(self.capture_btn)
        pc.addLayout(ctl)

        hint = QLabel("Frames are taken straight from the decoded video at native resolution, with no player controls in the shot. "
                      "They join the current session (Review, PDF and Markdown work as usual).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7280; font-size: 8pt;")
        pc.addWidget(hint)
        splitter.addWidget(player_col)

        # --- chapters panel ---
        chap_box = QGroupBox("YouTube Timestamps")
        cb = QVBoxLayout(chap_box)
        add_row = QHBoxLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Chapter title (e.g. Intro)")
        self.title_edit.returnPressed.connect(self.add_chapter)
        add_row.addWidget(self.title_edit, 1)
        add_btn = make_button("Add  (M)", self.add_chapter, bg="#d9ead3")
        add_btn.setToolTip("Add a timestamp at the current video position")
        add_row.addWidget(add_btn)
        cb.addLayout(add_row)

        self.chap_list = QListWidget()
        self.chap_list.setFont(QFont("Consolas", 10))
        self.chap_list.itemDoubleClicked.connect(lambda _i: self.goto_chapter())
        QShortcut(QKeySequence.Delete, self.chap_list, self.delete_chapter, context=Qt.WidgetShortcut)
        cb.addWidget(self.chap_list, 1)

        edit_grid = QGridLayout()
        edit_grid.addWidget(make_button("Go To", self.goto_chapter), 0, 0)
        edit_grid.addWidget(make_button("Rename", self.rename_chapter), 0, 1)
        move_btn = make_button("Move Here", self.move_chapter_here)
        move_btn.setToolTip("Move the selected timestamp to the current video position")
        edit_grid.addWidget(move_btn, 1, 0)
        edit_grid.addWidget(make_button("Delete", self.delete_chapter, fg="red"), 1, 1)
        cb.addLayout(edit_grid)

        exp_row = QHBoxLayout()
        exp_btn = make_button("Export YouTube .txt", self.export_chapters, bg="#d9ead3", bold=True)
        exp_row.addWidget(exp_btn, 1)
        exp_row.addWidget(make_button("Copy", self.copy_chapters))
        cb.addLayout(exp_row)

        rules = QLabel("YouTube chapters need the first timestamp at 0:00, at least 3 timestamps, and at least 10 seconds between them.")
        rules.setWordWrap(True)
        rules.setStyleSheet("color: #6b7280; font-size: 8pt;")
        cb.addWidget(rules)
        chap_box.setMinimumWidth(260)
        splitter.addWidget(chap_box)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([760, 300])

        # --- keyboard shortcuts (text fields keep their own typing keys) ---
        for keys, slot in [("Space", self.toggle_play), ("Right", lambda: self.step_frame(1)), ("Left", lambda: self.step_frame(-1)),
                           ("Shift+Right", lambda: self.jump(5000)), ("Shift+Left", lambda: self.jump(-5000)),
                           ("C", self.capture_frame), ("M", self.add_chapter)]:
            QShortcut(QKeySequence(keys), self, slot, context=Qt.WidgetWithChildrenShortcut)

        self._set_controls_enabled(False)

    # ---------------- loading ----------------
    def _set_controls_enabled(self, on):
        for w in (self.prev_frame_btn, self.back_btn, self.play_btn, self.fwd_btn, self.next_frame_btn,
                  self.capture_btn, self.slider, self.speed_combo):
            w.setEnabled(on)

    def open_video(self, path=None):
        if not path:
            start = os.path.dirname(self.video_path) if self.video_path else os.path.expanduser("~")
            path, _ = QFileDialog.getOpenFileName(self, "Open Video", start,
                                                  "Videos (" + " ".join("*" + e for e in VIDEO_EXTS) + ");;All Files (*.*)")
            if not path: return
        path = os.path.normpath(path)
        self.player.stop()
        self.video_path = path
        self.last_frame = None
        self.file_lbl.setText(os.path.basename(path))
        self.file_lbl.setToolTip(path)
        self.info_lbl.setText("Loading...")
        self.chapters = [list(c) for c in self.app.video_chapters.get(path, [])]
        self.refresh_chapters()
        self.stack.setCurrentWidget(self.video)
        self._start_ms = 0
        self._prime_times = []
        self._meta_fps = None
        self._prime_state = "waiting"
        self.player.setSource(QUrl.fromLocalFile(path))
        self._analyze_audio(path)

    def _on_status(self, status):
        if status == QMediaPlayer.LoadedMedia and self.video_path and self._prime_state == "waiting":
            md = self.player.metaData()
            fps = md.value(QMediaMetaData.VideoFrameRate)
            self._meta_fps = float(fps) if fps and float(fps) > 0 else None
            self.fps = self._meta_fps or 30.0
            res = md.value(QMediaMetaData.Resolution)
            self._res_txt = f"{res.width()} x {res.height()}" if res and res.width() > 0 else "unknown size"
            self._update_info()
            self._set_controls_enabled(True)
            # Qt only decodes frames once playback starts, so play muted until the first frame
            # arrives, then pause on it. This runs exactly once per opened video.
            self._prime_state = "playing"
            self._muted_before = self.audio.isMuted()
            self.audio.setMuted(True)
            self.player.play()
        elif status == QMediaPlayer.InvalidMedia:
            self._prime_state = "done"
            self.info_lbl.setText("Could not open this file")
            self._set_controls_enabled(False)

    def _on_error(self, _err, msg):
        # Some files have a track Qt can't decode (e.g. Opus audio in WEBM) but the video still plays.
        # Only interrupt the user if no picture ever shows up.
        self._last_error = msg or "Unknown error"
        def check():
            if self.last_frame is None:
                warn(self, "Video Error", f"This video couldn't be played:\n\n{self._last_error}")
            else:
                self._update_info()
                self.info_lbl.setText(self.info_lbl.text() + "  •  audio track not supported")
                self.info_lbl.setToolTip(self._last_error)
        QTimer.singleShot(2500, check)

    def _update_info(self):
        self.info_lbl.setText(f"{getattr(self, '_res_txt', '')}  •  {self.fps:g} fps  •  {format_clock(self.duration_ms())}")

    def _on_frame(self, frame):
        if not frame.isValid():
            return
        self.last_frame = frame
        if self._prime_state in ("playing", "settling"):
            self._prime_times.append(frame.startTime())
        if self._prime_state == "playing":
            self._prime_state = "settling"
            # The first decoded frame marks where the video really starts on the file's clock
            start = frame.startTime()
            self._start_ms = max(0, start // 1000) if 0 <= start < 60 * 60 * 1000 * 1000 else 0
            # Pausing inside the player's own callback (or too early) may not stick, so wait a moment
            QTimer.singleShot(200, self._finish_priming)

    def _finish_priming(self, attempt=0):
        if self._prime_state not in ("settling", "verifying"):
            return  # the user already took over (pressed play, seeked, stepped...)
        if attempt == 0:
            self._apply_measured_fps()
        self._prime_state = "verifying"
        self.player.pause()
        self.player.setPosition(self._start_ms)
        # A paused player must not move. Judge by movement, never by absolute position,
        # because some files (e.g. MOV/MKV with start offsets) don't begin at 0 ms.
        def first_read():
            if self._prime_state != "verifying": return
            p1 = self.player.position()
            def second_read():
                if self._prime_state != "verifying": return
                if self.player.position() - p1 > 40 and attempt < 3:
                    self.player.play()
                    QTimer.singleShot(150, lambda: self._finish_priming(attempt + 1))
                else:
                    self._end_priming()
            QTimer.singleShot(250, second_read)
        QTimer.singleShot(200, first_read)

    def _apply_measured_fps(self):
        """Many screen recordings (MKV, variable frame rate) carry no frame-rate metadata, so measure
        it from the frames decoded while loading and prefer it when metadata is missing or off."""
        times = sorted(set(t for t in self._prime_times if t >= 0))
        if len(times) < 4:
            return
        measured = 1e6 * (len(times) - 1) / (times[-1] - times[0])  # average over the whole sample
        if not 1 <= measured <= 240:
            return
        # Container timestamps are often rounded to 1 ms, so snap to the nearest standard rate
        standard = min(STANDARD_FPS, key=lambda r: abs(r - measured))
        if abs(standard - measured) / standard < 0.015:
            measured = standard
        if self._meta_fps is None or abs(measured - self._meta_fps) / self._meta_fps > 0.1:
            self.fps = round(measured, 3)
            self._update_info()

    def _end_priming(self):
        if self._prime_state != "done":
            self._prime_state = "done"
            self.audio.setMuted(self._muted_before)
            self._on_state(self.player.playbackState())

    def duration_ms(self):
        """Length of the video itself (excluding any start offset)."""
        return max(0, self.player.duration() - self._start_ms)

    def rel(self, pos):
        return max(0, pos - self._start_ms)

    def _on_duration(self, duration):
        self.slider.setRange(self._start_ms, max(self._start_ms, duration))
        self._update_time_label(self.player.position())

    def _analyze_audio(self, path):
        """Decode the audio track in the background into a loudness chart (low sample rate, mono)."""
        if self.decoder is not None:
            self.decoder.stop()
            self.decoder.deleteLater()
        self.wave.reset()
        self.decoder = QAudioDecoder(self)
        fmt = QAudioFormat()
        fmt.setSampleRate(4000)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.Int16)
        self.decoder.setAudioFormat(fmt)
        dec = self.decoder
        def on_buffer():
            if dec is not self.decoder: return
            buf = dec.read()
            if not buf.isValid(): return
            f = buf.format()
            if f.sampleFormat() != QAudioFormat.Int16 or f.channelCount() != 1:
                return
            self.wave.add_samples(buf.startTime() / 1000.0, array.array("h", bytes(buf.constData())), f.sampleRate())
        dec.bufferReady.connect(on_buffer)
        self._wave_timer = getattr(self, "_wave_timer", None) or QTimer(self)
        self._wave_timer.setInterval(300)
        try: self._wave_timer.timeout.disconnect()
        except (RuntimeError, TypeError): pass
        self._wave_timer.timeout.connect(self.wave.update)
        self._wave_timer.start()
        def done():
            if dec is self.decoder:
                self._wave_timer.stop()
                self.wave.finish()
        dec.finished.connect(done)
        dec.error.connect(lambda _e: dec is self.decoder and self.wave.reset("No audio track"))
        dec.setSource(QUrl.fromLocalFile(path))
        dec.start()

    def seek_to(self, pos_ms):
        """Jump to an absolute position (from the loudness chart)."""
        if not self.video_path: return
        self._end_priming()
        pos_ms = min(max(self._start_ms, pos_ms), self.player.duration())
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.setPosition(pos_ms)
        else:
            self.snap_to_frame(pos_ms)

    def _on_position(self, pos):
        self.wave.set_timeline(self._start_ms, self.duration_ms())
        self.wave.set_position(pos)
        if self.slider.minimum() != self._start_ms:
            self.slider.setRange(self._start_ms, max(self._start_ms, self.player.duration()))
            self._update_info()
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        self._update_time_label(pos)

    def _update_time_label(self, pos):
        frame_no = self.current_frame_index(pos) + 1
        self.time_lbl.setText(f"{format_clock(self.rel(pos), True)} / {format_clock(self.duration_ms(), True)}    Frame {frame_no}")

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlayingState and self._prime_state == "done"
        self.play_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaPause if playing else QStyle.SP_MediaPlay))

    # ---------------- transport ----------------
    def current_frame_index(self, pos=None):
        pos = self.player.position() if pos is None else pos
        # A position exactly on a frame boundary still shows the earlier frame, so lean backwards slightly
        return max(0, math.floor((self.rel(pos) - 0.5) * self.fps / 1000.0))

    def toggle_play(self):
        if not self.video_path: return
        self._end_priming()
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.snap_to_frame()
        else:
            if self.player.position() >= self.player.duration() - 50:
                self.player.setPosition(self._start_ms)
            self.player.play()

    def step_frame(self, direction):
        if not self.video_path: return
        self._end_priming()
        self.player.pause()
        last_index = max(0, math.ceil(self.duration_ms() * self.fps / 1000.0) - 1)
        idx = min(max(0, self.current_frame_index() + direction), last_index)
        self.player.setPosition(self.frame_center(idx))

    def frame_center(self, idx):
        # The middle of a frame is never ambiguous, unlike a position exactly on a frame boundary
        return self._start_ms + int(round((idx + 0.5) * 1000.0 / self.fps))

    def snap_to_frame(self, pos=None):
        """While paused, move to the centre of the frame at pos so the picture on screen and the
        frame counter always agree (frame stepping then moves exactly one frame)."""
        pos = self.player.position() if pos is None else pos
        self.player.setPosition(self.frame_center(self.current_frame_index(pos)))

    def jump(self, delta_ms):
        if not self.video_path: return
        self._end_priming()
        target = min(max(self._start_ms, self.player.position() + delta_ms), self.player.duration())
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.setPosition(target)
        else:
            self.snap_to_frame(target)

    def toggle_mute(self):
        self.audio.setMuted(not self.audio.isMuted())
        self.mute_btn.setIcon(self.style().standardIcon(QStyle.SP_MediaVolumeMuted if self.audio.isMuted() else QStyle.SP_MediaVolume))

    def _slider_pressed(self):
        self._end_priming()
        self._was_playing = self.player.playbackState() == QMediaPlayer.PlayingState
        self.player.pause()

    def _slider_released(self):
        if self._was_playing:
            self.player.setPosition(self.slider.value())
            # Resuming in the same instant as the seek can make the player drop the seek
            QTimer.singleShot(80, self.player.play)
        else:
            self.snap_to_frame(self.slider.value())

    # ---------------- capture ----------------
    def capture_frame(self):
        if not self.video_path or self.last_frame is None:
            warn(self, "No Frame", "Open a video first.")
            return
        img = self.last_frame.toImage()
        if img.isNull():
            warn(self, "No Frame", "Couldn't read the current frame. Try stepping one frame and capture again.")
            return
        self.app.save_raw_image(qimage_to_pil(img))

    # ---------------- chapters ----------------
    def _save_chapters(self):
        if not self.video_path: return
        if self.chapters:
            self.app.video_chapters[self.video_path] = self.chapters
        else:
            self.app.video_chapters.pop(self.video_path, None)
        self.app.save_settings()

    def refresh_chapters(self, select_ms=None):
        self.chapters.sort(key=lambda c: c[0])
        hours = self.duration_ms() >= 3600 * 1000 or any(c[0] >= 3600 * 1000 for c in self.chapters)
        self.chap_list.clear()
        for ms, title in self.chapters:
            item = QListWidgetItem(f"{format_clock(ms, hours=hours)}  {title}")
            item.setData(Qt.UserRole, ms)
            item.setToolTip(f"{format_clock(ms, with_ms=True, hours=hours)}  -  double-click to jump here")
            self.chap_list.addItem(item)
            if ms == select_ms:
                self.chap_list.setCurrentItem(item)

    def _selected_chapter(self):
        row = self.chap_list.currentRow()
        return row if 0 <= row < len(self.chapters) else None

    def add_chapter(self):
        if not self.video_path:
            warn(self, "No Video", "Open a video first.")
            return
        ms = self.rel(self.player.position())
        title = self.title_edit.text().strip() or f"Chapter {len(self.chapters) + 1}"
        self.chapters.append([ms, title])
        self.title_edit.clear()
        self.refresh_chapters(select_ms=ms)
        self._save_chapters()

    def goto_chapter(self):
        row = self._selected_chapter()
        if row is not None:
            self._end_priming()
            self.player.pause()
            self.snap_to_frame(self._start_ms + self.chapters[row][0])

    def rename_chapter(self):
        row = self._selected_chapter()
        if row is None: return
        text, ok = QInputDialog.getText(self, "Rename Timestamp", "Title:", text=self.chapters[row][1])
        if ok and text.strip():
            self.chapters[row][1] = text.strip()
            self.refresh_chapters(select_ms=self.chapters[row][0])
            self._save_chapters()

    def move_chapter_here(self):
        row = self._selected_chapter()
        if row is None: return
        self.chapters[row][0] = self.rel(self.player.position())
        self.refresh_chapters(select_ms=self.chapters[row][0])
        self._save_chapters()

    def delete_chapter(self):
        row = self._selected_chapter()
        if row is None: return
        del self.chapters[row]
        self.refresh_chapters()
        self._save_chapters()

    def youtube_lines(self):
        hours = self.duration_ms() >= 3600 * 1000 or any(c[0] >= 3600 * 1000 for c in self.chapters)
        return [f"{format_clock(ms, hours=hours)} {title}" for ms, title in sorted(self.chapters)]

    def _check_youtube_rules(self):
        """Offer to fix / confirm YouTube's chapter rules. Returns False if the user cancelled."""
        if not self.chapters:
            info(self, "No Timestamps", "Add some timestamps first.")
            return False
        self.chapters.sort(key=lambda c: c[0])
        if self.chapters[0][0] >= 1000:
            if ask_yes_no(self, "First Timestamp", "YouTube requires the first timestamp to be 0:00.\n\nAdd an 'Intro' timestamp at 0:00?"):
                self.chapters.insert(0, [0, "Intro"])
                self.refresh_chapters()
                self._save_chapters()
        else:
            self.chapters[0][0] = 0  # anything under one second counts as the start
        problems = []
        if self.chapters[0][0] != 0:
            problems.append("The first timestamp isn't 0:00.")
        if len(self.chapters) < 3:
            problems.append(f"Only {len(self.chapters)} timestamp(s); YouTube needs at least 3.")
        for (a_ms, a_t), (b_ms, b_t) in zip(self.chapters, self.chapters[1:]):
            if b_ms - a_ms < 10000:
                problems.append(f"'{a_t}' is shorter than 10 seconds.")
        if problems:
            return ask_yes_no(self, "YouTube Chapter Rules",
                              "YouTube may not show these as chapters:\n\n- " + "\n- ".join(problems[:8]) + "\n\nContinue anyway?")
        return True

    def export_chapters(self):
        if not self._check_youtube_rules(): return
        base = os.path.splitext(os.path.basename(self.video_path))[0] if self.video_path else "Video"
        start = os.path.join(os.path.dirname(self.video_path) if self.video_path else os.path.expanduser("~"),
                             f"{base} - YouTube Timestamps.txt")
        path, _ = QFileDialog.getSaveFileName(self, "Save YouTube Timestamps", start, "Text File (*.txt)")
        if not path: return
        text = "\n".join(self.youtube_lines()) + "\n"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            error(self, "Error", f"Could not save the file:\n{e}")
            return
        info(self, "Timestamps Exported", f"Saved to:\n{path}\n\nPaste this into your YouTube description:\n\n{text}")

    def copy_chapters(self):
        if not self._check_youtube_rules(): return
        QApplication.clipboard().setText("\n".join(self.youtube_lines()))
        info(self, "Copied", "Timestamps copied - paste them into your YouTube description.")

    def shutdown(self):
        self.player.stop()
        if self.decoder is not None:
            self.decoder.stop()


# ==========================================
# --- SCREENSHOTS (replacement for Win+Shift+S / Snipping Tool) ---
# ==========================================
GALLERY_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")


def windows_screenshots_folder():
    """The folder Windows' own screenshot tools save to (follows OneDrive / moved folders)."""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]
        u = uuid.UUID("b7bede81-df94-4682-a7d8-57a52620b86f")  # FOLDERID_Screenshots
        guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) == 0 and path.value:
            result = path.value
            ctypes.windll.ole32.CoTaskMemFree(path)
            return result
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitor_rect_at(point):
    """Physical-pixel rectangle of the monitor containing point."""
    user32 = ctypes.windll.user32
    handle = user32.MonitorFromPoint(wt.POINT(*point), 2)  # MONITOR_DEFAULTTONEAREST
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    user32.GetMonitorInfoW(handle, ctypes.byref(info))
    r = info.rcMonitor
    return (r.left, r.top, r.right, r.bottom)


def virtual_screen_rect():
    m = ctypes.windll.user32.GetSystemMetrics
    x, y = m(76), m(77)
    return (x, y, x + m(78), y + m(79))


def list_windows_physical():
    """Visible, titled top-level windows (top of the z-order first) as physical-pixel rectangles."""
    user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    own_pid = os.getpid()
    rects = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == own_pid:
            return True
        cloaked = ctypes.c_int(0)
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED (hidden UWP windows)
        if cloaked.value:
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW"):
            return True
        r = wt.RECT()
        # the visible frame, without the invisible resize borders
        if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left > 20 and r.bottom - r.top > 20:
            rects.append((r.left, r.top, r.right, r.bottom))
        return True

    user32.EnumWindows(callback, 0)
    return rects


def send_to_recycle_bin(paths):
    """Delete files to the Recycle Bin (undoable). Windows warns if a file can't be recycled."""
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wt.HWND), ("wFunc", wt.UINT), ("pFrom", ctypes.c_void_p), ("pTo", ctypes.c_void_p),
                    ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wt.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wt.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_WANTNUKEWARNING = 3, 0x4, 0x10, 0x40, 0x4000
    names = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    buf = ctypes.create_unicode_buffer(names, len(names) + 1)
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p)
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_WANTNUKEWARNING
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return result == 0 and not op.fAnyOperationsAborted


def unique_path(folder, filename):
    base, ext = os.path.splitext(filename)
    candidate, n = os.path.join(folder, filename), 2
    while os.path.exists(candidate):
        candidate = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return candidate


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "Auto-Scapture")
            return True
    except OSError:
        return False


def set_autostart(enabled):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "Auto-Scapture", 0, winreg.REG_SZ, f'"{sys.executable}" --tray')
        else:
            try:
                winreg.DeleteValue(key, "Auto-Scapture")
            except FileNotFoundError:
                pass


class ScreenshotSession(QObject):
    """Snipping-Tool style capture: freezes the screen, then drag for an area or click a window."""
    captured = Signal(object)  # PIL image
    cancelled = Signal()

    def __init__(self):
        super().__init__()
        self.overlays = []
        self.start_phys = None
        self.cur_phys = None
        self.hover = None
        self._esc_hotkey = None

    def start(self):
        vx, vy, vx2, vy2 = virtual_screen_rect()
        self.origin = (vx, vy)
        self.frozen = ImageGrab.grab(all_screens=True)
        self.windows = list_windows_physical()
        self.overlays = [ScreenshotOverlay(self, scr) for scr in QGuiApplication.screens()]
        for o in self.overlays:
            o.show()
        self.update_hover()
        under = next((o for o in self.overlays if o.geometry().contains(QCursor.pos())), self.overlays[0])
        under.activateWindow()
        under.setFocus()
        try:  # Esc works even if Windows didn't give the overlay keyboard focus
            self._esc_hotkey = keyboard.add_hotkey("esc", lambda: QTimer.singleShot(0, self.cancel))
        except Exception:
            self._esc_hotkey = None

    # --- state used by the overlays ---
    def selection(self):
        if self.start_phys and self.cur_phys:
            (sx, sy), (cx, cy) = self.start_phys, self.cur_phys
            return (min(sx, cx), min(sy, cy), max(sx, cx), max(sy, cy))
        return None

    def update_hover(self):
        pt = cursor_pos_physical()
        self.hover = next((r for r in self.windows if r[0] <= pt[0] < r[2] and r[1] <= pt[1] < r[3]), None) or monitor_rect_at(pt)
        self.repaint_all()

    def repaint_all(self):
        for o in self.overlays:
            o.update()

    # --- input ---
    def press(self):
        self.start_phys = self.cur_phys = cursor_pos_physical()
        self.repaint_all()

    def move(self):
        if self.start_phys:
            self.cur_phys = cursor_pos_physical()
            self.repaint_all()
        else:
            self.update_hover()

    def release(self):
        sel = self.selection()
        if sel and sel[2] - sel[0] >= 5 and sel[3] - sel[1] >= 5:
            self.finish(sel)          # dragged an area
        else:
            self.finish(self.hover)   # simple click: the window (or screen) under the cursor

    def capture_screen(self):
        self.finish(monitor_rect_at(cursor_pos_physical()))

    def capture_all(self):
        self.finish((self.origin[0], self.origin[1], self.origin[0] + self.frozen.width, self.origin[1] + self.frozen.height))

    def _close(self):
        if self._esc_hotkey is not None:
            try: keyboard.remove_hotkey(self._esc_hotkey)
            except (KeyError, ValueError): pass
            self._esc_hotkey = None
        for o in self.overlays:
            o.close()
        self.overlays = []

    def cancel(self):
        if not self.overlays: return
        self._close()
        self.cancelled.emit()

    def finish(self, rect):
        if not self.overlays: return
        self._close()
        ox, oy = self.origin
        x1, y1 = max(0, rect[0] - ox), max(0, rect[1] - oy)
        x2, y2 = min(self.frozen.width, rect[2] - ox), min(self.frozen.height, rect[3] - oy)
        if x2 - x1 < 2 or y2 - y1 < 2:
            self.cancelled.emit()
            return
        self.captured.emit(self.frozen.crop((x1, y1, x2, y2)))


class ScreenshotOverlay(QWidget):
    """One frozen, dimmed monitor. Undimmed: the dragged area or the window under the cursor."""

    def __init__(self, session, screen):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.session = session
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.g = screen.geometry()
        self.dpr = screen.devicePixelRatio()
        self.setGeometry(self.g)
        ox, oy = session.origin
        px1, py1 = self.g.x() - ox, self.g.y() - oy
        crop = session.frozen.crop((px1, py1, px1 + round(self.g.width() * self.dpr), py1 + round(self.g.height() * self.dpr)))
        self.background = pil_to_pixmap(crop)

    def to_local(self, rect):
        x1, y1, x2, y2 = rect
        return QRectF((x1 - self.g.x()) / self.dpr, (y1 - self.g.y()) / self.dpr, (x2 - x1) / self.dpr, (y2 - y1) / self.dpr)

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.background)
        sel = self.session.selection()
        target = sel if sel else self.session.hover
        dim = QColor(0, 0, 0, 115)
        if target:
            local = self.to_local(target)
            p.setClipRegion(QRegion(self.rect()).subtracted(QRegion(local.toAlignedRect())))
            p.fillRect(self.rect(), dim)
            p.setClipping(False)
            pen = QPen(QColor("#1f6feb"), 2)
            if not sel:
                pen.setStyle(Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(local.adjusted(1, 1, -1, -1))
            label = f"{target[2] - target[0]} × {target[3] - target[1]}"
            f = QFont("Segoe UI", 9); f.setBold(True)
            p.setFont(f)
            tw = QFontMetrics(f).horizontalAdvance(label) + 14
            box = QRectF(local.left(), max(0, local.top() - 24), tw, 20)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(17, 24, 39, 220))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor("white"))
            p.drawText(box, Qt.AlignCenter, label)
        else:
            p.fillRect(self.rect(), dim)
        # hint
        hint = "Drag to snip an area   •   Click a window to capture it   •   F: this screen   •   A: all screens   •   Esc: cancel"
        f = QFont("Segoe UI", 10)
        p.setFont(f)
        tw = QFontMetrics(f).horizontalAdvance(hint) + 28
        box = QRectF((self.width() - tw) / 2, 16, tw, 30)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(17, 24, 39, 230))
        p.drawRoundedRect(box, 15, 15)
        p.setPen(QColor("#e5e7eb"))
        p.drawText(box, Qt.AlignCenter, hint)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.session.press()
        elif event.button() == Qt.RightButton:
            self.session.cancel()

    def mouseMoveEvent(self, event):
        self.session.move()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.session.release()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Escape:
            self.session.cancel()
        elif key == Qt.Key_F:
            self.session.capture_screen()
        elif key == Qt.Key_A:
            self.session.capture_all()


class _ThumbSignals(QObject):
    done = Signal(int, str, QImage)


class _ThumbJob(QRunnable):
    def __init__(self, generation, path, size, signals):
        super().__init__()
        self.generation, self.path, self.size, self.signals = generation, path, size, signals

    def run(self):
        reader = QImageReader(self.path)
        reader.setAutoTransform(True)
        full = reader.size()
        if full.isValid():
            reader.setScaledSize(full.scaled(self.size, Qt.KeepAspectRatio))
        image = reader.read()
        self.signals.done.emit(self.generation, self.path, image)


class ScreenshotsTab(QWidget):
    """Take screenshots (Snipping-Tool replacement) and manage the Screenshots folder."""
    THUMB = QSize(176, 104)
    CACHE_LIMIT = 700

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.prefs = app.shot_prefs
        self.folder = self.prefs.get("folder") or windows_screenshots_folder()
        self.entries = []        # [(path, mtime, size)]
        self.items = {}          # path -> QListWidgetItem
        self.thumbs = OrderedDict()
        self.pending = set()
        self.generation = 0
        self.session = None
        self._busy = False
        self._hotkey_handle = None
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(3)
        self.thumb_signals = _ThumbSignals()
        self.thumb_signals.done.connect(self._on_thumb)
        self.placeholder = QPixmap(self.THUMB)
        self.placeholder.fill(QColor("#e5e7eb"))

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 10)

        # --- take a screenshot ---
        take_box = QGroupBox("Take a Screenshot")
        tb = QVBoxLayout(take_box)
        row = QHBoxLayout()
        self.snip_btn = make_button("Snip  (area or window)", lambda: self.take("snip"), bg=PRIMARY, fg="white", bold=True, point_size=10)
        self.snip_btn.setMinimumHeight(34)
        self.snip_btn.setToolTip("Freeze the screen, then drag an area or click a window")
        row.addWidget(self.snip_btn)
        self.screen_btn = make_button("Full Screen", lambda: self.take("screen"))
        self.screen_btn.setToolTip("The monitor your mouse is on")
        row.addWidget(self.screen_btn)
        self.all_btn = make_button("All Monitors", lambda: self.take("all"))
        row.addWidget(self.all_btn)
        row.addSpacing(10)
        row.addWidget(QLabel("Delay:"))
        self.delay_combo = QComboBox()
        self.delay_combo.addItems(["None", "3 seconds", "5 seconds", "10 seconds"])
        self.delay_combo.setCurrentText(self.prefs.get("delay", "None"))
        self.delay_combo.currentTextChanged.connect(lambda t: self._set_pref("delay", t))
        row.addWidget(self.delay_combo)
        self.clip_cb = QCheckBox("Copy to clipboard")
        self.clip_cb.setChecked(self.prefs.get("copy_to_clipboard", True))
        self.clip_cb.toggled.connect(lambda v: self._set_pref("copy_to_clipboard", v))
        row.addWidget(self.clip_cb)
        row.addStretch()
        tb.addLayout(row)

        row2 = QHBoxLayout()
        self.hotkey_cb = QCheckBox("Screenshot key:")
        self.hotkey_cb.setToolTip("Press this key anywhere to snip (while Auto-Scapture is running).\n"
                                  "It replaces what Windows would normally do with that key.")
        self.hotkey_combo = QComboBox()
        self.hotkey_combo.setEditable(True)
        self.hotkey_combo.addItems(["print screen", "ctrl+print screen", "ctrl+shift+x", "ctrl+alt+s", "f8"])
        self.hotkey_combo.setEditText(self.prefs.get("hotkey", "print screen"))
        self.hotkey_combo.setMinimumWidth(140)
        self.hotkey_combo.lineEdit().editingFinished.connect(self._hotkey_changed)
        self.hotkey_combo.textActivated.connect(lambda _t: self._hotkey_changed())
        self.hotkey_cb.setChecked(self.prefs.get("hotkey_enabled", False))
        self.hotkey_cb.toggled.connect(self._hotkey_toggled)
        row2.addWidget(self.hotkey_cb)
        row2.addWidget(self.hotkey_combo)
        row2.addSpacing(14)
        self.tray_cb = QCheckBox("Keep running in the tray when closed")
        self.tray_cb.setToolTip("Closing the window leaves Auto-Scapture in the system tray so the screenshot key keeps working.\n"
                                "Right-click the tray icon to quit.")
        self.tray_cb.setChecked(self.prefs.get("run_in_background", False))
        self.tray_cb.toggled.connect(self._tray_toggled)
        row2.addWidget(self.tray_cb)
        self.autostart_cb = QCheckBox("Start with Windows")
        frozen = getattr(sys, "frozen", False)
        self.autostart_cb.setEnabled(frozen)
        self.autostart_cb.setToolTip("Start Auto-Scapture in the tray when you sign in to Windows" if frozen
                                     else "Available in the .exe version")
        self.autostart_cb.setChecked(frozen and autostart_enabled())
        self.autostart_cb.toggled.connect(self._autostart_toggled)
        row2.addWidget(self.autostart_cb)
        row2.addStretch()
        tb.addLayout(row2)
        root.addWidget(take_box)

        # --- folder bar ---
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Folder:"))
        self.folder_edit = QLineEdit(self.folder)
        self.folder_edit.setReadOnly(True)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(make_button("Change...", self.choose_folder))
        default_btn = make_button("Windows Default", self.use_default_folder)
        default_btn.setToolTip("Use the folder Windows' own screenshot tools save to")
        folder_row.addWidget(default_btn)
        folder_row.addWidget(make_button("Open in Explorer", lambda: os.path.isdir(self.folder) and os.startfile(self.folder)))
        self.subfolders_cb = QCheckBox("Include subfolders")
        self.subfolders_cb.setChecked(self.prefs.get("include_subfolders", False))
        self.subfolders_cb.toggled.connect(lambda v: (self._set_pref("include_subfolders", v), self.refresh()))
        folder_row.addWidget(self.subfolders_cb)
        organize_btn = make_button("Organize by Month...", self.organize_by_month, bg="#ece3f7")
        organize_btn.setToolTip("Move loose screenshots into YYYY-MM folders")
        folder_row.addWidget(organize_btn)
        root.addLayout(folder_row)

        # --- filter bar ---
        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda _t: self._filter_timer.start())
        filter_row.addWidget(self.search_edit, 1)
        self.date_combo = QComboBox()
        self.date_combo.addItems(["All dates", "Today", "Last 7 days", "Last 30 days", "Older than 30 days"])
        self.date_combo.setCurrentText(self.prefs.get("date_filter", "All dates"))
        self.date_combo.currentTextChanged.connect(lambda t: (self._set_pref("date_filter", t), self.apply_filter()))
        filter_row.addWidget(self.date_combo)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Newest first", "Oldest first", "Largest first", "Name"])
        self.sort_combo.setCurrentText(self.prefs.get("sort", "Newest first"))
        self.sort_combo.currentTextChanged.connect(lambda t: (self._set_pref("sort", t), self.apply_filter()))
        filter_row.addWidget(self.sort_combo)
        filter_row.addWidget(make_button("Refresh", self.refresh))
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color: #6b7280;")
        filter_row.addWidget(self.count_lbl)
        root.addLayout(filter_row)

        # --- gallery + details ---
        split = QSplitter(Qt.Horizontal)
        self.gallery = QListWidget()
        self.gallery.setViewMode(QListView.IconMode)
        self.gallery.setResizeMode(QListView.Adjust)
        self.gallery.setMovement(QListView.Static)
        self.gallery.setIconSize(self.THUMB)
        self.gallery.setGridSize(QSize(self.THUMB.width() + 20, self.THUMB.height() + 40))
        self.gallery.setUniformItemSizes(True)
        self.gallery.setWordWrap(False)
        self.gallery.setTextElideMode(Qt.ElideMiddle)
        self.gallery.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.gallery.setSpacing(4)
        self.gallery.itemDoubleClicked.connect(lambda _i: self.open_selected())
        self.gallery.currentItemChanged.connect(lambda *_: self.update_preview())
        self.gallery.itemSelectionChanged.connect(self._update_action_states)
        self.gallery.verticalScrollBar().valueChanged.connect(lambda _v: self._visible_timer.start())
        QShortcut(QKeySequence.Delete, self.gallery, self.delete_selected, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence.Copy, self.gallery, self.copy_selected, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence("F2"), self.gallery, self.rename_selected, context=Qt.WidgetShortcut)
        split.addWidget(self.gallery)

        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(8, 0, 0, 0)
        self.preview = QLabel("Select a screenshot")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(260, 170)
        self.preview.setStyleSheet("background: #1f2937; color: #9ca3af; border-radius: 8px;")
        sl.addWidget(self.preview, 1)
        self.info_lbl = QLabel("")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self.info_lbl)
        actions = QGridLayout()
        self.act_buttons = {}
        for i, (name, slot, bg, fg) in enumerate([
                ("Open", self.open_selected, None, None), ("Copy", self.copy_selected, None, None),
                ("Show in Folder", self.show_in_folder, None, None), ("Rename", self.rename_selected, None, None),
                ("Edit / Redact", self.edit_selected, "#ffd9b3", None), ("Add to Session", self.add_to_session, "#cfe2f3", None),
                ("Move To...", self.move_selected, None, None), ("Delete", self.delete_selected, None, "red")]):
            b = make_button(name, slot, bg=bg, fg=fg)
            self.act_buttons[name] = b
            actions.addWidget(b, i // 2, i % 2)
        self.act_buttons["Add to Session"].setToolTip("Add to the Auto-Scapture session (for Review, PDF and Markdown)")
        self.act_buttons["Delete"].setToolTip("Move to the Recycle Bin  (Delete key)")
        sl.addLayout(actions)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([800, 300])
        root.addWidget(split, 1)

        # timers / watchers
        self._filter_timer = QTimer(self, singleShot=True, interval=200, timeout=self.apply_filter)
        self._visible_timer = QTimer(self, singleShot=True, interval=60, timeout=self._request_visible_thumbs)
        self._refresh_timer = QTimer(self, singleShot=True, interval=600, timeout=self.refresh)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.directoryChanged.connect(lambda _p: self._refresh_timer.start())

        self._update_action_states()
        self._loaded = False
        if self.hotkey_cb.isChecked():
            self._register_hotkey()

    # ---------------- prefs ----------------
    def _set_pref(self, key, value):
        self.prefs[key] = value
        self.app.save_settings()

    # ---------------- folder listing ----------------
    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            QTimer.singleShot(0, self.refresh)
        else:
            self._visible_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._visible_timer.start()

    def set_folder(self, folder):
        self.folder = os.path.normpath(folder)
        self.folder_edit.setText(self.folder)
        self._set_pref("folder", self.folder)
        self.refresh()

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Screenshots Folder", self.folder if os.path.isdir(self.folder) else "")
        if folder:
            self.set_folder(folder)

    def use_default_folder(self):
        self.set_folder(windows_screenshots_folder())

    def refresh(self):
        entries = []
        if os.path.isdir(self.folder):
            stack = [self.folder]
            while stack:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                if self.subfolders_cb.isChecked():
                                    stack.append(e.path)
                            elif e.name.lower().endswith(GALLERY_EXTS):
                                st = e.stat()
                                entries.append((os.path.normpath(e.path), st.st_mtime, st.st_size))
                except OSError:
                    pass
            if self.folder not in self.watcher.directories():
                if self.watcher.directories():
                    self.watcher.removePaths(self.watcher.directories())
                self.watcher.addPath(self.folder)
        self.entries = entries
        self.apply_filter()

    def _filtered(self):
        now = time.time()
        day = 86400
        start_of_today = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
        date_filter = self.date_combo.currentText()
        needle = self.search_edit.text().strip().lower()
        out = []
        for path, mtime, size in self.entries:
            if date_filter == "Today" and mtime < start_of_today: continue
            if date_filter == "Last 7 days" and mtime < now - 7 * day: continue
            if date_filter == "Last 30 days" and mtime < now - 30 * day: continue
            if date_filter == "Older than 30 days" and mtime >= now - 30 * day: continue
            if needle and needle not in os.path.basename(path).lower(): continue
            out.append((path, mtime, size))
        sort = self.sort_combo.currentText()
        if sort == "Newest first": out.sort(key=lambda e: e[1], reverse=True)
        elif sort == "Oldest first": out.sort(key=lambda e: e[1])
        elif sort == "Largest first": out.sort(key=lambda e: e[2], reverse=True)
        else: out.sort(key=lambda e: os.path.basename(e[0]).lower())
        return out

    def apply_filter(self, select_path=None):
        selected = select_path or (self.gallery.currentItem().data(Qt.UserRole) if self.gallery.currentItem() else None)
        shown = self._filtered()
        self.generation += 1
        self.pending.clear()
        self.gallery.setUpdatesEnabled(False)
        self.gallery.clear()
        self.items = {}
        placeholder_icon = QIcon(self.placeholder)
        for path, mtime, size in shown:
            item = QListWidgetItem(self.thumbs.get(path) and QIcon(self.thumbs[path]) or placeholder_icon, os.path.basename(path))
            item.setData(Qt.UserRole, path)
            item.setToolTip(f"{os.path.basename(path)}\n{datetime.datetime.fromtimestamp(mtime):%Y-%m-%d %H:%M}  •  {size / 1e6:.1f} MB")
            self.gallery.addItem(item)
            self.items[path] = item
        self.gallery.setUpdatesEnabled(True)
        total_mb = sum(e[2] for e in self.entries) / 1e6
        if not os.path.isdir(self.folder):
            self.count_lbl.setText("Folder not found - take a screenshot to create it")
        elif len(shown) == len(self.entries):
            self.count_lbl.setText(f"{len(shown):,} screenshots  •  {total_mb:,.1f} MB")
        else:
            self.count_lbl.setText(f"{len(shown):,} of {len(self.entries):,} screenshots  •  {total_mb:,.1f} MB total")
        if selected in self.items:
            self.gallery.setCurrentItem(self.items[selected])
            self.gallery.scrollToItem(self.items[selected])
        self.update_preview()
        self._update_action_states()
        self._visible_timer.start()

    # ---------------- thumbnails (only for what's on screen) ----------------
    def _request_visible_thumbs(self):
        if not self.isVisible() or not self.items:
            return
        view = self.gallery.viewport().rect().adjusted(0, -self.THUMB.height() * 2, 0, self.THUMB.height() * 2)
        for path, item in self.items.items():
            if path in self.thumbs or path in self.pending:
                continue
            if self.gallery.visualItemRect(item).intersects(view):
                self.pending.add(path)
                self.pool.start(_ThumbJob(self.generation, path, self.THUMB, self.thumb_signals))

    def _on_thumb(self, generation, path, image):
        self.pending.discard(path)
        if image.isNull():
            return
        pix = QPixmap.fromImage(image)
        self.thumbs[path] = pix
        self.thumbs.move_to_end(path)
        while len(self.thumbs) > self.CACHE_LIMIT:
            old_path, _ = self.thumbs.popitem(last=False)
            if old_path in self.items:
                self.items[old_path].setIcon(QIcon(self.placeholder))
        if path in self.items:
            self.items[path].setIcon(QIcon(pix))

    def _forget_thumbs(self, paths):
        for pth in paths:
            self.thumbs.pop(pth, None)

    # ---------------- selection / preview ----------------
    def selected_paths(self):
        return [it.data(Qt.UserRole) for it in self.gallery.selectedItems()]

    def _update_action_states(self):
        n = len(self.selected_paths())
        for name, b in self.act_buttons.items():
            single = name in ("Copy", "Rename", "Show in Folder")
            b.setEnabled(n == 1 if single else n >= 1)

    def update_preview(self):
        item = self.gallery.currentItem()
        if item is None:
            self.preview.setPixmap(QPixmap())
            self.preview.setText("Select a screenshot")
            self.info_lbl.setText("")
            return
        path = item.data(Qt.UserRole)
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        full = reader.size()
        target = self.preview.size() - QSize(10, 10)
        if full.isValid():
            reader.setScaledSize(full.scaled(target, Qt.KeepAspectRatio) if (full.width() > target.width() or full.height() > target.height()) else full)
        img = reader.read()
        if img.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText("Can't preview this file")
        else:
            self.preview.setPixmap(QPixmap.fromImage(img))
        try:
            st = os.stat(path)
            dims = f"{full.width()} × {full.height()} px  •  " if full.isValid() else ""
            self.info_lbl.setText(f"<b>{os.path.basename(path)}</b><br>{dims}{st.st_size / 1e6:.2f} MB<br>"
                                  f"{datetime.datetime.fromtimestamp(st.st_mtime):%A %d %B %Y, %H:%M}")
        except OSError:
            self.info_lbl.setText(os.path.basename(path))

    # ---------------- actions ----------------
    def open_selected(self):
        for path in self.selected_paths()[:10]:
            os.startfile(path)

    def copy_selected(self):
        paths = self.selected_paths()
        if len(paths) == 1:
            QApplication.clipboard().setImage(QImage(paths[0]))
            self.app.show_toast(os.path.basename(paths[0]), 0, None, title_text="Copied to clipboard")

    def show_in_folder(self):
        paths = self.selected_paths()
        if paths:
            import subprocess
            subprocess.Popen(["explorer", "/select,", paths[0]])

    def rename_selected(self):
        paths = self.selected_paths()
        if len(paths) != 1: return
        old = paths[0]
        base, ext = os.path.splitext(os.path.basename(old))
        new_base, ok = QInputDialog.getText(self, "Rename Screenshot", "New name:", text=base)
        new_base = (new_base or "").strip()
        if not ok or not new_base or new_base == base: return
        if any(c in new_base for c in '\\/:*?"<>|'):
            warn(self, "Invalid Name", 'A file name can\'t contain any of these characters:  \\ / : * ? " < > |')
            return
        new = os.path.join(os.path.dirname(old), new_base + ext)
        if os.path.exists(new):
            warn(self, "Name Taken", f"'{new_base + ext}' already exists.")
            return
        try:
            os.rename(old, new)
        except OSError as e:
            error(self, "Rename Failed", str(e))
            return
        self._replace_path(old, new)
        self.refresh_keep(new)

    def _replace_path(self, old, new):
        if old in self.thumbs:
            self.thumbs[new] = self.thumbs.pop(old)
        if old in self.app.session_images:
            self.app.session_images[self.app.session_images.index(old)] = new

    def refresh_keep(self, select_path=None):
        self.refresh()
        if select_path and select_path in self.items:
            self.gallery.clearSelection()
            self.gallery.setCurrentItem(self.items[select_path])

    def edit_selected(self):
        paths = self.selected_paths()
        if not paths: return
        def after():
            self._forget_thumbs(paths)
            self.apply_filter()
        self.app.launch_manual_redaction(paths, self.app, after)

    def add_to_session(self):
        added = [pth for pth in self.selected_paths() if pth not in self.app.session_images]
        self.app.session_images.extend(added)
        self.app.update_session_label()
        info(self, "Added to Session", f"Added {len(added)} screenshot(s) to the Auto-Scapture session.\n\n"
                                       "Use Review / Redact or Export on the Auto-Scapture tab.")

    def move_selected(self):
        paths = self.selected_paths()
        if not paths: return
        dest = QFileDialog.getExistingDirectory(self, "Move Screenshots To", self.folder)
        if not dest: return
        moved = 0
        for pth in paths:
            if os.path.normcase(os.path.dirname(pth)) == os.path.normcase(os.path.normpath(dest)):
                continue
            try:
                new = unique_path(dest, os.path.basename(pth))
                shutil.move(pth, new)
                self._replace_path(pth, new)
                moved += 1
            except OSError as e:
                warn(self, "Move Failed", f"{os.path.basename(pth)}:\n{e}")
        self.refresh()
        info(self, "Moved", f"Moved {moved} screenshot(s) to:\n{dest}")

    def delete_selected(self):
        paths = self.selected_paths()
        if not paths: return
        if not ask_yes_no(self, "Delete Screenshots", f"Move {len(paths)} screenshot(s) to the Recycle Bin?"):
            return
        if not send_to_recycle_bin(paths):
            warn(self, "Delete", "Some screenshots were not deleted.")
        self._forget_thumbs(paths)
        self.refresh()

    def organize_by_month(self):
        if not os.path.isdir(self.folder): return
        loose = [(pth, mt) for pth, mt, _sz in self.entries if os.path.normcase(os.path.dirname(pth)) == os.path.normcase(self.folder)]
        if not loose:
            info(self, "Organize by Month", "There are no loose screenshots to organize.")
            return
        months = sorted({datetime.datetime.fromtimestamp(mt).strftime("%Y-%m") for _p, mt in loose})
        if not ask_yes_no(self, "Organize by Month",
                          f"Move {len(loose):,} screenshot(s) into {len(months)} month folder(s) inside:\n{self.folder}\n\n"
                          f"({months[0]} ... {months[-1]})\n\nNothing is deleted. Continue?"):
            return
        moved = 0
        for pth, mt in loose:
            dest = os.path.join(self.folder, datetime.datetime.fromtimestamp(mt).strftime("%Y-%m"))
            try:
                os.makedirs(dest, exist_ok=True)
                new = unique_path(dest, os.path.basename(pth))
                shutil.move(pth, new)
                self._replace_path(pth, new)
                moved += 1
            except OSError:
                pass
        self.subfolders_cb.setChecked(True)  # keep them visible (also refreshes)
        info(self, "Organize by Month", f"Moved {moved:,} screenshot(s) into month folders.")

    # ---------------- taking screenshots ----------------
    def delay_seconds(self):
        text = self.delay_combo.currentText()
        return int(text.split()[0]) if text[0].isdigit() else 0

    def take(self, kind="snip", delay=None):
        if self._busy:
            return
        self._busy = True
        delay = self.delay_seconds() if delay is None else delay
        if delay > 0:
            self._countdown(delay, kind)
        else:
            self._begin(kind)

    def _countdown(self, remaining, kind):
        if remaining <= 0:
            if getattr(self, "_count_toast", None):
                self._count_toast.close()
                self._count_toast = None
            QTimer.singleShot(150, lambda: self._begin(kind))
            return
        if not getattr(self, "_count_toast", None):
            t = QLabel()
            t.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
            t.setAttribute(Qt.WA_ShowWithoutActivating)
            t.setStyleSheet("background: #1f2937; color: white; padding: 10px 18px; border-radius: 6px;")
            f = QFont("Segoe UI", 12); f.setBold(True)
            t.setFont(f)
            self._count_toast = t
        self._count_toast.setText(f"Screenshot in {remaining}...")
        self._count_toast.adjustSize()
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self._count_toast.move(geo.right() - self._count_toast.width() - 20, geo.bottom() - self._count_toast.height() - 20)
        self._count_toast.show()
        exclude_from_capture(self._count_toast)
        QTimer.singleShot(1000, lambda: self._countdown(remaining - 1, kind))

    def _begin(self, kind):
        hide_self = self.app.hide_from_capture and self.app.isVisible()
        if hide_self:
            exclude_from_capture(self.app, True)  # screenshot what's behind Auto-Scapture
        QTimer.singleShot(150 if hide_self else 0, lambda: self._grab(kind))

    def _grab(self, kind):
        try:
            if kind == "snip":
                self.session = ScreenshotSession()
                self.session.captured.connect(self._on_shot)
                self.session.cancelled.connect(self._on_cancel)
                self.session.start()
            elif kind == "screen":
                self._on_shot(ImageGrab.grab(bbox=monitor_rect_at(cursor_pos_physical()), all_screens=True))
            else:
                self._on_shot(ImageGrab.grab(all_screens=True))
        except Exception as e:
            self._busy = False
            error(self, "Screenshot Failed", str(e))
        finally:
            self.app.apply_capture_exclusion()

    def _on_cancel(self):
        self._busy = False
        self.session = None

    def _on_shot(self, img):
        self._busy = False
        self.session = None
        try:
            os.makedirs(self.folder, exist_ok=True)
            path = unique_path(self.folder, time.strftime("Screenshot %Y-%m-%d %H%M%S.png"))
            img.save(path)
        except OSError as e:
            error(self, "Screenshot Not Saved", f"Couldn't save to:\n{self.folder}\n\n{e}")
            return
        copied = self.clip_cb.isChecked()
        if copied:
            QApplication.clipboard().setImage(pil_to_pixmap(img).toImage())
        self.last_shot = path
        self.app.show_toast(os.path.basename(path), 0, img,
                            title_text="Screenshot saved" + (" & copied" if copied else ""))
        self.refresh_keep(os.path.normpath(path))

    # ---------------- global key / tray / autostart ----------------
    def _register_hotkey(self):
        self._unregister_hotkey()
        hk = self.hotkey_combo.currentText().strip().lower()
        if not hk:
            return
        try:
            # suppress=True so Windows (e.g. its own Print Screen handler) doesn't also react
            self._hotkey_handle = keyboard.add_hotkey(hk, lambda: self.app.ui_call(lambda: self.take("snip", delay=0)), suppress=True)
        except (ValueError, ImportError) as e:
            self._hotkey_handle = None
            self.hotkey_cb.blockSignals(True)
            self.hotkey_cb.setChecked(False)
            self.hotkey_cb.blockSignals(False)
            warn(self, "Screenshot Key", f"'{hk}' can't be used as a screenshot key:\n{e}")

    def _unregister_hotkey(self):
        if self._hotkey_handle is not None:
            try: keyboard.remove_hotkey(self._hotkey_handle)
            except (KeyError, ValueError): pass
            self._hotkey_handle = None

    def _hotkey_toggled(self, on):
        self._set_pref("hotkey_enabled", on)
        if on:
            if self.hotkey_combo.currentText().strip().lower() == self.app.hotkey_combo.currentText().strip().lower():
                warn(self, "Screenshot Key", "This is also your Auto-Scapture capture hotkey - pick a different key.")
                self.hotkey_cb.setChecked(False)
                return
            self._register_hotkey()
        else:
            self._unregister_hotkey()

    def _hotkey_changed(self):
        self._set_pref("hotkey", self.hotkey_combo.currentText().strip().lower())
        if self.hotkey_cb.isChecked():
            self._register_hotkey()

    def _tray_toggled(self, on):
        self._set_pref("run_in_background", on)
        self.app.update_tray()

    def _autostart_toggled(self, on):
        try:
            set_autostart(on)
        except OSError as e:
            warn(self, "Start with Windows", f"Couldn't change the startup setting:\n{e}")
        if on and not self.tray_cb.isChecked():
            self.tray_cb.setChecked(True)  # starting hidden only makes sense with the tray icon

    def shutdown(self):
        self._unregister_hotkey()
        self.pool.clear()


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
        self.dup_flags = {}          # capture path -> path of the near-identical capture before it
        self._last_saved_img = None
        self._last_saved_path = None
        self._run_flagged = 0

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
        self.play_sound = prefs.get("play_sound", False)
        self.hide_from_capture = prefs.get("hide_from_capture", True)
        self.pdf_page_size = prefs.get("pdf_page_size", "Original")
        self.pdf_page_numbers = prefs.get("pdf_page_numbers", False)
        self.is_pinned = False

        self.saved_hotkeys = prefs.get("saved_hotkeys", ["ctrl+shift+a", "f5"])
        self.saved_filenames = prefs.get("saved_filenames", ["Module1_"])

        self.renamer_extensions = prefs.get("renamer_extensions", [".txt", ".cfg", ".csv", ".json", ".md", ".log"])
        self.video_chapters = self.settings_data.get("video_chapters", {})
        self.shot_prefs = self.settings_data.get("screenshots", {})
        self.tray = None
        self._quitting = False
        self._tray_hint_shown = False

        # ==========================================
        # --- TABBED INTERFACE ---
        # ==========================================
        outer = QVBoxLayout(self)
        self._outer = outer
        outer.setContentsMargins(0, 0, 0, 0)
        self.notebook = QTabWidget()
        outer.addWidget(self.notebook)

        # The capture tab is a centred column that scrolls if the window is short, so all tabs can
        # share one window size (nothing resizes when switching tabs).
        self.cap_tab = QWidget()
        self.cap_tab.setMaximumWidth(620)
        cap_holder = QWidget()
        holder_lay = QHBoxLayout(cap_holder)
        holder_lay.setContentsMargins(0, 0, 0, 0)
        holder_lay.addStretch(1)
        holder_lay.addWidget(self.cap_tab, 100)
        holder_lay.addStretch(1)
        cap_scroll = QScrollArea()
        cap_scroll.setWidgetResizable(True)
        cap_scroll.setFrameShape(QFrame.NoFrame)
        cap_scroll.setWidget(cap_holder)
        self.rename_tab = QWidget()

        self.notebook.addTab(cap_scroll, "Auto-Scapture")
        self.notebook.addTab(self.rename_tab, "Lab Renamer")

        self.setup_capture_ui()
        self.setup_renamer_ui()
        self.shots_tab = ScreenshotsTab(self)
        self.notebook.insertTab(1, self.shots_tab, "Screenshots")

        self.video_tab = None
        if VIDEO_AVAILABLE:
            self.video_tab = VideoCaptureTab(self)
            self.notebook.addTab(self.video_tab, "Video Capture")

        self._apply_tab_size_policies(0)
        self.notebook.currentChanged.connect(self._apply_tab_size_policies)
        QShortcut(QKeySequence("Ctrl+M"), self, self.toggle_minimal)
        if self.view_size == "minimal":
            self._enter_minimal()
            self.set_status(self._status_text)
        self._sync_view_buttons()

        self.setAcceptDrops(True)
        # One window for every tab: restore the last size/position (and maximised state) if saved
        geometry = self.settings_data.get("window_geometry")
        restored = False
        if geometry:
            try:
                restored = self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            except Exception:
                restored = False
        if not restored:
            avail = self.screen().availableGeometry()
            self.resize(min(1100, avail.width() - 40), min(900, avail.height() - 40))

        icon_file = resource_path(os.path.join("assets", "icon.png"))
        if os.path.exists(icon_file):
            self.setWindowIcon(QIcon(icon_file))

        if prefs.get("pinned", False):
            self.pin_btn.setChecked(True)
        self.apply_capture_exclusion()
        self.update_tray()

    def apply_capture_exclusion(self):
        # Only hide the window while a capture is running, so screen sharing (Discord, Teams, OBS)
        # can still see Auto-Scapture the rest of the time.
        exclude_from_capture(self, self.hide_from_capture and self.is_listening)

    # ==========================================
    # --- LAYOUT MODES (Regular / Minimal x Vertical / Horizontal) ---
    # ==========================================
    def _view_key(self):
        return f"{self.view_size}-{self.view_orientation}"

    def _apply_tab_size_policies(self, index):
        """Only the visible tab sets the window's minimum size, so the Auto-Scapture tab can be
        made small even though the video player needs more room. (Switching tabs never resizes the
        window unless it's too small for the new tab.)"""
        for i in range(self.notebook.count()):
            policy = QSizePolicy.Preferred if i == index else QSizePolicy.Ignored
            self.notebook.widget(i).setSizePolicy(policy, policy)
        self.notebook.updateGeometry()

    def _take(self, widget):
        """Detach a widget from whatever layout currently holds it (so it can be moved)."""
        for lay in self.findChildren(QLayout):
            if lay.indexOf(widget) >= 0:
                lay.removeWidget(widget)
                return

    def _arrange_capture_tab(self):
        """(Re)build the regular Auto-Scapture tab in the current orientation using the existing cards."""
        cards = [self._top_row_w, self._header_w, self.setup_box, self.area_box, self.mode_frame, self.start_btn, self.session_box]
        for w in cards:
            self._take(w)
        host = QWidget()
        if self.view_orientation == "horizontal":
            v = QVBoxLayout(host)
            v.setContentsMargins(12, 8, 12, 10)
            v.setSpacing(4)
            v.addWidget(self._top_row_w)
            row = QHBoxLayout()
            row.setSpacing(12)
            col1, col2, col3 = QVBoxLayout(), QVBoxLayout(), QVBoxLayout()
            col1.addWidget(self._header_w)
            col1.addWidget(self.setup_box)
            col1.addStretch()
            col2.addWidget(self.area_box)
            col2.addWidget(self.mode_frame)
            col2.addSpacing(4)
            col2.addWidget(self.start_btn)
            col2.addStretch()
            col3.addWidget(self.session_box)
            col3.addStretch()
            for c in (col1, col2, col3):
                row.addLayout(c, 1)
            v.addLayout(row)
            v.addStretch()
            self.cap_tab.setMaximumWidth(16777215)
        else:
            v = QVBoxLayout(host)
            v.setContentsMargins(12, 8, 12, 10)
            v.setSpacing(4)
            for w in (self._top_row_w, self._header_w, self.setup_box, self.area_box, self.mode_frame):
                v.addWidget(w)
            v.addSpacing(6)
            v.addWidget(self.start_btn)
            v.addWidget(self.session_box)
            v.addStretch()
            self.cap_tab.setMaximumWidth(620)
        old = self._cap_host
        self.cap_tab.layout().addWidget(host)
        self._cap_host = host
        if old is not None:
            self.cap_tab.layout().removeWidget(old)
            old.deleteLater()

    # widgets shared between the regular tab and the minimal controller
    def _shared_widgets(self):
        return [self.pin_btn, self.select_btn, self.show_area_btn, self.capture_now_btn, self.status_label,
                self.start_btn, self.last_thumb, self.session_label, self.flag_label, self.review_btn]

    def _enter_minimal(self):
        self.notebook.setCurrentIndex(0)
        for w in self._shared_widgets():
            self._take(w)
        panel = QFrame()
        panel.setObjectName("miniPanel")
        self.mini_mode_combo = QComboBox()
        self.mini_mode_combo.addItems(["Manual", "Auto", "Smart"])
        self.mini_mode_combo.setCurrentText(self.capture_mode())
        self.mini_mode_combo.setToolTip("Capture mode (set slide count, delay and target in the full view)")
        self.mini_mode_combo.currentTextChanged.connect(lambda t: self.mode_radios[t].setChecked(True))
        self.mini_mode_combo.setEnabled(not self.is_listening)
        full_btn = make_button("Full View", lambda: self.set_view(size="regular"))
        full_btn.setToolTip("Back to the full window  (Ctrl+M)")
        other = "horizontal" if self.view_orientation == "vertical" else "vertical"
        rotate_btn = make_button(other.capitalize(), lambda: self.set_view(orientation=other))
        rotate_btn.setToolTip(f"Lay the compact controller out {other}ly")
        self.last_thumb.setFixedSize(80, 45)
        self.pin_btn.setText("Pin")
        self.select_btn.setText("Select Area")

        if self.view_orientation == "horizontal":
            # one slim strip
            lay = QHBoxLayout(panel)
            lay.setContentsMargins(8, 6, 8, 6)
            lay.setSpacing(6)
            for w in (self.pin_btn, self.select_btn, self.show_area_btn):
                lay.addWidget(w)
            lay.addWidget(self.status_label, 0, Qt.AlignVCenter)
            lay.addSpacing(6)
            lay.addWidget(self.mini_mode_combo)
            lay.addWidget(self.start_btn)
            lay.addWidget(self.capture_now_btn)
            lay.addSpacing(6)
            lay.addWidget(self.last_thumb)
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addWidget(self.session_label)
            col.addWidget(self.flag_label)
            lay.addLayout(col)
            self.review_btn.hide()  # kept to the vertical controller / full view to keep the strip short
            lay.addSpacing(6)
            lay.addWidget(rotate_btn)
            lay.addWidget(full_btn)
        else:
            # small column for a screen corner
            lay = QVBoxLayout(panel)
            lay.setContentsMargins(8, 6, 8, 8)
            lay.setSpacing(6)
            top = QHBoxLayout()
            top.addWidget(self.pin_btn)
            top.addStretch()
            top.addWidget(rotate_btn)
            top.addWidget(full_btn)
            lay.addLayout(top)
            lay.addWidget(self.status_label, alignment=Qt.AlignHCenter)
            r1 = QHBoxLayout()
            r1.addWidget(self.select_btn, 1)
            r1.addWidget(self.show_area_btn)
            lay.addLayout(r1)
            r2 = QHBoxLayout()
            r2.addWidget(self.mini_mode_combo, 1)
            r2.addWidget(self.capture_now_btn)
            lay.addLayout(r2)
            lay.addWidget(self.start_btn)
            r3 = QHBoxLayout()
            r3.addWidget(self.last_thumb)
            col = QVBoxLayout()
            col.setSpacing(0)
            col.addStretch()
            col.addWidget(self.session_label)
            col.addWidget(self.flag_label)
            col.addStretch()
            r3.addLayout(col, 1)
            lay.addLayout(r3)
            self.review_btn.show()
            lay.addWidget(self.review_btn)

        self.flag_label.setWordWrap(False)
        self.notebook.hide()
        self._outer.addWidget(panel)
        self.mini_panel = panel
        self.update_session_label()

    def _leave_minimal(self):
        for w in self._shared_widgets():
            self._take(w)
        # put every shared widget back exactly where it lives in the regular tab
        self._top_frame.insertWidget(0, self.pin_btn)
        self._area_row.insertWidget(0, self.select_btn, 1)
        self._area_row.insertWidget(1, self.show_area_btn)
        self._area_row.insertWidget(2, self.capture_now_btn)
        self._area_lay.insertWidget(1, self.status_label, 0, Qt.AlignHCenter)
        self._info_row.insertWidget(0, self.last_thumb)
        self._info_col.insertWidget(1, self.session_label)
        self._info_col.insertWidget(2, self.flag_label)
        self._btns.addWidget(self.review_btn, 0, 1)
        self.last_thumb.setFixedSize(112, 63)
        self.pin_btn.setText("Pin Window")
        self.select_btn.setText("1. Select Screen Area")
        self.review_btn.show()
        self.flag_label.setWordWrap(True)
        if self.mini_panel is not None:
            self._outer.removeWidget(self.mini_panel)
            self.mini_panel.deleteLater()
        self.mini_panel = None
        self.mini_mode_combo = None
        self.notebook.show()
        self._arrange_capture_tab()  # also re-inserts the start button
        self.update_session_label()

    def _sync_view_buttons(self):
        self.vert_btn.setChecked(self.view_orientation == "vertical")
        self.horiz_btn.setChecked(self.view_orientation == "horizontal")

    def toggle_minimal(self):
        self.set_view(size="regular" if self.view_size == "minimal" else "minimal")

    def set_view(self, size=None, orientation=None):
        size = size or self.view_size
        orientation = orientation or self.view_orientation
        if (size, orientation) == (self.view_size, self.view_orientation):
            self._sync_view_buttons()
            return
        # each layout remembers its own window size and position
        self._view_geoms[self._view_key()] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        was_minimal = self.view_size == "minimal"
        if was_minimal:
            self._leave_minimal()
        self.view_size, self.view_orientation = size, orientation
        if size == "minimal":
            self._enter_minimal()
        else:
            self._arrange_capture_tab()
        self._sync_view_buttons()
        self.update_session_label()
        self.set_status(getattr(self, "_status_text", "Area: Not Selected"))
        self._apply_tab_size_policies(self.notebook.currentIndex())
        QTimer.singleShot(0, self._restore_view_geometry)
        self.save_settings()

    def _restore_view_geometry(self):
        saved = self._view_geoms.get(self._view_key())
        if saved:
            try:
                if self.restoreGeometry(QByteArray.fromBase64(saved.encode("ascii"))):
                    return
            except Exception:
                pass
        if self.isMaximized() or self.isFullScreen():
            self.showNormal()
        self.layout().activate()
        avail = self.screen().availableGeometry()
        if self.view_size == "minimal":
            self.resize(self.minimumSizeHint())
        elif self.view_orientation == "horizontal":
            content_h = self._cap_host.sizeHint().height() + self.notebook.tabBar().sizeHint().height() + 8
            self.resize(min(1300, avail.width() - 40), min(content_h, avail.height() - 40))
        else:
            self.resize(min(1100, avail.width() - 40), min(900, avail.height() - 40))

    def ui_call(self, fn):
        """Thread-safe: schedule fn on the GUI thread."""
        self.bridge.call.emit(fn)

    # ==========================================
    # --- TAB 1: EXACT 1.1.1 CAPTURE UI ---
    # ==========================================
    def setup_capture_ui(self):
        prefs = self.settings_data.get("preferences", {})
        self.view_size = prefs.get("view_size", "regular")              # regular | minimal
        self.view_orientation = prefs.get("view_orientation", "vertical")  # vertical | horizontal
        self._view_geoms = dict(self.settings_data.get("view_geometries", {}))
        cap_outer = QVBoxLayout(self.cap_tab)
        cap_outer.setContentsMargins(0, 0, 0, 0)
        self._cap_host = None
        center = Qt.AlignHCenter

        top_frame = QHBoxLayout()
        self.pin_btn = QCheckBox("Pin Window")
        self.pin_btn.toggled.connect(self.toggle_pin)
        top_frame.addWidget(self.pin_btn)
        top_frame.addStretch()
        self.settings_btn = make_button("Preferences", self.open_settings_window)
        # Layout switches: Vertical / Horizontal, and Minimal (compact controller for pinning)
        self.vert_btn = make_button("Vertical", lambda: self.set_view(orientation="vertical"))
        self.horiz_btn = make_button("Horizontal", lambda: self.set_view(orientation="horizontal"))
        self.mini_btn = make_button("Minimal", lambda: self.set_view(size="minimal"))
        self.mini_btn.setToolTip("Switch to a compact controller you can pin over your slides  (Ctrl+M)")
        for b in (self.vert_btn, self.horiz_btn):
            b.setCheckable(True)
            b.setToolTip("Arrange the controls " + b.text().lower() + "ly")
        top_frame.addWidget(self.vert_btn)
        top_frame.addWidget(self.horiz_btn)
        top_frame.addSpacing(6)
        top_frame.addWidget(self.mini_btn)
        top_frame.addSpacing(6)
        top_frame.addWidget(self.settings_btn)
        self._top_frame = top_frame
        self._top_row_w = QWidget()
        self._top_row_w.setLayout(top_frame)

        header = QHBoxLayout()
        header.addStretch()
        logo = QLabel()
        icon_file = resource_path(os.path.join("assets", "icon.png"))
        if os.path.exists(icon_file):
            logo.setPixmap(QPixmap(icon_file).scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        header.addWidget(logo)
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_label = QLabel(APP_TITLE)
        f = QFont("Segoe UI", 13); f.setBold(True)
        title_label.setFont(f)
        title_label.setStyleSheet("color: #1b4a96;")
        subtitle = QLabel("Capture  •  Review  •  Export")
        subtitle.setStyleSheet("color: #6b7280;")
        title_col.addWidget(title_label)
        title_col.addWidget(subtitle)
        header.addLayout(title_col)
        header.addStretch()
        self._header_w = QWidget()
        self._header_w.setLayout(header)

        # --- Session setup card ---
        setup_box = QGroupBox("Session Setup")
        grid = QGridLayout(setup_box)
        grid.setHorizontalSpacing(6)
        grid.setColumnStretch(1, 1)

        default_path = os.path.join(os.path.expanduser("~"), "Desktop", "AutoCaptures")
        self.folder_entry = QLineEdit(prefs.get("save_folder") or default_path)
        self.folder_entry.editingFinished.connect(self.scan_for_pdfs)
        self.browse_btn = make_button("Browse", self.browse_folder)
        self.open_folder_btn = make_button("Open", self.open_session_folder)
        self.open_folder_btn.setToolTip("Open the current session folder (or the save folder)")

        self.name_combo = PopupComboBox()
        self.name_combo.setEditable(True)
        self.name_combo.aboutToShowPopup.connect(self.scan_for_pdfs)
        self.save_name_btn = make_button("Save", self.save_filename)
        self.del_name_btn = make_button("Delete", self.delete_filename)

        self.hotkey_combo = QComboBox()
        self.hotkey_combo.setEditable(True)
        self.hotkey_combo.addItems(self.saved_hotkeys)
        self.hotkey_combo.setEditText(self.saved_hotkeys[0] if self.saved_hotkeys else "ctrl+shift+a")
        self.save_hk_btn = make_button("Save", self.save_hotkey)
        self.del_hk_btn = make_button("Delete", self.delete_hotkey)

        self.preset_combo = QComboBox()
        self.preset_combo.addItems(list(self.presets.keys()))
        self.preset_combo.setCurrentIndex(-1)
        self.preset_combo.setPlaceholderText("Choose a saved area…")
        self.preset_combo.textActivated.connect(self.apply_preset)
        self.save_preset_btn = make_button("Save Area", self.save_preset)
        self.del_preset_btn = make_button("Delete", self.delete_preset)

        for row, (label, field, b1, b2) in enumerate([
                ("Master Save Directory:", self.folder_entry, self.browse_btn, self.open_folder_btn),
                ("Base File Name:", self.name_combo, self.save_name_btn, self.del_name_btn),
                ("Capture Hotkey:", self.hotkey_combo, self.save_hk_btn, self.del_hk_btn),
                ("Saved Area Presets:", self.preset_combo, self.save_preset_btn, self.del_preset_btn)]):
            grid.addWidget(QLabel(label), row * 2, 0, 1, 4)
            grid.addWidget(field, row * 2 + 1, 0, 1, 2)
            grid.addWidget(b1, row * 2 + 1, 2)
            grid.addWidget(b2, row * 2 + 1, 3)
        self.setup_box = setup_box

        # --- Capture area card ---
        area_box = QGroupBox("Capture Area")
        area_lay = QVBoxLayout(area_box)
        area_row = QHBoxLayout()
        self.select_btn = make_button("1. Select Screen Area", self.activate_snipping, bg=PRIMARY, fg="white", bold=True)
        self.select_btn.setMinimumHeight(32)
        self.show_area_btn = make_button("Show", self.flash_area)
        self.show_area_btn.setToolTip("Briefly outline the capture area on screen")
        self.capture_now_btn = make_button("Capture Now", self.capture_now)
        self.capture_now_btn.setToolTip("Take a single screenshot of the area right now")
        area_row.addWidget(self.select_btn, 1)
        area_row.addWidget(self.show_area_btn)
        area_row.addWidget(self.capture_now_btn)
        area_lay.addLayout(area_row)
        self.status_label = QLabel()
        self.status_label.setAlignment(Qt.AlignCenter)
        area_lay.addWidget(self.status_label, alignment=center)
        self.area_box, self._area_lay, self._area_row = area_box, area_lay, area_row

        # --- Capture mode card ---
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
        self.dyn_grid.setContentsMargins(0, 5, 0, 0)
        mode_lay.addWidget(self.dynamic_settings_frame)

        self.auto_count_lbl = QLabel("Slides:")
        self.auto_count_entry = QSpinBox()
        self.auto_count_entry.setRange(1, 9999)
        self.auto_count_entry.setValue(10)

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

        self.skip_dupes_cb = QCheckBox("Skip duplicate slides (screen didn't change)")
        self.skip_dupes_cb.setChecked(prefs.get("skip_duplicates", True))
        self.skip_dupes_cb.toggled.connect(self.save_settings)
        self.stop_static_cb = QCheckBox("Stop early when slides stop changing (end of deck)")
        self.stop_static_cb.setChecked(prefs.get("stop_on_static", True))
        self.stop_static_cb.toggled.connect(self.save_settings)

        self.dyn_widgets = [self.auto_count_lbl, self.auto_count_entry, self.auto_delay_lbl, self.delay_frame,
                            self.auto_key_lbl, self.auto_key_combo, self.set_ref_btn, self.ref_status_lbl,
                            self.skip_dupes_cb, self.stop_static_cb]

        self.cont_cb = QCheckBox("Enable Continuous Capture (Hold key to spam)")
        self.cont_cb.setChecked(prefs.get("continuous_capture", False))
        self.cont_cb.toggled.connect(self.save_settings)
        mode_lay.addWidget(self.cont_cb)
        self.mode_frame = mode_frame

        self.start_btn = make_button("2. Start Capture Sequence", self.toggle_listening, bg=PRIMARY, fg="white", bold=True, point_size=10)
        self.start_btn.setMinimumHeight(38)
        self.start_btn.setEnabled(False)

        # --- Session card ---
        session_box = QGroupBox("Session")
        sess = QVBoxLayout(session_box)
        info_row = QHBoxLayout()
        self.last_thumb = QLabel("No captures yet")
        self.last_thumb.setFixedSize(112, 63)
        self.last_thumb.setAlignment(Qt.AlignCenter)
        self.last_thumb.setStyleSheet("background: #eef1f5; color: #9ca3af; border: 1px solid #dde3ea; border-radius: 6px; font-size: 8pt;")
        info_row.addWidget(self.last_thumb)
        info_col = QVBoxLayout()
        info_col.setSpacing(2)
        self.session_label = QLabel("Images in current session: 0")
        f = self.session_label.font(); f.setBold(True); self.session_label.setFont(f)
        self.flag_label = QLabel()
        self.flag_label.setStyleSheet("color: #b45309; font-weight: bold;")
        self.flag_label.setWordWrap(True)
        self.flag_label.hide()
        hint = QLabel("Tip: drop image files on this window to add them.")
        hint.setStyleSheet("color: #6b7280; font-size: 8pt;")
        hint.setWordWrap(True)
        info_col.addStretch()
        info_col.addWidget(self.session_label)
        info_col.addWidget(self.flag_label)
        info_col.addWidget(hint)
        info_col.addStretch()
        info_row.addLayout(info_col, 1)
        sess.addLayout(info_row)

        btns = QGridLayout()
        self.load_btn = make_button("Load Past Images", self.load_past_images, bg="#fff2cc")
        self.review_btn = make_button("Review / Redact", self.open_review_window, bg="#cfe2f3")
        self.md_btn = make_button("Export Markdown", self.export_to_markdown, bg="#ece3f7")
        self.md_btn.setToolTip("Write a Markdown document (one step per image) next to the images")
        self.clear_btn = make_button("Clear Session", self.clear_session, bg="#ffcccc")
        self.pdf_btn = make_button("3. Export Session to PDF", self.export_to_pdf, bg="#d9ead3", bold=True, point_size=10)
        self.pdf_btn.setMinimumHeight(34)
        btns.addWidget(self.load_btn, 0, 0)
        btns.addWidget(self.review_btn, 0, 1)
        btns.addWidget(self.md_btn, 1, 0)
        btns.addWidget(self.clear_btn, 1, 1)
        btns.addWidget(self.pdf_btn, 2, 0, 1, 2)
        sess.addLayout(btns)
        self.session_box = session_box
        self._info_row, self._info_col, self._btns = info_row, info_col, btns
        self.mini_panel = None
        self.mini_mode_combo = None
        self._arrange_capture_tab()

        if self.saved_filenames:
            self.name_combo.setEditText(self.saved_filenames[0])
        else:
            self.name_combo.setEditText("Module1_")
        self.scan_for_pdfs()
        self.set_status("Area: Not Selected")
        saved_mode = prefs.get("capture_mode", "Manual")
        self.mode_radios.get(saved_mode, self.mode_radios["Manual"]).setChecked(True)
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

        sel_btn = make_button("1. Select Files to Rename", self.load_rename_files, bg=PRIMARY, fg="white", bold=True, point_size=10)
        sel_btn.setMinimumHeight(40)
        sel_row = QHBoxLayout(); sel_row.setContentsMargins(20, 0, 20, 0); sel_row.addWidget(sel_btn)
        lay.addLayout(sel_row)

        self.rename_lb_main = QListWidget()
        self.rename_lb_main.setFont(QFont("Consolas", 9))
        lay.addWidget(self.rename_lb_main, 1)

        self.rename_count_lbl = QLabel("Files Loaded: 0")
        lay.addWidget(self.rename_count_lbl, alignment=Qt.AlignHCenter)
        tip = QLabel("Tip: you can also drop files onto this tab.")
        tip.setStyleSheet("color: #6b7280; font-size: 8pt;")
        lay.addWidget(tip, alignment=Qt.AlignHCenter)

        rev_btn = make_button("2. Review, Convert & Rename", self.open_renamer_review_window, bg="#cfe2f3", bold=True, point_size=10)
        rev_btn.setMinimumHeight(40)
        rev_row = QHBoxLayout(); rev_row.setContentsMargins(20, 10, 20, 10); rev_row.addWidget(rev_btn)
        lay.addLayout(rev_row)
        lay.addWidget(make_button("Clear Loaded Files", self.clear_rename_list, bg="#ffcccc"), alignment=Qt.AlignHCenter)

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
        QShortcut(QKeySequence.Delete, self.r_listbox, self.remove_rename_item, context=Qt.WidgetShortcut)
        left.addWidget(self.r_listbox, 1)

        for fp in self.rename_files_list:
            self.r_listbox.addItem(os.path.basename(fp))

        btn_grid = QGridLayout()
        btn_grid.addWidget(make_button("Up", self.move_rename_up), 0, 0)
        btn_grid.addWidget(make_button("Down", self.move_rename_down), 0, 1)
        btn_grid.addWidget(make_button("Delete", self.remove_rename_item, fg="red"), 0, 2)
        btn_grid.addWidget(make_button("Top", self.move_rename_top), 1, 0)
        btn_grid.addWidget(make_button("Bottom", self.move_rename_bottom), 1, 1)
        btn_grid.addWidget(make_button("Reverse", self.reverse_rename_list), 1, 2)
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
        self.rn_smart_cb = QCheckBox("Context Naming (use 'Title:' / 'Experiment:' header inside the file)")
        self.rn_smart_cb.setStyleSheet("color: white;")
        rg.addWidget(self.rn_smart_cb, 1, 0, 1, 6)
        rg.addWidget(make_button("APPLY RENAME & CONVERT TO ALL", lambda: self.execute_batch_rename(r_win),
                                 bg="#4CAF50", fg="white", bold=True, point_size=10), 2, 0, 1, 6)
        rg.setColumnStretch(1, 1)
        right.addWidget(rename_frame)

        combine_frame = QGroupBox("File Merger")
        cl = QVBoxLayout(combine_frame)
        cl.addWidget(make_button("COMBINE ALL LISTED FILES INTO ONE", self.combine_files, bg="#337ab7", fg="white", bold=True, point_size=10))
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

        use_context = self.rn_smart_cb.isChecked()
        plan = []
        for i, old_path in enumerate(self.rename_files_list):
            dir_n = os.path.dirname(old_path)
            ext = os.path.splitext(old_path)[1]
            final_ext = ext if new_ext_val == "Keep Original" else new_ext_val
            new_name = f"{base}{start_idx + i}{final_ext}"

            if use_context and ext.lower() in [".txt", ".cfg", ".log", ".md", ".csv", ".ini"]:
                try:
                    with open(old_path, 'r', encoding='utf-8', errors='ignore') as f:
                        for _ in range(5):
                            line = f.readline()
                            m = re.search(r"(?:Title|Experiment|Lab|Name):\s*(.*)", line, re.I)
                            if m:
                                clean = "".join(x for x in m.group(1).strip() if x.isalnum() or x in " -_").strip()
                                if clean:
                                    new_name = f"{clean}{final_ext}"
                                    break
                except Exception as e:
                    print(f"Error reading file {old_path}: {e}")

            plan.append((i, old_path, os.path.join(dir_n, new_name)))

        # Refuse plans where two files would end up with the same name
        seen, dups = set(), set()
        for _, _, new_path in plan:
            key = os.path.normcase(new_path)
            if key in seen: dups.add(os.path.basename(new_path))
            seen.add(key)
        if dups:
            error(window, "Name Collision", "These names would be used by more than one file:\n\n" + "\n".join(sorted(dups)[:10]))
            return

        # Warn before overwriting files that are not part of this batch
        batch = {os.path.normcase(old) for _, old, _ in plan}
        conflicts = [os.path.basename(n) for _, _, n in plan if os.path.normcase(n) not in batch and os.path.exists(n)]
        if conflicts and not ask_yes_no(window, "Overwrite?", f"{len(conflicts)} file(s) already exist and will be overwritten:\n\n"
                                        + "\n".join(conflicts[:10]) + "\n\nContinue?"):
            return

        # Two-phase rename so files can swap/shift names without clobbering each other
        count, errors, staged = 0, [], []
        for i, old_path, new_path in plan:
            if old_path == new_path:
                count += 1
                continue
            tmp_path = f"{old_path}.renaming{i}"
            try:
                os.rename(old_path, tmp_path)
                staged.append((i, old_path, tmp_path, new_path))
            except Exception as e:
                errors.append(f"{os.path.basename(old_path)}: {e}")

        for i, old_path, tmp_path, new_path in staged:
            try:
                os.replace(tmp_path, new_path)
                self.rename_files_list[i] = new_path
                count += 1
            except Exception as e:
                errors.append(f"{os.path.basename(old_path)}: {e}")
                try: os.rename(tmp_path, old_path)
                except OSError: self.rename_files_list[i] = tmp_path

        if new_ext_val != "Keep Original" and new_ext_val not in self.renamer_extensions:
            self.renamer_extensions.append(new_ext_val)
            self.save_settings()

        self.refresh_renamer_main_list()
        if errors:
            self._refresh_r_listbox()
            warn(window, "Finished With Errors", f"Renamed {count} of {len(plan)} files.\n\nProblems:\n" + "\n".join(errors[:10]))
            return
        info(window, "Success", f"Successfully renamed {count} files.")
        window.close()

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
        settings_win = self._new_dialog(self, "Auto-Scapture Preferences", 680, 480)
        lay = QVBoxLayout(settings_win)

        opts = QGroupBox("Capture && Export")
        og = QGridLayout(opts)

        def pref_checkbox(text, attr, row, col, after=None):
            box = QCheckBox(text)
            box.setChecked(getattr(self, attr))
            def changed(checked):
                setattr(self, attr, checked)
                if after: after()
                self.save_settings()
            box.toggled.connect(changed)
            og.addWidget(box, row, col)

        pref_checkbox("Play a sound on each capture", "play_sound", 0, 0)
        pref_checkbox("Hide Auto-Scapture from its own screenshots (while capturing)", "hide_from_capture", 0, 1, self.apply_capture_exclusion)
        pref_checkbox("Add page numbers to PDF", "pdf_page_numbers", 1, 0)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("PDF page size:"))
        size_combo = QComboBox()
        size_combo.addItems(["Original", "A4", "Letter"])
        size_combo.setCurrentText(self.pdf_page_size)
        def size_changed(text):
            self.pdf_page_size = text
            self.save_settings()
        size_combo.currentTextChanged.connect(size_changed)
        size_row.addWidget(size_combo)
        size_row.addStretch()
        og.addLayout(size_row, 1, 1)
        lay.addWidget(opts)

        top_pref_frame = QHBoxLayout()
        cb = QCheckBox("Auto-open PDF after export")
        cb.setChecked(self.auto_open_pdf)
        def on_auto_open(checked):
            self.auto_open_pdf = checked
            self.save_settings()
        cb.toggled.connect(on_auto_open)
        top_pref_frame.addWidget(cb)
        top_pref_frame.addStretch()
        top_pref_frame.addWidget(make_button("View Raw settings.json", self.view_settings_file))
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
            "video_chapters": self.video_chapters,
            "screenshots": self.shot_prefs,
            "window_geometry": bytes(self.saveGeometry().toBase64()).decode("ascii"),
            "view_geometries": self._view_geoms,
            "preferences": {
                "auto_open_pdf": self.auto_open_pdf,
                "continuous_capture": self.cont_cb.isChecked(),
                "saved_hotkeys": self.saved_hotkeys,
                "saved_filenames": self.saved_filenames,
                "delay_min": self.auto_delay_min_entry.value(),
                "delay_max": self.auto_delay_max_entry.value(),
                "renamer_extensions": self.renamer_extensions,
                "save_folder": self.folder_entry.text(),
                "capture_mode": self.capture_mode(),
                "view_size": self.view_size,
                "view_orientation": self.view_orientation,
                "pinned": self.is_pinned,
                "play_sound": self.play_sound,
                "hide_from_capture": self.hide_from_capture,
                "pdf_page_size": self.pdf_page_size,
                "pdf_page_numbers": self.pdf_page_numbers,
                "skip_duplicates": self.skip_dupes_cb.isChecked(),
                "stop_on_static": self.stop_static_cb.isChecked()
            }
        }
        try:
            with open(self.settings_file, "w") as f: json.dump(data, f, indent=4)
        except OSError as e:
            print(f"Could not save settings: {e}")

    def set_status(self, text):
        has_area = self.capture_region is not None
        self._status_text = text
        if getattr(self, "view_size", "regular") == "minimal":
            # the compact controller only has room for the size
            if has_area:
                x1, y1, x2, y2 = self.capture_region
                text = f"{x2 - x1} × {y2 - y1} px"
            else:
                text = "No area selected"
        self.status_label.setText(text)
        if has_area:
            self.status_label.setStyleSheet("background: #e6f4ea; color: #1e7e34; border-radius: 10px; padding: 3px 12px;")
        else:
            self.status_label.setStyleSheet("background: #eef1f5; color: #5b6573; border-radius: 10px; padding: 3px 12px;")
        self.show_area_btn.setEnabled(has_area)
        self.capture_now_btn.setEnabled(has_area and not (self.is_listening and self.capture_mode() != "Manual"))

    def update_mode_ui(self):
        if self.mini_mode_combo is not None and self.mini_mode_combo.currentText() != self.capture_mode():
            self.mini_mode_combo.blockSignals(True)
            self.mini_mode_combo.setCurrentText(self.capture_mode())
            self.mini_mode_combo.blockSignals(False)
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
            place(self.skip_dupes_cb, 1, 0, 1, 6)
            place(self.stop_static_cb, 2, 0, 1, 6)
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
            place(self.skip_dupes_cb, 2, 0, 1, 6)
            self.start_btn.setText("2. Start Smart Capture (3s Delay)")
            self.hotkey_combo.setEnabled(False)
            self.save_hk_btn.setEnabled(False)
            self.del_hk_btn.setEnabled(False)

    def capture_reference_image(self):
        if not self.capture_region:
            warn(self, "No Area", "Please select a Screen Area first!")
            return

        self.reference_end_image = self._grab_without_self()
        self.ref_status_lbl.setText("Target Slide: SAVED")
        self.ref_status_lbl.setStyleSheet("color: green;")

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
        self.apply_capture_exclusion()  # changing window flags recreates the native window
        self.save_settings()

    def apply_preset(self, selected):
        if selected in self.presets:
            self.capture_region = tuple(self.presets[selected])
            w, h = self.capture_region[2] - self.capture_region[0], self.capture_region[3] - self.capture_region[1]
            self.set_status(f"Preset '{selected}': {w} × {h} px")
            self.start_btn.setEnabled(True)

    def browse_folder(self):
        folder_selected = QFileDialog.getExistingDirectory(self, "Select Folder", self.folder_entry.text())
        if folder_selected:
            self.folder_entry.setText(os.path.normpath(folder_selected))
            self.scan_for_pdfs()

    def activate_snipping(self):
        self.snipper = SnipController()
        self.snipper.regionSelected.connect(self.on_region_selected)
        self.snipper.show()

    def _grab_without_self(self):
        """Grab the capture area, making sure this window isn't in the shot."""
        if self.hide_from_capture:
            exclude_from_capture(self, True)
            QApplication.processEvents()
            time.sleep(0.1)  # let the compositor apply it
            try:
                return grab_region(self.capture_region)
            finally:
                self.apply_capture_exclusion()
        self.setWindowOpacity(0.0)
        QApplication.processEvents()
        time.sleep(0.2)
        try:
            return grab_region(self.capture_region)
        finally:
            self.setWindowOpacity(1.0)

    def capture_now(self):
        if not self.capture_region:
            warn(self, "No Area", "Please select a Screen Area first!")
            return
        self.save_raw_image(self._grab_without_self())

    def flash_area(self):
        if not self.capture_region: return
        self._flash = AreaFlash(physical_to_logical_rect(self.capture_region))
        self._flash.show()
        exclude_from_capture(self._flash)

    def open_session_folder(self):
        folder = self.current_session_folder or self.folder_entry.text()
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [os.path.normpath(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if os.path.isfile(p)]
        if self.video_tab is not None and self.notebook.currentWidget() is self.video_tab:
            videos = [p for p in paths if p.lower().endswith(VIDEO_EXTS)]
            if videos:
                self.video_tab.open_video(videos[0])
            else:
                info(self, "Not a Video", "Drop a video file (MP4, MKV, MOV, AVI, WEBM...) to open it.")
        elif self.notebook.currentWidget() is self.rename_tab:
            added = [p for p in paths if p not in self.rename_files_list]
            self.rename_files_list.extend(added)
            self.refresh_renamer_main_list()
        elif not self.is_listening:
            added = [p for p in paths if p.lower().endswith(IMAGE_EXTS) and p not in self.session_images]
            self.session_images.extend(added)
            self.update_session_label()
            if not added:
                info(self, "Nothing Added", "Drop PNG, JPG or BMP images to add them to the session.")
        event.acceptProposedAction()

    def on_region_selected(self, region):
        self.capture_region = region
        self.preset_combo.setCurrentIndex(-1)
        w, h = region[2] - region[0], region[3] - region[1]
        self.set_status(f"Area Selected: {w} × {h} px")
        self.flash_area()
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

        for i in range(1, self.notebook.count()):
            self.notebook.setTabEnabled(i, enabled)

        mode = self.capture_mode()
        if mode == "Manual":
            self.hotkey_combo.setEnabled(enabled)
            self.cont_cb.setEnabled(enabled)
        elif mode == "Auto":
            self.auto_count_entry.setEnabled(enabled)
            self.auto_delay_min_entry.setEnabled(enabled)
            self.auto_delay_max_entry.setEnabled(enabled)
            self.auto_key_combo.setEnabled(enabled)
            self.skip_dupes_cb.setEnabled(enabled)
            self.stop_static_cb.setEnabled(enabled)
            self.capture_now_btn.setEnabled(enabled and self.capture_region is not None)
        elif mode == "Smart":
            self.set_ref_btn.setEnabled(enabled)
            self.auto_delay_min_entry.setEnabled(enabled)
            self.auto_delay_max_entry.setEnabled(enabled)
            self.auto_key_combo.setEnabled(enabled)
            self.skip_dupes_cb.setEnabled(enabled)
            self.capture_now_btn.setEnabled(enabled and self.capture_region is not None)

        for rb in self.mode_radios.values():
            rb.setEnabled(enabled)
        if self.mini_mode_combo is not None:
            self.mini_mode_combo.setEnabled(enabled)

    def set_start_btn(self, text, color=None):
        """color is passed while a capture is running; the button turns red as a stop button."""
        self.start_btn.setText(text)
        self.start_btn.setStyleSheet(button_qss(DANGER if color else PRIMARY, "white"))

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
            self._run_flagged = 0
            self.apply_capture_exclusion()
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
                    self.apply_capture_exclusion()
                    return
                self.set_start_btn(f"Stop Listening ({custom_hotkey})", "red")
            else:
                # Global Esc works even while the slideshow has focus
                self.abort_hotkey = keyboard.add_hotkey("esc", lambda: self.ui_call(self.abort_capture))

            if mode == "Auto":
                self.set_start_btn("Stop Auto-Capture (Esc / Click to Abort)", "red")
                slides = self.auto_count_entry.value()
                threading.Thread(target=self.run_auto_capture_thread, daemon=True,
                                 args=(slides, delay_min, delay_max, key, self.skip_dupes_cb.isChecked(), self.stop_static_cb.isChecked())).start()
            elif mode == "Smart":
                self.set_start_btn("Stop Smart Capture (Esc / Click to Abort)", "red")
                threading.Thread(target=self.run_smart_capture_thread, daemon=True,
                                 args=(delay_min, delay_max, key, self.skip_dupes_cb.isChecked())).start()

        else:
            self.is_listening = False
            self.apply_capture_exclusion()
            self.toggle_ui_lock(lock=False)
            if getattr(self, "abort_hotkey", None) is not None:
                try: keyboard.remove_hotkey(self.abort_hotkey)
                except (KeyError, ValueError): pass
                self.abort_hotkey = None
            mode = self.capture_mode()
            if mode == "Manual":
                try: keyboard.remove_hotkey(getattr(self, "current_hotkey", None))
                except (KeyError, ValueError): pass
                self.set_start_btn("2. Start Listening")
            elif mode == "Auto":
                self.set_start_btn("2. Start Auto-Capture (3s Delay)")
            elif mode == "Smart":
                self.set_start_btn("2. Start Smart Capture (3s Delay)")

    def abort_capture(self):
        if self.is_listening and self.capture_mode() in ("Auto", "Smart"):
            self.toggle_listening()

    def compare_images(self, img1, img2, tolerance=2.0):
        return image_difference(img1, img2) < tolerance

    def _reset_duplicate_tracking(self):
        self.dup_flags.clear()
        self._last_saved_img = None
        self._last_saved_path = None

    def flagged_in_session(self):
        return [p for p in self.session_images if p in self.dup_flags]

    def _flag_summary(self):
        n = self._run_flagged
        if not n:
            return ""
        return (f"\n\n{n} capture(s) were flagged as possible duplicates (nearly identical to the slide before). "
                "This usually means a slide was captured before it finished changing.\n\n"
                "Check them in Review / Redact, or increase the delay.")

    def _report_flags(self):
        summary = self._flag_summary().strip()
        if summary:
            warn(self, "Possible Duplicates", summary)

    def _confirm_flagged_export(self):
        """Returns True if it's OK to export (no flags, or the user chose to export anyway)."""
        flagged = self.flagged_in_session()
        if not flagged:
            return True
        if ask_yes_no(self, "Possible Duplicates",
                      f"{len(flagged)} capture(s) in this session are flagged as possible duplicates.\n\n"
                      "Export anyway?\n\n(Choose 'No' to review them first.)"):
            return True
        self.open_review_window()
        return False

    def run_smart_capture_thread(self, delay_min, delay_max, key, skip_dupes=False):
        for i in range(3, 0, -1):
            if not self.is_listening: return
            self.ui_call(lambda i=i: self.start_btn.setText(f"Starting in {i}..."))
            time.sleep(1)

        if not self.is_listening: return

        max_slides = 200
        suppress_detection = False
        last_img = None
        skipped = 0

        for i in range(max_slides):
            if not self.is_listening: break

            self.ui_call(lambda curr=i+1: self.start_btn.setText(f"Scanning Slide {curr} - Esc to Stop"))
            current_img = grab_region(self.capture_region)

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

            if skip_dupes and last_img is not None and is_identical(current_img, last_img):
                skipped += 1
            else:
                self.ui_call(lambda img=current_img: self.save_raw_image(img))
            last_img = current_img
            time.sleep(0.3)

            if i < max_slides - 1:
                keyboard.send(key)
                actual_delay = random.uniform(delay_min, delay_max)
                time.sleep(actual_delay)

        if i >= max_slides - 1:
            self.ui_call(lambda: warn(self, "Max Slides Reached", "Stopped automatically after 200 slides to prevent infinite loop."))

        if self.is_listening:
            self.ui_call(self.toggle_listening)
            self.ui_call(self._report_flags)

    def run_auto_capture_thread(self, slides, delay_min, delay_max, key, skip_dupes=False, stop_on_static=False):
        for i in range(3, 0, -1):
            if not self.is_listening: return
            self.ui_call(lambda i=i: self.start_btn.setText(f"Starting in {i}..."))
            time.sleep(1)

        if not self.is_listening: return

        region = self.capture_region
        last_img = None
        unchanged_streak = 0
        saved = skipped = 0
        stopped_early = False

        for i in range(slides):
            if not self.is_listening: break

            self.ui_call(lambda curr=i+1, total=slides: self.start_btn.setText(f"Capturing {curr}/{total} - Esc to Stop"))
            img = grab_region(region)

            if last_img is not None and is_identical(img, last_img):
                # Pressing the key didn't change the slide
                unchanged_streak += 1
                if stop_on_static and unchanged_streak >= 2:
                    stopped_early = True
                    break
                if skip_dupes:
                    skipped += 1
                else:
                    self.ui_call(lambda img=img: self.save_raw_image(img))
                    saved += 1
            else:
                unchanged_streak = 0
                self.ui_call(lambda img=img: self.save_raw_image(img))
                saved += 1
            last_img = img
            time.sleep(0.3)

            if i < slides - 1:
                keyboard.send(key)
                actual_delay = random.uniform(delay_min, delay_max)
                time.sleep(actual_delay)

        if self.is_listening:
            msg = f"Captured {saved} slides!"
            if skipped: msg += f"\n\nSkipped {skipped} duplicate slide(s)."
            if stopped_early: msg += "\n\nStopped early because the slides stopped changing (end of deck)."
            self.ui_call(self.toggle_listening)
            self.ui_call(lambda: info(self, "Done", msg + self._flag_summary()))

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

        # Flag captures that are nearly identical to the previous one (e.g. captured mid-transition)
        flagged_prev = None
        if (self._last_saved_img is not None and self.session_images
                and self.session_images[-1] == self._last_saved_path
                and looks_like_duplicate(img_object, self._last_saved_img)):
            flagged_prev = self._last_saved_path

        img_object.save(filepath)
        self.session_images.append(filepath)
        self._last_saved_img, self._last_saved_path = img_object, filepath
        if flagged_prev:
            self.dup_flags[filepath] = flagged_prev
            self._run_flagged += 1
        self.show_toast(filename, len(self.session_images), img_object, flagged=bool(flagged_prev))
        self.update_session_label()
        self.counter += 1
        if self.play_sound:
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
            except Exception:
                pass

    def update_session_label(self):
        n = len(self.session_images)
        flagged = len(self.flagged_in_session())
        if getattr(self, "view_size", "regular") == "minimal":
            self.session_label.setText(f"{n} image(s)")
            self.flag_label.setText(f"{flagged} flagged")
        else:
            self.session_label.setText(f"Images in current session: {n}")
            self.flag_label.setText(f"{flagged} possible duplicate(s) flagged - check Review / Redact")
        self.flag_label.setVisible(flagged > 0)
        pix = QPixmap(self.session_images[-1]) if n else QPixmap()
        if pix.isNull():
            self.last_thumb.setPixmap(QPixmap())
            self.last_thumb.setText("No captures" if getattr(self, "view_size", "regular") == "minimal" else "No captures yet")
        else:
            self.last_thumb.setPixmap(pix.scaled(self.last_thumb.size() - QSize(4, 4), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def take_screenshot(self, is_auto=False):
        if not is_auto and not self.cont_cb.isChecked():
            if getattr(self, '_key_locked', False): return
            self._key_locked = True
            self.check_key_release()

        if self.capture_region:
            img = grab_region(self.capture_region)
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

            self.update_session_label()

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

        review_win = self._new_dialog(self, "Review Session Queue", 900, 600)
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
        f = QFont("Segoe UI", 10); f.setBold(True); hdr.setFont(f)
        left.addWidget(hdr, alignment=Qt.AlignHCenter)
        count_lbl = QLabel()
        count_lbl.setStyleSheet("color: #6b7280;")
        left.addWidget(count_lbl, alignment=Qt.AlignHCenter)

        listbox = ReorderList()
        listbox.setSelectionMode(QAbstractItemView.ExtendedSelection)
        listbox.setIconSize(QSize(96, 54))
        left.addWidget(listbox, 1)

        thumbs = {}
        def thumb(path):
            if path not in thumbs:
                pix = QPixmap(path)
                thumbs[path] = QIcon(pix.scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)) if not pix.isNull() else QIcon()
            return thumbs[path]

        def style_item(item, n, filepath):
            ref = self.dup_flags.get(filepath)
            if ref:
                ref_label = f"#{self.session_images.index(ref) + 1}" if ref in self.session_images else "a removed image"
                item.setText(f"{n}. {os.path.basename(filepath)}\n     Possible duplicate of {ref_label}")
                item.setBackground(QColor("#fff4ce"))
                item.setToolTip("Nearly identical to the capture before it - it may have been taken before the slide finished changing.")
            else:
                item.setText(f"{n}. {os.path.basename(filepath)}")
                item.setBackground(QColor(0, 0, 0, 0))
                item.setToolTip("")

        def update_count():
            flagged = len(self.flagged_in_session())
            text = f"{len(self.session_images)} image(s)"
            if flagged: text += f"  •  {flagged} flagged"
            count_lbl.setText(text + "  •  drag to reorder")
            count_lbl.setStyleSheet("color: #b45309;" if flagged else "color: #6b7280;")

        def refresh_lb():
            listbox.clear()
            for n, filepath in enumerate(self.session_images, 1):
                item = QListWidgetItem(thumb(filepath), "")
                item.setData(Qt.UserRole, filepath)
                style_item(item, n, filepath)
                listbox.addItem(item)
            update_count()

        def restyle_all():
            for i in range(listbox.count()):
                style_item(listbox.item(i), i + 1, self.session_images[i])
            update_count()
            self.update_session_label()

        def sync_order():
            # After a drag-and-drop, the list is the source of truth for the session order
            self.session_images[:] = [listbox.item(i).data(Qt.UserRole) for i in range(listbox.count())]
            restyle_all()
        listbox.orderChanged.connect(sync_order)

        refresh_lb()

        def select_flagged():
            listbox.clearSelection()
            first = None
            for i in range(listbox.count()):
                if self.session_images[i] in self.dup_flags:
                    listbox.item(i).setSelected(True)
                    first = i if first is None else first
            if first is None:
                info(review_win, "No Flags", "No captures are flagged as possible duplicates.")
            else:
                listbox.scrollToItem(listbox.item(first))

        def unflag_selected():
            for idx in self._selected_rows(listbox):
                self.dup_flags.pop(self.session_images[idx], None)
            restyle_all()

        def scan_duplicates():
            # Re-check every neighbouring pair in the current order
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self.dup_flags.clear()
                prev_img, prev_path = None, None
                for path in self.session_images:
                    try:
                        cur = Image.open(path).convert("RGB")
                        cur.thumbnail((960, 960))
                    except Exception:
                        prev_img = None
                        continue
                    if prev_img is not None and looks_like_duplicate(cur, prev_img):
                        self.dup_flags[path] = prev_path
                    prev_img, prev_path = cur, path
            finally:
                QApplication.restoreOverrideCursor()
            restyle_all()
            n = len(self.flagged_in_session())
            info(review_win, "Scan Complete", f"{n} possible duplicate(s) found." if n else "No possible duplicates found.")

        flag_row = QHBoxLayout()
        sel_flag_btn = make_button("Select Flagged", select_flagged, bg="#fff4ce")
        sel_flag_btn.setToolTip("Select every capture flagged as a possible duplicate (then press Delete to remove them)")
        unflag_btn = make_button("Unflag", unflag_selected)
        unflag_btn.setToolTip("Keep the selected captures and clear their duplicate flag")
        scan_btn = make_button("Scan Again", scan_duplicates)
        scan_btn.setToolTip("Re-check the whole queue (in its current order) for near-identical neighbours")
        flag_row.addWidget(sel_flag_btn)
        flag_row.addWidget(unflag_btn)
        flag_row.addWidget(scan_btn)
        left.addLayout(flag_row)

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
                    preview_label.setPixmap(pix.scaled(560, 480, Qt.KeepAspectRatio, Qt.SmoothTransformation))

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
                self.dup_flags.pop(path, None)
            refresh_lb()
            self.update_session_label()
            preview_label.setPixmap(QPixmap())
            preview_label.setText("Select an image to preview")

        btn_grid = QGridLayout()
        btn_grid.addWidget(make_button("Up", lambda: move(lambda i, n: max(i - 1, 0))), 0, 0)
        btn_grid.addWidget(make_button("Down", lambda: move(lambda i, n: min(i + 1, n - 1))), 0, 1)
        btn_grid.addWidget(make_button("Delete", remove_item, fg="red"), 0, 2)
        def confirm_remove():
            n = len(self._selected_rows(listbox))
            if n and ask_yes_no(review_win, "Delete?", f"Delete {n} selected image(s) from disk?"):
                remove_item()
        QShortcut(QKeySequence.Delete, listbox, confirm_remove, context=Qt.WidgetShortcut)
        btn_grid.addWidget(make_button("Top", lambda: move(lambda i, n: 0)), 1, 0)
        btn_grid.addWidget(make_button("Bottom", lambda: move(lambda i, n: n - 1)), 1, 1)
        btn_grid.addWidget(make_button("Reverse", reverse_list), 1, 2)
        left.addLayout(btn_grid)

        def open_redaction_tool():
            rows = self._selected_rows(listbox)
            if not rows:
                info(review_win, "Selection", "Please select at least one image to redact.")
                return
            filepaths = [self.session_images[idx] for idx in rows]
            def after_redaction():
                for fp in filepaths: thumbs.pop(fp, None)
                refresh_lb()
                for idx in rows: listbox.item(idx).setSelected(True)
                update_preview()
                self.update_session_label()
            self.launch_manual_redaction(filepaths, review_win, after_redaction)
        listbox.itemDoubleClicked.connect(lambda _item: open_redaction_tool())

        def copy_to_clipboard():
            rows = self._selected_rows(listbox)
            if not rows: return
            QApplication.clipboard().setPixmap(QPixmap(self.session_images[rows[0]]))
            copy_btn.setText("Copied!")
            QTimer.singleShot(1200, lambda: copy_btn.setText("Copy Image"))

        action_row = QHBoxLayout()
        action_row.addStretch()
        redact_btn = make_button("Open Redaction Tool for Selected Image(s)", open_redaction_tool, bg="#ffd9b3")
        redact_btn.setToolTip("Tip: double-click an image to open it in the Redaction Studio")
        copy_btn = make_button("Copy Image", copy_to_clipboard, bg="#e8eef7")
        copy_btn.setToolTip("Copy the selected image to the clipboard (paste into Word, Teams, etc.)")
        action_row.addWidget(redact_btn)
        action_row.addWidget(copy_btn)
        action_row.addStretch()
        right.addLayout(action_row)

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
        box_rb = QRadioButton("Draw Box"); box_rb.setFont(bold9); box_rb.setChecked(True)
        text_rb = QRadioButton("Add Text"); text_rb.setFont(bold9)
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

        top_bar.addWidget(make_button("Undo Last (Ctrl+Z)", undo_last))
        QShortcut(QKeySequence.Undo, redact_win, undo_last)
        QShortcut(QKeySequence.Save, redact_win, save_redactions)
        top_bar.addWidget(make_button(f"Save to {len(filepaths)} Image(s)", save_redactions, bg="#4CAF50", fg="white", bold=True, point_size=10))

        redact_win.show()

    def show_toast(self, filename, session_index, img=None, flagged=False, title_text=None):
        # Frameless, always-on-top, never takes focus (so auto modes keep sending keys to the slideshow)
        # and hidden from screenshots so it can never end up inside a capture.
        toast = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)
        toast.setAttribute(Qt.WA_ShowWithoutActivating)
        toast.setAttribute(Qt.WA_DeleteOnClose)
        toast.setAttribute(Qt.WA_TranslucentBackground)  # lets the inner panel have rounded corners
        outer = QVBoxLayout(toast)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("toast")
        panel.setStyleSheet("QFrame#toast { background-color: #1f2937; border: 1px solid #374151; border-radius: 10px; }"
                            "QLabel { background: transparent; }")
        outer.addWidget(panel)
        row = QHBoxLayout(panel)
        row.setContentsMargins(10, 10, 16, 10)
        if img is not None:
            thumb = img.copy()
            thumb.thumbnail((112, 63))
            pic = QLabel()
            pic.setPixmap(pil_to_pixmap(thumb))
            row.addWidget(pic)
        col = QVBoxLayout()
        col.setSpacing(2)
        title = QLabel(title_text or (f"Captured #{session_index}" + ("  -  possible duplicate" if flagged else "")))
        f = QFont("Segoe UI", 11); f.setBold(True)
        title.setFont(f)
        title.setStyleSheet("color: #fbbf24;" if flagged else "color: #4ade80;")
        name = QLabel(filename)
        name.setStyleSheet("color: #d1d5db;")
        col.addWidget(title)
        col.addWidget(name)
        row.addLayout(col)
        toast.adjustSize()

        geo = QGuiApplication.primaryScreen().availableGeometry()
        toast.move(geo.right() - toast.width() - 20, geo.bottom() - toast.height() - 20)
        toast.show()
        exclude_from_capture(toast)

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
            self._reset_duplicate_tracking()
            self.counter = 1
            self.current_session_folder = None
            self.update_session_label()
            info(self, "Session Cleared", "All images in the current session have been deleted.")

    def export_to_pdf(self):
        if not self.session_images:
            warn(self, "Empty Session", "You haven't taken any screenshots yet!")
            return
        if not self._confirm_flagged_export():
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
            # Lossless, full-resolution pages written one at a time (low memory, no JPEG blur)
            total = len(self.session_images)
            QApplication.setOverrideCursor(Qt.WaitCursor)
            writer = LosslessPdfWriter(pdf_path)
            try:
                for n, filepath in enumerate(self.session_images, 1):
                    self.pdf_btn.setText(f"Exporting page {n}/{total}…")
                    QApplication.processEvents()
                    writer.add_image_page(filepath, self.pdf_page_size,
                                          footer=f"{n} / {total}" if self.pdf_page_numbers else None)
                writer.close()
            except Exception:
                writer.abort()
                raise
            finally:
                QApplication.restoreOverrideCursor()
                self.pdf_btn.setText("3. Export Session to PDF")

            if self.auto_open_pdf:
                info(self, "Success", f"Saved {len(self.session_images)} images.\n\nFile Location:\n{pdf_path}\n\nOpening PDF now...")
            else:
                info(self, "Success", f"Saved {len(self.session_images)} images to:\n{pdf_path}")

            self.session_images.clear()
            self._reset_duplicate_tracking()
            self.counter = 1
            self.current_session_folder = None
            self.update_session_label()

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

    def _export_name_and_folder(self):
        base_name = self.name_combo.currentText()
        clean_name = base_name.strip("_") if base_name.endswith("_") else base_name
        if not clean_name: clean_name = "Compiled_Document"
        if self.current_session_folder:
            save_folder = self.current_session_folder
        else:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            save_folder = os.path.join(self.folder_entry.text(), f"{clean_name}_Export_{timestamp}")
            os.makedirs(save_folder, exist_ok=True)
        return clean_name, save_folder

    def export_to_markdown(self):
        """Write a step-by-step Markdown document that references the session images."""
        if not self.session_images:
            warn(self, "Empty Session", "You haven't taken any screenshots yet!")
            return
        if not self._confirm_flagged_export():
            return
        clean_name, save_folder = self._export_name_and_folder()
        md_path = os.path.join(save_folder, f"{clean_name}.md")
        if os.path.exists(md_path) and not ask_yes_no(self, "File Already Exists", f"'{os.path.basename(md_path)}' already exists. Overwrite it?"):
            return

        lines = [f"# {clean_name}", "", f"_Generated by Auto-Scapture on {time.strftime('%Y-%m-%d %H:%M')}_", ""]
        for n, img_path in enumerate(self.session_images, 1):
            try:
                ref = os.path.relpath(img_path, save_folder)
            except ValueError:  # different drive
                ref = img_path
            ref = ref.replace("\\", "/")
            lines += [f"## Step {n}", "", f"![Step {n}](<{ref}>)", ""]
        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            error(self, "Error", f"Could not write Markdown file:\n{e}")
            return
        info(self, "Markdown Exported", f"Saved {len(self.session_images)} steps to:\n{md_path}\n\nThe session is kept, so you can still export a PDF.")

    # ---------------- system tray (keeps the screenshot key alive) ----------------
    def update_tray(self):
        if self.shot_prefs.get("run_in_background", False):
            if self.tray is None:
                self.tray = QSystemTrayIcon(self.windowIcon(), self)
                self.tray.setToolTip("Auto-Scapture")
                menu = QMenu()
                for text, slot in [("Snip (area or window)", lambda: self.shots_tab.take("snip", delay=0)),
                                   ("Full Screen", lambda: self.shots_tab.take("screen", delay=0)),
                                   ("Open Auto-Scapture", self.show_from_tray)]:
                    act = QAction(text, menu)
                    act.triggered.connect(slot)
                    menu.addAction(act)
                menu.addSeparator()
                quit_act = QAction("Quit", menu)
                quit_act.triggered.connect(self.quit_app)
                menu.addAction(quit_act)
                self._tray_menu = menu
                self.tray.setContextMenu(menu)
                self.tray.activated.connect(lambda reason: reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) and self.show_from_tray())
            self.tray.show()
        elif self.tray is not None:
            self.tray.hide()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        self._quitting = True
        self.close()
        QApplication.quit()

    def closeEvent(self, event):
        if self.tray is not None and self.tray.isVisible() and not self._quitting:
            # keep running in the tray so the screenshot key still works
            self.save_settings()
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage("Auto-Scapture is still running",
                                      "Your screenshot key keeps working. Right-click the tray icon to quit.",
                                      QSystemTrayIcon.Information, 4000)
            return
        self.shots_tab.shutdown()
        if self.video_tab is not None:
            self.video_tab.shutdown()
        self.save_settings()
        self.is_listening = False
        try: keyboard.unhook_all()
        except Exception: pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    apply_light_theme(app)
    window = ScreenCaptureApp()
    if "--tray" in sys.argv and window.tray is not None:
        pass  # started with Windows: stay in the tray until needed
    else:
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
