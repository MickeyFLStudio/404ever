# name=TS404 Panel
# supportedDevices=TS404 Panel
# receiveFrom=TS404 Panel
"""
TS404 Panel - runs INSIDE FL Studio as a MIDI controller script.

FL Studio 2025+ removed the TS404's window but kept its engine and parameters, so
old projects still play. The TS404 Panel app gives the knobs back: it sends
commands on the "TS404 Panel" MIDI port (its own driver, ts404_midi.dll) and this
script applies them to the TS404 channel, and mirrors the channel's values back to
it through ts404.json.

Everything is file based (next to this script): FL 2025/2026 runs scripts in a
sub-interpreter without threads or sockets, so only plain open()/write() is used.

  heartbeat.json   written ~every 0.5 s while loaded: {"t", "version", "ts404": [[index, name], ...]}
  ts404.json       the watched channels' parameter values, while a panel watches them
  log.txt          append-only log
  launch.txt       written by the panel service / installer: how to start the service (program, arguments)
  server.json      written by the service every second while it runs: {"t", "pid"}

The service only needs to run while a project with a TS404 is open, so this script starts
it (launch.txt) when it sees a TS404 channel and no live server.json; the service quits by
itself when there is nothing to do.

Commands arrive as plain CCs on MIDI channel 16, NOT SysEx (incoming SysEx deadlocks
FL 2026's main thread): CC 120 = start (value = command), CC 121 = next payload
byte, CC 122 = run it. Payload:
  01 ch_hi ch_lo off_hi off_lo val_hi val_lo mode   set a param
      off = REC offset inside the channel; mode 0: val is 0..16383 (normalized),
      mode 1: val is the raw value (+8192 bias)
  02 ch_hi ch_lo                watch a channel (127,127 = follow the selected TS404)
  03                            play / stop (Space pressed in the panel)
  04 ch_hi ch_lo off_hi off_lo mode   open the event editor (mode = EE_EE / EE_PR / EE_PL)
  05 ch_hi ch_lo off_hi off_lo        make the param FL's "last tweaked" (for Tools > Last tweaked)
A watch expires 6 s after the last watch command (the panel re-sends it every ~2 s).
"""
import os
import sys
import time
import traceback

try:
    import json
except Exception:  # pragma: no cover
    json = None

VERSION = 1
HEARTBEAT_EVERY = 10          # idle ticks (~0.5 s at 20 Hz)
CC_STATUS = 0xBF              # CC on MIDI channel 16


def _base_dir():
    try:
        d = os.path.dirname(os.path.abspath(__file__))
        if d:
            return d
    except Exception:
        pass
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, "Documents", "Image-Line", "FL Studio", "Settings", "Hardware", "TS404 Panel")


BASE = _base_dir()
HB = os.path.join(BASE, "heartbeat.json")
TS404_JSON = os.path.join(BASE, "ts404.json")
LOG = os.path.join(BASE, "log.txt")

# channel REC offsets the panel may read and write: filter cutoff / resonance (channel
# parameters 2 and 3) and the TS404's own parameters (0x100 + n)
OFFS = [2, 3] + [0x100 + i for i in (3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17,
                                   20, 21, 22, 23, 24, 29, 30, 31, 32, 33, 34, 35)]
STATE = {"tick": 0, "errors": 0, "started": 0.0, "launched": 0.0}
LAUNCH = os.path.join(BASE, "launch.txt")
SERVER = os.path.join(BASE, "server.json")
TS = {"watch": {}, "last": None, "sel": -1, "written": 0.0}   # watch: channel index or "sel" -> expiry time
_frame = []


def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _write_json(path, obj):
    try:
        data = json.dumps(obj) if json else repr(obj)
        with open(path, "w") as f:
            f.write(data)
    except Exception as e:
        log("cannot write %s: %s" % (path, e))


def _chans():
    """[[index, name], ...] of every TS404 channel (TS404 = the CT_Hybrid channel type)."""
    out = []
    try:
        import channels, midi
        for i in range(channels.channelCount()):
            if channels.getChannelType(i) == midi.CT_Hybrid:
                out.append([i, channels.getChannelName(i)])
    except Exception:
        pass
    return out


def _is_ts404(ch):
    import channels, midi
    return isinstance(ch, int) and 0 <= ch < channels.channelCount() and channels.getChannelType(ch) == midi.CT_Hybrid


def _server_alive():
    try:
        with open(SERVER) as f:
            return time.time() - float((json.load(f) if json else eval(f.read()))["t"]) < 5
    except Exception:
        return False


def _start_server(chans):
    """Start the panel service when a TS404 channel exists and no service is running (at most once per 30 s)."""
    now = time.time()
    if not chans or now - STATE["launched"] < 30 or _server_alive():
        return
    STATE["launched"] = now
    try:
        with open(LAUNCH) as f:
            lines = f.read().splitlines() + ["", ""]
        prog, args = lines[0].strip(), lines[1].strip()
        if prog and os.path.exists(prog):
            if args:
                os.startfile(prog, arguments=args)
            else:
                os.startfile(prog)
            log("started the panel service: %s %s" % (prog, args))
    except Exception as e:
        log("cannot start the panel service: %s" % e)


