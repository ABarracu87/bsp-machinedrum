"""Drive the Gearmulator MD front panel (plugin MCP server) like fingers on the machine."""
import atexit, base64, json, struct, sys, time, zlib
from pathlib import Path

import keys
from mcp import Mcp

ENCODERS = "ABCDEFGH"
PAGES = ("stSynth", "stFx", "stRoute")
MD_CH = 1                      # MD GLOBAL base channel
TRIGS = 0x29DE4                 # 16 x 32-bit big-endian trig words, bit s = step s+1 (1-32)
TRIGS_HI = 0x82D72              # same for steps 33-64 (found by tapping each pattern page)
LEN_STEPS, LEN_MULT, LEN_PAGES = 0x29E75, 0x29E76, 0x29E77   # get_state RAM, found by diffing
SCALE_FIELDS = (LEN_STEPS, LEN_MULT, LEN_PAGES)                # scale setup (mult 0 = 1x)
# The MD double-buffers the current pattern: after a pattern change (or plugin reload) the
# live copy sits 0x2AEE lower (found 2026-10-14 by matching the panel's step LEDs).
# Every address above is buffer A; `shift` is added to all of them (0 or BUFFER_B).
BUFFER_B = -0x2AEE
KNOWN = [0, BUFFER_B, -0x2258]  # pattern blocks seen so far (shift from A)
MISSES = 3                      # reads with no match before searching RAM
shift, _misses = 0, 0
MENU_GAP = 0.3                 # s between MD menu presses (0.12 dropped one, 2026-10-15)
FOCUS_ROW = (245, 555, 700)    # scale-setup underline row on the screenshot: y, x0, x1
FOCUS_FIELDS = (("steps", 610), ("total", 670), ("mult", 999))   # solid-line centre < edge
HOLD_TIMEOUT = 10.0            # never keep Shift down longer than this
WHEEL = 0.125                  # one BSP detent ~ one encoder step (plugin accelerates)


def find_port(name="Gearmulator MD"):
    for inst in json.loads((Path.home() / ".gearmulator_mcp.json").read_text()):
        if inst["pluginName"] == name:
            return inst["port"], inst["pid"]
    raise RuntimeError(f"{name} MCP server not running: enable it in the plugin settings")


frontmost_pid = keys.frontmost_pid


# ---- light RAM reads: only the bytes we need, not the whole 1 MiB snapshot ----
_ranged = None          # None = untested; True/False = the MCP server honours ranges


def ranged_region(mcp, start, length):
    """`length` bytes of MD RAM at `start`, if the server honours offset/length."""
    if _ranged is False:
        return None
    try:
        blob = base64.b64decode(mcp.tool("get_state", type="currentProgram",
                                         offset=start, length=length)["data"])
    except Exception:
        return None
    return blob if len(blob) == length else None


def ram_regions(mcp, regions):
    """-> {offset: bytes} for [(offset, length)]. The first call proves the ranged
    reads against a full snapshot; every later call reads only the regions."""
    global _ranged
    if _ranged is None:
        full = base64.b64decode(mcp.tool("get_state", type="currentProgram")["data"])
        _ranged = all(ranged_region(mcp, o, l) == full[o:o + l] for o, l in regions)
        print(f"MD RAM reads: {'ranged (light)' if _ranged else 'full snapshot (server ignores ranges)'}",
              flush=True)
        return {o: full[o:o + l] for o, l in regions}
    out = {}
    for o, l in regions:
        data = ranged_region(mcp, o, l)
        if data is None:               # transient failure: one full read
            full = base64.b64decode(mcp.tool("get_state", type="currentProgram")["data"])
            return {oo: full[oo:oo + ll] for oo, ll in regions}
        out[o] = data
    return out


def trig_words(mcp, ram=None, sh=None):
    """Every track's trigs, steps 1-64 (bit s = step s+1), from RAM (read-only).
    Without `ram`, only the two 64-byte trig regions are read."""
    sh = shift if sh is None else sh
    lo, hi = TRIGS + sh, TRIGS_HI + sh   # ponytail: hi in buffer B assumed at the same shift (unverified)
    if ram is None:
        regions = ram_regions(mcp, ((lo, 64), (hi, 64)))
        word = lambda base, t: int.from_bytes(regions[base][4 * t:4 * t + 4], "big")
    else:
        word = lambda base, t: int.from_bytes(ram[base + 4 * t:base + 4 * t + 4], "big")
    return [word(hi, t) << 32 | word(lo, t) for t in range(16)]


