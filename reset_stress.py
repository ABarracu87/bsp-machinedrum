"""Live test: does the knob reset (report off + 7D 7D 09 in one packet) freeze the BSP's panel?
Two freezes on 2026-10-14 each followed resets sent while the surface clock ran.
User: keep turning knob 1 back and forth the WHOLE time. A freeze = no knob report for 2 s.
Phases: 30 s each, a reset every 0.5 s, LED redraw `gap` s after each reset.
Run 1 (2026-10-14): A stopped ok (60); B LEDs at once FROZE after 56, SysEx dead too.
Run with the surface stopped and the MD not needed. Log: reset_stress_<time>.log"""
import threading, time

from mdmm_bsp import bsp_get, bsp_ports
from surface import KNOB_RESET, REPORT, led_msg

mi, mo = bsp_ports()
mi.ignore_types(sysex=False, timing=True, active_sense=True)
out = threading.Lock()


def send(m):
    with out:
        mo.send_message(m)


clock_on = threading.Event()


def clock(bpm=120):
    nxt = time.perf_counter()
    while True:
        clock_on.wait()
        nxt = max(nxt + 60 / (bpm * 24), time.perf_counter() - 0.1)
        while time.perf_counter() < nxt:
            time.sleep(0.0005)
        send([0xF8])


def phase(name, leds, clocked, gap=0.0, hush=0.0, start=True, secs=30):
    if clocked:
        if start:
            send([0xFA])
        clock_on.set()
    off = [0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT, 0, 0xF7]
    last_cc, resets, t0, nxt, redraw = time.time(), 0, time.time(), time.time(), None
    try:
        while time.time() - t0 < secs:
            now = time.time()
            while (m := mi.get_message()):
                if m[0][0] == 0xB0:
                    last_cc = now
            if now - last_cc > 2.0:
                return f"{name}: FROZE after {resets} resets ({now - t0:.1f} s)"
            if now >= nxt:
                with out:                       # hush: no clock tick around the reset
                    time.sleep(hush)
                    mo.send_message(off + KNOB_RESET)
                    time.sleep(hush)
                resets += 1; nxt = now + 0.5
                redraw = now + gap if leds else None
            if redraw and now >= redraw:
                redraw = None
                for i in range(16):
                    send(led_msg(i, 0x01 if (i + resets) % 4 == 0 else 0)); time.sleep(0.001)
            time.sleep(0.002)
        return f"{name}: ok, {resets} resets"
    finally:
        if clocked:
            clock_on.clear()
            if start:
                send([0xFC])


# Run 2: D stopped + LEDs 0.25 s later ok (60); E clock + LEDs 0.25 s later FROZE after 4.
# Run 3 (2026-10-15): G ok (60), F ok (60). G = the surface's reset (RESET_HUSH, RESET_QUIET).
PHASES = (("G clock+start, no tick 25 ms around reset, LEDs 0.25 s later", True, True, 0.25, 0.025),
          ("F clock ticks only (no START), LEDs 0.25 s later", True, True, 0.25, 0.0, False))


def main():
    threading.Thread(target=clock, daemon=True).start()
    log = open(f"reset_stress_{time.strftime('%Y%m%d-%H%M%S')}.log", "w")
    print("turn knob 1 back and forth NOW, keep going until 'done'", flush=True)
    t = time.time()
    while time.time() - t < 20 and not any(m[0][0] == 0xB0 for m in iter(mi.get_message, None)):
        time.sleep(0.01)
    try:
        for args in PHASES:
            r = phase(*args)
            print(r, flush=True); log.write(r + "\n")
            if "FROZE" in r:
                try:
                    r2 = f"  SysEx still answered: global 125 = {bsp_get(mi, mo, 0x41, REPORT)}"
                except RuntimeError as e:
                    r2 = f"  SysEx dead too: {e}"
                print(r2, flush=True); log.write(r2 + "\n")
                break
    finally:
        send([0xF0, 0, 0x20, 0x6B, 0x7F, 0x42, 0x02, 0x00, 0x41, REPORT, 0, 0xF7])
        print("done (report mode off)", flush=True)


if __name__ == "__main__":
    main()
