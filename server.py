"""TS404 Panel server.

FL Studio 2025+ removed the TS404's window but kept its engine and parameters,
so old projects still play. This serves a TS404-style panel (index.html) at
http://127.0.0.1:8911 and talks to FL through the "TS404 Panel" controller script
(fl/device_TS404Panel.py, installed into FL's Hardware folder):

  knob moves  -> CCs on channel 16 of the "TS404 Panel" MIDI port -> the script
  FL's values <- ts404.json, written by the script while a panel watches a channel

The "TS404 Panel" port is our own: ts404_midi.dll (midi/), a user-mode MME driver registered in
HKLM\...\Drivers32, gives every 64-bit program one loopback MIDI in/out of that name. No loopMIDI,
no kernel driver. (A loopMIDI port of the same name still works if that is what is there.)

It also puts a panel into every TS404 plugin window you open in FL (see Companion):
normally hosted INSIDE FL by ts404_host.dll (a WebView2 child of FL's plugin tab, so it
is aligned, clipped and stacked by Windows itself), or, if the DLL cannot be loaded, an
Edge window laid over the plugin tab from outside.

Standard library only. Starts with Windows (Startup folder shortcut). "--panel" opens
a free-standing panel window that follows the selected TS404 channel.
"""
import ctypes
import sys
import json
import os
import subprocess
import threading
import time
import traceback
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import hostlink   # ts404_host.dll: the panel as a real child window inside FL's plugin window

PORT = 8911
FROZEN = getattr(sys, "frozen", False)   # packaged as TS404Panel.exe (PyInstaller)
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))   # index.html, ts404_host.dll
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TS404 Panel")   # logs, caches
FOLLOW = 0x3FFF   # "watch whichever TS404 channel is selected"


def documents_dir():
    """The user's Documents folder as Windows knows it (FL keeps its settings there)."""
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]
        g = GUID(0xFDD39AD0, 0x238F, 0x46AF, (ctypes.c_ubyte * 8)(0xAD, 0xB4, 0x6D, 0x4E, 0xB3, 0xD6, 0xE4, 0xD8))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(p)) == 0:
            path = p.value
            ctypes.windll.ole32.CoTaskMemFree(p)
            return path
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


HARDWARE = os.path.join(documents_dir(), "Image-Line", "FL Studio", "Settings", "Hardware")
# Controller scripts that understand the panel's commands, each on a MIDI port of the same
# name as its folder: the installed "TS404 Panel" script (our driver's port), or the FL Bridge
# dev setup (loopMIDI) whose script has the same section. Whichever FL is running (fresh
# heartbeat) is used.
BRIDGES = ["TS404 Panel", "FL Bridge"]
# On-demand service: FL's script sandbox cannot start programs, but our MIDI driver runs inside FL
# (loaded when FL opens the "TS404 Panel" port) and can. It reads launch.txt and starts us as soon
# as FL's heartbeat lists a TS404 channel while server.json is stale; we quit again after a while
# with nothing to do (no FL, no TS404 channel, no panel window). Nothing runs at login.
SCRIPT_DIR = os.path.join(HARDWARE, "TS404 Panel")
LAUNCH = os.path.join(DATA_DIR, "launch.txt")        # program / arguments / heartbeat.json / server.json
ALIVE = os.path.join(SCRIPT_DIR, "server.json")      # {"t", "pid"}, refreshed every second
IDLE_EXIT = 120.0                                     # seconds of nothing to do before quitting
FL_DB = os.path.join(documents_dir(), "Image-Line", "FL Studio", "Presets", "Plugin database")
PICKER_FST = "TS404 bassline synthesizer.fst"


