"""SONA — a borderless, locally-rendered lyrics companion for Spotify on Windows.

Requirements on the user's PC:
    py -m pip install PySide6 winrt-Windows.Media.Control

The application uses Windows Global System Media Transport Controls (GSMTC)
for Spotify metadata and playback position, and LiriQo + LRCLIB for lyrics.
It does not pause or control Spotify.
"""

from __future__ import annotations

import asyncio
import bisect
import ctypes
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPointF
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QBrush,
    QAction,
)
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QMessageBox


APP_NAME = "SONA"
APP_VERSION = "1.0.0"
LIRIQO_URL = "https://api.liriqo-alfarrizi.my.id/v1/lyrics"
LRCLIB_BASE = "https://lrclib.net/api"

DATA_DIR = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "SONA-Lyrics"
SETTINGS_PATH = DATA_DIR / "settings.json"
CACHE_PATH = DATA_DIR / "lyrics-cache.json"

DEFAULT_SETTINGS = {
    "x": 180,
    "y": 150,
    "width": 1120,
    "height": 360,
    "font_size": 52,
    "offset_ms": 0,
    "show_previous": True,
    "show_next": True,
    "diagnostics": False,
}

# ---------- Small, testable core helpers ----------


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def normalize_text(value: str) -> str:
    value = (value or "").casefold().strip()
    value = re.sub(r"\s+", " ", value)
    return value


def settings_load() -> dict:
    result = DEFAULT_SETTINGS.copy()
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as f:
            incoming = json.load(f)
        if isinstance(incoming, dict):
            result.update({k: v for k, v in incoming.items() if k in result})
    except (OSError, ValueError, TypeError):
        pass
    result["font_size"] = int(max(24, min(88, safe_float(result["font_size"], 52))))
    result["offset_ms"] = int(max(-2500, min(2500, safe_float(result["offset_ms"], 0))))
    result["width"] = int(max(520, min(2400, safe_float(result["width"], 1120))))
    result["height"] = int(max(200, min(1000, safe_float(result["height"], 360))))
    return result


