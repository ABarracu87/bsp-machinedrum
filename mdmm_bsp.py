"""BeatStep Pro -> Gearmulator Machinedrum + Monomachine.

  python mdmm_bsp.py configure   # back up BSP settings, then program it
  python mdmm_bsp.py restore     # put the backed-up settings back
  python mdmm_bsp.py run         # the router (leave it running while you play)
  python mdmm_bsp.py monitor     # print raw MIDI from the BSP
  python mdmm_bsp.py test        # self-check of the CC encoding

CC layout is copied from gearmulator-md-mm source/elektron/md/mdLib/mdautomation.cpp.
"""
import json, sys, time
from pathlib import Path

# ---- settings: match these to GLOBAL > MIDI on each machine (1-based channels) ----
MD_BASE_CH = 1      # MD uses channels 1-4 (4 tracks per channel)
MM_BASE_CH = 5      # MM uses channels 5-10 (1 track per channel); its default is 1, which collides with MD
CTRL_CH = 16        # BSP control mode -> router only, never reaches the plugins
SEQ1_CH, SEQ2_CH = MM_BASE_CH, MM_BASE_CH + 1   # SEQ1 plays MM track 1, SEQ2 MM track 2
# MD trig notes per track (BD SD HT MT LT CP RS CB CH OH RC CC M1 M2 M3 M4).
# Elektron default map, MD manual OS1.63 appendix B-1 (GLOBAL > MAP EDITOR can change it).
MD_NOTES = [36, 38, 40, 41, 43, 45, 47, 48, 50, 52, 53, 55, 57, 59, 60, 62]
BACKUP = Path(__file__).with_name("bsp_backup.json")

# R11 (DRUM-mode panel CCs, see docs/REQUIREMENTS.md): knobs 1-16 send
# CC 102-117 relative (64 +/- delta, endless), step holds send CC 118/119
# with the value page x 16 + step - 1. The panel channel is 16 (CTRL_CH):
# it must never equal the Drum Send MIDI Channel (channel 1 here).
R11_KNOB0, R11_PRESS, R11_RELEASE = 102, 118, 119