def list_ts404_in_picker():
    """FL 2025+ dropped the TS404 from its plugin list, but still ships its picker entry (the channel
    preset FL used to add one with). Copying that file into the user's plugin database brings it back
    to the plugin picker (F8) and the Browser, so new TS404 channels can be added again."""
    targets = [os.path.join(FL_DB, "Generators", "Synth classic", PICKER_FST),
               os.path.join(FL_DB, "Installed", "Generators", "Fruity", PICKER_FST)]
    if all(os.path.exists(t) for t in targets):
        return
    import glob
    import shutil
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    found = sorted(glob.glob(os.path.join(pf, "Image-Line", "FL Studio*", "Data", "System", "Default",
                                          "Installed plugins", "Generators", "Fruity", PICKER_FST)))
    if not found or not os.path.isdir(FL_DB):
        return
    for t in targets:
        if not os.path.exists(t):
            try:
                os.makedirs(os.path.dirname(t), exist_ok=True)
                shutil.copyfile(found[-1], t)
                log("TS404 picker entry: %s" % t)
            except OSError as e:
                log("TS404 picker entry failed: %s" % e)


def log(msg):
    try:
        with open(os.path.join(DATA_DIR, "server.log"), "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def read_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return None


# ------------------------------------------------------------------ MIDI out
class MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32), ("wTechnology", wintypes.WORD),
                ("wVoices", wintypes.WORD), ("wNotes", wintypes.WORD),
                ("wChannelMask", wintypes.WORD), ("dwSupport", wintypes.DWORD)]


winmm = ctypes.windll.winmm


class MidiOut:
    def __init__(self):
        self.h = None
        self.lock = threading.Lock()

    def _open(self):
        caps = MIDIOUTCAPSW()
        found = []
        for i in range(winmm.midiOutGetNumDevs()):
            winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
            if caps.szPname == bridge.current():
                found.append((caps.wPid != 0x4041, i))   # our own driver (product id 0x4041) first
        for _, i in sorted(found):
            h = wintypes.HANDLE()
            if winmm.midiOutOpen(ctypes.byref(h), i, 0, 0, 0) == 0:
                self.h = h
                return True
        return False

    def command(self, payload):
        # One panel command as plain CCs on channel 16: CC120 = start (value = first byte),
        # CC121 = each further byte, CC122 = run. Never SysEx: incoming SysEx deadlocks FL 2026
        # (see _ts404_cc in the FL Bridge script).
        msgs = [(120, payload[0])] + [(121, v) for v in payload[1:]] + [(122, 0)]
        with self.lock:   # frames from different threads must not interleave
            if self.h is None and not self._open():
                return False
            for cc, v in msgs:
                if winmm.midiOutShortMsg(self.h, 0xBF | (cc << 8) | ((v & 0x7F) << 16)) != 0:
                    self._reset()
                    return False
            return True

    def _reset(self):
        if self.h is not None:
            winmm.midiOutClose(self.h)
        self.h = None


midi = MidiOut()


class Bridge:
    """Which controller script FL is running right now: its port name (= its folder's name)."""
    def __init__(self):
        self.name = None
        self.checked = 0.0

    @staticmethod
    def alive(name):
        hb = read_json(os.path.join(HARDWARE, name, "heartbeat.json")) or {}
        return bool(hb.get("t")) and time.time() - hb["t"] < 3

    def current(self):
        # called from several threads at once: the name is never None, and a lookup in progress
        # on one thread must not leave another with nothing (a thread died on that once)
        now = time.time()
        if now - self.checked > 2:
            alive = [n for n in BRIDGES if self.alive(n)]
            name = alive[0] if alive else (self.name or BRIDGES[0])
            if name != self.name:
                self.name = name
                midi._reset()   # reopen the MIDI port under its new name
                log("using the %r controller script" % name)
            self.checked = now
        return self.name or BRIDGES[0]

    def path(self, file):
        return os.path.join(HARDWARE, self.current(), file)


bridge = Bridge()


def b14(v):
    v = max(0, min(0x3FFF, int(v)))
    return [(v >> 7) & 0x7F, v & 0x7F]


