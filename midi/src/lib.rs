//! ts404_midi.dll: a user-mode MME (winmm) MIDI driver with one loopback port, "TS404 Panel".
//!
//! Registered as `midiN=<full path>` under HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Drivers32,
//! winmm loads it into every 64-bit program that uses MIDI and asks it for its devices, the same way
//! wdmaud.drv is asked for the hardware ones. It offers one MIDI output and one MIDI input of the same
//! name: whatever any program sends to the output arrives at the input, like a loopMIDI port, except
//! that there is no kernel driver and nothing to run. Short messages only (notes, CCs...): SysEx is
//! accepted and dropped, since the panel never uses it (incoming SysEx deadlocks FL 2026).
//!
//! Transport: a shared-memory ring of packed MIDI words (file mapping in the session's Local\ namespace),
//! a named mutex for writers and an auto-reset event that wakes the reader thread in the program that
//! opened the input. That thread hands each message to the program through DriverCallback, like any
//! MIDI driver does.
//!
//! The DLL is loaded into unrelated programs too (anything enumerating MIDI devices), so it does nothing
//! until a device is opened, and nothing it does can fail loudly: every error is just an MMSYSERR code.
#![allow(non_snake_case, non_camel_case_types, clippy::missing_safety_doc)]

use std::ffi::c_void;
use std::ptr;
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::Mutex;

// ----------------------------------------------------------------------------- Win32 (hand declared)
type HANDLE = *mut c_void;
type BOOL = i32;
type DWORD = u32;
type DWORD_PTR = usize;
type LPARAM = isize;
type LRESULT = isize;

#[link(name = "kernel32")]
extern "system" {
    fn CreateFileMappingW(h: HANDLE, sec: *const c_void, prot: DWORD, hi: DWORD, lo: DWORD, name: *const u16) -> HANDLE;
    fn MapViewOfFile(h: HANDLE, access: DWORD, hi: DWORD, lo: DWORD, n: usize) -> *mut c_void;
    fn CreateEventW(sec: *const c_void, manual: BOOL, initial: BOOL, name: *const u16) -> HANDLE;
    fn CreateMutexW(sec: *const c_void, owner: BOOL, name: *const u16) -> HANDLE;
    fn SetEvent(h: HANDLE) -> BOOL;
    fn WaitForSingleObject(h: HANDLE, ms: DWORD) -> DWORD;
    fn WaitForMultipleObjects(n: DWORD, hs: *const HANDLE, all: BOOL, ms: DWORD) -> DWORD;
    fn ReleaseMutex(h: HANDLE) -> BOOL;
    fn CloseHandle(h: HANDLE) -> BOOL;
    fn GetTickCount() -> DWORD;
}
#[link(name = "user32")]
extern "system" {
    fn DefDriverProc(id: DWORD_PTR, hdrvr: HANDLE, msg: u32, l1: LPARAM, l2: LPARAM) -> LRESULT;
}
#[link(name = "winmm")]
extern "system" {
    fn DriverCallback(cb: DWORD_PTR, flags: DWORD, hdev: HANDLE, msg: DWORD, user: DWORD_PTR, p1: DWORD_PTR, p2: DWORD_PTR) -> BOOL;
}

const INVALID_HANDLE_VALUE: HANDLE = -1isize as HANDLE;
const PAGE_READWRITE: DWORD = 0x04;
const FILE_MAP_ALL_ACCESS: DWORD = 0xF001F;
const WAIT_OBJECT_0: DWORD = 0;
const WAIT_TIMEOUT: DWORD = 0x102;
const WAIT_ABANDONED: DWORD = 0x80;
const INFINITE: DWORD = 0xFFFF_FFFF;

// mmsystem / mmddk
const MMSYSERR_NOERROR: DWORD = 0;
const MMSYSERR_ERROR: DWORD = 1;
const MMSYSERR_BADDEVICEID: DWORD = 2;
const MMSYSERR_ALLOCATED: DWORD = 4;
const MMSYSERR_INVALHANDLE: DWORD = 5;
const MMSYSERR_NOTSUPPORTED: DWORD = 8;
const MMSYSERR_INVALPARAM: DWORD = 11;

