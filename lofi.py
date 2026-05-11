#!/usr/bin/env python3
"""lofi — native macOS streaming audio player with animated equalizer."""

import json
import math
import os
import random
import socket
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

import shutil as _shutil
_MPV = _shutil.which("mpv") or "/opt/homebrew/bin/mpv"

def _subprocess_env():
    """Minimal clean env for subprocesses — avoids py2app vars that corrupt child Python processes."""
    return {
        "PATH":   os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
        "HOME":   str(Path.home()),
        "USER":   os.environ.get("USER", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG":   os.environ.get("LANG", "en_US.UTF-8"),
    }

CONFIG_DIR  = Path.home() / ".config" / "lofi"
CONFIG_FILE = CONFIG_DIR / "config.json"

TICK_MS = 40  # ~25 fps

# ── Palette ───────────────────────────────────────────────────────────────────
BG        = "#0c0c16"
FG        = "#c0c0da"
DIM       = "#44445a"
ACCENT    = "#8787ff"
GREEN     = "#87d75f"
YELLOW    = "#ffd75f"
BORDER    = "#22224a"
ENTRY_BG  = "#12121e"
PEAK_CLR  = "#d7afd7"

EQ_GRAD = [
    "#5050a0",  # 0 – deep indigo   (bottom)
    "#6060b8",
    "#7070d0",
    "#8787ff",  # 3 – periwinkle
    "#9f9fff",
    "#b5b5ff",
    "#c9aff5",
    "#d7afd7",  # 7 – pink-lavender (top)
]

def _eq_color(zone: float) -> str:
    """zone 0 = bottom, 1 = top."""
    idx = min(len(EQ_GRAD) - 1, int(zone * len(EQ_GRAD)))
    return EQ_GRAD[idx]


# ── Persistence ───────────────────────────────────────────────────────────────

def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {"last_url": "", "last_title": ""}

def save_config(url: str, title: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps({"last_url": url, "last_title": title}))


# ── Title fetching ────────────────────────────────────────────────────────────

def fetch_title(url: str) -> str:
    try:
        r = subprocess.run(
            ["yt-dlp", "--get-title", "--no-playlist", "-q", "--no-warnings", url],
            capture_output=True, text=True, timeout=10, env=_subprocess_env(),
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", url],
            capture_output=True, text=True, timeout=10, env=_subprocess_env(),
        )
        if r.returncode == 0:
            tags = json.loads(r.stdout).get("format", {}).get("tags", {})
            for key in ("title", "Title", "icy-title", "StreamTitle"):
                if tags.get(key):
                    return tags[key]
    except Exception:
        pass
    return ""


# ── MPV controller ────────────────────────────────────────────────────────────

class MPV:
    def __init__(self):
        self._sock   = f"/tmp/sp-mpv-{os.getpid()}.sock"
        self._proc   = None
        self.playing = False
        self.paused  = False
        self._lock   = threading.Lock()
        self._meta: dict = {}
        self.duration: float | None = None

    def play(self, url: str) -> None:
        self.stop()
        self._proc = subprocess.Popen(
            [_MPV, "--no-video", "--no-terminal", "--really-quiet",
             f"--input-ipc-server={self._sock}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_subprocess_env(), cwd="/",
        )
        self.playing = True
        self.paused  = False
        threading.Thread(target=self._poll, daemon=True).start()

    def _cmd(self, obj: dict):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1.5)
            s.connect(self._sock)
            s.sendall(json.dumps(obj).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            s.close()
            return json.loads(buf.decode().strip().splitlines()[0])
        except Exception:
            return None

    def _poll(self) -> None:
        time.sleep(2)
        while self.playing:
            r = self._cmd({"command": ["get_property", "metadata"]})
            if r and r.get("error") == "success":
                with self._lock:
                    self._meta = r.get("data") or {}
            d = self._cmd({"command": ["get_property", "duration"]})
            if d and d.get("error") == "success" and isinstance(d.get("data"), (int, float)):
                self.duration = float(d["data"])
            time.sleep(4)

    def toggle_pause(self) -> None:
        self._cmd({"command": ["cycle", "pause"]})
        self.paused = not self.paused

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        self.playing  = False
        self.paused   = False
        self.duration = None

    @property
    def active(self) -> bool:
        return self.playing and not self.paused

    def live_title(self) -> str:
        with self._lock:
            m = dict(self._meta)
        for k in ("title", "icy-title", "StreamTitle", "TITLE"):
            if m.get(k):
                return m[k]
        return ""


# ── Equalizer ─────────────────────────────────────────────────────────────────

class Equalizer:
    N = 32

    def __init__(self):
        self.vals      = [0.0] * self.N
        self.tgts      = [0.0] * self.N
        self.peaks     = [0.0] * self.N
        self.peak_hold = [0]   * self.N
        self.active    = False
        threading.Thread(target=self._tick, daemon=True).start()

    def _tick(self) -> None:
        t = 0
        beat_next = random.randint(14, 30)
        while True:
            t += 1
            if self.active:
                beat = (t == beat_next)
                if beat:
                    beat_next = t + random.randint(12, 28)
                for i in range(self.N):
                    x = i / self.N
                    curve = (
                        0.85 * math.exp(-9  * (x - 0.18) ** 2) +
                        0.55 * math.exp(-6  * (x - 0.38) ** 2) +
                        0.30 * math.exp(-5  * (x - 0.60) ** 2) +
                        0.12
                    )
                    curve = min(1.0, curve)
                    if beat and i < int(self.N * 0.30):
                        curve = min(1.0, curve * 2.0)
                    if random.random() < 0.20:
                        self.tgts[i] = random.uniform(0.02, curve)
                for i in range(self.N):
                    self.vals[i] += (self.tgts[i] - self.vals[i]) * 0.28
                    if self.vals[i] >= self.peaks[i]:
                        self.peaks[i]     = self.vals[i]
                        self.peak_hold[i] = 18
                    elif self.peak_hold[i] > 0:
                        self.peak_hold[i] -= 1
                    else:
                        rate = 0.014 + (i / self.N) * 0.010
                        self.peaks[i] = max(0.0, self.peaks[i] - rate)
            else:
                for i in range(self.N):
                    self.vals[i]  *= 0.82
                    self.peaks[i]  = max(0.0, self.peaks[i] - 0.025)
            time.sleep(0.040)

    def snapshot(self):
        return list(self.vals), list(self.peaks)


# ── App window ────────────────────────────────────────────────────────────────

class App(tk.Tk):
    W    = 500
    EQ_H = 130
    PAD  = 16

    def __init__(self):
        super().__init__()
        try:
            from AppKit import NSApplication, NSImage
            nsapp = NSApplication.sharedApplication()
            nsapp.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
            icon_path = Path(__file__).parent / "lofi.icns"
            if icon_path.exists():
                icon = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
                if icon:
                    NSApplication.sharedApplication().setApplicationIconImage_(icon)
        except Exception:
            pass
        cfg          = load_config()
        self._url    = cfg["last_url"]
        self._ttl    = cfg["last_title"]
        self.mpv     = MPV()
        self.eq      = Equalizer()
        self._play_start:    float | None = None
        self._paused_total:  float        = 0.0
        self._paused_since:  float | None = None
        self._build()
        self._tick()

    def _build(self) -> None:
        self.title("lofi")
        self.resizable(False, False)
        self.configure(bg=BG)
        self.wm_attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.createcommand("::tk::mac::Quit", self._on_close)
        import signal
        signal.signal(signal.SIGTERM, lambda *_: self.after(0, self._on_close))

        P = self.PAD
        F    = ("Menlo", 9)
        F_SM = ("Menlo", 9)

        # ── State label — floats at traffic-light level via place() ──────────
        # y is refined by _setup_native_window once the real titlebar height is known
        self._state_lbl = tk.Label(self, text="■  STOPPED", font=F, fg=DIM, bg=BG)
        self._state_lbl.place(relx=1.0, x=-P, y=6, anchor="ne")

        # ── URL row ───────────────────────────────────────────────────────────
        self._url_row = tk.Frame(self, bg=BG)
        url_row = self._url_row
        url_row.pack(fill="x", padx=P, pady=(36, 3))

        tk.Label(url_row, text="URL ›", font=F, fg=DIM, bg=BG, width=5,
                 anchor="w").pack(side="left")

        self._url_var = tk.StringVar(value=self._url)
        self._url_entry = tk.Entry(
            url_row,
            textvariable=self._url_var,
            font=F, fg=ACCENT, bg=ENTRY_BG,
            insertbackground=ACCENT,
            relief="flat", bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
        )
        self._url_entry.pack(side="left", fill="x", expand=True,
                             padx=(6, 0), ipady=4)
        self._url_entry.bind("<Return>", self._on_play)
        self._url_entry.bind("<Escape>", lambda _: self.focus_set())
        self._url_entry.bind("<FocusIn>",  self._url_focus_in)
        self._url_entry.bind("<FocusOut>", self._url_focus_out)
        self._placeholder = "paste a stream URL and press ↵"
        if not self._url:
            self._url_entry.insert(0, self._placeholder)
            self._url_entry.config(fg=DIM)

        # ── Title row ─────────────────────────────────────────────────────────
        ttl_row = tk.Frame(self, bg=BG)
        ttl_row.pack(fill="x", padx=P, pady=(0, 8))

        tk.Label(ttl_row, text="TTL ›", font=F, fg=DIM, bg=BG, width=5,
                 anchor="w").pack(side="left")
        self._ttl_lbl = tk.Label(
            ttl_row, text=self._ttl or "—",
            font=F, fg=FG, bg=BG, anchor="w",
        )
        self._ttl_lbl.pack(side="left", fill="x", expand=True, padx=(6, 0))

        # ── EQ canvas ─────────────────────────────────────────────────────────
        self._canvas = tk.Canvas(
            self, height=self.EQ_H, bg=BG,
            highlightthickness=0, bd=0,
        )
        self._canvas.pack(fill="x", padx=P, pady=(10, 8))

        # ── Divider ───────────────────────────────────────────────────────────
        tk.Frame(self, height=1, bg=BORDER).pack(fill="x", padx=P)

        # ── Status bar ────────────────────────────────────────────────────────
        bot = tk.Frame(self, bg=BG)
        bot.pack(fill="x", padx=P, pady=(6, P))

        self._status_lbl = tk.Label(
            bot, text="press Enter to play",
            font=F_SM, fg=DIM, bg=BG, anchor="w",
        )
        self._status_lbl.pack(side="left")
        self._hint_lbl = tk.Label(bot, text="[↵] play  [⎵] pause  [U] url  [T] top ●",
                 font=F_SM, fg=DIM, bg=BG)
        self._hint_lbl.pack(side="right")

        # ── Keyboard bindings ─────────────────────────────────────────────────
        self.bind("<Return>", self._on_play)
        self.bind("<space>",  self._on_space)
        self.bind("u",        self._focus_url)
        self.bind("U",        self._focus_url)
        self.bind("t",        self._toggle_topmost)
        self.bind("T",        self._toggle_topmost)
        self._topmost = True

        # ── Drag-to-move (all non-interactive surfaces) ───────────────────────
        for w in (self, self._canvas, bot):
            w.bind("<Button-1>",  self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # Fix window width after widgets are placed
        self.update_idletasks()
        h = self.winfo_reqheight()
        self.geometry(f"{self.W}x{h}")

        self.after(100, self._setup_native_window)

    def _setup_native_window(self, attempt: int = 0) -> None:
        try:
            from AppKit import NSApplication, NSColor, NSAppearance
            nsapp = NSApplication.sharedApplication()

            nswin = nsapp.mainWindow() or nsapp.keyWindow()
            if nswin is None:
                for w in nsapp.windows():
                    if w.title() == "lofi":
                        nswin = w
                        break

            if nswin is None:
                if attempt < 8:
                    self.after(150, lambda: self._setup_native_window(attempt + 1))
                return

            dark = NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
            bg   = NSColor.colorWithRed_green_blue_alpha_(
                0x0c/255, 0x0c/255, 0x16/255, 1.0
            )
            FULL_SIZE = 1 << 15
            nswin.setStyleMask_(nswin.styleMask() | FULL_SIZE)
            nswin.setTitlebarAppearsTransparent_(True)
            nswin.setTitleVisibility_(1)
            nswin.setMovableByWindowBackground_(True)
            nswin.setBackgroundColor_(bg)
            nswin.setAppearance_(dark)

            content_h  = nswin.contentView().frame().size.height
            layout_h   = nswin.contentLayoutRect().size.height
            titlebar_h = content_h - layout_h
            lbl_y      = max(2, round(titlebar_h / 2) - 8)
            self._state_lbl.place_configure(y=lbl_y)
            self._url_row.pack_configure(pady=(max(28, titlebar_h) + 8, 3))
        except Exception:
            pass

    # ── Drag-to-move ──────────────────────────────────────────────────────────

    def _drag_start(self, event) -> None:
        self._drag_ox = event.x_root - self.winfo_x()
        self._drag_oy = event.y_root - self.winfo_y()

    def _drag_move(self, event) -> None:
        self.geometry(f"+{event.x_root - self._drag_ox}+{event.y_root - self._drag_oy}")

    # ── State helpers ─────────────────────────────────────────────────────────

    def _set_state(self) -> None:
        if self.mpv.active:
            self._state_lbl.config(text="▶  PLAYING", fg=GREEN)
        elif self.mpv.paused:
            self._state_lbl.config(text="⏸  PAUSED",  fg=YELLOW)
        else:
            self._state_lbl.config(text="■  STOPPED", fg=DIM)

    def _set_status(self, text: str) -> None:
        self._status_lbl.config(text=text)

    # ── Actions ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fmt_time(secs: float) -> str:
        s = int(secs)
        h, rem = divmod(s, 3600)
        m, s   = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _elapsed(self) -> float:
        if not self._play_start:
            return 0.0
        base = self._paused_since if (self.mpv.paused and self._paused_since) else time.time()
        return base - self._play_start - self._paused_total

    def _on_play(self, _=None) -> None:
        url = self._url_var.get().strip()
        if not url or url == self._placeholder:
            return
        self._url = url
        self._set_status("connecting…")
        self.eq.active    = True
        self._play_start  = time.time()
        self._paused_total = 0.0
        self._paused_since = None
        self.mpv.play(url)
        self._set_state()
        self.focus_set()
        threading.Thread(target=self._bg_title, args=(url,), daemon=True).start()

    def _on_space(self, _=None) -> None:
        if self.focus_get() is self._url_entry:
            return
        if self.mpv.playing:
            if not self.mpv.paused:
                self._paused_since = time.time()
            elif self._paused_since is not None:
                self._paused_total += time.time() - self._paused_since
                self._paused_since  = None
            self.mpv.toggle_pause()
            self.eq.active = self.mpv.active
            self._set_state()

    def _focus_url(self, _=None) -> None:
        self._url_entry.focus_set()
        self._url_entry.select_range(0, "end")

    def _url_focus_in(self, _=None) -> None:
        if self._url_var.get() == self._placeholder:
            self._url_entry.delete(0, "end")
            self._url_entry.config(fg=ACCENT)

    def _url_focus_out(self, _=None) -> None:
        if not self._url_var.get():
            self._url_entry.insert(0, self._placeholder)
            self._url_entry.config(fg=DIM)

    def _toggle_topmost(self, _=None) -> None:
        if self.focus_get() is self._url_entry:
            return
        self._topmost = not self._topmost
        self.wm_attributes("-topmost", self._topmost)
        dot = "●" if self._topmost else "○"
        self._hint_lbl.config(text=f"[↵] play  [⎵] pause  [U] url  [T] top {dot}")

    def _bg_title(self, url: str) -> None:
        self.after(0, lambda: self._ttl_lbl.config(text="fetching…"))
        t   = fetch_title(url)
        ttl = t or url.rstrip("/").split("/")[-1] or url
        self._ttl = ttl
        save_config(url, ttl)
        self.after(0, lambda: self._ttl_lbl.config(text=ttl))

    # ── Animation ─────────────────────────────────────────────────────────────

    def _draw_eq(self) -> None:
        c = self._canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = self.EQ_H
        if cw < 4:
            return

        vals, peaks = self.eq.snapshot()
        n     = len(vals)
        gap   = 2
        bar_w = max(3, (cw - gap * (n - 1)) // n)
        total = n * bar_w + (n - 1) * gap
        x0    = (cw - total) // 2

        for i in range(n):
            x1     = x0 + i * (bar_w + gap)
            x2     = x1 + bar_w
            v      = vals[i]
            p      = peaks[i]
            bar_px = int(v * ch)

            if bar_px > 1:
                segs = min(bar_px, 8)
                for s in range(segs):
                    y2 = ch - int(s       * bar_px / segs)
                    y1 = ch - int((s + 1) * bar_px / segs)
                    if y1 < y2:
                        c.create_rectangle(x1, y1, x2, y2,
                                           fill=_eq_color(s / segs), outline="")

            peak_y = ch - int(p * ch)
            if p > 0.04 and peak_y < ch - bar_px - 1:
                c.create_rectangle(x1, peak_y, x2, peak_y + 2,
                                   fill=PEAK_CLR, outline="")

    def _tick(self) -> None:
        self._draw_eq()
        live = self.mpv.live_title()
        if live and live != self._ttl_lbl.cget("text"):
            self._ttl_lbl.config(text=live)
        if self.mpv.playing and self._play_start:
            elapsed = self._fmt_time(self._elapsed())
            dur = self.mpv.duration
            text = f"{elapsed} / {self._fmt_time(dur)}" if dur else elapsed
            self._status_lbl.config(text=text)
        elif not self.mpv.playing and self._play_start is None:
            self._status_lbl.config(text="press Enter to play")
        self.after(TICK_MS, self._tick)

    def _on_close(self) -> None:
        self.mpv.stop()
        self._play_start = None
        self.quit()
        self.destroy()


def main() -> None:
    if not _check_mpv():
        return
    App().mainloop()


def _check_mpv() -> bool:
    try:
        subprocess.run(["mpv", "--version"], capture_output=True, timeout=3, env=_subprocess_env())
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        import tkinter.messagebox as mb
        root = tk.Tk()
        root.withdraw()
        mb.showerror("Stream Player", "mpv is required.\n\nbrew install mpv")
        root.destroy()
        return False


if __name__ == "__main__":
    main()
