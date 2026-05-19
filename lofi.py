#!/usr/bin/env python3
"""lofi — native macOS streaming audio player with animated equalizer."""

import json
import math
import os
import random
import re
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path

os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

import objc
from Foundation import NSURL
from AVFoundation import AVPlayer, AVPlayerItem
from CoreMedia import CMTimeGetSeconds, CMTimeMakeWithSeconds

# Load MediaPlayer framework for remote command / now-playing integration.
# pyobjc-framework-MediaPlayer is not always installed, so we load the bundle
# manually and fall back silently if unavailable.
_MP_OK = False
_MPRemoteCommandCenter = None
_MPNowPlayingInfoCenter = None
try:
    _MP: dict = {}
    objc.loadBundle(
        "MediaPlayer",
        bundle_path="/System/Library/Frameworks/MediaPlayer.framework",
        module_globals=_MP,
    )
    _MPRemoteCommandCenter   = _MP["MPRemoteCommandCenter"]
    _MPNowPlayingInfoCenter  = _MP["MPNowPlayingInfoCenter"]
    _MP_OK = True
except Exception:
    pass

# Known string constants from the MediaPlayer SDK.
_MP_TITLE         = "title"
_MP_PLAYBACK_RATE = "MPNowPlayingInfoPropertyPlaybackRate"
_MP_ELAPSED       = "MPNowPlayingInfoPropertyElapsedPlaybackTime"


# Helper ObjC object that receives remote-command callbacks via addTarget:action:
# and forwards them to a Python callable.  We define it only when the framework
# loaded successfully so the NSObject import doesn't fail in degenerate envs.
if _MP_OK:
    from AppKit import NSObject as _NSObject

    class _CommandHandler(_NSObject):  # type: ignore[name-defined]
        def initWithCallback_(self, cb):
            self = objc.super(_CommandHandler, self).init()
            if self is None:
                return None
            self._cb = cb
            return self

        # Signature: NSInteger (id, SEL, id) — MPRemoteCommandHandlerStatus return.
        def handleCommand_(self, event):
            self._cb()
            return 0  # MPRemoteCommandHandlerStatusSuccess
        handleCommand_ = objc.selector(handleCommand_, signature=b"q@:@")


