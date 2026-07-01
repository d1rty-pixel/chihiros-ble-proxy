#!/usr/bin/env python3
"""
Live BLE scanner + sniffer for Chihiros aquarium devices.

Scan mode:   ↑/↓ navigate · Enter open sniffer · A show-all · C clear stale · Q quit
Sniff mode:  ↑/↓ scroll · ESC back to scanner · Q quit

Requires: sudo pacman -S python-bleak
"""

import asyncio
import curses
import time
import argparse
import sys
from dataclasses import dataclass, field

try:
    from bleak import BleakScanner, BleakClient
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    print("bleak not installed.  Run: sudo pacman -S python-bleak", file=sys.stderr)
    sys.exit(1)


# ── Chihiros protocol ─────────────────────────────────────────────────────────

NUS_RX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # device → us (notify)
NUS_TX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # us → device (write)

CHAR_LABELS = {
    "6e400001-b5a3-f393-e0a9-e50e24dcca9e": "NUS svc",
    NUS_TX:                                  "NUS TX ",
    NUS_RX:                                  "NUS RX ",
}

_CMD = {
    0x04: "AUTH",        0x09: "RTC",         0x05: "MODE",
    0x16: "CO2_SCHEMA",  0x07: "BRIGHT/SPD",  0x01: "SETTINGS",
    0x19: "SCHEDULE",    0x14: "STIR_TOGGLE", 0x15: "STIR_TIMER",
    0x1b: "STIR_SPEED",  0x20: "STIR_ENABLE", 0x1f: "STIR_APPLY",
    0x2a: "STIR_SCHEMA", 0x21: "TEMP_THRESH",
}
_AUTH = {0x01: "base", 0x06: "ext1(fan)", 0x08: "ext2(fan)",
         0x04: "dose1", 0x05: "dose2"}
_MODE = {0x07: "reset_schema", 0x12: "reset_auto",
         0x22: "silent_on",    0x23: "silent_off"}
_CO2V = {0x64: "ON", 0x00: "OFF", 0x6f: "EMPTY"}
_DOW  = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
_RGB  = {0: "R", 1: "G", 2: "B"}


def _crc_ok(data: bytes) -> bool:
    if len(data) < 3:
        return False
    crc = 0
    for b in data[1:-1]:
        crc ^= b
    return crc == data[-1]


def decode_chihiros(data: bytes) -> str | None:
    """Decode a Chihiros NUS frame; returns None if not a valid Chihiros frame."""
    if len(data) < 7 or data[0] not in (0x5a, 0xa5):
        return None
    hdr  = "BASE" if data[0] == 0x5a else "DEV"
    seq  = data[4]
    cmd  = data[5]
    pl   = data[6:-1]   # payload (between cmd and CRC)
    flag = "" if _crc_ok(data) else " ⚠CRC"
    name = _CMD.get(cmd, f"0x{cmd:02x}")
    det  = ""

    if cmd == 0x04 and pl:                      # AUTH
        det = _AUTH.get(pl[0], f"0x{pl[0]:02x}")
    elif cmd == 0x09 and len(pl) >= 6:          # RTC
        y, mo, wd, h, m, s = pl[:6]
        det = f"20{y:02d}-{mo:02d} {_DOW.get(wd,'?')} {h:02d}:{m:02d}:{s:02d}"
    elif cmd == 0x05 and pl:                    # MODE
        det = _MODE.get(pl[0], f"0x{pl[0]:02x}")
    elif cmd == 0x16 and len(pl) >= 3:          # CO2_SCHEMA
        h, m, v = pl[:3]
        det = f"{h:02d}:{m:02d} {_CO2V.get(v, f'0x{v:02x}')}"
    elif cmd == 0x07 and len(pl) >= 2:          # BRIGHTNESS / FAN_SPEED
        ch, val = pl[0], pl[1]
        det = f"fan speed={val}" if ch == 0xff else f"ch={_RGB.get(ch,ch)} {val}%"
    elif cmd == 0x19 and len(pl) >= 9:          # WRGB2 SCHEDULE
        on_h, on_m, off_h, off_m, ramp, wd, r, g, b = pl[:9]
        det = f"{on_h:02d}:{on_m:02d}→{off_h:02d}:{off_m:02d} ramp={ramp}m R={r} G={g} B={b}"
    elif cmd == 0x21 and len(pl) >= 2:          # TEMP_THRESH (fan)
        det = f"start={pl[0]}°C max={pl[1]}°C"
    elif cmd == 0x2a and len(pl) >= 4:          # STIR_SCHEMA
        ch, _, vl, sp = pl[:4]
        det = f"ch={ch} lead={vl}s speed={sp}"
    elif cmd == 0x15 and len(pl) >= 6:          # STIR_TIMER
        ch, _, h, m, _, dur = pl[:6]
        det = f"ch={ch} {h:02d}:{m:02d} dur={dur}s"
    elif cmd == 0x20 and len(pl) >= 3:          # STIR_ENABLE / DOSE_ENABLE
        ch, _, en = pl[:3]
        det = f"ch={ch} {'on' if en else 'off'}"
    elif cmd == 0x14:                           # STIR_TOGGLE / stir_restore
        on_bits = [i for i, b in enumerate(pl) if b == 0x01]
        if on_bits:
            det = "on=" + ",".join(str(b) for b in on_bits)

    return f"{hdr} #{seq} {name}" + (f"  {det}" if det else "") + flag