def _heartbeat():
    chans = _chans()
    hb = {"t": time.time(), "tick": STATE["tick"], "version": VERSION, "errors": STATE["errors"],
          "started": STATE["started"], "python": sys.version.split()[0], "ts404": chans}
    try:
        import transport
        hb["playing"] = bool(transport.isPlaying())
        hb["recording"] = bool(transport.isRecording())
    except Exception:
        pass
    _write_json(HB, hb)
    try:
        _start_server(chans)
    except Exception:
        pass


def _export():
    import channels, device, general, midi
    sel = channels.selectedChannel()
    if _is_ts404(sel):
        TS["sel"] = sel   # "follow selected" keeps the last selected TS404
    targets = {TS["sel"] if k == "sel" else k for k in TS["watch"]}
    snap = {"sel": TS["sel"] if _is_ts404(TS["sel"]) else -1, "chans": _chans(), "data": {}}
    get = midi.REC_GetValue | midi.REC_NoLink
    for ch in targets:
        if not _is_ts404(ch):
            continue
        rid = channels.getRecEventId(ch)
        d = {"name": channels.getChannelName(ch), "params": {}}
        for off in OFFS:
            e = rid + off
            try:
                d["params"][str(off)] = [general.processRECEvent(e, 0, get), round(device.getLinkedValue(e), 5)]
            except Exception:
                pass
        snap["data"][str(ch)] = d
    now = time.time()
    if snap != TS["last"] or now - TS["written"] > 1.0:   # at least 1/s so the panel knows FL is alive
        TS["last"] = snap
        TS["written"] = now
        out = dict(snap)
        out["t"] = now
        _write_json(TS404_JSON, out)


def _command(data):
    import channels, general, midi, transport, ui
    cmd = data[0]
    if cmd == 3:
        if transport.isPlaying():
            transport.stop()
        else:
            transport.start()
        return
    if cmd in (4, 5) and len(data) >= 5:
        ch = (data[1] << 7) | data[2]
        off = (data[3] << 7) | data[4]
        if not _is_ts404(ch) or off not in OFFS:
            return
        e = channels.getRecEventId(ch) + off
        if cmd == 4:
            ui.openEventEditor(e, data[5] if len(data) >= 6 else midi.EE_EE)
        else:   # re-store the current value with "touched" set: it becomes the last tweaked param
            general.processRECEvent(e, general.processRECEvent(e, 0, midi.REC_GetValue | midi.REC_NoLink), midi.REC_Control)
        return
    if cmd == 2 and len(data) >= 3:
        ch = (data[1] << 7) | data[2]
        key = "sel" if ch == 0x3FFF else ch
        if key not in TS["watch"]:
            TS["last"] = None   # force a fresh export
        TS["watch"][key] = time.time() + 6.0
    elif cmd == 1 and len(data) >= 8:
        ch = (data[1] << 7) | data[2]
        off = (data[3] << 7) | data[4]
        val = (data[5] << 7) | data[6]
        if not _is_ts404(ch) or off not in OFFS:
            return
        e = channels.getRecEventId(ch) + off
        # behave like a hardware knob: shows the hint and records automation when recording
        if data[7] == 0:
            general.processRECEvent(e, int(val * midi.FromMIDI_Max / 16383), midi.REC_MIDIController)
        else:
            general.processRECEvent(e, val - 8192, midi.REC_Control)


def _cc(cc, value):
    if cc == 120:
        _frame[:] = [value]
    elif cc == 121 and _frame:
        _frame.append(value)
    elif cc == 122 and _frame:
        data = bytes(_frame)
        _frame[:] = []
        try:
            _command(data)
        except Exception:
            STATE["errors"] += 1
            log("command: %s" % traceback.format_exc().splitlines()[-1])


# ------------------------------------------------------------- FL callbacks
def OnInit():
    STATE["started"] = time.time()
    _heartbeat()
    log("init ok v%d dir=%s" % (VERSION, BASE))


def OnDeInit():
    _write_json(HB, {"t": 0, "version": VERSION, "unloaded": True})
    log("deinit")


def OnIdle():
    STATE["tick"] += 1
    if TS["watch"]:
        now = time.time()
        for k in [k for k, until in TS["watch"].items() if now > until]:
            del TS["watch"][k]
        if TS["watch"] and STATE["tick"] % 2 == 0:
            try:
                _export()
            except Exception:
                STATE["errors"] += 1
    if STATE["tick"] % HEARTBEAT_EVERY == 0:
        _heartbeat()


def OnMidiMsg(event):
    # panel commands are handled here and never reach FL as controller moves
    try:
        if event.status == CC_STATUS and event.data1 in (120, 121, 122):
            event.handled = True
            _cc(event.data1, event.data2)
    except Exception:
        pass


def OnProjectLoad(status):
    if status == 100:   # load finished
        TS["last"] = None
        _heartbeat()
