# BSP Machinedrum

Use an **Arturia BeatStep Pro** as a hardware front panel for the **Gearmulator Machinedrum**
plugin (Elektron Machinedrum emulation), with steady lights and real-machine behaviour:
step trigs, p-locks (hold step + turn knob), track select, pages, pattern length, tempo.

The MD's own sequencer plays (p-locks live there). The app talks to the plugin only through
the plugin's **MCP server** — no MIDI goes into the plugin from this app — and drives the BSP
over USB MIDI in an undocumented "report mode" where the computer owns the BSP's lights.

> Unofficial. Not affiliated with Arturia, Elektron or the Gearmulator project.
> **Windows support is experimental and untested** (see below).

## What you need

- Arturia BeatStep Pro on stock firmware (tested: 2.0.1.6), in **Control mode**, over USB.
- Gearmulator **Machinedrum** plugin with its **MCP server enabled** (plugin settings).
  The app finds its port in `~/.gearmulator_mcp.json`, which the plugin writes.
- A DAW hosting the plugin (tested: Ableton Live 12 on macOS), set to follow the BSP's clock:
  - Ableton: Settings → Link/Tempo/MIDI → **Sync on** for the BeatStep Pro **input**, off on
    its output; **EXT** on in the transport bar.
  - Live can drop these MIDI settings after a crash: check them first if the MD stays silent.
- The MD plugin window open (the app reads its panel), in grid record.
- macOS: allow the app under System Settings → Privacy & Security → **Accessibility**
  (p-locks hold the real Shift key; the plugin needs it).

## Run

Download the zip from Releases, unzip, open **BSP Machinedrum** (first time on macOS:
right-click → Open, the app is not signed), set the tempo, press **Start**.
**Stop** gives the BSP back its normal mode; if the BSP is ever left in report mode
(frozen-looking UI, lights stay), press **Release BSP**.

From source:

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                 # the window
.venv/bin/python surface.py run 120     # or headless, at 120 BPM
```

Build the app: `./build.sh` (PyInstaller; output in `build/dist/`). Tagging `v*` builds
macOS and Windows zips on GitHub Actions and attaches them to the release.

Each run writes a timestamped log: macOS `~/Library/Logs/BSP Machinedrum/`, Windows
`%LOCALAPPDATA%\BSP Machinedrum\logs\` (last 20 kept). Stalls are logged as `! slow MD read`
or `! light loop stalled`.

## Controls

| BSP | Machinedrum |
|---|---|
| Step tap | toggle a trig (selected track, page shown) |
| Hold steps + turn a knob | p-lock those steps |
| Knobs 1–4 / 9–12 | DATA ENTRY A–H (endless) |
| Knob 7 / knob 16 | LEVEL / track (SOUND wheel) |
| TEMPO knob | tempo, 1 BPM per click |
| Pads | select track (top row = tracks 1–8); pads light like the MD's track lights |
| << >> / CHAN | page back / forward / cycle (like SCALE) |
| SHIFT, then << >> (within 1.5 s) | pattern length: one page less / more (16–64) |
| LST STEP, then a step | last step, inside the page shown (total length stays) |
| REC / PLAY / STOP | MD RECORD, start / stop |

The step lights mirror the MD's step LEDs (green = playhead), the pads its track lights.

## How it works (short)

- **BSP report mode**: hidden global 125 ≠ 0 makes the BSP report every control as CC on
  channel 1 and leave its LEDs to the host (SysEx param 0x10: steps `0x30+n`, pads `0x70+n`).
  Knobs are 0–127 counters in steps of 8; SysEx `7D 7D 09` resets them all to 64, which makes
  them endless. That reset **locks the BSP** if clock ticks arrive right around it or LED writes
  right after it, so the app pauses the clock 25 ms either side and the LEDs for 0.25 s.
- **Clock**: the app is the master clock; the BSP follows (Auto-sync, switched on at start)
  and passes it to the DAW, which clocks the MD.
- **MD**: buttons are clicks on the plugin's UI elements over MCP; the lights are read from
  the plugin's UI (step LEDs, page dots, track LEDs) in one query per frame. Pattern length is
  set through Scale Setup, checking the cursor on a screenshot of the MD's LCD before any
  UP/DOWN.

## Known limits

- Report mode reports presses only, no releases: "hold" gestures are press-then-act windows.
- Pad lights are on/off only (no brightness); the selected track and a trig look the same.
- The BSP's own display is frozen while in report mode.
- When LST STEP lands on an earlier page, the exact step is not read back (it relies on the
  MD taking every DOWN press, which held in all tests).
- Windows: the code paths exist (Shift key, foreground window, port names) but were never run.
  Windows MIDI ports are often exclusive to one app, and both this app and the DAW open the
  BSP, which may not work without Windows MIDI Services.

## Licence

MIT, see `LICENSE`.
