"""BeatStep Pro as the Machinedrum's front panel, in the BSP's hidden report mode.

Global 125 != 0 (found by brute force) makes the BSP report every control on channel 1 and
leaves its LEDs to the computer (steady, no flicker). The surface is the master clock; the
BSP follows it (Auto-sync) and passes it to Ableton, which clocks the MD. The MD's own
sequencer plays; the lights mirror the MD's panel, read over the plugin's MCP server.

  python surface.py run [bpm]  # run (report mode on, and off again on exit)
  python surface.py release    # switch report mode off (if the BSP is stuck)
  python surface.py test       # self-check

  step tap                 toggle MD trig (selected track, page shown)
  hold steps + turn a knob  p-lock each held step; ends KNOB_IDLE after the last turn
  knobs 1-4 / 9-12         DATA ENTRY A-H    knob 7 LEVEL   knob 16 track (SOUND wheel)
                           endless: at 0/127 every knob resets to 64 (7D 7D 09)
  TEMPO knob               1 BPM per click
  pads                     select MD track (top row = 1-8); pads light like the MD's
                           track lights (selected steady, trigs flash)
  << >> / CHAN             MD page back/forward / cycle (like SCALE)
  arrow above DRUM chan    MD LFO page on/off
  SHIFT, then << >>        pattern length: a page less/more (16-64)
  LST STEP, then a step    last step, inside the page shown (the total stays)
  REC / PLAY / STOP        MD RECORD, start/stop the clock
"""
import _thread, queue, signal, sys, threading, time, traceback

from mdmm_bsp import MD_NOTES, bsp_ports, knob_slot, pad_track

REPORT = 125                  # hidden global: 0 = normal, 2 = report mode
PRESS, RELEASE = 1, 3         # report CCs: value = control id
STEP0, PAGE_BTN, REC, PLAY, STOP = 0x30, 0x1B, 0x57, 0x58, 0x59   # ids: MCC BeatStepProTest.json
LST_STEP, PREV, NEXT, CHAN = 0x55, 0x52, 0x53, 0x54   # CHAN: id seen live 2026-10-14
SHIFT = 0x56                  # press only, no release (live 2026-10-15)
LFO_BTN = 0x1E                # right arrow above the drum channel button: MD LFO page on/off
DRUM_BTN, SEQ1_BTN = 0x1A, 0x10   # machine select: DRUM = Machinedrum, SEQ1 = Monomachine
MD_MUTE, MM_MUTE = 0x1C, 0x12     # open/close that machine's mute window (2026-10-21)
DATA_PAGE_KNOB = 7            # knob 8: data/parameter pages up/down (both machines)
MM_CH = 8                     # Monomachine AUTO TRACK channel (0-based; manual default 9)
OCTAVE = 4                    # MM pad keyboard start octave: C4 = note 60
# MM pad keyboard, one octave (user layout 2026-10-21): pad 0-15, bottom-left first.
# Bottom row C D E F G A B C', top row C# D# - F# G# A# octave- octave+.
PAD_SEMI = {0: 0, 1: 2, 2: 4, 3: 5, 4: 7, 5: 9, 6: 11, 7: 12, 8: 1, 9: 3, 11: 6, 12: 8, 13: 10}
PAD_OCT_DOWN, PAD_OCT_UP = 14, 15
SHIFT_WINDOW = 1.5            # s: an arrow this soon after SHIFT (or a shifted arrow) = SHIFT+arrow
LEN_ARM = 15.0                # LST STEP gives up after this long (press it again to cancel)
AUTOSYNC = 17                 # global: Auto-sync (the BSP follows an incoming clock)
ACCEL = 4                     # global: knob acceleration (off while we run: 1 per detent)
KNOB0 = 0x20                  # knob k (0-15) turns = CC 0x20+k, touch id = 0x20+k
KNOB_STEP = 8                 # report values per detent (acceleration setting does not change it)
FINE = 0.5                    # wheel per report: 0.5 = ~1 MD step (1.0 trips the plugin's wheel accel)
TAP_WAIT = 0.5                # a step tap waits this long for a knob touch (= p-lock)
KNOB_IDLE = 6.0               # a p-lock ends this long after the last knob turn (user: 2.5 s
                              # expired too soon, 2026-10-17); a step press ends it at once
KNOB_ACCEL = ((0.03, 4), (0.06, 2))   # (s between reports, x) fast turns move further
MIRROR_PERIOD = 0.02          # MD panel reads while playing (one find_elements, ~10 ms)
IDLE_PERIOD = 0.1             # ... while stopped (each read costs Live CPU: 2026-10-15)
EDIT_STEPS = 3                # a LED flip outlasting this many steps is an edit, not the playhead
RAM_PERIOD = 1.0              # MD RAM reads, for the length only (the server ignores ranges,
                              # so each read is the full 1 MiB snapshot)
OFF, RED, GREEN, YELLOW, WHITE = 0x00, 0x01, 0x04, 0x05, 0x15   # step LED: 2 bits each R, G, B
MM_COLOUR = {"red": RED, "yellow": YELLOW, "green": GREEN}       # MM trig light classes
SPECIAL = {6: "encLevel", 15: "encSound"}


SURFACE_CLOCK = True          # True: the surface is the master clock (BSP follows via Auto-sync,
                              # Ableton syncs to the BSP input, EXT on); TEMPO knob sets the BPM.
                              # False: the BSP is master (Auto-sync clock burst, fixed tempo).
TEMPO = 120.0                 # start tempo (surface clock); surface.py run <bpm>
STALL = 0.15                  # s: log a read or light-loop gap longer than this (hiccups)
LED_GAP, LED_CAP, LED_WINDOW = 0.004, 10, 0.1   # LED writes to the BSP: >= 4 ms apart, <= 10
                              # per 100 ms (user: limit what we send back at once, 2026-10-17)
RESET_PRE_QUIET = 0.15        # s without LED writes BEFORE a knob reset
RESET_IDLE = 0.3              # s with no knob report before a knob reset
RESET_MIN_GAP = 0.6           # s between knob resets (stress-tested spacing: 0.5 s)
RESET_QUIET = 0.25            # s without LED writes after a knob reset (reset_stress.py)
TOUCH_HOLD = 5.0              # s a knob touch counts without its release (a lost release
                              # must not block resets for good)
RESET_HUSH = 0.025            # s without clock ticks either side of a knob reset (reset_stress.py)
TEMPO_KNOB = 0x66             # report CC of the big TEMPO encoder (encoder 16)
class Mirror:
    """MD panel step LEDs -> stable trigs + the MD's playhead (live 2026-10-14).
    Stopped, the LEDs are exactly the trigs of the shown page. Playing, the MD flips the
    playhead's LED (an empty step lights, a trig goes dark) and leaves a ~1-step trail:
    the newest flip is the playhead; a flip that outlasts EDIT_STEPS steps is a real edit."""

    def __init__(self):
        self.trigs, self.head, self.key, self.since = 0, None, None, {}
        self.known = {}                         # (track, page) -> trigs last confirmed

    def update(self, lit, key, playing, now, step_time):
        if not playing:                         # stopped: the LEDs are the trigs
            self.key, self.trigs, self.head, self.since = key, lit, None, {}
            self.known[key] = lit
            return
        if key != self.key:                     # switched while playing: the LEDs now carry
            self.key, self.head, self.since = key, None, {}   # the playhead + trail, so
            self.trigs = self.known.get(key, lit)              # start from memory if seen
            return
        diff = lit ^ self.trigs
        for b in range(16):
            if diff >> b & 1:
                if b not in self.since:
                    self.since[b], self.head = now, b
            else:
                self.since.pop(b, None)
        hold = max(0.6, EDIT_STEPS * step_time)
        for b, t in list(self.since.items()):
            if b != self.head and now - t > hold:
                self.trigs ^= 1 << b              # flipped for good: an edit
                del self.since[b]
                self.known[key] = self.trigs


