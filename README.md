# lofi stream player

A minimal native macOS app for streaming internet radio and audio URLs, with an animated equalizer display.

![lofi stream player](screenshot.png)

## Using the release build

Download the latest `lofi-vX.X-macos.zip` from [Releases](../../releases), unzip, and move `lofi.app` to `/Applications`.

**Dependency:**
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — required for YouTube URL resolution and title fetching

```bash
brew install yt-dlp
```

**First launch:** macOS will block the app because it is unsigned. Go to **System Settings → Privacy & Security** and click **Open Anyway**, then confirm in the dialog that appears.

## Building from source

**Dependencies:**

- Python 3.13
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [py2app](https://py2app.readthedocs.io/)
- [pyobjc-framework-AVFoundation](https://pypi.org/project/pyobjc-framework-AVFoundation/) and [pyobjc-framework-CoreMedia](https://pypi.org/project/pyobjc-framework-CoreMedia/)

```bash
brew install yt-dlp
pip install py2app pyobjc-framework-AVFoundation pyobjc-framework-CoreMedia --break-system-packages
```

**Build and install to `/Applications`:**

```bash
./build.sh
```

**Build and publish a GitHub release:**

```bash
./build.sh --release
```

## Running without building

```bash
python3 lofi.py
```

## Usage

| Key | Action |
|-----|--------|
| `U` | Focus the URL field |
| `↵` | Play the entered URL |
| `Space` | Pause / resume |
| `T` | Toggle always-on-top |

Paste any direct audio stream URL or a YouTube URL into the URL field and press Enter. The last played URL is remembered between sessions.

On first play of a URL, resolution takes ~10–15 seconds while yt-dlp fetches the stream. Subsequent plays of the same URL are instant — the resolved stream URL is cached for its full validity period (~6 hours for YouTube). The app also pre-resolves the saved URL in the background on launch, so it is usually ready by the time you press Enter.

## How it works

Audio is played natively via macOS **AVFoundation** (`AVPlayer`) — no external player binary required. For YouTube URLs, yt-dlp resolves the HLS stream URL and fetches the title in a single call before handing it off to AVPlayer.

## Configuration

Settings are stored in `~/.config/lofi/config.json` and contain only the last used URL and title.