def decode_fan_rx(data: bytes) -> str | None:
    """Decode a ventilator notification frame."""
    if len(data) >= 13 and data[4] == 0x01:
        spd  = data[5]
        rtemp = ((data[6] << 8) | data[7]) / 256.0
        wtemp = ((data[10] << 8) | data[11]) / 10.0
        hum  = data[12]
        return f"fan={spd}%  room={rtemp:.1f}°C  water={wtemp:.1f}°C  hum={hum}%"
    return None


# ── Device classification ─────────────────────────────────────────────────────

DEVICE_TYPES: dict[str, tuple[str, str]] = {
    "DYNT90": ("WRGB2 light",      "light_mac"),
    "DYSIL":  ("WRGB2 light",      "light_mac"),
    "DYPCO2": ("CO2 controller",   "co2_mac"),
    "DYMIX":  ("Magnetic stirrer", "stirrer_mac"),
    "DYNFAN": ("Cooling fan",      "fan_mac"),
    "DYNDOC": ("Doctor Mate",      "doctor_mac"),
    "DYDOSE": ("Dosing pump",      "dosing_mac"),
}

SPINNER = "◐◓◑◒"

# colour pair indices
C_HEADER  = 1   # cyan        — table header / dividers
C_KNOWN   = 2   # green       — identified Chihiros device
C_UNKNOWN = 3   # yellow      — unknown DY device
C_MUTED   = 4   # white dim   — non-Chihiros
C_BAR     = 5   # black/cyan  — status bar
C_TITLE   = 6   # white bold  — title
C_RX      = 7   # green       — RX packet
C_CONN    = 8   # cyan        — connection events

MODE_SCAN  = "scan"
MODE_SNIFF = "sniff"


def classify(name: str) -> tuple[str, str] | None:
    if not name:
        return None
    for prefix, info in DEVICE_TYPES.items():
        if name.startswith(prefix):
            return info
    return ("Unknown Chihiros", "?") if name.startswith("DY") else None


def should_autoconnect(entry: Entry) -> bool:
    """Direct BLE sniff only makes sense for devices that push continuous notifications."""
    return entry.info is None or entry.info[1] in ("fan_mac", "?")


def signal_bar(rssi: int, w: int = 5) -> str:
    filled = max(0, min(w, round((rssi + 100) * w / 60)))
    return "█" * filled + "░" * (w - filled)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class Entry:
    mac: str
    name: str
    rssi: int
    info: tuple[str, str] | None
    first_seen: float = field(default_factory=time.monotonic)
    last_seen:  float = field(default_factory=time.monotonic)
    packets: int = 0


