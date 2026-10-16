#!/usr/bin/env python3
"""
dymo-linux-service: a stand-in for the DYMO Connect web service on Linux.

DYMO ships its "DYMO Connect" web service (the thing that lets a web page print
to a LabelWriter) for Windows and macOS only. This script speaks the same
HTTPS API on the same port (https://127.0.0.1:41951/DYMO/DLS/Printing/...),
so a web app that prints through DYMO Connect works unchanged on a Linux
workstation. Labels are rendered from DYMO Connect's .dymo XML with Pillow
and printed through CUPS with `lp`.

Endpoints (the subset DYMO's own JS framework uses to print):
  GET  StatusConnected   -> true
  GET  GetPrinters       -> <Printers><LabelWriterPrinter>...</LabelWriterPrinter></Printers>
  POST RenderLabel       -> "<base64 PNG>"   (labelXml, renderParamsXml, printerName)
  POST PrintLabel        -> true             (printerName, labelXml, printParamsXml, labelSetXml)

Supported label objects: TextObject, AddressObject, QRCodeObject. Other
objects (barcodes, images, shapes) are logged and skipped.

Dependencies: Python 3.8+, Pillow, qrcode, CUPS (lp/lpstat/lpoptions), openssl.
  Debian/Ubuntu: sudo apt install cups printer-driver-dymo python3-pil python3-qrcode libnss3-tools

Environment (optional; the systemd unit reads ~/.config/dymo-linux-service/env):
  DYMO_LINUX_PORT       listen port (default 41951; DYMO clients scan 41951-41960)
  DYMO_LINUX_PRINTERS   which CUPS queues to offer: "dymo" (default: name, model or
                        device URI mentions DYMO), "all", or a comma-separated list of
                        queue names
  DYMO_LINUX_MEDIA      CUPS media name to print on (default: matched to the label's
                        size from the queue's PageSize choices, e.g. w162h90 for 30334)
  DYMO_LINUX_ROTATE     extra rotation (0/90/180/270) applied before printing, on top
                        of the 90 degrees a Landscape label gets
  DYMO_LINUX_DPI        print resolution (default 300)
  DYMO_LINUX_FONT       font family to use for every text object (default: whatever
                        fontconfig picks for the family named in the label)
  DYMO_LINUX_DRY_RUN    "1": write the PDF and log the lp command instead of printing
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

VERSION = "0.1.0"
SERVICE_PATH = "/DYMO/DLS/Printing"
DEFAULT_PORT = 41951
DEFAULT_PRINT_DPI = 300
PREVIEW_DPI = 150
# How far (points) a queue's PageSize may differ from the label and still be used.
MEDIA_TOLERANCE_PT = 8
# USB vendor id of every DYMO LabelWriter, for the "is it plugged in" check.
DYMO_USB_VENDOR = "0922"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dymo-linux-service"
UNIT_NAME = "dymo-linux-service.service"
# Nickname of the service certificate in the browsers' NSS stores.
CERT_NICK = "dymo-linux-service"
# Where install.sh copies the script (DYMO_LINUX_HOME overrides it there too).
INSTALL_DIR = Path(os.environ.get("DYMO_LINUX_HOME", Path.home() / ".local" / "share" / "dymo-linux-service"))

log = logging.getLogger("dymo-linux-service")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - reported at startup
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]
try:
    import qrcode
    import qrcode.constants
except ImportError:  # pragma: no cover
    qrcode = None  # type: ignore[assignment]


def check_dependencies() -> None:
    missing = []
    if Image is None:
        missing.append("Pillow (python3-pil)")
    if qrcode is None:
        missing.append("qrcode (python3-qrcode)")
    for tool in ("lp", "lpstat", "lpoptions"):
        if shutil.which(tool) is None:
            missing.append(f"{tool} (cups-client)")
    if missing:
        sys.exit("dymo-linux-service needs: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Label model + .dymo parsing
# ---------------------------------------------------------------------------


@dataclass
class Box:
    x: float  # inches, from the label's top-left
    y: float
    w: float
    h: float


@dataclass
class TextLine:
    text: str
    font: str
    size_pt: float
    bold: bool
    italic: bool


@dataclass
class TextObject:
    name: str
    box: Box
    lines: List[TextLine]
    halign: str  # Left | Center | Right
    valign: str  # Top | Middle | Bottom
    fit: str  # None | ShrinkToFit | AlwaysFit


@dataclass
class QrObject:
    name: str
    box: Box
    data: str
    halign: str
    valign: str


@dataclass
class Label:
    width_in: float
    height_in: float
    landscape: bool
    name: str
    objects: List[object] = field(default_factory=list)


def _text(el: Optional[ET.Element], default: str = "") -> str:
    return (el.text or default) if el is not None and el.text is not None else default


def _float(el: Optional[ET.Element], default: float = 0.0) -> float:
    try:
        return float(_text(el, str(default)))
    except ValueError:
        return default


def _child(el: ET.Element, tag: str) -> Optional[ET.Element]:
    for c in el:
        if c.tag == tag:
            return c
    return None


def _layout(obj: ET.Element) -> Box:
    layout = _child(obj, "ObjectLayout")
    if layout is None:
        return Box(0, 0, 0, 0)
    pt = _child(layout, "DYMOPoint")
    size = _child(layout, "Size")
    return Box(
        _float(pt.find("X") if pt is not None else None),
        _float(pt.find("Y") if pt is not None else None),
        _float(size.find("Width") if size is not None else None),
        _float(size.find("Height") if size is not None else None),
    )


def _parse_text_object(obj: ET.Element) -> TextObject:
    formatted = obj.find("FormattedText")
    lines: List[TextLine] = []
    if formatted is not None:
        for line in formatted.findall("LineTextSpan"):
            spans = line.findall("TextSpan")
            if not spans:
                lines.append(TextLine("", "Helvetica", 10, False, False))
                continue
            info = spans[0].find("FontInfo")
            lines.append(
                TextLine(
                    text="".join(_text(s.find("Text")) for s in spans),
                    font=_text(info.find("FontName") if info is not None else None, "Helvetica"),
                    size_pt=_float(info.find("FontSize") if info is not None else None, 10),
                    bold=_text(info.find("IsBold") if info is not None else None) == "True",
                    italic=_text(info.find("IsItalic") if info is not None else None) == "True",
                )
            )
    fit_el = formatted.find("FitMode") if formatted is not None else None
    return TextObject(
        name=_text(obj.find("Name")),
        box=_layout(obj),
        lines=lines,
        halign=_text(obj.find("HorizontalAlignment"), "Left"),
        valign=_text(obj.find("VerticalAlignment"), "Middle"),
        fit=_text(fit_el if fit_el is not None else obj.find("FitMode"), "None"),
    )


def _parse_qr_object(obj: ET.Element) -> QrObject:
    data = ""
    data_el = _child(obj, "Data")
    if data_el is not None:
        data = _text(data_el.find("DataString"))
    if not data:
        # Per-type holder, e.g. <TextDataHolder><Value>
        for holder in obj:
            if holder.tag.endswith("DataHolder"):
                data = _text(holder.find("Value"))
                if data:
                    break
    return QrObject(
        name=_text(obj.find("Name")),
        box=_layout(obj),
        data=data,
        halign=_text(obj.find("HorizontalAlignment"), "Center"),
        valign=_text(obj.find("VerticalAlignment"), "Middle"),
    )


def parse_label(xml_text: str) -> Label:
    """Parse a DYMO Connect .dymo (<DesktopLabel>) document."""
    xml_text = xml_text.lstrip("﻿")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"label XML is not well-formed: {e}") from e
    if root.tag != "DesktopLabel":
        raise ValueError(
            f"expected a DYMO Connect <DesktopLabel> document, got <{root.tag}> "
            "(legacy DYMO Label v8 .label files are not supported)"
        )
    dymo = root.find("DYMOLabel")
    if dymo is None:
        raise ValueError("label has no <DYMOLabel> element")

    rect = dymo.find("DYMORect")
    pt = rect.find("DYMOPoint") if rect is not None else None
    size = rect.find("Size") if rect is not None else None
    # DYMORect is the printable area inset from the label's edges; the physical
    # label is the area plus the inset on both sides (30334: 0.04+2.17+0.04 = 2.25").
    off_x = _float(pt.find("X") if pt is not None else None)
    off_y = _float(pt.find("Y") if pt is not None else None)
    width = round(2 * off_x + _float(size.find("Width") if size is not None else None), 3)
    height = round(2 * off_y + _float(size.find("Height") if size is not None else None), 3)
    if width <= 0 or height <= 0:
        raise ValueError("label has no usable <DYMORect> size")

    label = Label(
        width_in=width,
        height_in=height,
        landscape=_text(dymo.find("Orientation"), "Portrait") == "Landscape",
        name=_text(dymo.find("LabelName")),
    )
    objects = dymo.find("DynamicLayoutManager/LabelObjects")
    for obj in objects if objects is not None else []:
        if obj.tag in ("TextObject", "AddressObject"):
            label.objects.append(_parse_text_object(obj))
        elif obj.tag == "QRCodeObject":
            label.objects.append(_parse_qr_object(obj))
        else:
            log.warning("skipping unsupported label object <%s> %r", obj.tag, _text(obj.find("Name")))
    return label


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_FALLBACK_FONTS = {
    # (bold, italic) -> candidate files, first found wins
    (False, False): [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
    (True, False): [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    (False, True): [
        "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
    ],
    (True, True): [
        "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
    ],
}


@lru_cache(maxsize=None)
def resolve_font_file(family: str, bold: bool, italic: bool) -> Tuple[Optional[str], int]:
    """(file, face index) for a family + style, via fontconfig when available."""
    family = os.environ.get("DYMO_LINUX_FONT") or family or "Helvetica"
    if shutil.which("fc-match"):
        pattern = family + (":bold" if bold else "") + (":italic" if italic else "")
        try:
            out = subprocess.run(
                ["fc-match", "-f", "%{file}\n%{index}", pattern],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.splitlines()
            if out and out[0] and Path(out[0]).is_file():
                return out[0], int(out[1]) if len(out) > 1 and out[1].isdigit() else 0
        except (subprocess.SubprocessError, ValueError) as e:
            log.warning("fc-match failed for %r: %s", pattern, e)
    for candidate in _FALLBACK_FONTS[(bold, italic)] + _FALLBACK_FONTS[(bold, False)] + _FALLBACK_FONTS[(False, False)]:
        if Path(candidate).is_file():
            return candidate, 0
    return None, 0


@lru_cache(maxsize=256)
def load_font(family: str, bold: bool, italic: bool, size_px: int):
    size_px = max(1, size_px)
    file, index = resolve_font_file(family, bold, italic)
    if file:
        try:
            return ImageFont.truetype(file, size_px, index=index)
        except OSError as e:
            log.warning("could not load font %s: %s", file, e)
    try:
        return ImageFont.load_default(size=size_px)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _measure(lines: List[TextLine], scale: float, dpi: float):
    """Per-line (font, width_px, height_px) at a uniform scale of the designed sizes."""
    out = []
    for line in lines:
        px = max(1, int(round(line.size_pt * dpi / 72 * scale)))
        font = load_font(line.font, line.bold, line.italic, px)
        ascent, descent = font.getmetrics()
        out.append((font, font.getlength(line.text) if line.text else 0.0, ascent + descent))
    return out


def _fit_scale(obj: TextObject, box_w: float, box_h: float, dpi: float) -> float:
    """The scale DYMO's FitMode would apply to the designed font sizes."""
    if obj.fit not in ("AlwaysFit", "ShrinkToFit") or not obj.lines:
        return 1.0

    def fits(scale: float) -> bool:
        m = _measure(obj.lines, scale, dpi)
        return max(w for _, w, _ in m) <= box_w and sum(h for _, _, h in m) <= box_h

    base_px = obj.lines[0].size_pt * dpi / 72
    # Search over the first line's pixel size; AlwaysFit may grow, ShrinkToFit only shrink.
    hi = int(max(base_px, box_h)) if obj.fit == "AlwaysFit" else int(base_px)
    lo = 1
    if fits(hi / base_px):
        return hi / base_px
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid / base_px):
            lo = mid
        else:
            hi = mid - 1
    return lo / base_px


