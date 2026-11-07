//! ts404_host.dll - runs INSIDE FL Studio (FL64.exe), on its main thread.
//!
//! FL 2026 kept the TS404 engine but deleted its window: the plugin tab of a TS404
//! channel is an empty TVectorSheet with a "deprecated" label. server.py loads this
//! DLL into FL with a WH_GETMESSAGE hook on FL's main thread; the hook proc runs
//! `init` once, which creates a message-only window named "TS404Host". The server
//! then sends it WM_COPYDATA text commands:
//!
//!   host <sheet hwnd> <url>   put a WebView2 panel (index.html) into that sheet
//!   drop <sheet hwnd>         remove it
//!   ping                      -> 1000 + number of panels
//!
//! The panel is a real child window of the sheet, so Windows keeps it aligned,
//! clipped and stacked with the plugin window; FL hides it with the tab and closes
//! it with the window. Everything here runs on FL's thread, so nothing is ever
//! parented across processes (no attached input queues).
//!
//! Safety rules: every entry point is wrapped in catch_unwind (a panic must never
//! unwind into FL), errors go to host.log next to the DLL, and the DLL pins itself
//! so unhooking never unloads it while windows still exist.
#![allow(non_snake_case)]

use std::cell::RefCell;
use std::collections::HashMap;
use std::ffi::c_void;
use std::io::Write;
use std::panic::{catch_unwind, AssertUnwindSafe};

use webview2_com::Microsoft::Web::WebView2::Win32::*;
use webview2_com::{
    CreateCoreWebView2ControllerCompletedHandler, CreateCoreWebView2EnvironmentCompletedHandler,
};
use windows::core::{w, Interface, PCWSTR};
use windows::Win32::Foundation::*;
use windows::Win32::Graphics::Gdi::*;
use windows::Win32::System::Com::*;
use windows::Win32::System::DataExchange::COPYDATASTRUCT;
use windows::Win32::System::LibraryLoader::*;
use windows::Win32::UI::WindowsAndMessaging::*;

const HOST_CLASS: PCWSTR = w!("TS404HostWnd");
const PANEL_CLASS: PCWSTR = w!("TS404Panel");
const TIMER_FIT: usize = 1;
const BG: (u8, u8, u8) = (0x34, 0x3b, 0x42); // index.html --bg

struct Panel {
    sheet: HWND,
    url: String,
    controller: Option<ICoreWebView2Controller>,
    size: (i32, i32),
    visible: Option<bool>,
}

struct State {
    hmod: HMODULE,
    host: HWND,
    env: Option<ICoreWebView2Environment>,
    env_requested: bool,
    panels: HashMap<isize, Panel>, // child hwnd -> panel
    pending: Vec<isize>,           // children waiting for the environment
}

thread_local! {
    static STATE: RefCell<Option<State>> = const { RefCell::new(None) };
}

static mut LOG_PATH: Option<std::path::PathBuf> = None;

fn log(msg: &str) {
    // SAFETY: only written once in init on the UI thread before any other use
    let path = unsafe { (*std::ptr::addr_of!(LOG_PATH)).clone() };
    if let Some(p) = path {
        if let Ok(mut f) = std::fs::OpenOptions::new().append(true).create(true).open(p) {
            let t = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs())
                .unwrap_or(0);
            let _ = writeln!(f, "{} {}", t, msg);
        }
    }
}

/// %LOCALAPPDATA%\TS404 Panel: the log and WebView2's cache (shared with server.py's DATA_DIR)
fn data_dir() -> std::path::PathBuf {
    let base = std::env::var("LOCALAPPDATA").unwrap_or_else(|_| std::env::temp_dir().to_string_lossy().into_owned());
    std::path::PathBuf::from(base).join("TS404 Panel")
}