@dataclass
class Packet:
    t: float           # seconds since sniffer start
    char_uuid: str
    raw: bytes
    decoded: str | None


# ── App state ─────────────────────────────────────────────────────────────────

class App:
    def __init__(self, show_all: bool) -> None:
        self.show_all      = show_all
        self.devices:  dict[str, Entry]  = {}
        self.start     = time.monotonic()
        self.done      = False

        # scan-mode navigation
        self.selected_mac: str | None = None

        # sniff-mode state
        self.mode          = MODE_SCAN
        self.sniff_target: Entry | None = None
        self.sniff_packets: list[Packet] = []
        self.sniff_scroll  = 0       # rows scrolled back from tail (0 = follow)
        self.sniff_status  = ""
        self.sniff_start   = 0.0

    # ── BLE scanner callback ──────────────────────────────────────────────────

    def on_adv(self, device: BLEDevice, adv: AdvertisementData) -> None:
        name = device.name or adv.local_name or ""
        mac  = device.address.upper()
        rssi = adv.rssi if adv.rssi is not None else -99
        info = classify(name)
        if not self.show_all and info is None:
            return
        now = time.monotonic()
        if mac in self.devices:
            e = self.devices[mac]
            e.rssi     = rssi
            e.last_seen = now
            e.packets  += 1
            if name and not e.name:
                e.name = name
        else:
            self.devices[mac] = Entry(mac=mac, name=name, rssi=rssi, info=info)
            if self.selected_mac is None:
                self.selected_mac = mac

    # ── Sniffer packet callback ───────────────────────────────────────────────

    def on_rx(self, char_uuid: str, data: bytes) -> None:
        decoded = decode_chihiros(data)
        if decoded is None:
            decoded = decode_fan_rx(data)
        self.sniff_packets.append(Packet(
            t        = time.monotonic() - self.sniff_start,
            char_uuid= char_uuid.lower(),
            raw      = data,
            decoded  = decoded,
        ))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def sorted_devices(self) -> list[Entry]:
        # Sort by first-seen time (stable) so the list doesn't jump on RSSI updates.
        return sorted(self.devices.values(), key=lambda e: e.first_seen)

    def selected_entry(self) -> Entry | None:
        devices = self.sorted_devices()
        if not devices:
            return None
        if self.selected_mac:
            for e in devices:
                if e.mac == self.selected_mac:
                    return e
        self.selected_mac = devices[0].mac
        return devices[0]

    def _selected_index(self) -> int:
        devices = self.sorted_devices()
        for i, e in enumerate(devices):
            if e.mac == self.selected_mac:
                return i
        return 0

    def move_selection(self, delta: int) -> None:
        devices = self.sorted_devices()
        if not devices:
            return
        idx = (self._selected_index() + delta) % len(devices)
        self.selected_mac = devices[idx].mac

    def clear_stale(self, age: float = 30.0) -> None:
        cutoff = time.monotonic() - age
        self.devices = {k: v for k, v in self.devices.items()
                        if v.last_seen > cutoff}

    def elapsed_str(self) -> str:
        s = int(time.monotonic() - self.start)
        return f"{s // 60}:{s % 60:02d}"


# ── Sniff connection task ─────────────────────────────────────────────────────

async def sniff_connect(app: App) -> None:
    mac = app.sniff_target.mac
    app.sniff_status = "connecting…"
    app.sniff_start  = time.monotonic()
    try:
        async with BleakClient(mac, timeout=10.0) as client:
            app.sniff_status = "connected"

            for svc in client.services:
                for char in svc.characteristics:
                    if "notify" in char.properties or "indicate" in char.properties:
                        uuid = char.uuid
                        def make_cb(u):
                            def cb(_, data):
                                app.on_rx(u, bytes(data))
                            return cb
                        try:
                            await client.start_notify(char.uuid, make_cb(uuid))
                        except Exception:
                            pass   # some chars may reject subscribe

            while app.mode == MODE_SNIFF and not app.done:
                await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        pass
    except Exception as exc:
        app.sniff_status = f"error: {exc}"


