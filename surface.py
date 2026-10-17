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
  SHIFT, then << >>        pattern length: a page less/more (16-64)
  LST STEP, then a step    last step, inside the page shown (the total stays)
  REC / PLAY / STOP        MD RECORD, start/stop the clock
"""
import _thread, queue, signal, sys, threading, time

from mdmm_bsp import MD_NOTES, bsp_ports, knob_slot, pad_track

REPORT = 125                  # hidden global: 0 = normal, 2 = report mode
PRESS, RELEASE = 1, 3         # report CCs: value = control id
STEP0, PAGE_BTN, REC, PLAY, STOP = 0x30, 0x1B, 0x57, 0x58, 0x59   # ids: MCC BeatStepProTest.json
LST_STEP, PREV, NEXT, CHAN = 0x55, 0x52, 0x53, 0x54   # CHAN: id seen live 2026-10-14
SHIFT = 0x56                  # press only, no release (live 2026-10-15)
SHIFT_WINDOW = 1.5            # s: an arrow this soon after SHIFT (or a shifted arrow) = SHIFT+arrow
LEN_ARM = 15.0                # LST STEP gives up after this long (press it again to cancel)
AUTOSYNC = 17                 # global: Auto-sync (the BSP follows an incoming clock)
ACCEL = 4                     # global: knob acceleration (off while we run: 1 per detent)
KNOB0 = 0x20                  # knob k (0-15) turns = CC 0x20+k, touch id = 0x20+k
KNOB_STEP = 8                 # report values per detent (acceleration setting does not change it)
FINE = 0.5                    # wheel per report: 0.5 = ~1 MD step (1.0 trips the plugin's wheel accel)
TAP_WAIT = 0.5                # a step tap waits this long for a knob touch (= p-lock)
KNOB_IDLE = 2.5               # a p-lock ends this long after the last knob turn
MIRROR_PERIOD = 0.02          # MD panel reads while playing (one find_elements, ~10 ms)
IDLE_PERIOD = 0.1             # ... while stopped (each read costs Live CPU: 2026-10-15)
EDIT_STEPS = 3                # a LED flip outlasting this many steps is an edit, not the playhead
RAM_PERIOD = 1.0              # MD RAM reads, for the length only (the server ignores ranges,
                              # so each read is the full 1 MiB snapshot)
OFF, RED, GREEN = 0x00, 0x01, 0x04
SPECIAL = {6: "encLevel", 15: "encSound"}


SURFACE_CLOCK = True          # True: the surface is the master clock (BSP follows via Auto-sync,
                              # Ableton syncs to the BSP input, EXT on); TEMPO knob sets the BPM.
                              # False: the BSP is master (Auto-sync clock burst, fixed tempo).
TEMPO = 120.0                 # start tempo (surface clock); surface.py run <bpm>
STALL = 0.15                  # s: log a read or light-loop gap longer than this (hiccups)
LED_GAP, LED_CAP, LED_WINDOW = 0.004, 10, 0.1   # LED writes to the BSP: >= 4 ms apart, <= 10
                              # per 100 ms (user: limit what we send back at once, 2026-10-17)
RESET_PRE_QUIET = 0.15        # s without LED writes BEFORE a knob reset
RESET_MIN_GAP = 0.6           # s between knob resets (stress-tested spacing: 0.5 s)
RESET_QUIET = 0.25            # s without LED writes after a knob reset (reset_stress.py)
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


def report_mode(mo, on):
    mo.send_message([0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT, 2 if on else 0, 0xF7])


def pad_led_msg(pad, on):
    """Pad LED ctrl 0x70 + pad (pad 0 = bottom-left, as the pad notes); on/off only:
    8..127 all light fully (live 2026-10-15)."""
    return [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x10, 0x70 + pad, 0x7F if on else 0, 0xF7]


def track_pad(track):
    """MD track 0-15 -> pad 0-15 (inverse of pad_track: top row = tracks 1-8)."""
    return (track + 8) % 16


def led_msg(step, color):
    return [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x10, 0x30 + step, color, 0xF7]


class Surface:
    """Report decoding and the tap / p-lock gesture. `panel` = panel.Panel (or a fake)."""

    def __init__(self, panel, bsp_send=lambda msg: None, recenter=lambda k: True):
        self.panel, self.bsp_send, self.recenter = panel, bsp_send, recenter
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
        self.running = False            # our transport (the BSP ignores Start/Stop in report mode)
        self.md = {"trigs": [0] * 16, "len": 16, "track": 0, "playing": False,   # MD reader
                   "frame": (0, 0, None), "pages": 1, "tracks_lit": []}

    def report(self, msg, now):
        st, cc, v = msg[0], msg[1], msg[2] if len(msg) > 2 else 0
        kind, key = st & 0xF0, (st & 0x0F, msg[1])   # (channel, note)
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
            return
        if cc == PRESS and not KNOB0 <= v < KNOB0 + 16:
            print(f"press id {v:#04x}", flush=True)
        if cc == PRESS and v == SHIFT:
            self.shift_at = now
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
        elif cc == PRESS and STEP0 <= v < STEP0 + 16 and self.length_arm:
            steps = self.page * 16 + v - STEP0 + 1           # inside the page shown
            self.length_arm = None
            ok = self.panel.set_end(steps)
            if ok:
                self.md["len"] = steps                        # no RAM read while playing
            print(f"MD last step -> {steps}: {'ok' if ok else 'FAILED'}", flush=True)
        elif cc == PRESS and STEP0 <= v < STEP0 + 16:
            step = v - STEP0                      # tap, or one of several p-locked steps
            self.holds[step] = (now, self.page)   # the page as pressed, not after TAP_WAIT
            self.toggled[self.page * 16 + step] = [now, None]   # light toggles at once
        elif cc == PRESS and v == PAGE_BTN:
            self.panel.next_page(1)
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
        # knob touch reports (CC 1/3, id 0x20-0x2F) are ignored: knob 1's sensor fires non-stop

    def knobs_at_64(self):
        """The knob reset moves every knob counter (and TEMPO) to 64."""
        self.knob_val = dict.fromkeys(range(16), 64)
        self.tempo_val = 64

    def turn(self, k, v, now):
        last, self.knob_val[k] = self.knob_val.get(k), v
        print(f"knob {k + 1}: {last} -> {v}", flush=True)
        if last is None or v == last:
            return                                            # first report = baseline
        d = (v - last) / KNOB_STEP
        detents = (int(d) if abs(d) >= 1 else (1 if d > 0 else -1)) * FINE
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
        """16 step colours: trigs of the shown page red, playhead green, p-locked step blinks."""
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
            c = RED if on else OFF
            if play_step == s:
                c = GREEN
            if self.lock == i and int(now * 4) % 2:
                c = OFF if c else RED
            out.append(c)
        return out


def run(*args):
    from panel import Panel
    from mdmm_bsp import bsp_get, bsp_set
    import keys
    keys.no_app_nap()                   # the clock must not be throttled in the background
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
    mi.ignore_types(sysex=True, timing=False, active_sense=True)
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

    panel = Panel()
    from panel import PageDots, length, pick_buffer, panel_frame
    clock_on = threading.Event()

    def surface_transport(play):
        """SURFACE_CLOCK: Start + a steady clock; the BSP follows (Auto-sync) and passes
        the clock to Ableton, which clocks the MD."""
        if play:
            send([0xFA])
            clock_on.set()
        else:
            clock_on.clear()
            send([0xFC])

    clock_warned = [0.0]

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
                late = time.perf_counter() - nxt          # hiccup diagnosis; a knob reset
                if late > STALL / 10 and time.time() - clock_warned[0] > 1.0:   # shows ~50 ms
                    clock_warned[0] = time.time()
                    print(f"! clock late {1000 * late:.0f} ms", flush=True)

    shown = [None] * 16                 # step LEDs as last sent
    pads_shown = [None] * 16            # pad LEDs as last sent

    reset = {"quiet_until": 0.0, "last": 0.0, "pending": False}

    def recenter(k):
        """7D 7D 09 resets every knob counter to 64 (live 2026-10-14). It acts only
        with report mode off and switches it back on; it also zeroes globals
        38/71/73 (all 0 on this unit). -> True (done).
        It LOCKS the BSP (no reports, no SysEx replies; power cycle) with clock ticks
        right up to it while playing, or LED writes right after it. reset_stress.py,
        2026-10-14/15: clock+START+LEDs 0.25 s later FROZE after 4; the same with no
        tick 25 ms either side: 60 ok. So: hold the clock RESET_HUSH s either side
        (the late tick catches up) and no LED writes for RESET_QUIET s."""
        if time.time() - reset["last"] < RESET_MIN_GAP:   # a fast spin: resets 2-3 per second
            reset["pending"] = True     # froze the BSP after 27 (2026-10-17); the stress test
            return False                # only ever spaced them 0.5 s. Done later (LED loop).
        reset["pending"] = False
        off = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT, 0, 0xF7]
        reset["quiet_until"] = time.time() + 10      # no new LED writes from now on, and
        while led_sent and time.time() - led_sent[-1] < RESET_PRE_QUIET:   # none in the
            time.sleep(0.005)                        # last RESET_PRE_QUIET s before it
        with out:                       # the clock thread waits on this lock
            time.sleep(RESET_HUSH)
            mo.send_message(off + KNOB_RESET)   # one packet: no normal-mode frame between
            time.sleep(RESET_HUSH)
            reset["quiet_until"] = time.time() + RESET_QUIET
            reset["last"] = time.time()
        shown[:] = [None] * 16          # redraw the step LEDs after the quiet time
        pads_shown[:] = [None] * 16     # and the pads (a reset once left one lit)
        return True

    s = Surface(panel, bsp_send=surface_transport if SURFACE_CLOCK else bsp_transport,
                recenter=recenter)
    if args:
        s.bpm = float(args[0])
    if SURFACE_CLOCK:
        threading.Thread(target=clock, daemon=True).start()
        print(f"surface clock: {s.bpm:.0f} BPM (TEMPO knob: 1 BPM per click)", flush=True)
    md = s.md

    eyes = Panel()                      # own MCP client: reading never blocks the buttons/LEDs

    mirror = Mirror()
    md["dots"] = PageDots()             # page shown / pages from the dots, also while playing
    panel.shown_page = lambda: md["frame"][0]   # show_page closes its loop on what we see

    def reader():                       # MD -> BSP: panel 50 Hz playing / 10 Hz stopped
        last_ram = 0.0
        while True:
            try:
                now = time.time()
                t_read = time.time()
                _, bits, _, lit, dots = panel_frame(eyes.mcp)
                if time.time() - t_read > STALL:            # hiccup diagnosis: slow MCP read
                    print(f"! slow MD read {1000 * (time.time() - t_read):.0f} ms", flush=True)
                page, md["pages"] = md["dots"].feed(now, dots, md["playing"])
                md["track"] = pick_track(md["track"], lit)
                md["tracks_lit"] = lit
                mirror.update(bits, (md["track"], page), md["playing"], now, 15.0 / s.bpm)
                md["frame"] = (page, mirror.trigs, mirror.head)   # one object: never half a page turn
                if not md["playing"] and now - last_ram >= RAM_PERIOD:
                    last_ram = now          # never while playing: a 1 MiB get_state is the
                    ram = eyes.ram()        # suspect for MD timing hiccups (2026-10-17)
                    pick_buffer(ram, md["track"], page, bits)
                    md["len"] = length(None, ram)   # length only; the setters update it too
            except Exception as e:      # a timed-out read must not kill the lights
                print(f"! MD read failed: {e!r}", flush=True)
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
    report_mode(mo, False)              # knob reset = report mode with every knob at 64:
    time.sleep(0.05)                    # a restart keeps the old counters, and a knob left
    mo.send_message(KNOB_RESET)         # at its end sends nothing that way (2026-10-15)
    time.sleep(RESET_QUIET)             # clock stopped here; no LEDs right after it
    panel.grid_rec()                    # trig keys edit trigs
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
                panel.watchdog()
            except Exception as e:
                print(f"! tick failed: {e!r}", flush=True)

    threading.Thread(target=worker, daemon=True).start()
    clocks, last_clock, last_status, led_count = 0, 0.0, 0.0, 0
    last_loop = last_stall = time.time()
    try:
        while True:
            now = time.time()
            if now - last_loop > STALL and now - last_stall > 1.0:   # hiccup diagnosis
                print(f"! light loop stalled {1000 * (now - last_loop):.0f} ms", flush=True)
                last_stall = now
            last_loop = now
            while (m := mi.get_message()):
                b = m[0]
                if b[0] == 0xF8:
                    clocks += 1                    # the BSP's clock: playing or not
                    last_clock = now
                elif b[0] & 0xF0 in (0x80, 0x90, 0xA0, 0xB0) and len(b) > 2:
                    inbox.put((b, now))            # MD actions run on the worker
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
            if reset["pending"] and now - reset["last"] >= RESET_MIN_GAP and recenter(None):
                s.knobs_at_64()                    # deferred knob reset (RESET_MIN_GAP)
                print(f"  knobs recentred (deferred) {time.strftime('%H:%M:%S')}", flush=True)
            if now < reset["quiet_until"]:         # no LED writes right after a reset
                time.sleep(0.002)
                continue
            on = {track_pad(t) for t in md["tracks_lit"]}   # pads = the MD's track lights
            for i in range(16):
                if (i in on) != pads_shown[i]:
                    if not send_led(pad_led_msg(i, i in on)):
                        break
                    pads_shown[i] = i in on
                    led_count += 1
            s.page, view, head = md["frame"]       # the BSP shows the MD's page
            step = s.page * 16 + head if playing and head is not None else None
            cols = s.colors(view << 16 * s.page, L, step, now)
            if sum(c != shown[i] for i, c in enumerate(cols)) >= 8 and None not in shown:
                print("  big redraw (pattern/page change?)", flush=True)   # freeze diagnosis
            for i, c in enumerate(cols):
                if c != shown[i]:
                    if not send_led(led_msg(i, c)):
                        break                      # limited: the rest on a later pass
                    shown[i] = c
                    led_count += 1
            time.sleep(0.002)
    finally:
        if SURFACE_CLOCK and s.running:
            surface_transport(False)    # stop the clock: the BSP falls back to INT
        panel.release()
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
    s.tick(3.5); assert s.lock == 2
    s.tick(3.9); assert p.log[-1] == ("release",) and s.lock is None
    assert ("tap", 2) not in p.log                                 # a lock never toggles
    s.report([0xB0, KNOB0 + 8, 40], 3.0); s.report([0xB0, KNOB0 + 8, 32], 3.1)
    assert p.log[-1] == ("encoder", 4, -0.5)                       # knob 9 = E
    s.report([0xB0, PRESS, PAGE_BTN], 3.2); assert p.log[-1] == ("next_page", 1)
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
    s.report([0xB0, PRESS, STEP0 + 15], 3.70); assert 31 in s.toggled # light at the press
    s.page = 0; s.tick(4.3)                                           # view moved before the tap
    assert p.log[-1] == ("tap", 15) and p.page == 1, p.log[-1]  # still page 2, step 16
    s2 = Surface(Fake()); s2.tap(1)
    assert s2.colors(0, 16, None, time.time())[1] == RED                   # tap shows at once
    assert 1 in s2.toggled and s2.colors(0b10, 16, None, time.time())[1] == RED
    assert 1 not in s2.toggled                                             # RAM caught up
    cols = Surface(Fake()).colors(0b10001, 16, 4, 0.0)
    assert cols[0] == RED and cols[4] == GREEN and cols[1] == OFF
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
    print("all checks passed")


if __name__ == "__main__":
    {"run": run, "release": release, "test": test}[sys.argv[1] if len(sys.argv) > 1 else "run"](*sys.argv[2:])