# ---- the two machines, as the plugin decodes them ----
MD_PAGES = [  # (label, 8 names) ; page index = CC page in mdautomation.cpp
    ("SYN", ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"]),
    ("FX", ["AMD", "AMF", "EQF", "EQG", "FLTF", "FLTW", "FLTQ", "SRR"]),
    ("ROUT", ["DIST", "VOL", "PAN", "DEL", "REV", "LFOS", "LFOD", "LFOM"]),
]
MM_PAGES = [
    ("SYN", list("ABCDEFGH")),
    ("AMP", ["ATK", "HOLD", "DEC", "REL", "DIST", "VOL", "PAN", "PORT"]),
    ("FILT", ["BASE", "WDTH", "HPQ", "LPQ", "ATK", "DEC", "BOFS", "WOFS"]),
    ("FX", ["EQF", "EQG", "SRR", "DTIM", "DSND", "DFB", "DBAS", "DWID"]),
    ("LFO1", ["PAGE", "DEST", "TRIG", "WAVE", "MULT", "SPD", "INTL", "DPTH"]),
    ("LFO2", ["PAGE", "DEST", "TRIG", "WAVE", "MULT", "SPD", "INTL", "DPTH"]),
    ("LFO3", ["PAGE", "DEST", "TRIG", "WAVE", "MULT", "SPD", "INTL", "DPTH"]),
]
LEVEL = "LEVEL"


def md_cc(track, page, idx):
    """-> (channel 0-15, cc). page None = level."""
    ch = MD_BASE_CH - 1 + track // 4
    lane = track % 4
    if page is None:
        return ch, 8 + lane
    return ch, [16, 40, 72, 96][lane] + page * 8 + idx


def mm_cc(track, page, idx):
    ch = MM_BASE_CH - 1 + track
    if page is None:
        return ch, 7
    return ch, [48, 56, 72, 80, 88, 104, 112][page] + idx


def md_mute(track):
    return MD_BASE_CH - 1 + track // 4, 12 + track % 4


def mm_mute(track):
    return MM_BASE_CH - 1 + track, 3


MACHINES = {
    "MD": dict(tracks=16, pages=MD_PAGES, cc=md_cc, mute=md_mute),
    "MM": dict(tracks=6, pages=MM_PAGES, cc=mm_cc, mute=mm_mute),
}


def knob_slot(n):
    """knob 0-15 -> (block, slot 0-7). Two 2x4 blocks laid out like the MD's encoders:
    left block = knobs 1-4 / 9-12, right block = knobs 5-8 / 13-16."""
    row, col = divmod(n, 8)
    return ("L" if col < 4 else "R"), row * 4 + col % 4


# ---- BeatStep Pro SysEx (Arturia v2: F0 00 20 6B 7F 42 cmd 00 param ctrl [value] F7) ----
KNOBS = range(32, 48)       # knob 1..16
STEPS = range(48, 64)       # step button 1..16
PADS = range(112, 128)      # pad 1..16 (pad 1 = bottom-left)
GLOBAL = 0x41
G_SEQ1_CH, G_SEQ2_CH, G_DRUM_CH, G_DRUMMAP = 64, 66, 68, 39
G_SCENE_MODE = 13
G_TRANSPORT_CH = (97, 98, 99)   # stop, rec, play channels
CC_REC, CC_STOP, CC_PLAY = 50, 51, 54
G_PAD_NOTE = range(48, 64)  # custom drum map note, pad 1..16


def is_bsp_port(name):
    """The BSP's main MIDI port, not its editor port. macOS: 'Arturia BeatStep Pro Arturia
    BeatStepPro' vs '... BeatStepPro OutEditor'. Windows names are a guess (untested):
    'Arturia BeatStep Pro' vs 'MIDIIN2/MIDIOUT2 (Arturia BeatStep Pro)'."""
    n = name.lower().replace(" ", "")
    return "beatsteppro" in n and "editor" not in n and "midiin2" not in n and "midiout2" not in n


def bsp_ports():
    import rtmidi
    mi, mo = rtmidi.MidiIn(), rtmidi.MidiOut()
    pi = [i for i, p in enumerate(mi.get_ports()) if is_bsp_port(p)]
    po = [i for i, p in enumerate(mo.get_ports()) if is_bsp_port(p)]
    if not pi or not po:
        sys.exit("BeatStep Pro not found: is it plugged in?")
    mi.ignore_types(sysex=False, timing=True, active_sense=True)
    mi.open_port(pi[0])
    mo.open_port(po[0])
    return mi, mo


def bsp_get(mi, mo, param, ctrl):
    while mi.get_message():
        pass
    mo.send_message([0xF0, 0x00, 0x20, 0x6B, 0x7F, 0x42, 0x01, 0x00, param, ctrl, 0xF7])
    t = time.time()
    while time.time() - t < 0.5:
        m = mi.get_message()
        if m and m[0][:10] == [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0, param, ctrl]:
            return m[0][10]
        time.sleep(0.002)
    raise RuntimeError(f"no reply for param {param} ctrl {ctrl}")


def bsp_set(mo, param, ctrl, value):
    mo.send_message([0xF0, 0x00, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, param, ctrl, value, 0xF7])
    time.sleep(0.01)


def pad_track(pad):
    """pad 0-15 (pad 1 = bottom-left) -> MD track: top row = tracks 1-8, bottom = 9-16."""
    return (pad + 8) % 16


def wanted_settings():
    """{(param, ctrl): value} for everything we program."""
    ch = CTRL_CH - 1
    s = {}
    for n, c in enumerate(KNOBS):     # CC n+1, relative #1 (64 +/- n)
        s.update({(1, c): 1, (2, c): ch, (3, c): n + 1, (6, c): 1})
    for n, c in enumerate(STEPS):     # note n, gate
        s.update({(1, c): 9, (2, c): ch, (3, c): n, (6, c): 1})
    for n, c in enumerate(PADS):      # note 36+n, gate
        s.update({(1, c): 9, (2, c): ch, (3, c): 36 + n, (6, c): 1})
    s.update({(GLOBAL, G_SEQ1_CH): SEQ1_CH - 1, (GLOBAL, G_SEQ2_CH): SEQ2_CH - 1,
              (GLOBAL, G_DRUM_CH): MD_BASE_CH - 1, (GLOBAL, G_DRUMMAP): 0,
              (GLOBAL, G_SCENE_MODE): 0})   # scene mode on = step buttons send no MIDI
    for g in G_TRANSPORT_CH:          # transport CCs were on ch 1 = MD SD track params
        s[(GLOBAL, g)] = ch
    for n, g in enumerate(G_PAD_NOTE):
        s[(GLOBAL, g)] = MD_NOTES[pad_track(n)]
    return s


MAX_SYNC_LEN = 64


def pattern_length(events, done, window=128):
    """Length of the BSP pattern from what it played: the smallest period P <= 64 that
    repeats over every step heard (last `window`), needing one loop of it + 16 steps. A period that
    divides 16/32/48/64 is taken as that (plays the same, and that's how pages work)."""
    if not any(events.get(s) for s in range(max(0, done - window), done)):
        return None
    for p in range(1, 65):
        if done < p + min(p, 16):       # one loop + 16 steps of the next confirms it
            return None
        if all(events.get(s, set()) == events.get(s - p, set())
               for s in range(max(p, done - window), done)):
            return next((L for L in (16, 32, 48, 64) if L % p == 0), p)
    return None


class DrumBridge:
    """Counts the BSP's clock (6 per step) and records which MD notes it played on each step."""

    def __init__(self):
        self.clocks = None              # None = BSP stopped
        self.events = {}                # step since start -> notes played on it

    def start(self):
        self.clocks, self.events = 0, {}

    def stop(self):
        self.clocks = None

    def clock(self):
        if self.clocks is not None:
            self.clocks += 1

    length, have = None, None       # set by the sync worker: MD length and trigs

    def is_new(self, note):
        """The BSP just played this note on a step where the MD has no trig (yet)."""
        if self.clocks is None or self.have is None or note not in MD_NOTES:
            return False
        step = (self.clocks + 2) // 6 % self.length
        return not self.have[MD_NOTES.index(note)] >> step & 1

    def done(self):
        """Steps heard completely (a step's notes arrive on its first clock or one late)."""
        return (self.clocks + 2) // 6

    def note(self, note):
        if self.clocks is not None and note in MD_NOTES:
            self.events.setdefault((self.clocks + 2) // 6, set()).add(note)


def track_lengths(events, done, window=128):
    """-> {MD track: its BSP track's length, as heard} (each BSP drum track has its own)."""
    return {t: pattern_length({a: {n} for a, notes in events.items() if n in notes}, done, window)
            for t, n in enumerate(MD_NOTES) if any(n in notes for notes in events.values())}


TRACK_NAMES = "BD SD HT MT LT CP RS CB CH OH RC CC M1 M2 M3 M4".split()


def wanted_trigs(events, done, length):
    """-> per MD track, bit s = the BSP played it on step s of the last full loop."""
    want = [0] * 16
    for a in range(done - length, done):
        for n in events.get(a, ()):
            want[MD_NOTES.index(n)] |= 1 << (a % length)
    return want


def configure():
    mi, mo = bsp_ports()
    want = wanted_settings()
    if not BACKUP.exists():
        old = {f"{p},{c}": bsp_get(mi, mo, p, c) for p, c in want}
        BACKUP.write_text(json.dumps(old, indent=0))
        print(f"backed up {len(old)} settings -> {BACKUP}")
    for (p, c), v in want.items():
        bsp_set(mo, p, c, v)
    bad = {k: (v, bsp_get(mi, mo, *k)) for k, v in want.items() if bsp_get(mi, mo, *k) != v}
    print("verified OK" if not bad else f"MISMATCH (wanted, got): {bad}")


def restore():
    mi, mo = bsp_ports()
    old = json.loads(BACKUP.read_text())
    for k, v in old.items():
        bsp_set(mo, *map(int, k.split(",")), v)
    print(f"restored {len(old)} settings")


def monitor():
    mi, _ = bsp_ports()
    print("move things on the BSP, ctrl-c to stop")
    while True:
        m = mi.get_message()
        if m:
            print(m[0])
        time.sleep(0.001)


# ---- router ----
class Router:
    """BSP as the Machinedrum's front panel (MD sequencer plays, BSP clock drives Ableton).
    DRUM mode:  write the pattern on the BSP; every step it plays is copied to the MD
                (BSP pattern is the master: MD trigs it doesn't play are removed)
    CTRL mode:  steps 1-16   tap = toggle trig, hold + knob = p-lock on that step
                knobs 1-4 / 9-12  DATA ENTRY A-D / E-H    knob 7 LEVEL   knob 8 page
                knob 16      SOUND SELECTION (track)      pads = play the 16 tracks
    DRUM mode + R11: knobs send CC 102-117 (endless, relative), step holds
                send CC 118/119. A knob turn with steps held p-locks each of
                them on the MD over MCP (hold the trig, turn, release); a turn
                with nothing held drives the R7 parameters. The DRUM bridge
                copies the BSP pattern onto the MD as before."""
    SPECIAL = {6: "encLevel", 15: "encSound"}

    def __init__(self, panel, bridge=None, sound=None):
        self.panel = panel
        self.sound = sound or (lambda n, v: (panel.note(n, v), panel.note(n, 0)))
        self.bridge = bridge or DrumBridge()
        self.step = None            # step button currently down
        self.used = False           # was a knob turned while it was down?
        self.track = None           # MD track last selected from a pad
        self.last_clock = 0.0       # time of the last BSP clock (none = stopped)
        self.warned = False
        self.r11_holds = set()      # R11: DRUM-mode steps currently held

    def r7_turn(self, n, detents):
        """The R7 mapping: knobs 1-4 / 9-12 = DATA ENTRY A-H, knob 7 = LEVEL,
        knob 16 = track. The other knobs are unassigned."""
        block, slot = knob_slot(n)
        if block == "L":
            self.panel.encoder(slot, detents)
        elif n in self.SPECIAL:
            self.panel.turn(self.SPECIAL[n], detents)

    def r11_knob(self, n, delta):
        """R11: a DRUM-mode knob turn (CC 102+n, value 64+delta). With steps
        held (CC 118): p-lock each of them on the MD over MCP - the real-
        machine gesture, hold the trig key, turn the encoder, release. Without:
        the R7 parameter mapping."""
        if not delta:
            return
        if knob_slot(n)[0] != "L" and n not in self.SPECIAL:
            return                      # unassigned in R7 (knobs 5, 6, 8, 13-15)
        if self.r11_holds:
            self.panel.grid_rec()      # trig keys edit trigs only in grid record
            for s in sorted(self.r11_holds):
                self.panel.goto_page(s >> 4)   # the step must be the one shown
                if not self.panel.hold(s & 15):
                    return                # no hold -> do not touch the kit
                self.r7_turn(n, delta)
                self.panel.release()
        else:
            self.r7_turn(n, delta)

    def knob(self, n, detents):
        block, slot = knob_slot(n)
        if block == "L":
            if self.step is not None:
                self.used = True              # a lock attempt never toggles the trig
                if not self.panel.hold(self.step):
                    return                    # no hold -> do not touch the kit
            self.panel.encoder(slot, detents)
        elif n == 7:
            self.panel.next_page(detents)
        elif n in self.SPECIAL:
            self.panel.turn(self.SPECIAL[n], detents)

    def step_button(self, n, down):
        if down:
            self.step, self.used = n, False
        elif n == self.step:
            if self.panel.held is not None:
                self.panel.release()
            elif not self.used:
                self.panel.tap(n)
            self.step = None

    def from_bsp(self, msg):
        st = msg[0]
        if st == 0xF8:
            self.last_clock = time.time()     # the BSP only sends clock while it plays
            if self.bridge.clocks is None and not self.warned:
                print("! BSP is playing but I missed its start: press STOP then PLAY to sync")
                self.warned = True
            self.bridge.clock()
        elif st in (0xFA, 0xFB):
            self.warned = False
            print("BSP start")
            self.bridge.start()
        elif st == 0xFC:
            print(f"BSP stop after {self.bridge.clocks} clocks")
            self.bridge.stop()
        elif st < 0xF0 and (st & 0x0F) == MD_BASE_CH - 1 and msg[1] in MD_NOTES:
            kind, t = st & 0xF0, MD_NOTES.index(msg[1])
            stopped = time.time() - self.last_clock > 0.2
            pad = kind == 0xA0 or (kind == 0x90 and msg[2] and stopped)
            if kind == 0x90 and msg[2]:
                self.bridge.note(msg[1])              # what the BSP plays, step by step
                if not stopped and self.bridge.is_new(msg[1]):
                    self.sound(msg[1], msg[2])        # a step the MD lacks yet: play it now
            if kind == 0x90 and msg[2] and stopped:
                self.panel.note(msg[1], msg[2])       # pad while stopped: play it
                self.panel.note(msg[1], 0)
            if pad and t != self.track:               # aftertouch only comes from a pad,
                self.track = t                        # never from the BSP sequencer
                self.panel.select_track(t)
        elif st < 0xF0 and (st & 0x0F) == CTRL_CH - 1:
            kind, d1, d2 = st & 0xF0, msg[1], msg[2]
            on = kind == 0x90 and d2 > 0
            if kind == 0xB0 and 1 <= d1 <= 16:
                if d2 != 64:                  # Relative #1; 64 = no movement
                    self.knob(d1 - 1, d2 - 64)
            elif kind == 0xB0 and d1 == R11_PRESS:
                self.r11_holds.add(d2)        # value = page x 16 + step - 1
            elif kind == 0xB0 and d1 == R11_RELEASE:
                self.r11_holds.discard(d2)
            elif kind == 0xB0 and R11_KNOB0 <= d1 < R11_KNOB0 + 16:
                self.r11_knob(d1 - R11_KNOB0, d2 - 64)
            elif kind in (0x90, 0x80) and d1 < 16:
                self.step_button(d1, on)
            elif kind in (0x90, 0x80) and 36 <= d1 < 52:
                self.panel.note(MD_NOTES[pad_track(d1 - 36)], d2 if on else 0)
        # everything else is dropped: nothing reaches the plugin as MIDI


def md_sync(panel, bridge, lock):
    """Worker, while the BSP plays: MD length = the BSP pattern's length (heard), MD trigs =
    what the BSP played in the last loop. Reads the MD from RAM (no scrolling); writes with
    trig keys, the current track first (normally the one you're editing: no scrolling)."""
    length, warned = None, None
    while True:
        time.sleep(0.03)
        if bridge.clocks is None:
            continue
        done = bridge.done()
        found = pattern_length(bridge.events, done)
        if found is None and done >= 128 and warned != "mixed":
            lens = track_lengths(bridge.events, done)
            if None not in lens.values() and len(set(lens.values())) > 1:   # None = being edited
                print("! BSP tracks have different lengths (" + ", ".join(
                    f"{TRACK_NAMES[t]} {L}" for t, L in sorted(lens.items())) + "): the MD has one"
                    " length per pattern. Give them all the same length (or 16/32/64-friendly).")
                warned = "mixed"
        length = found or length   # keep it while you edit
        if length is None or done < length:
            continue
        if length > MAX_SYNC_LEN:
            if warned != length:
                print(f"! BSP pattern repeats every {length} steps: only up to {MAX_SYNC_LEN} for now")
                warned = length
            continue
        want = wanted_trigs(bridge.events, done, length)
        with lock:
            if panel.held is not None:
                continue
            if panel.length() != length:      # light reads: scale fields + trig words
                print(f"BSP length {length}: setting the MD (restart PLAY if they drift apart)")
                print("  ok" if panel.set_length(length) else "  ! MD length not set")
            mask = (1 << length) - 1
            have = panel.trig_words()
            bridge.length, bridge.have = length, have
            todo = [t for t in range(16) if (have[t] ^ want[t]) & mask]
            if not todo:
                continue
            panel.grid_rec()
            home = panel.current_track()
            t0 = time.time()
            for t in sorted(todo, key=lambda t: t != home):
                if not panel.select_track(t):
                    print(f"! could not select track {t + 1}")
                    continue
                for s in range(length):
                    if (have[t] ^ want[t]) >> s & 1 and not panel.set_trig(t, s, want[t] >> s & 1):
                        print(f"! track {t + 1} step {s + 1} not set")
                have[t] = want[t]
                print(f"track {t + 1}: trigs {[s + 1 for s in range(length) if want[t] >> s & 1]}"
                      f"  ({time.time() - t0:.2f}s)")
            if home is not None:
                panel.select_track(home)


def run():
    import threading
    from panel import Panel
    mi, _ = bsp_ports()
    mi.ignore_types(sysex=True, timing=False, active_sense=True)   # clock places the steps
    panel = Panel()
    lock = threading.Lock()           # one panel user at a time (router or sync worker)
    from mcp import Mcp
    from panel import find_port
    snd = Mcp(find_port()[0], resolve=lambda: find_port()[0])         # own client: new-step sounds never wait for the panel lock
    r = Router(panel, sound=lambda n, v: (
        snd.tool("send_midi", status=0x90 | (MD_BASE_CH - 1), data1=n, data2=v),
        snd.tool("send_midi", status=0x80 | (MD_BASE_CH - 1), data1=n, data2=0)))
    threading.Thread(target=md_sync, args=(panel, r.bridge, lock), daemon=True).start()
    print("router up (MCP only, no MIDI to the plugin)")
    print(Router.__doc__)
    try:
        while True:
            msgs = []
            while (m := mi.get_message()):
                msgs.append(m[0])
            merged = {}                       # merge bursts of knob detents into one turn
            for msg in msgs:
                if (msg[0] == 0xB0 | (CTRL_CH - 1) and msg[2] != 64
                        and (1 <= msg[1] <= 16
                             or R11_KNOB0 <= msg[1] < R11_KNOB0 + 16)):
                    merged[msg[1]] = merged.get(msg[1], 0) + msg[2] - 64
                    continue
                playing_note = (msg[0] == 0x90 | (MD_BASE_CH - 1) and msg[2]
                                and time.time() - r.last_clock < 0.2)
                if msg[0] == 0xF8 or playing_note:   # clock and BSP notes never wait for the panel
                    r.from_bsp(msg)
                    continue
                with lock:
                    for cc, d in merged.items():  # keep order around button presses
                        if cc >= R11_KNOB0:
                            r.r11_knob(cc - R11_KNOB0, d)
                        else:
                            r.knob(cc - 1, d)
                    merged = {}
                    r.from_bsp(msg)
            with lock:
                for cc, d in merged.items():
                    if cc >= R11_KNOB0:
                        r.r11_knob(cc - R11_KNOB0, d)
                    else:
                        r.knob(cc - 1, d)
                panel.watchdog()
            if not msgs:
                time.sleep(0.001)
    finally:
        panel.release()


def test():
    assert is_bsp_port("Arturia BeatStep Pro Arturia BeatStepPro")
    assert not is_bsp_port("Arturia BeatStep Pro BeatStepPro OutEditor")
    # expected values from the repo's automationMidiTest.cpp (channels 0-based there)
    global MD_BASE_CH, MM_BASE_CH
    MD_BASE_CH, MM_BASE_CH = 1, 1
    assert md_cc(0, 0, 0) == (0, 16)
    assert md_cc(0, 1, 0) == (0, 24)
    assert md_cc(3, 2, 7) == (0, 119)
    assert md_cc(4, 0, 0) == (1, 16)
    assert md_cc(15, None, 0) == (3, 11)
    assert mm_cc(0, 0, 0) == (0, 48) and mm_cc(0, 1, 0) == (0, 56) and mm_cc(0, 2, 0) == (0, 72)
    assert mm_cc(0, 3, 3) == (0, 83) and mm_cc(0, 4, 0) == (0, 88) and mm_cc(0, 5, 0) == (0, 104)
    assert mm_cc(5, 6, 7) == (5, 119) and mm_cc(2, None, 0) == (2, 7) and mm_mute(4) == (4, 3)
    MD_BASE_CH = 5                            # repo: MD Mute track 10, 0-based base 4 -> 0xb6 cc 13
    assert md_mute(9) == (6, 13)
    MD_BASE_CH, MM_BASE_CH = 1, 5
    assert [knob_slot(n) for n in (0, 3, 4, 7, 8, 11, 12, 15)] == [
        ("L", 0), ("L", 3), ("R", 0), ("R", 3), ("L", 4), ("L", 7), ("R", 4), ("R", 7)]
    class FakePanel:
        def __init__(self):
            self.log, self.held, self.ok = [], None, True
        def encoder(self, i, d): self.log.append(("enc", "ABCDEFGH"[i], d))
        def turn(self, el, d): self.log.append(("turn", el, d))
        def next_page(self, d): self.log.append(("page", d))
        def tap(self, s): self.log.append(("tap", s))
        def hold(self, s):
            if self.ok:
                self.held = s; self.log.append(("hold", s))
            return self.ok
        def release(self): self.held = None; self.log.append(("release",))
        def goto_page(self, p):
            if self.page != p % 4:
                self.log.append(("page_to", p))
                self.page = p % 4
        def grid_rec(self): self.log.append(("grid_rec",))
        def press(self, el): self.log.append(("press", el))
        def note(self, n, v): self.log.append(("note", n, v))
    fp = FakePanel()
    fp.page = 0
    r = Router(fp)
    r.from_bsp([0xBF, 1, 66]); assert fp.log[-1] == ("enc", "A", 2)        # knob 1 = A
    r.from_bsp([0xBF, 12, 63]); assert fp.log[-1] == ("enc", "H", -1)      # knob 12 = H
    r.from_bsp([0xBF, 16, 65]); assert fp.log[-1] == ("turn", "encSound", 1)
    r.from_bsp([0xBF, 7, 65]); assert fp.log[-1] == ("turn", "encLevel", 1)
    r.from_bsp([0xBF, 8, 65]); assert fp.log[-1] == ("page", 1)
    r.from_bsp([0x9F, 4, 100]); r.from_bsp([0x8F, 4, 0])                   # tap step 5
    assert fp.log[-1] == ("tap", 4)
    r.from_bsp([0x9F, 2, 100]); r.from_bsp([0xBF, 3, 66])                 # hold 3 + knob 3
    assert fp.log[-2:] == [("hold", 2), ("enc", "C", 2)], fp.log
    r.from_bsp([0x9F, 2, 0])                                               # vel-0 = release
    assert fp.log[-1] == ("release",) and r.step is None
    fp.ok = False; n = len(fp.log)                                         # not frontmost:
    r.from_bsp([0x9F, 6, 100]); r.from_bsp([0xBF, 1, 66])                  # no kit change
    assert fp.log[n:] == [], fp.log[n:]
    r.from_bsp([0x8F, 6, 0]); assert fp.log[n:] == [], fp.log[n:]          # and no tap
    r.from_bsp([0x9F, 36 + 9, 90]); assert fp.log[-1] == ("note", 38, 90)  # pad 10 = SD
    r.from_bsp([0x8F, 36 + 9, 0]); assert fp.log[-1] == ("note", 38, 0)
    assert [pad_track(p) for p in (0, 7, 8, 15)] == [8, 15, 0, 7]
    n = len(fp.log)
    for m in ([0x94, 60, 100], [0xBF, 54, 127], [0xBF, 51, 127], [0xBF, 50, 127]):
        r.from_bsp(m)                                                      # MM ch / transport:
    assert len(fp.log) == n, fp.log[n:]                                    # dropped
    fp.select_track = lambda t: fp.log.append(("select", t))
    r.from_bsp([0x90, 38, 70])                                             # pad SD, stopped
    assert fp.log[-3:] == [("note", 38, 70), ("note", 38, 0), ("select", 1)], fp.log[-3:]
    r2 = Router(fp); r2.from_bsp([0xF8]); n = len(fp.log)                 # start missed,
    r2.from_bsp([0x90, 40, 100]); assert len(fp.log) == n, fp.log[n:]    # still no pad action
    r.from_bsp([0xFA]); r.from_bsp([0xF8]); n = len(fp.log)
    r.from_bsp([0x90, 40, 100]); assert len(fp.log) == n                   # BSP seq note: no sound,
    assert r.bridge.events == {0: {40}}                                    # but recorded
    snd = []; r.sound = lambda n, v: snd.append(n)
    r.bridge.length, r.bridge.have = 16, [0] * 16
    r.from_bsp([0x90, 36, 100]); assert snd == [36]                        # MD lacks it: sound
    r.bridge.have = [1] + [0] * 15
    r.from_bsp([0x90, 36, 100]); assert snd == [36]                        # MD has it: silent
    r.from_bsp([0xA0, 40, 90]); assert fp.log[-1] == ("select", 2)          # pad while playing
    r.from_bsp([0xA0, 40, 95]); assert len(fp.log) == n + 1                 # once per pad
    br = DrumBridge(); br.start()
    for _ in range(2 * 6 + 2): br.clock()                                  # 2 clocks late (swing)
    br.note(36); br.note(99); assert br.events == {2: {36}}
    ev = {a: {36} for a in range(0, 64, 4)} | {a: {38} for a in range(2, 64, 16)}
    ev[50] = {38}                                                          # added in the last loop
    assert wanted_trigs(ev, 64, 16) == [0x1111, 0x4 | 1 << 2] + [0] * 14
    assert wanted_trigs(ev, 64, 32)[1] == 1 << 2 | 1 << 18                # steps 3, 19
    def heard(pattern, L, loops):          # {step: notes} of a length-L pattern, played
        return {a: set(pattern[a % L]) for a in range(L * loops) if a % L in pattern}
    assert pattern_length(heard({0: [36], 4: [38]}, 16, 2), 32) == 16
    assert pattern_length(heard({0: [36], 4: [36]}, 8, 4), 32) == 16     # period 8 plays as 16
    assert pattern_length(heard({0: [36], 5: [38]}, 13, 2), 26) == 13
    assert pattern_length(heard({0: [36], 20: [38]}, 32, 1), 32) is None # one loop: not sure yet
    assert pattern_length(heard({0: [36], 20: [38]}, 32, 2), 64) == 32
    assert pattern_length(heard({0: [36], 40: [38], 60: [40]}, 64, 2), 128) == 64
    assert pattern_length(heard({0: [36], 20: [38]}, 32, 2), 48) == 32      # 1.5 loops is enough
    assert pattern_length(heard({0: [36], 40: [38], 60: [40]}, 64, 2), 79) is None
    assert pattern_length({}, 64) is None                                # silence: no guess
    poly = {a: {36} for a in range(256) if a % 16 in (0, 6)}               # BD, 16 steps
    for a in range(0, 256, 13): poly.setdefault(a, set()).add(MD_NOTES[11])  # CC, 13 steps
    assert pattern_length(poly, 256) is None                               # 16 x 13: > 64
    assert track_lengths(poly, 256) == {0: 16, 11: 13}
    r.from_bsp([0xFC]); assert r.bridge.clocks is None
    assert [(k, c) for (k, c), v in wanted_settings().items() if k == GLOBAL and c in (97, 98, 99)
            and v == CTRL_CH - 1] == [(GLOBAL, 97), (GLOBAL, 98), (GLOBAL, 99)]
    # R11: DRUM-mode knobs (CC 102-117) and step holds (CC 118/119)
    fp.ok = True                                    # frontmost again
    r = Router(fp)
    assert r.r11_holds == set()
    r.from_bsp([0xBF, 118, 4])            # hold step 5 (page 0, value 4)
    r.from_bsp([0xBF, 102, 65])           # knob 1 cw: p-lock step 5
    assert fp.log[-4:] == [("grid_rec",), ("hold", 4), ("enc", "A", 1), ("release",)]
    r.from_bsp([0xBF, 119, 4])            # release step 5
    r.from_bsp([0xBF, 102, 63])           # knob 1 ccw, nothing held: plain turn
    assert fp.log[-1] == ("enc", "A", -1) and fp.held is None
    r.from_bsp([0xBF, 118, 33])           # hold page 2, step 2 (value 33)
    r.from_bsp([0xBF, 107, 65])           # knob 6: right block, unassigned
    assert fp.log[-1] == ("enc", "A", -1)  # nothing happens
    r.from_bsp([0xBF, 108, 65])           # knob 7 cw: p-lock LEVEL on that step
    assert fp.log[-4:] == [("page_to", 2), ("hold", 1), ("turn", "encLevel", 1), ("release",)]
    r.from_bsp([0xBF, 119, 33])           # release step 2
    r.from_bsp([0xBF, 102, 65])           # nothing held: encoder A again
    assert fp.log[-1] == ("enc", "A", 1)
    r.from_bsp([0xBF, 118, 16])           # page 1, step 1 and page 2, step 2
    r.from_bsp([0xBF, 118, 33])
    r.from_bsp([0xBF, 102, 65])           # one knob turn: both steps locked
    assert fp.log[-9:] == [("grid_rec",), ("page_to", 1), ("hold", 0),
                           ("enc", "A", 1), ("release",), ("page_to", 2),
                           ("hold", 1), ("enc", "A", 1), ("release",)]
    r.from_bsp([0xBF, 119, 16]); r.from_bsp([0xBF, 119, 33])
    assert fp.log[-2:] == [("enc", "A", 1), ("release",)] and r.r11_holds == set()
    print("all checks passed")


if __name__ == "__main__":
    {"configure": configure, "restore": restore, "run": run,
     "monitor": monitor, "test": test}[sys.argv[1] if len(sys.argv) > 1 else "run"]()