const DRV_LOAD: u32 = 1;
const DRV_ENABLE: u32 = 2;
const DRV_OPEN: u32 = 3;
const DRV_CLOSE: u32 = 4;
const DRV_DISABLE: u32 = 5;
const DRV_FREE: u32 = 6;
const DRV_QUERYCONFIGURE: u32 = 7;
const DRV_CONFIGURE: u32 = 8;
const DRV_INSTALL: u32 = 9;
const DRV_REMOVE: u32 = 10;
const DRVCNF_OK: LRESULT = 1;

const DRVM_INIT: u32 = 100;
const DRVM_EXIT: u32 = 101;
const DRVM_DISABLE: u32 = 102;
const DRVM_ENABLE: u32 = 103;

const MIDM_GETNUMDEVS: u32 = 53;
const MIDM_GETDEVCAPS: u32 = 54;
const MIDM_OPEN: u32 = 55;
const MIDM_CLOSE: u32 = 56;
const MIDM_PREPARE: u32 = 57;     // (numbering verified against winmm: PREPARE 57, UNPREPARE 58,
const MIDM_UNPREPARE: u32 = 58;   //  ADDBUFFER 59, START 60, STOP 61, RESET 62)
const MIDM_ADDBUFFER: u32 = 59;
const MIDM_START: u32 = 60;
const MIDM_STOP: u32 = 61;
const MIDM_RESET: u32 = 62;

const MODM_GETNUMDEVS: u32 = 1;
const MODM_GETDEVCAPS: u32 = 2;
const MODM_OPEN: u32 = 3;
const MODM_CLOSE: u32 = 4;
const MODM_PREPARE: u32 = 5;
const MODM_UNPREPARE: u32 = 6;
const MODM_DATA: u32 = 7;
const MODM_LONGDATA: u32 = 8;
const MODM_RESET: u32 = 9;
const MODM_GETVOLUME: u32 = 10;
const MODM_SETVOLUME: u32 = 11;
const MODM_CACHEPATCHES: u32 = 12;
const MODM_CACHEDRUMPATCHES: u32 = 13;

const MIM_OPEN: DWORD = 0x3C1;
const MIM_CLOSE: DWORD = 0x3C2;
const MIM_DATA: DWORD = 0x3C3;
const MIM_LONGDATA: DWORD = 0x3C4;
const MOM_OPEN: DWORD = 0x3C7;
const MOM_CLOSE: DWORD = 0x3C8;
const MOM_DONE: DWORD = 0x3C9;

const CALLBACK_TYPEMASK: DWORD = 0x0007_0000;
const MHDR_DONE: DWORD = 1;
const MHDR_INQUEUE: DWORD = 4;
const MOD_MIDIPORT: u16 = 1;

const MANUFACTURER: u16 = 0xFFFF; // MM_UNMAPPED
const PRODUCT: u16 = 0x4041;      // "TS404 Panel"
const VERSION: u32 = 0x0100;

#[repr(C)]
struct MIDIINCAPSW { wMid: u16, wPid: u16, vDriverVersion: u32, szPname: [u16; 32], dwSupport: DWORD }
#[repr(C)]
struct MIDIOUTCAPSW { wMid: u16, wPid: u16, vDriverVersion: u32, szPname: [u16; 32], wTechnology: u16,
                      wVoices: u16, wNotes: u16, wChannelMask: u16, dwSupport: DWORD }
#[repr(C)]
struct MIDIOPENDESC { hMidi: HANDLE, dwCallback: DWORD_PTR, dwInstance: DWORD_PTR, dnDevNode: DWORD_PTR, cIds: DWORD }
#[repr(C)]
struct MIDIHDR { lpData: *mut u8, dwBufferLength: DWORD, dwBytesRecorded: DWORD, dwUser: DWORD_PTR, dwFlags: DWORD,
                 lpNext: *mut MIDIHDR, reserved: DWORD_PTR, dwOffset: DWORD, dwReserved: [DWORD_PTR; 8] }

