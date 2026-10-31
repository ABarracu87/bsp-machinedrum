# BSP Machinedrum

Use an **Arturia BeatStep Pro** as a hardware front panel for the Gearmulator **Machinedrum**
and **Monomachine** plugins (Elektron emulations), with steady lights and real-machine
behaviour: step trigs, p-locks (hold step + turn knob), track select, pages, pattern length,
mute windows, tempo, and a pad keyboard for the Monomachine.

The machines' own sequencers play (p-locks live there). The app talks to the plugins only
through each plugin's **MCP server** (no MIDI goes into the plugins from this app) and drives
the BSP over USB MIDI in an undocumented "report mode" where the computer owns the BSP's lights.

> Unofficial. Not affiliated with Arturia, Elektron or the Gearmulator project.
> **Windows support is experimental and untested** (see below).

## What you need

- Arturia BeatStep Pro on stock firmware (tested: 2.0.1.6), in **Control mode**, over USB,
  on an **empty project/pattern** (its own sequencer notes would play the plugins too).
- Gearmulator **Machinedrum** and/or **Monomachine** plugin, each with its **MCP server
  enabled** (plugin settings; the setting is per computer). The app finds the ports in
  `~/.gearmulator_mcp.json`, which the plugins write.
- Each plugin's **window open** while playing: the app reads and clicks the panel, and the
  MCP server answers only while the window is open.
- macOS: allow the app under System Settings → Privacy & Security → **Accessibility**
  (p-locks hold the real Shift key; the plugin needs it).

### Ableton Live setup (tested: Live 12 on macOS)

- Settings → Link/Tempo/MIDI, BeatStep Pro **input**: **Sync on**, Track off, Remote off.
  BeatStep Pro **output**: Sync off.
- **EXT** on in the transport bar: Live follows the BSP's clock.
- The plugin tracks: **MIDI From = No Input**.
- Live can drop these MIDI settings after a crash: check them first if a machine stays silent.

### Monomachine plugin setup

- GLOBAL → CONTROL → CONTROL IN: **TEMPO SYNC = EXTERNAL**, **TRANSPORT = ACCEPT**.
  Without it the MM does not play when you press PLAY on the BSP.
- Notes reach the MM on its AUTO TRACK channel (default 9).

## Run

Download the zip from Releases, unzip, open **BSP Machinedrum** (first time on macOS:
right-click → Open, the app is not notarised), set the tempo, press **Start**.

Order: open Live with the plugin windows first, then press Start. A plugin opened after
Start is picked up when you press its button (DRUM / SEQ1).

**Stop** gives the BSP back its normal mode; if the BSP is ever left in report mode
(frozen-looking UI, lights stay), press **Release BSP**.