def send_set(ch, off, value, raw):
    v = int(value) + 8192 if raw else int(round(float(value) * 16383))
    return midi.command([0x01] + b14(ch) + b14(off) + b14(v) + [1 if raw else 0])


def send_watch(ch):
    return midi.command([0x02] + b14(ch))


def send_cmd(cmd, ch=None, off=None, extra=()):
    data = [cmd]
    if ch is not None:
        data += b14(ch) + b14(off)
    return midi.command(data + list(extra))


# ------------------------------------------------------- watch keep-alive
WATCH = {}   # channel (or FOLLOW) -> {"seen": last poll from a panel, "sent": last watch message}


def watch(ch):
    w = WATCH.setdefault(ch, {"seen": 0.0, "sent": 0.0})
    w["seen"] = time.time()
    if w["sent"] == 0.0 and send_watch(ch):   # first poll: start exporting right away
        w["sent"] = time.time()


def write_launch():
    """How the driver inside FL can start this service: the packaged exe, or python + server.py in
    development; plus the files it watches to decide (heartbeat.json: TS404 channels; server.json: us)."""
    try:
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        if FROZEN:
            prog, args = sys.executable, ""
        else:
            prog, args = sys.executable.replace("python.exe", "pythonw.exe"), '"%s"' % os.path.abspath(__file__)
        with open(LAUNCH, "w", encoding="utf-8") as f:
            f.write("\n".join([prog, args, os.path.join(SCRIPT_DIR, "heartbeat.json"), ALIVE]) + "\n")
    except OSError as e:
        log("cannot write %s: %s" % (LAUNCH, e))


def needed():
    """Something to do: a panel window is polling, or FL runs a project with a TS404 channel."""
    if WATCH:
        return True
    hb = read_json(bridge.path("heartbeat.json")) or {}
    return bool(hb.get("t")) and time.time() - hb["t"] < 3 and bool(hb.get("ts404"))


def keepalive():
    try:
        _keepalive()
    except Exception:   # a dead keepalive leaves the panel stuck on 'Connecting': say why
        log(traceback.format_exc())


def _keepalive():
    # the script stops exporting a channel ~6 s after its last watch message, so
    # refresh every channel some panel window is still polling
    last_needed = time.time()
    last_alive = 0.0
    while True:
        now = time.time()
        for ch, w in list(WATCH.items()):
            if now - w["seen"] > 3:
                del WATCH[ch]
            elif now - w["sent"] > 2 and send_watch(ch):
                w["sent"] = now
        if now - last_alive > 1:
            last_alive = now
            try:
                with open(ALIVE, "w") as f:
                    json.dump({"t": now, "pid": os.getpid()}, f)
            except OSError:
                pass
            if needed():
                last_needed = now
            elif now - last_needed > IDLE_EXIT:
                log("nothing to do for %d s: quitting (the FL script starts us again when needed)" % IDLE_EXIT)
                try:
                    os.remove(ALIVE)
                except OSError:
                    pass
                os._exit(0)
        time.sleep(0.25)


# ------------------------------------------------- panel over FL's TS404 windows
# FL 2026 still opens a plugin window for TS404 channels, but its plugin tab only
# shows a "deprecated" message. When one is open on that tab, a panel window for that
# channel is laid exactly over that message, follows the window when it moves or
# resizes, hides on the other tabs and closes with it.
#
# Hands off FL: we only READ FL's windows (no calls that send FL messages) and only
# ever move/clip/restack/show/close Edge's own window. Edge is NOT made a child or owned
# window of FL: that ties the two programs' input together, so one hanging hangs both.
# (The 2026-10-16/17 FL freezes were SysEx, not the overlay; see send_cmd.)
user32 = ctypes.windll.user32
for _f in ("GetParent", "FindWindowW", "MonitorFromWindow"):
    getattr(user32, _f).restype = wintypes.HWND
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
WinEventProc = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                  wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, WinEventProc,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]