fn with_state<R>(f: impl FnOnce(&mut State) -> R) -> Option<R> {
    STATE.with(|s| s.borrow_mut().as_mut().map(f))
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

// ------------------------------------------------------------------ entry
/// Hook procedure for WH_GETMESSAGE on FL's main thread. Installed by server.py; its only
/// job is to get this DLL loaded and initialised on that thread.
#[no_mangle]
pub unsafe extern "system" fn ts404_hook(code: i32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    let _ = catch_unwind(|| init());
    CallNextHookEx(None, code, wparam, lparam)
}

fn init() {
    if STATE.with(|s| s.borrow().is_some()) {
        return;
    }
    unsafe {
        // keep the DLL resident after the hook is removed
        let mut hmod = HMODULE::default();
        let _ = GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_PIN,
            PCWSTR(ts404_hook as *const u16),
            &mut hmod,
        );
        let data = data_dir();
        let _ = std::fs::create_dir_all(&data);
        LOG_PATH = Some(data.join("host.log"));
        std::panic::set_hook(Box::new(|info| log(&format!("PANIC {}", info))));

        // FL's thread is normally an STA already; S_FALSE/RPC_E_CHANGED_MODE are fine
        let _ = CoInitializeEx(None, COINIT_APARTMENTTHREADED);

        let hinst = HINSTANCE(hmod.0);
        for (class, proc_) in [(HOST_CLASS, Some(host_wndproc as unsafe extern "system" fn(HWND, u32, WPARAM, LPARAM) -> LRESULT)), (PANEL_CLASS, Some(panel_wndproc as unsafe extern "system" fn(HWND, u32, WPARAM, LPARAM) -> LRESULT))] {
            let wc = WNDCLASSW {
                lpfnWndProc: proc_,
                hInstance: hinst,
                lpszClassName: class,
                hbrBackground: HBRUSH(std::ptr::null_mut()),
                ..Default::default()
            };
            RegisterClassW(&wc); // 0 if already registered: fine
        }
        let host = match CreateWindowExW(
            WINDOW_EX_STYLE(0),
            HOST_CLASS,
            w!("TS404Host"),
            WINDOW_STYLE(0),
            0,
            0,
            0,
            0,
            Some(HWND_MESSAGE),
            None,
            Some(hinst),
            None,
        ) {
            Ok(h) => h,
            Err(e) => {
                log(&format!("host window failed: {e}"));
                return;
            }
        };
        SetTimer(Some(host), TIMER_FIT, 250, None);
        STATE.with(|s| {
            *s.borrow_mut() = Some(State {
                hmod,
                host,
                env: None,
                env_requested: false,
                panels: HashMap::new(),
                pending: Vec::new(),
            })
        });
        log(&format!("init ok, host window {:?}", host));
    }
}

// ------------------------------------------------------------------ host window
unsafe extern "system" fn host_wndproc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match catch_unwind(AssertUnwindSafe(|| host_message(hwnd, msg, wparam, lparam))) {
        Ok(Some(r)) => r,
        Ok(None) => DefWindowProcW(hwnd, msg, wparam, lparam),
        Err(_) => LRESULT(0),
    }
}

unsafe fn host_message(_hwnd: HWND, msg: u32, _wparam: WPARAM, lparam: LPARAM) -> Option<LRESULT> {
    match msg {
        WM_COPYDATA => {
            let cds = &*(lparam.0 as *const COPYDATASTRUCT);
            let bytes = std::slice::from_raw_parts(cds.lpData as *const u8, cds.cbData as usize);
            let text = String::from_utf8_lossy(bytes).into_owned();
            Some(LRESULT(command(&text)))
        }
        WM_TIMER => {
            fit_all();
            Some(LRESULT(0))
        }
        _ => None,
    }
}

fn command(text: &str) -> isize {
    let mut it = text.trim().splitn(3, ' ');
    let cmd = it.next().unwrap_or("");
    let arg = it.next().unwrap_or("");
    let rest = it.next().unwrap_or("");
    match cmd {
        "ping" => 1000 + with_state(|s| s.panels.len() as isize).unwrap_or(-1000),
        "host" => {
            let sheet = HWND(arg.parse::<isize>().unwrap_or(0) as *mut c_void);
            if sheet.0.is_null() || rest.is_empty() || !unsafe { IsWindow(Some(sheet)) }.as_bool() {
                return 0;
            }
            host_panel(sheet, rest.to_string())
        }
        "drop" => {
            let sheet = arg.parse::<isize>().unwrap_or(0);
            let child = with_state(|s| {
                s.panels.iter().find(|(_, p)| p.sheet.0 as isize == sheet).map(|(h, _)| *h)
            })
            .flatten();
            if let Some(h) = child {
                unsafe {
                    let _ = DestroyWindow(HWND(h as *mut c_void));
                }
                1
            } else {
                0
            }
        }
        _ => 0,
    }
}