def length(mcp, ram=None):
    """-> pattern length in steps (scale setup's first value)."""
    if ram is None:
        return ram_regions(mcp, ((LEN_STEPS + shift, 1),))[LEN_STEPS + shift][0]
    return ram[LEN_STEPS + shift]


def scale(mcp):
    """-> {address: value} for the scale setup fields (buffer-A addresses as keys)."""
    r = ram_regions(mcp, ((LEN_STEPS + shift, len(SCALE_FIELDS)),))[LEN_STEPS + shift]
    return {a: r[i] for i, a in enumerate(SCALE_FIELDS)}


def panel_frame(mcp):
    """-> (page shown 0-3, 16-bit word of the lit step LEDs, pages in the pattern, lit
    tracks) as the MD panel shows them, from ONE find_elements call: one snapshot (no page
    turn between reads) and 1/4 of the plugin's work (live 2026-10-15: a second poller at
    14 polls/s added ~25% CPU to Live)."""
    found = mcp.tool("find_elements", selector=".mdStepLed, .mdScaleDot, .mdDrumLed")["elements"]
    lit = [e["id"] for e in found if "lit" in e["class"].split()]
    num = lambda pre: [int(i[len(pre):]) for i in lit if i.startswith(pre)]
    dots, bits = num("mdPatternPage"), sum(1 << b for b in num("stepLed"))
    # the last page's dot is always lit (= the length in pages); the other lit dot is the
    # page shown, so the shown page is the LOWEST lit dot (live 2026-10-14: 64 steps
    # cycled (2,3) (3,) (0,3) (1,3) under SCALE)
    return (min(dots) if dots else 0), bits, (max(dots) + 1 if dots else 1), num("drumLed"), dots