EDGE = next((p for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                         r"C:\Program Files\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)), "msedge")
EDGE_PROFILE = os.path.join(DATA_DIR, "edge")
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
GW_HWNDPREV, GA_PARENT, GA_ROOT = 3, 1, 2
for _f in ("GetWindow", "GetAncestor"):
    getattr(user32, _f).restype = wintypes.HWND
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
gdi32 = ctypes.windll.gdi32
gdi32.CreateRectRgn.restype = wintypes.HANDLE
gdi32.CombineRgn.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE, ctypes.c_int]
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]


def wtext(h):
    b = ctypes.create_unicode_buffer(256)
    user32.InternalGetWindowText(h, b, 256)   # no message to the window's program (GetWindowText would send one)
    return b.value


def wclass(h):
    b = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(h, b, 128)
    return b.value


def wrect(h):
    r = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return r


def children(h, recursive=False):
    out = []
    user32.EnumChildWindows(h, EnumProc(lambda c, _: (out.append(c), True)[1]), 0)
    return out if recursive else [c for c in out if user32.GetParent(c) == h]


kernel32 = ctypes.windll.kernel32


def wpid(h):
    p = wintypes.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(p))
    return p.value


def is_64bit_process(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    wow = wintypes.BOOL(False)
    kernel32.IsWow64Process(h, ctypes.byref(wow))
    kernel32.CloseHandle(h)
    return not wow.value


def fl_main_window():
    """The 64-bit FL Studio's main window (FL 11 is 32-bit and has no TS404 problem), or 0."""
    tops = []
    user32.EnumWindows(EnumProc(lambda h, _: (tops.append(h), True)[1]), 0)
    for h in tops:
        if wclass(h) == "TFruityLoopsMainForm" and is_64bit_process(wpid(h)):
            return h
    return 0


def fl_plugin_forms():
    fl = fl_main_window()
    if not fl:
        return []
    pid = wpid(fl)
    tops = []
    user32.EnumWindows(EnumProc(lambda h, _: (tops.append(h), True)[1]), 0)
    forms = []
    for h in tops:
        if wpid(h) != pid:
            continue
        c = wclass(h)
        if c == "TFruityLoopsMainForm":
            forms += [f for f in children(h, True) if wclass(f) == "TPluginForm"]
        elif c == "TPluginForm":   # detached plugin window
            forms.append(h)
    return forms


def edge_offsets(edge):
    """(left, top, right, bottom) of Edge's frame around the web page, or None before the page is up."""
    page = next((c for c in children(edge, True)
                 if wclass(c) == "Chrome_RenderWidgetHostHWND" and user32.IsWindowVisible(c)), None)
    if not page:
        return None
    e, r = wrect(edge), wrect(page)
    if r.right - r.left < 50:
        return None
    return (r.left - e.left, r.top - e.top, e.right - r.right, e.bottom - r.bottom)


def covering_rects(form):
    """Screen rects of visible FL windows stacked above the plugin window inside FL (other plugin
    windows, piano roll...), so the panel never hides them."""
    out, h = [], form
    while h and user32.GetAncestor(h, GA_ROOT) != h:
        sib = user32.GetWindow(h, GW_HWNDPREV)
        while sib:
            if user32.IsWindowVisible(sib):
                r = wrect(sib)
                out.append((r.left, r.top, r.right, r.bottom))
            sib = user32.GetWindow(sib, GW_HWNDPREV)
        h = user32.GetAncestor(h, GA_PARENT)
    return out


def visible_on_screen(h):
    while h:
        if not user32.IsWindowVisible(h) or user32.IsIconic(h):
            return False
        h = user32.GetParent(h)
    return True


def panel_title(name):
    return "TS404 - " + name   # index.html sets exactly this title in companion mode


def plugin_sheet(form):
    """The plugin window's visible content sheet when it is on the plugin tab, else None. FL's other
    tabs fill their sheet with controls; the TS404 plugin tab has none (only our hosted panel)."""
    sheet = next((c for c in children(form) if wclass(c) == "TVectorSheet" and user32.IsWindowVisible(c)), None)
    if sheet is None or any(user32.IsWindowVisible(c) and wclass(c) != "TS404Panel" for c in children(sheet)):
        return None
    return sheet


class Companion:
    def __init__(self):
        self.panels = {}   # Edge overlay mode: FL plugin form -> {"ch", "name", "edge", "launched", "shown", "key", "offs"}
        self.hosted = {}   # hosted mode: FL plugin form -> {"ch", "name", "sheet"}
        self.fl = None     # (FL pid, host window inside FL or 0, time of that attempt)
        self.dirty = True
        self.hook_proc = WinEventProc(self._on_event)

    def _on_event(self, *a):
        self.dirty = True   # some window moved / showed / hid: re-check on this thread's next pass

    def host_window(self):
        """ts404_host.dll's command window inside the running FL (loading the DLL first time), or 0."""
        fl = fl_main_window()
        if not fl:
            self.fl = None
            return 0
        pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(fl, ctypes.byref(pid))
        if self.fl and self.fl[0] == pid.value:
            if self.fl[1] and user32.IsWindow(self.fl[1]):
                return self.fl[1]
            if time.time() - self.fl[2] < 15:
                return 0   # last attempt failed: Edge overlay meanwhile, retry later
        try:
            host = hostlink.inject(tid, pid.value)
        except Exception:
            log(traceback.format_exc())
            host = 0
        self.fl = (pid.value, host, time.time())
        log("ts404_host.dll in FL pid %d: %s" % (pid.value, "window %#x" % host if host else "FAILED, Edge overlay instead"))
        return host

    def ts404_by_name(self):
        hb = read_json(bridge.path("heartbeat.json")) or {}
        if not hb.get("t") or time.time() - hb["t"] > 3:
            return None
        return {name: i for i, name in reversed(hb.get("ts404") or [])}

    def launch(self, p):
        url = "http://127.0.0.1:%d/?ch=%d&companion=1" % (PORT, p["ch"])
        subprocess.Popen([EDGE, "--app=" + url, "--user-data-dir=" + EDGE_PROFILE, "--no-first-run",
                          "--no-default-browser-check", "--window-position=-32000,-32000", "--window-size=576,392"])   # panel is 560 x 346
        p["launched"] = time.time()

    def tick(self):
        names = self.ts404_by_name()
        host = self.host_window() if names else 0
        if host:
            for form in list(self.panels):   # switching to hosted mode: no more overlays
                self.drop(form)
            return self.tick_hosted(host, names)
        seen = set()
        for form in (fl_plugin_forms() if names else []):
            # "Rave (Insert 10)": FL wraps the grey part in private-use formatting characters
            title = "".join(c for c in wtext(form) if not "\ue000" <= c <= "\uf8ff")
            name = title.rsplit(" (", 1)[0] if title.endswith(")") else title
            ch = names.get(name)
            if ch is None:
                continue
            p = self.panels.get(form)
            if p and p["ch"] != ch:
                self.drop(form)
                p = None
            # plugin tab = FL's content sheet has no controls of its own (other tabs fill it with panels)
            sheet = next((c for c in children(form) if wclass(c) == "TVectorSheet" and user32.IsWindowVisible(c)), None)
            on_plugin_tab = sheet is not None and not any(user32.IsWindowVisible(c) for c in children(sheet))
            open_now = visible_on_screen(form)
            if not open_now:
                continue   # closed (FL hides plugin windows): dropped below
            seen.add(form)
            if not p:
                if not on_plugin_tab:
                    continue
                p = self.panels[form] = {"ch": ch, "name": name, "edge": None, "launched": 0, "shown": None, "key": None, "offs": None}
                self.launch(p)
            self.place(form, p, on_plugin_tab)
        for form in list(self.panels):
            if form not in seen:
                self.drop(form)

    def tick_hosted(self, host, names):
        """Every open TS404 plugin window on its plugin tab gets a panel hosted in its sheet by the
        DLL; from then on FL itself moves, resizes, hides (other tabs, closed window) and destroys it."""
        seen = set()
        for form in fl_plugin_forms():
            title = "".join(c for c in wtext(form) if not "" <= c <= "")
            name = title.rsplit(" (", 1)[0] if title.endswith(")") else title
            ch = names.get(name)
            if ch is None:
                continue
            seen.add(form)
            p = self.hosted.get(form)
            if p and p["ch"] == ch and user32.IsWindow(p["sheet"]):
                continue   # hosted already (hidden tabs / closed windows keep their panel)
            sheet = plugin_sheet(form)
            if sheet is None:
                continue   # not on the plugin tab right now (or not open): nothing to put a panel into
            if p:
                hostlink.send(host, "drop %d" % p["sheet"])
            r = hostlink.send(host, "host %d http://127.0.0.1:%d/?ch=%d&companion=1&hosted=1" % (sheet, PORT, ch))
            if r == 1:
                self.hosted[form] = {"ch": ch, "name": name, "sheet": sheet}
                log("hosted panel for channel %d (%s) in sheet %#x" % (ch, name, sheet))
            else:
                log("host command failed for %s: %r" % (name, r))
        for form in list(self.hosted):
            if form not in seen:   # window gone, or no longer a TS404
                p = self.hosted.pop(form)
                if user32.IsWindow(p["sheet"]):
                    hostlink.send(host, "drop %d" % p["sheet"])

    def place(self, form, p, on_plugin_tab):
        if p["edge"] and not user32.IsWindow(p["edge"]):   # you closed the panel: leave it closed for this window
            p["edge"] = False
        if p["edge"] is False:
            return
        if not p["edge"]:
            edge = user32.FindWindowW(None, panel_title(p["name"]))
            if not edge:
                if time.time() - p["launched"] > 20:   # Edge never came up: try again
                    self.launch(p)
                return
            p["edge"], p["key"], p["offs"] = edge, None, None
        edge = p["edge"]
        offs = edge_offsets(edge)   # Edge's frame around the page (changes once its window is really up)
        if not offs:
            return
        p["offs"] = offs
        sheet = next((c for c in children(form) if wclass(c) == "TVectorSheet" and user32.IsWindowVisible(c)), None)
        top = user32.GetAncestor(form, GA_ROOT)
        if not on_plugin_tab or not sheet:
            self.hide(p)
            return
        # Edge's page goes exactly over FL's content area (the deprecation message), its own
        # frame is clipped away, and so is any FL window lying on top of the plugin window
        s = wrect(sheet)
        dl, dt, dr, db = p["offs"]
        x, y, w, h = s.left - dl, s.top - dt, s.right - s.left, s.bottom - s.top
        covers = [c for c in covering_rects(form) if c[0] < s.right and c[2] > s.left and c[1] < s.bottom and c[3] > s.top]
        key = (x, y, w, h, tuple(covers), offs)
        if key != p["key"]:
            user32.SetWindowPos(edge, None, x, y, w + dl + dr, h + dt + db, SWP_NOZORDER | SWP_NOACTIVATE)
            rgn = gdi32.CreateRectRgn(dl, dt, dl + w, dt + h)
            for l, t, r, b in covers:
                c = gdi32.CreateRectRgn(l - x, t - y, r - x, b - y)
                gdi32.CombineRgn(rgn, rgn, c, 4)   # RGN_DIFF
                gdi32.DeleteObject(c)
            user32.SetWindowRgn(edge, rgn, True)   # Windows owns rgn now
            p["key"] = key
        # keep Edge stacked right above FL (not above everything): it sinks with FL behind other apps
        above = user32.GetWindow(top, GW_HWNDPREV)
        if above != edge:
            user32.SetWindowPos(edge, above or None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        if not p["shown"]:
            user32.ShowWindow(edge, 4)   # SW_SHOWNOACTIVATE
            p["shown"] = True

    def hide(self, p):
        if p["shown"] is not False:
            user32.ShowWindow(p["edge"], 0)   # SW_HIDE
            p["shown"] = False

    def drop(self, form):
        p = self.panels.pop(form, None)
        if p and p["edge"] and user32.IsWindow(p["edge"]):
            user32.PostMessageW(p["edge"], 0x0010, 0, 0)   # WM_CLOSE

    def run(self):
        # window move / show / hide / foreground / minimize events -> re-check soon
        # (WINEVENT_OUTOFCONTEXT: delivered asynchronously to this thread, FL never waits on us)
        hooks = [user32.SetWinEventHook(lo, hi, None, self.hook_proc, 0, 0, 0)
                 for lo, hi in ((0x8002, 0x8003), (0x800B, 0x800B), (0x0003, 0x0003), (0x0016, 0x0017))]
        msg = wintypes.MSG()
        last = 0.0
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            now = time.time()
            if self.dirty or now - last > 0.3:
                self.dirty = False
                last = now
                try:
                    self.tick()
                except Exception:
                    log(traceback.format_exc())
            user32.MsgWaitForMultipleObjects(0, None, False, 20, 0x04FF)


# ------------------------------------------------------------------- HTTP
# ------------------------------------------- Tools > Last tweaked, clicked for you
# FL's scripting API can make a param "last tweaked" (bridge command 05) but has no call
# to create an automation clip or link a controller, so we click FL's own menu items:
# TOOLS on the menu bar -> "Last tweaked" (second-to-last row) -> the item in its submenu.
# Offsets are FL's layout at 96 dpi * 1.25 (scaled by FL's DPI). Real mouse input only,
# the same as you clicking; the mouse goes back where it was.
LT_ITEMS = {"clip": 167, "link": 230}   # submenu row centres below the submenu's top edge
lt_lock = threading.Lock()


def fl_popups(pid):
    out = []
    def cb(h, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h) and wclass(h) == "TQuickPopupMenuWindow":
            r = wrect(h)
            out.append((r.left, r.top, r.right, r.bottom))
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    return out


def wait_popups(pid, test, timeout=1.5):
    end = time.time() + timeout
    while time.time() < end:
        found = [q for q in fl_popups(pid) if test(q)]
        if found:
            return found[0]
        time.sleep(0.03)
    return None


def key(vk):
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)


def click(x, y):
    user32.SetCursorPos(x, y)
    time.sleep(0.04)
    user32.mouse_event(2, 0, 0, 0, 0)   # left down
    user32.mouse_event(4, 0, 0, 0, 0)   # left up


def last_tweaked(action):
    fl = fl_main_window()
    if action not in LT_ITEMS or not fl or not visible_on_screen(fl):
        return False
    with lt_lock:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(fl, ctypes.byref(pid))
        pid = pid.value
        sc = user32.GetDpiForWindow(fl) / 120
        back = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(back))
        try:
            time.sleep(0.25)   # let FL handle the touch command first
            o = wintypes.POINT(0, 0)
            user32.ClientToScreen(fl, ctypes.byref(o))
            click(o.x + int(305 * sc), o.y + int(25 * sc))   # TOOLS
            menu = wait_popups(pid, lambda q: q[3] - q[1] > 300 * sc)
            if not menu:
                return False
            l, t, r, b = menu
            row_y = b - int(36 * sc)   # "Last tweaked"
            user32.SetCursorPos(l + int(57 * sc), row_y)
            sub = wait_popups(pid, lambda q: q != menu and abs(q[1] - row_y) < 40 * sc)
            if not sub:
                # close only our menu: Escape with no menu open closes FL's focused window
                for _ in range(3):
                    if not fl_popups(pid):
                        break
                    key(0x1B)
                    time.sleep(0.1)
                return False
            sx = sub[0] + int(60 * sc)
            user32.SetCursorPos(sx, row_y)   # slide along the row into the submenu so it stays open
            time.sleep(0.1)
            click(sx, sub[1] + int(LT_ITEMS[action] * sc))
            return True
        finally:
            time.sleep(0.05)
            user32.SetCursorPos(back.x, back.y)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/state":
            # /state?ch=N (a pinned/overlay panel) or /state?ch=sel (follow the selected TS404)
            q = parse_qs(url.query).get("ch", ["sel"])[0]
            key = FOLLOW if q == "sel" else int(q)
            watch(key)
            full = read_json(bridge.path("ts404.json")) or {}
            hb = read_json(bridge.path("heartbeat.json")) or {}
            now = time.time()
            ch = full.get("sel", -1) if key == FOLLOW else key
            d = (full.get("data") or {}).get(str(ch))
            st = {"chans": full.get("chans") or hb.get("ts404") or [], "chan": ch if d else -1,
                  "name": d and d["name"], "params": d and d["params"] or {},
                  "bridge": bool(hb.get("t")) and now - hb["t"] < 3,
                  "fresh": bool(full.get("t")) and now - full["t"] < 2.5,
                  "midi": midi.h is not None}
            return self._send(200, st)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/set":
            ok = send_set(int(body["ch"]), int(body["off"]), body["v"], bool(body.get("raw")))
            return self._send(200, {"ok": ok})
        if self.path == "/transport":   # Space in the panel: play / stop
            return self._send(200, {"ok": send_cmd(3)})
        if self.path == "/editor":      # mode 0 = event editor, 1 = piano roll, 2 = playlist
            return self._send(200, {"ok": send_cmd(4, int(body["ch"]), int(body["off"]), [int(body.get("mode", 0)) & 0x7F])})
        if self.path == "/lasttweaked":   # Tools > Last tweaked > create clip / link to controller
            threading.Thread(target=last_tweaked, args=(body.get("action"),), daemon=True).start()
            return self._send(200, {"ok": True})
        if self.path == "/touch":       # make it FL's "last tweaked" parameter
            return self._send(200, {"ok": send_cmd(5, int(body["ch"]), int(body["off"]))})
        self._send(404, {"error": "not found"})