fn host_panel(sheet: HWND, url: String) -> isize {
    // one panel per sheet; a new URL just navigates the existing one
    let existing = with_state(|s| {
        s.panels
            .iter_mut()
            .find(|(_, p)| p.sheet == sheet)
            .map(|(h, p)| (*h, p.url == url, p.controller.clone()))
    })
    .flatten();
    if let Some((h, same, ctl)) = existing {
        if !same {
            with_state(|s| {
                if let Some(p) = s.panels.get_mut(&h) {
                    p.url = url.clone();
                }
            });
            if let Some(ctl) = ctl {
                unsafe {
                    if let Ok(wv) = ctl.CoreWebView2() {
                        let _ = wv.Navigate(PCWSTR(wide(&url).as_ptr()));
                    }
                }
            }
        }
        return 1;
    }
    let hinst = match with_state(|s| HINSTANCE(s.hmod.0)) {
        Some(h) => h,
        None => return 0,
    };
    let child = unsafe {
        let mut rc = RECT::default();
        let _ = GetClientRect(sheet, &mut rc);
        match CreateWindowExW(
            WINDOW_EX_STYLE(0),
            PANEL_CLASS,
            w!("TS404"),
            WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN | WS_CLIPSIBLINGS,
            0,
            0,
            rc.right,
            rc.bottom,
            Some(sheet),
            None,
            Some(hinst),
            None,
        ) {
            Ok(h) => h,
            Err(e) => {
                log(&format!("panel window failed: {e}"));
                return 0;
            }
        }
    };
    let key = child.0 as isize;
    with_state(|s| {
        s.panels.insert(
            key,
            Panel { sheet, url, controller: None, size: (0, 0), visible: None },
        );
    });
    log(&format!("panel {:?} in sheet {:?}", child, sheet));
    let env = with_state(|s| s.env.clone()).flatten();
    match env {
        Some(env) => create_controller(&env, key),
        None => {
            with_state(|s| s.pending.push(key));
            ensure_env();
        }
    }
    1
}

fn ensure_env() {
    if with_state(|s| std::mem::replace(&mut s.env_requested, true)).unwrap_or(true) {
        return;
    }
    let udf = wide(&data_dir().join("webview2").to_string_lossy());
    let handler = CreateCoreWebView2EnvironmentCompletedHandler::create(Box::new(
        |err: windows_core::Result<()>, env: Option<ICoreWebView2Environment>| {
            let _ = catch_unwind(AssertUnwindSafe(|| match (err, env) {
                (Ok(()), Some(env)) => {
                    let pending = with_state(|s| {
                        s.env = Some(env.clone());
                        std::mem::take(&mut s.pending)
                    })
                    .unwrap_or_default();
                    log(&format!("webview2 environment ready, {} pending", pending.len()));
                    for key in pending {
                        create_controller(&env, key);
                    }
                }
                (e, _) => {
                    log(&format!("webview2 environment failed: {:?}", e));
                    with_state(|s| s.env_requested = false);
                }
            }));
            Ok(())
        },
    ));
    let r = unsafe {
        CreateCoreWebView2EnvironmentWithOptions(PCWSTR::null(), PCWSTR(udf.as_ptr()), None, &handler)
    };
    if let Err(e) = r {
        log(&format!("CreateCoreWebView2EnvironmentWithOptions: {e}"));
        with_state(|s| s.env_requested = false);
    }
}

fn create_controller(env: &ICoreWebView2Environment, key: isize) {
    let child = HWND(key as *mut c_void);
    let handler = CreateCoreWebView2ControllerCompletedHandler::create(Box::new(
        move |err: windows_core::Result<()>, ctl: Option<ICoreWebView2Controller>| {
            let _ = catch_unwind(AssertUnwindSafe(|| controller_ready(key, err, ctl)));
            Ok(())
        },
    ));
    if let Err(e) = unsafe { env.CreateCoreWebView2Controller(child, &handler) } {
        log(&format!("CreateCoreWebView2Controller: {e}"));
    }
}