/// Debug trace, only while the environment variable TS404_MIDI_TRACE is set: %TEMP%\ts404_midi.log
fn trace(what: &str) {
    use std::io::Write;
    static ON: std::sync::OnceLock<bool> = std::sync::OnceLock::new();
    if !*ON.get_or_init(|| std::env::var_os("TS404_MIDI_TRACE").is_some()) { return; }
    if let Ok(t) = std::env::var("TEMP") {
        if let Ok(mut f) = std::fs::OpenOptions::new().append(true).create(true).open(format!("{t}\\ts404_midi.log")) {
            let _ = writeln!(f, "pid {} {}", std::process::id(), what);
        }
    }
}

fn wide(s: &str) -> Vec<u16> { s.encode_utf16().chain(std::iter::once(0)).collect() }
fn name32() -> [u16; 32] { let mut n = [0u16; 32]; for (i, c) in "TS404 Panel".encode_utf16().enumerate() { n[i] = c; } n }

// ----------------------------------------------------------------------------- shared ring
const MAGIC: u32 = 0x3430_3454; // "T404"
const CAP: u32 = 4096;          // messages; a panel sends ~10 per knob move

#[repr(C)]
struct Ring { magic: AtomicU32, cap: AtomicU32, head: AtomicU32, tail: AtomicU32,
              stats: [AtomicU32; 8],   // in_open, started, pushed, delivered, dropped, opens, closes, spawns (diagnostics)
              msgs: [AtomicU32; CAP as usize] }
const ST_OPEN: usize = 0; const ST_STARTED: usize = 1; const ST_PUSHED: usize = 2; const ST_DELIVERED: usize = 3;
const ST_DROPPED: usize = 4; const ST_OPENS: usize = 5; const ST_CLOSES: usize = 6; const ST_SPAWNS: usize = 7;

struct Shared { ring: *mut Ring, event: HANDLE, lock: HANDLE }
unsafe impl Send for Shared {}

static SHARED: Mutex<Option<Shared>> = Mutex::new(None);

/// Maps (creating if needed) the ring shared by every program that has the port open.
unsafe fn shared() -> Option<(*mut Ring, HANDLE, HANDLE)> {
    let mut g = SHARED.lock().ok()?;
    if let Some(s) = g.as_ref() { return Some((s.ring, s.event, s.lock)); }
    let size = std::mem::size_of::<Ring>();
    let map = CreateFileMappingW(INVALID_HANDLE_VALUE, ptr::null(), PAGE_READWRITE, 0, size as u32, wide("Local\\TS404Panel.Midi.v2").as_ptr());
    if map.is_null() { return None; }
    let view = MapViewOfFile(map, FILE_MAP_ALL_ACCESS, 0, 0, size) as *mut Ring;
    if view.is_null() { CloseHandle(map); return None; }
    let event = CreateEventW(ptr::null(), 0, 0, wide("Local\\TS404Panel.Midi.v2.Data").as_ptr());
    let lock = CreateMutexW(ptr::null(), 0, wide("Local\\TS404Panel.Midi.v2.Lock").as_ptr());
    if event.is_null() || lock.is_null() { return None; }
    WaitForSingleObject(lock, 2000);
    if (*view).magic.load(Ordering::Acquire) != MAGIC {   // fresh mapping (zeroed by Windows)
        (*view).cap.store(CAP, Ordering::Relaxed);
        (*view).head.store(0, Ordering::Relaxed);
        (*view).tail.store(0, Ordering::Relaxed);
        (*view).magic.store(MAGIC, Ordering::Release);
    }
    ReleaseMutex(lock);
    *g = Some(Shared { ring: view, event, lock });
    Some((view, event, lock))
}

unsafe fn push(msg: u32) -> bool {
    let Some((ring, event, lock)) = shared() else { return false };
    let w = WaitForSingleObject(lock, 1000);
    if w != WAIT_OBJECT_0 && w != WAIT_ABANDONED { return false; }   // abandoned (a writer died holding it) is still ours
    let head = (*ring).head.load(Ordering::Relaxed);
    let tail = (*ring).tail.load(Ordering::Acquire);
    let ok = head.wrapping_sub(tail) < CAP;   // full (nobody reading): drop, never block the sender
    if ok {
        (*ring).msgs[(head % CAP) as usize].store(msg, Ordering::Relaxed);
        (*ring).head.store(head.wrapping_add(1), Ordering::Release);
        (*ring).stats[ST_PUSHED].fetch_add(1, Ordering::Relaxed);
    } else {
        (*ring).stats[ST_DROPPED].fetch_add(1, Ordering::Relaxed);
    }
    ReleaseMutex(lock);
    SetEvent(event);
    ok
}