# ── Drawing ───────────────────────────────────────────────────────────────────

def draw(stdscr: curses.window, app: App) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.erase()

    def put(row: int, col: int, text: str, attr: int = 0) -> None:
        if row < 0 or row >= height or col >= width:
            return
        try:
            stdscr.addstr(row, col, text[: width - col], attr)
        except curses.error:
            pass

    def hline(row: int, attr: int = 0) -> None:
        put(row, 0, "─" * (width - 1), attr)

    tick  = SPINNER[int(time.monotonic() * 3) % len(SPINNER)]
    right = f"{tick} Scanning  {app.elapsed_str()}"

    # ── Shared column layout for device rows ──────────────────────────────────
    W_RSSI, W_SIG, W_MAC, W_VAR = 5, 5, 17, 11
    GAP   = "  "
    flex  = max(0, width - W_RSSI - W_SIG - W_MAC - W_VAR - len(GAP) * 4)
    W_NAME = min(24, max(8, flex // 2))
    W_TYPE = max(0, flex - W_NAME)

    def device_row(e: Entry) -> str:
        rssi_s = f"{e.rssi:+d}" if e.rssi != -99 else "  ?"
        bar    = signal_bar(e.rssi, W_SIG)
        name   = (e.name or "(no name)")
        type_s = e.info[0] if e.info else ""
        var_s  = e.info[1] if e.info else ""
        return (f"{rssi_s:>{W_RSSI}}{GAP}{bar:<{W_SIG}}{GAP}"
                f"{e.mac:<{W_MAC}}{GAP}"
                f"{name[:W_NAME]:<{W_NAME}}{GAP}"
                f"{type_s[:W_TYPE]:<{W_TYPE}}{GAP}"
                f"{var_s[:W_VAR]:<{W_VAR}}")

    def device_attr(e: Entry, selected: bool = False, now: float = 0.0) -> int:
        stale = now and (now - e.last_seen) > 8.0
        if selected:
            return curses.A_REVERSE
        if stale:
            return curses.A_DIM
        if e.info is None:
            return curses.color_pair(C_MUTED)
        if e.info[1] == "?":
            return curses.color_pair(C_UNKNOWN)
        return curses.color_pair(C_KNOWN) | curses.A_BOLD

    def dev_table_header(row: int) -> None:
        h = (f"{'RSSI':>{W_RSSI}}{GAP}{'Sig':<{W_SIG}}{GAP}"
             f"{'MAC':<{W_MAC}}{GAP}{'Name':<{W_NAME}}{GAP}"
             f"{'Type':<{W_TYPE}}{GAP}{'YAML var':<{W_VAR}}")
        put(row, 0, h, curses.color_pair(C_HEADER) | curses.A_BOLD)

    # ── Title ─────────────────────────────────────────────────────────────────
    put(0, 0, "Chihiros BLE Scanner",
        curses.color_pair(C_TITLE) | curses.A_BOLD)
    put(0, width - len(right) - 1, right,
        curses.color_pair(C_KNOWN) | curses.A_BOLD)

    # ══════════════════════════════════════════════════════════════════════════
    if app.mode == MODE_SCAN:
        _draw_scan(stdscr, app, height, width, put, hline,
                   dev_table_header, device_row, device_attr)
    else:
        _draw_sniff(stdscr, app, height, width, put, hline,
                    dev_table_header, device_row, device_attr)

    stdscr.refresh()


def _draw_scan(stdscr, app, height, width, put, hline,
               dev_table_header, device_row, device_attr):
    # ── Table header ──────────────────────────────────────────────────────────
    dev_table_header(2)
    hline(3, curses.color_pair(C_HEADER))

    # ── Rows ──────────────────────────────────────────────────────────────────
    table_top = 4
    table_bot = height - 2
    max_rows  = table_bot - table_top

    devices   = app.sorted_devices()
    sel_entry = app.selected_entry()
    now       = time.monotonic()

    for i, e in enumerate(devices):
        if i >= max_rows:
            break
        selected = sel_entry is not None and e.mac == sel_entry.mac
        line     = device_row(e)
        attr     = device_attr(e, selected=selected, now=now)
        # fill full row width when selected so highlight bar spans the line
        if selected:
            line = line.ljust(width - 1)
        put(table_top + i, 0, line, attr)

    overflow = len(devices) - max_rows
    if overflow > 0:
        put(table_bot - 1, 2,
            f"  … {overflow} more (resize terminal)",
            curses.color_pair(C_MUTED))

    # ── Status bar ────────────────────────────────────────────────────────────
    known_n = sum(1 for e in devices if e.info is not None)
    total_n = len(devices)
    left = (f" {total_n} device(s) · {known_n} Chihiros"
            if app.show_all else f" {known_n} Chihiros device(s)")

    if app.show_all:
        keys = " ↑↓ navigate · Enter sniff · A Chihiros-only · C clear · Q quit "
    else:
        keys = " ↑↓ navigate · Enter sniff · A show-all · C clear · Q quit "

    bar_row = height - 1
    try:
        stdscr.addstr(bar_row, 0, " " * (width - 1), curses.color_pair(C_BAR))
        put(bar_row, 0, left, curses.color_pair(C_BAR) | curses.A_BOLD)
        put(bar_row, width - len(keys) - 1, keys, curses.color_pair(C_BAR))
    except curses.error:
        pass


def _draw_sniff(stdscr, app, height, width, put, hline,
                dev_table_header, device_row, device_attr):
    # ── Selected device (1 row + header) ─────────────────────────────────────
    dev_table_header(2)
    hline(3, curses.color_pair(C_HEADER))
    if app.sniff_target:
        line = device_row(app.sniff_target).ljust(width - 1)
        put(4, 0, line, curses.A_REVERSE)

    # ── Sniffer section header ────────────────────────────────────────────────
    hline(5, curses.color_pair(C_HEADER))
    name_str = app.sniff_target.name if app.sniff_target else ""
    mac_str  = app.sniff_target.mac  if app.sniff_target else ""
    sniff_title = f" BLE Sniffer — {mac_str}  {name_str}"
    conn_status = f" {app.sniff_status} "
    put(6, 0, sniff_title, curses.color_pair(C_CONN) | curses.A_BOLD)
    if app.sniff_status in ("passive", "released"):
        dot = "○"
        status_attr = curses.color_pair(C_MUTED) | curses.A_DIM
    elif "connected" in app.sniff_status and "error" not in app.sniff_status:
        dot = "◉"
        status_attr = curses.color_pair(C_KNOWN) | curses.A_BOLD
    else:
        dot = "○"
        status_attr = curses.color_pair(C_UNKNOWN)
    put(6, width - len(conn_status) - 2, f"{dot}{conn_status}", status_attr)

    # ── Packet table header ───────────────────────────────────────────────────
    W_TIME, W_CHAR = 8, 7
    GAP = "  "
    flex = max(0, width - W_TIME - W_CHAR - len(GAP) * 2)
    W_RAW     = min(32, max(10, flex // 2))
    W_DECODED = max(0, flex - W_RAW)

    def pkt_header_row(row: int) -> None:
        h = (f"{'Time':>{W_TIME}}{GAP}{'Char':<{W_CHAR}}{GAP}"
             f"{'Raw hex':<{W_RAW}}{GAP}{'Decoded':<{W_DECODED}}")
        put(row, 0, h, curses.color_pair(C_HEADER) | curses.A_BOLD)

    pkt_header_row(7)
    hline(8, curses.color_pair(C_HEADER))

    # ── Packet rows ───────────────────────────────────────────────────────────
    pkt_top = 9
    pkt_bot = height - 2
    max_pkt_rows = max(0, pkt_bot - pkt_top)

    pkts = app.sniff_packets
    total_pkts = len(pkts)

    # Clamp scroll so it never exceeds available packets
    app.sniff_scroll = max(0, min(app.sniff_scroll, max(0, total_pkts - max_pkt_rows)))

    # Passive mode: no connection attempted, show help text
    if app.sniff_status == "passive" and not app.sniff_packets:
        put(pkt_top,     4, "Dieses Gerät wird vom ESP32 verwaltet.",        curses.color_pair(C_MUTED))
        put(pkt_top + 1, 4, "Protokollverkehr sichtbar mit:",                curses.color_pair(C_MUTED))
        put(pkt_top + 2, 6, "esphome logs chihiros-ble-proxy.yaml",          curses.color_pair(C_UNKNOWN) | curses.A_BOLD)
        put(pkt_top + 4, 4, "Space → direkt verbinden (sperrt Smartphone)",  curses.color_pair(C_MUTED) | curses.A_DIM)

    # Which slice to show (tail unless scrolled up)
    start = max(0, total_pkts - max_pkt_rows - app.sniff_scroll)
    end   = start + max_pkt_rows
    visible = pkts[start:end]

    for i, p in enumerate(visible):
        row = pkt_top + i
        if row >= pkt_bot:
            break

        m  = int(p.t) // 60
        s  = int(p.t) % 60
        cs = int((p.t % 1) * 100)
        t_str    = f"{m}:{s:02d}.{cs:02d}"
        char_lbl = CHAR_LABELS.get(p.char_uuid, p.char_uuid[-7:])
        hex_str  = " ".join(f"{b:02x}" for b in p.raw)
        dec_str  = p.decoded or ""

        # Truncate hex to fit column
        max_hex_chars = W_RAW
        if len(hex_str) > max_hex_chars:
            hex_str = hex_str[:max_hex_chars - 1] + "…"

        line = (f"{t_str:>{W_TIME}}{GAP}{char_lbl:<{W_CHAR}}{GAP}"
                f"{hex_str:<{W_RAW}}{GAP}{dec_str[:W_DECODED]:<{W_DECODED}}")

        if p.decoded:
            attr = curses.color_pair(C_KNOWN)
        else:
            attr = curses.color_pair(C_MUTED)
        put(row, 0, line, attr)

    # scroll indicator
    if app.sniff_scroll > 0:
        put(pkt_bot - 1, 2,
            f" ↑ scrolled back {app.sniff_scroll} row(s) — ↓ to return to tail ",
            curses.color_pair(C_UNKNOWN))

    # ── Status bar ────────────────────────────────────────────────────────────
    left = f" {total_pkts} packet(s)"
    keys = " ↑↓ scroll · Space release/connect · ESC back · Q quit "
    bar_row = height - 1
    try:
        stdscr.addstr(bar_row, 0, " " * (width - 1), curses.color_pair(C_BAR))
        put(bar_row, 0, left, curses.color_pair(C_BAR) | curses.A_BOLD)
        put(bar_row, width - len(keys) - 1, keys, curses.color_pair(C_BAR))
    except curses.error:
        pass


# ── Input handling ────────────────────────────────────────────────────────────

def handle_key(app: App, key: int) -> str | None:
    """Returns 'start_sniff', 'stop_sniff', or None."""
    if app.mode == MODE_SCAN:
        if key in (ord("q"), ord("Q")):
            app.done = True
        elif key == 27:                          # ESC → quit
            app.done = True
        elif key == curses.KEY_UP:
            app.move_selection(-1)
        elif key == curses.KEY_DOWN:
            app.move_selection(+1)
        elif key in (curses.KEY_ENTER, 10, 13):
            sel = app.selected_entry()
            if sel:
                app.sniff_target  = sel
                app.sniff_packets = []
                app.sniff_scroll  = 0
                app.sniff_status  = "waiting…"
                app.mode          = MODE_SNIFF
                return "start_sniff"
        elif key in (ord("a"), ord("A")):
            app.show_all = not app.show_all
            if not app.show_all:
                app.devices = {k: v for k, v in app.devices.items()
                               if v.info is not None}
        elif key in (ord("c"), ord("C")):
            app.clear_stale()

    elif app.mode == MODE_SNIFF:
        if key in (ord("q"), ord("Q")):
            app.done = True
        elif key == 27:                          # ESC → back to scan
            app.mode = MODE_SCAN
            return "stop_sniff"
        elif key == ord(" "):                    # Space → release / reconnect
            return "toggle_sniff"
        elif key == curses.KEY_UP:
            app.sniff_scroll += 1
        elif key == curses.KEY_DOWN:
            app.sniff_scroll = max(0, app.sniff_scroll - 1)

    return None


# ── Async main ────────────────────────────────────────────────────────────────

async def run(app: App, stdscr: curses.window) -> None:
    ble_scanner = BleakScanner(detection_callback=app.on_adv)
    scanning = False

    async def start_scan() -> None:
        nonlocal scanning
        if not scanning:
            await ble_scanner.start()
            scanning = True

    async def stop_scan() -> None:
        nonlocal scanning
        if scanning:
            await ble_scanner.stop()
            scanning = False

    await start_scan()
    sniff_task: asyncio.Task | None = None

    try:
        while not app.done:
            key    = stdscr.getch()
            action = handle_key(app, key)

            if action == "start_sniff":
                if sniff_task:
                    sniff_task.cancel()
                    try:
                        await sniff_task
                    except (asyncio.CancelledError, Exception):
                        pass
                await stop_scan()
                if should_autoconnect(app.sniff_target):
                    sniff_task = asyncio.create_task(sniff_connect(app))
                else:
                    app.sniff_status = "passive"

            elif action == "stop_sniff":
                if sniff_task:
                    sniff_task.cancel()
                    try:
                        await sniff_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    sniff_task = None
                await start_scan()

            elif action == "toggle_sniff":
                if sniff_task and not sniff_task.done():
                    sniff_task.cancel()
                    try:
                        await sniff_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    sniff_task = None
                    app.sniff_status = "released"
                else:
                    await stop_scan()
                    sniff_task = asyncio.create_task(sniff_connect(app))

            # Sniff task may finish on its own (disconnect / error)
            if sniff_task and sniff_task.done():
                try:
                    await sniff_task
                except (asyncio.CancelledError, Exception):
                    pass
                sniff_task = None
                if app.mode == MODE_SCAN:
                    await start_scan()

            try:
                draw(stdscr, app)
            except curses.error:
                pass   # terminal resize mid-draw — next frame repairs it

            await asyncio.sleep(0.1)

    finally:
        if sniff_task:
            sniff_task.cancel()
            try:
                await sniff_task
            except (asyncio.CancelledError, Exception):
                pass
        await stop_scan()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live BLE scanner + sniffer for Chihiros devices")
    parser.add_argument("--all", action="store_true",
                        help="Start in show-all mode (all BLE devices)")
    args = parser.parse_args()

    app    = App(show_all=args.all)
    stdscr = curses.initscr()
    curses.noecho()
    curses.cbreak()
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(C_HEADER,  curses.COLOR_CYAN,    -1)
    curses.init_pair(C_KNOWN,   curses.COLOR_GREEN,   -1)
    curses.init_pair(C_UNKNOWN, curses.COLOR_YELLOW,  -1)
    curses.init_pair(C_MUTED,   curses.COLOR_WHITE,   -1)
    curses.init_pair(C_BAR,     curses.COLOR_BLACK,   curses.COLOR_CYAN)
    curses.init_pair(C_TITLE,   curses.COLOR_WHITE,   -1)
    curses.init_pair(C_RX,      curses.COLOR_GREEN,   -1)
    curses.init_pair(C_CONN,    curses.COLOR_CYAN,    -1)

    stdscr.nodelay(True)
    stdscr.keypad(True)

    try:
        asyncio.run(run(app, stdscr))
    except KeyboardInterrupt:
        pass
    finally:
        curses.nocbreak()
        stdscr.keypad(False)
        curses.echo()
        curses.endwin()


if __name__ == "__main__":
    main()