fn controller_ready(key: isize, err: windows_core::Result<()>, ctl: Option<ICoreWebView2Controller>) {
    let child = HWND(key as *mut c_void);
    let ctl = match (err, ctl) {
        (Ok(()), Some(c)) => c,
        (e, _) => {
            log(&format!("controller failed: {:?}", e));
            return;
        }
    };
    let url = with_state(|s| s.panels.get(&key).map(|p| p.url.clone())).flatten();
    let url = match url {
        Some(u) if unsafe { IsWindow(Some(child)) }.as_bool() => u,
        _ => {
            // the panel went away while the controller was being made
            unsafe {
                let _ = ctl.Close();
            }
            return;
        }
    };
    unsafe {
        if let Ok(c2) = ctl.cast::<ICoreWebView2Controller2>() {
            let _ = c2.SetDefaultBackgroundColor(COREWEBVIEW2_COLOR { A: 255, R: BG.0, G: BG.1, B: BG.2 });
        }
        let mut rc = RECT::default();
        let _ = GetClientRect(child, &mut rc);
        let _ = ctl.SetBounds(rc);
        if let Ok(wv) = ctl.CoreWebView2() {
            if let Ok(st) = wv.Settings() {
                let _ = st.SetAreDefaultContextMenusEnabled(false);
                let _ = st.SetIsStatusBarEnabled(false);
                let _ = st.SetIsZoomControlEnabled(false);
                let _ = st.SetAreDevToolsEnabled(true);
            }
            let _ = wv.Navigate(PCWSTR(wide(&url).as_ptr()));
        }
    }
    with_state(|s| {
        if let Some(p) = s.panels.get_mut(&key) {
            p.controller = Some(ctl);
        }
    });
    log(&format!("panel {:?} navigated to {}", child, url));
}

/// Keep every panel the size of its sheet (FL resizes the sheet with the plugin window)
/// and tell WebView2 when a panel is hidden with its tab / window, so it stops rendering.
fn fit_all() {
    let items: Vec<(isize, HWND, Option<ICoreWebView2Controller>, (i32, i32), Option<bool>)> = with_state(|s| {
        s.panels
            .iter()
            .map(|(k, p)| (*k, p.sheet, p.controller.clone(), p.size, p.visible))
            .collect()
    })
    .unwrap_or_default();
    for (key, sheet, ctl, size, vis) in items {
        let child = HWND(key as *mut c_void);
        unsafe {
            if !IsWindow(Some(child)).as_bool() {
                with_state(|s| s.panels.remove(&key));
                continue;
            }
            let mut rc = RECT::default();
            let _ = GetClientRect(sheet, &mut rc);
            if (rc.right, rc.bottom) != size && rc.right > 0 && rc.bottom > 0 {
                let _ = MoveWindow(child, 0, 0, rc.right, rc.bottom, true);
                with_state(|s| {
                    if let Some(p) = s.panels.get_mut(&key) {
                        p.size = (rc.right, rc.bottom);
                    }
                });
            }
            let now = IsWindowVisible(child).as_bool();
            if vis != Some(now) {
                if let Some(c) = &ctl {
                    let _ = c.SetIsVisible(now);
                }
                with_state(|s| {
                    if let Some(p) = s.panels.get_mut(&key) {
                        p.visible = Some(now);
                    }
                });
            }
        }
    }
}

// ------------------------------------------------------------------ panel window
unsafe extern "system" fn panel_wndproc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match catch_unwind(AssertUnwindSafe(|| panel_message(hwnd, msg, wparam, lparam))) {
        Ok(Some(r)) => r,
        Ok(None) => DefWindowProcW(hwnd, msg, wparam, lparam),
        Err(_) => LRESULT(0),
    }
}

unsafe fn panel_message(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> Option<LRESULT> {
    let key = hwnd.0 as isize;
    match msg {
        WM_SIZE => {
            let rc = RECT { left: 0, top: 0, right: (lparam.0 & 0xFFFF) as i32, bottom: ((lparam.0 >> 16) & 0xFFFF) as i32 };
            if let Some(Some(c)) = with_state(|s| s.panels.get(&key).and_then(|p| p.controller.clone())) {
                let _ = c.SetBounds(rc);
            }
            Some(LRESULT(0))
        }
        WM_ERASEBKGND => {
            let hdc = HDC(wparam.0 as *mut c_void);
            let mut rc = RECT::default();
            let _ = GetClientRect(hwnd, &mut rc);
            let brush = CreateSolidBrush(COLORREF((BG.2 as u32) << 16 | (BG.1 as u32) << 8 | BG.0 as u32));
            FillRect(hdc, &rc, brush);
            let _ = DeleteObject(brush.into());
            Some(LRESULT(1))
        }
        WM_DESTROY => {
            if let Some(Some(p)) = with_state(|s| s.panels.remove(&key)) {
                if let Some(c) = p.controller {
                    let _ = c.Close();
                }
                log(&format!("panel {:?} destroyed", hwnd));
            }
            Some(LRESULT(0))
        }
        _ => None,
    }
}