def _draw_text(draw: "ImageDraw.ImageDraw", obj: TextObject, dpi: float) -> None:
    if not any(line.text.strip() for line in obj.lines):
        return
    bx, by = obj.box.x * dpi, obj.box.y * dpi
    bw, bh = obj.box.w * dpi, obj.box.h * dpi
    measured = _measure(obj.lines, _fit_scale(obj, bw, bh, dpi), dpi)
    total_h = sum(h for _, _, h in measured)
    y = by if obj.valign == "Top" else by + bh - total_h if obj.valign == "Bottom" else by + (bh - total_h) / 2
    for line, (font, w, h) in zip(obj.lines, measured):
        x = bx if obj.halign == "Left" else bx + bw - w if obj.halign == "Right" else bx + (bw - w) / 2
        if line.text:
            draw.text((x, y), line.text, font=font, fill=0)
        y += h


def qr_matrix(data: str, border: int = 1) -> List[List[bool]]:
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    return qr.get_matrix()


def _draw_qr(img: "Image.Image", obj: QrObject, dpi: float) -> None:
    if not obj.data:
        return
    matrix = qr_matrix(obj.data)
    n = len(matrix)
    bw, bh = obj.box.w * dpi, obj.box.h * dpi
    module = max(1, int(min(bw, bh) // n))
    side = module * n
    bx, by = obj.box.x * dpi, obj.box.y * dpi
    x0 = bx if obj.halign == "Left" else bx + bw - side if obj.halign == "Right" else bx + (bw - side) / 2
    y0 = by if obj.valign == "Top" else by + bh - side if obj.valign == "Bottom" else by + (bh - side) / 2
    code = Image.new("L", (n, n), 255)
    code.putdata([0 if cell else 255 for row in matrix for cell in row])
    img.paste(code.resize((side, side), Image.NEAREST), (int(round(x0)), int(round(y0))))


def render_label(label: Label, dpi: float) -> "Image.Image":
    """The label as designed (unrotated), white background, grayscale."""
    img = Image.new("L", (int(round(label.width_in * dpi)), int(round(label.height_in * dpi))), 255)
    draw = ImageDraw.Draw(img)
    for obj in label.objects:
        if isinstance(obj, TextObject):
            _draw_text(draw, obj, dpi)
        elif isinstance(obj, QrObject):
            _draw_qr(img, obj, dpi)
    return img


def render_png(label: Label, dpi: float = PREVIEW_DPI) -> bytes:
    buf = io.BytesIO()
    render_label(label, dpi).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Printers (CUPS)
# ---------------------------------------------------------------------------


@dataclass
class Printer:
    name: str  # CUPS queue name; what PrintLabel gets back as printerName
    model: str
    uri: str
    connected: bool


def _run(cmd: List[str], timeout: int = 20) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True).stdout


def _usb_present(uri: str) -> Optional[bool]:
    """Whether the USB device behind a usb:// URI is plugged in (None = can't tell)."""
    if not uri.startswith("usb://"):
        return None
    devices = Path("/sys/bus/usb/devices")
    if not devices.is_dir():
        return None
    m = re.search(r"[?&]serial=([^&]+)", uri)
    wanted_serial = m.group(1) if m else None
    for dev in devices.iterdir():
        try:
            if (dev / "idVendor").read_text().strip().lower() != DYMO_USB_VENDOR:
                continue
            if wanted_serial is None:
                return True
            if (dev / "serial").read_text().strip() == wanted_serial:
                return True
        except OSError:
            continue
    return False


def _make_and_model(name: str) -> str:
    try:
        out = _run(["lpoptions", "-p", name])
    except (subprocess.SubprocessError, OSError):
        return ""
    m = re.search(r"printer-make-and-model='([^']*)'|printer-make-and-model=(\S+)", out)
    return (m.group(1) or m.group(2)) if m else ""


def list_printers() -> List[Printer]:
    uris = {}
    for line in _run(["lpstat", "-v"]).splitlines():
        m = re.match(r"device for (\S+): (.*)", line)
        if m:
            uris[m.group(1)] = m.group(2).strip()
    states = {}
    try:
        for line in _run(["lpstat", "-p"]).splitlines():
            m = re.match(r"printer (\S+) (.*)", line)
            if m:
                states[m.group(1)] = m.group(2)
    except subprocess.CalledProcessError:
        pass  # no printers at all: lpstat -p exits 1

    which = os.environ.get("DYMO_LINUX_PRINTERS", "dymo").strip()
    wanted = None if which.lower() in ("dymo", "all") else {n.strip() for n in which.split(",") if n.strip()}

    printers = []
    for name, uri in uris.items():
        model = _make_and_model(name)
        if wanted is not None:
            if name not in wanted:
                continue
        elif which.lower() == "dymo" and "dymo" not in f"{name} {uri} {model}".lower():
            continue
        state = states.get(name, "")
        connected = not re.search(r"\b(disabled|paused)\b", state, re.I)
        plugged = _usb_present(uri)
        if plugged is False:
            connected = False
        printers.append(Printer(name=name, model=model or name, uri=uri, connected=connected))
    return printers


def printers_xml(printers: List[Printer]) -> str:
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = ["<Printers>"]
    for p in printers:
        parts.append(
            "<LabelWriterPrinter>"
            f"<Name>{esc(p.name)}</Name><ModelName>{esc(p.model)}</ModelName>"
            f"<IsConnected>{'True' if p.connected else 'False'}</IsConnected>"
            "<IsLocal>True</IsLocal><IsTwinTurbo>False</IsTwinTurbo>"
            "</LabelWriterPrinter>"
        )
    parts.append("</Printers>")
    return "".join(parts)


def page_size_choices(printer: str) -> List[str]:
    try:
        out = _run(["lpoptions", "-p", printer, "-l"])
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("lpoptions -l failed for %s: %s", printer, e)
        return []
    for line in out.splitlines():
        if line.startswith("PageSize/") or line.startswith("PageSize:"):
            return [c.lstrip("*") for c in line.split(":", 1)[1].split()]
    return []


def match_media(printer: str, width_pt: float, height_pt: float) -> Optional[str]:
    """The queue's PageSize closest to the label, else a custom size, else None."""
    override = os.environ.get("DYMO_LINUX_MEDIA")
    if override:
        return override
    choices = page_size_choices(printer)
    best, best_err = None, None
    for choice in choices:
        m = re.fullmatch(r"w(\d+)h(\d+)(?:\.\d+)?", choice)
        if not m:
            continue
        dw, dh = abs(int(m.group(1)) - width_pt), abs(int(m.group(2)) - height_pt)
        if dw <= MEDIA_TOLERANCE_PT and dh <= MEDIA_TOLERANCE_PT and (best_err is None or dw + dh < best_err):
            best, best_err = choice, dw + dh
    if best:
        return best
    if any(c.startswith("Custom") for c in choices):
        return f"Custom.{int(round(width_pt))}x{int(round(height_pt))}"
    log.warning(
        "%s has no PageSize near %.0fx%.0fpt; printing on its default media (set DYMO_LINUX_MEDIA to pin one)",
        printer, width_pt, height_pt,
    )
    return None


_print_lock = threading.Lock()


def print_label(printer: str, label: Label, copies: int = 1, title: str = "DYMO label") -> str:
    """Render, rotate for the feed direction, and hand to lp. Returns lp's reply."""
    dpi = float(os.environ.get("DYMO_LINUX_DPI", DEFAULT_PRINT_DPI))
    # Thermal heads are 1-bit: threshold the antialiased render so edges stay crisp.
    img = render_label(label, dpi).point(lambda v: 255 if v > 128 else 0).convert("1")
    rotate = (90 if label.landscape else 0) + int(os.environ.get("DYMO_LINUX_ROTATE", "0") or 0)
    if rotate % 360:
        img = img.rotate(rotate % 360, expand=True, fillcolor=1)

    width_pt, height_pt = img.width / dpi * 72, img.height / dpi * 72
    media = match_media(printer, width_pt, height_pt)

    with _print_lock:
        with tempfile.NamedTemporaryFile(prefix="dymo-label-", suffix=".pdf", delete=False) as tmp:
            img.save(tmp, "PDF", resolution=dpi)
            path = tmp.name
        cmd = ["lp", "-d", printer, "-n", str(max(1, copies)), "-t", title]
        if media:
            cmd += ["-o", f"media={media}"]
        cmd.append(path)
        if os.environ.get("DYMO_LINUX_DRY_RUN") == "1":
            log.info("dry run: %s  (PDF kept at %s)", " ".join(cmd), path)
            return f"dry run: {' '.join(cmd)}"
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if res.returncode != 0:
            raise RuntimeError(f"lp failed ({res.returncode}): {(res.stderr or res.stdout).strip()}")
        return res.stdout.strip()


def copies_from_params(print_params_xml: str) -> int:
    m = re.search(r"<Copies>\s*(\d+)\s*</Copies>", print_params_xml or "")
    return max(1, int(m.group(1))) if m else 1


# ---------------------------------------------------------------------------
# HTTP service
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"dymo-linux-service/{VERSION}"

    def log_message(self, fmt, *args):  # route through logging, one line per request
        log.info("%s %s", self.address_string(), fmt % args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin") or "*"
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # Chrome's private/local network access preflight (both the old and new names).
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Local-Network", "true")
        self.send_header("Access-Control-Max-Age", "600")

    def _reply(self, status: int, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, value, status: int = 200) -> None:
        self._reply(status, json.dumps(value), "application/json; charset=utf-8")

    def _command(self) -> Optional[str]:
        path = urlsplit(self.path).path
        if not path.lower().startswith(SERVICE_PATH.lower() + "/"):
            return None
        return path[len(SERVICE_PATH) + 1 :].strip("/").lower()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        cmd = self._command()
        try:
            if cmd == "statusconnected":
                self._json(True)
            elif cmd == "getprinters":
                self._reply(200, printers_xml(list_printers()), "text/xml; charset=utf-8")
            elif cmd is None and urlsplit(self.path).path == "/":
                self._reply(
                    200,
                    f"dymo-linux-service {VERSION} is running. "
                    f"Service check: {SERVICE_PATH}/StatusConnected\n",
                    "text/plain; charset=utf-8",
                )
            else:
                self._reply(404, f"unknown command {cmd!r}", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001 - every failure must reach the browser
            self._fail(e)

    def _fail(self, e: Exception) -> None:
        # Bad input and lp failures are one line; anything else is a bug and gets its traceback.
        if isinstance(e, (ValueError, RuntimeError)):
            log.error("%s %s failed: %s", self.command, self.path, e)
        else:
            log.exception("%s %s failed", self.command, self.path)
        self._reply(500, f"{type(e).__name__}: {e}", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        cmd = self._command()
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}
        try:
            if cmd == "renderlabel":
                label = parse_label(form.get("labelXml", ""))
                self._json(base64.b64encode(render_png(label)).decode("ascii"))
            elif cmd == "printlabel":
                printer = form.get("printerName", "")
                if not printer:
                    raise ValueError("printerName is required")
                label = parse_label(form.get("labelXml", ""))
                copies = copies_from_params(form.get("printParamsXml", ""))
                reply = print_label(printer, label, copies)
                log.info("printed %s x%d on %s: %s", label.name or "label", copies, printer, reply)
                self._json(True)
            else:
                self._reply(404, f"unknown command {cmd!r}", "text/plain; charset=utf-8")
        except Exception as e:  # noqa: BLE001
            self._fail(e)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Port scans and certificate rejections show up as TLS handshake errors;
        # one line each, not a traceback.
        err = sys.exc_info()[1]
        if isinstance(err, (ssl.SSLError, ConnectionError, TimeoutError)):
            log.debug("connection from %s dropped: %s", client_address[0], err)
        else:
            super().handle_error(request, client_address)


# ---------------------------------------------------------------------------
# Certificate + trust
# ---------------------------------------------------------------------------


def cert_paths() -> Tuple[Path, Path]:
    return CONFIG_DIR / "cert.pem", CONFIG_DIR / "key.pem"


def ensure_cert(regenerate: bool = False) -> Tuple[Path, Path]:
    """A self-signed certificate for localhost / 127.0.0.1, created on first use."""
    cert, key = cert_paths()
    if cert.is_file() and key.is_file() and not regenerate:
        return cert, key
    if shutil.which("openssl") is None:
        sys.exit("openssl is needed to create the service certificate (sudo apt install openssl)")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256",
            "-keyout", str(key), "-out", str(cert), "-days", "3650",
            "-subj", "/CN=localhost/O=dymo-linux-service",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
            "-addext", "extendedKeyUsage=serverAuth",
        ],
        check=True,
        capture_output=True,
    )
    os.chmod(key, 0o600)
    log.info("created certificate %s", cert)
    return cert, key


def nss_databases() -> List[Path]:
    """NSS cert stores Chrome/Chromium (and snap/flatpak builds) read on Linux."""
    home = Path.home()
    candidates = [
        home / ".pki" / "nssdb",
        home / "snap" / "chromium" / "current" / ".pki" / "nssdb",
        home / ".var" / "app" / "com.google.Chrome" / ".pki" / "nssdb",
        home / ".var" / "app" / "org.chromium.Chromium" / ".pki" / "nssdb",
        home / ".var" / "app" / "com.brave.Browser" / ".pki" / "nssdb",
    ]
    return [c for c in candidates if c.is_dir()]


def trust_cert(cert: Path) -> int:
    """Import the certificate into every Chrome NSS store found; returns how many."""
    if shutil.which("certutil") is None:
        print(
            "certutil not found (sudo apt install libnss3-tools), so the certificate was not "
            f"added to Chrome. Either install it and run `{sys.argv[0]} trust`, or open "
            f"https://127.0.0.1:{DEFAULT_PORT}{SERVICE_PATH}/StatusConnected in the browser and accept the warning.",
            file=sys.stderr,
        )
        return 0
    dbs = nss_databases()
    if not dbs:
        db = Path.home() / ".pki" / "nssdb"
        db.mkdir(parents=True, exist_ok=True)
        subprocess.run(["certutil", "-d", f"sql:{db}", "-N", "--empty-password"], check=True)
        dbs = [db]
    for db in dbs:
        subprocess.run(["certutil", "-d", f"sql:{db}", "-D", "-n", CERT_NICK], capture_output=True)
        subprocess.run(["certutil", "-d", f"sql:{db}", "-A", "-t", "C,,", "-n", CERT_NICK, "-i", str(cert)], check=True)
        print(f"trusted {cert} in {db}")
    return len(dbs)


def untrust_cert() -> None:
    """Remove the certificate from every Chrome NSS store that has it."""
    if shutil.which("certutil") is None:
        print("certutil not found; remove the dymo-linux-service certificate from the browser by hand", file=sys.stderr)
        return
    for db in nss_databases():
        r = subprocess.run(["certutil", "-d", f"sql:{db}", "-D", "-n", CERT_NICK], capture_output=True)
        if r.returncode == 0:
            print(f"removed the certificate from {db}")


# ---------------------------------------------------------------------------
# systemd user unit
# ---------------------------------------------------------------------------


def install_unit(port: int) -> Path:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / UNIT_NAME
    script = Path(__file__).resolve()
    python = shutil.which("python3") or sys.executable
    env_file = CONFIG_DIR / "env"
    unit.write_text(
        "[Unit]\n"
        "Description=DYMO Connect web service stand-in (label printing for web apps)\n"
        "After=network.target\n\n"
        "[Service]\n"
        f"ExecStart={python} {script} serve --port {port}\n"
        f"EnvironmentFile=-{env_file}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    if not env_file.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        env_file.write_text(
            "# Overrides for dymo-linux-service; see the README. Restart after editing:\n"
            f"#   systemctl --user restart {UNIT_NAME}\n"
            "#DYMO_LINUX_PRINTERS=dymo\n"
            "#DYMO_LINUX_MEDIA=w162h90\n"
            "#DYMO_LINUX_ROTATE=0\n"
        )
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT_NAME], check=True)
    else:
        print("systemctl not found; start the service by hand with: " f"{python} {script} serve", file=sys.stderr)
    return unit


