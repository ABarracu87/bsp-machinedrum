"""OS glue: hold the real Shift key (the plugin's Shift-hold latch needs it) and find the
frontmost app's process id. macOS: Quartz/AppKit. Windows (experimental, untested):
user32 via ctypes."""
import sys

if sys.platform == "darwin":
    import AppKit
    import Quartz

    _SHIFT = 56   # kVK_Shift

    def shift(down):
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        ev = Quartz.CGEventCreateKeyboardEvent(src, _SHIFT, down)
        Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskShift if down else 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

    def frontmost_pid():
        return AppKit.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()

    def can_hold_keys():
        """Accessibility permission: without it macOS drops our Shift key silently and
        p-locks become plain taps. A rebuilt (re-signed) app loses it (2026-10-17)."""
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        # prompt: macOS shows its dialog and lists the app under Accessibility (unticked);
        # without it the app is missing from the list until added by hand
        return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}))

    _activity = None

    def no_app_nap():
        """Opt out of App Nap: in the background (the user works in the DAW) macOS throttles
        timers, and the MIDI clock showed ~70 ms stalls (2026-10-17). Kept for the process."""
        global _activity
        from Foundation import NSActivityLatencyCritical, NSActivityUserInitiated, NSProcessInfo
        _activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiated | NSActivityLatencyCritical, "MIDI clock")

elif sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.windll.user32
    _VK_SHIFT, _KEYUP = 0x10, 0x0002

    def shift(down):
        _user32.keybd_event(_VK_SHIFT, 0, 0 if down else _KEYUP, 0)

    def no_app_nap():
        pass                          # no App Nap on Windows

    def can_hold_keys():
        return True

    def frontmost_pid():
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value

else:
    raise ImportError(f"unsupported platform: {sys.platform}")


# ---- the app window posts the keys for the surface (2026-10-17): macOS grants Accessibility
# to the app the user ticked, not to the surface process the app starts, so a Shift posted
# by the surface was dropped and p-locks became plain taps. The window serves a one-byte
# protocol on 127.0.0.1 (port in BSPM_KEYS_PORT): b"1"/b"0" = Shift down/up, b"?" = trusted?
import os, socket, threading

_direct_shift, _direct_can = shift, can_hold_keys


def serve():
    """Window side: start the key server, -> port for the child's environment."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        while True:
            conn, _ = srv.accept()
            with conn:
                while (b := conn.recv(1)):
                    if b == b"?":
                        conn.sendall(b"y" if _direct_can() else b"n")
                    else:
                        _direct_shift(b == b"1")
                        conn.sendall(b"k")       # the key is down/up: the click may follow
    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


if os.environ.get("BSPM_KEYS_PORT"):              # surface started by the window
    _conn = None

    def _ask(cmd):
        global _conn
        if _conn is None:
            _conn = socket.create_connection(("127.0.0.1", int(os.environ["BSPM_KEYS_PORT"])), 2)
        _conn.sendall(cmd)
        return _conn.recv(1)

    def shift(down):
        _ask(b"1" if down else b"0")

    def can_hold_keys():
        return _ask(b"?") == b"y"