def _subprocess_env():
    """Minimal clean env for subprocesses — avoids py2app vars that corrupt child Python processes."""
    return {
        "PATH":   os.environ.get("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
        "HOME":   str(Path.home()),
        "USER":   os.environ.get("USER", ""),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG":   os.environ.get("LANG", "en_US.UTF-8"),
    }


def _yt_dlp_bin() -> str:
    import shutil
    return shutil.which("yt-dlp") or "/opt/homebrew/bin/yt-dlp"


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
        cfg = json.loads(CONFIG_FILE.read_text())
        cfg.setdefault("stations", [])
        return cfg
    except Exception:
        return {"last_url": "", "last_title": "", "stations": []}


# ── URL resolution & title fetching ──────────────────────────────────────────

_FMT = "91/92/93/140/bestaudio[ext=m4a]/bestaudio/best"
_EXPIRE_RE = re.compile(r'/expire/(\d+)/')


def _stream_expires(url: str) -> float:
    """Extract expiry unix timestamp from a Google CDN URL, or 0 if not found."""
    m = _EXPIRE_RE.search(url)
    return float(m.group(1)) if m else 0.0


def resolve_and_get_title(url: str) -> tuple[str, str]:
    """Return (stream_url, title) via yt-dlp. Falls back to (url, '') on failure."""
    ytdlp = _yt_dlp_bin()
    try:
        r = subprocess.run(
            [ytdlp, "-f", _FMT, "--print", "%(title)s|||%(url)s",
             "--no-playlist", "-q", "--no-warnings", url],
            capture_output=True, text=True, timeout=60, env=_subprocess_env(),
        )
        if r.returncode == 0 and r.stdout.strip():
            line = r.stdout.strip().splitlines()[0]
            if "|||" in line:
                title, stream = line.split("|||", 1)
                if stream.strip():
                    return stream.strip(), title.strip()
    except Exception:
        pass
    return url, ""


# ── AVFoundation player ───────────────────────────────────────────────────────

class Player:
    def __init__(self):
        self._player  = None
        self.playing  = False
        self.paused   = False
        self._lock    = threading.Lock()
        self._meta: dict = {}
        self.duration: float | None = None

    def play(self, url: str) -> None:
        self.stop()
        ns_url       = NSURL.URLWithString_(url)
        item         = AVPlayerItem.playerItemWithURL_(ns_url)
        self._player = AVPlayer.playerWithPlayerItem_(item)
        self._player.setVolume_(1.0)
        self._player.play()
        self.playing = True
        self.paused  = False
        threading.Thread(target=self._poll, daemon=True).start()

    def error(self) -> str | None:
        """Return a human-readable error string if the current item failed, else None."""
        if not self._player:
            return None
        item = self._player.currentItem()
        if item and item.status() == 2:  # AVPlayerItemStatusFailed
            err = item.error()
            return str(err) if err else "unknown playback error"
        return None

    def _poll(self) -> None:
        time.sleep(2)
        while self.playing and self._player:
            item = self._player.currentItem()
            if item:
                # Timed metadata — covers ICY/StreamTitle on HTTP streams
                try:
                    meta = {}
                    timed = item.timedMetadata()
                    if timed:
                        for m in timed:
                            k = str(m.commonKey() or m.key() or "")
                            v = m.stringValue() or ""
                            if k and v:
                                meta[k] = str(v)
                    with self._lock:
                        self._meta = meta
                except Exception:
                    pass
                # Duration (live streams return kCMTimeIndefinite — skip those)
                try:
                    dur = CMTimeGetSeconds(item.duration())
                    if dur and 0 < dur < 1e9:
                        self.duration = dur
                except Exception:
                    pass
            time.sleep(4)

    def current_time(self) -> float:
        if not self._player:
            return 0.0
        try:
            return float(CMTimeGetSeconds(self._player.currentTime()))
        except Exception:
            return 0.0

    def seek(self, position: float) -> None:
        if not self._player or self.duration is None:
            return
        try:
            t = CMTimeMakeWithSeconds(max(0.0, min(position, self.duration)), 600)
            self._player.seekToTime_(t)
        except Exception:
            pass

    def toggle_pause(self) -> None:
        if not self._player:
            return
        if self.paused:
            self._player.play()
        else:
            self._player.pause()
        self.paused = not self.paused

    def stop(self) -> None:
        if self._player:
            self._player.pause()
            self._player = None
        self.playing  = False
        self.paused   = False
        self.duration = None
        with self._lock:
            self._meta = {}

    @property
    def active(self) -> bool:
        return self.playing and not self.paused

    def live_title(self) -> str:
        with self._lock:
            m = dict(self._meta)
        for k in ("title", "icy-title", "StreamTitle", "TITLE", "commonTitle"):
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
    W      = 500
    EQ_H   = 130
    PAD    = 16
    MINI_H = 34

    def __init__(self):
        super().__init__()
        try:
            from AppKit import NSApplication, NSImage
            nsapp = NSApplication.sharedApplication()
            nsapp.setActivationPolicy_(0)  # NSApplicationActivationPolicyRegular — show dock icon
            icon_path = Path(__file__).parent / "lofi.icns"
            if icon_path.exists():
                icon = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
                if icon:
                    NSApplication.sharedApplication().setApplicationIconImage_(icon)
        except Exception:
            pass
        cfg              = load_config()
        self._url        = cfg["last_url"]
        self._ttl        = cfg["last_title"]
        self._stations: list[dict] = cfg["stations"]
        self.player     = Player()
        self.eq         = Equalizer()
        self._play_start:    float | None = None
        self._paused_total:  float        = 0.0
        self._paused_since:  float | None = None
        self._resolving: bool             = False
        self._cache: dict[str, tuple[str, str, float]] = {}
        self._nswin      = None
        self._mini       = False
        self._scrubbing  = False
        self._scrub_frac = 0.0
        if self._url:
            threading.Thread(target=self._bg_pre_resolve, args=(self._url,), daemon=True).start()
        self._build()
        self._setup_remote_commands()
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
        self._url_entry.bind("<Return>", self._on_entry_return)
        self._url_entry.bind("<Escape>", lambda _: self.focus_set())
        self._url_entry.bind("<FocusIn>",  self._url_focus_in)
        self._url_entry.bind("<FocusOut>", self._url_focus_out)
        self._placeholder = "paste a stream URL and press ↵"
        if not self._url:
            self._url_entry.insert(0, self._placeholder)
            self._url_entry.config(fg=DIM)

        # ── Title row ─────────────────────────────────────────────────────────
        ttl_row = tk.Frame(self, bg=BG)
        ttl_row.pack(fill="x", padx=P, pady=(0, 4))
        self._ttl_row = ttl_row

        tk.Label(ttl_row, text="TTL ›", font=F, fg=DIM, bg=BG, width=5,
                 anchor="w").pack(side="left")
        self._ttl_lbl = tk.Label(
            ttl_row, text=self._ttl or "—",
            font=F, fg=FG, bg=BG, anchor="w",
        )
        self._ttl_lbl.pack(side="left", fill="x", expand=True, padx=(6, 0))

        # ── Stations list ─────────────────────────────────────────────────────
        self._stations_frame = tk.Frame(self, bg=BG)
        self._stations_frame.pack(fill="x", padx=P)
        self._rebuild_stations()

        # ── EQ canvas ─────────────────────────────────────────────────────────
        self._canvas = tk.Canvas(
            self, height=self.EQ_H, bg=BG,
            highlightthickness=0, bd=0,
        )
        self._canvas.pack(fill="x", padx=P, pady=(10, 4))

        # ── Scrubber ──────────────────────────────────────────────────────────
        self._scrub_canvas = tk.Canvas(
            self, height=14, bg=BG,
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self._scrub_canvas.pack(fill="x", padx=P, pady=(0, 6))
        self._scrub_canvas.bind("<Button-1>",        self._scrub_click)
        self._scrub_canvas.bind("<B1-Motion>",       self._scrub_drag)
        self._scrub_canvas.bind("<ButtonRelease-1>", self._scrub_release)

        # ── Divider ───────────────────────────────────────────────────────────
        self._divider = tk.Frame(self, height=1, bg=BORDER)
        self._divider.pack(fill="x", padx=P)

        # ── Status bar ────────────────────────────────────────────────────────
        bot = tk.Frame(self, bg=BG)
        bot.pack(fill="x", padx=P, pady=(6, P))
        self._bot = bot

        self._status_lbl = tk.Label(
            bot, text="press Enter to play",
            font=F_SM, fg=DIM, bg=BG, anchor="w",
        )
        self._status_lbl.pack(side="left")
        self._hint_lbl = tk.Label(bot, text="[↵] play  [⎵] pause  [U] url  [S] save  [M] mini  [T] top ●",
                 font=F_SM, fg=DIM, bg=BG)
        self._hint_lbl.pack(side="right")

        # ── Keyboard bindings ─────────────────────────────────────────────────
        self.bind("<Return>", self._on_play)
        self.bind("<space>",  self._on_space)
        self.bind("u",        self._focus_url)
        self.bind("U",        self._focus_url)
        self.bind("t",        self._toggle_topmost)
        self.bind("T",        self._toggle_topmost)
        self.bind("s",        self._save_station)
        self.bind("S",        self._save_station)
        self.bind("m",        self._toggle_mini)
        self.bind("M",        self._toggle_mini)
        for i in range(1, 6):
            self.bind(str(i), lambda e, n=i: self._play_station_num(n))
        self._topmost = True

        # ── Mini bar frame (hidden until M is pressed) ────────────────────────
        MINI_PAD_X = 10
        MINI_PAD_Y = 5
        EQ_MINI_W  = 140
        self._mini_frame = tk.Frame(self, bg=BG, height=self.MINI_H)
        _mini_inner = tk.Frame(self._mini_frame, bg=BG)
        _mini_inner.pack(fill="both", expand=True,
                         padx=MINI_PAD_X, pady=MINI_PAD_Y)
        self._mini_canvas = tk.Canvas(
            _mini_inner, width=EQ_MINI_W,
            bg=BG, highlightthickness=0, bd=0,
        )
        self._mini_canvas.pack(side="left", fill="y")
        self._mini_state_lbl = tk.Label(
            _mini_inner, text="■", font=F, fg=DIM, bg=BG, padx=6,
        )
        self._mini_state_lbl.pack(side="left")
        self._mini_hint_lbl = tk.Label(
            _mini_inner, text="[M] expand  [⎵] pause",
            font=F_SM, fg=DIM, bg=BG,
        )
        self._mini_hint_lbl.pack(side="right")
        self._mini_title_lbl = tk.Label(
            _mini_inner, text="—", font=F, fg=FG, bg=BG, anchor="w",
        )
        self._mini_title_lbl.pack(side="left", padx=(4, 0))
        self._mini_scrub_canvas = tk.Canvas(
            _mini_inner, width=400,
            bg=BG, highlightthickness=0, bd=0, cursor="hand2",
        )
        self._mini_scrub_canvas.pack(side="left", padx=(8, 0))
        self._mini_scrub_canvas.bind("<Button-1>",        self._scrub_click)
        self._mini_scrub_canvas.bind("<B1-Motion>",       self._scrub_drag)
        self._mini_scrub_canvas.bind("<ButtonRelease-1>", self._scrub_release)

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

            self._nswin = nswin
            dark = NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
            bg   = NSColor.colorWithRed_green_blue_alpha_(
                0x0c/255, 0x0c/255, 0x16/255, 1.0
            )
            FULL_SIZE = 1 << 15
            nswin.setStyleMask_(nswin.styleMask() | FULL_SIZE)
            nswin.setTitlebarAppearsTransparent_(True)
            nswin.setTitleVisibility_(1)
            nswin.setMovableByWindowBackground_(False)
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

    # ── State helpers ─────────────────────────────────────────────────────────

    def _setup_remote_commands(self) -> None:
        if not _MP_OK:
            return
        # _remote_cmd: 0 = nothing, 1 = toggle, 2 = play, 3 = pause.
        # Written from the ObjC callback thread (no tkinter/Tcl interaction),
        # read and cleared on the main thread in _tick().  Any Python GIL
        # interaction with Tcl from the callback thread crashes Python 3.13
        # (PyEval_RestoreThread gets a NULL tstate on the main thread).
        self._remote_cmd = 0
        try:
            center = _MPRemoteCommandCenter.sharedCommandCenter()

            def on_toggle():
                self._remote_cmd = 1

            def on_play():
                self._remote_cmd = 2

            def on_pause():
                self._remote_cmd = 3

            h_toggle = _CommandHandler.alloc().initWithCallback_(on_toggle)
            h_play   = _CommandHandler.alloc().initWithCallback_(on_play)
            h_pause  = _CommandHandler.alloc().initWithCallback_(on_pause)

            center.togglePlayPauseCommand().addTarget_action_(h_toggle, b"handleCommand:")
            center.playCommand().addTarget_action_(h_play,   b"handleCommand:")
            center.pauseCommand().addTarget_action_(h_pause, b"handleCommand:")

            self._remote_handlers = (h_toggle, h_play, h_pause)
        except Exception:
            self._remote_cmd = 0

    def _update_now_playing(self) -> None:
        if not _MP_OK:
            return
        try:
            info: dict = {}
            title = self._ttl_lbl.cget("text")
            if title and title != "—":
                info[_MP_TITLE] = title
            info[_MP_PLAYBACK_RATE] = 1.0 if self.player.active else 0.0
            if self.player.active or self.player.paused:
                info[_MP_ELAPSED] = self._elapsed()
            _MPNowPlayingInfoCenter.defaultCenter().setNowPlayingInfo_(info or None)
        except Exception:
            pass

    def _set_state(self) -> None:
        if self.player.active:
            self._state_lbl.config(text="▶  PLAYING", fg=GREEN)
            self._mini_state_lbl.config(text="▶", fg=GREEN)
        elif self.player.paused:
            self._state_lbl.config(text="⏸  PAUSED",  fg=YELLOW)
            self._mini_state_lbl.config(text="⏸", fg=YELLOW)
        else:
            self._state_lbl.config(text="■  STOPPED", fg=DIM)
            self._mini_state_lbl.config(text="■", fg=DIM)
        self._update_now_playing()

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
        base = self._paused_since if (self.player.paused and self._paused_since) else time.time()
        return base - self._play_start - self._paused_total

    def _on_entry_return(self, _=None):
        self._on_play()
        return "break"  # stop event from propagating to the window <Return> binding

    def _on_play(self, _=None) -> None:
        url = self._url_var.get().strip()
        if not url or url == self._placeholder:
            return
        self._url = url
        self.player.stop()
        self.eq.active     = False
        self._play_start   = None
        self._paused_total = 0.0
        self._paused_since = None
        self._resolving = True
        self._set_state()
        self._set_status("resolving…")
        self._ttl_lbl.config(text="—")
        self.focus_set()
        threading.Thread(target=self._bg_resolve_and_play, args=(url,), daemon=True).start()

    def _bg_pre_resolve(self, url: str) -> None:
        stream, title = resolve_and_get_title(url)
        if stream and stream != url:
            self._cache[url] = (stream, title, _stream_expires(stream))

    def _bg_resolve_and_play(self, url: str) -> None:
        cached = self._cache.get(url)
        if cached:
            stream, title, expires = cached
            if expires == 0 or expires > time.time() + 300:
                self.after(0, lambda: self._start_play(url, stream, title))
                # Refresh cache in background for next play
                threading.Thread(target=self._bg_pre_resolve, args=(url,), daemon=True).start()
                return
        stream, title = resolve_and_get_title(url)
        if stream and stream != url:
            self._cache[url] = (stream, title, _stream_expires(stream))
        self.after(0, lambda: self._start_play(url, stream, title))

    def _start_play(self, original_url: str, stream_url: str, title: str) -> None:
        self._resolving = False
        try:
            self.player.play(stream_url)
        except Exception as exc:
            self._set_status(f"error: {exc}")
            self.eq.active = False
            self._set_state()
            return
        self.eq.active     = True
        self._play_start   = time.time()
        self._paused_total = 0.0
        self._paused_since = None
        self._set_state()
        ttl = title or original_url.rstrip("/").split("/")[-1] or original_url
        self._ttl = ttl
        self._url = original_url
        self._save_config()
        self._ttl_lbl.config(text=ttl)

    def _on_space(self, _=None) -> None:
        if self.focus_get() is self._url_entry:
            return
        if self.player.playing:
            if not self.player.paused:
                self._paused_since = time.time()
            elif self._paused_since is not None:
                self._paused_total += time.time() - self._paused_since
                self._paused_since  = None
            self.player.toggle_pause()
            self.eq.active = self.player.active
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
        self._hint_lbl.config(text=f"[↵] play  [⎵] pause  [U] url  [S] save  [M] mini  [T] top {dot}")

    # ── Mini / status-bar mode ────────────────────────────────────────────────

    def _toggle_mini(self, _=None) -> None:
        if self.focus_get() is self._url_entry:
            return
        self._mini = not self._mini
        if self._mini:
            self._enter_mini()
        else:
            self._exit_mini()

    def _enter_mini(self) -> None:
        self._pre_mini_geometry = self.geometry()
        self._state_lbl.place_forget()
        self._url_row.pack_forget()
        self._ttl_row.pack_forget()
        self._stations_frame.pack_forget()
        self._canvas.pack_forget()
        self._scrub_canvas.pack_forget()
        self._divider.pack_forget()
        self._bot.pack_forget()
        self._mini_frame.pack(fill="both", expand=True)
        self._set_nswin_borderless(True)
        x, tk_y, w = self._mini_bar_geometry()
        self.resizable(True, False)
        self.geometry(f"{w}x{self.MINI_H}+{x}+{tk_y}")

    def _exit_mini(self) -> None:
        self._mini_frame.pack_forget()
        P = self.PAD
        self._url_row.pack(fill="x", padx=P, pady=(36, 3))
        self._ttl_row.pack(fill="x", padx=P, pady=(0, 4))
        self._stations_frame.pack(fill="x", padx=P)
        self._canvas.pack(fill="x", padx=P, pady=(10, 4))
        self._scrub_canvas.pack(fill="x", padx=P, pady=(0, 6))
        self._divider.pack(fill="x", padx=P)
        self._bot.pack(fill="x", padx=P, pady=(6, P))
        self._state_lbl.place(relx=1.0, x=-P, y=6, anchor="ne")
        self._set_nswin_borderless(False)
        self.resizable(False, False)
        self._resize_window()
        # Restore position from before mini mode (keep saved x/y, recalc height)
        saved = getattr(self, "_pre_mini_geometry", None)
        if saved:
            import re as _re
            m = _re.match(r"\d+x\d+\+(-?\d+)\+(-?\d+)", saved)
            if m:
                self.update_idletasks()
                h = self.winfo_reqheight()
                self.geometry(f"{self.W}x{h}+{m.group(1)}+{m.group(2)}")
        self.after(200, self._setup_native_window)

    def _mini_bar_geometry(self) -> tuple[int, int, int]:
        """Return (x, tk_y, width) to dock mini bar just above the macOS dock."""
        try:
            from AppKit import NSScreen
            vf = NSScreen.mainScreen().visibleFrame()
            sh = self.winfo_screenheight()
            x      = int(vf.origin.x)
            tk_y   = sh - int(vf.origin.y) - self.MINI_H
            width  = int(vf.size.width)
            return x, tk_y, width
        except Exception:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            return 0, sh - self.MINI_H, sw

    def _set_nswin_borderless(self, borderless: bool) -> None:
        try:
            if self._nswin is None:
                return
            if borderless:
                self._nswin.setStyleMask_(0)  # NSWindowStyleMaskBorderless
            else:
                TITLED = 1; CLOSABLE = 2; MINIATURIZABLE = 4; FULL_SIZE = 1 << 15
                self._nswin.setStyleMask_(TITLED | CLOSABLE | MINIATURIZABLE | FULL_SIZE)
                self._nswin.setTitlebarAppearsTransparent_(True)
                self._nswin.setTitleVisibility_(1)
        except Exception:
            pass

    # ── Animation ─────────────────────────────────────────────────────────────

    def _draw_eq(self) -> None:
        if self._mini:
            self._draw_eq_mini()
            return
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

    def _draw_eq_mini(self) -> None:
        c = self._mini_canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 4 or ch < 2:
            return
        vals, _ = self.eq.snapshot()
        n    = len(vals)  # 32 bars
        gap  = 1
        bar_w = max(2, (cw - gap * (n - 1)) // n)
        total = n * bar_w + (n - 1) * gap
        x0    = max(0, (cw - total) // 2)
        for i, v in enumerate(vals):
            x1     = x0 + i * (bar_w + gap)
            x2     = x1 + bar_w
            bar_px = max(1, int(v * ch))
            c.create_rectangle(x1, ch - bar_px, x2, ch,
                               fill=_eq_color(v), outline="")

    def _draw_scrubber(self) -> None:
        c = self._scrub_canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 4:
            return
        ty = ch // 2
        c.create_rectangle(0, ty - 1, cw, ty + 1, fill=DIM, outline="")
        dur = self.player.duration
        if not dur:
            return
        frac = self._scrub_frac if self._scrubbing else min(1.0, self.player.current_time() / dur)
        fx = int(frac * cw)
        if fx > 0:
            c.create_rectangle(0, ty - 1, fx, ty + 1, fill=ACCENT, outline="")
        r = 5
        c.create_oval(fx - r, ty - r, fx + r, ty + r, fill=ACCENT, outline="")

    def _draw_scrubber_mini(self) -> None:
        c = self._mini_scrub_canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 4 or ch < 2:
            return
        ty = ch // 2
        c.create_rectangle(0, ty - 1, cw, ty + 1, fill=DIM, outline="")
        dur = self.player.duration
        if not dur:
            return
        frac = self._scrub_frac if self._scrubbing else min(1.0, self.player.current_time() / dur)
        fx = int(frac * cw)
        if fx > 0:
            c.create_rectangle(0, ty - 1, fx, ty + 1, fill=ACCENT, outline="")
        r = 3
        c.create_oval(fx - r, ty - r, fx + r, ty + r, fill=ACCENT, outline="")

    def _scrub_click(self, event) -> None:
        if not self.player.duration:
            return
        self._scrubbing = True
        w = event.widget.winfo_width()
        self._scrub_frac = max(0.0, min(1.0, event.x / w)) if w > 0 else 0.0

    def _scrub_drag(self, event) -> None:
        if not self._scrubbing or not self.player.duration:
            return
        w = event.widget.winfo_width()
        self._scrub_frac = max(0.0, min(1.0, event.x / w)) if w > 0 else 0.0

    def _scrub_release(self, event) -> None:
        if not self._scrubbing:
            return
        self._scrubbing = False
        if not self.player.duration:
            return
        pos = self._scrub_frac * self.player.duration
        self.player.seek(pos)
        now = time.time()
        if self.player.paused and self._paused_since is not None:
            self._play_start = self._paused_since - pos - self._paused_total
        elif self._play_start is not None:
            self._play_start = now - pos - self._paused_total

    def _tick(self) -> None:
        # Drain remote-command flag set by the ObjC media-key callback.
        cmd, self._remote_cmd = self._remote_cmd, 0
        if cmd == 1:
            self._on_space()
        elif cmd == 2 and self.player.paused:
            self._on_space()
        elif cmd == 3 and self.player.active:
            self._on_space()
        self._draw_eq()
        if self._mini:
            self._draw_scrubber_mini()
        else:
            self._draw_scrubber()
        live = self.player.live_title()
        if live and live != self._ttl_lbl.cget("text"):
            self._ttl_lbl.config(text=live)
            self._update_now_playing()
        cur_title = self._ttl_lbl.cget("text")
        if self._mini_title_lbl.cget("text") != cur_title:
            self._mini_title_lbl.config(text=cur_title)
        err = self.player.error()
        if err:
            self.player.stop()
            self.eq.active   = False
            self._play_start = None
            self._set_state()
            self._set_status(f"error: {err}")
        elif self.player.playing and self._play_start:
            elapsed = self._fmt_time(self._elapsed())
            dur = self.player.duration
            text = f"{elapsed} / {self._fmt_time(dur)}" if dur else elapsed
            self._status_lbl.config(text=text)
        elif not self.player.playing and self._play_start is None and not self._resolving:
            self._status_lbl.config(text="press Enter to play")
        self.after(TICK_MS, self._tick)

    # ── Stations ──────────────────────────────────────────────────────────────

    def _save_config(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "last_url":   self._url,
            "last_title": self._ttl,
            "stations":   self._stations,
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2))

    def _rebuild_stations(self) -> None:
        for w in self._stations_frame.winfo_children():
            w.destroy()
        F = ("Menlo", 9)
        for i, st in enumerate(self._stations, 1):
            row = tk.Frame(self._stations_frame, bg=BG)
            row.pack(fill="x", pady=1)
            num_lbl = tk.Label(row, text=f"[{i}]", font=F, fg=DIM, bg=BG,
                               width=4, anchor="w")
            num_lbl.pack(side="left")
            raw = st.get("title") or st.get("url", "")
            title_text = raw if len(raw) <= 55 else raw[:52] + "…"
            del_btn = tk.Label(row, text="×", font=F, fg=DIM, bg=BG, cursor="hand2",
                               padx=4)
            del_btn.pack(side="right")
            title_lbl = tk.Label(row, text=title_text, font=F, fg=FG, bg=BG,
                                 anchor="w", cursor="hand2")
            title_lbl.pack(side="left", fill="x", expand=True, padx=(2, 0))
            url = st["url"]
            title_lbl.bind("<Button-1>", lambda e, u=url: self._play_station(u))
            num_lbl.bind("<Button-1>",   lambda e, u=url: self._play_station(u))
            del_btn.bind("<Button-1>",   lambda e, idx=i-1: self._remove_station(idx))
            for w in (title_lbl, num_lbl):
                w.bind("<Enter>", lambda e, lbl=title_lbl: lbl.config(fg=ACCENT))
                w.bind("<Leave>", lambda e, lbl=title_lbl: lbl.config(fg=FG))
            del_btn.bind("<Enter>", lambda e, btn=del_btn: btn.config(fg=ACCENT))
            del_btn.bind("<Leave>", lambda e, btn=del_btn: btn.config(fg=DIM))
        self._resize_window()

    def _resize_window(self) -> None:
        if self._mini:
            return
        self.update_idletasks()
        h = self.winfo_reqheight()
        self.geometry(f"{self.W}x{h}")

    def _save_station(self, _=None) -> None:
        if self.focus_get() is self._url_entry:
            return
        if not self._url or not self.player.playing:
            return
        for st in self._stations:
            if st["url"] == self._url:
                return
        if len(self._stations) >= 5:
            return
        self._stations.append({"url": self._url, "title": self._ttl or self._url})
        self._save_config()
        self._rebuild_stations()

    def _remove_station(self, idx: int) -> None:
        if 0 <= idx < len(self._stations):
            self._stations.pop(idx)
            self._save_config()
            self._rebuild_stations()

    def _play_station(self, url: str) -> None:
        self._url_var.set(url)
        self._url = url
        self._on_play()

    def _play_station_num(self, n: int) -> None:
        if self.focus_get() is self._url_entry:
            return
        if 1 <= n <= min(5, len(self._stations)):
            self._play_station(self._stations[n - 1]["url"])

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        self.player.stop()
        self._play_start = None
        self.quit()
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
