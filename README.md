<p align="center"><img src="brand/icon_512.png" width="128" alt=""></p>

<h1 align="center">404ever</h1>

<p align="center"><b>The TS404's window, back in FL Studio.</b><br>
FL Studio 2025 and 2026 dropped the TS404's window but still play it. This puts a window back.</p>

## What it does

Open a TS404 channel in FL Studio 2025 or 2026 and its plugin tab is empty: just a note that the
TS404 is deprecated. With 404ever installed, that tab shows the TS404's knobs again: oscillators,
envelope, filter, LFO, distortion, FM and ring mod. They drive FL's own TS404 engine, the one that still
plays your old projects, so nothing in the project changes and nothing needs to be replaced.

- **Inside the plugin window**: the panel is a real part of FL's TS404 window. It moves, resizes,
  hides with the other tabs and closes with the window, like any plugin.
- **Live both ways**: turn a knob and FL's value changes (with FL's hint and automation recording);
  change it in FL (automation, events, a controller) and the knob follows.
- **Right-click a knob**: create an automation clip, link to a controller, edit events, like FL's own knobs.
  **Middle-click** a knob or button to reset it to FL's default for a new TS404.
- **Free-standing window**: the Start menu entry opens the panel on its own; it follows the selected TS404 channel.

## Install

Download `404ever-Setup-x.y.z.exe` from [Releases](../../releases) and run it (it asks for admin rights
once: the MIDI port it adds is a system-wide driver entry). Close FL Studio first. The only other thing it
needs is Microsoft Edge WebView2, which is part of Windows; the setup tells you if it is missing. Inside FL the port and controller script are called "TS404 Panel".

Windows sometimes drops third-party MIDI driver entries when it reinstalls audio devices (after a crash or a
driver update). 404ever checks for that at startup, at logon and every 15 minutes and puts its port back by itself;
if the TS404 window is ever empty, restart FL Studio once.

Then, once, in FL Studio:

1. Restart FL Studio if it was running.
2. **Options > MIDI settings**: under **Input**, select **TS404 Panel** and switch **Enable** on.
   The controller type picks itself.

Open any TS404 channel: the panel is there.

To add a **new** TS404 (FL 2025+ took it out of the plugin list): press F8 and type `TS404`, or take
"TS404 bassline synthesizer" from Browser > Plugin database > Generators > Synth classic. The panel service
puts that entry back using the preset file FL still ships.

## How it works

FL Studio kept the TS404 engine and all its parameters, only the window code is gone. Four pieces bring it back:

- `midi/` (`ts404_midi.dll`): a user-mode MME MIDI driver, installed into System32 and registered in
  `HKLM\...\Drivers32` like any MIDI driver, that gives every 64-bit program one loopback MIDI port named
  "TS404 Panel". No kernel driver, no
  loopMIDI, nothing to keep running. Short MIDI messages only; SysEx is accepted and dropped.
- `fl/device_TS404Panel.py`: an FL controller script (installed into FL's `Hardware` folder, attached to that
  port). It applies knob moves it receives as CC frames to the TS404 channel, and writes the channel's values
  to a file ~20 times a second while a panel is watching.
- `server.py` (`TS404Panel.exe`): a small local server on 127.0.0.1:8911 that serves the panel page
  (`index.html`), sends its knob moves to the port, and watches FL for open TS404 windows. The FL script
  starts it when a project with a TS404 is open; it quits by itself when nothing needs it.
- `host/` (`ts404_host.dll`): loaded into FL Studio on its main thread, it makes the panel a WebView2 child
  window of the TS404 window's plugin tab. Everything is done on FL's own thread, so no windows are ever
  parented across processes.

Commands travel as plain MIDI CCs, never SysEx: incoming SysEx deadlocks FL 2026's main thread.

## Build

Needs Rust, Python 3 with `pyinstaller` and `pillow`, and Inno Setup 6.

    build.bat

writes `installer\Output\404ever-Setup-<version>.exe`.

## License

MIT. Made by Mickey Perry ([Pafu Igniters](https://soundcloud.com/pafu)).