def pick_track(cur, lit):
    """Selected MD track from its track lights. The selected light stays on, also while its
    track plays; other lights only blip 0.07-0.3 s on their trigs (live 2026-10-14, 54 Hz).
    So keep the current track while lit, else take a lone light; several lit and the
    current dark: wait for the blips to end.
    ponytail: a track trigging on every step is never alone - add onset tracking if needed."""
    if cur in lit:
        return cur
    return lit[0] if len(lit) == 1 else cur


def pad_to_track(ch, note):
    """Pad -> MD track 0-15 (top row = tracks 1-8, bottom = 9-16). Both BSP modes send
    notes 36-51 in pad order, bottom-left first (live 2026-10-14): Control mode on
    channel 16, DRUM mode on channel 1 (stock chromatic drum map)."""
    if ch in (0, 15) and 36 <= note < 52:
        return pad_track(note - 36)
    return None


KNOB_RESET = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x7D, 0x7D, 0x09, 0x7F, 0xF7]


REPORT_ON = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT, 2, 0xF7]


def report_mode(mo, on):
    mo.send_message(REPORT_ON[:10] + [2 if on else 0, 0xF7])


def pad_led_msg(pad, on):
    """Pad LED ctrl 0x70 + pad (pad 0 = bottom-left, as the pad notes); on/off only:
    8..127 all light fully (live 2026-10-15)."""
    return [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x10, 0x70 + pad, 0x7F if on else 0, 0xF7]


def button_led_msg(button, on):
    """Button LED, ctrl = the button's report id (DRUM 0x1A, SEQ1 0x10, mutes 0x12/0x1C; live
    2026-10-21; the mute buttons light blue). Never ctrl 0x1F / 0x5F: firmware actions."""
    assert button not in (0x1F, 0x5F)
    return [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x10, button, 0x7F if on else 0, 0xF7]


def track_pad(track):
    """MD track 0-15 -> pad 0-15 (inverse of pad_track: top row = tracks 1-8)."""
    return (track + 8) % 16


def led_msg(step, color):
    return [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x10, 0x30 + step, color, 0xF7]