// ----------------------------------------------------------------------------- input device (one open at a time)
#[derive(Clone, Copy)]
struct Client { cb: DWORD_PTR, flags: DWORD, hmidi: HANDLE, instance: DWORD_PTR }
impl Client {
    unsafe fn notify(&self, msg: DWORD, p1: DWORD_PTR, p2: DWORD_PTR) {
        if self.cb != 0 { DriverCallback(self.cb, self.flags, self.hmidi, msg, self.instance, p1, p2); }
    }
}

struct Input {
    client: Client,
    started: bool,
    start_tick: DWORD,
    buffers: Vec<*mut MIDIHDR>,   // SysEx buffers the program queued; we never fill them, only hand them back
    stop: HANDLE,
    thread: Option<std::thread::JoinHandle<()>>,
}
unsafe impl Send for Input {}

static INPUT: Mutex<Option<Input>> = Mutex::new(None);

// ----------------------------------------------------------------------------- starting the panel service
// FL's controller-script sandbox cannot start programs, but this driver lives inside FL while the port
// is open. launch.txt (%LOCALAPPDATA%\TS404 Panel) says: program, arguments, FL's heartbeat.json, the
// service's server.json. When the heartbeat is fresh and lists a TS404 channel and server.json is stale,
// the service is started (at most once per 30 s).
static LAST_SPAWN: AtomicU32 = AtomicU32::new(0);

fn fresh(path: &str, secs: u64) -> bool {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
        .and_then(|t| std::time::SystemTime::now().duration_since(t).ok())
        .map(|d| d.as_secs() < secs).unwrap_or(false)
}

unsafe fn maybe_launch(ring: *mut Ring) {
    use std::os::windows::process::CommandExt;
    let now = GetTickCount();
    let last = LAST_SPAWN.load(Ordering::Relaxed);
    if last != 0 && now.wrapping_sub(last) < 30_000 { return; }
    let Some(base) = std::env::var_os("LOCALAPPDATA") else { return };
    let path = std::path::Path::new(&base).join("TS404 Panel").join("launch.txt");
    let Ok(text) = std::fs::read_to_string(&path) else { return };
    let lines: Vec<&str> = text.lines().map(str::trim).collect();
    if lines.len() < 4 || lines[0].is_empty() { return; }
    let (prog, args, heartbeat, alive) = (lines[0], lines[1], lines[2], lines[3]);
    if fresh(alive, 5) || !fresh(heartbeat, 5) { return; }   // service already running / FL script not running
    let Ok(hb) = std::fs::read_to_string(heartbeat) else { return };
    if !hb.contains("\"ts404\": [[") { return; }              // no TS404 channel in this project
    LAST_SPAWN.store(now.max(1), Ordering::Relaxed);
    let mut cmd = std::process::Command::new(prog);
    if !args.is_empty() { cmd.raw_arg(args); }
    cmd.creation_flags(0x0800_0008);   // CREATE_NO_WINDOW | DETACHED_PROCESS
    match cmd.spawn() {
        Ok(_) => { (*ring).stats[ST_SPAWNS].fetch_add(1, Ordering::Relaxed); trace(&format!("started {prog} {args}")); }
        Err(e) => trace(&format!("cannot start {prog}: {e}")),
    }
}