From source:

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py                 # the window
.venv/bin/python surface.py run 120     # or headless, at 120 BPM
.venv/bin/python surface.py test        # self-check
```

Build the app: `./build.sh` with the venv's Python on PATH (PyInstaller; output in
`build/dist/`). Tagging `v*` builds macOS and Windows zips on GitHub Actions and attaches
them to the release.

Each run writes a timestamped log: macOS `~/Library/Logs/BSP Machinedrum/`, Windows
`%LOCALAPPDATA%\BSP Machinedrum\logs\` (last 20 kept). Stalls are logged as `! slow MD read`
or `! light loop stalled`; input the app does not expect as `? unexpected MIDI from the BSP`.

## Controls

### Both machines

| BSP | Action |
|---|---|
| DRUM / SEQ1 | drive the Machinedrum / the Monomachine (the button of the machine in use is lit) |
| Step tap | toggle a trig (selected track, page shown) |
| Hold steps + turn a knob | p-lock those steps (ends 6 s after the last turn, or on a step press) |
| Knobs 1–4 / 9–12 | DATA ENTRY A–H (endless) |
| Knob 7 | LEVEL (track volume) |
| Knob 8 | parameter page down / up (MD SYN → FX → ROUT; MM DATA PAGE) |
| TEMPO knob | tempo, 1 BPM per click |
| MD mute / MM mute | open that machine's **mute window**: the step lights show the tracks (lit = playing), a step toggles that track; press again to close (the button is lit while open) |
| REC / PLAY / STOP | RECORD, start / stop |

The playhead is white.

### Machinedrum (DRUM)

| BSP | Machinedrum |
|---|---|
| Knob 16 | track (SOUND wheel) |
| Pads | select track (top row = tracks 1–8); pads light like the MD's track lights |
| << >> / CHAN | page back / forward / cycle (like SCALE) |
| Right arrow above the drum channel button | LFO page on / off |
| SHIFT, then << >> (within 1.5 s) | pattern length: one page less / more (16–64) |
| LST STEP, then a step | last step, inside the page shown (total length stays) |
| Mute window | steps 1–16 = tracks 1–16 |

### Monomachine (SEQ1)

| BSP | Monomachine |
|---|---|
| Knob 16 | track up / down |
| Pads | one-octave keyboard: bottom row C D E F G A B C, top row C# D# · F# G# A#; a held pad is dark |
| Top-row pads 7 / 8 | octave down / up |
| Step, then a pad (within 0.5 s) | that trig's note |
| SHIFT, then pads 1–6 | select track 1–6 |
| Step lights | red = note trig, yellow = note off, green = trigless (as the MM shows them) |
| Mute window | steps 1–6 = MM tracks, steps 9–14 = MIDI sequencer tracks |

## How it works (short)

- **BSP report mode**: hidden global 125 ≠ 0 makes the BSP report every control as CC on
  channel 1 and leave its LEDs to the host (SysEx param 0x10: steps `0x30+n`, pads `0x70+n`,
  buttons by their id). Knobs are 0–127 counters in steps of 8; SysEx `7D 7D 09` resets them
  all to 64, which makes them endless. That reset **locks the BSP** if clock ticks arrive
  right around it or LED writes right after it, so the app pauses the clock 25 ms either side
  and the LEDs for 0.25 s, and it only resets with **no hand on any knob** (the knobs report
  touch and release).
- **Clock**: the app is the master clock; the BSP follows (Auto-sync, switched on at start)
  and passes it to the DAW, which clocks the plugins.
- **Plugins**: buttons are clicks on the plugins' UI elements over MCP; the lights are read
  from the plugin's UI (step LEDs, page dots, track LEDs) in one query per frame. MM notes go
  in as MCP MIDI messages. MD pattern length is set through Scale Setup, checking the cursor
  on a screenshot of the MD's LCD before any UP/DOWN.
- **Freeze alarm**: the app asks the BSP for a setting every 5 s and after each knob reset.
  No answer: a red warning, the clock stops, and the app reconnects after a power cycle.
  An answer saying report mode is off: the app switches it back on.

## Known limits

- Report mode reports presses only for steps and buttons, no releases: "hold" gestures are
  press-then-act windows.
- Pad lights are on/off only (no brightness).
- The BSP's own display is frozen while in report mode.
- The BSP can still freeze now and then (lights and controls stop). The cause is not known;
  power-cycle it and the app reconnects. Note anything shown on its display.
- The BSP sends a CC on channel 16 for PLAY / STOP even in report mode; the app ignores it.
  Keep Remote off for the BSP input in Live, or those CCs may trigger MIDI mappings.
- Monomachine: pattern length (SHIFT + arrows, LST STEP) and trigless trigs are not
  available from the BSP yet; the playhead is counted from the clock (the MM's step lights
  do not show it).
- When LST STEP lands on an earlier MD page, the exact step is not read back.
- Windows: the code paths exist (Shift key, foreground window, port names) but were never run.
  Windows MIDI ports are often exclusive to one app, and both this app and the DAW open the
  BSP, which may not work without Windows MIDI Services.

## Licence

MIT, see `LICENSE`.
