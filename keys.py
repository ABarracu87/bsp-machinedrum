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

    def frontmost_pid():
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(_user32.GetForegroundWindow(), ctypes.byref(pid))
        return pid.value

else:
    raise ImportError(f"unsupported platform: {sys.platform}")