unsafe fn reader(stop: HANDLE) {
    let Some((ring, event, _)) = shared() else { return };
    let hs = [stop, event];
    loop {
        match WaitForMultipleObjects(2, hs.as_ptr(), 0, 2000) {
            WAIT_TIMEOUT => { maybe_launch(ring); continue; }
            r if r == WAIT_OBJECT_0 + 1 => {}
            _ => return,
        }
        loop {
            let tail = (*ring).tail.load(Ordering::Relaxed);
            if tail == (*ring).head.load(Ordering::Acquire) { break; }
            let msg = (*ring).msgs[(tail % CAP) as usize].load(Ordering::Relaxed);
            (*ring).tail.store(tail.wrapping_add(1), Ordering::Release);
            let target = match INPUT.lock() {   // copied out: the program's callback runs with no lock held
                Ok(g) => g.as_ref().filter(|i| i.started).map(|i| (i.client, i.start_tick)),
                Err(_) => None,
            };
            if let Some((client, start)) = target {
                client.notify(MIM_DATA, msg as DWORD_PTR, GetTickCount().wrapping_sub(start) as DWORD_PTR);
                (*ring).stats[ST_DELIVERED].fetch_add(1, Ordering::Relaxed);
            }
        }
    }
}

/// Hands every queued buffer back empty (what midiInReset does); call with INPUT unlocked.
unsafe fn return_buffers(client: Client, buffers: Vec<*mut MIDIHDR>, t: DWORD_PTR) {
    for h in buffers {
        (*h).dwBytesRecorded = 0;
        (*h).dwFlags = ((*h).dwFlags & !MHDR_INQUEUE) | MHDR_DONE;
        client.notify(MIM_LONGDATA, h as DWORD_PTR, t);
    }
}