class Surface:
    """Report decoding and the tap / p-lock gesture. `panel` = panel.Panel (or a fake)."""

    def __init__(self, panel, bsp_send=lambda msg: None, recenter=lambda k: True, panels=None):
        self.panel, self.bsp_send, self.recenter = panel, bsp_send, recenter
        self.panels = panels or {"MD": panel}   # machine -> its Panel (None if not loaded)
        self.machine = next(m for m, p in self.panels.items() if p is panel)
        self.track_of = {"MD": 0, "MM": 0}      # each machine's selected track
        self.mute_mode = None           # machine whose mute window is open: steps = tracks
        self.octave = OCTAVE
        self.mm_notes = {}              # pad -> MM note sounding (released on pad off)
        self.data_page_t = 0.0          # last knob-8 page step (one page per flick)
        self.knob_val = {}              # knob -> last reported value
        self.pad_note = {}              # note -> velocity, until its pressure proves a pad
        self.bpm = TEMPO                # surface clock tempo (TEMPO knob: 1 BPM per detent)
        self.tempo_val = 64             # last TEMPO knob report
        self.holds = {}                 # step -> press time (tap, or p-lock on turn)
        self.auto = {}                  # step -> time of a timeout-tap (slow holds convert)
        self.lock = None                # step being p-locked (blink)
        self.idle_since = None          # last knob turn while locking
        self.page = 0                   # 16-step page shown and edited
        self.toggled = {}               # step -> time: shown at once, before the RAM catches up
        self.length_arm = None          # time of LST STEP: the next step sets the length
        self.shift_at = -1e9            # last SHIFT (or shifted arrow): no releases reported
        self.last_knob = 0.0            # time of the last knob report
        self.odd_at = 0.0               # last unexpected-input log line
        self.knob_t = {}                # knob -> time of its last report (acceleration)
        self.lfo = False                # LFO page opened from the BSP
        self.running = False            # our transport (the BSP ignores Start/Stop in report mode)
        self.md = {"trigs": [0] * 16, "len": 16, "track": 0, "playing": False,   # MD reader
                   "frame": (0, 0, None), "pages": 1, "tracks_lit": []}

    def switch(self, machine):
        """DRUM / SEQ1: the BSP drives the other machine. Holds, locks and the view reset."""
        if machine == self.machine:
            return
        if self.panels.get(machine) is None and not self.connect(machine):
            return                      # (loaded after Start: looked up again on its button)
        if self.lock is not None:
            self.panel.release()
            self.lock = self.idle_since = None
        if self.mute_mode:
            self.panel.mute_window(False)
            self.mute_mode = None
        for pad, n in self.mm_notes.items():
            self.panel.note_off(n)
        self.mm_notes.clear()
        self.track_of[self.machine] = self.md["track"]
        self.machine, self.panel = machine, self.panels[machine]
        self.holds.clear(); self.auto.clear(); self.toggled.clear()
        self.page, self.length_arm, self.lfo = 0, None, False
        self.md.update(track=self.track_of[machine], frame=(0, 0, None), pages=1, len=16,
                       tracks_lit=[], colours=[None] * 16)
        print(f"machine -> {machine}", flush=True)
        self.on_switch(machine)

    def connect(self, machine):
        """run() looks the plugin up again here. -> True when found."""
        print(f"! {machine}: plugin not loaded (or its MCP server is off)", flush=True)
        return False

    def on_switch(self, machine):
        """run() hooks the reader/lights here."""

    def mm_pad(self, pad, velocity, now):
        """Monomachine pad keyboard: octave pads, SHIFT + pad 1-6 = track, a pad just after a
        step = that step's pitch (hold TRIG + key), else play the note live."""
        if pad == PAD_OCT_DOWN or pad == PAD_OCT_UP:
            self.octave = max(0, min(8, self.octave + (1 if pad == PAD_OCT_UP else -1)))
            print(f"MM octave {self.octave}", flush=True)
            return
        if now - self.shift_at < SHIFT_WINDOW and pad < 6:
            self.shift_at = 0
            self.md["track"] = self.track_of["MM"] = pad
            print(f"MM track {pad + 1}: {'ok' if self.panel.select_track(pad) else 'FAILED'}", flush=True)
            return
        if pad not in PAD_SEMI:
            return
        note = 12 * (self.octave + 1) + PAD_SEMI[pad]
        recent = [(st, pg) for st, (t, pg) in self.holds.items() if now - t < TAP_WAIT]
        if recent:                                 # step then pad: the step's pitch
            for st, pg in recent:
                del self.holds[st]
                self.toggled.pop(pg * 16 + st, None)
                if pg == self.page:
                    ok = self.panel.pitch(st, note)
                    print(f"MM step {pg * 16 + st + 1} pitch {note}: {'ok' if ok else 'FAILED'}", flush=True)
            return
        self.mm_notes[pad] = note
        self.panel.note_on(note, velocity)

    def report(self, msg, now):
        st, cc, v = msg[0], msg[1], msg[2] if len(msg) > 2 else 0
        kind, key = st & 0xF0, (st & 0x0F, msg[1])   # (channel, note)
        if self.machine == "MM" and st & 0x0F == 15 and kind in (0x80, 0x90):
            pad = msg[1] - 36                     # Control-mode pads: ch 16, notes 36-51
            if 0 <= pad < 16:
                if kind == 0x90 and v:
                    self.mm_pad(pad, v, now)
                elif pad in self.mm_notes:
                    self.panel.note_off(self.mm_notes.pop(pad))
            return
        if self.machine == "MM" and kind == 0xA0:
            return                                # pressure: not used on the MM
        if kind == 0x90 and v:                # pad OR the BSP's own sequencer (same channel):
            self.pad_note[key] = v            # wait for pressure, which only a real pad sends
            return
        if kind in (0x80, 0x90):
            self.pad_note.pop(key, None)
            return
        if kind == 0xA0:                      # pad pressure = a real pad press
            t = pad_to_track(st & 0x0F, msg[1])
            if t is None:
                return
            self.pad_note.pop(key, None)      # no sound: the MD is in grid record and
                                              # RECORDS incoming notes as trigs (2026-10-14)
            if t != self.md["track"]:
                self.md["track"] = t
                ok = self.panel.select_track(t)
                print(f"pad ch{(st & 15) + 1} note {msg[1]} -> track {t + 1}: "
                      f"{'ok' if ok else 'FAILED'}, MD lit {[x + 1 for x in (self.panel.lit_tracks() or [])]}",
                      flush=True)
            return
        if st != 0xB0:
            self.odd(msg)
            return
        if cc == PRESS and not KNOB0 <= v < KNOB0 + 16:
            print(f"press id {v:#04x}", flush=True)
        if cc == PRESS and v == SHIFT:
            self.shift_at = now
        elif cc == PRESS and v in (DRUM_BTN, SEQ1_BTN):
            self.switch("MD" if v == DRUM_BTN else "MM")
        elif cc == PRESS and v in (MD_MUTE, MM_MUTE):
            m = "MD" if v == MD_MUTE else "MM"            # like the machine: a mute window,
            if self.mute_mode == m:                       # steps toggle tracks (user 2026-10-21)
                self.mute_mode = None
                self.panel.mute_window(False)
            else:
                self.switch(m)                            # closes the other one's window
                if self.machine == m:
                    self.mute_mode = m
                    self.panel.mute_window(True)
            print(f"{m} mute window {'open' if self.mute_mode else 'closed'}", flush=True)
        elif cc == PRESS and v == LFO_BTN and self.machine == "MD":   # MD LFO page
            self.lfo = not self.lfo
            self.panel.press("altLfo" if self.lfo else "btExit")
        elif cc == PRESS and v == LST_STEP:
            self.length_arm = None if self.length_arm else now
            print("length: press the last step (of the page shown)" if self.length_arm
                  else "length: cancelled", flush=True)
        elif cc == PRESS and v in (PREV, NEXT) and now - self.shift_at < SHIFT_WINDOW:
            self.shift_at = now                               # SHIFT+arrow: whole pages
            pages = max(1, min(4, self.md["pages"] + (1 if v == NEXT else -1)))
            ok = self.panel.set_pages(pages)
            self.md["pages"], self.md["len"] = pages, pages * 16   # steps follow the total
            if "dots" in self.md:
                self.md["dots"].pages = pages                 # playing, the tracker only grows
            self.page = min(self.page, pages - 1)
            print(f"MD length -> {pages * 16} ({pages} pages): {'ok' if ok else 'FAILED'}", flush=True)
        elif cc == PRESS and v in (PREV, NEXT):               # turn the MD's page; the view
            pages = self.md["pages"]                          # follows the MD (Mirror)
            self.page = max(0, min(pages - 1, self.page + (1 if v == NEXT else -1)))
            self.panel.show_page(self.page)
        elif cc == PRESS and v == CHAN and not self.length_arm:   # MD SCALE: cycle the pages
            self.page = (self.page + 1) % max(1, self.md["pages"])
            self.panel.show_page(self.page)
        elif cc == PRESS and STEP0 <= v < STEP0 + 16 and self.mute_mode:
            self.panel.press(f"trigKey{v - STEP0}")       # mute window: toggle that track
        elif cc == PRESS and STEP0 <= v < STEP0 + 16 and self.length_arm:
            steps = self.page * 16 + v - STEP0 + 1           # inside the page shown
            self.length_arm = None
            ok = self.panel.set_end(steps)
            if ok:
                self.md["len"] = steps                        # no RAM read while playing
            print(f"MD last step -> {steps}: {'ok' if ok else 'FAILED'}", flush=True)
        elif cc == PRESS and STEP0 <= v < STEP0 + 16:
            if self.lock is not None:             # a step press ends the p-lock first: a
                print(f"p-lock step {self.lock + 1}: released (step pressed)", flush=True)
                self.panel.release()              # trig still held under Shift made the next
                self.lock = self.idle_since = None    # press act on it (cancelled trigs)
            step = v - STEP0                      # tap, or one of several p-locked steps
            self.holds[step] = (now, self.page)   # the page as pressed, not after TAP_WAIT
            self.toggled[self.page * 16 + step] = [now, None]   # light toggles at once
        elif cc == PRESS and v == PAGE_BTN:
            self.panel.data_page(1)
        elif cc == PRESS and v == REC:
            self.panel.press("btRec")
        elif cc == PRESS and v in (PLAY, STOP):
            self.running = v == PLAY
            self.bsp_send(v == PLAY)               # Start/Stop the BSP itself
        elif KNOB0 <= cc < KNOB0 + 16:
            self.turn(cc - KNOB0, v, now)
        elif cc == TEMPO_KNOB:
            last, self.tempo_val = self.tempo_val, v
            if v != last:
                self.bpm = max(30.0, min(300.0, self.bpm + (1 if v > last else -1)))
                print(f"tempo {self.bpm:.0f} BPM", flush=True)
            if v in (0, 127) and self.recenter(None):
                self.knobs_at_64()
        elif cc not in (PRESS, RELEASE):      # knob touch/release: run() gates resets on them
            self.odd(msg)

    def odd(self, msg):
        """Input the surface does not expect: logged (once a second), so a stuck BSP shows
        what it sends instead (normal-mode CCs would mean report mode dropped)."""
        if time.time() - self.odd_at > 1.0:
            self.odd_at = time.time()
            print(f"? unexpected MIDI from the BSP: {' '.join(f'{x:02X}' for x in msg)}", flush=True)

    def knobs_at_64(self):
        """The knob reset moves every knob counter (and TEMPO) to 64."""
        self.knob_val = dict.fromkeys(range(16), 64)
        self.tempo_val = 64

    def turn(self, k, v, now):
        self.last_knob = time.time()            # knob motion: resets wait for a pause
        dt, self.knob_t[k] = now - self.knob_t.get(k, -1e9), now
        accel = next((x for limit, x in KNOB_ACCEL if 0 <= dt < limit), 1)
        last, self.knob_val[k] = self.knob_val.get(k), v
        print(f"knob {k + 1}: {last} -> {v}", flush=True)
        if last is None and v in (0, 127):
            self.recenter(k)                                  # left at an end: queue a reset
        if last is None or v == last:
            return                                            # first report = baseline
        d = (v - last) / KNOB_STEP
        detents = (int(d) if abs(d) >= 1 else (1 if d > 0 else -1)) * FINE * accel
        block, slot = knob_slot(k)
        if self.holds:
            for st, (_, page) in self.holds.items():      # a p-lock, not a tap: undo the light
                self.toggled.pop(page * 16 + st, None)
            if block == "L" or k in SPECIAL:
                self.lock_steps(sorted(self.holds), block, slot, k, detents, now)
                self.holds.clear()
            else:
                self.holds.clear()        # unassigned knob: drop holds silently
            if self.lock is not None:
                self.idle_since = now
            if v in (0, 127) and self.recenter(k):
                self.knobs_at_64()
                print(f"  knob {k + 1} at {v}: recentred {time.strftime('%H:%M:%S')}", flush=True)
            return                            # holds consumed: no extra plain turn
        recent = [s for s, (t, _) in self.auto.items() if now - t < 2.0]
        if len(recent) == 1 and (block == "L" or k in SPECIAL):
            self.tap(recent[0], self.auto[recent[0]][1])  # undo the timeout-tap
            del self.auto[recent[0]]
            self.lock_steps([recent[0]], block, slot, k, detents, now)
            if self.lock is not None:
                self.idle_since = now
            return
        if self.lock is not None:
            self.idle_since = now
        if v in (0, 127) and self.recenter(k):               # knob at its end: reset it
            self.knobs_at_64()
            print(f"  knob {k + 1} at {v}: recentred {time.strftime('%H:%M:%S')}", flush=True)
        if block == "L":
            print(f"  encoder {'ABCDEFGH'[slot]} {detents:+}", flush=True)
            self.panel.encoder(slot, detents)
        elif k == DATA_PAGE_KNOB or (k == 15 and self.machine == "MM"):
            if now - self.data_page_t < 0.15:                 # one step per flick
                return
            self.data_page_t = now
            if k == DATA_PAGE_KNOB:
                self.panel.data_page(1 if detents > 0 else -1)
            else:                                             # MM: knob 16 = track 1-6
                t = max(0, min(5, self.md["track"] + (1 if detents > 0 else -1)))
                self.md["track"] = self.track_of["MM"] = t
                self.panel.select_track(t)
        elif k in SPECIAL:
            self.panel.turn(SPECIAL[k], detents)

    def lock_steps(self, steps, block, slot, k, detents, now):
        """One step: hold across turns (released at idle). Several: hold each,
        turn, release at once (the MD holds one trig at a time)."""
        if len(steps) == 1 and self.panel.hold(steps[0]):
            print(f"p-lock step {steps[0] + 1}: holding", flush=True)
            self.turn_locked(block, slot, k, detents)
            self.lock, self.idle_since = steps[0], now
            return
        for s in steps:
            if not self.panel.hold(s):
                print(f"p-lock step {s + 1}: NOT held, aborting", flush=True)
                return                                        # no hold -> do not touch the kit
            self.turn_locked(block, slot, k, detents)
            self.panel.release()
            print(f"p-lock step {s + 1}", flush=True)
        self.lock, self.idle_since = steps[-1], now

    def turn_locked(self, block, slot, k, detents):
        if block == "L":
            self.panel.encoder(slot, detents)
        else:
            self.panel.turn(SPECIAL[k], detents)

    def tap(self, step, page=None):
        page = self.page if page is None else page
        s = page * 16 + step
        if s in self.toggled:
            self.toggled[s][0] = time.time()    # keep the expectation shown at the press
        else:
            self.toggled[s] = [time.time(), None]   # expected state: set in colors()
        self.panel.show_page(page)          # no-op when the MD already shows it
        self.panel.tap(step)

    def tick(self, now):
        for s, (t, page) in list(self.holds.items()):
            if now - t > TAP_WAIT:
                del self.holds[s]
                self.tap(s, page)                             # plain tap...
                self.auto[s] = (now, page)                    # ...convertible if a turn follows
        self.auto = {s: tp for s, tp in self.auto.items() if now - tp[0] < 5.0}
        if self.length_arm and now - self.length_arm > LEN_ARM:
            self.length_arm = None
        if self.lock is not None and self.idle_since and now - self.idle_since > KNOB_IDLE:
            print(f"p-lock step {self.lock + 1}: released", flush=True)
            self.panel.release()
            self.lock = self.idle_since = None

    def colors(self, trigs, length, play_step, now):
        """16 step colours: trigs of the shown page red (MM: its red/yellow/green), playhead
        white (user, 2026-10-21), p-locked step blinks."""
        if self.mute_mode:                    # mute window: the machine's lights, lit = playing
            return [RED if c else OFF for c in self.md.get("colours", [None] * 16)]
        if self.length_arm:                                   # choosing a length: show it
            page = self.page
            blink = int(now * 4) % 2
            return [GREEN if page * 16 + i < length else (RED if blink and i == 0 else OFF)
                    for i in range(16)]
        out = []
        for i in range(16):
            s = self.page * 16 + i
            on = bool(trigs >> s & 1) if s < length else False
            if s in self.toggled:                             # show a tap at once
                t, want = self.toggled[s]
                if want is None:
                    want = self.toggled[s][1] = not on
                if on == want or now - t > 1.0:               # RAM caught up (or gave up)
                    del self.toggled[s]
                on = want
            c = (MM_COLOUR.get(self.md.get("colours", [None] * 16)[i], RED)
                 if self.machine == "MM" else RED) if on else OFF
            if play_step == s:
                c = WHITE
            if self.lock == i and int(now * 4) % 2:
                c = OFF if c else RED
            out.append(c)
        return out