def server_up():
    import socket
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.5).close()
        return True
    except OSError:
        return False


def open_panel_window():
    """A free-standing panel window that follows the selected TS404 channel (starts the server if needed)."""
    if not server_up():
        subprocess.Popen([sys.executable] if FROZEN else [sys.executable, os.path.abspath(__file__)],
                         creationflags=0x08000000)   # CREATE_NO_WINDOW
        for _ in range(50):
            time.sleep(0.1)
            if server_up():
                break
    url = "http://127.0.0.1:%d/" % PORT
    if os.path.exists(EDGE):
        subprocess.Popen([EDGE, "--app=" + url, "--user-data-dir=" + EDGE_PROFILE, "--no-first-run",
                          "--no-default-browser-check", "--window-size=600,430"])
    else:
        import webbrowser
        webbrowser.open(url)


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        list_ts404_in_picker()
    except Exception:
        log(traceback.format_exc())
    write_launch()
    if "--panel" in sys.argv:
        open_panel_window()
        raise SystemExit
    # same (physical pixel) coordinates as FL and Edge, which are both per-monitor DPI aware
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    ThreadingHTTPServer.allow_reuse_address = False   # so a second copy really fails to bind
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        raise SystemExit   # already running (started at login)
    threading.Thread(target=keepalive, daemon=True).start()
    threading.Thread(target=lambda: Companion().run(), daemon=True).start()
    server.serve_forever()