#[no_mangle]
pub unsafe extern "system" fn midMessage(dev: u32, msg: u32, user: DWORD_PTR, p1: DWORD_PTR, p2: DWORD_PTR) -> DWORD {
    trace(&format!("midMessage dev={dev} msg={msg} user={user:#x} p1={p1:#x} p2={p2:#x}"));
    match msg {
        DRVM_INIT | DRVM_EXIT | DRVM_ENABLE | DRVM_DISABLE => MMSYSERR_NOERROR,
        MIDM_GETNUMDEVS => 1,
        MIDM_GETDEVCAPS => {
            if dev != 0 { return MMSYSERR_BADDEVICEID; }
            let caps = MIDIINCAPSW { wMid: MANUFACTURER, wPid: PRODUCT, vDriverVersion: VERSION, szPname: name32(), dwSupport: 0 };
            let n = (p2 as usize).min(std::mem::size_of::<MIDIINCAPSW>());
            if p1 == 0 || n == 0 { return MMSYSERR_INVALPARAM; }
            ptr::copy_nonoverlapping(&caps as *const _ as *const u8, p1 as *mut u8, n);
            MMSYSERR_NOERROR
        }
        MIDM_OPEN => {
            if dev != 0 { return MMSYSERR_BADDEVICEID; }
            if p1 == 0 { return MMSYSERR_INVALPARAM; }
            let Ok(mut g) = INPUT.lock() else { return MMSYSERR_ERROR };
            if g.is_some() { return MMSYSERR_ALLOCATED; }
            let Some((ring, _, _)) = shared() else { return MMSYSERR_ERROR };
            (*ring).tail.store((*ring).head.load(Ordering::Acquire), Ordering::Release);   // forget what was sent while nobody listened
            let d = &*(p1 as *const MIDIOPENDESC);
            let client = Client { cb: d.dwCallback, flags: ((p2 as DWORD) & CALLBACK_TYPEMASK) >> 16, hmidi: d.hMidi, instance: d.dwInstance };
            let stop = CreateEventW(ptr::null(), 1, 0, ptr::null());
            if stop.is_null() { return MMSYSERR_ERROR; }
            let stop_u = stop as usize;
            let thread = std::thread::Builder::new().name("TS404 Panel MIDI".into()).spawn(move || reader(stop_u as HANDLE)).ok();
            if thread.is_none() { CloseHandle(stop); return MMSYSERR_ERROR; }
            *g = Some(Input { client, started: false, start_tick: GetTickCount(), buffers: Vec::new(), stop, thread });
            (*ring).stats[ST_OPEN].store(1, Ordering::Relaxed);
            (*ring).stats[ST_OPENS].fetch_add(1, Ordering::Relaxed);
            if user != 0 { *(user as *mut DWORD_PTR) = 1; }
            let c = client;
            drop(g);
            c.notify(MIM_OPEN, 0, 0);
            MMSYSERR_NOERROR
        }
        MIDM_CLOSE => {
            let Ok(mut g) = INPUT.lock() else { return MMSYSERR_ERROR };
            let Some(mut inp) = g.take() else { return MMSYSERR_INVALHANDLE };
            drop(g);
            // a program may close without resetting first (FL does): hand its buffers back instead of refusing,
            // or its next open would fail with MMSYSERR_ALLOCATED and the port would be dead for it
            let t = GetTickCount().wrapping_sub(inp.start_tick) as DWORD_PTR;
            return_buffers(inp.client, std::mem::take(&mut inp.buffers), t);
            if let Some((ring, _, _)) = shared() {
                (*ring).stats[ST_OPEN].store(0, Ordering::Relaxed);
                (*ring).stats[ST_STARTED].store(0, Ordering::Relaxed);
                (*ring).stats[ST_CLOSES].fetch_add(1, Ordering::Relaxed);
            }
            SetEvent(inp.stop);
            // the reader may be inside the program's own callback right now: wait for it a little,
            // never forever (a join here from the program's main thread could deadlock it)
            if let Some(t) = inp.thread.take() {
                use std::os::windows::io::AsRawHandle;
                if WaitForSingleObject(t.as_raw_handle() as HANDLE, 3000) == WAIT_OBJECT_0 { let _ = t.join(); } else { trace("reader thread did not stop in time; detached"); }
            }
            CloseHandle(inp.stop);
            inp.client.notify(MIM_CLOSE, 0, 0);
            MMSYSERR_NOERROR
        }
        MIDM_ADDBUFFER => {
            let Ok(mut g) = INPUT.lock() else { return MMSYSERR_ERROR };
            let Some(inp) = g.as_mut() else { return MMSYSERR_INVALHANDLE };
            if p1 == 0 { return MMSYSERR_INVALPARAM; }
            let h = p1 as *mut MIDIHDR;
            (*h).dwFlags = ((*h).dwFlags & !MHDR_DONE) | MHDR_INQUEUE;
            (*h).dwBytesRecorded = 0;
            inp.buffers.push(h);
            MMSYSERR_NOERROR
        }
        MIDM_PREPARE | MIDM_UNPREPARE => MMSYSERR_NOTSUPPORTED,   // winmm does the standard preparation itself
        MIDM_RESET => {
            let Ok(mut g) = INPUT.lock() else { return MMSYSERR_ERROR };
            let Some(inp) = g.as_mut() else { return MMSYSERR_INVALHANDLE };
            inp.started = false;
            let (client, t) = (inp.client, GetTickCount().wrapping_sub(inp.start_tick) as DWORD_PTR);
            let buffers = std::mem::take(&mut inp.buffers);
            drop(g);
            return_buffers(client, buffers, t);
            MMSYSERR_NOERROR
        }
        MIDM_START => {
            let Ok(mut g) = INPUT.lock() else { return MMSYSERR_ERROR };
            let Some(inp) = g.as_mut() else { return MMSYSERR_INVALHANDLE };
            if !inp.started { inp.started = true; inp.start_tick = GetTickCount(); }
            if let Some((ring, _, _)) = shared() { (*ring).stats[ST_STARTED].store(1, Ordering::Relaxed); }
            MMSYSERR_NOERROR
        }
        MIDM_STOP => {
            let Ok(mut g) = INPUT.lock() else { return MMSYSERR_ERROR };
            let Some(inp) = g.as_mut() else { return MMSYSERR_INVALHANDLE };
            inp.started = false;
            if let Some((ring, _, _)) = shared() { (*ring).stats[ST_STARTED].store(0, Ordering::Relaxed); }
            MMSYSERR_NOERROR
        }
        _ => MMSYSERR_NOTSUPPORTED,
    }
}