def run(*args):
    from panel import Panel
    from mdmm_bsp import bsp_get, bsp_set, is_bsp_port
    import keys
    keys.no_app_nap()                   # the clock must not be throttled in the background
    if not keys.can_hold_keys():
        print("! No Accessibility permission: p-locks will NOT work (macOS drops the Shift key). "
              "System Settings > Privacy & Security > Accessibility: remove 'BSP Machinedrum', "
              "add it again, switch it on, restart the app.", flush=True)
    mi, mo = bsp_ports()
    for attempt in range(10):           # a BSP just powered on can take a moment
        try:
            accel = bsp_get(mi, mo, 0x41, ACCEL)    # (needs SysEx on: read before ignoring it)
            break
        except RuntimeError:
            time.sleep(0.5)
    else:
        print("! The BeatStep Pro does not answer: it is frozen or still starting. "
              "Power-cycle it (unplug USB or switch it off and on), then press Start again.",
              flush=True)
        sys.exit(1)
    if SURFACE_CLOCK and bsp_get(mi, mo, 0x41, AUTOSYNC) != 1:
        bsp_set(mo, 0x41, AUTOSYNC, 1)      # the BSP must follow our clock
        print("BSP Auto-sync switched on", flush=True)
    mi.ignore_types(sysex=False, timing=False, active_sense=True)   # SysEx: freeze probe replies
    out = threading.Lock()

    def send(msg):
        with out:
            mo.send_message(msg)

    led_sent = []                       # times of recent LED writes (the limiter)

    def send_led(msg):
        """An LED write, rate-limited and checked against the reset quiet time UNDER the
        lock (a write waiting for the lock went out ~25 ms after a reset; the BSP froze on
        the 16th, 2026-10-17). At most LED_CAP per LED_WINDOW s, LED_GAP s apart.
        -> False when held back (the loop redraws it on a later pass)."""
        with out:
            now = time.time()
            del led_sent[:-LED_CAP]
            if (now < reset["quiet_until"] or (led_sent and now - led_sent[-1] < LED_GAP)
                    or (len(led_sent) == LED_CAP and now - led_sent[0] < LED_WINDOW)):
                return False
            mo.send_message(msg)
            led_sent.append(now)
            return True

    transport_playing = False

    def bsp_transport(play):
        """Start/Stop the BSP itself (Auto-sync, global 17). Ableton syncs to
        the BSP's real port, so no virtual clock port is needed. The clock
        burst engages the BSP's follow; when the burst stops, the BSP switches
        back to its internal clock (verified live 2026-10-13)."""
        def go():
            nonlocal transport_playing
            with out:
                transport_playing = play
                mo.send_message([0xFA] if play else [0xFC])
                if not play:
                    return
                for _ in range(12):       # 0.25 s of clock is enough
                    if not transport_playing:
                        return             # STOP came while bursting
                    mo.send_message([0xF8])
                    time.sleep(0.021)
        threading.Thread(target=go, daemon=True).start()

    from panel import PageDots, length, pick_buffer, panel_frame
    panels = {}
    for m in ("MD", "MM"):              # each loaded Gearmulator plugin (its MCP server on)
        try:
            panels[m] = Panel(m)
        except Exception as e:
            panels[m] = None
            print(f"{m} plugin not found ({e}); its mode is off", flush=True)
    if not any(panels.values()):
        print("! No Gearmulator MD or MM plugin with MCP found. Load one in Ableton.", flush=True)
        sys.exit(1)
    panel = panels["MD"] or panels["MM"]
    clock_on = threading.Event()

    def surface_transport(play):
        """SURFACE_CLOCK: Start + a steady clock; the BSP follows (Auto-sync) and passes
        the clock to Ableton, which clocks the MD."""
        if play:
            ticks[0] = 0                    # MM playhead counts from Start
            send([0xFA])
            clock_on.set()
        else:
            clock_on.clear()
            send([0xFC])

    clock_warned = [0.0]
    ticks = [0]                         # clock ticks sent since Start (MM playhead)

    def clock():
        """24 clocks per beat at s.bpm: sleep to ~1 ms before the tick, then spin."""
        while True:
            clock_on.wait()
            nxt = time.perf_counter()
            while clock_on.is_set():
                nxt += 60.0 / (s.bpm * 24)
                while (d := nxt - time.perf_counter()) > 0.0015:
                    time.sleep(d - 0.001)
                while time.perf_counter() < nxt:
                    pass
                send([0xF8])
                ticks[0] += 1
                late = time.perf_counter() - nxt          # hiccup diagnosis; a knob reset
                if late > STALL / 10 and time.time() - clock_warned[0] > 1.0:   # shows ~50 ms
                    clock_warned[0] = time.time()
                    print(f"! clock late {1000 * late:.0f} ms", flush=True)

    shown = [None] * 16                 # step LEDs as last sent
    pads_shown = [None] * 16            # pad LEDs as last sent
    btn_shown = [None]                  # machine whose button (DRUM / SEQ1) is lit
    mute_shown = {MD_MUTE: None, MM_MUTE: None}   # mute button lights as last sent

    reset = {"quiet_until": 0.0, "last": 0.0, "pending": False}
    probe = {"at": None, "sent": None, "frozen": False, "retry": 0.0}   # freeze alarm
    QUERY = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x01, 0x00, 0x41, REPORT, 0xF7]
    REPLY = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT]

    touched = {}                        # knob id -> time touched, until its release

    def hands_on(now):
        """A hand on a knob (touch CC1 id .. release CC3 id; knob 1's sensor fires non-stop,
        so not knob 1). A reset while a knob moves is the one pattern every freeze shared."""
        return any(now - t < TOUCH_HOLD for t in touched.values())

    def redraw():
        shown[:] = [None] * 16
        pads_shown[:] = [None] * 16     # (a reset once left a pad lit)
        btn_shown[0] = None
        mute_shown.update(dict.fromkeys(mute_shown))

    def drain(now):
        """BSP input: probe replies, its clock, touches; the rest to the worker."""
        nonlocal clocks, last_clock
        while (m := mi.get_message()):
            b = m[0]
            if b[:10] == REPLY:
                probe["at"] = probe["sent"] = None     # it answered: alive
                if len(b) > 11 and b[10] == 0:         # ...but out of report mode: its
                    print("! BSP left report mode: switching it back on", flush=True)
                    send(REPORT_ON)                    # lights and controls are its own
                    redraw()
                continue
            if b[0] == 0xF8:
                clocks += 1                    # the BSP's clock: playing or not
                last_clock = now
            elif b[0] & 0xF0 in (0x80, 0x90, 0xA0, 0xB0) and len(b) > 2:
                if b[0] == 0xB0 and b[1] in (PRESS, RELEASE) and KNOB0 + 1 <= b[2] < KNOB0 + 16:
                    if b[1] == PRESS:
                        touched[b[2]] = now
                    else:
                        touched.pop(b[2], None)
                inbox.put((b, now))            # MD actions run on the worker

    def reconnect():
        """After a power cycle: reopen the BSP ports, and if it answers, set it up again."""
        import rtmidi
        for port, name in ((mi, "in"), (mo, "out")):
            port.close_port()
            idx = [i for i, p in enumerate(port.get_ports()) if is_bsp_port(p)]
            if not idx:
                return False
            port.open_port(idx[0])
        mi.ignore_types(sysex=False, timing=False, active_sense=True)
        try:
            bsp_get(mi, mo, 0x41, REPORT)
        except RuntimeError:
            return False
        mo.send_message([0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, ACCEL, 0, 0xF7])
        if SURFACE_CLOCK:
            bsp_set(mo, 0x41, AUTOSYNC, 1)
        off = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT, 0, 0xF7]
        mo.send_message(off + KNOB_RESET)   # report mode on, knobs at 64 (clock stopped)
        time.sleep(RESET_QUIET)
        mo.send_message([0xFC])             # Stop through the BSP: Ableton (and the MD) kept
        reset["quiet_until"] = time.time() + RESET_QUIET   # running alone while it was gone
        redraw()
        s.knobs_at_64()
        return True

    def recenter(k):
        """7D 7D 09 resets every knob counter to 64 (live 2026-10-14). It acts only
        with report mode off and switches it back on; it also zeroes globals
        38/71/73 (all 0 on this unit). -> True (done).
        It LOCKS the BSP (no reports, no SysEx replies; power cycle) with clock ticks
        right up to it while playing, or LED writes right after it. reset_stress.py,
        2026-10-14/15: clock+START+LEDs 0.25 s later FROZE after 4; the same with no
        tick 25 ms either side: 60 ok. So: hold the clock RESET_HUSH s either side
        (the late tick catches up) and no LED writes for RESET_QUIET s."""
        if k != "now":                  # from a knob report: only mark it. The LED loop does it
            reset["pending"] = True     # once no knob moved for RESET_IDLE s (every real freeze
            return False                # had a knob in motion at the reset) and RESET_MIN_GAP
        reset["pending"] = False        # passed; one thread, so never two resets at once
        off = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT, 0, 0xF7]
        reset["quiet_until"] = time.time() + 10      # no new LED writes from now on, and
        while led_sent and time.time() - led_sent[-1] < RESET_PRE_QUIET:   # none in the
            time.sleep(0.005)                        # last RESET_PRE_QUIET s before it
        drain(time.time())                           # last look: a hand arrived meanwhile?
        if hands_on(time.time()):
            reset["pending"], reset["quiet_until"] = True, 0.0     # later, hands off
            return False
        with out:                       # the clock thread waits on this lock
            time.sleep(RESET_HUSH)
            mo.send_message(off + KNOB_RESET)   # one packet: no normal-mode frame between
            time.sleep(RESET_HUSH)
            reset["quiet_until"] = time.time() + RESET_QUIET
            reset["last"] = time.time()
            probe["at"] = reset["last"] + RESET_QUIET + 0.35    # does it still answer?
        redraw()                        # after the quiet time
        return True

    s = Surface(panel, bsp_send=surface_transport if SURFACE_CLOCK else bsp_transport,
                recenter=recenter, panels=panels)
    if args:
        s.bpm = float(args[0])
    if SURFACE_CLOCK:
        threading.Thread(target=clock, daemon=True).start()
        print(f"surface clock: {s.bpm:.0f} BPM (TEMPO knob: 1 BPM per click)", flush=True)
    md = s.md

    eyes = {m: Panel(m) for m, p in panels.items() if p}   # own MCP clients: reading never
                                                            # blocks the buttons/LEDs
    mirror_box = {"mirror": Mirror()}   # (not "view": the light loop uses that name)
    md["dots"] = PageDots()             # page shown / pages from the dots, also while playing
    md["colours"] = [None] * 16
    for p in panels.values():
        if p:
            p.shown_page = lambda: md["frame"][0]   # show_page closes its loop on what we see

    def prepare(machine):
        """A machine taking over the BSP: grid record on, page count (MM), fresh lights."""
        p = panels[machine]
        if machine == "MM":
            md["mm_pages"] = p.page_count()
            md["len"] = md["mm_pages"] * 16     # playhead wrap (length inside a page: later)
        p.grid_rec()

    def connect(m):
        try:
            panels[m], eyes[m] = Panel(m), Panel(m)
        except Exception as e:
            print(f"! {m} plugin not found ({e}): open its window, MCP server on", flush=True)
            return False
        panels[m].shown_page = lambda: md["frame"][0]
        print(f"{m} plugin found", flush=True)
        return True

    s.connect = connect

    def switched(machine):
        mirror_box["mirror"] = Mirror()
        md["dots"] = PageDots()
        shown[:] = [None] * 16
        pads_shown[:] = [None] * 16
        prepare(machine)

    s.on_switch = switched

    def reader():                       # MD -> BSP: panel 50 Hz playing / 10 Hz stopped
        last_ram, last_err = 0.0, [None, 0.0]
        while True:
            try:
                now = time.time()
                t_read = time.time()
                machine = s.machine
                _, bits, _, lit, dots, colours, _ = panel_frame(eyes[machine].mcp, machine)
                if time.time() - t_read > STALL:            # hiccup diagnosis: slow MCP read
                    print(f"! slow MD read {1000 * (time.time() - t_read):.0f} ms", flush=True)
                if machine != s.machine:
                    continue                # switched while reading: drop this frame
                if machine == "MM":         # grid record: only the edited page's dot is lit
                    page = min(dots) if dots else md["frame"][0]   # (dark while it flashes)
                    md["pages"] = md.get("mm_pages", 1)
                    md["track"] = pick_track(md["track"], lit)
                    md["tracks_lit"], md["colours"] = lit, colours
                    head = None             # its step lights never show the playhead
                    if md["playing"] and s.running:   # (live 2026-10-21): count our clock
                        pos = ticks[0] // 6 % max(1, md["len"] or 16)
                        head = pos % 16 if pos // 16 == page else None
                    md["frame"] = (page, bits, head)
                    time.sleep(MIRROR_PERIOD if md["playing"] else IDLE_PERIOD)
                    continue
                else:
                    page, md["pages"] = md["dots"].feed(now, dots, md["playing"])
                md["track"] = pick_track(md["track"], lit)
                md["tracks_lit"] = lit
                md["colours"] = colours
                mirror = mirror_box["mirror"]
                mirror.update(bits, (md["track"], page), md["playing"], now, 15.0 / s.bpm)
                md["frame"] = (page, mirror.trigs, mirror.head)   # one object: never half a page turn
                if machine == "MD" and not md["playing"] and now - last_ram >= RAM_PERIOD:
                    last_ram = now          # never while playing: a 1 MiB get_state is the
                    ram = eyes["MD"].ram()  # suspect for MD timing hiccups (2026-10-17)
                    pick_buffer(ram, md["track"], page, bits)
                    md["len"] = length(None, ram)   # length only; the setters update it too
            except Exception as e:      # a timed-out read must not kill the lights
                where = traceback.extract_tb(e.__traceback__)[-1]
                msg = f"! {s.machine} read failed: {e!r} ({where.name}:{where.lineno})"
                if msg != last_err[0] or time.time() - last_err[1] > 10:   # once, not 50/s
                    print(msg, flush=True)
                    last_err[:] = [msg, time.time()]
            time.sleep(MIRROR_PERIOD if md["playing"] else IDLE_PERIOD)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def stdin_quit():                   # "q" on stdin = clean stop (the app's Stop button;
        for line in sys.stdin:          # Windows can't deliver SIGTERM to a handler)
            cmd = line.split()
            if cmd[:1] == ["bpm"] and len(cmd) == 2:      # the app's tempo box
                try:
                    s.bpm = max(30.0, min(300.0, float(cmd[1])))
                    print(f"tempo {s.bpm:.0f} BPM", flush=True)
                except ValueError:
                    pass
            if line.strip() == "q":     # (EOF alone, e.g. nohup's /dev/null, is not a stop)
                _thread.interrupt_main()    # KeyboardInterrupt: the finally cleans up
                return

    if not sys.stdin.isatty():
        threading.Thread(target=stdin_quit, daemon=True).start()
    mo.send_message([0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, ACCEL, 0, 0xF7])
    report_mode(mo, True)               # plain report mode, NO knob reset: a reset at start froze
    time.sleep(RESET_QUIET)             # the BSP twice on 2026-10-17 (plain start: days, none).
                                        # A knob left at an end resets on its first report.
    if not panel.ready():
        name = "Machinedrum" if s.machine == "MD" else "Monomachine"
        print(f"! Open the {name} plugin window in Ableton: the app reads its panel. "
              "Waiting...", flush=True)
        while not panel.ready():
            time.sleep(1.0)
        print(f"{name} plugin window found", flush=True)
    prepare(s.machine)                  # grid record (trig keys edit trigs); MM page count
    print(__doc__)
    print("tip: put the BSP on an EMPTY drum pattern - its own drum notes would play the MD too")
    threading.Thread(target=reader, daemon=True).start()
    inbox = queue.Queue()

    def worker():                       # every MCP action: never blocks the LED loop
        while True:
            try:
                b, t = inbox.get(timeout=0.005)
                s.report(b, t)
            except queue.Empty:
                pass
            except Exception as e:      # keep the surface alive; log the failure
                print(f"! report failed: {e!r}", flush=True)
            try:
                s.tick(time.time())
                s.panel.watchdog()
            except Exception as e:
                print(f"! tick failed: {e!r}", flush=True)

    threading.Thread(target=worker, daemon=True).start()
    clocks, last_clock, last_status, led_count = 0, 0.0, 0.0, 0
    last_loop = last_stall = time.time()
    big_redraw = [None]
    try:
        while True:
            now = time.time()
            if now - last_loop > STALL and now - last_stall > 1.0:   # hiccup diagnosis
                print(f"! light loop stalled {1000 * (now - last_loop):.0f} ms", flush=True)
                last_stall = now
            last_loop = now
            if probe["frozen"]:                    # alarm: wait for the power cycle
                if now >= probe["retry"]:
                    probe["retry"] = now + 1.0
                    try:
                        back = reconnect()
                    except Exception:
                        back = False
                    if back:
                        probe["frozen"] = False
                        print("BSP back: reconnected, report mode on. Press PLAY.", flush=True)
                time.sleep(0.01)
                continue
            if probe["at"] is None and now - probe.get("last", 0) > 5.0 and now >= reset["quiet_until"]:
                probe["at"] = now                  # and every 5 s anyway (a freeze at start)
            if probe["at"] and now >= probe["at"] and probe["sent"] is None:
                probe["last"] = now
                send(QUERY)                        # one query after each knob reset
                probe["sent"] = now
            if probe["sent"] and now - probe["sent"] > 0.5:
                probe["at"] = probe["sent"] = None
                probe["frozen"] = True
                print("! BSP FROZEN: it stopped answering. Power-cycle the "
                      "BeatStep Pro (unplug USB or switch it off and on); the app reconnects by "
                      "itself.", flush=True)
                if s.running:
                    s.running = False
                    surface_transport(False)       # stop the clock: the BSP is gone
                continue
            drain(now)
            playing = now - last_clock < 0.2   # clock running = playing (either master)
            md["playing"] = now - last_clock < 1.0    # + 1 s to settle after STOP
            if now - last_status > 2:              # debug: playhead inputs
                last_status = now
                print(f"status running={s.running} bpm={s.bpm:.0f} clock_age={now - last_clock:.2f} "
                      f"clocks={clocks} len={md['len']} page={s.page} "
                      f"track={md['track'] + 1} view={md['frame'][1]:#06x} head={md['frame'][2]} "
                      f"queue={inbox.qsize()} leds={led_count}", flush=True)
                led_count = 0
            L = md["len"] or 16                    # RAM length; the MD's page dots win
            if (L + 15) // 16 != md["pages"]:      # when they disagree (stale RAM copy)
                L = md["pages"] * 16
            if (reset["pending"] and now - reset["last"] >= RESET_MIN_GAP
                    and now - s.last_knob >= RESET_IDLE and not hands_on(now)
                    and recenter("now")):
                s.knobs_at_64()                    # knob reset, done here only (one thread)
                print(f"  knobs recentred {time.strftime('%H:%M:%S')}", flush=True)
            if now < reset["quiet_until"]:         # no LED writes right after a reset
                time.sleep(0.002)
                continue
            s.page, view, head = md["frame"]       # the BSP shows the MD's page
            step = s.page * 16 + head if playing and head is not None else None
            cols = s.colors(view << 16 * s.page, L, step, now)
            if (sum(c != shown[i] for i, c in enumerate(cols)) >= 8 and None not in shown
                    and cols != big_redraw[0]):          # once per target, not per held-back pass
                big_redraw[0] = cols
                print("  big redraw (pattern/page change?)", flush=True)   # freeze diagnosis
            order = sorted(range(16), key=lambda i: not (cols[i] == WHITE or shown[i] == WHITE))
            for i in order:                        # playhead first, then trigs, then pads:
                c = cols[i]                        # the limiter budget goes where it shows
                if c != shown[i]:
                    if not send_led(led_msg(i, c)):
                        break                      # limited: the rest on a later pass
                    shown[i] = c
                    led_count += 1
            if btn_shown[0] != s.machine:          # DRUM / SEQ1 lit for the machine in use (the
                if (send_led(button_led_msg(DRUM_BTN, s.machine == "MD"))   # LED ids = the
                        and send_led(button_led_msg(SEQ1_BTN, s.machine == "MM"))):  # button ids)
                    btn_shown[0] = s.machine
            for b, m in ((MD_MUTE, "MD"), (MM_MUTE, "MM")):    # lit = its mute window open
                want = s.mute_mode == m
                if mute_shown[b] != want and send_led(button_led_msg(b, want)):
                    mute_shown[b] = want
            on = ((set(PAD_SEMI) | {PAD_OCT_DOWN, PAD_OCT_UP}) - set(s.mm_notes)   # MM: the
                  if s.machine == "MM"                     # keyboard lit, a held pad dark
                  else {track_pad(t) for t in md["tracks_lit"]}) # MD: its track lights
            for i in range(16):
                if (i in on) != pads_shown[i]:
                    if not send_led(pad_led_msg(i, i in on)):
                        break
                    pads_shown[i] = i in on
                    led_count += 1
            time.sleep(0.002)
    finally:
        if SURFACE_CLOCK and s.running:
            surface_transport(False)    # stop the clock: the BSP falls back to INT
        for p in panels.values():
            if p:
                p.release()
        report_mode(mo, False)
        mo.send_message([0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, ACCEL, accel, 0xF7])
        print("report mode off, knob acceleration restored")