def remove_unit() -> None:
    unit = Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME], capture_output=True)
    if unit.exists():
        unit.unlink()
        print(f"removed {unit}")
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_serve(args) -> None:
    check_dependencies()
    cert, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    server = Server(("127.0.0.1", args.port), Handler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    log.info("dymo-linux-service %s listening on https://127.0.0.1:%d%s", VERSION, args.port, SERVICE_PATH)
    try:
        printers = list_printers()
        log.info("offering %d printer(s): %s", len(printers), ", ".join(p.name for p in printers) or "none")
    except Exception as e:  # noqa: BLE001
        log.warning("could not list printers at startup: %s", e)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_install(args) -> None:
    check_dependencies()
    cert, _ = ensure_cert()
    trust_cert(cert)
    unit = install_unit(args.port)
    print(f"installed {unit}; the service starts with your login.")
    print(f"Check it: https://127.0.0.1:{args.port}{SERVICE_PATH}/StatusConnected should show true.")
    print("Restart the browser once so it picks up the trusted certificate.")


def cmd_uninstall(args) -> None:
    remove_unit()
    untrust_cert()
    if args.keep_config:
        print(f"kept {CONFIG_DIR}")
    elif CONFIG_DIR.is_dir():
        shutil.rmtree(CONFIG_DIR)
        print(f"removed {CONFIG_DIR}")
    # The copy install.sh made, and its ~/.local/bin link. A git checkout
    # elsewhere is left alone.
    installed = INSTALL_DIR / "dymo-linux-service.py"
    link = Path.home() / ".local" / "bin" / "dymo-linux-service"
    if link.is_symlink() and link.resolve() == installed.resolve():
        link.unlink()
        print(f"removed {link}")
    if installed.is_file():
        shutil.rmtree(INSTALL_DIR)
        print(f"removed {INSTALL_DIR}")
    print("Uninstalled. Restart the browser; apt packages (cups, printer-driver-dymo, ...) were left installed.")
    print("If you imported the certificate into Firefox, delete it there: Settings > Certificates > Authorities.")


def cmd_trust(args) -> None:
    cert, _ = ensure_cert(regenerate=args.regenerate)
    trust_cert(cert)


def cmd_printers(args) -> None:
    check_dependencies()
    printers = list_printers()
    if not printers:
        print("no printers offered (is the LabelWriter added in CUPS? set DYMO_LINUX_PRINTERS=all to offer every queue)")
    for p in printers:
        print(f"{p.name:32} {p.model:32} {'connected' if p.connected else 'offline':10} {p.uri}")


def cmd_render(args) -> None:
    check_dependencies()
    label = parse_label(Path(args.label).read_text(encoding="utf-8"))
    render_label(label, args.dpi).save(args.out)
    print(f"{args.out}: {label.width_in}x{label.height_in} in at {args.dpi} dpi, {len(label.objects)} object(s)")


def cmd_print(args) -> None:
    check_dependencies()
    label = parse_label(Path(args.label).read_text(encoding="utf-8"))
    printer = args.printer
    if not printer:
        connected = [p for p in list_printers() if p.connected] or list_printers()
        if not connected:
            sys.exit("no printer found; pass --printer")
        printer = connected[0].name
    print(print_label(printer, label, args.copies))


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="DYMO Connect web service stand-in for Linux")
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("serve", help="run the service in the foreground (default)")
    p.add_argument("--port", type=int, default=int(os.environ.get("DYMO_LINUX_PORT", DEFAULT_PORT)))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("install", help="create the certificate, trust it in Chrome, install + start the systemd user service")
    p.add_argument("--port", type=int, default=int(os.environ.get("DYMO_LINUX_PORT", DEFAULT_PORT)))
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="stop and remove the systemd user service, the certificate and the installed copy")
    p.add_argument("--keep-config", action="store_true", help=f"keep {CONFIG_DIR} (certificate and env file)")
    p.set_defaults(func=cmd_uninstall)

    p = sub.add_parser("trust", help="(re)import the service certificate into Chrome's certificate store")
    p.add_argument("--regenerate", action="store_true", help="make a new certificate first")
    p.set_defaults(func=cmd_trust)

    p = sub.add_parser("printers", help="list the printers the service would offer")
    p.set_defaults(func=cmd_printers)

    p = sub.add_parser("render", help="render a .dymo file to a PNG, to check the layout")
    p.add_argument("label")
    p.add_argument("out")
    p.add_argument("--dpi", type=float, default=DEFAULT_PRINT_DPI)
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("print", help="print a .dymo file")
    p.add_argument("label")
    p.add_argument("--printer", default="")
    p.add_argument("--copies", type=int, default=1)
    p.set_defaults(func=cmd_print)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    if args.command is None:
        args.port = int(os.environ.get("DYMO_LINUX_PORT", DEFAULT_PORT))
        cmd_serve(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