def png_row(path, y):
    """-> (bytes of pixel row y, bytes per pixel) of an 8-bit RGB/RGBA PNG, stdlib only
    (no PIL; macOS and Windows alike). Rows 0..y are unfiltered: ~0.4 s for row 245."""
    b, pos, idat = open(path, "rb").read(), 8, []
    while pos < len(b):
        n, = struct.unpack_from(">I", b, pos)
        kind, data = b[pos + 4:pos + 8], b[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, _, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", data)
            assert depth == 8 and ctype in (2, 6) and not interlace, "unsupported PNG"
        elif kind == b"IDAT":
            idat.append(data)
        pos += 12 + n
    bpp = 3 if ctype == 2 else 4
    stride, raw = w * bpp, zlib.decompress(b"".join(idat))
    prev = bytearray(stride)
    for r in range(y + 1):
        f, cur = raw[r * (stride + 1)], bytearray(raw[r * (stride + 1) + 1:(r + 1) * (stride + 1)])
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            if f == 1:
                cur[i] = (cur[i] + a) & 255
            elif f == 2:
                cur[i] = (cur[i] + prev[i]) & 255
            elif f == 3:
                cur[i] = (cur[i] + (a + prev[i]) // 2) & 255
            elif f == 4:
                up, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + up - c
                pa, pb, pc = abs(p - a), abs(p - up), abs(p - c)
                cur[i] = (cur[i] + (a if pa <= pb and pa <= pc else up if pb <= pc else c)) & 255
        prev = cur
    return prev, bpp


class PageDots:
    """MD page dots -> (page shown, pages). Stopped: the shown page's dot and the last
    page's dot are lit. Playing (manual + live 2026-10-15): the PLAYED page's dot flashes
    (~0.25 s on/off) and the SHOWN page's dot is steady; when they are the same page it
    flashes, and the last page's dot is dark half the loop. So, playing: shown = the dot
    lit through the whole window, else the only dot seen in it; pages = the highest seen
    (set_pages lowers it through `pages`)."""
    WINDOW = 0.4                # > the flash off-time (~0.25 s), short for a quick page turn

    def __init__(self):
        self.seen, self.shown, self.pages = [], 0, 1

    def feed(self, t, dots, playing):
        if not playing:
            self.seen = []
            if dots:
                self.shown, self.pages = min(dots), max(dots) + 1
            return self.shown, self.pages
        self.seen = [(tt, d) for tt, d in self.seen if t - tt < self.WINDOW] + [(t, set(dots))]
        if dots:
            self.pages = max(self.pages, max(dots) + 1)
        if t - self.seen[0][0] >= self.WINDOW * 0.8:             # a full window to judge
            steady = set.intersection(*(d for _, d in self.seen))
            union = set.union(*(d for _, d in self.seen))
            if len(steady) == 1:
                self.shown = steady.pop()
            elif not steady and len(union) == 1:
                self.shown = union.pop()
        return self.shown, self.pages


def panel_page_bits(mcp):
    return panel_frame(mcp)[:3]


def _sane(ram, sh):
    """Scale-setup bytes of a candidate pattern block look like a real pattern."""
    o = LEN_STEPS + sh
    return 0 <= o and o + 2 < len(ram) and 1 <= ram[o] <= 64 and ram[o + 1] < 8 and ram[o + 2] < 4


def _search(ram, track, bits, page=0):
    """-> shifts of every block whose `track` word has `bits` as its `page` half
    (page 0 = low 16 bits, page 1 = high) and sane scale bytes."""
    needle, out, i = bits.to_bytes(2, "big"), [], -1
    half = 2 if page == 0 else 0                # byte offset of that half in the BE word
    while (i := ram.find(needle, i + 1)) != -1:
        sh = i - half - 4 * track - TRIGS
        if _sane(ram, sh):
            out.append(sh)
    return out


def pick_buffer(ram, track, page, bits):
    """Set `shift` to the pattern block whose `track` trigs on `page` match the panel LEDs.
    The MD keeps the live pattern in more than one place (seen: A, A-0x2AEE, A-0x2258,
    2026-10-14), so: keep the current block while it matches; else try the known
    blocks; after MISSES reads with no match, search RAM and take a unique hit.
    Call it only while the MD is STOPPED: playing, its LEDs carry the playhead and the
    search matched garbage blocks (live 2026-10-14). An empty page never switches. -> shift"""
    global shift, _misses
    def view(sh):
        return trig_words(None, ram, sh)[track] >> (16 * page) & 0xFFFF
    if view(shift) == bits:
        _misses = 0
        return shift
    for sh in KNOWN:
        if sh != shift and _sane(ram, sh) and view(sh) == bits:
            shift, _misses = sh, 0
            print(f"MD pattern block -> A{sh:+#x}", flush=True)
            return shift
    _misses += 1
    if _misses >= MISSES and bits and page < 2:    # ponytail: pages 1-2 only (steps 33-64
        hits = _search(ram, track, bits, page)     # live elsewhere in the extra blocks)
        if len(hits) == 1:
            shift, _misses = hits[0], 0
            KNOWN.append(shift)
            print(f"MD pattern block -> A{shift:+#x} (found by search)", flush=True)
    return shift


class Panel:
    def __init__(self):
        self.mcp = Mcp(find_port()[0], resolve=lambda: find_port()[0])
        self.held = None           # trig index being held
        self.held_at = 0.0
        self.last_page = 0.0
        self.page = 0              # the MD's viewed pattern page (0-3)
        atexit.register(self.release)

    def turn(self, element, detents):
        """Wheel `detents` as events of at most 0.5 each: one bigger event trips the
        plugin's own wheel acceleration (jumps). A held trig stays held while turning."""
        n = max(1, round(abs(detents) / 0.5))
        for _ in range(n):
            self.mcp.tool("mouse_wheel", id=element, deltaY=-WHEEL * detents / n)
        if self.held is not None:
            self.held_at = time.time()          # the watchdog counts from the last turn

    def encoder(self, idx, detents):
        self.turn("enc" + ENCODERS[idx], detents)

    def next_page(self, detents):
        if time.time() - self.last_page < 0.3:          # one page per flick
            return
        self.last_page = time.time()
        for _ in range(1 if detents > 0 else 2):        # cycle is SYN -> FX -> ROUT
            self.mcp.tool("click_element", id="btSynth")

    def press(self, element):
        self.mcp.tool("click_element", id=element)

    def note(self, note, velocity):
        """Play a track through the plugin's MCP server (channel = MD base channel)."""
        status = (0x90 if velocity else 0x80) | (MD_CH - 1)
        self.mcp.tool("send_midi", status=status, data1=note, data2=velocity)

    def step_leds(self):
        """The MD's 16 step LEDs (trigs of the selected track in grid record)."""
        found = self.mcp.tool("find_elements", selector=".mdStepLed")["elements"]
        lit = {e["id"]: "lit" in e["class"].split() for e in found}
        return [lit.get(f"stepLed{i}", False) for i in range(16)]

    def lit_tracks(self):
        found = self.mcp.tool("find_elements", selector=".mdDrumLed")["elements"]
        return [int(e["id"][7:]) for e in found if "lit" in e["class"].split()]

    def current_track(self):
        lit = self.lit_tracks()
        return lit[0] if len(lit) == 1 else None

    def select_track(self, track):
        """SOUND wheel, closed-loop. (FUNCTION + trig key is NOT used: MCP clicks release
        FUNCTION before the trig key lands, so it toggled a trig - live 2026-10-14.)"""
        if self.current_track() == track:
            return True
        for _ in range(40):            # wheel accelerates, so step and re-read
            cur = self.current_track()
            if cur == track:
                return True
            if cur is None:
                time.sleep(0.05)
                continue
            self.turn("encSound", 1 if track > cur else -1)
            time.sleep(0.03)
        return self.current_track() == track

    def grid_rec(self):
        """Make sure the MD is in GRID RECORDING (trig keys edit trigs)."""
        led = self.mcp.tool("get_element", id="ledRecord")
        if "lit" not in led["class"].split():
            self.press("btRec")

    def ram(self):
        """MD patch RAM (1 MiB); the current pattern lives at PATTERN."""
        return base64.b64decode(self.mcp.tool("get_state", type="currentProgram")["data"])

    def synced_ram(self):
        """One RAM snapshot, with `shift` set to the live pattern buffer."""
        ram = self.ram()
        track = self.current_track()
        if track is not None:
            pick_buffer(ram, track, *panel_page_bits(self.mcp)[:2])
        return ram

    def length(self, ram=None):
        """-> pattern length in steps (scale setup's first value)."""
        return length(self.mcp, ram if ram is not None else self.synced_ram())

    def scale(self):
        return scale(self.mcp)

    def _menu_press(self, el):
        self.press(el)
        time.sleep(0.12)              # the panel drops presses that come too close

    def _key(self, el):
        self.press(el)
        time.sleep(MENU_GAP)

    # MD 1.63 scale setup, live 2026-10-15 (MCP screenshots of the LCD): RIGHT cycles the
    # focus steps -> total -> mult (LEFT does nothing), remembered between visits, shown as
    # a SOLID underline (focus()); UP/DOWN only after seeing it; trig key n sets the end step on the LAST page from any
    # focus; steps may be below the total (37/64); on the total UP/DOWN wrap 16..64 and reset
    # the steps to the total; in the menu the page dots show the total and the step LEDs
    # the steps within their page; 16 DOWNs 0.02 s apart all landed. No RAM: probing it
    # after every press flooded the MD, and its copy disagreed with the screen.

    def lcd_row(self, y, x0, x1):
        """One pixel row of the MD LCD from an MCP screenshot: True = ink (dark)."""
        row, bpp = png_row(self.mcp.tool("screenshot")["path"], y)
        return [row[x * bpp] < 120 for x in range(x0, x1)]      # red channel

    def focus(self):
        """Scale-setup field under the cursor: 'steps' / 'total' / 'mult' / None. The LCD
        underlines the values with dots and the focused one SOLID; the lines follow the
        digits, so classify the longest solid run by where it sits (live 2026-10-15)."""
        y, x0, x1 = FOCUS_ROW
        row, best, run = self.lcd_row(y, x0, x1) + [False], (0, 0), 0
        for i, ink in enumerate(row):
            run = run + 1 if ink else 0
            if run > best[0]:
                best = (run, i)
        if best[0] < 12:
            return None
        centre = x0 + best[1] - best[0] / 2
        return next(name for name, edge in FOCUS_FIELDS if centre < edge)

    def _goto(self, field):
        """RIGHT until the screen shows the focus on `field` (each press checked)."""
        for _ in range(4):
            if self.focus() == field:
                return True
            self._key("btRight")
        return False

    def _menu(self):
        return panel_frame(self.mcp)

    def _set_total(self, pages):
        """Total = `pages` (UP/DOWN wrap 16..64): only with the focus SEEN on the total, so
        the multiplier is never touched (2026-10-15: guessing the focus set it to 2x)."""
        if self._menu()[2] == pages:
            return True
        if not self._goto("total"):
            return False
        for _ in range(3):
            have = self._menu()[2]
            if have == pages:
                break
            self._key("btUp" if (pages - have) % 4 <= 2 else "btDown")
        return self._menu()[2] == pages

    def _set_end(self, steps):
        """Steps within the current total: trig key (any focus) sets it on the last page,
        then DOWN 16 per page, only with the focus SEEN on the steps."""
        end, pages = (steps - 1) % 16, self._menu()[2]
        self._key(f"trigKey{end}")
        if bin(self._menu()[1]).count("1") != end + 1:     # the LEDs show the last page's steps
            return False
        downs = 16 * (pages - 1 - (steps - 1) // 16)
        if downs:
            if not self._goto("steps"):
                return False
            for _ in range(downs):
                self.press("btDown"); time.sleep(0.02)         # 3 x 16 at 0.02 s all landed
            time.sleep(MENU_GAP)
            return self._menu()[1] == 0        # end off the last page: dark (exact page unseen)
        return True

    def set_pages(self, pages):
        """Total length only (SHIFT + << >>): the steps follow the total."""
        self._key("altScaleSetup")
        try:
            return self._set_total(max(1, min(4, pages)))
        finally:
            self._goto("steps")                         # RIGHT never changes a value
            self._key("btExit")

    def set_end(self, steps):
        """Steps only (LST STEP): the total stays; steps past it are refused."""
        self._key("altScaleSetup")
        try:
            return steps <= 16 * self._menu()[2] and self._set_end(max(1, steps))
        finally:
            self._goto("steps")
            self._key("btExit")

    def set_length(self, steps):
        """Both: total = the pages `steps` fills, then the end step."""
        steps = max(1, min(64, steps))
        self._key("altScaleSetup")
        try:
            return self._set_total((steps + 15) // 16) and self._set_end(steps)
        finally:
            self._goto("steps")
            self._key("btExit")

    def trig_words(self, ram=None):
        """Every track's trigs, steps 1-64 (bit s = step s+1), from RAM (read-only)."""
        return trig_words(self.mcp, ram if ram is not None else self.synced_ram())

    def show_page(self, page, shown=None):
        """Make the MD show pattern page 0-3 (SCALE cycles them in grid record), closed-loop
        on the page dots. `shown()`: the page as a PageDots reader sees it (needed while
        playing: one read of the flashing dots is not the shown page). -> True when it does."""
        shown = shown or getattr(self, "shown_page", None) or (lambda: panel_page_bits(self.mcp)[0])
        for _ in range(5):
            if shown() == page:
                return True
            before = shown()
            self.press("btScale")
            t = time.time()
            while shown() == before and time.time() - t < 1.0:   # PageDots needs ~0.4 s
                time.sleep(0.05)
        return shown() == page

    def goto_page(self, page):
        """Switch the MD's viewed pattern page (0-3): the SCALE key cycles the
        pages in grid record (the same key set_trig uses)."""
        page %= 4
        while self.page != page:
            self.press("btScale")
            time.sleep(0.1)
            self.page = (self.page + 1) % 4

    def set_trig(self, track, step, on):
        """Trig key for step 0-63 of the selected track, checked in RAM. A tap that lands on
        another pattern page is undone and the page switched (SCALE in grid record)."""
        for _ in range(10):
            before = self.trig_words()[track]
            if (before >> step & 1) == on:
                return True
            self.tap(step % 16)
            time.sleep(0.03)
            after = self.trig_words()[track]
            if (after >> step & 1) == on:
                self.page = step // 16      # the tap landed: this is the page
                return True
            if after != before:
                self.tap(step % 16)       # wrong page: put that step back
                time.sleep(0.1)
            self.press("btScale")
            time.sleep(0.1)
        return False

    def tap(self, step):
        self.mcp.tool("click_element", id=f"trigKey{step}")

    def hold(self, step):
        """Hold a trig. Needs the real Shift key and Ableton in front (plugin checks both)."""
        if self.held == step:
            return True
        if frontmost_pid() != find_port()[1]:       # re-read: Ableton may have restarted
            print("! bring Ableton to the front to make p-locks (not holding)")
            return False
        keys.shift(True)
        self.mcp.tool("click_element", id=f"trigKey{step}", modifiers={"shift": True})
        self.held, self.held_at = step, time.time()
        return True

    def release(self):
        if self.held is not None:
            keys.shift(False)
            self.held = None

    def watchdog(self):
        if self.held is not None and time.time() - self.held_at > HOLD_TIMEOUT:
            print("! trig held too long, releasing")
            self.release()


def test():
    """Self-check of the light RAM reads (ranged get_state, with fallback)."""
    ram = bytearray(0x100000)
    for t in range(16):
        ram[TRIGS + 4 * t:TRIGS + 4 * t + 4] = (1 << t).to_bytes(4, "big")
        ram[TRIGS_HI + 4 * t:TRIGS_HI + 4 * t + 4] = (1 << (15 - t)).to_bytes(4, "big")
    ram[LEN_STEPS] = 28
    # double buffer: B holds the live pattern; the panel shows track 0 page 0 = 0x1625
    import panel as P
    rb = bytearray(ram)
    rb[TRIGS + BUFFER_B:TRIGS + BUFFER_B + 4] = (0x14011625).to_bytes(4, "big")
    rb[LEN_STEPS + BUFFER_B] = 32
    assert P.pick_buffer(rb, 0, 0, 0x1625) == BUFFER_B and P.length(None, rb) == 32
    assert P.pick_buffer(rb, 0, 0, 0x1624) == BUFFER_B          # no exact match: stays
    assert P.pick_buffer(rb, 0, 1, 0x1401) == BUFFER_B
    assert P.pick_buffer(ram, 0, 0, 0x0001) == 0                 # A matches: back to A
    # an unknown block: found by search after MISSES reads, then remembered
    rc = bytearray(ram); C = -0x5000
    rc[TRIGS + C + 4:TRIGS + C + 8] = (0x0000ABCD).to_bytes(4, "big")   # track 1
    rc[LEN_STEPS + C] = 16
    for _ in range(P.MISSES - 1):
        assert P.pick_buffer(rc, 1, 0, 0xABCD) == 0              # not yet: could be the playhead
    assert P.pick_buffer(rc, 1, 0, 0xABCD) == C and C in P.KNOWN

    d = PageDots()                                  # live dot pattern, 2 pages, showing page 0
    assert d.feed(0.0, [0, 1], False) == (0, 2)
    for i in range(20):                             # playing page 0: dot 0 flashes, dot 1 dark
        d.feed(1 + i * 0.05, [0] if i % 10 < 5 else [], True)
    assert d.feed(2.0, [0], True) == (0, 2)
    for i in range(20):                             # playing page 1: dot 0 steady, dot 1 flashes
        d.feed(2 + i * 0.05, [0, 1] if i % 10 < 5 else [0], True)
    assert d.shown == 0
    for i in range(20):                             # user turns to page 1 while page 1 plays
        d.feed(3 + i * 0.05, [1] if i % 10 < 5 else [], True)
    assert d.shown == 1 and d.pages == 2
    class FakeMcp:
        def __init__(self, ranged):
            self.ranged, self.fulls = ranged, 0
        def tool(self, name, **args):
            assert name == "get_state", name
            if "offset" in args:
                if not self.ranged:        # a server that ignores the range args
                    return {"data": base64.b64encode(bytes(ram)).decode()}
                return {"data": base64.b64encode(
                    bytes(ram[args["offset"]:args["offset"] + args["length"]])).decode()}
            self.fulls += 1
            return {"data": base64.b64encode(bytes(ram)).decode()}

    global _ranged
    for ranged in (True, False):
        _ranged = None
        m = FakeMcp(ranged)
        words = trig_words(m)
        assert words == [(1 << (15 - t) << 32) | (1 << t) for t in range(16)], words
        assert length(m) == 28
        assert scale(m) == {LEN_STEPS: 28, LEN_MULT: 0, LEN_PAGES: 0}
        assert trig_words(m) == words                 # same result on re-read
        # ranged: one full read (the proof); fallback: one full read per call
        assert m.fulls == (1 if ranged else 4), m.fulls
        _ranged = None
    print("all checks passed")


if __name__ == "__main__":
    {"test": test}[sys.argv[1] if len(sys.argv) > 1 else "test"]()