def release():
    _, mo = bsp_ports()
    report_mode(mo, False)
    print("report mode off")


def test():
    class Fake:
        def __init__(self): self.log, self.held = [], None
        def __getattr__(self, name): return lambda *a: self.log.append((name,) + a)
        def hold(self, s): self.held = s; self.log.append(("hold", s)); return True
        def release(self): self.held = None; self.log.append(("release",))
        def show_page(self, page): self.page = page; return True   # not logged
    p = Fake(); s = Surface(p)
    s.report([0xB0, PRESS, STEP0 + 4], 0.0); s.tick(0.3); assert p.log == []
    s.tick(0.6); assert p.log == [("tap", 4)]                      # tap after TAP_WAIT
    p.log.clear()
    s.report([0xB0, PRESS, STEP0 + 2], 1.0)
    s.report([0xB0, PRESS, KNOB0 + 0], 1.1)                        # touch alone: ignored
    s.report([0xB0, KNOB0 + 0, 64], 1.2)                           # baseline (first report)
    s.report([0xB0, KNOB0 + 0, 80], 1.3)                           # turn: lock step 3
    assert p.log == [("hold", 2), ("encoder", 0, 1.0)] and s.lock == 2  # +16 = 2 reports on A
    q = Fake(); u = Surface(q)                                     # a step press ends a lock
    u.report([0xB0, PRESS, STEP0 + 1], 10.0); u.report([0xB0, KNOB0, 64], 10.1)
    u.report([0xB0, KNOB0, 80], 10.2); assert u.lock == 1
    u.report([0xB0, PRESS, STEP0 + 5], 10.3); assert q.log[-1] == ("release",) and u.lock is None
    s.tick(7.1); assert s.lock == 2                                # 1.3 + KNOB_IDLE
    s.tick(7.4); assert p.log[-1] == ("release",) and s.lock is None
    assert ("tap", 2) not in p.log                                 # a lock never toggles
    s.report([0xB0, KNOB0 + 8, 40], 3.0); s.report([0xB0, KNOB0 + 8, 32], 3.1)
    assert p.log[-1] == ("encoder", 4, -0.5)                       # knob 9 = E
    s.report([0xB0, KNOB0 + 8, 24], 3.12); assert p.log[-1] == ("encoder", 4, -2.0)  # fast: x4
    s.report([0xB0, KNOB0 + 8, 16], 3.16); assert p.log[-1] == ("encoder", 4, -1.0)  # x2
    s.report([0xB0, PRESS, PAGE_BTN], 3.2); assert p.log[-1] == ("data_page", 1)
    s.report([0xB0, PRESS, REC], 3.3); assert p.log[-1] == ("press", "btRec")
    sent = []; s.bsp_send = sent.append
    s.report([0xB0, PRESS, PLAY], 3.4); assert s.running
    s.report([0xB0, PRESS, STOP], 3.5); assert sent == [True, False] and not s.running
    s.md["pages"], s.page = 2, 1
    s.report([0xB0, PRESS, LST_STEP], 3.6)
    s.report([0xB0, PRESS, STEP0 + 11], 3.8)                       # page 2, LST, step 12 -> 28
    assert p.log[-1] == ("set_end", 28) and s.length_arm is None and s.holds == {}
    s.report([0xB0, PRESS, SHIFT], 3.81); s.report([0xB0, PRESS, NEXT], 3.82)
    assert p.log[-1] == ("set_pages", 3) and s.md["pages"] == 3     # SHIFT+>>: a page more
    s.report([0xB0, PRESS, PREV], 3.9); s.report([0xB0, PRESS, PREV], 4.0)  # still shifted
    assert p.log[-1] == ("set_pages", 1) and s.page == 0
    s.report([0xB0, PRESS, PREV], 6.0); assert p.log[-1] == ("set_pages", 1)    # window over
    s.page, s.md["pages"], s.shift_at = 0, 1, -1e9            # (later times run backwards)
    n = len(p.log)
    s.report([0x90, 36, 90], 3.6); s.report([0x90, 36, 0], 3.61)
    assert len(p.log) == n                                        # sequencer note: not played
    s.report([0x90, 45, 90], 3.62); s.report([0xA0, 45, 40], 3.65)  # DRUM top row 2 = SD
    assert p.log[-2:-1] == [("select_track", 1)] and ("note", 38, 90) not in p.log, p.log[-4:]
    s.report([0xA0, 45, 50], 3.66); assert len(p.log) == n + 2     # more pressure: once only
    assert pick_track(1, [0, 1]) == 1 and pick_track(1, [0]) == 0 and pick_track(1, [0, 2]) == 1
    assert all(pad_track(track_pad(t)) == t for t in range(16)) and track_pad(0) == 8
    assert pad_to_track(15, 36) == 8 and pad_to_track(15, 44) == 0     # Control: bottom-left = 9
    assert pad_to_track(15, 51) == 7 and pad_to_track(4, 36) is None   # SEQ1 notes: not pads
    s.report([0x9F, 44, 99], 3.67); s.report([0xAF, 44, 60], 3.68)     # Control pad 9 = BD
    assert p.log[-2:-1] == [("select_track", 0)], p.log[-4:]
    s.md["len"], s.md["pages"] = 32, 2; s.report([0xB0, PRESS, NEXT], 3.66)
    assert s.page == 1
    s.report([0xB0, PRESS, NEXT], 3.67); assert s.page == 1           # only 2 pages
    s.report([0xB0, PRESS, CHAN], 3.675); assert s.page == 0          # CHAN wraps like SCALE
    s.report([0xB0, PRESS, CHAN], 3.676); assert s.page == 1
    s.report([0xB0, PRESS, PLAY], 3.68); assert s.page == 1           # PLAY never moves the view
    s.report([0xB0, PRESS, LFO_BTN], 3.681); assert p.log[-1] == ("press", "altLfo")
    s.report([0xB0, PRESS, LFO_BTN], 3.682); assert p.log[-1] == ("press", "btExit")
    s.report([0xB0, PRESS, STEP0 + 15], 3.70); assert 31 in s.toggled # light at the press
    s.page = 0; s.tick(4.3)                                           # view moved before the tap
    assert p.log[-1] == ("tap", 15) and p.page == 1, p.log[-1]  # still page 2, step 16
    s2 = Surface(Fake()); s2.tap(1)
    assert s2.colors(0, 16, None, time.time())[1] == RED                   # tap shows at once
    assert 1 in s2.toggled and s2.colors(0b10, 16, None, time.time())[1] == RED
    assert 1 not in s2.toggled                                             # RAM caught up
    cols = Surface(Fake()).colors(0b10001, 16, 4, 0.0)
    assert cols[0] == RED and cols[4] == WHITE and cols[1] == OFF
    assert led_msg(3, RED) == [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 2, 0, 0x10, 0x33, 1, 0xF7]
    # R11-style: several holds + one turn locks each in turn, nothing toggles
    p3, s3 = Fake(), Surface(Fake())
    s3.panel = p3
    s3.report([0xB0, PRESS, STEP0 + 1], 10.0)
    s3.report([0xB0, PRESS, STEP0 + 4], 10.1)
    assert set(s3.holds) == {1, 4} and p3.log == []
    s3.report([0xB0, KNOB0 + 2, 64], 10.2)
    s3.report([0xB0, KNOB0 + 2, 72], 10.3)                 # +8 = 1 detent: lock both
    assert p3.log == [("hold", 1), ("encoder", 2, 0.5), ("release",),
                      ("hold", 4), ("encoder", 2, 0.5), ("release",)], p3.log
    assert ("tap", 1) not in p3.log and ("tap", 4) not in p3.log
    # slow holder: timeout-tap, then a turn converts it back and locks
    p4, s4 = Fake(), Surface(Fake())
    s4.panel = p4
    s4.report([0xB0, PRESS, STEP0 + 6], 20.0)
    s4.tick(20.6)                                          # tap fires (too slow to hold)
    assert p4.log == [("tap", 6)]
    s4.report([0xB0, KNOB0 + 0, 64], 20.7)
    s4.report([0xB0, KNOB0 + 0, 72], 20.8)                 # convert: untap + lock
    assert p4.log[-3:] == [("tap", 6), ("hold", 6), ("encoder", 0, 0.5)], p4.log
    # endless: at 127 the knobs reset to 64; the next click counts against 64
    p5, resets = Fake(), []
    s5 = Surface(p5, recenter=lambda k: resets.append(k) or True)
    s5.report([0xB0, KNOB0 + 0, 120], 30.0)
    s5.report([0xB0, KNOB0 + 0, 127], 30.1)
    assert resets == [0] and s5.knob_val[5] == 64
    s5.report([0xB0, KNOB0 + 0, 72], 30.2)
    assert p5.log[-1] == ("encoder", 0, 0.5), p5.log       # one more click up, none lost
    # TEMPO knob: 1 BPM per report, endless via the knob reset
    t, resets = Surface(Fake(), recenter=lambda k: resets.append(k) or True), []
    t.report([0xB0, TEMPO_KNOB, 72], 40.0); t.report([0xB0, TEMPO_KNOB, 80], 40.1)
    assert t.bpm == TEMPO + 2
    t.report([0xB0, TEMPO_KNOB, 0], 40.2); assert t.bpm == TEMPO + 1 and resets and t.tempo_val == 64
    u = Surface(Fake(), recenter=lambda k: False)         # a reset not done keeps the counters
    u.report([0xB0, KNOB0 + 0, 127], 41.0); assert u.knob_val == {0: 127}
    # Mirror: stopped = LEDs; playing = newest flip is the playhead, a lasting flip an edit
    m = Mirror()
    m.update(0b101, (0, 0), False, 0.0, 0.125); assert m.trigs == 0b101 and m.head is None
    m.update(0b111, (0, 0), True, 0.1, 0.125); assert m.head == 1 and m.trigs == 0b101
    m.update(0b001, (0, 0), True, 0.2, 0.125); assert m.head == 2 and m.trigs == 0b101  # trig 3 dark
    m.update(0b101, (0, 0), True, 0.3, 0.125); assert m.trigs == 0b101                  # trail gone
    m.update(0b1101, (0, 0), True, 0.4, 0.125); assert m.head == 3
    m.update(0b11101, (0, 0), True, 0.5, 0.125); assert m.head == 4
    m.update(0b10101 | 1 << 8, (0, 0), True, 0.6, 0.125); assert m.head == 8
    for t in (0.8, 1.0, 1.2, 1.4):                                      # step 5 stays lit:
        m.update(0b10101 | 1 << (9 + int(t * 5)), (0, 0), True, t, 0.125)
    assert m.trigs >> 4 & 1, bin(m.trigs)                               # an edit on the MD
    m.update(0b1111, (0, 1), True, 2.0, 0.125)                          # page 2, unseen: LEDs
    m.update(0b1110000, (0, 0), True, 2.1, 0.125)                       # back to page 1 mid-play:
    assert m.trigs == m.known[(0, 0)] and m.trigs >> 4 & 1              # memory, not the trail
    # Monomachine mode (2026-10-21): SEQ1 / DRUM switch, pad keyboard, pitch entry, mutes
    md_p, mm_p = Fake(), Fake()
    w = Surface(md_p, panels={"MD": md_p, "MM": mm_p})
    w.report([0xB0, PRESS, SEQ1_BTN], 50.0); assert w.machine == "MM" and w.panel is mm_p
    w.report([0x9F, 36 + 2, 90], 50.1); assert mm_p.log[-1] == ("note_on", 64, 90)   # E4
    w.report([0x8F, 36 + 2, 0], 50.2); assert mm_p.log[-1] == ("note_off", 64)
    w.report([0x9F, 36 + 15, 90], 50.3); assert w.octave == OCTAVE + 1               # oct +
    w.report([0x9F, 36 + 8, 90], 50.4); assert mm_p.log[-1] == ("note_on", 73, 90)   # C#5
    w.report([0xB0, PRESS, SHIFT], 50.5); w.report([0x9F, 36 + 3, 90], 50.6)
    assert mm_p.log[-1] == ("select_track", 3) and w.track_of["MM"] == 3          # SHIFT+pad 4
    w.report([0xB0, PRESS, STEP0 + 5], 51.0); w.report([0x9F, 36 + 0, 90], 51.1)
    assert mm_p.log[-1] == ("pitch", 5, 72) and not w.holds                      # step 6 = C5
    w.report([0xB0, PRESS, MM_MUTE], 51.2); assert mm_p.log[-1] == ("mute_window", True)
    w.md["colours"] = ["red"] * 6 + [None] * 10
    assert w.colors(0, 16, 3, 51.21)[:7] == [RED] * 6 + [OFF]                    # no playhead
    w.report([0xB0, PRESS, STEP0 + 2], 51.22); assert mm_p.log[-1] == ("press", "trigKey2")
    assert not w.holds                                                           # not a trig
    w.report([0xB0, PRESS, MD_MUTE], 51.3)                                       # MD from MM:
    assert ("mute_window", False) in mm_p.log and w.machine == "MD"              # MM's closes
    assert md_p.log[-1] == ("mute_window", True) and w.mute_mode == "MD"
    w.report([0xB0, PRESS, MD_MUTE], 51.31); assert w.mute_mode is None
    w.report([0xB0, PRESS, SEQ1_BTN], 51.32); assert w.machine == "MM"
    w.report([0xB0, KNOB0 + DATA_PAGE_KNOB, 64], 51.4); w.report([0xB0, KNOB0 + DATA_PAGE_KNOB, 72], 51.5)
    assert mm_p.log[-1] == ("data_page", 1)
    w.md["colours"] = ["yellow"] + [None] * 15
    assert w.colors(0b1, 16, None, 52.0)[0] == YELLOW                              # note off
    w.report([0xB0, PRESS, DRUM_BTN], 52.1); assert w.machine == "MD" and w.md["track"] == 0
    assert w.colors(0b1, 16, None, 52.2)[0] == RED                                 # MD: lit = red
    w.report([0x9F, 36 + 2, 90], 52.3); assert ("note_on", 64, 90) not in md_p.log  # MD pads
    late = Surface(md_p, panels={"MD": md_p, "MM": None})          # MM loaded after Start:
    late.connect = lambda m: late.panels.update(MM=mm_p) or True   # found on its button
    late.report([0xB0, PRESS, SEQ1_BTN], 53.0); assert late.machine == "MM" and late.panel is mm_p
    print("all checks passed")


if __name__ == "__main__":
    {"run": run, "release": release, "test": test}[sys.argv[1] if len(sys.argv) > 1 else "run"](*sys.argv[2:])
