"""BSP -> Machinedrum: a small window around `surface.py run`.

Start runs the surface as a child process (its own clock, MIDI and MCP threads stay out of
the UI); Stop sends "q" on its stdin so it cleans up (clock off, BSP report mode off).
The same executable is the child: `app --surface <bpm>` / `app --release`."""
import os, queue, subprocess, sys, threading
import tkinter as tk
from tkinter import ttk

FROZEN = getattr(sys, "frozen", False)


def child_cmd(*args):
    if FROZEN:
        return [sys.executable, *args]
    return [sys.executable, os.path.abspath(__file__), *args]


class App:
    def __init__(self, root):
        self.root, self.proc, self.lines = root, None, queue.Queue()
        root.title("BSP → Machinedrum")
        root.minsize(520, 360)
        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Tempo").pack(side="left")
        self.bpm = tk.StringVar(value="120")
        self.tempo = tk.Spinbox(top, from_=30, to=300, width=5, textvariable=self.bpm,
                                command=self.send_bpm)     # classic: ttk's showed empty on Tk 9
        self.tempo.pack(side="left", padx=(4, 12))
        self.tempo.bind("<Return>", lambda e: self.send_bpm())
        self.bpm.set("120")
        self.button = ttk.Button(top, text="Start", command=self.toggle)
        self.button.pack(side="left")
        ttk.Button(top, text="Release BSP", command=self.release).pack(side="left", padx=8)
        self.status = tk.StringVar(value="stopped")
        ttk.Label(root, textvariable=self.status, padding=(8, 0)).pack(fill="x")
        self.log = tk.Text(root, height=18, wrap="none", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=8)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(100, self.pump)

    def toggle(self):
        if self.proc and self.proc.poll() is None:
            self.stop()
        else:
            self.start()

    def start(self):
        try:
            bpm = str(max(30, min(300, round(float(self.bpm.get())))))
        except ValueError:            # an empty box once crashed the surface at start
            bpm = "120"
        self.bpm.set(bpm)
        self.proc = subprocess.Popen(child_cmd("--surface", bpm), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"})
        threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()
        self.button.config(text="Stop")
        self.status.set("starting…")

    def send_bpm(self):
        """Tempo box -> the running surface (the TEMPO knob comes back via the status)."""
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.write(f"bpm {self.bpm.get()}\n")
            self.proc.stdin.flush()

    def stop(self):
        try:
            self.proc.stdin.write("q\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()          # last resort: report mode may stay on -> Release BSP

    def release(self):
        if self.proc and self.proc.poll() is None:
            self.stop()
        out = subprocess.run(child_cmd("--release"), capture_output=True, text=True)
        self.lines.put((out.stdout + out.stderr).strip() or "BSP released")

    def read(self, proc):
        for line in proc.stdout:
            self.lines.put(line.rstrip())
        self.lines.put(None)          # child ended

    def pump(self):
        while not self.lines.empty():
            line = self.lines.get()
            if line is None:
                self.button.config(text="Start")
                self.status.set("stopped")
            elif line.startswith("tempo ") and self.root.focus_get() is not self.tempo:
                self.bpm.set(line.split()[1])               # knob turn: show it at once
                self.add_log(line)
            elif line.startswith("status "):
                f = dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv)
                if "bpm" in f and self.root.focus_get() is not self.tempo:
                    self.bpm.set(f["bpm"])                  # TEMPO knob turns show here
                self.status.set(f"{'playing' if f.get('running') == 'True' else 'ready'} · "
                                f"{f.get('bpm', '?')} BPM · "
                                f"track {f.get('track', '?')} · page {int(f.get('page', 0)) + 1} · "
                                f"length {f.get('len', '?')}")
            else:
                self.add_log(line)
        self.root.after(100, self.pump)

    def add_log(self, line):
        self.log.config(state="normal")
        self.log.insert("end", line + "\n")
        self.log.delete("1.0", "end-500l")
        self.log.see("end")
        self.log.config(state="disabled")

    def quit(self):
        if self.proc and self.proc.poll() is None:
            self.stop()
        self.root.destroy()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--surface":
        import surface
        try:
            surface.run(*sys.argv[2:3])
        except KeyboardInterrupt:     # Stop ("q"): surface.run already cleaned up
            pass
    elif len(sys.argv) > 1 and sys.argv[1] == "--release":
        import surface
        surface.release()
    else:
        root = tk.Tk()
        App(root)
        root.mainloop()


if __name__ == "__main__":
    main()