def settings_save(data: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temp = SETTINGS_PATH.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp.replace(SETTINGS_PATH)
    except OSError:
        pass


def cache_load() -> dict:
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def cache_save(cache: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # Keep the newest 180 entries; avoid unbounded cache growth.
        if len(cache) > 180:
            ordered = sorted(
                cache.items(),
                key=lambda pair: safe_float(pair[1].get("saved_at"), 0)
                if isinstance(pair[1], dict) else 0,
                reverse=True,
            )[:180]
            cache = dict(ordered)
        temp = CACHE_PATH.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        temp.replace(CACHE_PATH)
    except OSError:
        pass


def cache_key(title: str, artist: str, duration: float) -> str:
    raw = "|".join((normalize_text(title), normalize_text(artist), str(int(round(duration)))))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_lrc(raw: str) -> list[dict]:
    """Parse LRC timestamps, multiple stamps per line, and the optional offset tag."""
    entries: list[dict] = []
    offset_seconds = 0.0
    stamp_pattern = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")

    for raw_line in (raw or "").splitlines():
        offset_match = re.search(r"\[offset:([+-]?\d+)\]", raw_line, re.IGNORECASE)
        if offset_match:
            offset_seconds = int(offset_match.group(1)) / 1000.0
            continue

        stamps = list(stamp_pattern.finditer(raw_line))
        if not stamps:
            continue
        lyric = stamp_pattern.sub("", raw_line).strip()
        if not lyric:
            continue
        for match in stamps:
            minutes = int(match.group(1))
            seconds = int(match.group(2))
            fraction = (match.group(3) or "0").ljust(3, "0")[:3]
            start = max(0.0, minutes * 60 + seconds + int(fraction) / 1000.0 + offset_seconds)
            entries.append({"start": start, "end": None, "text": lyric, "words": []})

    entries.sort(key=lambda line: line["start"])
    return dedupe_lines(entries)


def dedupe_lines(lines: list[dict]) -> list[dict]:
    clean: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for line in sorted(lines, key=lambda item: safe_float(item.get("start"), 0)):
        text = str(line.get("text") or "").strip()
        start = max(0.0, safe_float(line.get("start"), 0))
        if not text:
            continue
        marker = (round(start * 1000), text)
        if marker in seen:
            continue
        seen.add(marker)
        words = line.get("words") if isinstance(line.get("words"), list) else []
        clean.append({
            "start": start,
            "end": safe_float(line.get("end"), -1) if line.get("end") is not None else None,
            "text": text,
            "words": words,
        })
    return clean


def parse_liriqo_response(data: dict) -> tuple[list[dict], str, str]:
    """Choose the richest *actual* timing track; never invent word timestamps."""
    candidates: list[dict] = []
    primary = data.get("primary") if isinstance(data, dict) else None
    if isinstance(primary, dict):
        candidates.append(primary)
    for item in (data.get("tracks", []) if isinstance(data, dict) else []) or []:
        if isinstance(item, dict) and item not in candidates:
            candidates.append(item)

    score = {"syllable": 3, "word": 3, "character": 3, "line": 2, "plain": 1}
    candidates.sort(key=lambda item: score.get(str(item.get("syncLevel", "plain")).lower(), 0), reverse=True)

    for track in candidates:
        timed = track.get("timed") or []
        if not isinstance(timed, list) or not timed:
            continue

        top_words = track.get("words") if isinstance(track.get("words"), dict) else {}
        rows: list[dict] = []
        for row in timed:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            start = safe_float(row.get("start"), -1) / 1000.0
            end_raw = row.get("end")
            end = safe_float(end_raw, -1) / 1000.0 if end_raw is not None else None
            if not text or start < 0:
                continue

            raw_words = row.get("words")
            if not isinstance(raw_words, list):
                raw_words = top_words.get(str(row.get("id")), [])
                if not raw_words and row.get("id") in top_words:
                    raw_words = top_words[row.get("id")]
            words: list[dict] = []
            if isinstance(raw_words, list):
                for word in raw_words:
                    if not isinstance(word, dict):
                        continue
                    word_text = str(word.get("text") or "")
                    word_start = safe_float(word.get("start"), -1) / 1000.0
                    word_end = safe_float(word.get("end"), -1) / 1000.0
                    if word_text and word_start >= 0 and word_end >= word_start:
                        words.append({"text": word_text, "start": word_start, "end": word_end})
            words.sort(key=lambda word: word["start"])
            rows.append({"start": start, "end": end, "text": text, "words": words})

        rows = dedupe_lines(rows)
        if rows:
            # Trust the payload, not just its label. Some provider results are
            # line-synced even when metadata claims a richer tier.
            word_rows = sum(bool(row.get("words")) for row in rows)
            if word_rows == len(rows):
                sync_level = "word"
            elif word_rows:
                sync_level = "mixed"
            else:
                sync_level = "line"
            provider = str(track.get("upstreamProvider") or track.get("lyricsSource") or track.get("provider") or "LiriQo")
            return rows, sync_level, provider

    return [], "none", ""


def parse_plain_text(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def request_json(url: str, timeout: float = 18.0) -> tuple[dict | list, dict]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SONA/1.0.0 (https://github.com/FreePizza-the-real/SONA)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", errors="replace")
        return json.loads(body), dict(response.headers.items())


def fetch_liriqo(title: str, artist: str, duration: float) -> tuple[list[dict], str, str]:
    params = {"title": title, "artist": artist}
    if duration > 0:
        params["duration"] = str(int(round(duration)))
    # Default quality-aware mode: it favours the best verified sync available
    # without waiting for every provider as full=1 does.
    url = LIRIQO_URL + "?" + urllib.parse.urlencode(params)
    data, _headers = request_json(url, timeout=22.0)
    if not isinstance(data, dict) or data.get("error"):
        return [], "none", ""
    return parse_liriqo_response(data)


def fetch_lrclib(title: str, artist: str, duration: float) -> tuple[list[dict], str, str]:
    params = {"track_name": title, "artist_name": artist}
    if duration > 0:
        params["duration"] = str(int(round(duration)))
    exact_url = LRCLIB_BASE + "/get?" + urllib.parse.urlencode(params)

    data: dict | None = None
    try:
        result, _ = request_json(exact_url, timeout=7.0)
        if isinstance(result, dict):
            data = result
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        data = None

    if data and data.get("syncedLyrics"):
        lines = parse_lrc(str(data["syncedLyrics"]))
        if lines:
            return lines, "line", "LRCLIB"

    # Search fallback is stricter than simply taking the first result.
    search_params = {"track_name": title, "artist_name": artist}
    search_url = LRCLIB_BASE + "/search?" + urllib.parse.urlencode(search_params)
    results, _ = request_json(search_url, timeout=7.0)
    if isinstance(results, list):
        target_title = normalize_text(title)
        target_artist = normalize_text(artist)
        ranked = sorted(
            (item for item in results if isinstance(item, dict) and item.get("syncedLyrics")),
            key=lambda item: (
                normalize_text(str(item.get("trackName", ""))) == target_title,
                normalize_text(str(item.get("artistName", ""))) == target_artist,
                -abs(safe_float(item.get("duration"), duration) - duration),
            ),
            reverse=True,
        )
        for item in ranked:
            # Do not silently use a different song with a similar title.
            title_match = normalize_text(str(item.get("trackName", ""))) == target_title
            artist_match = normalize_text(str(item.get("artistName", ""))) == target_artist
            duration_match = duration <= 0 or abs(safe_float(item.get("duration"), duration) - duration) <= 3.0
            if title_match and artist_match and duration_match:
                lines = parse_lrc(str(item.get("syncedLyrics") or ""))
                if lines:
                    return lines, "line", "LRCLIB"

    return [], "none", ""


def load_or_fetch_lyrics(title: str, artist: str, duration: float) -> tuple[list[dict], str, str, str]:
    key = cache_key(title, artist, duration)
    cache = cache_load()
    saved = cache.get(key)
    if isinstance(saved, dict) and time.time() - safe_float(saved.get("saved_at"), 0) < 60 * 86400:
        try:
            rows = dedupe_lines(saved.get("lines", []))
            if rows:
                return rows, str(saved.get("sync_level", "line")), str(saved.get("provider", "CACHE")), "CACHE"
        except Exception:
            pass

    errors: list[str] = []
    try:
        lines, sync_level, provider = fetch_liriqo(title, artist, duration)
        if lines:
            cache[key] = {
                "saved_at": time.time(),
                "lines": lines,
                "sync_level": sync_level,
                "provider": provider,
            }
            cache_save(cache)
            return lines, sync_level, provider, "LIRIQO"
    except Exception as exc:
        errors.append(f"LiriQo: {str(exc)[:100]}")

    try:
        lines, sync_level, provider = fetch_lrclib(title, artist, duration)
        if lines:
            cache[key] = {
                "saved_at": time.time(),
                "lines": lines,
                "sync_level": sync_level,
                "provider": provider,
            }
            cache_save(cache)
            return lines, sync_level, provider, "LRCLIB"
    except Exception as exc:
        errors.append(f"LRCLIB: {str(exc)[:100]}")

    return [], "none", "", " | ".join(errors) if errors else "No synchronized lyrics were found."


# ---------- Windows media clock ----------


def enum_is_playing(status: Any) -> bool:
    """WinRT playback status is an IntEnum in Python; str(status) may be '4'."""
    name = getattr(status, "name", "")
    if str(name).casefold() == "playing":
        return True
    try:
        # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING == 4.
        return int(status) == 4
    except (TypeError, ValueError, OverflowError):
        return "playing" in str(status).casefold()


def enum_status_name(status: Any) -> str:
    name = getattr(status, "name", None)
    if name:
        return str(name).upper()
    try:
        number = int(status)
        return {0: "CLOSED", 1: "OPENED", 2: "CHANGING", 3: "STOPPED", 4: "PLAYING", 5: "PAUSED"}.get(number, str(number))
    except (TypeError, ValueError, OverflowError):
        return str(status).upper()


def datetime_epoch(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
    except Exception:
        return None
    return None


class SpotifyWatcher(QThread):
    track_changed = Signal(dict)
    position_sample = Signal(float, bool, float, float, float, str)
    status_changed = Signal(str)

    def __init__(self):
        super().__init__()
        self._running = True

    def stop(self):
        self._running = False
        self.wait(1800)

    def run(self):
        try:
            asyncio.run(self._watch())
        except Exception as exc:
            self.status_changed.emit("MEDIA SESSION ERROR: " + str(exc)[:100])

    async def _watch(self):
        try:
            from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
            manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        except Exception as exc:
            self.status_changed.emit(
                "WINDOWS MEDIA CONTROL UNAVAILABLE — " + str(exc)[:110]
            )
            return

        current_session_key = None
        current_track_key = None
        last_metadata_check = 0.0
        last_status = ""
        fallback_anchor_pos = 0.0
        fallback_anchor_perf = time.perf_counter()
        fallback_last_raw = None
        fallback_last_playing = False
        timeline_age_ms = 0.0
        last_playing_state = None
        last_updated_epoch_seen = None
        last_position_now = None
        last_sample_perf = None
        last_rate = 1.0
        resume_anchor = None
        paused_anchor = None
        loop = asyncio.get_running_loop()
        wake_event = asyncio.Event()
        event_session_marker = None
        event_session = None
        event_tokens = []

        def event_handler(_sender, _args):
            try:
                loop.call_soon_threadsafe(wake_event.set)
            except RuntimeError:
                # The media app can fire a late event as this worker is closing.
                pass

        def remove_session_events():
            nonlocal event_session, event_tokens
            if event_session is not None:
                for add_name, token in event_tokens:
                    try:
                        remove_name = add_name.replace("add_", "remove_", 1)
                        remover = getattr(event_session, remove_name, None)
                        if remover is not None:
                            remover(token)
                    except Exception:
                        pass
            event_session = None
            event_tokens = []

        manager_token = None
        try:
            if hasattr(manager, "add_sessions_changed"):
                manager_token = manager.add_sessions_changed(event_handler)
        except Exception:
            manager_token = None

        while self._running:
            # Clear before reading; events arriving during the read remain queued.
            wake_event.clear()
            try:
                sessions = list(manager.get_sessions() or [])
                spotify_sessions = [
                    session for session in sessions
                    if "spotify" in str(getattr(session, "source_app_user_model_id", "")).casefold()
                ]

                if not spotify_sessions:
                    if last_status != "WAITING FOR SPOTIFY":
                        self.status_changed.emit("WAITING FOR SPOTIFY")
                        self.track_changed.emit({"key": "", "clear": True})
                        last_status = "WAITING FOR SPOTIFY"
                    current_session_key = None
                    current_track_key = None
                    event_session_marker = None
                    remove_session_events()
                    try:
                        await asyncio.wait_for(wake_event.wait(), timeout=0.25)
                    except asyncio.TimeoutError:
                        pass
                    continue

                # Prefer an actively playing Spotify session if multiple sessions exist.
                session = spotify_sessions[0]
                for candidate in spotify_sessions:
                    try:
                        if enum_is_playing(candidate.get_playback_info().playback_status):
                            session = candidate
                            break
                    except Exception:
                        continue

                now_perf = time.perf_counter()
                session_marker = str(getattr(session, "source_app_user_model_id", "Spotify"))
                if session_marker != current_session_key:
                    current_session_key = session_marker
                    current_track_key = None
                    fallback_last_raw = None
                    last_metadata_check = 0.0
                    last_playing_state = None
                    last_updated_epoch_seen = None
                    last_position_now = None
                    last_sample_perf = None
                    resume_anchor = None
                    paused_anchor = None

                if session_marker != event_session_marker:
                    remove_session_events()
                    event_session_marker = session_marker
                    event_session = session
                    for method_name in (
                        "add_timeline_properties_changed",
                        "add_playback_info_changed",
                        "add_media_properties_changed",
                    ):
                        try:
                            method = getattr(session, method_name, None)
                            if method is not None:
                                token = method(event_handler)
                                event_tokens.append((method_name, token))
                        except Exception:
                            pass

                if now_perf - last_metadata_check >= 0.65:
                    last_metadata_check = now_perf
                    try:
                        media = await session.try_get_media_properties_async()
                        title = str(media.title or "").strip()
                        artist = str(media.artist or "").strip()
                        if title:
                            timeline_for_duration = session.get_timeline_properties()
                            duration = max(0.0, timeline_for_duration.end_time.total_seconds())
                            track_key = "|".join((normalize_text(title), normalize_text(artist)))
                            if track_key != current_track_key:
                                current_track_key = track_key
                                # Do not carry the previous song's clock anchor into a new track.
                                fallback_last_raw = None
                                last_playing_state = None
                                last_updated_epoch_seen = None
                                last_position_now = None
                                last_sample_perf = None
                                resume_anchor = None
                                paused_anchor = None
                                self.track_changed.emit({
                                    "key": track_key,
                                    "title": title,
                                    "artist": artist,
                                    "duration": duration,
                                })
                    except Exception:
                        pass

                timeline = session.get_timeline_properties()
                playback = session.get_playback_info()
                if timeline is None or playback is None:
                    await asyncio.sleep(0.06)
                    continue

                raw_position = max(0.0, timeline.position.total_seconds())
                rate_value = getattr(playback, "playback_rate", None)
                rate = safe_float(rate_value, 1.0)
                if rate <= 0 or rate > 4:
                    rate = 1.0
                playing = enum_is_playing(playback.playback_status)
                status_name = enum_status_name(playback.playback_status)
                updated_epoch = datetime_epoch(getattr(timeline, "last_updated_time", None))

                emit_perf = time.perf_counter()
                if updated_epoch is not None:
                    age = max(0.0, min(7200.0, time.time() - updated_epoch))
                    timeline_age_ms = age * 1000.0
                    source_advanced = (
                        last_updated_epoch_seen is None
                        or updated_epoch > last_updated_epoch_seen + 0.001
                    )

                    # Resume edge case: if Spotify reports PLAYING again before its
                    # timeline timestamp advances, start from the frozen pause point.
                    if playing and last_playing_state is False:
                        if paused_anchor is not None and updated_epoch <= paused_anchor[1] + 0.001:
                            resume_anchor = (paused_anchor[0], emit_perf, paused_anchor[1])
                        elif not source_advanced and last_position_now is not None:
                            resume_anchor = (last_position_now, emit_perf, last_updated_epoch_seen)
                        paused_anchor = None

                    if playing and resume_anchor is not None:
                        if updated_epoch > resume_anchor[2] + 0.001:
                            resume_anchor = None
                            position_now = raw_position + age * rate
                        else:
                            position_now = resume_anchor[0] + max(0.0, emit_perf - resume_anchor[1]) * rate
                    elif playing:
                        position_now = raw_position + age * rate
                    elif last_playing_state is True and last_position_now is not None and last_sample_perf is not None:
                        # Freeze at the predicted point if the pause event arrives
                        # before Windows refreshes the timeline timestamp.
                        predicted_at_pause = last_position_now + max(0.0, emit_perf - last_sample_perf) * last_rate
                        position_now = predicted_at_pause if not source_advanced else raw_position
                        paused_anchor = (position_now, updated_epoch)
                        resume_anchor = None
                    elif paused_anchor is not None:
                        # Stay frozen on repeated paused samples. A changed timestamp
                        # means a seek/update occurred while paused, so accept the new position.
                        if updated_epoch > paused_anchor[1] + 0.001:
                            position_now = raw_position
                            paused_anchor = (position_now, updated_epoch)
                        else:
                            position_now = paused_anchor[0]
                    else:
                        position_now = raw_position
                        resume_anchor = None

                    position_now = max(0.0, position_now)
                    fallback_anchor_pos = position_now
                    fallback_anchor_perf = emit_perf
                    fallback_last_raw = raw_position
                    fallback_last_playing = playing
                    clock_quality = "LASTUPDATEDTIME"
                    if resume_anchor is None:
                        last_updated_epoch_seen = updated_epoch
                else:
                    # Compatibility fallback for older WinRT projections. Do not
                    # reset the local clock on every read of an unchanged stale value.
                    changed = (
                        fallback_last_raw is None
                        or abs(raw_position - fallback_last_raw) >= 0.025
                        or playing != fallback_last_playing
                    )
                    if changed:
                        predicted = fallback_anchor_pos + max(0.0, emit_perf - fallback_anchor_perf) * rate
                        if fallback_last_raw is not None and playing and fallback_last_playing:
                            # Blend small normal drift; snap on a true seek.
                            if abs(raw_position - predicted) <= 0.45:
                                fallback_anchor_pos = predicted * 0.70 + raw_position * 0.30
                            else:
                                fallback_anchor_pos = raw_position
                        elif fallback_last_raw is not None and fallback_last_playing and not playing:
                            fallback_anchor_pos = predicted if abs(raw_position - predicted) <= 0.75 else raw_position
                        elif fallback_last_raw is not None and not fallback_last_playing and playing:
                            fallback_anchor_pos = fallback_anchor_pos if abs(raw_position - fallback_anchor_pos) <= 0.75 else raw_position
                        else:
                            fallback_anchor_pos = raw_position
                        fallback_anchor_perf = emit_perf
                        fallback_last_raw = raw_position
                        fallback_last_playing = playing
                    elapsed = max(0.0, emit_perf - fallback_anchor_perf)
                    position_now = fallback_anchor_pos + (elapsed * rate if playing else 0.0)
                    timeline_age_ms = elapsed * 1000.0
                    clock_quality = "LOCAL INTERPOLATION"

                last_position_now = position_now
                last_sample_perf = emit_perf
                last_playing_state = playing
                last_rate = rate

                self.position_sample.emit(
                    position_now,
                    playing,
                    rate,
                    emit_perf,
                    timeline_age_ms,
                    f"{clock_quality} · {status_name}",
                )

                if last_status != "SPOTIFY CONNECTED":
                    self.status_changed.emit("SPOTIFY CONNECTED")
                    last_status = "SPOTIFY CONNECTED"

            except Exception as exc:
                message = "SPOTIFY SESSION ERROR: " + str(exc)[:100]
                if message != last_status:
                    self.status_changed.emit(message)
                    last_status = message

            # Prefer Windows media-session events. The timeout is only a failsafe
            # for missed events/session changes; local time interpolation drives lyrics.
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=0.12)
            except asyncio.TimeoutError:
                pass

        remove_session_events()
        if manager_token is not None:
            try:
                manager.remove_sessions_changed(manager_token)
            except Exception:
                pass


class LyricsWorker(QThread):
    loaded = Signal(str, object, str)

    def __init__(self, track_key: str, title: str, artist: str, duration: float):
        super().__init__()
        self.track_key = track_key
        self.title = title
        self.artist = artist
        self.duration = duration

    def run(self):
        try:
            lines, sync_level, provider, source = load_or_fetch_lyrics(
                self.title, self.artist, self.duration
            )
            payload = {
                "lines": lines,
                "sync_level": sync_level,
                "provider": provider,
                "source": source,
            }
            message = "" if lines else source
            self.loaded.emit(self.track_key, payload, message)
        except Exception as exc:
            self.loaded.emit(self.track_key, {"lines": [], "sync_level": "none", "provider": "", "source": "ERROR"}, str(exc)[:140])


# ---------- Transparent lyrics window ----------


class LyricsOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = settings_load()
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.resize(self.settings["width"], self.settings["height"])
        self.move(self.settings["x"], self.settings["y"])

        self.track = None
        self.lines: list[dict] = []
        self.line_times: list[float] = []
        self.sync_level = "none"
        self.provider = ""
        self.source = ""
        self.lookup_message = ""
        self.media_status = "WAITING FOR SPOTIFY"
        self.position_anchor = 0.0
        self.position_anchor_perf = time.perf_counter()
        self.playing = False
        self.playback_rate = 1.0
        self.timeline_age_ms = 0.0
        self.clock_status = "NO CLOCK"
        self._drag_origin = None
        self._window_origin = None
        self._resize_origin = None
        self._resize_size = None
        self.edit_mode = False
        self._hotkey_down = False
        self._active_track_key = None
        self._workers: list[LyricsWorker] = []
        self._shutdown_started = False
        self._allow_final_close = False

        self.watcher = SpotifyWatcher()
        self.watcher.track_changed.connect(self.on_track_changed)
        self.watcher.position_sample.connect(self.on_position_sample)
        self.watcher.status_changed.connect(self.on_media_status)
        self.watcher.start()

        self.frame_timer = QTimer(self)
        self.frame_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.frame_timer.timeout.connect(self.update)
        self.frame_timer.start(6)  # local clock/render check; presentation follows the display refresh

        self.hotkey_timer = QTimer(self)
        self.hotkey_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.hotkey_timer.timeout.connect(self.poll_edit_hotkey)
        self.hotkey_timer.start(25)

        self.setMouseTracking(True)
        QTimer.singleShot(0, self._apply_mouse_mode)

    def _apply_mouse_mode(self):
        """Make the overlay click-through during normal use so games receive input."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            hwnd = ctypes.c_void_p(int(self.winId()))
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.argtypes = [ctypes.c_void_p, ctypes.c_int]
            get_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            set_long.restype = ctypes.c_ssize_t

            GWL_EXSTYLE = -20
            WS_EX_TRANSPARENT = 0x00000020
            WS_EX_NOACTIVATE = 0x08000000
            style = int(get_long(hwnd, GWL_EXSTYLE))
            if self.edit_mode:
                style &= ~WS_EX_TRANSPARENT
            else:
                style |= WS_EX_TRANSPARENT
            style |= WS_EX_NOACTIVATE
            set_long(hwnd, GWL_EXSTYLE, style)
            user32.SetWindowPos(
                hwnd, None, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0004 | 0x0020,  # no move/size/z-order + frame changed
            )
        except Exception:
            pass

    def set_edit_mode(self, enabled: bool):
        self.edit_mode = bool(enabled)
        self._apply_mouse_mode()
        self.update()

    def poll_edit_hotkey(self):
        """Ctrl+Alt+L toggles temporary mouse editing, including over games."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            pressed = all(
                bool(user32.GetAsyncKeyState(vk) & 0x8000)
                for vk in (0x11, 0x12, 0x4C)  # Ctrl, Alt, L
            )
        except Exception:
            return
        if pressed and not self._hotkey_down:
            self.set_edit_mode(not self.edit_mode)
        self._hotkey_down = pressed

    def current_position(self) -> float:
        now = time.perf_counter()
        elapsed = max(0.0, now - self.position_anchor_perf)
        position = self.position_anchor + (elapsed * self.playback_rate if self.playing else 0.0)
        return max(0.0, position + self.settings["offset_ms"] / 1000.0)

    def on_media_status(self, status: str):
        self.media_status = status
        self.update()

    def on_position_sample(self, position: float, playing: bool, rate: float,
                           sample_perf: float, age_ms: float, quality: str):
        # sample_perf and perf_counter use the same monotonic clock across threads.
        self.position_anchor = max(0.0, position)
        self.position_anchor_perf = sample_perf
        self.playing = bool(playing)
        self.playback_rate = rate if rate > 0 else 1.0
        self.timeline_age_ms = max(0.0, age_ms)
        self.clock_status = quality

    def on_track_changed(self, data: dict):
        if data.get("clear"):
            self._active_track_key = None
            self.track = None
            self.lines = []
            self.line_times = []
            self.sync_level = "none"
            self.provider = ""
            self.source = ""
            self.lookup_message = ""
            self.update()
            return
        key = str(data.get("key") or "")
        if not key or key == self._active_track_key:
            return
        self._active_track_key = key
        self.track = data
        self.lines = []
        self.line_times = []
        self.sync_level = "none"
        self.provider = ""
        self.source = ""
        self.lookup_message = "Loading synchronized lyrics…"
        self.update()

        worker = LyricsWorker(
            key,
            str(data.get("title") or ""),
            str(data.get("artist") or ""),
            safe_float(data.get("duration"), 0),
        )
        worker.loaded.connect(self.on_lyrics_loaded)
        worker.finished.connect(lambda w=worker: self._remove_worker(w))
        self._workers.append(worker)
        worker.start()

    def _remove_worker(self, worker: LyricsWorker):
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    def on_lyrics_loaded(self, key: str, payload: dict, message: str):
        # Old network requests may finish after Spotify has moved to another track.
        if key != self._active_track_key:
            return
        self.lines = payload.get("lines", []) or []
        self.line_times = [safe_float(line.get("start"), 0) for line in self.lines]
        self.sync_level = str(payload.get("sync_level", "none"))
        self.provider = str(payload.get("provider", ""))
        self.source = str(payload.get("source", ""))
        self.lookup_message = message
        self.update()

    def line_index(self, position: float) -> int:
        if not self.line_times:
            return -1
        return bisect.bisect_right(self.line_times, position) - 1

    def _font(self, size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
        font = QFont("Aptos Display")
        font.setPixelSize(size)
        font.setWeight(weight)
        return font

    def _draw_plain_glow(self, painter: QPainter, text: str, baseline: float,
                         font: QFont, color: QColor, width: float,
                         glow: bool = True):
        metrics = QFontMetricsF(font)
        while metrics.horizontalAdvance(text) > width and font.pixelSize() > 25:
            font.setPixelSize(font.pixelSize() - 1)
            metrics = QFontMetricsF(font)
        if metrics.horizontalAdvance(text) > width:
            text = self._ellipsize(text, font, width)
            metrics = QFontMetricsF(font)
        text_width = metrics.horizontalAdvance(text)
        x = (self.width() - text_width) / 2.0
        path = QPainterPath()
        path.addText(QPointF(x, baseline), font, text)
        if glow:
            painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 65), 8.0,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
            painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 35), 16.0,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(color))
        painter.drawPath(path)

    def _draw_word_line(self, painter: QPainter, line: dict, position: float,
                        baseline: float, font: QFont, max_width: float):
        words = line.get("words") or []
        if not words:
            color = QColor("#F1F3FA")
            self._draw_plain_glow(painter, str(line.get("text", "")), baseline, font, color, max_width, True)
            return

        metrics = QFontMetricsF(font)
        rows: list[list[dict]] = [[]]
        row_widths = [0.0]
        for word_index, word in enumerate(words):
            token = str(word.get("text", ""))
            if word_index < len(words) - 1:
                next_token = str(words[word_index + 1].get("text", ""))
                punctuation = ",.!?;:%)]}”’»"
                if (token and not token[-1].isspace()
                        and next_token and not next_token[0].isspace()
                        and next_token[0] not in punctuation):
                    token += " "
            token_width = metrics.horizontalAdvance(token)
            if rows[-1] and row_widths[-1] + token_width > max_width:
                rows.append([])
                row_widths.append(0.0)
            rows[-1].append((word, token))
            row_widths[-1] += token_width

        line_height = metrics.height() * 1.18
        total_height = line_height * len(rows)
        first_baseline = baseline - (total_height - line_height) / 2.0
        for row_index, row in enumerate(rows):
            x = (self.width() - row_widths[row_index]) / 2.0
            y = first_baseline + row_index * line_height
            for word, token in row:
                start = safe_float(word.get("start"), 0)
                end = safe_float(word.get("end"), start)
                if position < start:
                    color = QColor("#8993AA")
                elif position <= end:
                    color = QColor("#F7F8FF")
                else:
                    color = QColor("#D8DDF0")
                if start <= position <= end and token.strip():
                    path = QPainterPath()
                    path.addText(QPointF(x, y), font, token)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(QColor(165, 150, 255, 50), 7.0,
                                        Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
                    painter.drawPath(path)
                painter.setFont(font)
                painter.setPen(color)
                painter.drawText(QPointF(x, y), token)
                x += metrics.horizontalAdvance(token)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        w, h = self.width(), self.height()
        position = self.current_position()
        index = self.line_index(position)
        margin = max(34, int(w * 0.055))
        max_text_width = w - 2 * margin
        center_y = h * 0.49

        # Previous line — context only, deliberately no animated transition.
        if self.settings["show_previous"] and index > 0:
            previous = self.lines[index - 1]
            font = self._font(max(18, int(self.settings["font_size"] * 0.48)), QFont.Weight.Medium)
            metrics = QFontMetricsF(font)
            text = str(previous.get("text", ""))
            if metrics.horizontalAdvance(text) > max_text_width:
                text = self._ellipsize(text, font, max_text_width)
            self._draw_plain_glow(painter, text, center_y - 90, font, QColor("#69748C"), max_text_width, False)

        # Current line — word timing when the source actually supplies it.
        if index >= 0:
            current = self.lines[index]
            font = self._font(self.settings["font_size"], QFont.Weight.DemiBold)
            self._draw_word_line(painter, current, position, center_y + 12, font, max_text_width)
        elif self.lines:
            font = self._font(max(20, self.settings["font_size"] // 2), QFont.Weight.Medium)
            self._draw_plain_glow(painter, "…", center_y + 12, font, QColor("#AAB3C8"), max_text_width, False)
        elif self.track:
            font = self._font(18, QFont.Weight.Medium)
            message = "Loading synchronized lyrics…" if self.lookup_message.startswith("Loading") else "No synchronized lyrics found"
            self._draw_plain_glow(painter, message, center_y + 12, font, QColor("#9BA6BD"), max_text_width, False)
        else:
            font = self._font(18, QFont.Weight.Medium)
            self._draw_plain_glow(painter, "Waiting for Spotify", center_y + 12, font, QColor("#9BA6BD"), max_text_width, False)

        # Upcoming line.
        if self.settings["show_next"] and 0 <= index < len(self.lines) - 1:
            upcoming = self.lines[index + 1]
            font = self._font(max(18, int(self.settings["font_size"] * 0.48)), QFont.Weight.Medium)
            metrics = QFontMetricsF(font)
            text = str(upcoming.get("text", ""))
            if metrics.horizontalAdvance(text) > max_text_width:
                text = self._ellipsize(text, font, max_text_width)
            self._draw_plain_glow(painter, text, center_y + 91, font, QColor("#8792A8"), max_text_width, False)

        if self.edit_mode:
            edit_font = self._font(11, QFont.Weight.DemiBold)
            self._draw_plain_glow(
                painter, "EDIT MODE  ·  CTRL + ALT + L TO FINISH", 24,
                edit_font, QColor("#B9AEFF"), max_text_width, False
            )

        # Metadata is intentionally quiet. No panel, border, progress bar, or buttons.
        if self.track:
            title = str(self.track.get("title", ""))
            artist = str(self.track.get("artist", ""))
            attribution = (f"{self.sync_level.upper()} SYNC · {self.provider}"
                           if self.provider and self.sync_level != "none" else "")
            metadata = f"{artist or 'Unknown artist'}   /   {title}"
            if attribution:
                metadata += f"     ·     {attribution}"
            meta_font = self._font(13, QFont.Weight.Medium)
            metrics = QFontMetricsF(meta_font)
            if metrics.horizontalAdvance(metadata) > max_text_width:
                metadata = self._ellipsize(metadata, meta_font, max_text_width)
            self._draw_plain_glow(painter, metadata, h - 36, meta_font, QColor("#9CA6BA"), max_text_width, False)

        if self.settings["diagnostics"]:
            provider = self.provider or "NO SOURCE"
            sync = self.sync_level.upper()
            diag = (
                f"{self.media_status}  ·  {sync} / {provider}  ·  "
                f"POS {position:07.3f}s  ·  OFFSET {self.settings['offset_ms']:+d}ms  ·  "
                f"TIMELINE AGE {self.timeline_age_ms:.0f}ms  ·  {self.clock_status}"
            )
            diag_font = self._font(10, QFont.Weight.Normal)
            self._draw_plain_glow(painter, diag, h - 12, diag_font, QColor("#69748C"), max_text_width, False)

        painter.end()

    @staticmethod
    def _ellipsize(text: str, font: QFont, max_width: float) -> str:
        metrics = QFontMetricsF(font)
        if metrics.horizontalAdvance(text) <= max_width:
            return text
        suffix = "…"
        while text and metrics.horizontalAdvance(text + suffix) > max_width:
            text = text[:-1]
        return text + suffix

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background: #171A24; color: #E9ECF5; border: 1px solid #34394A; padding: 6px; }
            QMenu::item { padding: 7px 24px; }
            QMenu::item:selected { background: #343A50; }
            QMenu::separator { height: 1px; background: #34394A; margin: 5px 8px; }
        """)

        earlier = menu.addAction("Lyrics earlier  (+10 ms)")
        later = menu.addAction("Lyrics later  (−10 ms)")
        earlier_large = menu.addAction("Lyrics earlier  (+50 ms)")
        later_large = menu.addAction("Lyrics later  (−50 ms)")
        reset = menu.addAction("Reset synchronization")
        menu.addSeparator()

        previous_action = QAction("Show previous line", menu)
        previous_action.setCheckable(True)
        previous_action.setChecked(bool(self.settings["show_previous"]))
        menu.addAction(previous_action)
        next_action = QAction("Show next line", menu)
        next_action.setCheckable(True)
        next_action.setChecked(bool(self.settings["show_next"]))
        menu.addAction(next_action)
        diagnostics_action = QAction("Show timing diagnostics", menu)
        diagnostics_action.setCheckable(True)
        diagnostics_action.setChecked(bool(self.settings["diagnostics"]))
        menu.addAction(diagnostics_action)
        exit_edit_action = menu.addAction("Finish editing  (Ctrl+Alt+L)")
        menu.addSeparator()
        bigger = menu.addAction("Increase text size")
        smaller = menu.addAction("Decrease text size")
        menu.addSeparator()
        quit_action = menu.addAction("Quit SONA")

        chosen = menu.exec(event.globalPos())
        if chosen == earlier:
            self._change_offset(10)
        elif chosen == later:
            self._change_offset(-10)
        elif chosen == earlier_large:
            self._change_offset(50)
        elif chosen == later_large:
            self._change_offset(-50)
        elif chosen == reset:
            self.settings["offset_ms"] = 0
        elif chosen == previous_action:
            self.settings["show_previous"] = previous_action.isChecked()
        elif chosen == next_action:
            self.settings["show_next"] = next_action.isChecked()
        elif chosen == diagnostics_action:
            self.settings["diagnostics"] = diagnostics_action.isChecked()
        elif chosen == exit_edit_action:
            self.set_edit_mode(False)
        elif chosen == bigger:
            self.settings["font_size"] = min(88, self.settings["font_size"] + 3)
        elif chosen == smaller:
            self.settings["font_size"] = max(24, self.settings["font_size"] - 3)
        elif chosen == quit_action:
            self.close()
            return
        self._persist()
        self.update()

    def _change_offset(self, delta: int):
        self.settings["offset_ms"] = int(max(-2500, min(2500, self.settings["offset_ms"] + delta)))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.edit_mode:
            pos = event.position()
            if pos.x() >= self.width() - 28 and pos.y() >= self.height() - 28:
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_size = self.size()
            else:
                self._drag_origin = event.globalPosition().toPoint()
                self._window_origin = self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and self._window_origin is not None:
            delta = event.globalPosition().toPoint() - self._drag_origin
            self.move(self._window_origin + delta)
            event.accept()
            return
        if self._resize_origin is not None and self._resize_size is not None:
            delta = event.globalPosition().toPoint() - self._resize_origin
            self.resize(max(520, self._resize_size.width() + delta.x()),
                        max(200, self._resize_size.height() + delta.y()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        self._window_origin = None
        self._resize_origin = None
        self._resize_size = None
        self._persist()
        super().mouseReleaseEvent(event)

    def _persist(self):
        self.settings["x"] = self.x()
        self.settings["y"] = self.y()
        self.settings["width"] = self.width()
        self.settings["height"] = self.height()
        settings_save(self.settings)

    def closeEvent(self, event):
        if not self._shutdown_started:
            self._shutdown_started = True
            self.frame_timer.stop()
            self.hotkey_timer.stop()
            self._persist()
            try:
                self.watcher.stop()
            except Exception:
                pass

        if self._allow_final_close:
            event.accept()
            return

        running_workers = [worker for worker in self._workers if worker.isRunning()]
        if running_workers:
            # Keep the Qt event loop alive until urllib requests finish. Closing
            # a running QThread can crash the process during interpreter cleanup.
            self.hide()
            event.ignore()
            QTimer.singleShot(100, self._check_workers_before_exit)
            return

        event.accept()

    def _check_workers_before_exit(self):
        if any(worker.isRunning() for worker in self._workers):
            QTimer.singleShot(100, self._check_workers_before_exit)
            return
        self._allow_final_close = True
        self.close()


# ---------- App entry ----------


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("SONA")
    app.setQuitOnLastWindowClosed(True)

    window = LyricsOverlay()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