// ----------------------------------------------------------------------------- output device (any number of opens)
#[no_mangle]
pub unsafe extern "system" fn modMessage(dev: u32, msg: u32, user: DWORD_PTR, p1: DWORD_PTR, p2: DWORD_PTR) -> DWORD {
    trace(&format!("modMessage dev={dev} msg={msg} user={user:#x} p1={p1:#x} p2={p2:#x}"));
    match msg {
        DRVM_INIT | DRVM_EXIT | DRVM_ENABLE | DRVM_DISABLE => MMSYSERR_NOERROR,
        MODM_GETNUMDEVS => 1,
        MODM_GETDEVCAPS => {
            if dev != 0 { return MMSYSERR_BADDEVICEID; }
            let caps = MIDIOUTCAPSW { wMid: MANUFACTURER, wPid: PRODUCT, vDriverVersion: VERSION, szPname: name32(),
                                      wTechnology: MOD_MIDIPORT, wVoices: 0, wNotes: 0, wChannelMask: 0xFFFF, dwSupport: 0 };
            let n = (p2 as usize).min(std::mem::size_of::<MIDIOUTCAPSW>());
            if p1 == 0 || n == 0 { return MMSYSERR_INVALPARAM; }
            ptr::copy_nonoverlapping(&caps as *const _ as *const u8, p1 as *mut u8, n);
            MMSYSERR_NOERROR
        }
        MODM_OPEN => {
            if dev != 0 { return MMSYSERR_BADDEVICEID; }
            if p1 == 0 { return MMSYSERR_INVALPARAM; }
            if shared().is_none() { return MMSYSERR_ERROR; }
            let d = &*(p1 as *const MIDIOPENDESC);
            let c = Box::new(Client { cb: d.dwCallback, flags: ((p2 as DWORD) & CALLBACK_TYPEMASK) >> 16, hmidi: d.hMidi, instance: d.dwInstance });
            c.notify(MOM_OPEN, 0, 0);
            if user != 0 { *(user as *mut DWORD_PTR) = Box::into_raw(c) as DWORD_PTR; }
            MMSYSERR_NOERROR
        }
        MODM_CLOSE => {
            if user == 0 { return MMSYSERR_INVALHANDLE; }
            let c = Box::from_raw(user as *mut Client);
            c.notify(MOM_CLOSE, 0, 0);
            MMSYSERR_NOERROR
        }
        MODM_DATA => { if push(p1 as u32) { MMSYSERR_NOERROR } else { MMSYSERR_ERROR } }
        MODM_LONGDATA => {   // SysEx: taken and dropped (the panel never sends any, and FL 2026 must never receive any)
            if user == 0 { return MMSYSERR_INVALHANDLE; }
            if p1 == 0 { return MMSYSERR_INVALPARAM; }
            let h = p1 as *mut MIDIHDR;
            (*h).dwFlags = ((*h).dwFlags & !MHDR_INQUEUE) | MHDR_DONE;
            (*(user as *const Client)).notify(MOM_DONE, p1, 0);
            MMSYSERR_NOERROR
        }
        MODM_PREPARE | MODM_UNPREPARE => MMSYSERR_NOTSUPPORTED,
        MODM_RESET => MMSYSERR_NOERROR,
        MODM_GETVOLUME | MODM_SETVOLUME | MODM_CACHEPATCHES | MODM_CACHEDRUMPATCHES => MMSYSERR_NOTSUPPORTED,
        _ => MMSYSERR_NOTSUPPORTED,
    }
}

// ----------------------------------------------------------------------------- driver entry
#[no_mangle]
pub unsafe extern "system" fn DriverProc(id: DWORD_PTR, hdrvr: HANDLE, msg: u32, l1: LPARAM, l2: LPARAM) -> LRESULT {
    trace(&format!("DriverProc id={id:#x} msg={msg} l1={l1:#x} l2={l2:#x}"));
    match msg {
        DRV_LOAD | DRV_ENABLE | DRV_OPEN => 1,
        DRV_CLOSE | DRV_DISABLE | DRV_FREE => 1,
        DRV_QUERYCONFIGURE => 0,   // nothing to configure
        DRV_CONFIGURE => DRVCNF_OK,
        DRV_INSTALL | DRV_REMOVE => DRVCNF_OK,
        _ => DefDriverProc(id, hdrvr, msg, l1, l2),
    }
}
