# SONA

**A free, non-commercial synchronized lyrics overlay for Windows.**

SONA is a lightweight desktop application that displays synchronized song lyrics over your screen while you listen to music. It is designed to be minimal, unobtrusive, and easy to use.

## Features

- Live synchronized lyrics.
- Transparent, borderless overlay.
- Always-on-top display.
- Automatically detects supported Windows media sessions.
- Fetches synchronized lyrics using LiriQo and LRCLIB.
- Caches lyrics locally to reduce repeated requests.
- Editable overlay positioning and settings.
- No SONA account or Spotify API key required.

## Requirements

- Windows 10 or later.
- Python 3.11 or later.
- Spotify desktop app or another supported Windows media session.
- Internet connection for retrieving lyrics.

## Installation

1. Install Python from [python.org](https://www.python.org/downloads/).
2. Download or clone this repository.
3. Open the project folder.
4. Open Command Prompt in that folder.
5. Install the required dependencies:

```bash
py -3 -m pip install -r requirements.txt
```

6. Double-click `Launch_SONA.bat` to start SONA.

## Controls

| Shortcut | Action |
|---|---|
| `Ctrl + Alt + L` | Toggle edit mode |
| Right-click overlay | Open context menu |

**Edit mode:** Use this mode to adjust the overlay. Press the shortcut again to return to normal display mode.

## How It Works

SONA reads currently playing media information from Windows' media session system.

It uses the track title and artist to search for synchronized lyrics through external lyric services.

Lyrics are loaded and cached locally. SONA uses playback timing information to keep the displayed lyrics synchronized.

SONA does not control Spotify playback.

## Privacy

SONA does not require an account.

To retrieve lyrics, track information such as the song title, artist, and available duration is sent to the lyric services used by the application.

Local settings and cached lyrics are stored in:

```text
%LOCALAPPDATA%\SONA-Lyrics\
```

This folder contains files such as:

- `settings.json`
- `lyrics-cache.json`

SONA does not include built-in advertising or analytics in the inspected source code.

See [PRIVACY.md](PRIVACY.md) for more information.

## Licensing

SONA is distributed under the **PolyForm Noncommercial License 1.0.0**.

You may use, modify, and redistribute SONA for permitted non-commercial purposes under the license terms.

**You may not:**
- Sell SONA.
- Sell modified versions of SONA.
- Charge people to download or access SONA.
- Bundle SONA into a paid product or service.
- Use SONA commercially without the required permission.

SONA is intended to remain free.

Read the complete [LICENSE](LICENSE) before using or distributing the project.

## Third-Party Software and Services

SONA uses third-party software and services, including:

- PySide6
- PyWinRT / Windows Media Control
- LiriQo
- LRCLIB

These projects and services have their own terms and licenses.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Disclaimer

SONA is an independent project and is not affiliated with, endorsed by, or sponsored by Spotify, Microsoft, LiriQo, or LRCLIB.

Lyrics may be inaccurate, unavailable, or incorrectly synchronized. Availability depends on external services.

See [DISCLAIMER.md](DISCLAIMER.md).

## Contributing

Contributions, improvements, and bug reports are welcome, provided they comply with the project's license.

Please keep SONA free for non-commercial use.

---

**SONA — Simple. Synchronized. Unobtrusive.**