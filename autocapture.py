import sys
import os
import json
import threading
import time
import random
import re # Added for the smart renamer context peek
import ctypes
import io
import struct
import zlib

import keyboard
from PIL import ImageGrab, Image, ImageChops, ImageStat, ImageDraw, ImageFont

from PySide6.QtCore import Qt, QObject, QTimer, Signal, QRect, QPoint, QSize
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QIcon, QKeySequence, QPainter, QPalette, QPen, QShortcut, QPixmap, QImage, QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QWidget, QDialog, QTabWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QCheckBox, QRadioButton, QButtonGroup, QLineEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QListWidget, QListWidgetItem, QAbstractItemView, QFrame,
    QPlainTextEdit, QScrollArea, QStackedWidget, QMessageBox, QInputDialog,
    QFileDialog, QColorDialog,
)

APP_TITLE = "Auto-Scapture Test Build (1.6.2)"


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

        # ==========================================
        # --- TABBED INTERFACE ---
        # ==========================================
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.notebook = QTabWidget()
        outer.addWidget(self.notebook)

        self.cap_tab = QWidget()
        self.rename_tab = QWidget()

        self.notebook.addTab(self.cap_tab, "Auto-Scapture")
        self.notebook.addTab(self.rename_tab, "Lab Renamer")

        self.setup_capture_ui()
        self.setup_renamer_ui()

        self.setAcceptDrops(True)
        self.resize(520, self.sizeHint().height())

        icon_file = resource_path(os.path.join("assets", "icon.png"))
        if os.path.exists(icon_file):
            self.setWindowIcon(QIcon(icon_file))

        if prefs.get("pinned", False):
            self.pin_btn.setChecked(True)
        self.apply_capture_exclusion()

    def apply_capture_exclusion(self):
        exclude_from_capture(self, self.hide_from_capture)

    def ui_call(self, fn):
        """Thread-safe: schedule fn on the GUI thread."""
        self.bridge.call.emit(fn)

    # ==========================================
    # --- TAB 1: EXACT 1.1.1 CAPTURE UI ---
    # ==========================================
    def setup_capture_ui(self):
        prefs = self.settings_data.get("preferences", {})
        lay = QVBoxLayout(self.cap_tab)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(4)
        center = Qt.AlignHCenter

        top_frame = QHBoxLayout()
        self.pin_btn = QCheckBox("Pin Window")
        self.pin_btn.toggled.connect(self.toggle_pin)
        top_frame.addWidget(self.pin_btn)
        top_frame.addStretch()
        self.settings_btn = make_button("Preferences", self.open_settings_window)
        top_frame.addWidget(self.settings_btn)
        lay.addLayout(top_frame)

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
        lay.addLayout(header)

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
        lay.addWidget(setup_box)

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
        lay.addWidget(area_box)

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
        lay.addWidget(mode_frame)

        lay.addSpacing(6)
        self.start_btn = make_button("2. Start Capture Sequence", self.toggle_listening, bg=PRIMARY, fg="white", bold=True, point_size=10)
        self.start_btn.setMinimumHeight(38)
        self.start_btn.setEnabled(False)
        lay.addWidget(self.start_btn)

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
        lay.addWidget(session_box)
        lay.addStretch()

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
        pref_checkbox("Hide Auto-Scapture from its own screenshots", "hide_from_capture", 0, 1, self.apply_capture_exclusion)
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
        self.status_label.setText(text)
        if has_area:
            self.status_label.setStyleSheet("background: #e6f4ea; color: #1e7e34; border-radius: 10px; padding: 3px 12px;")
        else:
            self.status_label.setStyleSheet("background: #eef1f5; color: #5b6573; border-radius: 10px; padding: 3px 12px;")
        self.show_area_btn.setEnabled(has_area)
        self.capture_now_btn.setEnabled(has_area and not (self.is_listening and self.capture_mode() != "Manual"))

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
            return grab_region(self.capture_region)
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
        if self.notebook.currentIndex() == 1:
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
        self.session_label.setText(f"Images in current session: {n}")
        flagged = len(self.flagged_in_session())
        self.flag_label.setText(f"{flagged} possible duplicate(s) flagged - check Review / Redact")
        self.flag_label.setVisible(flagged > 0)
        pix = QPixmap(self.session_images[-1]) if n else QPixmap()
        if pix.isNull():
            self.last_thumb.setPixmap(QPixmap())
            self.last_thumb.setText("No captures yet")
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

    def show_toast(self, filename, session_index, img=None, flagged=False):
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
        title = QLabel(f"Captured #{session_index}" + ("  -  possible duplicate" if flagged else ""))
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

    def closeEvent(self, event):
        self.save_settings()
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
