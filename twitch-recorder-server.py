#!/usr/bin/env python3
"""Twitch Auto-Recorder local helper — streamlink on 127.0.0.1:8765 only."""

import http.server
import json
import re  # v6.57 (ad-break log lines); also re-imported below for v6.55
import sys
import os
import shutil
import subprocess
import tempfile
import threading
import socket  # v6.38: computer name for no-clobber Drive copies
import ntpath  # v6.38: Windows Drive for Desktop detection (unit-testable on any OS)
import time
import datetime  # v6.41: local timestamps in the Drive status file
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("TWITCH_RECORDER_PORT") or 8765)  # v6.35: env override for tests
HELPER_VERSION = "6.62"
REC_DIR = Path(
    os.environ.get("TWITCH_RECORDER_REC_DIR") or (Path.home() / "TwitchRecordings")
).expanduser()  # v6.35: env override for tests
HTML_NAME = "twitch-auto-recorder.html"
HERE = Path(__file__).resolve().parent

# GitHub auto-update (public repo). Install dir = directory of this running script
# (Application Support when installed via LaunchAgent / Desktop launcher).
UPDATE_REPO = "ewanders1-web/twitch-auto-recorder"
UPDATE_BRANCH = "main"
UPDATE_RAW_BASE = (
    f"https://raw.githubusercontent.com/{UPDATE_REPO}/{UPDATE_BRANCH}/"
)
UPDATE_FILES = (
    "twitch-auto-recorder.html",
    "twitch-recorder-server.py",
    "README-recorder.txt",
    "VERSION",
)
INSTALL_DIR = HERE

# Quick-fail window and backoff between early streamlink retries (went-live race).
QUICK_FAIL_SECS = 20
RETRY_BACKOFFS = (5, 15, 30)
# Mid-stream death: short backoff, then keep retrying with last value while wanted.
MID_RESUME_BACKOFFS = (3, 8, 15)
# After establish: no output growth for this long → kill + mid-resume (hung streamlink).
STALL_SECS = 90
# v6.57: Twitch ad breaks. streamlink 7+ always cuts Twitch ads out of the recording, so the
# file simply stops growing for the whole break (mid-rolls are often 90-180 s, pre-rolls on a
# fresh connection 15-90 s). That used to look like a hung streamlink: the 90 s stall check
# killed it mid-ad (new segment, a few seconds of the set lost at the cut) and the 20 s
# establish check killed every fresh start that sat in a pre-roll — and the retry got a new
# pre-roll. streamlink logs the pause ("Filtering out segments and pausing stream output",
# "Waiting for pre-roll ads to finish", "Detected advertisement break of N seconds"), so while
# that says an ad is running we wait for it (up to the ad length + slack, capped here) instead.
# 0 disables (old behavior). Env: TWITCH_RECORDER_AD_PAUSE_MAX_SECS.
try:
    AD_PAUSE_MAX_SECS = max(0, int(os.environ.get("TWITCH_RECORDER_AD_PAUSE_MAX_SECS", "480")))
except ValueError:
    AD_PAUSE_MAX_SECS = 480
AD_STATS = {"breaks": 0, "prerolls": 0, "stallsAvoided": 0, "lastAt": 0.0, "lastUser": "", "lastSecs": None}
# Mid-resume: consecutive establish failures before giving up (stream likely offline /
# page closed so Twitch poll cannot /api/stop). Auto-Rec can POST /api/record again if still live.
MAX_MID_QUICK_FAILS = 5
# v6.42: streamer ends and immediately restarts (new broadcast) → streamlink exits with
# "no playable streams" for a minute or more while Twitch spins the new one up. Instead of
# giving up after ~1 min, keep retrying with backoff for this long after a recording ends
# or a start quick-fails, while that streamer has Auto-Rec armed (or no page has reported in).
# 0 disables (old behavior). Env: TWITCH_RECORDER_RESTART_GRACE_SECS.
try:
    RESTART_GRACE_SECS = max(0.0, float(os.environ.get("TWITCH_RECORDER_RESTART_GRACE_SECS") or 480))
except ValueError:
    RESTART_GRACE_SECS = 480.0
RESTART_GRACE_BACKOFFS = (15, 20, 30)  # once normal retries are used up; caps at 30s
# v6.46: same window as status-file recorderPageOpen — after this, the page is gone
# and restart_grace_applies treats the helper as running alone.
PAGE_HEARTBEAT_STALE_SECS = float(os.environ.get("TWITCH_RECORDER_PAGE_STALE_SECS") or 120)
# v6.49: when the recorder page is closed/stale, the helper itself polls whether
# persisted Auto-Rec armed streamers are live and starts recording (same path as /api/record).
# Env: TWITCH_RECORDER_SOLO_WATCH_SECS (default 60; 0 disables). Startup delay gives the page
# a chance to reconnect after a reboot before the helper starts watching alone.
try:
    SOLO_WATCH_SECS = max(0.0, float(os.environ.get("TWITCH_RECORDER_SOLO_WATCH_SECS") or 60))
except ValueError:
    SOLO_WATCH_SECS = 60.0
try:
    SOLO_WATCH_STARTUP_DELAY_SECS = max(0.0, float(os.environ.get("TWITCH_RECORDER_SOLO_WATCH_STARTUP_SECS") or 75))
except ValueError:
    SOLO_WATCH_STARTUP_DELAY_SECS = 75.0
# Public Twitch web Client-ID (same family streamlink uses) — no user credentials needed.
TWITCH_GQL_URL = "https://gql.twitch.tv/gql"
TWITCH_GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
# v6.48: the recorder page ends the restart grace early (POST /api/grace/release) once Twitch
# Helix has said the streamer is offline for this long while the helper is waiting. Real restarts
# usually come back within a minute or two; a normal stream end then reaches voice removal /
# Drive ~2.5 min after the end instead of 8. The page enforces the timing (Helix is only visible
# there); the helper just publishes this value in /api/health.graceOfflineSecs. With the page
# closed/stale there is no Helix signal and the full RESTART_GRACE_SECS still applies.
# Env: TWITCH_RECORDER_GRACE_OFFLINE_SECS (default 150; 0 = page never releases early).
try:
    GRACE_OFFLINE_SECS = max(0.0, float(os.environ.get("TWITCH_RECORDER_GRACE_OFFLINE_SECS") or 150))
except ValueError:
    GRACE_OFFLINE_SECS = 150.0
# Soft give-up sooner when streamlink stderr clearly says offline / no streams.
OFFLINE_STDERR_HINTS = (
    "no playable streams",
    "no streams found",
    "unable to find",
    "is offline",
    "offline",
    "404 client error",
    "failed to open playlist",
)
FILES_CAP = 50
DOWNLOAD_TYPES = {
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".webm": "video/webm",
    ".flac": "audio/flac",
}
# Music-only upload caps
MUSIC_UPLOAD_MAX_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB
MUSIC_UPLOAD_EXTS = {
    ".mp4", ".mkv", ".mov", ".ts", ".mp3", ".m4a", ".wav", ".webm", ".ogg", ".flac", ".m4v", ".aac",
}
# Refuse new /api/record when free space on recordings volume is below this.
DISK_BLOCK_BYTES = 512 * 1024 * 1024  # 512 MiB
# Warn via /api/health (HTML one-shot notify) when free space drops below this.
DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
# Write-rate window for /api/status bps (bytes/sec).
BPS_WINDOW_SECS = 1.75
# Optional ffmpeg peak sample cadence (non-blocking background threads).
PEAK_SAMPLE_SECS = 1.8
# Tiny leftover after a failed establish (went-live race) — safe to delete.
SCRUB_MAX_BYTES = 8192

LOCK = threading.Lock()
# username -> {proc, file, startedAt, quality, pid, starting, retrying, want, attempt, gen, supervised, resumes,
#              bps, peak, level, size_samples, peak_at, peak_busy}
ACTIVE = {}
# username -> last error string (survives mid-stream death until next ok start)
LAST_ERROR = {}
# username -> generation; bumped on stop so a dying supervisor cannot reclaim
GEN = {}

# Music-only (demucs) async jobs: jobId -> {status, progress, progressPct, error, name, outputName, outputUrl, vocalsName, vocalsUrl, startedAt, finishedAt, redo}
MUSIC_JOBS = {}
MUSIC_JOBS_LOCK = threading.Lock()
# Keep done/error snapshots so a refreshed page can still see result once.
MUSIC_JOB_KEEP_FINISHED_SECS = 15 * 60
# v6.28: demucs is CPU/RAM heavy — run one at a time; extras stay queued (Auto Music mid-stream safe).
DEMUCS_MAX_CONCURRENT = 1
DEMUCS_SLOTS = threading.Semaphore(DEMUCS_MAX_CONCURRENT)
# v6.29: last orphan music-only-* / seamless-*.txt cleanup summary (for /api/health flash)
ORPHAN_TEMPS_LAST = {
    "dirs": 0,
    "files": 0,
    "bytes": 0,
    "at": None,
    "reason": None,
}
_ORPHAN_DISKWARN_DONE = False

# v6.32: schedule at most one idle auto-restart per process after apply_update
_auto_restart_scheduled = False

# Seamless join (ffmpeg concat) async jobs — same shape as music for UI reuse.
SEAMLESS_JOBS = {}
SEAMLESS_JOBS_LOCK = threading.Lock()
SEAMLESS_JOB_KEEP_FINISHED_SECS = 15 * 60
# username -> {segments: [basenames], at: epoch} after stop / give-up / supervisor exit
RECENT_SESSIONS = {}
RECENT_SESSIONS_KEEP_SECS = 6 * 60 * 60


def which_streamlink_system():
    return shutil.which("streamlink") or shutil.which("streamlink.exe")


def which_streamlink():
    """v6.59: the helper's own verified streamlink (newer Python) wins over the system copy."""
    try:
        priv = sl_private_bin()
    except Exception:
        priv = None
    return priv or which_streamlink_system()


def which_ffmpeg():
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


def which_demucs():
    """Return demucs CLI path, or 'python -m demucs' if importable, else None."""
    path = shutil.which("demucs") or shutil.which("demucs.exe")
    if path:
        return path
    try:
        import demucs  # noqa: F401
        return "python -m demucs"
    except ImportError:
        return None


def demucs_available():
    return bool(which_demucs())


def seamless_available():
    return bool(which_ffmpeg())


def is_seamless_export_name(name):
    """True if basename looks like a seamless join sibling (not a native segment)."""
    stem = Path(name or "").stem
    import re as _re
    return bool(_re.search(r"-seamless(?:-\d+)?$", stem))


def basename_of(path):
    if not path:
        return ""
    return Path(path).name


def remember_session_segments(username, segments, reason="stream-end"):
    """Stash finished segment basenames after stop/give-up for POST /api/seamless {username}.

    Safe to call while holding LOCK (does not re-enter LOCK).
    """
    segs = [s for s in (segments or []) if s and isinstance(s, str)]
    try:  # v6.53: session ended normally in this process — nothing to recover after a restart
        live_session_forget(username, segs)
    except Exception as e:
        log(f"live-session forget failed for {username}: {e}")
    if not segs:
        return
    RECENT_SESSIONS[username] = {"segments": list(segs), "at": time.time()}
    # v6.36: recording finalized → remove voice → split → send to Drive (if enabled)
    try:
        _on_session_finalized(username, segs, reason=reason)
    except Exception as e:
        log(f"pipeline trigger failed for {username}: {e}")


def prune_recent_sessions():
    now = time.time()
    dead = [
        u
        for u, info in list(RECENT_SESSIONS.items())
        if (now - float(info.get("at") or 0)) > RECENT_SESSIONS_KEEP_SECS
    ]
    for u in dead:
        RECENT_SESSIONS.pop(u, None)


def collect_session_segments_from_rec(rec):
    """Finished segments for a session slot (exclude tiny leftovers / empty)."""
    segs = []
    seen = set()
    for name in list(rec.get("sessionSegments") or []):
        if not name or name in seen:
            continue
        p = REC_DIR / name
        try:
            if p.is_file() and p.stat().st_size > SCRUB_MAX_BYTES:
                segs.append(name)
                seen.add(name)
        except OSError:
            continue
    cur = rec.get("file") or ""
    if cur:
        name = basename_of(cur)
        if name and name not in seen:
            try:
                if Path(cur).is_file() and file_size(cur) > SCRUB_MAX_BYTES:
                    segs.append(name)
                    seen.add(name)
            except OSError:
                pass
    return segs


def append_finished_segment_locked(rec, path):
    """Record a finished mid-resume segment basename on the ACTIVE slot. Caller holds LOCK."""
    if not rec or not path:
        return
    name = basename_of(path)
    if not name or is_music_export_name(name) or is_seamless_export_name(name):
        return
    try:
        if file_size(path) <= SCRUB_MAX_BYTES:
            return
    except OSError:
        return
    segs = rec.setdefault("sessionSegments", [])
    if name not in segs:
        segs.append(name)


# v6.51: a log line must never break the work it describes. On Windows, when the helper's output
# goes to a file or pipe (Start-Process / Task Scheduler / a launcher capturing it, or the auto-update
# self-test), Python writes it in the old Windows code page (cp1252) and raises on characters that
# page doesn't have (e.g. "→"). The success lines for Drive sends contain "→", so every part set and
# original that reached Drive was then marked "error" ('charmap' codec can't encode …), local parts were
# never cleared and the one-copy note said "error". A closed console / dead pipe raises OSError the same
# way. log() now falls back to an escaped copy of the line, or drops it, and never raises.
LOG_WRITE_ERRORS = [0]  # v6.51: lines that could not be written as-is (shown in /api/health.logWriteErrors)


def _harden_stdio():
    """v6.51: unknown characters become \\uXXXX instead of raising (stdout and stderr). Never raises."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="backslashreplace")
        except Exception:
            pass


def log(msg):
    line = f"[recorder] {msg}"
    try:
        print(line, flush=True)
        return
    except UnicodeEncodeError:
        pass
    except Exception:  # stdout closed / broken pipe / no console — never stop the caller
        LOG_WRITE_ERRORS[0] += 1
        return
    LOG_WRITE_ERRORS[0] += 1
    try:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "backslashreplace").decode(enc, "replace"), flush=True)
    except Exception:
        pass


def safe_username(raw):
    name = (raw or "").strip().lower()
    if not name or len(name) > 40:
        return None
    if not all(c.isalnum() or c == "_" for c in name):
        return None
    return name


def stamp_iso():
    # Include weekday so exports are easy to skim (Tue, Fri, Sun, …) in local time.
    return time.strftime("%a-%Y-%m-%dT%H-%M-%S")


def unique_recording_path(username, ext):
    """Pick username-Weekday-stamp.ext that does not already exist (avoid --force clobber)."""
    REC_DIR.mkdir(parents=True, exist_ok=True)
    stamp = stamp_iso()
    candidate = REC_DIR / f"{username}-{stamp}.{ext}"
    if not candidate.exists():
        return candidate
    n = 2
    while n < 1000:
        candidate = REC_DIR / f"{username}-{stamp}-{n}.{ext}"
        if not candidate.exists():
            return candidate
        n += 1
    # Extremely unlikely: fall back to millisecond suffix
    return REC_DIR / f"{username}-{stamp}-{int(time.time() * 1000) % 100000}.{ext}"


def scrub_failed_output(path):
    """Delete tiny leftover files from failed streamlink establish attempts."""
    if not path:
        return
    try:
        pth = Path(path)
        if not pth.is_file():
            return
        sz = pth.stat().st_size
        if sz > SCRUB_MAX_BYTES:
            return
        abspath = os.path.abspath(str(pth))
        with LOCK:
            for rec in ACTIVE.values():
                cur = rec.get("file") or ""
                if cur and os.path.abspath(cur) == abspath:
                    return
        pth.unlink()
        log(f"scrubbed failed output: {pth.name} ({sz}B)")
    except OSError:
        pass


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def db_to_unit(db):
    """Map Peak level dB to 0-1 amplitude (cap at 1.0)."""
    try:
        db = float(db)
    except (TypeError, ValueError):
        return None
    if db > 0:
        db = 0.0
    amp = 10.0 ** (db / 20.0)
    if amp < 0:
        amp = 0.0
    if amp > 1.0:
        amp = 1.0
    return amp


def parse_ffmpeg_peak_db(stderr_text):
    """Pull Peak level dB from ffmpeg astats stderr."""
    import re as _re
    if not stderr_text:
        return None
    m = _re.search(
        r"Overall[\s\S]*?Peak level dB:\s*([-+]?[0-9]*\.?[0-9]+)",
        stderr_text,
        _re.I,
    )
    if not m:
        m = _re.search(
            r"Peak level dB:\s*([-+]?[0-9]*\.?[0-9]+)",
            stderr_text,
            _re.I,
        )
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def sample_file_peak(path):
    """ffmpeg astats sample of recent audio in path. Returns 0-1 or None."""
    ff = which_ffmpeg()
    if not ff or not path:
        return None
    try:
        if not os.path.isfile(path) or file_size(path) < 4096:
            return None
    except OSError:
        return None
    cmd = [
        ff,
        "-hide_banner",
        "-nostats",
        "-sseof",
        "-2",
        "-i",
        path,
        "-t",
        "1",
        "-vn",
        "-af",
        "astats=metadata=1:reset=1",
        "-f",
        "null",
        "-",
    ]
    env = os.environ.copy()
    env["PYTHONWARNINGS"] = "ignore"
    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=10,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    err = (r.stderr or b"").decode("utf-8", errors="replace")
    db = parse_ffmpeg_peak_db(err)
    return db_to_unit(db)


def touch_bps_locked(rec):
    """Update rec['bps'] from a short size-delta window. Caller holds LOCK."""
    path = rec.get("file") or ""
    sz = file_size(path) if path else 0
    now = time.time()
    samples = rec.setdefault("size_samples", [])
    prev_path = rec.get("_bps_path")
    if prev_path != path:
        samples.clear()
        rec["_bps_path"] = path
        rec["bps"] = 0.0
    samples.append((now, sz))
    cutoff = now - BPS_WINDOW_SECS
    samples[:] = [s for s in samples if s[0] >= cutoff - 0.25]
    if len(samples) >= 2:
        t0, s0 = samples[0]
        t1, s1 = samples[-1]
        dt = t1 - t0
        if dt >= 0.35:
            rec["bps"] = max(0.0, (s1 - s0) / dt)
        else:
            rec["bps"] = float(rec.get("bps") or 0.0)
    else:
        rec["bps"] = float(rec.get("bps") or 0.0)
    return sz


def schedule_peak_sample(username, path):
    """Spawn a daemon thread to sample peak; never blocks the HTTP thread."""
    if not which_ffmpeg() or not path:
        return

    def worker():
        peak = sample_file_peak(path)
        with LOCK:
            rec = ACTIVE.get(username)
            if not rec:
                return
            if (rec.get("file") or "") != path:
                rec["peak_busy"] = False
                return
            if peak is not None:
                rec["peak"] = peak
                rec["level"] = peak
            rec["peak_at"] = time.time()
            rec["peak_busy"] = False

    with LOCK:
        rec = ACTIVE.get(username)
        if not rec:
            return
        if rec.get("peak_busy"):
            return
        last = float(rec.get("peak_at") or 0.0)
        if time.time() - last < PEAK_SAMPLE_SECS:
            return
        rec["peak_busy"] = True
    t = threading.Thread(target=worker, daemon=True, name=f"peak-{username}")
    t.start()


def meter_loop():
    """Background: refresh bps windows and kick optional ffmpeg peak samples."""
    while True:
        time.sleep(0.5)
        to_sample = []
        with LOCK:
            for user, rec in list(ACTIVE.items()):
                touch_bps_locked(rec)
                path = rec.get("file") or ""
                if path and not rec.get("starting") and not rec.get("retrying"):
                    to_sample.append((user, path))
        for user, path in to_sample:
            schedule_peak_sample(user, path)


def disk_usage_for_rec_dir():
    """Return (free, total) bytes for the recordings volume, or (None, None)."""
    try:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(str(REC_DIR))
        return int(usage.free), int(usage.total)
    except OSError:
        return None, None


def disk_status():
    free, total = disk_usage_for_rec_dir()
    if free is None:
        return {
            "diskFree": None,
            "diskTotal": None,
            "diskWarn": False,
            "diskBlock": False,
        }
    return {
        "diskFree": free,
        "diskTotal": total,
        "diskWarn": free < DISK_WARN_BYTES,
        "diskBlock": free < DISK_BLOCK_BYTES,
    }


def disk_block_error():
    free, _total = disk_usage_for_rec_dir()
    if free is None:
        return None
    if free >= DISK_BLOCK_BYTES:
        return None
    mb = free / (1024 * 1024)
    return (
        f"disk almost full ({mb:.0f} MB free under {REC_DIR}) — "
        f"free at least {DISK_BLOCK_BYTES // (1024 * 1024)} MB before recording"
    )


def _dir_byte_size(root):
    """Best-effort recursive size; ignores unreadable entries."""
    total = 0
    try:
        for p in Path(root).rglob("*"):
            try:
                if p.is_file():
                    total += int(p.stat().st_size)
            except OSError:
                continue
    except OSError:
        pass
    return total


def cleanup_orphan_temps(reason="startup"):
    """Delete leftover music-only-* work dirs and stale seamless-*.txt under REC_DIR.

    Never deletes user recordings or -music / -vocals / -seamless exports.
    Safe if REC_DIR is missing or empty.
    """
    global ORPHAN_TEMPS_LAST
    dirs_cleared = 0
    files_cleared = 0
    bytes_freed = 0
    try:
        if not REC_DIR.is_dir():
            ORPHAN_TEMPS_LAST = {
                "dirs": 0,
                "files": 0,
                "bytes": 0,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "reason": reason,
            }
            return ORPHAN_TEMPS_LAST
        now = time.time()
        for p in list(REC_DIR.iterdir()):
            try:
                name = p.name
                # tempfile.mkdtemp(prefix="music-only-") work dirs only — never files/exports
                if p.is_dir() and name.startswith("music-only-"):
                    # v6.35: never remove the work dir of a live job (diskWarn runs mid-job)
                    if _is_active_work_path(p):
                        continue
                    size = _dir_byte_size(p)
                    shutil.rmtree(p, ignore_errors=True)
                    if not p.exists():
                        dirs_cleared += 1
                        bytes_freed += size
                    continue
                # leftover ffmpeg concat list files (prefix seamless-, suffix .txt)
                if (
                    p.is_file()
                    and name.startswith("seamless-")
                    and name.endswith(".txt")
                ):
                    try:
                        st = p.stat()
                        age = now - float(st.st_mtime)
                    except OSError:
                        continue
                    if age < 3600:
                        continue
                    size = int(st.st_size)
                    try:
                        p.unlink()
                    except OSError:
                        continue
                    files_cleared += 1
                    bytes_freed += size
            except OSError:
                continue
    except OSError as e:
        log(f"orphan cleanup ({reason}) skipped: {e}")
    try:
        d_removed, d_freed = cleanup_drive_temps(reason)
        files_cleared += d_removed
        bytes_freed += d_freed
    except Exception as e:
        log(f"drive temp cleanup ({reason}) skipped: {e}")
    ORPHAN_TEMPS_LAST = {
        "dirs": dirs_cleared,
        "files": files_cleared,
        "bytes": bytes_freed,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reason": reason,
    }
    if dirs_cleared or files_cleared:
        log(
            f"orphan cleanup ({reason}): {dirs_cleared} dirs, "
            f"{files_cleared} files, {bytes_freed} bytes freed"
        )
    else:
        log(f"orphan cleanup ({reason}): nothing to clear")
    return ORPHAN_TEMPS_LAST


def maybe_cleanup_orphan_temps_on_diskwarn(disk):
    """One cleanup per diskWarn episode (in addition to startup).

    v6.30: also cancel waiting (queued) demucs jobs once per episode so a long
    Auto Music backlog does not start as soon as one byte frees while still under
    warn. Running demucs is never killed.
    """
    global _ORPHAN_DISKWARN_DONE
    if not disk or not disk.get("diskWarn"):
        _ORPHAN_DISKWARN_DONE = False
        return None
    if _ORPHAN_DISKWARN_DONE:
        return None
    _ORPHAN_DISKWARN_DONE = True
    try:
        payload, _status = cancel_music_only({"waiting": True, "keepPipeline": True})
        cancelled = (payload or {}).get("cancelled") or []
        if cancelled:
            log(
                f"diskWarn: cancelled {len(cancelled)} waiting Music only job(s) "
                f"(running demucs kept)"
            )
    except Exception as e:
        log(f"diskWarn: cancel waiting Music failed: {e}")
    return cleanup_orphan_temps(reason="diskWarn")


def set_last_error(username, msg):
    if msg:
        LAST_ERROR[username] = str(msg)
    else:
        LAST_ERROR.pop(username, None)


def clear_errors(username=None):
    """Clear LAST_ERROR for one username, or all if username is None/empty."""
    if username:
        LAST_ERROR.pop(username, None)
        return {"ok": True, "cleared": username}
    LAST_ERROR.clear()
    return {"ok": True, "cleared": "all"}


def safe_rec_basename(raw_name):
    """Strict basename under REC_DIR only. No path traversal. None if unsafe.
    Does not require the file to exist (unlike resolve_download_path).
    """
    if not raw_name or not isinstance(raw_name, str):
        return None
    name = raw_name.strip()
    if not name or name in (".", "..") or chr(0) in name:
        return None
    if "/" in name or "\\" in name:
        return None
    if os.path.basename(name) != name:
        return None
    return name


def resolve_download_path(raw_name):
    """Strict basename under REC_DIR only. No path traversal. None if missing/unsafe."""
    name = safe_rec_basename(raw_name)
    if not name:
        return None
    try:
        rec_root = REC_DIR.resolve()
        target = (REC_DIR / name).resolve()
    except OSError:
        return None
    try:
        target.relative_to(rec_root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return target


def _sibling_audio_exts():
    """Known audio/video extensions for sibling discovery (DOWNLOAD_TYPES ∪ MUSIC_UPLOAD_EXTS)."""
    return set(DOWNLOAD_TYPES.keys()) | set(MUSIC_UPLOAD_EXTS)


def collect_native_sibling_paths(native_name):
    """Existing -music / -vocals / -seamless siblings for a native basename under REC_DIR.

    Matches numbered variants (-music-2, -seamless-3, …). Never returns the native itself.
    """
    name = safe_rec_basename(native_name)
    if not name:
        return []
    stem = Path(name).stem
    import re as _re
    pat = _re.compile(
        r"^" + _re.escape(stem) + r"-(music|vocals|seamless)(?:-\d+)?$",
    )
    exts = {e.lower() for e in _sibling_audio_exts()}
    found = []
    try:
        if not REC_DIR.is_dir():
            return []
        for p in REC_DIR.iterdir():
            try:
                if not p.is_file():
                    continue
            except OSError:
                continue
            if p.suffix.lower() not in exts:
                continue
            if pat.match(p.stem):
                found.append(p)
    except OSError as e:
        log(f"delete sibling scan failed: {e}")
    return found


def _delete_busy_for_candidates(candidates):
    """Return (busy_users, busy_music_names) for ACTIVE / in-flight Music inputs.

    candidates: list of Path under REC_DIR. Never mutates ACTIVE or MUSIC_JOBS.
    """
    cand_names = set()
    abs_cands = {}
    for p in candidates:
        try:
            cand_names.add(p.name)
            abs_cands[os.path.abspath(str(p))] = p.name
        except OSError:
            continue
    busy_users = []
    with LOCK:
        reap_locked()
        for user, rec in ACTIVE.items():
            f = rec.get("file") or ""
            if not f:
                continue
            try:
                af = os.path.abspath(f)
            except OSError:
                continue
            if af in abs_cands:
                busy_users.append(user)
    busy_users = sorted(set(busy_users))
    busy_music = []
    with MUSIC_JOBS_LOCK:
        for _jid, job in MUSIC_JOBS.items():
            if (job.get("status") or "") not in ("queued", "running"):
                continue
            jname = job.get("name") or ""
            if jname and jname in cand_names:
                busy_music.append(jname)
    busy_music = sorted(set(busy_music))
    return busy_users, busy_music


def _delete_one_primary(bn, with_siblings=True, already_deleted=None):
    """Delete one path-safe primary (+ optional siblings). Independent of other names.

    Returns dict:
      ok True  -> deleted:[...], bytesFreed:int
      ok False -> error:str, status:int (409/404), optional active/musicJobs
    already_deleted: set of basenames removed earlier in this multi request (mutated).

    v6.37: withSiblings on a native also removes Drive parts/<native_stem>/;
    deleting a -music primary removes Drive parts/<music_base_stem>/. Refuses 409
    while a Drive copy is mid-send for that exact base.
    """
    if already_deleted is None:
        already_deleted = set()
    # Already removed as a sibling of an earlier primary in this request
    if bn in already_deleted:
        return {"ok": True, "deleted": [], "bytesFreed": 0, "skippedAlreadyDeleted": True}

    candidates = []
    cand_names = set()

    def _add(path):
        if path is None:
            return
        try:
            key = path.name
            if key in cand_names or key in already_deleted:
                return
            if not path.is_file():
                return
            cand_names.add(key)
            candidates.append(path)
        except OSError:
            return

    primary = resolve_download_path(bn)
    if primary:
        _add(primary)
    if with_siblings and not is_music_export_name(bn) and not is_seamless_export_name(bn):
        for sib in collect_native_sibling_paths(bn):
            _add(sib)

    if not candidates:
        return {"ok": False, "error": "not found", "status": 404}

    busy_users, busy_music = _delete_busy_for_candidates(candidates)
    if busy_users:
        who = ", ".join(busy_users)
        return {
            "ok": False,
            "status": 409,
            "error": (
                f"still recording — cannot delete while active for {who}; "
                f"stop first or wait for the segment to finish"
            ),
            "active": busy_users,
        }
    if busy_music:
        return {
            "ok": False,
            "status": 409,
            "error": (
                "Music only / Demucs in progress for "
                + ", ".join(busy_music)
                + " — cancel waiting or wait for it to finish before deleting"
            ),
            "musicJobs": busy_music,
        }

    # v6.37: which Drive parts/<base>/ folder (if any) should go with this delete
    drive_base = None
    if is_music_export_name(bn):
        drive_base = music_base_stem(bn)
    elif with_siblings and not is_seamless_export_name(bn):
        drive_base = Path(bn).stem
    if drive_base and DELIVERY_STATE.get("busy") and DELIVERY_STATE.get("base") == drive_base:
        return {
            "ok": False,
            "status": 409,
            "error": (
                "Drive send in progress for this recording — "
                "wait for it to finish before deleting"
            ),
        }

    deleted = []
    bytes_freed = 0
    errors = []
    for p in candidates:
        try:
            sz = 0
            try:
                sz = int(p.stat().st_size)
            except OSError:
                pass
            p.unlink()
            deleted.append(p.name)
            already_deleted.add(p.name)
            bytes_freed += max(0, sz)
            log(f"delete removed {p.name} ({sz} bytes)")
        except OSError as e:
            errors.append(f"{p.name}: {e}")
            log(f"delete failed {p.name}: {e}")

    if not deleted:
        err = "; ".join(errors) if errors else "not found"
        return {"ok": False, "error": err, "status": 404}

    # v6.37: remove matching Drive parts folder; count bytes; drop waiting deliveries
    if drive_base:
        try:
            parts_freed = remove_drive_parts_for_delete(drive_base)
            bytes_freed += max(0, int(parts_freed or 0))
            if parts_freed:
                deleted.append(f"Drive parts/{drive_base}/")
        except Exception as e:
            errors.append(f"Drive parts/{drive_base}: {e}")
            log(f"delete Drive parts failed base={drive_base}: {e}")

    # v6.38: a deleted original that never reached Drive can't be sent any more — drop it
    for nm in list(deleted):
        try:
            forget_original_if_unsent(os.path.basename(str(nm)))
        except Exception:
            pass
    out = {"ok": True, "deleted": deleted, "bytesFreed": bytes_freed}
    if errors:
        out["errors"] = errors
    return out


def delete_recording_files(names, with_siblings=True):
    """Delete finished recording file(s) under REC_DIR.

    Path-safe (basenames only). Never deletes ACTIVE/growing files or an in-flight
    demucs/music-only input. When with_siblings and the request is a native, also
    removes -music / -vocals / -seamless siblings and Drive parts/<stem>/. When the
    request is a -music export, also removes Drive parts/<music_base_stem>/. Other
    sibling exports delete only that file. Refuses 409 while Drive copy is mid-send
    for that base.

    v6.34: multi-name requests process each primary independently (one ACTIVE /
    Music conflict does not block deleting other finished names). Response always
    includes deleted[] and failed:[{name,error}]; bytesFreed / freedBytes sum.

    Returns (payload, http_status).
    """
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)):
        return {"ok": False, "error": "name or names required"}, 400

    # Unique safe basenames, preserve order
    seen = set()
    primaries = []
    unsafe = []
    for raw in names:
        bn = safe_rec_basename(raw)
        if not bn:
            if raw is not None and str(raw).strip():
                unsafe.append({"name": str(raw), "error": "unsafe name"})
            continue
        if bn in seen:
            continue
        seen.add(bn)
        primaries.append(bn)
    if not primaries and unsafe:
        return {
            "ok": False,
            "error": "name or names required",
            "deleted": [],
            "failed": unsafe,
            "bytesFreed": 0,
            "freedBytes": 0,
        }, 400
    if not primaries:
        return {"ok": False, "error": "name or names required"}, 400

    already_deleted = set()
    deleted = []
    failed = list(unsafe)
    bytes_freed = 0
    single = len(primaries) == 1 and not unsafe

    for bn in primaries:
        one = _delete_one_primary(bn, with_siblings=with_siblings, already_deleted=already_deleted)
        if one.get("ok"):
            for n in one.get("deleted") or []:
                if n not in deleted:
                    deleted.append(n)
            bytes_freed += int(one.get("bytesFreed") or 0)
        else:
            failed.append({
                "name": bn,
                "error": one.get("error") or "delete failed",
            })
            # Preserve single-name 409/404 shape for the existing History Delete button
            if single:
                status = int(one.get("status") or 404)
                payload = {"ok": False, "error": one.get("error") or "delete failed", "deleted": [], "failed": failed}
                if one.get("active") is not None:
                    payload["active"] = one["active"]
                if one.get("musicJobs") is not None:
                    payload["musicJobs"] = one["musicJobs"]
                payload["bytesFreed"] = 0
                payload["freedBytes"] = 0
                return payload, status

    if not deleted and failed:
        # Multi: none deleted — prefer 409 if any busy, else 404
        statuses = []
        for f in failed:
            err = (f.get("error") or "").lower()
            if "still recording" in err or "music only" in err or "demucs" in err:
                statuses.append(409)
            elif "unsafe" in err:
                statuses.append(400)
            else:
                statuses.append(404)
        status = 409 if 409 in statuses else (400 if all(s == 400 for s in statuses) else 404)
        err = failed[0]["error"] if len(failed) == 1 else (
            f"{len(failed)} failed — " + "; ".join(
                f"{x['name']}: {x['error']}" for x in failed[:5]
            )
        )
        return {
            "ok": False,
            "error": err,
            "deleted": [],
            "failed": failed,
            "bytesFreed": 0,
            "freedBytes": 0,
            "dir": str(REC_DIR),
        }, status

    payload = {
        "ok": True,
        "deleted": deleted,
        "failed": failed,
        "bytesFreed": bytes_freed,
        "freedBytes": bytes_freed,
        "dir": str(REC_DIR),
    }
    if failed:
        # Partial success — still ok:true so UI can drop deleted rows; surface failed
        payload["error"] = (
            f"Deleted {len(deleted)} file(s); {len(failed)} failed — "
            + "; ".join(f"{x['name']}: {x['error']}" for x in failed[:5])
        )
    return payload, 200


def api_delete_recordings(body):
    """POST /api/delete — {name} or {names}, optional withSiblings (default true).

    v6.34: multi-name deletes each primary independently (partial ok + failed[]).
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "invalid body"}, 400
    names = body.get("names")
    if names is None and body.get("name") is not None:
        names = [body.get("name")]
    if names is None:
        return {"ok": False, "error": "name or names required"}, 400
    with_siblings = body.get("withSiblings")
    if with_siblings is None:
        with_siblings = True
    else:
        with_siblings = bool(with_siblings)
    return delete_recording_files(names, with_siblings=with_siblings)


def reap_locked():
    """Drop finished streamlink processes. Caller must hold LOCK."""
    dead = []
    for user, rec in ACTIVE.items():
        # Supervisor owns lifecycle after establish (and early-fail retries).
        if rec.get("supervised"):
            continue
        # spawn / backoff wait in progress — keep slot so we never double-start
        if rec.get("starting") or rec.get("retrying"):
            if rec.get("proc") is None:
                continue
        proc = rec.get("proc")
        if proc is None:
            if not rec.get("starting") and not rec.get("retrying"):
                dead.append(user)
            continue
        code = proc.poll()
        if code is not None:
            # Supervisor owns early-fail retries; only reap unsupervised sessions.
            if rec.get("starting") or rec.get("retrying"):
                continue
            dead.append(user)
            err = f"streamlink exited mid-stream (code {code})"
            set_last_error(user, err)
            log(f"stream ended: {user} (exit {code}) file={rec.get('file')} — {err}")
    for user in dead:
        ACTIVE.pop(user, None)


def terminate_proc(proc):
    if proc is None:
        return
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        except Exception:
            pass
    close_stderr_file(proc)


def stop_one_locked(username):
    rec = ACTIVE.get(username)
    if not rec:
        return {"ok": False, "error": f"not recording {username}"}
    rec["want"] = False
    GEN[username] = max(GEN.get(username, 0), rec.get("gen", 0)) + 1
    terminate_proc(rec.get("proc"))
    path = rec.get("file") or ""
    size = file_size(path) if path else 0
    segments = collect_session_segments_from_rec(rec)
    ACTIVE.pop(username, None)
    remember_session_segments(username, segments)
    log(f"stopped {username} file={path} size={size} segments={len(segments)}")
    return {
        "ok": True,
        "file": path,
        "size": size,
        "username": username,
        "segments": segments,
    }


def stop_all_locked():
    results = []
    for user in list(ACTIVE.keys()):
        results.append(stop_one_locked(user))
    return results


def build_cmd(username, quality):
    """Always write OGG audio for helper recordings (no mp4). Uses streamlink audio_only.

    With ffmpeg: --ffmpeg-fout ogg → unique_recording_path(..., "ogg").
    Without ffmpeg: ogg remux is impossible, so fall back to .ts only.
    UI may pass quality best|audio_only; both map to audio_only for reliable OGG.
    """
    wait_streamlink_upgrade()  # v6.55: don't launch a half-replaced streamlink
    sl = which_streamlink()
    ff = which_ffmpeg()
    # Supported OGG path is audio_only (video→ogg is unreliable / not wanted).
    sl_quality = "audio_only"
    if ff:
        out = unique_recording_path(username, "ogg")
        cmd = [
            sl,
            "--force",
            "--loglevel",
            "info",  # v6.57: ad-break lines ("pausing stream output") must reach stderr
            "--output",
            str(out),
            "--ffmpeg-fout",
            "ogg",
            f"twitch.tv/{username}",
            sl_quality,
        ]
    else:
        # streamlink cannot remux to ogg without ffmpeg — keep .ts fallback
        out = unique_recording_path(username, "ts")
        cmd = [sl, "--force", "--loglevel", "info", "--output", str(out), f"twitch.tv/{username}", sl_quality]
    return cmd, out


def spawn_streamlink(cmd):
    # Capture stderr to a temp file (avoid PIPE deadlock). Attached on the Popen for later.
    err_file = tempfile.TemporaryFile()
    env = os.environ.copy()
    # System Python on macOS is LibreSSL; urllib3 v2 prints a NotOpenSSLWarning that
    # is not the real streamlink failure. Keep it out of lastError.
    env["PYTHONWARNINGS"] = "ignore"
    popen_kw = {
        "stdout": subprocess.DEVNULL,
        "stderr": err_file,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        cf = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if cf:
            popen_kw["creationflags"] = cf
    log(f"starting {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, **popen_kw)
    proc._stderr_file = err_file  # type: ignore[attr-defined]
    return proc


def close_stderr_file(proc):
    f = getattr(proc, "_stderr_file", None) if proc is not None else None
    if f is None:
        return
    try:
        f.close()
    except Exception:
        pass
    try:
        delattr(proc, "_stderr_file")
    except Exception:
        pass


def read_stderr_snippet(proc, limit=700):
    """Last ~limit chars of streamlink stderr, single-line for lastError."""
    f = getattr(proc, "_stderr_file", None) if proc is not None else None
    if f is None:
        return ""
    try:
        f.flush()
        f.seek(0)
        data = f.read() or b""
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        else:
            text = str(data)
        skip = ("notopensslwarning", "urllib3", "warnings.warn", "currently the 'ssl' module")
        lines = []
        for ln in text.splitlines():
            s = ln.strip()
            if not s:
                continue
            low = s.lower()
            if any(x in low for x in skip):
                continue
            lines.append(s)
        if not lines:
            return ""
        joined = " | ".join(lines)
        if len(joined) > limit:
            joined = "…" + joined[-limit:]
        return joined
    except Exception:
        return ""


def with_stderr(err_msg, proc):
    snippet = read_stderr_snippet(proc)
    if not snippet:
        return err_msg
    return f"{err_msg} — {snippet}"


_AD_PAUSE_RE = re.compile(r"filtering out segments and pausing stream output", re.I)
_AD_RESUME_RE = re.compile(r"resuming stream output", re.I)
_AD_PREROLL_RE = re.compile(r"waiting for pre-roll ads to finish", re.I)
_AD_BREAK_RE = re.compile(r"detected advertisement break of (\d+) second", re.I)


def _stderr_tail_bytes(proc, limit=16384):
    """v6.57: last bytes of streamlink's stderr temp file without moving the shared offset.

    The child writes through the same open file, so a seek here could make it overwrite its
    own log. POSIX: os.pread. Elsewhere: short seek/read, then back to the end.
    """
    f = getattr(proc, "_stderr_file", None) if proc is not None else None
    if f is None:
        return b""
    try:
        fd = f.fileno()
        size = os.fstat(fd).st_size
        start = max(0, size - limit)
        if hasattr(os, "pread"):
            return os.pread(fd, size - start, start) or b""
        try:
            os.lseek(fd, start, os.SEEK_SET)
            return os.read(fd, size - start) or b""
        finally:
            os.lseek(fd, 0, os.SEEK_END)
    except Exception:
        return b""


def streamlink_ad_state(proc, text=None):
    """v6.57: is streamlink currently sitting in a Twitch ad (output paused on purpose)?

    Returns {inAd, preroll, breakSecs}. inAd: the last pause / pre-roll line has no
    "Resuming stream output" after it. breakSecs: the newest "advertisement break of N s".
    """
    if text is None:
        text = _stderr_tail_bytes(proc).decode("utf-8", errors="replace")
    last_pause = -1
    for m in _AD_PAUSE_RE.finditer(text):
        last_pause = m.start()
    last_pre = -1
    for m in _AD_PREROLL_RE.finditer(text):
        last_pre = m.start()
    last_resume = -1
    for m in _AD_RESUME_RE.finditer(text):
        last_resume = m.start()
    secs = None
    for m in _AD_BREAK_RE.finditer(text):
        try:
            secs = int(m.group(1))
        except ValueError:
            pass
    started = max(last_pause, last_pre)
    in_ad = started >= 0 and last_resume < started
    return {"inAd": bool(in_ad), "preroll": bool(in_ad and last_pre >= 0 and last_pre >= last_pause),
            "breakSecs": secs}


def ad_pause_allowance(state, base_secs):
    """v6.57: how long a no-growth gap may last while streamlink says an ad is playing."""
    if not AD_PAUSE_MAX_SECS or not state or not state.get("inAd"):
        return base_secs
    secs = state.get("breakSecs")
    if secs:
        want = int(secs) + max(45, base_secs)
    else:
        want = AD_PAUSE_MAX_SECS
    return max(base_secs, min(AD_PAUSE_MAX_SECS, want))


def _note_ad_break_locked(username, gen, state, since, preroll=False):
    """v6.57: mark rec['adBreak'] (shown on the page) and count it once per break."""
    rec = ACTIVE.get(username)
    if not rec or rec.get("gen") != gen:
        return False
    if rec.get("adBreak"):
        rec["adBreak"]["secs"] = state.get("breakSecs") or rec["adBreak"].get("secs")
        return False
    rec["adBreak"] = {"since": since, "secs": state.get("breakSecs"), "preroll": bool(preroll)}
    rec["adBreaks"] = int(rec.get("adBreaks") or 0) + 1
    AD_STATS["prerolls" if preroll else "breaks"] += 1
    AD_STATS["lastAt"] = time.time()
    AD_STATS["lastUser"] = username
    AD_STATS["lastSecs"] = state.get("breakSecs")
    return True


def _clear_ad_break_locked(username, gen):
    rec = ACTIVE.get(username)
    if rec and rec.get("gen") == gen and rec.get("adBreak"):
        rec.pop("adBreak", None)
        return True
    return False


def looks_offline_err(err_msg):
    low = (err_msg or "").lower()
    return any(h in low for h in OFFLINE_STDERR_HINTS)


def still_wanted(username, gen):
    with LOCK:
        rec = ACTIVE.get(username)
        return bool(rec and rec.get("want", True) and rec.get("gen") == gen)


def monitor_quick_fail(username, gen, proc, out_path):
    """Watch ~QUICK_FAIL_SECS. Return (ok, err_msg). ok means established."""
    t0 = time.time()
    deadline = t0 + QUICK_FAIL_SECS
    last_size = 0
    grew = False
    ad_checked_at = 0.0
    ad_noted = False
    while True:
        now = time.time()
        if now >= deadline and not grew and AD_PAUSE_MAX_SECS:
            # v6.57: nothing written yet — a Twitch pre-roll (or an ad on the first
            # playlist) holds streamlink's output back. Wait it out instead of retrying
            # into a brand-new pre-roll.
            if now - ad_checked_at >= 1.5:
                ad_checked_at = now
                st = streamlink_ad_state(proc)
                if st["inAd"] and proc.poll() is None:
                    deadline = max(deadline, min(t0 + ad_pause_allowance(st, QUICK_FAIL_SECS),
                                                 t0 + AD_PAUSE_MAX_SECS + QUICK_FAIL_SECS))
                    if not ad_noted:
                        ad_noted = True
                        with LOCK:
                            _note_ad_break_locked(username, gen, st, t0, preroll=True)
                        log(f"ad break: {username} — Twitch pre-roll ad before the stream"
                            + (f" ({st['breakSecs']} s)" if st.get("breakSecs") else "")
                            + f"; waiting up to {int(deadline - t0)} s instead of retrying")
        if time.time() >= deadline:
            break
        if not still_wanted(username, gen):
            terminate_proc(proc)
            return False, "stopped"
        code = proc.poll()
        if code is not None:
            sz = file_size(out_path)
            base = (
                f"streamlink exited quickly (code {code}, file={sz}B) — "
                "is the stream live / playlist ready?"
            )
            return False, with_stderr(base, proc)
        sz = file_size(out_path)
        if sz > last_size:
            if last_size > 0 or sz > 0:
                grew = True
            last_size = sz
        # Playlist is clearly flowing — treat as established early
        if grew and last_size > 0 and (time.time() - t0) >= 3:
            if ad_noted:
                with LOCK:
                    _clear_ad_break_locked(username, gen)
                log(f"ad break over: {username} — pre-roll finished after {int(time.time() - t0)} s, recording")
            return True, None
        time.sleep(0.5)
    code = proc.poll()
    if code is not None:
        sz = file_size(out_path)
        base = (
            f"streamlink exited within {QUICK_FAIL_SECS}s (code {code}, file={sz}B)"
        )
        return False, with_stderr(base, proc)
    sz = file_size(out_path)
    if sz <= 0 and not grew:
        terminate_proc(proc)
        if ad_noted:
            with LOCK:
                _clear_ad_break_locked(username, gen)
        return False, with_stderr(
            f"no output file growth within {int(deadline - t0)}s"
            + (" (Twitch pre-roll ad never ended)" if ad_noted else "")
            + " — retrying",
            proc,
        )
    if ad_noted:
        with LOCK:
            _clear_ad_break_locked(username, gen)
    return True, None


def interruptible_backoff(username, gen, seconds):
    """Sleep up to seconds; return False if stop/gen cancelled.

    v6.48: returns True early when the restart grace was released (graceRelease) so the
    supervisor finalizes right away instead of finishing a 30 s sleep. Callers that can be
    in grace must check grace_release_reason() after this returns True.
    """
    end = time.time() + seconds
    while time.time() < end:
        with LOCK:
            rec = ACTIVE.get(username)
            if not (rec and rec.get("want", True) and rec.get("gen") == gen):
                return False
            if rec.get("graceRelease"):
                return True
        time.sleep(0.25)
    return still_wanted(username, gen)


def grace_release_reason(username, gen):
    """v6.48: reason string if the page released this slot's restart grace, else None."""
    with LOCK:
        rec = ACTIVE.get(username)
        if rec and rec.get("gen") == gen and rec.get("graceRelease"):
            return str(rec.get("graceReleaseReason") or "restart grace released")
    return None


def clear_grace_release_locked(rec):
    """v6.48: new grace episode / re-established — forget any earlier release. Hold LOCK."""
    rec["graceRelease"] = False
    rec.pop("graceReleaseReason", None)


def release_restart_grace(username, reason=None):
    """v6.48: end the v6.42 stream-restart wait early for a slot currently in grace.

    Called by POST /api/grace/release when the recorder page has seen Twitch Helix report the
    streamer offline for GRACE_OFFLINE_SECS. Sets graceRelease and clears graceUntil; the
    supervisor notices within ~0.25 s (interruptible_backoff) or right after the try in flight,
    and finalizes exactly like a grace timeout (segments → seamless / voice removal / Drive).
    A try that establishes before that wins: the flag is cleared and recording continues.
    """
    u = safe_username(username)
    if not u:
        return {"ok": False, "error": "invalid username"}, 400
    why = " ".join(str(reason or "").split())[:200] or "Twitch says offline"
    now = time.time()
    with LOCK:
        rec = ACTIVE.get(u)
        if not rec:
            return {"ok": False, "error": f"not recording {u}"}, 400
        until = float(rec.get("graceUntil") or 0.0)
        if rec.get("graceRelease"):
            return {"ok": True, "released": True, "already": True,
                    "reason": str(rec.get("graceReleaseReason") or why), "username": u}, 200
        if not rec.get("retrying") or until <= now:
            return {"ok": False, "error": f"{u} is not waiting through a stream restart"}, 400
        rec["graceRelease"] = True
        rec["graceReleaseReason"] = why
        rec["graceUntil"] = 0.0
    log(f"restart grace released {u}: {why}")
    return {"ok": True, "released": True, "reason": why, "username": u}, 200


def mid_resume_backoff(resume_idx):
    """Backoff for mid-stream resume attempt resume_idx (0-based). Never gives up."""
    if resume_idx < len(MID_RESUME_BACKOFFS):
        return MID_RESUME_BACKOFFS[resume_idx]
    return MID_RESUME_BACKOFFS[-1]


def restart_grace_applies(username):
    """v6.42 / v6.46: keep retrying through a stream restart?

    True when:
      • no page has reported in since the helper started (helper running alone), or
      • the latest heartbeat lists this streamer as Auto-Rec armed, or
      • the page heartbeat is stale (v6.46: Chrome closed/crashed / tab gone — same
        PAGE_HEARTBEAT_STALE_SECS window as status-file recorderPageOpen). The helper is
        alone with whatever it is already recording, so keep trying instead of using a
        stale empty armed list that would drop the restart grace.

    False only while a fresh page heartbeat says this streamer is not armed.

    Takes STATUS_FILE_LOCK only — never call while holding LOCK.
    """
    if RESTART_GRACE_SECS <= 0:
        return False
    u = (username or "").lower()
    try:
        with STATUS_FILE_LOCK:
            seen_at = float(PAGE_SEEN.get("at") or 0.0)
            armed = [str(a).lower() for a in (PAGE_SEEN.get("armed") or [])]
    except NameError:  # defined later in the module; only during import
        return True
    if not seen_at:
        return True
    # v6.46: page closed/crashed — don't trust a stale armed list; keep trying
    try:
        stale = float(PAGE_HEARTBEAT_STALE_SECS)
    except (TypeError, ValueError, NameError):
        stale = 120.0
    if stale > 0 and (time.time() - seen_at) >= stale:
        return True
    return u in armed


def grace_backoff(idx):
    if idx < len(RESTART_GRACE_BACKOFFS):
        return RESTART_GRACE_BACKOFFS[idx]
    return RESTART_GRACE_BACKOFFS[-1]


def _clock_label(ts):
    """'9:14 PM' local (no %-I so Windows works)."""
    lt = time.localtime(ts)
    h = lt.tm_hour % 12 or 12
    return f"{h}:{lt.tm_min:02d} {'AM' if lt.tm_hour < 12 else 'PM'}"


def grace_wait_msg(username, until, err):
    base = (f"waiting for {username} to come back (stream ended or restarting) — "
            f"helper keeps trying until {_clock_label(until)}")
    return base + (f" — last try: {err}" if err else "")


def set_grace_until(username, gen, until):
    with LOCK:
        rec = ACTIVE.get(username)
        if rec and rec.get("gen") == gen:
            rec["graceUntil"] = float(until or 0.0)


def _drop_slot_finalize_claimed_locked(username, gen):
    """v6.54: record_supervisor drops a slot whose recording never got (re-)established
    (quick-fail give-up, streamlink won't start, stop/gen race). Caller holds LOCK.

    v6.53 hands a crash/reboot's cut-off segments to the *next* start_record of the same
    streamer (claim_orphan_segments) and clears their crash note right away. If that new
    recording then never establishes — Helix still said live for a minute after the stream
    ended, the stream really did end during the reboot, streamlink is broken — the slot used to
    be popped without finalizing, so the claimed segments were never sent: no original, no voice
    removal, no Drive parts, and no note left to recover them on the next start. Now they are
    finalized here exactly like a normal stream end. Returns the finalized segment names.
    """
    rec = ACTIVE.get(username)
    if not rec or rec.get("gen") != gen:
        return []
    try:
        segs = collect_session_segments_from_rec(rec)
    except Exception:
        segs = []
    ACTIVE.pop(username, None)
    if not segs:
        return []
    try:
        _live_note("finalized cut-off recording", username, segs,
                   "the resumed recording never started — original + voice removal + parts to Drive")
    except Exception:
        pass
    remember_session_segments(username, segs, reason=RECOVER_UNSTARTED_REASON)
    return segs


RECOVER_UNSTARTED_REASON = "recovered after helper/computer restart (resumed recording never started)"


def record_supervisor(username, quality, gen):
    """Background: spawn streamlink, retry early failures, then mid-stream auto-resume."""
    attempt = 0
    max_attempts = 1 + len(RETRY_BACKOFFS)
    first_fail_at = None  # v6.42 restart grace (start quick-fails)
    grace_idx = 0
    tries = 0
    while attempt < max_attempts:
        if not still_wanted(username, gen):
            with LOCK:
                _drop_slot_finalize_claimed_locked(username, gen)
            return
        with LOCK:
            rec = ACTIVE.get(username)
            if not rec or rec.get("gen") != gen:
                return
            rec["starting"] = True
            rec["retrying"] = attempt > 0
            rec["attempt"] = attempt
            rec["proc"] = None
            rec["pid"] = None
            rec["supervised"] = False
        try:
            cmd, out = build_cmd(username, quality)
            proc = spawn_streamlink(cmd)
        except OSError as e:
            err = f"failed to start streamlink: {e}"
            set_last_error(username, err)
            log(err)
            if attempt >= len(RETRY_BACKOFFS):
                with LOCK:
                    _drop_slot_finalize_claimed_locked(username, gen)
                return
            backoff = RETRY_BACKOFFS[attempt]
            attempt += 1
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    rec["starting"] = False
                    rec["retrying"] = True
            log(f"retry {username} in {backoff}s (attempt {attempt})")
            if not interruptible_backoff(username, gen, backoff):
                with LOCK:
                    _drop_slot_finalize_claimed_locked(username, gen)
                return
            continue

        started = time.strftime("%Y-%m-%dT%H:%M:%S")
        with LOCK:
            rec = ACTIVE.get(username)
            if not rec or rec.get("gen") != gen or not rec.get("want", True):
                terminate_proc(proc)
                if rec and rec.get("gen") == gen:
                    _drop_slot_finalize_claimed_locked(username, gen)
                return
            rec["proc"] = proc
            rec["file"] = str(out)
            rec["startedAt"] = started
            rec["quality"] = quality
            rec["pid"] = proc.pid
            rec["starting"] = True
            rec["retrying"] = attempt > 0

        tries += 1
        ok, err = monitor_quick_fail(username, gen, proc, out)
        if not still_wanted(username, gen):
            terminate_proc(proc)
            with LOCK:
                _drop_slot_finalize_claimed_locked(username, gen)
            return
        if ok:
            set_last_error(username, None)
            keep = False
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    rec["starting"] = False
                    rec["retrying"] = False
                    rec["graceUntil"] = 0.0
                    clear_grace_release_locked(rec)  # v6.48: came back — keep recording
                    rec["proc"] = proc
                    rec["file"] = str(out)
                    rec["pid"] = proc.pid
                    rec["supervised"] = True
                    rec.setdefault("resumes", 0)
                    if not rec.get("sessionStartedAt"):
                        rec["sessionStartedAt"] = started
                    keep = True
            if not keep:
                terminate_proc(proc)
                return
            log(f"recording {username} pid={proc.pid} -> {out}")
            live_session_touch(username, gen)  # v6.53: crash/reboot can't orphan this session
            # Keep ownership: watch for mid-stream death and auto-resume.
            watch_and_resume(username, quality, gen, proc)
            return

        terminate_proc(proc)
        failed_path = str(out)
        set_last_error(username, err or "streamlink quick-fail")
        log(f"quick-fail {username}: {err}")
        with LOCK:
            rec = ACTIVE.get(username)
            if rec and rec.get("gen") == gen and (rec.get("file") or "") == failed_path:
                rec["file"] = ""
        scrub_failed_output(failed_path)
        now = time.time()
        if first_fail_at is None:
            first_fail_at = now
        if attempt >= len(RETRY_BACKOFFS):
            # v6.42: stream restart / slow playlist — keep trying for the grace window
            until = first_fail_at + RESTART_GRACE_SECS
            released = grace_release_reason(username, gen)  # v6.48: Helix confirmed offline
            if now < until and not released and restart_grace_applies(username):
                backoff = grace_backoff(grace_idx)
                grace_idx += 1
                set_last_error(username, grace_wait_msg(username, until, err))
                with LOCK:
                    rec = ACTIVE.get(username)
                    if not rec or rec.get("gen") != gen:
                        return
                    rec["proc"] = None
                    rec["pid"] = None
                    rec["starting"] = False
                    rec["retrying"] = True
                    rec["graceUntil"] = until
                log(f"restart grace {username}: retry in {backoff}s (try {tries + 1}, until {_clock_label(until)})")
                if not interruptible_backoff(username, gen, backoff):
                    with LOCK:
                        _drop_slot_finalize_claimed_locked(username, gen)
                    return
                released = grace_release_reason(username, gen)
                if not released:
                    continue
            if released:
                # v6.48: same finalize as the grace deadline, just sooner
                set_last_error(username, f"stopped waiting for {username} to come back after {tries} tries — "
                                         f"{released}")
            elif grace_idx:
                set_last_error(username, f"stopped retrying {username} after {tries} tries over "
                                         f"{int(round((now - first_fail_at) / 60.0))} min — {err}")
            with LOCK:
                _drop_slot_finalize_claimed_locked(username, gen)
            log(f"gave up on {username} after {tries} attempts")
            return
        backoff = RETRY_BACKOFFS[attempt]
        attempt += 1
        with LOCK:
            rec = ACTIVE.get(username)
            if not rec or rec.get("gen") != gen:
                return
            rec["proc"] = None
            rec["pid"] = None
            rec["starting"] = False
            rec["retrying"] = True
            rec["attempt"] = attempt
        log(f"retry {username} in {backoff}s (attempt {attempt + 1}/{max_attempts})")
        if not interruptible_backoff(username, gen, backoff):
            with LOCK:
                _drop_slot_finalize_claimed_locked(username, gen)
            return

    with LOCK:
        _drop_slot_finalize_claimed_locked(username, gen)


def watch_and_resume(username, quality, gen, proc):
    """After establish: detect mid-stream exit/stall, keep segment, start new file while wanted.

    Gives up after MAX_MID_QUICK_FAILS consecutive establish failures (or sooner when
    stderr clearly says offline), so a closed recorder page cannot leave a forever-retrying
    supervisor after the broadcast ends. If the stream is still live, the HTML Auto-Rec
    poll can POST /api/record again.
    """
    resume_idx = 0
    consecutive_quick_fails = 0
    # v6.42: when the last segment ended; restart grace runs from here
    seg_ended_at = 0.0
    grace_ok = False
    grace_idx = 0
    with LOCK:
        rec0 = ACTIVE.get(username) or {}
        path0 = rec0.get("file") or ""
    last_size = file_size(path0)
    last_growth = time.time()
    ad_checked_at = 0.0  # v6.57
    ad_state = None
    ad_logged = False

    def in_grace():
        """v6.42: still inside the restart window for this ended segment?"""
        return grace_ok and time.time() < seg_ended_at + RESTART_GRACE_SECS

    def give_up_released(reason):
        """v6.48: page saw Helix offline long enough — finalize like a grace timeout."""
        mins = int(round((time.time() - seg_ended_at) / 60.0)) if seg_ended_at else 0
        give_up(
            f"stopped waiting for {username} to come back"
            + (f" after {mins} min" if mins else "")
            + f" — {reason}"
        )

    def grace_or_give_up(fails, last_err):
        """Out of normal retries: keep waiting through a restart, or give up. True = keep going."""
        nonlocal grace_ok
        released = grace_release_reason(username, gen)  # v6.48
        if released:
            give_up_released(released)
            return False
        if grace_ok:
            # re-check armed state (page may have turned Auto-Rec off meanwhile)
            grace_ok = restart_grace_applies(username)
        if in_grace():
            until = seg_ended_at + RESTART_GRACE_SECS
            set_last_error(username, grace_wait_msg(username, until, last_err))
            return True
        mins = int(round((time.time() - seg_ended_at) / 60.0)) if seg_ended_at else 0
        give_up(
            f"gave up after {fails} mid-resume fails"
            + (f" over {mins} min" if grace_idx and mins else "")
            + " — stream likely offline (Auto-Rec will restart if still live)"
            + (f" — {last_err}" if last_err else "")
        )
        return False

    def begin_mid_resume(err_msg):
        """Mark slot retrying after a finished segment; caller owns resume loop."""
        nonlocal seg_ended_at, grace_ok, grace_idx
        seg_ended_at = time.time()
        grace_idx = 0
        grace_ok = restart_grace_applies(username)  # outside LOCK
        set_last_error(username, err_msg)
        with LOCK:
            rec = ACTIVE.get(username)
            if not rec or rec.get("gen") != gen:
                return False
            # Prior file is finished — keep it for seamless join; clear active path.
            finished = rec.get("file") or ""
            append_finished_segment_locked(rec, finished)
            rec["file"] = ""
            rec["retrying"] = True
            rec["starting"] = False
            rec["proc"] = None
            rec["pid"] = None
            rec["supervised"] = True
            rec["resumes"] = int(rec.get("resumes") or 0) + 1
            rec["graceUntil"] = (seg_ended_at + RESTART_GRACE_SECS) if grace_ok else 0.0
            clear_grace_release_locked(rec)  # v6.48: fresh grace episode for this segment end
        return True

    def give_up(reason):
        set_last_error(username, reason)
        log(f"mid-resume give-up {username}: {reason}")
        terminate_proc(proc)
        with LOCK:
            rec = ACTIVE.get(username)
            if rec and rec.get("gen") == gen:
                segs = collect_session_segments_from_rec(rec)
                ACTIVE.pop(username, None)
                remember_session_segments(username, segs)

    while still_wanted(username, gen):
        code = proc.poll()
        if code is None:
            with LOCK:
                rec = ACTIVE.get(username)
                path = (rec.get("file") if rec else None) or ""
            sz = file_size(path)
            now = time.time()
            if sz > last_size:
                if ad_logged:
                    with LOCK:
                        _clear_ad_break_locked(username, gen)
                    if now - last_growth >= STALL_SECS:
                        AD_STATS["stallsAvoided"] += 1
                    log(f"ad break over: {username} — output resumed after {int(now - last_growth)} s")
                ad_logged = False
                ad_state = None
                last_size = sz
                last_growth = now
                # Growing is the best time to notice a nearly-full volume before
                # the next segment write fails opaquely.
                disk_err = disk_block_error()
                if disk_err:
                    give_up(disk_err + " — stopped mid-stream to avoid filling the disk")
                    return
                time.sleep(0.75)
                continue
            gap = now - last_growth
            if AD_PAUSE_MAX_SECS and gap >= min(15.0, STALL_SECS * 0.5) and now - ad_checked_at >= 3:
                # v6.57: a Twitch ad pauses output on purpose — read streamlink's log
                ad_checked_at = now
                ad_state = streamlink_ad_state(proc)
                if ad_state["inAd"]:
                    with LOCK:
                        fresh = _note_ad_break_locked(username, gen, ad_state, last_growth)
                    if fresh or not ad_logged:
                        ad_logged = True
                        log(f"ad break: {username} — Twitch ad"
                            + (f" ({ad_state['breakSecs']} s)" if ad_state.get("breakSecs") else "")
                            + ", streamlink paused output; not a stall (waiting up to "
                            + f"{ad_pause_allowance(ad_state, STALL_SECS)} s)")
                elif ad_logged:
                    with LOCK:
                        _clear_ad_break_locked(username, gen)
                    ad_logged = False
            limit = ad_pause_allowance(ad_state, STALL_SECS) if ad_state else STALL_SECS
            if gap < limit:
                time.sleep(0.75)
                continue
            in_ad = bool(ad_state and ad_state.get("inAd"))
            if in_ad:
                with LOCK:
                    _clear_ad_break_locked(username, gen)
            err = with_stderr(
                f"streamlink stalled (no file growth for {int(gap)}s"
                + (", longer than the Twitch ad break it reported" if in_ad else "")
                + ") — resuming",
                proc,
            )
            log(f"mid-stream stall: {username} size={sz}B — {err}")
            terminate_proc(proc)
            if not begin_mid_resume(err):
                return
            # Fall into shared resume loop below (same as exit path).
        else:
            # Finished segment stays on disk; clear ACTIVE proc and resume.
            err = with_stderr(
                f"streamlink exited mid-stream (code {code}) — resuming",
                proc,
            )
            log(f"mid-stream death: {username} (exit {code}) — {err}")
            close_stderr_file(proc)
            if not begin_mid_resume(err):
                return

        # Keep trying while wanted; stop after consecutive establish failures.
        while still_wanted(username, gen):
            if consecutive_quick_fails >= MAX_MID_QUICK_FAILS:
                backoff = grace_backoff(grace_idx)  # v6.42 restart grace
                grace_idx += 1
            else:
                backoff = mid_resume_backoff(resume_idx)
            log(f"mid-resume {username} in {backoff}s (resume #{resume_idx + 1})")
            if not interruptible_backoff(username, gen, backoff):
                break
            if not still_wanted(username, gen):
                break
            released = grace_release_reason(username, gen)  # v6.48: woke early on release
            if released:
                give_up_released(released)
                return

            with LOCK:
                rec = ACTIVE.get(username)
                if not rec or rec.get("gen") != gen:
                    return
                rec["starting"] = True
                rec["retrying"] = True
                rec["proc"] = None
                rec["pid"] = None

            disk_err = disk_block_error()
            if disk_err:
                give_up(disk_err + " — stopped mid-resume to avoid filling the disk")
                return

            try:
                cmd, out = build_cmd(username, quality)
                new_proc = spawn_streamlink(cmd)
            except OSError as e:
                err2 = f"failed to start streamlink (mid-resume): {e}"
                set_last_error(username, err2)
                log(err2)
                with LOCK:
                    rec = ACTIVE.get(username)
                    if rec and rec.get("gen") == gen:
                        rec["starting"] = False
                        rec["retrying"] = True
                resume_idx += 1
                consecutive_quick_fails += 1
                if consecutive_quick_fails >= MAX_MID_QUICK_FAILS:
                    if not grace_or_give_up(consecutive_quick_fails, err2):
                        return
                continue

            started = time.strftime("%Y-%m-%dT%H:%M:%S")
            with LOCK:
                rec = ACTIVE.get(username)
                if not rec or rec.get("gen") != gen or not rec.get("want", True):
                    terminate_proc(new_proc)
                    return
                rec["proc"] = new_proc
                rec["file"] = str(out)
                rec["startedAt"] = started
                rec["quality"] = quality
                rec["pid"] = new_proc.pid
                rec["starting"] = True
                rec["retrying"] = True

            ok, qerr = monitor_quick_fail(username, gen, new_proc, out)
            if not still_wanted(username, gen):
                terminate_proc(new_proc)
                break
            if ok:
                set_last_error(username, None)
                consecutive_quick_fails = 0
                with LOCK:
                    rec = ACTIVE.get(username)
                    if rec and rec.get("gen") == gen:
                        rec["starting"] = False
                        rec["retrying"] = False
                        rec["proc"] = new_proc
                        rec["file"] = str(out)
                        rec["pid"] = new_proc.pid
                        rec["supervised"] = True
                        rec["graceUntil"] = 0.0
                        clear_grace_release_locked(rec)  # v6.48: came back — keep recording
                        if not rec.get("sessionStartedAt"):
                            rec["sessionStartedAt"] = started
                log(f"recording {username} (segment resume) pid={new_proc.pid} -> {out}"
                    + (f" after {int(time.time() - seg_ended_at)}s restart wait" if grace_idx else ""))
                live_session_touch(username, gen)  # v6.53
                proc = new_proc
                resume_idx = 0
                grace_idx = 0
                last_size = file_size(out)
                last_growth = time.time()
                break  # back to outer watch loop

            terminate_proc(new_proc)
            failed_path = str(out)
            qerr = qerr or "streamlink quick-fail (mid-resume)"
            set_last_error(username, qerr)
            log(f"mid-resume quick-fail {username}: {qerr}")
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    rec["proc"] = None
                    rec["pid"] = None
                    rec["starting"] = False
                    rec["retrying"] = True
                    if (rec.get("file") or "") == failed_path:
                        rec["file"] = ""
            scrub_failed_output(failed_path)
            resume_idx += 1
            consecutive_quick_fails += 1
            # Soft stop: stderr says offline after 2 fails; hard stop at MAX.
            soft = looks_offline_err(qerr) and consecutive_quick_fails >= 2
            hard = consecutive_quick_fails >= MAX_MID_QUICK_FAILS
            if soft or hard:
                # v6.42: a restart looks exactly like "offline" for a minute or two
                if not grace_or_give_up(consecutive_quick_fails, qerr):
                    return
        else:
            # inner while exited without break → stop wanted false or gen mismatch
            break
        # If outer still_wanted is false, fall through to cleanup below

    # Stop / gen cancel: terminate and drop slot if still ours
    terminate_proc(proc)
    with LOCK:
        rec = ACTIVE.get(username)
        if rec and rec.get("gen") == gen:
            segs = collect_session_segments_from_rec(rec)
            ACTIVE.pop(username, None)
            remember_session_segments(username, segs)
    log(f"supervisor exit {username} gen={gen}")



def start_record(username, quality, started_by=None):
    sl = which_streamlink()
    if not sl:
        err = "streamlink not found — install with: pip install streamlink"
        set_last_error(username, err)
        return {"ok": False, "error": err}
    quality = quality if quality in ("audio_only", "best") else "audio_only"
    disk_err = disk_block_error()
    if disk_err:
        set_last_error(username, disk_err)
        log(f"refuse record {username}: {disk_err}")
        return {"ok": False, "error": disk_err}
    by = (str(started_by).strip().lower() if started_by else "page") or "page"
    if by not in ("page", "helper"):
        by = "page"
    with LOCK:
        reap_locked()
        if username in ACTIVE:
            rec = ACTIVE[username]
            # Idempotent: already recording / starting / retrying — do not double-start
            return {
                "ok": True,
                "file": rec.get("file") or "",
                "pid": rec.get("pid"),
                "already": True,
                "retrying": bool(rec.get("retrying") or rec.get("starting")),
                "lastError": LAST_ERROR.get(username),
                "startedBy": rec.get("startedBy") or "page",
            }
        gen = GEN.get(username, 0) + 1
        GEN[username] = gen
        ACTIVE[username] = {
            "proc": None,
            "file": "",
            "startedAt": "",
            "sessionStartedAt": "",
            "quality": quality,
            "pid": None,
            "starting": True,
            "retrying": False,
            "want": True,
            "attempt": 0,
            "gen": gen,
            "supervised": False,
            "resumes": 0,
            "sessionSegments": [],
            "bps": 0.0,
            "peak": None,
            "level": None,
            "size_samples": [],
            "peak_at": 0.0,
            "peak_busy": False,
            "_bps_path": "",
            "startedBy": by,  # v6.49: "helper" when solo-watch started this
        }
    REC_DIR.mkdir(parents=True, exist_ok=True)
    try:  # v6.53: segments a crash/reboot cut off from this same broadcast join this session
        claimed = claim_orphan_segments(username)
    except Exception as e:
        claimed = []
        log(f"orphan claim failed for {username}: {e}")
    with LOCK:
        gen = ACTIVE[username]["gen"]
        if claimed:
            cur = ACTIVE[username].setdefault("sessionSegments", [])
            ACTIVE[username]["sessionSegments"] = [c for c in claimed if c not in cur] + cur
    if claimed:  # v6.54: re-note them under this run now, so a second crash before the new
        live_session_touch(username, gen)  # recording establishes can't lose them either
    t = threading.Thread(
        target=record_supervisor,
        args=(username, quality, gen),
        daemon=True,
        name=f"rec-{username}",
    )
    t.start()
    KEEP_AWAKE_WAKE.set()  # v6.40: hold sleep off right away
    return {
        "ok": True,
        "file": "",
        "pid": None,
        "retrying": True,
        "lastError": LAST_ERROR.get(username),
        "startedBy": by,
    }


def music_job_snapshot(job):
    """Public job fields for /api/health (and optional list)."""
    pct = job.get("progressPct")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
    return {
        "jobId": job.get("jobId"),
        "name": job.get("name"),
        "status": job.get("status") or "queued",
        "progress": job.get("progress") or "",
        "progressPct": pct,
        "error": job.get("error"),
        "outputName": job.get("outputName"),
        "vocalsName": job.get("vocalsName"),
        "redo": bool(job.get("redo")) if job.get("redo") is not None else None,
        "startedAt": job.get("startedAt"),
        "quality": job.get("quality"),  # v6.45 fast | better
        "qualityNote": job.get("qualityNote"),
        # v6.35: Drive / link jobs (absent keys = native History/Auto Music job)
        **_drive_job_extra(job),
    }


def _drive_job_extra(job):
    if job.get("source") not in ("drive", "link", "pipeline", "split"):
        return {}
    return {
        "segments": job.get("segments"),
        "base": job.get("base"),
        "partsCount": job.get("partsCount"),
        "partSecs": job.get("partSecs"),
        "deliveryStatus": job.get("deliveryStatus"),
        "musicName": job.get("musicName"),
        "source": job.get("source"),
        "displayName": job.get("displayName"),
        "srcPath": job.get("srcPath"),
        "fileId": job.get("fileId"),
        "outputPath": job.get("outputPath"),
        "outputUrl": job.get("outputUrl"),
        "outMode": job.get("outMode"),
        "phase": job.get("phase"),
        "info": job.get("info"),
        "leadingSilence": job.get("leadingSilence"),
        "keepSource": bool(job.get("keepSource")),
        "keptSourceName": job.get("keptSourceName"),
        "already": bool(job.get("already")),
        "finishedAt": job.get("finishedAt"),
    }


def music_jobs_for_health():
    """Queued/running jobs plus recently finished done/error (~15 min). Prunes older finished."""
    now = time.time()
    out = []
    dead = []
    with MUSIC_JOBS_LOCK:
        for jid, job in MUSIC_JOBS.items():
            st = job.get("status") or ""
            if st in ("done", "error", "cancelled"):
                finished_at = float(job.get("finishedAt") or 0.0)
                if finished_at and (now - finished_at) > MUSIC_JOB_KEEP_FINISHED_SECS:
                    dead.append(jid)
                    continue
            out.append(music_job_snapshot(job))
        for jid in dead:
            MUSIC_JOBS.pop(jid, None)
    return out


def music_demucs_queue_info():
    """Counts for UI: how many Music only jobs are running vs waiting on the demucs slot."""
    running = 0
    waiting = 0
    with MUSIC_JOBS_LOCK:
        for job in MUSIC_JOBS.values():
            st = job.get("status") or ""
            if st == "running":
                running += 1
            elif st == "queued":
                waiting += 1
    return {
        "maxConcurrent": DEMUCS_MAX_CONCURRENT,
        "running": running,
        "waiting": waiting,
    }


def disk_file_version():
    """Read INSTALL_DIR / VERSION (first line strip). Empty if missing/unreadable."""
    try:
        p = INSTALL_DIR / "VERSION"
        if p.is_file():
            return p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except OSError:
        pass
    return ""


def local_helper_version():
    """Prefer in-process HELPER_VERSION (running process); fall back to disk VERSION."""
    ver = str(HELPER_VERSION or "").strip()
    if ver:
        return ver
    return disk_file_version()


def pending_restart_needed():
    """True when disk VERSION is non-empty and differs from running HELPER_VERSION.

    Signals that apply_update wrote new files while this old process is still alive.
    """
    disk = disk_file_version()
    if not disk:
        return False
    running = str(HELPER_VERSION or "").strip()
    return bool(running) and disk != running


def music_jobs_busy():
    """True if any demucs Music only job is queued or running."""
    info = music_demucs_queue_info()
    if DELIVERY_STATE.get("busy"):  # v6.36: mid-copy into Drive
        return True
    return (int(info.get("running") or 0) > 0) or (int(info.get("waiting") or 0) > 0)


def schedule_graceful_restart(reason="manual"):
    """Respond to the client first; after ~0.8s replace this process via os.execv."""

    def _restart():
        try:
            time.sleep(0.8)
            script = Path(__file__).resolve()
            os.chdir(str(INSTALL_DIR))
            argv = [sys.executable, str(script)] + list(sys.argv[1:])
            log(f"restarting helper ({reason}): execv {argv!r}")
            keep_awake_release()  # v6.40: don't leave an orphan caffeinate behind
            os.execv(sys.executable, argv)
        except Exception as e:
            log(f"restart exec failed ({reason}): {e}")
            os._exit(1)

    t = threading.Thread(target=_restart, daemon=True, name="helper-restart")
    t.start()
    log(f"scheduled graceful restart in ~0.8s ({reason})")


def restart_helper(force=False):
    """POST /api/restart. Refuse when ACTIVE or demucs busy unless force:true."""
    with LOCK:
        reap_locked()
        active = list(ACTIVE.keys())
    disk_ver = disk_file_version()
    if active and not force:
        return (
            {
                "ok": False,
                "error": "active recordings",
                "active": active,
                "pendingRestart": True,
                "version": HELPER_VERSION,
                "diskVersion": disk_ver,
            },
            409,
        )
    if music_jobs_busy() and not force:
        return (
            {
                "ok": False,
                "error": "music jobs running",
                "musicDemucsQueue": music_demucs_queue_info(),
                "pendingRestart": True,
                "version": HELPER_VERSION,
                "diskVersion": disk_ver,
            },
            409,
        )
    reason = "force" if force else "api"
    schedule_graceful_restart(reason=reason)
    return (
        {
            "ok": True,
            "restarting": True,
            "version": HELPER_VERSION,
            "diskVersion": disk_ver,
            "note": "Helper restarting shortly — expect a brief disconnect, then reconnect.",
        },
        200,
    )


def maybe_auto_restart_idle():
    """If disk VERSION != running and idle, schedule one graceful restart (health path)."""
    global _auto_restart_scheduled
    if _auto_restart_scheduled:
        return
    if not pending_restart_needed():
        return
    # v6.43: same busy rules as the unattended updater (recording incl. restart-grace wait,
    # Demucs/split queue, Drive copies + queued originals, seamless joins, uploads) and only
    # after the helper has been idle for AUTO_UPDATE_IDLE_SECS (after-stream steps can queue).
    busy = update_busy_reason()
    if busy:
        _auto_update_set(waitingFor=busy)
        return
    if not update_idle_settled():
        _auto_update_set(waitingFor="just finished work — waiting a few idle minutes")
        return
    disk = disk_file_version()
    if os.environ.get("TWITCH_RECORDER_RESTARTED_FOR") == disk:
        # v6.43: we already restarted once for this disk VERSION and still don't match
        # (VERSION file out of step with the script) — don't loop restarting.
        _auto_restart_scheduled = True
        log(f"auto-restart skipped: already restarted for disk v{disk} but running v{HELPER_VERSION} "
            "(VERSION file doesn't match the helper script)")
        _auto_update_set(lastError=f"VERSION file says {disk} but the helper script is {HELPER_VERSION}")
        return
    os.environ["TWITCH_RECORDER_RESTARTED_FOR"] = disk  # survives execv into the new process
    _auto_restart_scheduled = True
    _auto_update_set(state="restarting", waitingFor=None)
    log(
        "auto-restart scheduled (idle-after-update): "
        f"disk v{disk_file_version()} != running v{HELPER_VERSION}"
    )
    schedule_graceful_restart(reason="idle-after-update")


def _parse_version_tuple(s):
    """Best-effort numeric tuple for compare (6.25 -> (6, 25))."""
    import re as _re
    parts = _re.findall(r"\d+", str(s or ""))
    if not parts:
        return (0,)
    try:
        return tuple(int(x) for x in parts)
    except ValueError:
        return (0,)


def fetch_url_text(url, timeout=12):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"TwitchAutoRecorder/{HELPER_VERSION}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def fetch_url_bytes(url, timeout=30):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"TwitchAutoRecorder/{HELPER_VERSION}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_remote_version():
    """Fetch remote VERSION; optionally parse HELPER_VERSION from server script."""
    ver = ""
    try:
        ver = fetch_url_text(UPDATE_RAW_BASE + "VERSION", timeout=12).strip().splitlines()[0].strip()
    except Exception as e:
        log(f"update check VERSION fetch failed: {e}")
    if not ver:
        try:
            py = fetch_url_text(UPDATE_RAW_BASE + "twitch-recorder-server.py", timeout=20)
            import re as _re
            m = _re.search(r'HELPER_VERSION\s*=\s*["\']([^"\']+)["\']', py)
            if m:
                ver = m.group(1).strip()
        except Exception as e:
            log(f"update check HELPER_VERSION parse failed: {e}")
            raise
    if not ver:
        raise RuntimeError("could not determine remote version")
    return ver


def check_update():
    local = local_helper_version()
    try:
        remote = fetch_remote_version()
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "localVersion": local,
            "remoteVersion": None,
            "updateAvailable": False,
            "repo": UPDATE_REPO,
            "rawBase": UPDATE_RAW_BASE,
        }
    available = _parse_version_tuple(remote) > _parse_version_tuple(local)
    out = {
        "ok": True,
        "localVersion": local,
        "remoteVersion": remote,
        "updateAvailable": available,
        "repo": UPDATE_REPO,
        "rawBase": UPDATE_RAW_BASE,
    }
    if available:
        out["files"] = list(UPDATE_FILES)
    return out


def atomic_write_bytes(dest: Path, data: bytes):
    """Write via temp file in same dir then os.replace (atomic on same volume)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=dest.name + ".",
        suffix=".tmp",
        dir=str(dest.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(dest))
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


UPDATE_BACKUP_DIRNAME = ".update-backup"  # v6.43: previous install kept here (one generation)
UPDATE_SELFTEST_TIMEOUT = 45


def _validate_update_blobs(blobs, remote):
    """v6.43: refuse a download that would leave a broken helper. Returns the new version."""
    import re as _re
    py = blobs.get("twitch-recorder-server.py") or b""
    html = blobs.get(HTML_NAME) or b""
    ver_raw = (blobs.get("VERSION") or b"").decode("utf-8", errors="replace").strip()
    ver_file = ver_raw.splitlines()[0].strip() if ver_raw else ""
    if len(py) < 20000:
        raise RuntimeError(f"downloaded helper looks truncated ({len(py)} bytes) — not installed")
    try:
        compile(py, "twitch-recorder-server.py", "exec")
    except SyntaxError as e:
        raise RuntimeError(f"downloaded helper has a syntax error (line {e.lineno}: {e.msg}) — not installed")
    m = _re.search(rb'HELPER_VERSION\s*=\s*["\']([^"\']+)["\']', py)
    if not m:
        raise RuntimeError("downloaded helper has no HELPER_VERSION — not installed")
    py_ver = m.group(1).decode("utf-8", errors="replace").strip()
    if not ver_file:
        raise RuntimeError("downloaded VERSION file is empty — not installed")
    if py_ver != ver_file:
        # would make the helper think a restart is pending forever (disk != running)
        raise RuntimeError(f"downloaded files disagree (VERSION {ver_file} vs helper {py_ver}) — not installed")
    if remote and _parse_version_tuple(ver_file) != _parse_version_tuple(remote):
        raise RuntimeError(f"GitHub changed while downloading (VERSION {remote} then {ver_file}) — try again")
    if len(html) < 20000 or b"</html>" not in html[-4096:].lower():
        raise RuntimeError(f"downloaded {HTML_NAME} looks truncated ({len(html)} bytes) — not installed")
    return ver_file


def _selftest_downloaded_helper(py_bytes):
    """v6.43: run the downloaded helper with --self-test in a temp dir (no port, no threads,
    temp recordings dir) to catch import-time / basic runtime errors before swapping.
    Skipped for scripts that don't know --self-test (older than 6.43). Returns note string."""
    if b"--self-test" not in py_bytes:
        return "self-test skipped (downloaded helper predates --self-test)"
    tmp = Path(tempfile.mkdtemp(prefix="tar-update-selftest-"))
    try:
        script = tmp / "twitch-recorder-server.py"
        script.write_bytes(py_bytes)
        rec = tmp / "rec"
        rec.mkdir()
        env = dict(os.environ)
        env["TWITCH_RECORDER_REC_DIR"] = str(rec)
        env["TWITCH_RECORDER_PORT"] = "0"
        env["TWITCH_RECORDER_KEEP_AWAKE"] = "0"
        try:
            r = subprocess.run([sys.executable, str(script), "--self-test"], cwd=str(tmp), env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=UPDATE_SELFTEST_TIMEOUT,
                               creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0))
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"downloaded helper self-test hung (> {UPDATE_SELFTEST_TIMEOUT}s) — not installed")
        out = (r.stdout or b"").decode("utf-8", errors="replace")
        err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        if r.returncode != 0 or '"selfTest": "ok"' not in out:
            tail = (err or out).strip().splitlines()[-1:] or ["no output"]
            raise RuntimeError(f"downloaded helper failed its self-test (exit {r.returncode}: {tail[0][:200]}) — not installed")
        return "self-test ok"
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


def _backup_install_files():
    """v6.43: copy the current UPDATE_FILES into INSTALL_DIR/.update-backup/ (replaces older backup)."""
    bdir = INSTALL_DIR / UPDATE_BACKUP_DIRNAME
    bdir.mkdir(parents=True, exist_ok=True)
    saved = []
    for name in UPDATE_FILES:
        src = INSTALL_DIR / name
        if src.is_file():
            atomic_write_bytes(bdir / name, src.read_bytes())
            saved.append(name)
    try:
        (bdir / "BACKUP-INFO.txt").write_text(
            f"Files from helper v{local_helper_version()} saved {time.strftime('%Y-%m-%d %H:%M:%S')} before an update.\n"
            "To roll back: stop the helper, copy these files back next to twitch-recorder-server.py, start it again.\n",
            encoding="utf-8")
    except OSError:
        pass
    return bdir, saved


def apply_update(source="manual"):
    """Download UPDATE_FILES into INSTALL_DIR. Never self-kill; always needsRestart.

    v6.43: download everything first, validate (compile + versions agree + html complete +
    --self-test of the new helper), back up the current files, then swap them in; if a write
    fails midway the backup is restored so the install dir never holds a half update.
    """
    local = local_helper_version()
    try:
        remote = fetch_remote_version()
    except Exception as e:
        return {
            "ok": False,
            "error": f"version check failed: {e}",
            "localVersion": local,
            "restarted": False,
            "needsRestart": False,
        }

    written = []
    blobs = {}
    try:
        for name in UPDATE_FILES:
            url = UPDATE_RAW_BASE + name
            data = fetch_url_bytes(url, timeout=45)
            if not data:
                raise RuntimeError(f"empty download: {name}")
            blobs[name] = data
        new_ver = _validate_update_blobs(blobs, remote)
        st_note = _selftest_downloaded_helper(blobs["twitch-recorder-server.py"])
        log(f"update v{new_ver} ({source}) downloaded + checked: {st_note}")
    except Exception as e:
        log(f"update ({source}) not installed: {e}")
        return {
            "ok": False,
            "error": str(e),
            "localVersion": local,
            "remoteVersion": remote,
            "written": written,
            "restarted": False,
            "needsRestart": False,
        }
    try:
        bdir, saved = _backup_install_files()
    except Exception as e:
        log(f"update ({source}) not installed — could not back up current files: {e}")
        return {"ok": False, "error": f"could not back up current files: {e}", "localVersion": local,
                "remoteVersion": remote, "written": [], "restarted": False, "needsRestart": False}
    try:
        # VERSION last: pendingRestart only flips once every other file is in place
        for name in sorted(UPDATE_FILES, key=lambda n: n == "VERSION"):
            dest = INSTALL_DIR / name
            atomic_write_bytes(dest, blobs[name])
            written.append(name)
            log(f"update wrote {dest} ({len(blobs[name])} bytes)")
    except Exception as e:
        restored = []
        for name in written:
            try:
                b = bdir / name
                if b.is_file():
                    atomic_write_bytes(INSTALL_DIR / name, b.read_bytes())
                    restored.append(name)
            except Exception as e2:
                log(f"update rollback of {name} failed: {e2}")
        log(f"update ({source}) write failed ({e}); restored {restored or 'nothing'} from {bdir}")
        return {
            "ok": False,
            "error": f"{e} (previous files restored)",
            "localVersion": local,
            "remoteVersion": remote,
            "written": [],
            "restarted": False,
            "needsRestart": False,
        }

    with LOCK:
        reap_locked()
        active = list(ACTIVE.keys())

    note = (
        "Update files written. Helper auto-restarts when no recordings / Music jobs "
        "are active, or use Restart helper / POST /api/restart. Then hard-refresh "
        "the recorder page."
    )
    if active:
        note = (
            f"Update written while recording {', '.join(active)} — process left running. "
            "Helper will auto-restart when recordings finish (and no Music jobs), "
            "or use Restart helper / POST /api/restart. Then hard-refresh the page."
        )
    _auto_update_set(lastAppliedAt=time.time(), lastAppliedVersion=remote, lastAppliedSource=source,
                     backupDir=str(bdir))
    STATUS_FILE_WAKE.set()

    return {
        "ok": True,
        "version": remote,
        "localVersion": local,
        "remoteVersion": remote,
        "written": written,
        "installDir": str(INSTALL_DIR),
        "backupDir": str(bdir),
        "active": active,
        "restarted": False,
        "needsRestart": True,
        "note": note,
    }


# ─── v6.43: HELPER UPDATES ITSELF UNATTENDED WHEN IDLE ───────────────
# Every release used to need someone to click Check for updates → Install update in the page,
# so a helper on a computer nobody looked at (Silver Bullet sat on 6.36) missed fixes. Now a
# background loop checks GitHub ~2 min after start and then every ~3 h; when a newer version is
# out and the "autoUpdate" pref is on (default) it waits until nothing is busy (see
# update_busy_reason) plus a few idle minutes, installs it through apply_update (validated,
# backed up) and restarts through the v6.32 deferred-safe restart. Never during a recording,
# a stream-restart grace wait, Demucs/split jobs, Drive copies or seamless joins.
def _env_secs(name, default, lo):
    try:
        return max(lo, float(os.environ.get(name) or default))
    except ValueError:
        return float(default)


AUTO_UPDATE_ENV_OFF = (os.environ.get("TWITCH_RECORDER_AUTO_UPDATE") or "").strip().lower() in (
    "0", "off", "false", "no")
AUTO_UPDATE_FIRST_SECS = _env_secs("TWITCH_RECORDER_AUTO_UPDATE_FIRST_SECS", 120, 1)
AUTO_UPDATE_EVERY_SECS = _env_secs("TWITCH_RECORDER_AUTO_UPDATE_SECS", 3 * 3600, 60)
AUTO_UPDATE_RETRY_SECS = _env_secs("TWITCH_RECORDER_AUTO_UPDATE_RETRY_SECS", 1800, 30)
AUTO_UPDATE_BUSY_SECS = _env_secs("TWITCH_RECORDER_AUTO_UPDATE_BUSY_SECS", 60, 1)
AUTO_UPDATE_IDLE_SECS = _env_secs("TWITCH_RECORDER_AUTO_UPDATE_IDLE_SECS", 300, 0)
AUTO_UPDATE_WAKE = threading.Event()
AUTO_UPDATE_LOCK = threading.Lock()
AUTO_UPDATE = {
    "state": "starting", "lastCheckAt": 0.0, "lastCheckSource": None, "lastResult": None,
    "lastError": None, "remoteVersion": None, "updateAvailable": False, "waitingFor": None,
    "nextCheckAt": 0.0, "lastAppliedAt": 0.0, "lastAppliedVersion": None, "lastAppliedSource": None,
    "backupDir": None, "lastBusyAt": 0.0, "checkNow": False,
}
UPLOADS_INFLIGHT = [0]  # v6.43: POST /api/music-only-upload bodies being received


def _auto_update_set(**kw):
    with AUTO_UPDATE_LOCK:
        AUTO_UPDATE.update(kw)


def auto_update_enabled():
    if AUTO_UPDATE_ENV_OFF:
        return False
    with PIPELINE_LOCK:
        return PIPELINE.get("autoUpdate", True) is not False


def update_busy_reason():
    """Plain-English reason the helper must NOT install/restart right now, or None when idle.

    Reuses the v6.40 keep-awake busy check (recording incl. the v6.42 restart-grace wait — those
    stay in ACTIVE — Demucs/split queue, Drive copy in flight, seamless joins) and adds originals
    queued for Drive and uploads still arriving."""
    reason = _keep_awake_busy_reason()
    if not reason:
        with PIPELINE_LOCK:
            if any((d.get("status") in ("queued", "sending"))
                   for d in (PIPELINE.get("originals") or {}).values()):
                reason = "originals queued for Google Drive"
            elif any(d.get("status") == "sending" for d in PIPELINE["deliveries"].values()):
                reason = "sending to Google Drive"
    if not reason and UPLOADS_INFLIGHT[0] > 0:
        reason = "receiving an upload"
    if not reason and SL_UPGRADING.is_set():  # v6.55
        reason = "updating streamlink"
    if reason:
        _auto_update_set(lastBusyAt=time.time())
    return reason


def update_idle_settled(now=None):
    """True once nothing has been busy for AUTO_UPDATE_IDLE_SECS (after-stream steps can queue)."""
    now = time.time() if now is None else now
    with AUTO_UPDATE_LOCK:
        last = float(AUTO_UPDATE.get("lastBusyAt") or 0.0)
    with KEEP_AWAKE_LOCK:  # keep-awake loop samples busy every ~15 s even when we don't
        last = max(last, float(KEEP_AWAKE.get("lastBusyAt") or 0.0))
    return (now - last) >= AUTO_UPDATE_IDLE_SECS


def note_update_check(res, source):
    """Remember the outcome of any GitHub check (manual or unattended) for health/status."""
    now = time.time()
    ok = bool(res.get("ok"))
    with AUTO_UPDATE_LOCK:
        AUTO_UPDATE["lastCheckAt"] = now
        AUTO_UPDATE["lastCheckSource"] = source
        if ok:
            AUTO_UPDATE["remoteVersion"] = res.get("remoteVersion")
            AUTO_UPDATE["updateAvailable"] = bool(res.get("updateAvailable"))
            AUTO_UPDATE["lastResult"] = (f"v{res.get('remoteVersion')} available" if res.get("updateAvailable")
                                         else f"up to date (GitHub v{res.get('remoteVersion')})")
            AUTO_UPDATE["lastError"] = None
        else:
            AUTO_UPDATE["lastResult"] = "check failed"
            AUTO_UPDATE["lastError"] = f"update check failed: {res.get('error') or 'unknown'}"
    STATUS_FILE_WAKE.set()


def auto_update_tick(now=None):
    """One step of the unattended updater. Returns seconds until the next step."""
    now = time.time() if now is None else now
    # 1) files already on disk (manual install while busy, or our own apply) → restart when idle.
    #    Runs even with the pref off: same as the v6.32 health-path auto-restart, but works with
    #    the page closed.
    if pending_restart_needed():
        _auto_update_set(state="restart-pending")
        maybe_auto_restart_idle()
        return AUTO_UPDATE_BUSY_SECS
    if not auto_update_enabled():
        _auto_update_set(state="off", waitingFor=None, nextCheckAt=0.0)
        return AUTO_UPDATE_EVERY_SECS
    with AUTO_UPDATE_LOCK:
        last = float(AUTO_UPDATE.get("lastCheckAt") or 0.0)
        had_err = bool(AUTO_UPDATE.get("lastError")) and AUTO_UPDATE.get("lastResult") == "check failed"
        force = bool(AUTO_UPDATE.get("checkNow"))
        AUTO_UPDATE["checkNow"] = False
        available = bool(AUTO_UPDATE.get("updateAvailable"))
        remote = AUTO_UPDATE.get("remoteVersion")
    due_every = AUTO_UPDATE_RETRY_SECS if had_err else AUTO_UPDATE_EVERY_SECS
    if force or not last or now - last >= due_every or (
            available and _parse_version_tuple(remote) <= _parse_version_tuple(local_helper_version())):
        _auto_update_set(state="checking")
        res = check_update()
        note_update_check(res, "auto")
        if not res.get("ok"):
            log(f"auto-update: GitHub check failed ({res.get('error')}); retry in {int(AUTO_UPDATE_RETRY_SECS // 60)} min")
            _auto_update_set(state="error", waitingFor=None, nextCheckAt=now + AUTO_UPDATE_RETRY_SECS)
            return AUTO_UPDATE_RETRY_SECS
        available = bool(res.get("updateAvailable"))
        remote = res.get("remoteVersion")
        last = now
        if not available:
            log(f"auto-update: up to date (v{local_helper_version()}, GitHub v{remote})")
    if not available:
        _auto_update_set(state="up-to-date", waitingFor=None, nextCheckAt=last + AUTO_UPDATE_EVERY_SECS)
        return max(1.0, last + AUTO_UPDATE_EVERY_SECS - now)
    busy = update_busy_reason()
    if busy or not update_idle_settled(now):
        why = busy or "just finished work — waiting a few idle minutes"
        with AUTO_UPDATE_LOCK:
            prev = AUTO_UPDATE.get("waitingFor")
        if prev != why:
            log(f"auto-update: v{remote} available — waiting until idle ({why})")
        _auto_update_set(state="waiting-idle", waitingFor=why, nextCheckAt=last + AUTO_UPDATE_EVERY_SECS)
        return AUTO_UPDATE_BUSY_SECS
    log(f"auto-update: installing v{remote} (running v{HELPER_VERSION}, helper idle)")
    _auto_update_set(state="installing", waitingFor=None)
    res = apply_update(source="auto")
    if not res.get("ok"):
        err = res.get("error") or "unknown"
        log(f"auto-update: install of v{remote} failed — {err}; helper keeps running v{HELPER_VERSION}; "
            f"retry in {int(AUTO_UPDATE_RETRY_SECS // 60)} min")
        _auto_update_set(state="error", lastError=f"install failed: {err}", lastResult="install failed",
                         nextCheckAt=now + AUTO_UPDATE_RETRY_SECS)
        STATUS_FILE_WAKE.set()
        return AUTO_UPDATE_RETRY_SECS
    log(f"auto-update: v{remote} installed into {INSTALL_DIR} (backup in {res.get('backupDir')}); restarting when idle")
    _auto_update_set(state="restart-pending", lastResult=f"v{remote} installed", lastError=None,
                     updateAvailable=False)
    maybe_auto_restart_idle()
    return AUTO_UPDATE_BUSY_SECS


def auto_update_loop():
    AUTO_UPDATE_WAKE.wait(AUTO_UPDATE_FIRST_SECS)
    AUTO_UPDATE_WAKE.clear()
    while True:
        wait = AUTO_UPDATE_EVERY_SECS
        try:
            wait = auto_update_tick()
        except Exception as e:
            log(f"auto-update loop error: {e}")
            _auto_update_set(state="error", lastError=f"auto-update error: {e}")
            wait = AUTO_UPDATE_RETRY_SECS
        AUTO_UPDATE_WAKE.wait(max(1.0, float(wait)))
        AUTO_UPDATE_WAKE.clear()


def auto_update_snapshot():
    with AUTO_UPDATE_LOCK:
        a = dict(AUTO_UPDATE)
    pend = pending_restart_needed()
    return {
        "enabled": auto_update_enabled(),
        "envDisabled": AUTO_UPDATE_ENV_OFF,
        "state": ("restart-pending" if pend and a.get("state") not in ("restarting",) else a.get("state")),
        "lastCheckAt": a.get("lastCheckAt") or None,
        "lastCheckSource": a.get("lastCheckSource"),
        "lastResult": a.get("lastResult"),
        "lastError": a.get("lastError"),
        "remoteVersion": a.get("remoteVersion"),
        "updateAvailable": bool(a.get("updateAvailable")),
        "waitingFor": a.get("waitingFor"),
        "nextCheckAt": a.get("nextCheckAt") or None,
        "lastAppliedAt": a.get("lastAppliedAt") or None,
        "lastAppliedVersion": a.get("lastAppliedVersion"),
        "backupDir": a.get("backupDir"),
        "pendingRestart": pend,
        "runningVersion": HELPER_VERSION,
        "diskVersion": disk_file_version(),
        "intervalSecs": int(AUTO_UPDATE_EVERY_SECS),
        "idleSecs": int(AUTO_UPDATE_IDLE_SECS),
    }


def api_update_auto(body):
    """POST /api/update/auto {enabled?: bool, checkNow?: bool} → snapshot."""
    body = body if isinstance(body, dict) else {}
    if "enabled" in body:
        on = bool(body.get("enabled"))
        with PIPELINE_LOCK:
            PIPELINE["autoUpdate"] = on
        _pipeline_save()
        log(f"auto-update pref: {'on' if on else 'off'}")
        STATUS_FILE_WAKE.set()
    if body.get("checkNow"):
        _auto_update_set(checkNow=True)
    AUTO_UPDATE_WAKE.set()
    return {"ok": True, "autoUpdate": auto_update_snapshot()}, 200


import re  # v6.55

# ─── v6.55: KEEP STREAMLINK CURRENT ──────────────────────────────────
# Twitch changes its site/API every few months and older streamlink releases then stop finding
# the stream ("No playable streams found", 403s, integrity errors) — every recording attempt fails
# even though the stream is live, and nothing on a computer nobody looks at says why. The helper
# now checks the installed streamlink against PyPI (a few minutes after start, then every ~12 h)
# and, when it was installed with pip (venv, user or system site), upgrades it to the newest
# release that this computer's Python can run — only when nothing is recording / processing /
# sending, same idle rules as the v6.43 helper auto-update. After the upgrade it checks that
# `streamlink --version` reports the new version, that the Twitch plugin still handles twitch.tv
# URLs and that every command-line option the recorder uses still exists; if not, it puts the
# previous version back. Homebrew / pipx / Windows-installer copies are never touched — the
# helper just says how to update them. When the newest streamlink needs a newer Python than the
# computer has (e.g. macOS Command Line Tools Python 3.9 can't run streamlink 8), that is said
# too. A recording that starts while an upgrade is running waits for it (≤ 4 min).
SL_UPDATE_ENV_OFF = (os.environ.get("TWITCH_RECORDER_STREAMLINK_UPDATE") or "").strip().lower() in (
    "0", "off", "false", "no")
SL_UPDATE_FIRST_SECS = _env_secs("TWITCH_RECORDER_STREAMLINK_UPDATE_FIRST_SECS", 240, 1)
SL_UPDATE_EVERY_SECS = _env_secs("TWITCH_RECORDER_STREAMLINK_UPDATE_SECS", 12 * 3600, 60)
SL_UPDATE_RETRY_SECS = _env_secs("TWITCH_RECORDER_STREAMLINK_UPDATE_RETRY_SECS", 3600, 5)
SL_UPDATE_MAX_FAILS = 3  # per target version; then wait for a newer release (or a manual "check now")
SL_PYPI_URL = os.environ.get("TWITCH_RECORDER_STREAMLINK_PYPI_URL") or "https://pypi.org/pypi/streamlink/json"
SL_REQUIRED_OPTS = ("--output", "--force", "--ffmpeg-fout", "--json", "--loglevel")  # build_cmd + live probe
SL_UPDATE_WAKE = threading.Event()
SL_UPDATE_LOCK = threading.Lock()
SL_UPGRADING = threading.Event()  # set while pip is replacing streamlink
SL_UPDATE = {
    "state": "starting", "installed": None, "kind": None, "python": None, "pythonVersion": None,
    "latest": None, "latestCompatible": None, "latestRequiresPython": None, "installedReleasedAt": None,
    "lastCheckAt": 0.0, "lastResult": None, "lastError": None, "hint": None, "waitingFor": None,
    "nextCheckAt": 0.0, "lastUpgradeAt": 0.0, "lastUpgradeFrom": None, "lastUpgradeTo": None,
    "fails": {}, "checkNow": False,
}


def _sl_set(**kw):
    with SL_UPDATE_LOCK:
        SL_UPDATE.update(kw)


def streamlink_update_enabled():
    if SL_UPDATE_ENV_OFF:
        return False
    with PIPELINE_LOCK:
        return PIPELINE.get("streamlinkUpdate", True) is not False


def _sl_run(cmd, timeout=60, extra_env=None):
    """subprocess.run, UTF-8 safe (v6.51), no console window on Windows. Returns (rc, out)."""
    kw = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
          "timeout": timeout, "stdin": subprocess.DEVNULL}
    env = os.environ.copy()
    env["PYTHONWARNINGS"] = "ignore"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    if extra_env:
        env.update(extra_env)
    kw["env"] = env
    if os.name == "nt" and getattr(subprocess, "CREATE_NO_WINDOW", 0):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run(cmd, **kw)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _sl_vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))) or (0,)


def streamlink_cli_version(sl=None):
    sl = sl or which_streamlink()
    if not sl:
        return None
    rc, out = _sl_run([sl, "--version"], timeout=45)
    m = re.search(r"streamlink\s+v?(\d+(?:\.\d+)+)", out or "", re.I)
    return m.group(1) if (rc == 0 and m) else None


def _sl_python_candidates(sl):
    cands = []
    try:
        p = Path(sl)
        if os.name == "nt":
            cands += [p.parent / "python.exe", p.parent.parent / "python.exe"]
        else:
            with open(p, "rb") as fh:
                first = fh.readline(400)
            if first.startswith(b"#!"):
                parts = first[2:].decode("utf-8", "replace").strip().split()
                if parts:
                    if os.path.basename(parts[0]) == "env" and len(parts) > 1:
                        w = shutil.which(parts[1])
                        if w:
                            cands.append(Path(w))
                    else:
                        cands.append(Path(parts[0]))
    except OSError:
        pass
    if sys.executable:
        cands.append(Path(sys.executable))
    seen, out = set(), []
    for c in cands:
        try:
            k = str(c)
            if k not in seen and c.is_file():
                seen.add(k)
                out.append(k)
        except OSError:
            continue
    return out


_SL_PROBE = ("import sys,site\nimport streamlink\n"
             "print('V='+streamlink.__version__)\nprint('F='+streamlink.__file__)\n"
             "print('P=%d.%d.%d'%tuple(sys.version_info[:3]))\n"
             "print('U='+(site.getusersitepackages() if hasattr(site,'getusersitepackages') else ''))\n")


def _sl_manual_hint(kind):
    return {
        "homebrew": "installed with Homebrew — update it with: brew upgrade streamlink",
        "pipx": "installed with pipx — update it with: pipx upgrade streamlink",
        "installer": "installed with the Streamlink Windows installer — install the newest one from "
                     "https://github.com/streamlink/windows-builds/releases (or: winget upgrade streamlink)",
        "externally-managed": "this Python refuses pip installs (externally managed) — update streamlink the way it "
                              "was installed (brew / pipx / your package manager)",
    }.get(kind, "streamlink wasn't installed with pip for a Python the helper can find — update it the way it "
                "was installed (e.g. pip install --upgrade streamlink)")


def streamlink_inspect(sl=None):
    """{installed, kind: pip|pip-user|homebrew|pipx|installer|other|missing, python, pythonVersion, hint}."""
    sl = sl or which_streamlink()
    if not sl:
        return {"installed": None, "kind": "missing", "python": None, "pythonVersion": None,
                "hint": "streamlink not found — pip install streamlink"}
    ver = streamlink_cli_version(sl)
    try:
        real = os.path.realpath(sl)
    except OSError:
        real = sl
    low = real.replace("\\", "/").lower()
    kind = None
    if "/cellar/" in low or "/homebrew/" in low or "linuxbrew" in low:
        kind = "homebrew"
    elif "/pipx/" in low:
        kind = "pipx"
    elif os.name == "nt" and "program files" in low and "/streamlink/" in low:
        kind = "installer"
    info = {"installed": ver, "kind": kind or "other", "python": None, "pythonVersion": None, "hint": None}
    if kind is None and ver:
        for py in _sl_python_candidates(sl):
            rc, out = _sl_run([py, "-c", _SL_PROBE], timeout=45)
            if rc != 0:
                continue
            kv = dict(ln.split("=", 1) for ln in (out or "").splitlines() if len(ln) > 2 and ln[1] == "=")
            if kv.get("V") != ver:
                continue
            rc2, _o2 = _sl_run([py, "-m", "pip", "--version"], timeout=60)
            if rc2 != 0:
                continue
            mod = os.path.realpath(kv.get("F") or "").replace("\\", "/").lower()
            usr = os.path.realpath(kv.get("U") or "").replace("\\", "/").lower() if kv.get("U") else ""
            info.update(python=py, pythonVersion=kv.get("P"),
                        kind="pip-user" if (usr and mod.startswith(usr.rstrip("/") + "/")) else "pip")
            break
    if info["kind"] not in ("pip", "pip-user"):
        info["hint"] = _sl_manual_hint(info["kind"])
    return info


def _py_satisfies(spec, pyver):
    """Tiny PEP 440 requires_python check (>=, >, <=, <, ==, !=, ~=, with .* wildcards)."""
    if not spec:
        return True
    pv = _sl_vtuple(pyver)
    for clause in str(spec).split(","):
        clause = clause.strip()
        if not clause:
            continue
        m = re.match(r"^(~=|==|!=|>=|<=|>|<)\s*([0-9][0-9.]*)(\.\*)?$", clause)
        if not m:
            continue  # unknown syntax — let pip decide
        op, v, wild = m.group(1), _sl_vtuple(m.group(2)), bool(m.group(3))
        n = len(v)
        a = pv[:n] + (0,) * max(0, n - len(pv[:n]))
        if op == "==":
            ok = a == v if wild else (pv + (0,) * 3)[:max(n, 3)] == (v + (0,) * 3)[:max(n, 3)]
        elif op == "!=":
            ok = a != v if wild else (pv + (0,) * 3)[:max(n, 3)] != (v + (0,) * 3)[:max(n, 3)]
        elif op == ">=":
            ok = a >= v
        elif op == ">":
            ok = a > v
        elif op == "<=":
            ok = a <= v
        elif op == "<":
            ok = a < v
        else:  # ~=
            ok = a >= v and pv[:max(1, n - 1)] == v[:max(1, n - 1)]
        if not ok:
            return False
    return True


def streamlink_latest(pyver=None, doc=None):
    """From PyPI JSON → {latest, latestRequiresPython, latestCompatible, releasedAt:{ver: iso}}."""
    if doc is None:
        doc = json.loads(fetch_url_text(SL_PYPI_URL, timeout=20))
    rels = doc.get("releases") if isinstance(doc, dict) else None
    if not isinstance(rels, dict):
        raise RuntimeError("PyPI: unexpected response")
    best = best_c = None
    req_of, when = {}, {}
    for ver, files in rels.items():
        if not re.fullmatch(r"\d+(?:\.\d+)*", str(ver)) or not isinstance(files, list):
            continue  # pre-releases / dev builds
        good = [f for f in files if isinstance(f, dict) and not f.get("yanked")]
        if not good:
            continue
        req = good[0].get("requires_python") or ""
        req_of[ver] = req
        when[ver] = str(good[0].get("upload_time") or good[0].get("upload_time_iso_8601") or "")[:10] or None
        if best is None or _sl_vtuple(ver) > _sl_vtuple(best):
            best = ver
        if (pyver is None or _py_satisfies(req, pyver)) and (best_c is None or _sl_vtuple(ver) > _sl_vtuple(best_c)):
            best_c = ver
    if not best:
        raise RuntimeError("PyPI: no streamlink releases listed")
    return {"latest": best, "latestRequiresPython": req_of.get(best) or None,
            "latestCompatible": best_c, "releasedAt": when}


def _sl_verify(expected, sl=None):
    """New streamlink works: version matches, Twitch plugin handles twitch.tv, our options exist."""
    sl = sl or which_streamlink()
    if not sl:
        return "streamlink disappeared after the upgrade"
    got = streamlink_cli_version(sl)
    if got != expected:
        return f"streamlink --version says {got or 'nothing'} (expected {expected})"
    rc, _o = _sl_run([sl, "--can-handle-url", "https://twitch.tv/twitch"], timeout=60)
    if rc != 0:
        return "the new streamlink's Twitch plugin doesn't handle twitch.tv URLs"
    rc, out = _sl_run([sl, "--help"], timeout=60)
    missing = [o for o in SL_REQUIRED_OPTS if o not in (out or "")]
    if rc != 0 or missing:
        return "the new streamlink lacks options the recorder uses: " + ", ".join(missing or ["--help failed"])
    return None


def _sl_pip_install(info, version):
    cmd = [info["python"], "-m", "pip", "install", "--upgrade", "--no-input", "--disable-pip-version-check",
           f"streamlink=={version}"]
    if info.get("kind") == "pip-user":
        cmd.insert(5, "--user")
    return _sl_run(cmd, timeout=900)


def streamlink_upgrade(info, target):
    """pip-upgrade to target, verify, roll back on failure. Returns (ok, message)."""
    old = info.get("installed")
    log(f"streamlink update: upgrading {old} → {target} with {info.get('python')} ({info.get('kind')})")
    SL_UPGRADING.set()
    try:
        rc, out = _sl_pip_install(info, target)
        if rc != 0:
            tail = _short(" ".join((out or "").split()[-60:]), 300)
            if "externally-managed" in (out or "").lower():
                return False, "externally-managed:" + tail
            if streamlink_cli_version() == old:
                return False, f"pip install failed (exit {rc}): {tail}"
            bad = f"pip install failed (exit {rc}) midway: {tail}"
        else:
            bad = _sl_verify(target)
            if not bad:
                return True, f"streamlink {old} → {target}"
        log(f"streamlink update: {bad} — putting {old} back")
        rrc, rout = _sl_pip_install(info, old) if old else (1, "no previous version known")
        back = streamlink_cli_version()
        if back == old:
            return False, f"{bad}; kept streamlink {old}"
        return False, (f"{bad}; ROLLBACK to {old} failed (exit {rrc}: {_short(rout, 160)}) — "
                       f"streamlink is now {back or 'not working'}; run: pip install streamlink=={old}")
    finally:
        SL_UPGRADING.clear()
        STATUS_FILE_WAKE.set()


def wait_streamlink_upgrade(max_secs=240):
    """A recording that starts while pip is swapping streamlink waits for it (bounded)."""
    if not SL_UPGRADING.is_set():
        return
    log("recording start waits for the streamlink upgrade to finish…")
    end = time.time() + max_secs
    while SL_UPGRADING.is_set() and time.time() < end:
        time.sleep(0.5)


# ---- v6.59: the helper's own streamlink on a newer Python -------------------------------------
# Twitch breaks old streamlink releases every few months, and the newest streamlink needs Python
# 3.10+. A Mac whose only Python is the Command Line Tools 3.9 (Silver Bullet) was stuck on
# streamlink 7.6.0 forever: v6.55 could only say "install a newer Python". Now, when that happens
# and "Keep streamlink up to date when idle" is on, the helper builds its own copy, once, when
# idle: a virtual environment with the newest streamlink, on a newer Python already on the computer
# (Homebrew, python.org, pyenv, Windows py launcher …) or, if there is none, on a standalone Python
# fetched with uv (Astral's installer: one checksum-verified download from GitHub, no admin, nothing
# installed system-wide). It lives in ~/.twitch-recorder/streamlink (Windows:
# %LOCALAPPDATA%\TwitchRecorder\streamlink). It is only switched on after the same checks as a v6.55
# upgrade (version, Twitch plugin, every option the recorder uses); from then on the normal v6.55
# pip updater keeps it current. The helper itself keeps running on the same Python as before, so
# a Mac's Full Disk Access for Drive is not affected. If the copy ever stops working the helper
# switches back to the system streamlink by itself.
SL_PRIVATE_ENV_OFF = (os.environ.get("TWITCH_RECORDER_PRIVATE_STREAMLINK") or "").strip().lower() in (
    "0", "off", "false", "no")
SL_PRIVATE_PY = (os.environ.get("TWITCH_RECORDER_PRIVATE_STREAMLINK_PYTHON") or "3.12").strip()
SL_PRIVATE_MIN_FREE = 800 * 1024 * 1024
SL_PRIVATE_KEY = "private-build"  # key in SL_UPDATE["fails"]
_SL_PRIV_CACHE = {"mtime": None, "bin": None}


def sl_private_root():
    d = (os.environ.get("TWITCH_RECORDER_PRIVATE_STREAMLINK_DIR") or "").strip()
    if d:
        return Path(d)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "TwitchRecorder" / "streamlink"
    return Path.home() / ".twitch-recorder" / "streamlink"


def _sl_venv_paths(venv):
    venv = Path(venv)
    if os.name == "nt":
        return venv / "Scripts" / "python.exe", venv / "Scripts" / "streamlink.exe"
    return venv / "bin" / "python", venv / "bin" / "streamlink"


def sl_private_info():
    """current.json of the private copy, or None. {dir, version, python, pythonVersion, how, at}."""
    cur = sl_private_root() / "current.json"
    try:
        doc = json.loads(cur.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) and doc.get("dir") else None
    except (OSError, ValueError):
        return None


def sl_private_bin():
    """Path of the verified private streamlink when it is switched on, else None (cheap: mtime cache)."""
    if SL_PRIVATE_ENV_OFF:
        return None
    cur = sl_private_root() / "current.json"
    try:
        mt = cur.stat().st_mtime
    except OSError:
        _SL_PRIV_CACHE.update(mtime=None, bin=None)
        return None
    if _SL_PRIV_CACHE.get("mtime") != mt:
        info = sl_private_info()
        b = None
        if info:
            _py, sl = _sl_venv_paths(sl_private_root() / str(info["dir"]))
            b = str(sl)
        _SL_PRIV_CACHE.update(mtime=mt, bin=b)
    b = _SL_PRIV_CACHE.get("bin")
    return b if (b and os.path.isfile(b)) else None


def sl_private_disable(reason):
    """Stop using the private copy (kept on disk as current.json.off for a look later)."""
    cur = sl_private_root() / "current.json"
    try:
        if cur.is_file():
            os.replace(str(cur), str(cur.with_name("current.json.off")))
            log(f"streamlink private copy switched off ({reason}) — using the system streamlink again")
    except OSError as e:
        log(f"streamlink private copy: could not switch off: {e}")
    _SL_PRIV_CACHE.update(mtime=None, bin=None)


def _sl_python_version(py):
    rc, out = _sl_run([py, "-c", "import sys,venv,ensurepip;print('PYV=%d.%d.%d'%tuple(sys.version_info[:3]))"],
                      timeout=45)
    m = re.search(r"PYV=(\d+\.\d+\.\d+)", out or "")
    return m.group(1) if (rc == 0 and m) else None


def _sl_python_search_list():
    import glob as _glob
    names = [f"python3.{m}" for m in range(20, 9, -1)] + ["python3"]
    cands = []
    for n in names:
        w = shutil.which(n)
        if w:
            cands.append(w)
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA") or ""
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        pats = [os.path.join(la, "Programs", "Python", "Python3*", "python.exe"),
                os.path.join(pf, "Python3*", "python.exe")]
        for pat in pats:
            cands += sorted(_glob.glob(pat), reverse=True)
        w = shutil.which("python") or shutil.which("python.exe")
        if w:
            cands.append(w)
    else:
        home = str(Path.home())
        dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/Library/Frameworks/Python.framework/Versions/Current/bin"]
        dirs += sorted(_glob.glob("/Library/Frameworks/Python.framework/Versions/3.*/bin"), reverse=True)
        dirs += sorted(_glob.glob(os.path.join(home, ".pyenv", "versions", "3.*", "bin")), reverse=True)
        for d in dirs:
            for n in names:
                c = os.path.join(d, n)
                if os.path.isfile(c):
                    cands.append(c)
    seen, out = set(), []
    for c in cands:
        try:
            real = os.path.realpath(c)
        except OSError:
            real = c
        low = real.replace("\\", "/").lower()
        if "/windowsapps/" in low:  # Microsoft Store alias stub — may pop the Store instead of running
            continue
        if real in seen or os.path.realpath(sys.executable or "") == real:
            continue
        seen.add(real)
        out.append(c)
    return out


def sl_find_newer_python(spec):
    """Newest Python on this computer that satisfies spec and has venv + ensurepip: (path, ver) or (None, None)."""
    best = (None, None)
    for c in _sl_python_search_list():
        v = _sl_python_version(c)
        if v and _py_satisfies(spec, v) and (best[1] is None or _sl_vtuple(v) > _sl_vtuple(best[1])):
            best = (c, v)
    return best


def _uv_asset(sysname=None, machine=None):
    import platform as _plat
    sysname = (sysname or _plat.system()).lower()
    machine = (machine or _plat.machine()).lower()
    arch = "aarch64" if machine in ("arm64", "aarch64", "armv8", "armv8l") else "x86_64"
    if sysname == "darwin":
        return f"uv-{arch}-apple-darwin.tar.gz"
    if sysname.startswith("win"):
        return f"uv-{arch}-pc-windows-msvc.zip"
    if sysname == "linux":
        return f"uv-{arch}-unknown-linux-gnu.tar.gz"
    return None


def _uv_extract(data, asset, dest_dir):
    """Write the uv binary from a release archive into dest_dir; returns its path."""
    import io as _io_uv
    want = "uv.exe" if asset.endswith(".zip") else "uv"
    blob = None
    if asset.endswith(".zip"):
        import zipfile as _zip
        with _zip.ZipFile(_io_uv.BytesIO(data)) as z:
            for n in z.namelist():
                if n.replace("\\", "/").rsplit("/", 1)[-1] == want:
                    blob = z.read(n)
                    break
    else:
        import tarfile as _tar
        with _tar.open(fileobj=_io_uv.BytesIO(data), mode="r:gz") as t:
            for m in t.getmembers():
                if m.isfile() and m.name.rsplit("/", 1)[-1] == want:
                    blob = t.extractfile(m).read()
                    break
    if not blob:
        raise RuntimeError(f"{want} not found in {asset}")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / want
    tmp = dest_dir / (want + ".partial")
    tmp.write_bytes(blob)
    if os.name != "nt":
        os.chmod(str(tmp), 0o755)
    os.replace(str(tmp), str(out))
    return str(out)


def sl_get_uv(root, allow_path=True):
    """uv already on PATH or already fetched, else download the release for this computer (sha256-checked)."""
    import hashlib as _hl
    if allow_path:
        w = shutil.which("uv") or shutil.which("uv.exe")
        if w:
            return w
    mine = Path(root) / "uv" / ("uv.exe" if os.name == "nt" else "uv")
    if mine.is_file():
        rc, _o = _sl_run([str(mine), "--version"], timeout=30)
        if rc == 0:
            return str(mine)
    asset = _uv_asset()
    if not asset:
        raise RuntimeError("no uv download for this kind of computer")
    base = (os.environ.get("TWITCH_RECORDER_UV_BASE_URL")
            or "https://github.com/astral-sh/uv/releases/latest/download/").rstrip("/") + "/"
    want = fetch_url_text(base + asset + ".sha256", timeout=30).strip().split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", want or ""):
        raise RuntimeError("uv checksum file looked wrong")
    data = fetch_url_bytes(base + asset, timeout=600)
    got = _hl.sha256(data).hexdigest()
    if got != want:
        raise RuntimeError(f"uv download checksum mismatch ({got[:12]}… ≠ {want[:12]}…) — not used")
    path = _uv_extract(data, asset, mine.parent)
    rc, out = _sl_run([path, "--version"], timeout=30)
    if rc != 0:
        raise RuntimeError(f"downloaded uv doesn't run: {_short(out, 160)}")
    log(f"streamlink private copy: fetched {(out or '').strip()} ({asset}, sha256 ok)")
    return path


def sl_private_build(spec, version, find_python=None, get_uv=None):
    """Build a fresh venv with streamlink==version on a Python matching spec; verify; switch to it.
    Returns (ok, message). Never touches the system streamlink; the old private copy stays until the
    new one is verified."""
    find_python = find_python or sl_find_newer_python
    get_uv = get_uv or sl_get_uv
    root = sl_private_root()
    root.mkdir(parents=True, exist_ok=True)
    try:
        free = shutil.disk_usage(str(root)).free
        if free < SL_PRIVATE_MIN_FREE:
            return False, f"only {free // 1048576} MB free in {root} (needs about {SL_PRIVATE_MIN_FREE // 1048576} MB)"
    except OSError:
        pass
    name = "venv-" + time.strftime("%Y%m%d-%H%M%S")
    venv = root / name
    py_path, py_ver = find_python(spec)
    if py_path:
        how = f"Python {py_ver} at {py_path}"
        rc, out = _sl_run([py_path, "-m", "venv", str(venv)], timeout=600)
    else:
        uv = get_uv(root)
        how = f"standalone Python {SL_PRIVATE_PY} (uv)"
        uv_env = {"UV_PYTHON_INSTALL_DIR": str(root / "python"), "UV_CACHE_DIR": str(root / "cache"),
                  "UV_NO_CONFIG": "1"}
        rc, out = _sl_run([uv, "venv", "--seed", "--managed-python", "--python", SL_PRIVATE_PY, str(venv)],
                          timeout=900, extra_env=uv_env)
    vpy, vsl = _sl_venv_paths(venv)

    def _fail(msg):
        shutil.rmtree(str(venv), ignore_errors=True)
        return False, msg
    if rc != 0 or not vpy.is_file():
        return _fail(f"couldn't make a Python environment with {how} (exit {rc}): "
                     f"{_short(' '.join((out or '').split()[-40:]), 240)}")
    pv = _sl_python_version(str(vpy)) or py_ver or "?"
    if not _py_satisfies(spec, pv):
        return _fail(f"{how} gave Python {pv}, streamlink {version} needs {spec}")
    rc, out = _sl_run([str(vpy), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
                       f"streamlink=={version}"], timeout=900)
    if rc != 0:
        return _fail(f"pip install streamlink=={version} failed (exit {rc}): "
                     f"{_short(' '.join((out or '').split()[-40:]), 240)}")
    bad = _sl_verify(version, sl=str(vsl))
    if bad:
        return _fail(bad)
    prev = sl_private_info()
    doc = {"dir": name, "version": version, "python": str(vpy), "pythonVersion": pv, "how": how,
           "at": time.time(), "helper": HELPER_VERSION}
    tmp = root / "current.json.partial"
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(root / "current.json"))
    _SL_PRIV_CACHE.update(mtime=None, bin=None)
    # old copies (an earlier build) go once nothing points at them; a busy file on Windows just stays
    # earlier builds are no longer pointed at (built only when idle, so none is recording); a file that is
    # still busy on Windows just stays until the next build
    for d in root.glob("venv-*"):
        if d.name != name:
            shutil.rmtree(str(d), ignore_errors=True)
    if prev:
        log(f"streamlink private copy: replaced {prev.get('dir')} ({prev.get('version')})")
    shutil.rmtree(str(root / "cache"), ignore_errors=True)  # uv's download cache (~100 MB) isn't needed after
    return True, f"streamlink {version} on {how}"


def sl_private_snapshot():
    info = sl_private_info() if not SL_PRIVATE_ENV_OFF else None
    active = bool(sl_private_bin())
    return {"active": active, "dir": str(sl_private_root()), "envDisabled": SL_PRIVATE_ENV_OFF,
            "version": (info or {}).get("version"), "pythonVersion": (info or {}).get("pythonVersion"),
            "how": (info or {}).get("how"), "builtAt": (info or {}).get("at")}


def streamlink_update_tick(now=None):
    """One step of the streamlink updater. Returns seconds until the next step."""
    now = time.time() if now is None else now
    with SL_UPDATE_LOCK:
        last = float(SL_UPDATE.get("lastCheckAt") or 0.0)
        had_err = bool(SL_UPDATE.get("lastError")) and SL_UPDATE.get("state") == "error"
        force = bool(SL_UPDATE.get("checkNow"))
        SL_UPDATE["checkNow"] = False
    due = SL_UPDATE_RETRY_SECS if had_err else SL_UPDATE_EVERY_SECS
    if force or not last or now - last >= due:
        info = streamlink_inspect()
        if (not info.get("installed")) and sl_private_bin():  # v6.59: private copy broke → system one
            sl_private_disable("its streamlink --version failed")
            info = streamlink_inspect()
        try:
            lat = streamlink_latest(info.get("pythonVersion"))
            lerr = None
        except Exception as e:
            lat, lerr = {}, f"PyPI check failed: {e}"
        rel = (lat.get("releasedAt") or {})
        _sl_set(installed=info.get("installed"), kind=info.get("kind"), python=info.get("python"),
                pythonVersion=info.get("pythonVersion"), hint=info.get("hint"),
                latest=lat.get("latest"), latestCompatible=lat.get("latestCompatible"),
                latestRequiresPython=lat.get("latestRequiresPython"),
                installedReleasedAt=rel.get(info.get("installed") or ""),
                lastCheckAt=now, lastError=lerr)
        last = now
        if lerr:
            log(f"streamlink update: {lerr}; retry in {int(SL_UPDATE_RETRY_SECS // 60)} min")
            _sl_set(state="error", lastResult="check failed", nextCheckAt=now + SL_UPDATE_RETRY_SECS)
            STATUS_FILE_WAKE.set()
            return SL_UPDATE_RETRY_SECS
        if force:
            with SL_UPDATE_LOCK:
                SL_UPDATE["fails"] = {}
    with SL_UPDATE_LOCK:
        s = dict(SL_UPDATE)
    inst, best, kind = s.get("installed"), s.get("latestCompatible"), s.get("kind")
    nxt = last + SL_UPDATE_EVERY_SECS
    if kind == "missing" or not inst:
        # a --version that failed/timed out (busy computer at boot) is re-checked within the hour
        _sl_set(state="missing", lastResult="streamlink not found" if kind == "missing" else
                "streamlink --version failed", waitingFor=None, nextCheckAt=last + SL_UPDATE_RETRY_SECS)
        return max(1.0, last + SL_UPDATE_RETRY_SECS - now)
    if not best or _sl_vtuple(inst) >= _sl_vtuple(best):
        res = f"up to date ({inst})"
        if s.get("latest") and _sl_vtuple(s["latest"]) > _sl_vtuple(inst):
            res = (f"{inst} is the newest this computer's Python {s.get('pythonVersion') or '?'} can run — "
                   f"streamlink {s['latest']} needs Python {s.get('latestRequiresPython') or 'newer'}")
        too_old = not res.startswith("up to date")
        if too_old and not SL_PRIVATE_ENV_OFF and streamlink_update_enabled() and not sl_private_bin():
            return _sl_private_tick(s, now, nxt, res)  # v6.59
        if s.get("lastResult") != res:
            log(f"streamlink update: {res}")
            STATUS_FILE_WAKE.set()
        _sl_set(state="python-too-old" if too_old else "up-to-date",
                lastResult=res, waitingFor=None, nextCheckAt=nxt)
        return max(1.0, nxt - now)
    if kind not in ("pip", "pip-user") or not streamlink_update_enabled():
        res = f"{inst} installed, {best} available"
        st2 = "outdated-manual" if kind not in ("pip", "pip-user") else "off"
        if s.get("lastResult") != res or s.get("state") != st2:
            log(f"streamlink update: {res} — " + (s.get("hint") or "automatic streamlink updates are off"))
        _sl_set(state=st2, lastResult=res, waitingFor=None, nextCheckAt=nxt)
        STATUS_FILE_WAKE.set()
        return max(1.0, nxt - now)
    fails = int((s.get("fails") or {}).get(best) or 0)
    if fails >= SL_UPDATE_MAX_FAILS:
        _sl_set(state="error", waitingFor=None, nextCheckAt=nxt,
                lastResult=f"{inst} installed, {best} available — automatic upgrade failed {fails}×")
        return max(1.0, nxt - now)
    busy = update_busy_reason()
    if busy or not update_idle_settled(now):
        why = busy or "just finished work — waiting a few idle minutes"
        if s.get("waitingFor") != why:
            log(f"streamlink update: {best} available (have {inst}) — waiting until idle ({why})")
        _sl_set(state="waiting-idle", waitingFor=why, lastResult=f"{inst} installed, {best} available",
                nextCheckAt=nxt)
        return AUTO_UPDATE_BUSY_SECS
    _sl_set(state="upgrading", waitingFor=None)
    ok, msg = streamlink_upgrade({"python": s.get("python"), "kind": kind, "installed": inst}, best)
    if ok:
        log(f"streamlink update: {msg} (verified: Twitch plugin + recorder options)")
        _sl_set(state="up-to-date", installed=best, lastResult=f"updated {msg}", lastError=None,
                lastUpgradeAt=time.time(), lastUpgradeFrom=inst, lastUpgradeTo=best, nextCheckAt=nxt,
                installedReleasedAt=None)
        return max(1.0, nxt - now)
    if msg.startswith("externally-managed:"):
        log(f"streamlink update: pip refused ({msg[19:]}) — {_sl_manual_hint('externally-managed')}")
        _sl_set(state="outdated-manual", kind="externally-managed", hint=_sl_manual_hint("externally-managed"),
                lastResult=f"{inst} installed, {best} available", lastError=None, nextCheckAt=nxt)
        return max(1.0, nxt - now)
    with SL_UPDATE_LOCK:
        SL_UPDATE.setdefault("fails", {})[best] = fails + 1
    log(f"streamlink update: upgrade to {best} failed — {msg}; retry in {int(SL_UPDATE_RETRY_SECS // 60)} min")
    _sl_set(state="error", lastError=f"streamlink upgrade to {best} failed: {msg}",
            lastResult=f"{inst} installed, {best} available", installed=streamlink_cli_version() or inst,
            nextCheckAt=now + SL_UPDATE_RETRY_SECS)
    STATUS_FILE_WAKE.set()
    return SL_UPDATE_RETRY_SECS


def _sl_private_tick(s, now, nxt, res):
    """v6.59: this computer's Python can't run the newest streamlink → build the helper's own copy when idle."""
    target = s.get("latest")
    fails = int((s.get("fails") or {}).get(SL_PRIVATE_KEY + ":" + str(target)) or 0)
    if fails >= SL_UPDATE_MAX_FAILS:
        _sl_set(state="python-too-old", lastResult=res, waitingFor=None, nextCheckAt=nxt)
        return max(1.0, nxt - now)
    busy = update_busy_reason()
    if busy or not update_idle_settled(now):
        why = busy or "just finished work — waiting a few idle minutes"
        if s.get("waitingFor") != why:
            log(f"streamlink update: {res} — will set up streamlink {target} on a newer Python when idle ({why})")
        _sl_set(state="private-waiting", waitingFor=why, lastResult=res, nextCheckAt=nxt)
        STATUS_FILE_WAKE.set()
        return AUTO_UPDATE_BUSY_SECS
    _sl_set(state="private-building", waitingFor=None)
    STATUS_FILE_WAKE.set()
    log(f"streamlink update: {res} — setting up streamlink {target} on a newer Python")
    try:
        ok, msg = sl_private_build(s.get("latestRequiresPython") or ">=3.10", target)
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"
    if ok:
        log(f"streamlink update: now using the helper's own {msg} (verified: Twitch plugin + recorder options)")
        info = streamlink_inspect()
        _sl_set(state="up-to-date", installed=info.get("installed") or target, kind=info.get("kind"),
                python=info.get("python"), pythonVersion=info.get("pythonVersion"), hint=info.get("hint"),
                latestCompatible=target, lastResult=f"updated to {msg}", lastError=None,
                lastUpgradeAt=time.time(), lastUpgradeFrom=s.get("installed"), lastUpgradeTo=target,
                installedReleasedAt=None, nextCheckAt=nxt)
        STATUS_FILE_WAKE.set()
        return max(1.0, nxt - now)
    with SL_UPDATE_LOCK:
        SL_UPDATE.setdefault("fails", {})[SL_PRIVATE_KEY + ":" + str(target)] = fails + 1
    log(f"streamlink update: couldn't set up streamlink {target} on a newer Python — {msg}; "
        f"still using {s.get('installed')}; retry in {int(SL_UPDATE_RETRY_SECS // 60)} min")
    _sl_set(state="python-too-old", lastResult=res, waitingFor=None,
            lastError=f"couldn't set up streamlink {target} on a newer Python: {msg}",
            nextCheckAt=now + SL_UPDATE_RETRY_SECS)
    STATUS_FILE_WAKE.set()
    return SL_UPDATE_RETRY_SECS


def streamlink_update_loop():
    SL_UPDATE_WAKE.wait(SL_UPDATE_FIRST_SECS)
    SL_UPDATE_WAKE.clear()
    while True:
        wait = SL_UPDATE_EVERY_SECS
        try:
            wait = streamlink_update_tick()
        except Exception as e:
            log(f"streamlink update loop error: {e}")
            _sl_set(state="error", lastError=f"streamlink update error: {e}")
            wait = SL_UPDATE_RETRY_SECS
        SL_UPDATE_WAKE.wait(max(1.0, float(wait)))
        SL_UPDATE_WAKE.clear()


def streamlink_update_snapshot():
    with SL_UPDATE_LOCK:
        s = dict(SL_UPDATE)
    s.pop("fails", None)
    s.pop("checkNow", None)
    s["enabled"] = streamlink_update_enabled()
    s["envDisabled"] = SL_UPDATE_ENV_OFF
    s["upgrading"] = SL_UPGRADING.is_set()
    s["lastCheckAt"] = s.get("lastCheckAt") or None
    s["nextCheckAt"] = s.get("nextCheckAt") or None
    s["lastUpgradeAt"] = s.get("lastUpgradeAt") or None
    s["outdated"] = bool(s.get("installed") and s.get("latestCompatible")
                         and _sl_vtuple(s["installed"]) < _sl_vtuple(s["latestCompatible"]))
    try:
        s["private"] = sl_private_snapshot()  # v6.59
    except Exception as e:
        s["private"] = {"active": False, "error": str(e)}
    return s


def streamlink_status_line(s):
    """One plain-English line for the Recorder status file / page."""
    if not s or not s.get("lastCheckAt"):
        return "streamlink: version not checked yet (first check a few minutes after the helper starts)"
    inst = s.get("installed")
    st = s.get("state")
    if st == "missing" or not inst:
        return "streamlink: NOT WORKING — " + (s.get("hint") or "streamlink --version failed")
    line = f"streamlink {inst}"
    if s.get("installedReleasedAt"):
        line += f" (released {s['installedReleasedAt']})"
    if st == "up-to-date":
        line += " — up to date"
    elif st == "python-too-old":
        line += f" — newest for Python {s.get('pythonVersion') or '?'}; streamlink {s.get('latest')} needs Python " \
                f"{s.get('latestRequiresPython') or 'newer'}"
        if s.get("lastError") and "newer Python" in str(s.get("lastError")):
            line += f" — {s['lastError']}"
        elif s.get("enabled") is False or SL_PRIVATE_ENV_OFF:
            line += " (install a newer Python to get Twitch fixes)"
    elif st == "private-waiting":
        line += f" — streamlink {s.get('latest')} needs Python {s.get('latestRequiresPython') or 'newer'}; " \
                f"setting up its own copy on a newer Python when idle ({s.get('waitingFor')})"
    elif st == "private-building":
        line += f" — setting up streamlink {s.get('latest')} on a newer Python…"
    elif st == "waiting-idle":
        line += f" — {s.get('latestCompatible')} available, upgrading when idle ({s.get('waitingFor')})"
    elif st == "upgrading":
        line += f" — upgrading to {s.get('latestCompatible')}…"
    elif st == "outdated-manual":
        line += f" — OUTDATED, {s.get('latestCompatible')} available; {s.get('hint')}"
    elif st == "off":
        line += f" — OUTDATED, {s.get('latestCompatible')} available (automatic streamlink updates off)"
    elif st == "error":
        line += " — " + (s.get("lastError") or s.get("lastResult") or "check failed")
    pv = s.get("private") or {}
    if pv.get("active"):
        line += f" [helper's own copy, Python {pv.get('pythonVersion') or '?'}]"
    if s.get("lastUpgradeTo"):
        line += f"; last auto-upgrade {s.get('lastUpgradeFrom')} → {s['lastUpgradeTo']} at {_iso_local(s.get('lastUpgradeAt'))}"
    return line


def api_streamlink_update(body):
    """POST /api/streamlink/update {enabled?: bool, checkNow?: bool} → snapshot."""
    body = body if isinstance(body, dict) else {}
    if "enabled" in body:
        with PIPELINE_LOCK:
            PIPELINE["streamlinkUpdate"] = bool(body.get("enabled"))
        _pipeline_save()
        log(f"streamlink update pref: {'on' if body.get('enabled') else 'off'}")
        STATUS_FILE_WAKE.set()
    if body.get("checkNow"):
        _sl_set(checkNow=True)
    SL_UPDATE_WAKE.set()
    return {"ok": True, "streamlinkUpdate": streamlink_update_snapshot()}, 200


def _self_test_streamlink_update():
    """v6.55: requires_python check, PyPI pick, tick decisions (idle wait, upgrade, rollback, manual)."""
    assert _py_satisfies(">=3.10", "3.9.6") is False
    assert _py_satisfies(">=3.10", "3.12.1") is True
    assert _py_satisfies(">=3.9", "3.9.6") is True
    assert _py_satisfies(">=3.8, !=3.9.*, <4", "3.9.6") is False
    assert _py_satisfies("~=3.9", "3.13.0") is True and _py_satisfies("", "2.7.0") is True
    doc = {"releases": {
        "6.5.0": [{"requires_python": ">=3.8", "upload_time": "2024-01-01T00:00:00"}],
        "7.6.0": [{"requires_python": ">=3.9", "upload_time": "2025-09-08T17:20:57"}],
        "8.6.2": [{"requires_python": ">=3.10", "upload_time": "2026-10-07T19:50:40"}],
        "9.0.0b1": [{"requires_python": ">=3.10"}],
        "9.0.0": [{"requires_python": ">=3.10", "yanked": True}],
    }}
    lat = streamlink_latest("3.9.6", doc=doc)
    assert lat["latest"] == "8.6.2" and lat["latestCompatible"] == "7.6.0", lat
    assert streamlink_latest("3.12.0", doc=doc)["latestCompatible"] == "8.6.2"
    g = globals()
    saved = {k: g[k] for k in ("streamlink_inspect", "streamlink_latest", "streamlink_upgrade",
                               "update_busy_reason", "update_idle_settled", "streamlink_cli_version",
                               "sl_private_build")}
    g["sl_private_build"] = lambda *a, **k: (False, "self-test: no private build")  # v6.59: never build for real
    with SL_UPDATE_LOCK:
        saved_state = dict(SL_UPDATE)
        saved_state["fails"] = dict(SL_UPDATE.get("fails") or {})
    with PIPELINE_LOCK:
        saved_pref = PIPELINE.get("streamlinkUpdate", True)
        PIPELINE["streamlinkUpdate"] = True
    calls = []
    env = {"info": {"installed": "6.5.0", "kind": "pip-user", "python": "/x/python3", "pythonVersion": "3.12.1",
                    "hint": None},
           "busy": "recording whistleface", "ok": (True, "streamlink 6.5.0 → 8.6.2")}

    def _reset():
        with SL_UPDATE_LOCK:
            SL_UPDATE.update(state="starting", lastCheckAt=0.0, lastError=None, lastResult=None, fails={},
                             waitingFor=None, lastUpgradeTo=None, checkNow=False)
    try:
        g["streamlink_inspect"] = lambda sl=None: dict(env["info"])
        g["streamlink_latest"] = lambda pyver=None, doc=None: streamlink_latest_real(pyver, doc=doc_)
        streamlink_latest_real, doc_ = saved["streamlink_latest"], doc
        g["update_busy_reason"] = lambda: env["busy"]
        g["update_idle_settled"] = lambda now=None: True
        g["streamlink_cli_version"] = lambda sl=None: env["info"]["installed"]

        def _fake_up(info, target):
            calls.append((info.get("installed"), target, info.get("kind")))
            return env["ok"]
        g["streamlink_upgrade"] = _fake_up
        _reset()
        # recording → wait, never upgrade mid-stream
        assert streamlink_update_tick() == AUTO_UPDATE_BUSY_SECS
        assert SL_UPDATE["state"] == "waiting-idle" and not calls, SL_UPDATE
        assert "upgrading when idle" in streamlink_status_line(streamlink_update_snapshot())
        # idle → upgrade to newest compatible
        env["busy"] = None
        streamlink_update_tick()
        assert calls == [("6.5.0", "8.6.2", "pip-user")], calls
        assert SL_UPDATE["state"] == "up-to-date" and SL_UPDATE["lastUpgradeTo"] == "8.6.2", SL_UPDATE
        # failed upgrade (rolled back) → error, retry later, give up after SL_UPDATE_MAX_FAILS
        _reset()
        calls.clear()
        env["ok"] = (False, "pip install failed (exit 1): boom; kept streamlink 6.5.0")
        for _i in range(SL_UPDATE_MAX_FAILS + 2):
            with SL_UPDATE_LOCK:
                SL_UPDATE["lastCheckAt"] = 0.0
            streamlink_update_tick()
        assert len(calls) == SL_UPDATE_MAX_FAILS, calls
        assert SL_UPDATE["state"] == "error"
        # Python 3.9 can't run 8.x → no upgrade, clear explanation
        _reset()
        calls.clear()
        env["info"] = dict(env["info"], installed="7.6.0", pythonVersion="3.9.6")
        streamlink_update_tick()
        assert not calls and SL_UPDATE["state"] == "python-too-old", SL_UPDATE
        assert "needs Python >=3.10" in streamlink_status_line(streamlink_update_snapshot())
        # Homebrew copy → never touched, hint shown
        _reset()
        env["info"] = {"installed": "6.5.0", "kind": "homebrew", "python": None, "pythonVersion": None,
                       "hint": _sl_manual_hint("homebrew")}
        streamlink_update_tick()
        assert not calls and SL_UPDATE["state"] == "outdated-manual", SL_UPDATE
        assert "brew upgrade streamlink" in streamlink_status_line(streamlink_update_snapshot())
        # pref off → report only
        _reset()
        env["info"] = {"installed": "6.5.0", "kind": "pip", "python": "/x/py", "pythonVersion": "3.12.0", "hint": None}
        with PIPELINE_LOCK:
            PIPELINE["streamlinkUpdate"] = False
        streamlink_update_tick()
        assert not calls and SL_UPDATE["state"] == "off", SL_UPDATE
    finally:
        g.update(saved)
        with SL_UPDATE_LOCK:
            SL_UPDATE.clear()
            SL_UPDATE.update(saved_state)
        with PIPELINE_LOCK:
            PIPELINE["streamlinkUpdate"] = saved_pref
    # recording start doesn't wait when no upgrade is running
    t0 = time.time()
    wait_streamlink_upgrade(5)
    assert time.time() - t0 < 1


def _self_test_v659():
    """v6.59: private streamlink on a newer Python — uv asset names, pointer/fallback, tick decisions, build."""
    import tempfile as _tmp59
    assert _uv_asset("Darwin", "arm64") == "uv-aarch64-apple-darwin.tar.gz"
    assert _uv_asset("Darwin", "x86_64") == "uv-x86_64-apple-darwin.tar.gz"
    assert _uv_asset("Windows", "AMD64") == "uv-x86_64-pc-windows-msvc.zip"
    assert _uv_asset("Linux", "aarch64") == "uv-aarch64-unknown-linux-gnu.tar.gz"
    g = globals()
    names = ("streamlink_inspect", "streamlink_latest", "streamlink_upgrade", "update_busy_reason",
             "update_idle_settled", "streamlink_cli_version", "sl_private_build", "_sl_verify", "_sl_run")
    saved = {k: g[k] for k in names}
    with SL_UPDATE_LOCK:
        saved_state = dict(SL_UPDATE)
        saved_state["fails"] = dict(SL_UPDATE.get("fails") or {})
    with PIPELINE_LOCK:
        saved_pref = PIPELINE.get("streamlinkUpdate", True)
        PIPELINE["streamlinkUpdate"] = True
    prev_env = os.environ.get("TWITCH_RECORDER_PRIVATE_STREAMLINK_DIR")
    doc = {"releases": {"7.6.0": [{"requires_python": ">=3.9", "upload_time": "2025-09-08T00:00:00"}],
                        "8.6.2": [{"requires_python": ">=3.10", "upload_time": "2026-10-07T00:00:00"}]}}
    lat_real = saved["streamlink_latest"]
    try:
        with _tmp59.TemporaryDirectory(prefix="tar-slpriv-") as td:
            os.environ["TWITCH_RECORDER_PRIVATE_STREAMLINK_DIR"] = td
            _SL_PRIV_CACHE.update(mtime=None, bin=None)
            assert sl_private_bin() is None and which_streamlink() == which_streamlink_system()
            # pointer → private bin wins; missing file → ignored; disable → back to system
            venv = Path(td) / "venv-x"
            _vpy, vsl = _sl_venv_paths(venv)
            vsl.parent.mkdir(parents=True)
            vsl.write_text("#!/bin/sh\n", encoding="utf-8")
            (Path(td) / "current.json").write_text(json.dumps({"dir": "venv-x", "version": "8.6.2"}), encoding="utf-8")
            assert sl_private_bin() == str(vsl) and which_streamlink() == str(vsl)
            assert sl_private_snapshot()["active"] is True
            sl_private_disable("self-test")
            assert sl_private_bin() is None and (Path(td) / "current.json.off").is_file()
            shutil.rmtree(str(venv))
            # tick: Python 3.9 + newest needs 3.10 → wait while busy, build when idle, 3 failures max
            env = {"info": {"installed": "7.6.0", "kind": "pip", "python": "/x/py", "pythonVersion": "3.9.6",
                            "hint": None}, "busy": "recording whistleface", "ok": (True, "streamlink 8.6.2 on test")}
            calls = []
            g["streamlink_inspect"] = lambda sl=None: dict(env["info"])
            g["streamlink_latest"] = lambda pyver=None, doc=None: lat_real(pyver, doc=doc_)
            doc_ = doc
            g["update_busy_reason"] = lambda: env["busy"]
            g["update_idle_settled"] = lambda now=None: True
            g["streamlink_cli_version"] = lambda sl=None: env["info"]["installed"]
            g["streamlink_upgrade"] = lambda info, target: (_ for _ in ()).throw(AssertionError("no pip upgrade"))

            def _fake_build(spec, version, **kw):
                calls.append((spec, version))
                return env["ok"]
            g["sl_private_build"] = _fake_build

            def _reset():
                with SL_UPDATE_LOCK:
                    SL_UPDATE.update(state="starting", lastCheckAt=0.0, lastError=None, lastResult=None, fails={},
                                     waitingFor=None, lastUpgradeTo=None, checkNow=False)
            _reset()
            assert streamlink_update_tick() == AUTO_UPDATE_BUSY_SECS and not calls
            assert SL_UPDATE["state"] == "private-waiting", SL_UPDATE
            assert "newer Python when idle" in streamlink_status_line(streamlink_update_snapshot())
            env["busy"] = None
            streamlink_update_tick()
            assert calls == [(">=3.10", "8.6.2")], calls
            assert SL_UPDATE["state"] == "up-to-date" and SL_UPDATE["lastUpgradeTo"] == "8.6.2", SL_UPDATE
            _reset()
            calls.clear()
            env["ok"] = (False, "no network")
            for _i in range(SL_UPDATE_MAX_FAILS + 2):
                streamlink_update_tick()
            assert len(calls) == SL_UPDATE_MAX_FAILS, calls
            assert SL_UPDATE["state"] == "python-too-old"
            assert "couldn't set up streamlink 8.6.2" in streamlink_status_line(streamlink_update_snapshot())
            # pref off → no build, old hint
            _reset()
            calls.clear()
            with PIPELINE_LOCK:
                PIPELINE["streamlinkUpdate"] = False
            streamlink_update_tick()
            assert not calls and SL_UPDATE["state"] == "python-too-old"
            assert "install a newer Python" in streamlink_status_line(streamlink_update_snapshot())
            with PIPELINE_LOCK:
                PIPELINE["streamlinkUpdate"] = True
            # build path with fakes: found Python → venv → pip → verify → pointer switched; failure leaves nothing
            g["sl_private_build"] = saved["sl_private_build"]
            ran = []

            def _fake_run(cmd, timeout=60, extra_env=None):
                ran.append(cmd)
                if cmd[1:3] == ["-m", "venv"]:
                    py, sl = _sl_venv_paths(cmd[3])
                    py.parent.mkdir(parents=True, exist_ok=True)
                    py.write_text("", encoding="utf-8")
                    sl.write_text("", encoding="utf-8")
                    return 0, ""
                if "-c" in cmd:
                    return 0, "PYV=3.12.4"
                return 0, "ok"
            g["_sl_run"] = _fake_run
            g["_sl_verify"] = lambda expected, sl=None: None
            ok, msg = sl_private_build(">=3.10", "8.6.2", find_python=lambda spec: ("/opt/py3.12", "3.12.4"),
                                       get_uv=lambda root: (_ for _ in ()).throw(AssertionError("no uv")))
            assert ok and "Python 3.12.4" in msg, msg
            assert any(c[-1] == "streamlink==8.6.2" for c in ran), ran
            assert sl_private_bin() and sl_private_info()["version"] == "8.6.2"
            first = sl_private_info()["dir"]
            g["_sl_verify"] = lambda expected, sl=None: "the new streamlink's Twitch plugin doesn't handle twitch.tv URLs"
            time.sleep(1.1)
            ok, msg = sl_private_build(">=3.10", "8.6.3", find_python=lambda spec: ("/opt/py3.12", "3.12.4"))
            assert not ok and "Twitch plugin" in msg
            assert sl_private_info()["dir"] == first and len(list(Path(td).glob("venv-*"))) == 1
            # no newer Python → uv path with managed Python
            g["_sl_verify"] = lambda expected, sl=None: None
            ran.clear()

            def _fake_run_uv(cmd, timeout=60, extra_env=None):
                ran.append((cmd, extra_env))
                if len(cmd) > 1 and cmd[1] == "venv":
                    py, sl = _sl_venv_paths(cmd[-1])
                    py.parent.mkdir(parents=True, exist_ok=True)
                    py.write_text("", encoding="utf-8")
                    sl.write_text("", encoding="utf-8")
                    return 0, ""
                return _fake_run(cmd, timeout)
            g["_sl_run"] = _fake_run_uv
            time.sleep(1.1)
            ok, msg = sl_private_build(">=3.10", "8.6.2", find_python=lambda spec: (None, None),
                                       get_uv=lambda root: "/fake/uv")
            assert ok and "uv" in msg, msg
            uvc = [r for r in ran if r[0][0] == "/fake/uv"][0]
            assert "--managed-python" in uvc[0] and uvc[1]["UV_PYTHON_INSTALL_DIR"].startswith(td)
            assert len(list(Path(td).glob("venv-*"))) == 1
    finally:
        g.update(saved)
        if prev_env is None:
            os.environ.pop("TWITCH_RECORDER_PRIVATE_STREAMLINK_DIR", None)
        else:
            os.environ["TWITCH_RECORDER_PRIVATE_STREAMLINK_DIR"] = prev_env
        _SL_PRIV_CACHE.update(mtime=None, bin=None)
        with SL_UPDATE_LOCK:
            SL_UPDATE.clear()
            SL_UPDATE.update(saved_state)
        with PIPELINE_LOCK:
            PIPELINE["streamlinkUpdate"] = saved_pref


def _self_test_go_live_schedule():
    """v6.52: learned go-live windows from filenames, wrap past midnight, armed gating."""
    import tempfile as _tmp_gl
    global REC_DIR, PIPELINE_FILE
    prev_rec = REC_DIR
    prev_pf = PIPELINE_FILE
    with PIPELINE_LOCK:
        prev_hist = dict(PIPELINE.get("goLiveHistory") or {})
        prev_sched = PIPELINE.get("keepAwakeSchedule", True)
    with STATUS_FILE_LOCK:
        prev_page = dict(PAGE_SEEN)
    prev_cache = dict(KA_SCHED_CACHE)
    try:
        with _tmp_gl.TemporaryDirectory(prefix="tar-golive-") as td:
            REC_DIR = Path(td)
            PIPELINE_FILE = REC_DIR / ".drive-pipeline.json"  # never touch the real prefs file
            with PIPELINE_LOCK:
                PIPELINE["goLiveHistory"] = {}
                PIPELINE["keepAwakeSchedule"] = True
            now = time.time()
            # three past Fridays ~6:49-7:05 PM, one 1:10 AM restart segment (belongs to Friday night),
            # one lone Tuesday (not enough samples)
            fri = datetime.datetime.fromtimestamp(now).replace(hour=12, minute=0, second=0, microsecond=0)
            while fri.weekday() != 4:
                fri -= datetime.timedelta(days=1)
            for wk, (h, m) in enumerate(((18, 49), (19, 5), (18, 55))):
                d = fri - datetime.timedelta(days=7 * (wk + 1))
                t = d.replace(hour=h, minute=m)
                (REC_DIR / f"whistleface-{t.strftime('%a-%Y-%m-%dT%H-%M-%S')}.ogg").write_bytes(b"x" * KA_SCHED_MIN_BYTES)
            late = (fri - datetime.timedelta(days=6)).replace(hour=1, minute=10)  # Sat 1:10 AM
            (REC_DIR / f"whistleface-{late.strftime('%a-%Y-%m-%dT%H-%M-%S')}.ogg").write_bytes(b"x" * KA_SCHED_MIN_BYTES)
            (REC_DIR / f"whistleface-{late.strftime('%a-%Y-%m-%dT%H-%M-%S')}-music.ogg").write_bytes(b"x" * KA_SCHED_MIN_BYTES)
            tue = (fri - datetime.timedelta(days=10)).replace(hour=15)
            (REC_DIR / f"whistleface-{tue.strftime('%a-%Y-%m-%dT%H-%M-%S')}.ogg").write_bytes(b"x" * KA_SCHED_MIN_BYTES)
            (REC_DIR / "whistleface-Fri-2026-01-02T18-00-00.ogg").write_bytes(b"x")  # tiny crumb / too old
            starts = go_live_starts(now, force=True)
            assert len(starts.get("whistleface") or []) == 5, starts
            wins = learned_go_live_windows(["whistleface"], starts=starts, now=now)
            assert [w["weekdayName"] for w in wins] == ["Fri"], wins
            w = wins[0]
            assert w["samples"] == 3 and w["typical"] == "6:55 PM", w
            assert w["from"] == "4:49 PM" and w["to"] == "8:35 PM", w
            in_win = fri.replace(hour=17, minute=30).timestamp()
            out_win = fri.replace(hour=13, minute=0).timestamp()
            assert _window_bounds_now(w, in_win) and not _window_bounds_now(w, out_win)
            # history persisted so deleted files still count
            for f in list(REC_DIR.iterdir()):
                f.unlink()
            assert len(go_live_starts(now, force=True).get("whistleface") or []) == 5
            # armed gating: fresh page heartbeat with whistleface armed → reason; not armed → none
            with STATUS_FILE_LOCK:
                PAGE_SEEN["at"] = in_win
                PAGE_SEEN["armed"] = ["whistleface"]
            r = keep_awake_schedule_reason(in_win)
            assert r and "whistleface usually goes live Fri" in r, r
            assert keep_awake_schedule_reason(out_win) is None
            with STATUS_FILE_LOCK:
                PAGE_SEEN["armed"] = ["sweetdeesauce"]
            assert keep_awake_schedule_reason(in_win) is None
            with STATUS_FILE_LOCK:
                PAGE_SEEN["armed"] = ["whistleface"]
            with PIPELINE_LOCK:
                PIPELINE["keepAwakeSchedule"] = False
            assert keep_awake_schedule_reason(in_win) is None
            snap = keep_awake_schedule_snapshot(in_win)
            assert snap["enabled"] is False and snap["windows"], snap
    finally:
        REC_DIR = prev_rec
        PIPELINE_FILE = prev_pf
        with PIPELINE_LOCK:
            PIPELINE["goLiveHistory"] = prev_hist
            PIPELINE["keepAwakeSchedule"] = prev_sched
        with STATUS_FILE_LOCK:
            PAGE_SEEN.clear()
            PAGE_SEEN.update(prev_page)
        KA_SCHED_CACHE.clear()
        KA_SCHED_CACHE.update(prev_cache)


def self_test():
    """v6.43: `python twitch-recorder-server.py --self-test` — exercise the module without binding
    the port or starting threads (used by apply_update before swapping in a download)."""
    global _auto_restart_scheduled
    _auto_restart_scheduled = True  # a self-test must never restart anything
    _pipeline_load()
    h = health_payload()
    status_payload()
    render_status_text(build_status_doc())
    auto_update_snapshot()
    json.dumps(h)
    assert issubclass(Handler, http.server.BaseHTTPRequestHandler)
    # v6.46: stale page heartbeat → restart grace on; fresh empty armed → off; armed → on
    with STATUS_FILE_LOCK:
        prev_at, prev_armed = PAGE_SEEN.get("at"), list(PAGE_SEEN.get("armed") or [])
        PAGE_SEEN["at"] = time.time() - max(float(PAGE_HEARTBEAT_STALE_SECS), 1) - 5
        PAGE_SEEN["armed"] = []
    assert restart_grace_applies("whistleface") is True
    with STATUS_FILE_LOCK:
        PAGE_SEEN["at"] = time.time()
        PAGE_SEEN["armed"] = []
    assert restart_grace_applies("whistleface") is False
    with STATUS_FILE_LOCK:
        PAGE_SEEN["armed"] = ["whistleface"]
    assert restart_grace_applies("whistleface") is True
    with STATUS_FILE_LOCK:
        PAGE_SEEN["at"] = prev_at
        PAGE_SEEN["armed"] = prev_armed
    # v6.48: release a fake ACTIVE slot that is in restart grace (no streamlink)
    fake_u = "selftestgrace"
    with LOCK:
        prev_fake = ACTIVE.pop(fake_u, None)
        ACTIVE[fake_u] = {"proc": None, "file": "", "want": True, "gen": 987654,
                          "retrying": True, "starting": False, "supervised": True,
                          "graceUntil": time.time() + 300}
    try:
        res, st = release_restart_grace(fake_u, "self-test: Helix offline 150s")
        assert st == 200 and res.get("ok") and res.get("released"), res
        with LOCK:
            r = ACTIVE[fake_u]
            assert r.get("graceRelease") is True and float(r.get("graceUntil") or 0) == 0.0
        assert grace_release_reason(fake_u, 987654) == "self-test: Helix offline 150s"
        assert grace_release_reason(fake_u, 1) is None  # other generation untouched
        t0 = time.time()
        assert interruptible_backoff(fake_u, 987654, 30) is True
        assert time.time() - t0 < 2, "interruptible_backoff did not wake on graceRelease"
        res2, _ = release_restart_grace(fake_u, "again")
        assert res2.get("already") is True
        entry = [a for a in status_payload()["active"] if a["username"] == fake_u][0]
        assert entry["graceReleased"] is True and entry["graceUntil"] == 0.0
        with LOCK:
            clear_grace_release_locked(ACTIVE[fake_u])
            ACTIVE[fake_u]["graceUntil"] = time.time() - 1  # grace over → refuse
        res3, st3 = release_restart_grace(fake_u, "late")
        assert st3 == 400 and not res3.get("ok"), res3
        with LOCK:
            ACTIVE[fake_u]["graceUntil"] = time.time() + 300
            ACTIVE[fake_u]["retrying"] = False  # recording again → refuse
        res4, _ = release_restart_grace(fake_u, "live")
        assert not res4.get("ok"), res4
        res5, st5 = release_restart_grace("nobodyhere_xyz", "x")
        assert st5 == 400 and not res5.get("ok")
        res6, st6 = release_restart_grace("bad name!", "x")
        assert st6 == 400 and not res6.get("ok")
    finally:
        with LOCK:
            ACTIVE.pop(fake_u, None)
            if prev_fake is not None:
                ACTIVE[fake_u] = prev_fake
    assert "graceOfflineSecs" in health_payload()
    # v6.57: Twitch ad-break detection from streamlink's log
    _pre = "[cli][info] Opening stream: audio_only (hls)\n[plugins.twitch][info] Will skip ad segments\n" \
           "[plugins.twitch][info] Waiting for pre-roll ads to finish, be patient\n"
    st_a = streamlink_ad_state(None, _pre)
    assert st_a["inAd"] and st_a["preroll"] and st_a["breakSecs"] is None, st_a
    _mid = _pre + "[stream.hls][info] Resuming stream output\n[plugins.twitch][info] Detected advertisement " \
           "break of 120 seconds\n[stream.hls][info] Filtering out segments and pausing stream output\n"
    st_b = streamlink_ad_state(None, _mid)
    assert st_b["inAd"] and not st_b["preroll"] and st_b["breakSecs"] == 120, st_b
    st_c = streamlink_ad_state(None, _mid + "[stream.hls][info] Resuming stream output\n")
    assert not st_c["inAd"], st_c
    assert ad_pause_allowance(st_c, STALL_SECS) == STALL_SECS
    if AD_PAUSE_MAX_SECS:
        assert ad_pause_allowance(st_b, STALL_SECS) == min(AD_PAUSE_MAX_SECS, 120 + STALL_SECS)
        assert ad_pause_allowance(st_a, QUICK_FAIL_SECS) == AD_PAUSE_MAX_SECS
    import tempfile as _tmpf57
    with _tmpf57.TemporaryFile() as _tf:
        _tf.write(_mid.encode()); _tf.flush()
        class _P: pass
        _pp = _P(); _pp._stderr_file = _tf
        pos = _tf.tell()
        assert streamlink_ad_state(_pp)["inAd"]
        assert _tf.tell() == pos  # never moves the offset streamlink writes through
    assert "adPause" in health_payload()
    # v6.47: status_file_snapshot exposes local mirror + driveOk keys
    with STATUS_FILE_LOCK:
        prev_sf = dict(STATUS_FILE)
        STATUS_FILE.update(localLastWrittenAt=time.time(), localTxtPath="/tmp/fake-status.txt",
                           localError=None, lastWrittenAt=0.0, error="Google Drive for Desktop not found")
    snap = status_file_snapshot()
    for k in ("localLastWrittenAt", "localTxtPath", "localError", "driveOk"):
        assert k in snap, f"status_file_snapshot missing {k}"
    assert snap["driveOk"] is False
    assert snap["localTxtPath"] == "/tmp/fake-status.txt"
    da = drive_access_snapshot()
    assert da.get("ok") is False and da.get("kind") in ("missing", "fda", "other")
    # Local mirror write into a temp REC_DIR (Drive may fail; local must succeed)
    import tempfile
    global REC_DIR
    prev_rec = REC_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="tar-status-") as td:
            REC_DIR = Path(td)
            with STATUS_FILE_LOCK:
                STATUS_FILE["error"] = None
                STATUS_FILE["localError"] = None
                STATUS_FILE["localLastWrittenAt"] = 0.0
                STATUS_FILE["localTxtPath"] = None
            write_status_file("self-test")
            folder, txt, js = status_local_paths()
            assert txt.is_file() and js.is_file(), "local status mirror not written"
            snap2 = status_file_snapshot()
            assert snap2.get("localLastWrittenAt"), "localLastWrittenAt not set after write"
            assert snap2.get("localTxtPath") == str(txt)
    finally:
        REC_DIR = prev_rec
        with STATUS_FILE_LOCK:
            STATUS_FILE.clear()
            STATUS_FILE.update(prev_sf)
    # v6.49: solo-watch persistence + stale/fresh decision (no network)
    import tempfile as _tmp_sw
    prev_rec2 = REC_DIR
    with SOLO_WATCH_LOCK:
        prev_sw = {
            "enabled": SOLO_WATCH.get("enabled"),
            "users": dict(SOLO_WATCH.get("users") or {}),
            "active": SOLO_WATCH.get("active"),
            "lastCheckAt": SOLO_WATCH.get("lastCheckAt"),
            "lastError": SOLO_WATCH.get("lastError"),
            "startedByHelper": list(SOLO_WATCH.get("startedByHelper") or []),
            "loggedWatching": SOLO_WATCH.get("loggedWatching"),
        }
    with STATUS_FILE_LOCK:
        prev_page = {"at": PAGE_SEEN.get("at"), "armed": list(PAGE_SEEN.get("armed") or [])}
    try:
        with _tmp_sw.TemporaryDirectory(prefix="tar-solo-") as td:
            REC_DIR = Path(td)
            with SOLO_WATCH_LOCK:
                SOLO_WATCH["enabled"] = True
                SOLO_WATCH["users"] = {}
                SOLO_WATCH["active"] = False
                SOLO_WATCH["startedByHelper"] = []
            # persist + reload
            solo_watch_set_armed_users(["whistleface", "SweetDeeSauce"], explicit_empty=False)
            solo_watch_remember_options("whistleface", quality="best", after_stream=True, send_original=False)
            assert _solo_watch_path().is_file(), "solo-watch json not written"
            with SOLO_WATCH_LOCK:
                SOLO_WATCH["users"] = {}
            solo_watch_load()
            with SOLO_WATCH_LOCK:
                assert "whistleface" in SOLO_WATCH["users"]
                assert SOLO_WATCH["users"]["whistleface"]["quality"] == "best"
                assert SOLO_WATCH["users"]["whistleface"]["sendOriginal"] is False
                assert "sweetdeesauce" in SOLO_WATCH["users"]
            # empty armed clears
            solo_watch_set_armed_users([], explicit_empty=True)
            with SOLO_WATCH_LOCK:
                assert SOLO_WATCH["users"] == {}
            solo_watch_set_armed_users(["whistleface"], explicit_empty=False)
            # decision: fresh page open → not run
            with STATUS_FILE_LOCK:
                PAGE_SEEN["at"] = time.time()
                PAGE_SEEN["armed"] = ["whistleface"]
            ok, why = solo_watch_should_run(now=time.time(), helper_started_at=time.time() - 999)
            assert ok is False and why == "page-open", (ok, why)
            # stale page + armed → run
            with STATUS_FILE_LOCK:
                PAGE_SEEN["at"] = time.time() - max(float(PAGE_HEARTBEAT_STALE_SECS), 1) - 5
            ok, why = solo_watch_should_run(now=time.time(), helper_started_at=time.time() - 999)
            assert ok is True and why == "page-closed", (ok, why)
            # disabled → not run
            solo_watch_set_enabled(False, save=True)
            ok, why = solo_watch_should_run(now=time.time(), helper_started_at=time.time() - 999)
            assert ok is False and why == "disabled", (ok, why)
            solo_watch_set_enabled(True, save=True)
            # never seen + within startup delay → not run
            with STATUS_FILE_LOCK:
                PAGE_SEEN["at"] = 0.0
            ok, why = solo_watch_should_run(now=time.time(), helper_started_at=time.time())
            assert ok is False and why == "startup-delay", (ok, why)
            # never seen + past startup delay → run
            ok, why = solo_watch_should_run(
                now=time.time(),
                helper_started_at=time.time() - max(float(SOLO_WATCH_STARTUP_DELAY_SECS), 1) - 5,
            )
            assert ok is True and why == "page-closed", (ok, why)
            # no armed → not run
            solo_watch_set_armed_users([], explicit_empty=True)
            ok, why = solo_watch_should_run(
                now=time.time(),
                helper_started_at=time.time() - 999,
            )
            assert ok is False and why == "no-armed", (ok, why)
            snap = solo_watch_snapshot()
            assert "enabled" in snap and "users" in snap and "active" in snap
            assert "startedByHelper" in snap and "lastCheckAt" in snap
            assert "soloWatch" in health_payload()
            assert page_heartbeat_is_stale(time.time()) is True  # PAGE_SEEN.at == 0
    finally:
        REC_DIR = prev_rec2
        with SOLO_WATCH_LOCK:
            SOLO_WATCH.clear()
            SOLO_WATCH.update(prev_sw)
        with STATUS_FILE_LOCK:
            PAGE_SEEN["at"] = prev_page["at"]
            PAGE_SEEN["armed"] = prev_page["armed"]
    # v6.50: one copy per stream — manifest matching (temp folder, no Drive needed)
    import tempfile as _tmp_oc
    with _tmp_oc.TemporaryDirectory(prefix="tar-onecopy-") as td:
        tgt = Path(td)
        u, st0 = parse_recording_stamp("whistleface-Fri-2026-10-16T19-02-13-music-part1of7.ogg")
        assert u == "whistleface" and st0, (u, st0)
        assert parse_recording_stamp("not-a-recording.ogg") == (None, None)
        assert find_other_copy(str(tgt), u, st0, 4 * 3600) == (None, None)
        md = _sent_manifest_dir(tgt)
        md.mkdir(parents=True)
        other = "whistleface-Fri-2026-10-16T19-02-41"
        parts = []
        for i in range(1, 3):
            nm = f"{other}-music-part{i}of2.ogg"
            (tgt / nm).write_bytes(b"x" * (100 + i))
            parts.append({"name": nm, "size": 100 + i})
        doc = {"kind": "twitch-recorder-sent-parts", "v": 1, "computer": "OtherBox-SelfTest",
               "base": other, "username": "whistleface", "startEpoch": st0 + 28,
               "durationSecs": 4 * 3600 - 60, "state": "sent", "parts": parts, "updatedAt": time.time()}
        (md / f"{other}__OtherBox-SelfTest.json").write_text(json.dumps(doc), encoding="utf-8")
        k, _d = find_other_copy(str(tgt), "whistleface", st0, 4 * 3600)
        assert k == "sent", k
        # ours is much longer (other missed ~40 min) → still send ours
        assert find_other_copy(str(tgt), "whistleface", st0, 4 * 3600 + 2400)[0] is None
        # different streamer / different day → not covered
        assert find_other_copy(str(tgt), "sweetdeesauce", st0, 4 * 3600)[0] is None
        assert find_other_copy(str(tgt), "whistleface", st0 + 2 * 86400, 4 * 3600)[0] is None
        # a part removed from Drive → not covered
        (tgt / parts[1]["name"]).unlink()
        assert find_other_copy(str(tgt), "whistleface", st0, 4 * 3600)[0] is None
        # fresh "sending" claim → wait; stale claim → ignore
        doc["state"] = "sending"
        (md / f"{other}__OtherBox-SelfTest.json").write_text(json.dumps(doc), encoding="utf-8")
        assert find_other_copy(str(tgt), "whistleface", st0, 4 * 3600)[0] == "sending"
        assert find_other_copy(str(tgt), "whistleface", st0, 4 * 3600,
                               now=time.time() + ONE_COPY_CLAIM_STALE_SECS + 5)[0] is None
        # our own manifest never covers us
        assert write_sent_manifest(str(tgt), "whistleface-Fri-2026-10-16T19-02-13", "sent",
                                   parts=[{"name": parts[0]["name"], "size": parts[0]["size"]}],
                                   duration=5 * 3600)
        assert (md / f"whistleface-Fri-2026-10-16T19-02-13__{computer_name()}.json").is_file()
        doc["state"] = "error"
        (md / f"{other}__OtherBox-SelfTest.json").write_text(json.dumps(doc), encoding="utf-8")
        assert find_other_copy(str(tgt), "whistleface", st0, 4 * 3600)[0] is None
        assert _covered_note({"computer": "PC", "parts": [1, 2], "durationSecs": 600}).startswith("Already in Drive from PC")
    _self_test_cp1252_sends()  # v6.51
    _self_test_go_live_schedule()  # v6.52
    _self_test_orphan_recovery()  # v6.53
    _self_test_claimed_unstarted()  # v6.54
    _self_test_streamlink_update()  # v6.55
    _self_test_v656()  # v6.56
    _self_test_v658()  # v6.58
    _self_test_v659()  # v6.59
    _self_test_v660()  # v6.60
    _self_test_v661()  # v6.61
    print(json.dumps({"selfTest": "ok", "version": HELPER_VERSION}))
    return 0


def _self_test_cp1252_sends():
    """v6.51: with Windows-style cp1252 (strict) output, log() must not raise and a finished Drive send
    (parts + original) must stay "sent". Temp recordings dir + temp fake Drive; real state untouched."""
    import io as _io
    import tempfile as _tmp51
    global REC_DIR, DRIVE_PARTS_DIR, PIPELINE_FILE
    real_out = sys.stdout
    # 1) log() on a cp1252 stream and on a closed stream
    buf = _io.BytesIO()
    sys.stdout = _io.TextIOWrapper(buf, encoding="cp1252", errors="strict", write_through=True)
    try:
        log("self-test arrow → done")
        sys.stdout.flush()
        assert b"\\u2192" in buf.getvalue(), buf.getvalue()
        dead = _io.TextIOWrapper(_io.BytesIO(), encoding="utf-8")
        dead.close()
        sys.stdout = dead
        log("self-test closed stdout")  # must not raise
    finally:
        sys.stdout = real_out
    # 2) a full parts + original send while stdout is cp1252
    saved = {k: (os.environ.get(k)) for k in ("TWITCH_RECORDER_CLOUDSTORAGE", "TWITCH_RECORDER_DRIVE_VOLUMES")}
    prev_g = (REC_DIR, DRIVE_PARTS_DIR, PIPELINE_FILE)
    with PIPELINE_LOCK:
        prev_p = {k: (json.loads(json.dumps(PIPELINE.get(k))) if k in PIPELINE else None)
                  for k in ("deliveries", "originals", "target", "oneCopy")}
    try:
        with _tmp51.TemporaryDirectory(prefix="tar-cp1252-") as td:
            td = Path(td)
            REC_DIR = td / "rec"
            DRIVE_PARTS_DIR = REC_DIR / "Drive parts"
            PIPELINE_FILE = REC_DIR / ".drive-pipeline.json"
            my_drive = td / "CloudStorage" / "GoogleDrive-selftest@example.com" / "My Drive"
            tgt = my_drive / "Twitch"
            tgt.mkdir(parents=True)
            REC_DIR.mkdir(parents=True)
            os.environ["TWITCH_RECORDER_CLOUDSTORAGE"] = str(td / "CloudStorage")
            os.environ["TWITCH_RECORDER_DRIVE_VOLUMES"] = ""
            base = "selftestcp-Mon-2026-10-19T19-00-00"
            pdir = DRIVE_PARTS_DIR / base
            pdir.mkdir(parents=True)
            names = [f"{base}-music-part{i}of2.ogg" for i in (1, 2)]
            for nm in names:
                (pdir / nm).write_bytes(b"o" * 300)
            orig = f"{base}.ogg"
            (REC_DIR / orig).write_bytes(b"r" * 9000)
            with PIPELINE_LOCK:
                PIPELINE["deliveries"] = {}
                PIPELINE["originals"] = {}
                PIPELINE["target"] = os.path.realpath(str(tgt))
                PIPELINE["oneCopy"] = True
            register_delivery(base, f"{base}-music.ogg", pdir, names, 600.0, "stream")
            register_original(orig, "selftestcp", "stream")
            buf2 = _io.BytesIO()
            sys.stdout = _io.TextIOWrapper(buf2, encoding="cp1252", errors="strict", write_through=True)
            try:
                snap = deliver_one(base) or {}
                osnap = deliver_original(orig) or {}
            finally:
                sys.stdout = real_out
            assert snap.get("status") == "sent", snap
            assert snap.get("localPartsCleared") is True and not pdir.exists(), snap
            assert all((Path(PIPELINE["target"]) / nm).is_file() for nm in names)
            if one_copy_enabled():  # TWITCH_RECORDER_ONE_COPY=0 writes no note
                man = json.loads((_sent_manifest_dir(PIPELINE["target"]) / f"{base}__{computer_name()}.json")
                                 .read_text(encoding="utf-8"))
                assert man.get("state") == "sent", man
            assert osnap.get("status") == "sent", osnap
            assert (Path(PIPELINE["target"]) / ORIGINALS_SUBDIR / orig).is_file()
            assert (REC_DIR / orig).is_file()  # local original kept
    finally:
        sys.stdout = real_out
        REC_DIR, DRIVE_PARTS_DIR, PIPELINE_FILE = prev_g
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with PIPELINE_LOCK:
            for k, v in prev_p.items():
                if v is None:
                    PIPELINE.pop(k, None)
                else:
                    PIPELINE[k] = v


def _pipeline_health_summary():
    with PIPELINE_LOCK:
        dels = list(PIPELINE["deliveries"].values())
        enabled = bool(PIPELINE["enabled"])
        origs = list((PIPELINE.get("originals") or {}).values())
        orig_on = bool(PIPELINE.get("origEnabled", True))
    return {
        "enabled": enabled,
        "originals": orig_on,  # v6.38
        "origWaiting": sum(1 for d in origs if d.get("status") in ("waiting", "queued")),
        "origErrors": sum(1 for d in origs if d.get("status") == "error"),
        "computer": computer_name(),
        "waiting": sum(1 for d in dels if d.get("status") == "waiting"),
        "errors": sum(1 for d in dels if d.get("status") == "error"),
        "covered": sum(1 for d in dels if d.get("status") == "covered"),  # v6.50
        "oneCopy": one_copy_enabled(),  # v6.50
        "sending": bool(DELIVERY_STATE.get("busy")),
        "driveFound": bool(detect_drive_roots()),
        "voiceRetry": _pipeline_retry_snapshot(),  # v6.61
    }


def health_payload():
    with LOCK:
        reap_locked()
        active = list(ACTIVE.keys())
        last_errors = dict(LAST_ERROR)
        retrying = [
            u for u, r in ACTIVE.items() if r.get("retrying") or r.get("starting")
        ]
    sl = which_streamlink()
    disk = disk_status()
    maybe_cleanup_orphan_temps_on_diskwarn(disk)
    prune_recent_sessions()
    orphan = ORPHAN_TEMPS_LAST or {}
    # v6.32: if update landed on disk and we are idle, schedule one auto-restart
    maybe_auto_restart_idle()
    disk_ver = disk_file_version()
    pending = pending_restart_needed()
    return {
        "ok": True,
        "version": HELPER_VERSION,
        "diskVersion": disk_ver,
        "pendingRestart": pending,
        "streamlink": bool(sl),
        "ffmpeg": bool(which_ffmpeg()),
        "seamless": seamless_available(),
        "demucs": demucs_available(),
        # v6.24: install hint for HTML sticky banner / Convert / History "Need demucs"
        "demucsHint": None if demucs_available() else "pip install demucs (first run downloads models)",
        "voiceQuality": voice_quality_snapshot(),  # v6.45 Fast (ffmpeg) / Better (demucs)
        # v6.48: page ends restart grace after Helix says offline this long (0 = never early)
        "graceOfflineSecs": GRACE_OFFLINE_SECS,
        "restartGraceSecs": RESTART_GRACE_SECS,
        # v6.57: Twitch ad breaks waited out instead of treated as a stall / failed start
        "adPause": {"maxSecs": AD_PAUSE_MAX_SECS, **{k: AD_STATS[k] for k in AD_STATS}},
        "recordingsDir": str(REC_DIR),
        "active": active,
        "lastErrors": last_errors,
        "retrying": retrying,
        "installHint": None if sl else "pip install streamlink",
        "diskFree": disk["diskFree"],
        "diskTotal": disk["diskTotal"],
        "diskWarn": disk["diskWarn"],
        "diskBlock": disk["diskBlock"],
        "musicJobs": music_jobs_for_health(),
        # v6.36: after-stream → Drive delivery state
        "drivePipeline": _pipeline_health_summary(),
        "driveDeliveries": deliveries_snapshot(40),
        "driveOriginals": originals_snapshot(60),  # v6.38
        "musicDemucsQueue": music_demucs_queue_info(),
        "keepAwake": keep_awake_snapshot(),  # v6.40
        "statusFile": status_file_snapshot(),  # v6.41
        "driveAccess": drive_access_snapshot(),  # v6.47: sticky banner when FDA/Drive blocks
        "autoUpdate": auto_update_snapshot(),  # v6.43
        "streamlinkUpdate": streamlink_update_snapshot(),  # v6.55
        "autostart": autostart_snapshot(),  # v6.56 (Windows only; null elsewhere)
        "soloWatch": solo_watch_snapshot(),  # v6.49
        "recoveredSessions": recovery_snapshot(),  # v6.53
        # v6.51: log lines the console/log file couldn't take as-is (Windows code page, closed pipe)
        "logWriteErrors": int(LOG_WRITE_ERRORS[0]),
        "logEncoding": str(getattr(sys.stdout, "encoding", None) or ""),
        "seamlessJobs": seamless_jobs_for_health(),
        "updateRepo": f"https://github.com/{UPDATE_REPO}",
        # v6.29: last orphan temp cleanup (UI one-shot flash when non-zero after reconnect)
        "orphanTempsCleared": int(orphan.get("dirs") or 0) + int(orphan.get("files") or 0),
        "orphanTempsBytes": int(orphan.get("bytes") or 0),
        "orphanTempsDirs": int(orphan.get("dirs") or 0),
        "orphanTempsFiles": int(orphan.get("files") or 0),
    }


def status_payload():
    with LOCK:
        reap_locked()
        items = []
        for user, rec in ACTIVE.items():
            sz = touch_bps_locked(rec)
            entry = {
                "username": user,
                "file": rec.get("file") or "",
                "startedAt": rec.get("startedAt") or "",
                "sessionStartedAt": rec.get("sessionStartedAt")
                or rec.get("startedAt")
                or "",
                "size": sz,
                "bps": float(rec.get("bps") or 0.0),
                "lastError": LAST_ERROR.get(user),
                "retrying": bool(rec.get("retrying") or rec.get("starting")),
                "resumes": int(rec.get("resumes") or 0),
                "sessionSegments": list(rec.get("sessionSegments") or []),
                # v6.42: >0 while waiting through a stream restart (epoch secs)
                "graceUntil": float(rec.get("graceUntil") or 0.0),
                # v6.48: page released the wait (Helix offline long enough); finalizing
                "graceReleased": bool(rec.get("graceRelease")),
                # v6.49: "helper" when solo-watch started this while the page was closed
                "startedBy": rec.get("startedBy") or "page",
                # v6.57: Twitch ad playing — streamlink paused output on purpose (not a stall)
                "adBreak": dict(rec["adBreak"]) if rec.get("adBreak") else None,
                "adBreaks": int(rec.get("adBreaks") or 0),
            }
            peak = rec.get("peak")
            level = rec.get("level")
            if peak is None:
                peak = level
            if peak is not None:
                try:
                    entry["peak"] = float(peak)
                    entry["level"] = float(peak if level is None else level)
                except (TypeError, ValueError):
                    pass
            items.append(entry)
    return {"active": items, "lastErrors": dict(LAST_ERROR)}



def music_base_stem(src_name):
    """Native stem for pairing: strip trailing -music / -vocals / -music-N / -vocals-N."""
    stem = Path(src_name).stem
    import re as _re
    m = _re.match(r"^(.*)-(music|vocals)(?:-(\d+))?$", stem)
    if m:
        return m.group(1)
    return stem


def music_output_name(src_name, out_ext):
    """same basename + -music before extension."""
    stem = music_base_stem(src_name)
    ext = out_ext if out_ext.startswith(".") else ("." + out_ext)
    return f"{stem}-music{ext}"


def vocals_output_name(src_name, out_ext):
    stem = music_base_stem(src_name)
    ext = out_ext if out_ext.startswith(".") else ("." + out_ext)
    return f"{stem}-vocals{ext}"


def is_music_export_name(name):
    """True if basename looks like a demucs sibling export (not a native recording)."""
    stem = Path(name or "").stem
    import re as _re
    return bool(_re.search(r"-(music|vocals)(?:-\d+)?$", stem))


def find_existing_sibling(src_name, kind):
    """Return Path of existing <stem>-{kind}.{ogg,mp3,m4a,wav} (prefer ogg), or None."""
    if kind not in ("music", "vocals"):
        return None
    stem = music_base_stem(src_name)
    for ext in (".ogg", ".mp3", ".m4a", ".wav"):
        p = REC_DIR / f"{stem}-{kind}{ext}"
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def delete_music_siblings(src_name):
    """Delete prior -music / -vocals exports for redo. NEVER touches the native source."""
    stem = music_base_stem(src_name)
    removed = []
    for kind in ("music", "vocals"):
        for ext in (".ogg", ".mp3", ".m4a", ".wav"):
            p = REC_DIR / f"{stem}-{kind}{ext}"
            try:
                if not p.is_file():
                    continue
                # Extra safety: never delete the source path itself
                src = resolve_download_path(src_name)
                if src and p.resolve() == src.resolve():
                    continue
                p.unlink()
                removed.append(p.name)
                log(f"music-only redo removed {p.name}")
            except OSError as e:
                log(f"music-only redo could not remove {p.name}: {e}")
    return removed


def _set_music_job(job_id, **kwargs):
    with MUSIC_JOBS_LOCK:
        job = MUSIC_JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)


def _find_demucs_stem(work_dir, names):
    """Locate a demucs stem file under work_dir by basename prefixes (e.g. no_vocals, vocals)."""
    work = Path(work_dir)
    names_l = [n.lower() for n in names]
    candidates = []
    for p in work.rglob("*"):
        if not p.is_file():
            continue
        low = p.name.lower()
        stem_part = Path(low).stem
        if stem_part in names_l or any(low.startswith(n + ".") for n in names_l):
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: (0 if p.suffix.lower() == ".wav" else 1, len(str(p))))
    return candidates[0]


def _find_no_vocals(work_dir):
    """Locate demucs no_vocals (instrumental) stem under work_dir."""
    found = _find_demucs_stem(work_dir, ("no_vocals", "instrumental"))
    if found:
        return found
    # fallback: any path containing no_vocals
    work = Path(work_dir)
    candidates = []
    for p in work.rglob("*"):
        if not p.is_file():
            continue
        if "no_vocals" in p.name.lower():
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: (0 if p.suffix.lower() == ".wav" else 1, len(str(p))))
    return candidates[0]


def _find_vocals(work_dir):
    """Locate demucs vocals stem under work_dir (optional sibling export)."""
    return _find_demucs_stem(work_dir, ("vocals",))


def _remux_instrumental(wav_path, dest_path):
    """Prefer OGG (libvorbis, then libopus) via ffmpeg; else copy wav. Returns final Path."""
    ff = which_ffmpeg()
    wav_path = Path(wav_path)
    dest_path = Path(dest_path)
    if not ff:
        final = dest_path.with_suffix(".wav")
        shutil.copy2(str(wav_path), str(final))
        return final
    # Prefer ogg; fall back wav only if ogg encode fails (legacy mp3/m4a no longer preferred)
    for ext, args in (
        (".ogg", ["-codec:a", "libvorbis", "-q:a", "5"]),
        (".ogg", ["-codec:a", "libopus", "-b:a", "128k"]),
    ):
        out = dest_path.with_suffix(ext)
        cmd = [
            ff,
            "-y",
            "-i",
            str(wav_path),
            "-vn",
            *args,
            str(out),
        ]
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8", errors="replace",  # v6.51: not the Windows code page
                timeout=600,
            )
            if r.returncode == 0 and out.is_file() and out.stat().st_size > 0:
                return out
            log(f"ffmpeg remux to {ext} ({args[1]}) failed: {(r.stderr or '')[-400:]}")
            try:
                if out.is_file() and out.stat().st_size == 0:
                    out.unlink()
            except OSError:
                pass
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"ffmpeg remux {ext} error: {e}")
    final = dest_path.with_suffix(".wav")
    shutil.copy2(str(wav_path), str(final))
    return final


def _parse_demucs_progress_line(line):
    """Parse demucs/tqdm stdout line → (msg_or_None, demucs_pct_or_None)."""
    import re as _re

    s = (line or "").strip()
    if not s:
        return None, None
    # Prefer "XX%|" tqdm bar form, else bare "XX%"
    m = _re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s*\|", s)
    if not m:
        m = _re.search(r"(?<![.\d])(\d{1,3}(?:\.\d+)?)\s*%", s)
    pct = None
    if m:
        try:
            pct = float(m.group(1))
            if pct < 0:
                pct = 0.0
            elif pct > 100:
                pct = 100.0
        except ValueError:
            pct = None
    low = s.lower()
    msg = None
    if "separating" in low:
        msg = s if len(s) <= 120 else (s[:117] + "…")
    elif "download" in low or "downloading" in low or "fetch" in low:
        if pct is not None:
            msg = f"Downloading model… {pct:.0f}%"
        else:
            msg = "Downloading model…"
    elif pct is not None:
        msg = f"Demucs {pct:.0f}%"
    elif any(
        k in low
        for k in (
            "selected model",
            "loading",
            "torch",
            "cuda",
            "cpu",
            "info",
            "warning",
            "userwarning",
        )
    ):
        # Keep short status for useful setup lines; skip pure noise.
        if any(k in low for k in ("selected model", "loading")):
            msg = s if len(s) <= 100 else (s[:97] + "…")
        else:
            return None, None
    return msg, pct


def _map_demucs_pct_to_job(demucs_pct):
    """Map demucs 0–100 into job progressPct band ~5–88."""
    if demucs_pct is None:
        return None
    try:
        p = float(demucs_pct)
    except (TypeError, ValueError):
        return None
    if p < 0:
        p = 0.0
    elif p > 100:
        p = 100.0
    return round(5.0 + (p / 100.0) * 83.0, 1)


def _run_demucs(src_path, work_dir, progress_cb):
    """Run demucs streaming stdout+stderr line-by-line for progressPct updates.

    progress_cb(msg, pct=None) — pct is job-scale 0–100 (or None to hold last).
    Same CLI args as before; 6h timeout via wait(timeout=…) + kill.
    """
    demucs = which_demucs()
    if not demucs:
        raise RuntimeError(
            "demucs not installed — pip install demucs (first run downloads models)"
        )
    # Kick demucs phase (~5); finer % comes from tqdm parse.
    try:
        progress_cb("Running demucs (two-stems vocals / no_vocals)…", pct=5)
    except TypeError:
        progress_cb("Running demucs (two-stems vocals / no_vocals)…")
    cmd = demucs_cmd_base()  # v6.56: saves through soundfile/wave if torchaudio has no backend
    cmd += [
        "--two-stems=vocals",
        "-o",
        str(work_dir),
        str(src_path),
    ]
    timeout_secs = 3600 * 6
    # Binary + unbuffered so tqdm \r updates are visible before a newline.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    last_lines = []
    last_demucs_pct = {"v": None}
    lines_lock = threading.Lock()

    def _emit(msg, demucs_pct=None):
        if demucs_pct is not None:
            last_demucs_pct["v"] = demucs_pct
        mapped = _map_demucs_pct_to_job(last_demucs_pct["v"])
        try:
            if mapped is not None:
                progress_cb(msg, pct=mapped)
            else:
                progress_cb(msg, pct=None)
        except TypeError:
            # Older callers that only take msg
            progress_cb(msg)

    def _handle_line(line):
        with lines_lock:
            last_lines.append(line)
            if len(last_lines) > 200:
                del last_lines[:-100]
        msg, demucs_pct = _parse_demucs_progress_line(line)
        if demucs_pct is not None:
            _emit(msg or f"Demucs {demucs_pct:.0f}%", demucs_pct)
        elif msg:
            _emit(msg, None)

    def reader():
        try:
            buf = ""
            while True:
                chunk = proc.stdout.read(256)
                if not chunk:
                    break
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8", errors="replace")
                buf += chunk
                while True:
                    i_n = buf.find("\n")
                    i_r = buf.find("\r")
                    if i_n < 0 and i_r < 0:
                        break
                    if i_n < 0:
                        i = i_r
                    elif i_r < 0:
                        i = i_n
                    else:
                        i = min(i_n, i_r)
                    line = buf[:i]
                    skip = 1
                    if buf[i] == "\r" and i + 1 < len(buf) and buf[i + 1] == "\n":
                        skip = 2
                    buf = buf[i + skip :]
                    if line.strip():
                        _handle_line(line)
            if buf.strip():
                _handle_line(buf)
        except Exception as e:
            log(f"demucs progress reader: {e}")
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

    rt = threading.Thread(target=reader, daemon=True, name="demucs-progress")
    rt.start()
    try:
        rc = proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except Exception:
            pass
        rt.join(timeout=2)
        raise RuntimeError(f"demucs timed out after {timeout_secs}s")
    rt.join(timeout=5)
    if rc != 0:
        with lines_lock:
            err = "".join(last_lines).strip()
        if len(err) > 800:
            err = err[-800:]
        log(f"demucs failed (exit {rc}): {err[-400:] or 'unknown'}")
        raise RuntimeError(friendly_demucs_error(err, "demucs") + f" (exit {rc})")  # v6.56: no raw traceback
    stem = _find_no_vocals(work_dir)
    if not stem:
        raise RuntimeError("demucs finished but no_vocals stem not found")
    return stem


def _unique_sibling_dest(src_name, kind):
    """Pick REC_DIR / <stem>-{kind}.ogg (or -{kind}-N) that does not already exist as ogg/mp3/m4a/wav."""
    if kind not in ("music", "vocals"):
        raise ValueError("kind must be music or vocals")
    stem = music_base_stem(src_name)

    def taken(base_stem):
        for ext in (".ogg", ".mp3", ".m4a", ".wav"):
            if (REC_DIR / f"{base_stem}{ext}").exists():
                return True
        return False

    if not taken(f"{stem}-{kind}"):
        return REC_DIR / f"{stem}-{kind}.ogg"
    n = 2
    while n < 1000:
        if not taken(f"{stem}-{kind}-{n}"):
            return REC_DIR / f"{stem}-{kind}-{n}.ogg"
        n += 1
    return REC_DIR / f"{stem}-{kind}-{int(time.time())}.ogg"


def _unique_music_dest(src_name):
    return _unique_sibling_dest(src_name, "music")


def _unique_vocals_dest(src_name):
    return _unique_sibling_dest(src_name, "vocals")


def _music_job_is_cancelled(job_id):
    """True if job missing, cancelRequested, or already status=cancelled."""
    with MUSIC_JOBS_LOCK:
        job = MUSIC_JOBS.get(job_id)
        if not job:
            return True
        if job.get("cancelRequested"):
            return True
        return (job.get("status") or "") == "cancelled"


def _finalize_cancelled_music_job(job_id):
    """Ensure cancelled terminal fields (idempotent if cancel API already set them)."""
    with MUSIC_JOBS_LOCK:
        job = MUSIC_JOBS.get(job_id)
        if not job:
            return
        if (job.get("status") or "") != "cancelled":
            job["status"] = "cancelled"
            job["progress"] = "Cancelled"
            job["progressPct"] = job.get("progressPct") if job.get("progressPct") is not None else 0
            job["finishedAt"] = time.time()
        elif not job.get("finishedAt"):
            job["finishedAt"] = time.time()
        job["cancelRequested"] = True


def music_only_worker(job_id, src_path):
    src_path = Path(src_path)
    work = None
    slot_held = False
    quality = _job_get(job_id, "quality") or "better"  # v6.45
    try:
        # v6.28: stay queued until a demucs slot is free (one at a time).
        # v6.29: waiting is interruptible — Cancel waiting / Clear waiting Music.
        # v6.45: Fast (ffmpeg) jobs wait in the same one-at-a-time line.
        _set_music_job(
            job_id,
            status="queued",
            progress=voice_wait_msg(quality),
            progressPct=0,
        )
        while True:
            if _music_job_is_cancelled(job_id):
                _finalize_cancelled_music_job(job_id)
                log(f"music-only cancelled (before slot) job={job_id}")
                return
            got = DEMUCS_SLOTS.acquire(timeout=0.5)
            if not got:
                continue
            slot_held = True
            # Cancelled while waiting — release immediately, never start demucs.
            if _music_job_is_cancelled(job_id):
                try:
                    DEMUCS_SLOTS.release()
                except Exception:
                    pass
                slot_held = False
                _finalize_cancelled_music_job(job_id)
                log(f"music-only cancelled (after slot) job={job_id}")
                return
            break
        _set_music_job(
            job_id, status="running", progress="Preparing…", progressPct=3
        )
        REC_DIR.mkdir(parents=True, exist_ok=True)
        # Prefer recordings dir for temp (same volume); fall back to system tmp.
        try:
            work = Path(tempfile.mkdtemp(prefix="music-only-", dir=str(REC_DIR)))
        except OSError:
            work = Path(tempfile.mkdtemp(prefix="music-only-"))
        _register_work_path(work)  # v6.35: protect from diskWarn orphan cleanup

        def progress(msg, pct=None):
            # pct=None → update text only (hold last progressPct).
            if pct is not None:
                _set_music_job(job_id, progress=msg, progressPct=pct)
            else:
                _set_music_job(job_id, progress=msg)

        if quality == "fast":
            # v6.45: Twitchy Too Fast path — ffmpeg center-cancel, no demucs, -music only.
            info = probe_media(src_path)
            parts = voice_removed_parts(job_id, [src_path], work, info["duration"],
                                        progress, "fast", pct_lo=5.0, pct_hi=88.0)
            tmp_out = encode_joined_ogg(job_id, parts, work / "out.ogg", info["duration"],
                                        progress, pct_lo=88.0, pct_hi=98.0)
            final = atomic_publish(tmp_out, _unique_music_dest(src_path.name))
            vocals_wav = None
            out_name = final.name
        else:
            stem_wav = _run_demucs(src_path, work, progress)
            vocals_wav = _find_vocals(work)
            progress("Encoding instrumental…", pct=90)
            # Native source is never overwritten — sibling -music / -vocals only.
            provisional = _unique_music_dest(src_path.name)
            final = _remux_instrumental(stem_wav, provisional)
            out_name = final.name
        url = "/api/download/" + urllib.parse.quote(out_name)
        vocals_name = None
        vocals_url = None
        if vocals_wav is not None:
            try:
                progress("Encoding vocals…", pct=95)
                v_prov = _unique_vocals_dest(src_path.name)
                v_final = _remux_instrumental(vocals_wav, v_prov)
                vocals_name = v_final.name
                vocals_url = "/api/download/" + urllib.parse.quote(vocals_name)
            except Exception as ve:
                log(f"music-only vocals remux skipped job={job_id}: {ve}")
        _set_music_job(
            job_id,
            status="done",
            progress="Done",
            progressPct=100,
            outputName=out_name,
            outputUrl=url,
            vocalsName=vocals_name,
            vocalsUrl=vocals_url,
            error=None,
            finishedAt=time.time(),
        )
        log(
            f"music-only done job={job_id} quality={_job_get(job_id, 'quality')} out={out_name}"
            + (f" vocals={vocals_name}" if vocals_name else "")
        )
    except _JobCancelled:
        _finalize_cancelled_music_job(job_id)
        log(f"music-only cancelled job={job_id}")
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"music-only error job={job_id}: {msg}")
        # Leave last progressPct on error
        _set_music_job(
            job_id,
            status="error",
            progress="Error",
            error=msg,
            finishedAt=time.time(),
        )
    finally:
        if slot_held:
            try:
                DEMUCS_SLOTS.release()
            except Exception:
                pass
        if work is not None:
            try:
                shutil.rmtree(work, ignore_errors=True)
            except Exception:
                pass
            _unregister_work_path(work)


def start_music_only(raw_name, redo=False, quality=None):
    """Validate basename under REC_DIR, refuse if missing/recording, start background job.

    Native source is never deleted or overwritten. Output is always a sibling
    <stem>-music.* (and optionally <stem>-vocals.*).

    redo=False (default): if <stem>-music.* already exists, return already:true
    without re-running demucs.
    redo=True: delete prior -music / -vocals siblings, then re-run.

    Active/growing ACTIVE.file paths still return 409. Finished mid-resume
    segments (in sessionSegments, no longer the active path) are always eligible.
    """
    target = resolve_download_path(raw_name)
    if not target:
        return {"ok": False, "error": "not found"}, 404
    if is_voice_removed_name(target.name):
        return {
            "ok": False,
            "error": "pick the native recording — not a -music / -vocals / -novoice export",
        }, 400
    abspath = os.path.abspath(str(target))
    with LOCK:
        reap_locked()
        busy = any(
            (rec.get("file") or "")
            and os.path.abspath(rec.get("file") or "") == abspath
            for rec in ACTIVE.values()
        )
    if busy:
        return {
            "ok": False,
            "error": "still recording — stop first or wait for this segment to finish",
        }, 409

    existing_music = find_existing_sibling(target.name, "music")
    existing_vocals = find_existing_sibling(target.name, "vocals")
    if existing_music and not redo:
        out_name = existing_music.name
        payload = {
            "ok": True,
            "already": True,
            "outputName": out_name,
            "outputUrl": "/api/download/" + urllib.parse.quote(out_name),
        }
        if existing_vocals:
            payload["vocalsName"] = existing_vocals.name
            payload["vocalsUrl"] = (
                "/api/download/" + urllib.parse.quote(existing_vocals.name)
            )
        return payload, 200

    # Reuse in-flight job for the same native file (refresh / double-click).
    with MUSIC_JOBS_LOCK:
        for jid, job in MUSIC_JOBS.items():
            if (
                (job.get("name") or "") == target.name
                or target.name in (job.get("segments") or [])  # v6.36 stream pipeline covers it
            ) and (job.get("status") or "") in (
                "queued",
                "running",
            ):
                return {
                    "ok": True,
                    "jobId": jid,
                    "alreadyRunning": True,
                    "redo": bool(job.get("redo")),
                }, 200

    # v6.45: Better (demucs) or Fast (ffmpeg) — only fail when neither can run.
    eff_q, q_note = resolve_voice_quality(quality)
    if not eff_q:
        return {
            "ok": False,
            "error": q_note,
            "demucs": demucs_available(),
        }, 400

    # v6.30: refuse new Music only / Demucs when disk almost full (diskBlock).
    # diskWarn alone still allows manual Music only / Convert; Auto Music is
    # soft-skipped client-side.
    disk_err = disk_block_error()
    if disk_err:
        free, _total = disk_usage_for_rec_dir()
        mb = (free or 0) / (1024 * 1024)
        return {
            "ok": False,
            "error": (
                f"disk almost full ({mb:.0f} MB free) — "
                f"free space before Music only / Demucs"
            ),
            "diskBlock": True,
        }, 507

    if redo:
        delete_music_siblings(target.name)

    job_id = uuid.uuid4().hex[:12]
    with MUSIC_JOBS_LOCK:
        ahead = sum(
            1
            for jid, job in MUSIC_JOBS.items()
            if (job.get("status") or "") in ("queued", "running")
        )
        MUSIC_JOBS[job_id] = {
            "jobId": job_id,
            "status": "queued",
            "progress": (
                voice_wait_msg(eff_q)
                if ahead
                else "Queued…"
            ),
            "progressPct": 0,
            "error": None,
            "name": target.name,
            "outputName": None,
            "outputUrl": None,
            "vocalsName": None,
            "vocalsUrl": None,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "redo": bool(redo),
            "quality": eff_q,  # v6.45
            "qualityNote": q_note,
        }
    t = threading.Thread(
        target=music_only_worker,
        args=(job_id, str(target)),
        daemon=True,
        name=f"music-only-{job_id}",
    )
    t.start()
    log(
        f"music-only started job={job_id} file={target.name} redo={bool(redo)} quality={eff_q}"
        + (f" waitingBehind={ahead}" if ahead else "")
    )
    return {
        "ok": True,
        "jobId": job_id,
        "redo": bool(redo),
        "waitingBehind": bool(ahead),
        "queueAhead": int(ahead),
        "quality": eff_q,
        "qualityNote": q_note,
    }, 200


def music_only_status(job_id):
    if not job_id or not isinstance(job_id, str):
        return {"ok": False, "error": "invalid jobId"}, 400
    job_id = job_id.strip()
    if not job_id or "/" in job_id or "\\" in job_id or len(job_id) > 64:
        return {"ok": False, "error": "invalid jobId"}, 400
    with MUSIC_JOBS_LOCK:
        job = MUSIC_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}, 404
        pct = job.get("progressPct")
        if pct is not None:
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = None
        payload = {
            "ok": True,
            "jobId": job_id,
            "status": job.get("status") or "queued",
            "progress": job.get("progress") or "",
            "progressPct": pct,
            "error": job.get("error"),
            "name": job.get("name"),
            "outputName": job.get("outputName"),
            "outputUrl": job.get("outputUrl"),
            "vocalsName": job.get("vocalsName"),
            "vocalsUrl": job.get("vocalsUrl"),
        }
        payload.update(_drive_job_extra(job))
    return payload, 200


def cancel_music_only(body):
    """Cancel waiting (queued) Music only jobs — never kill a running demucs.

    Body:
      { "jobId": "..." } — cancel that job if still queued
      { "waiting": true } — cancel all currently queued jobs
    """
    if not isinstance(body, dict):
        body = {}
    job_id = body.get("jobId")
    waiting_all = bool(body.get("waiting"))
    if not waiting_all and not job_id:
        return {
            "ok": False,
            "error": "provide jobId or waiting:true",
            "cancelled": [],
            "skipped": [],
        }, 400

    cancelled = []
    skipped = []
    now = time.time()

    # v6.35: Drive / link jobs can be cancelled in any phase (download, chunk, encode).
    if job_id and not waiting_all:
        jid = str(job_id).strip()
        with MUSIC_JOBS_LOCK:
            is_drive = (MUSIC_JOBS.get(jid) or {}).get("source") in ("drive", "link")
        if is_drive:
            if cancel_drive_job(jid):
                return {"ok": True, "cancelled": [jid], "skipped": []}, 200
            return {"ok": True, "cancelled": [], "skipped": [{"jobId": jid, "reason": "not active"}]}, 200

    with MUSIC_JOBS_LOCK:
        if waiting_all:
            keep_pipe = bool(body.get("keepPipeline"))  # v6.36: auto pipeline pauses instead
            targets = [
                jid
                for jid, job in MUSIC_JOBS.items()
                if (job.get("status") or "") == "queued"
                and not (keep_pipe and job.get("source") == "pipeline")
            ]
        else:
            job_id = str(job_id).strip()
            if (
                not job_id
                or "/" in job_id
                or "\\" in job_id
                or len(job_id) > 64
            ):
                return {
                    "ok": False,
                    "error": "invalid jobId",
                    "cancelled": [],
                    "skipped": [],
                }, 400
            targets = [job_id]

        for jid in targets:
            job = MUSIC_JOBS.get(jid)
            if not job:
                skipped.append({"jobId": jid, "reason": "not found"})
                continue
            st = job.get("status") or ""
            if st == "queued":
                job["cancelRequested"] = True
                job["status"] = "cancelled"
                job["progress"] = "Cancelled"
                job["finishedAt"] = now
                cancelled.append(jid)
            elif st == "running":
                skipped.append({"jobId": jid, "reason": "already running"})
            elif st == "cancelled":
                skipped.append({"jobId": jid, "reason": "already cancelled"})
            elif st == "done":
                skipped.append({"jobId": jid, "reason": "already done"})
            elif st == "error":
                skipped.append({"jobId": jid, "reason": "already error"})
            else:
                skipped.append({"jobId": jid, "reason": f"status={st}"})

    if cancelled:
        log(
            f"music-only cancel: cancelled={cancelled}"
            + (f" skipped={skipped}" if skipped else "")
        )
    return {"ok": True, "cancelled": cancelled, "skipped": skipped}, 200


def seamless_job_snapshot(job):
    pct = job.get("progressPct")
    if pct is not None:
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = None
    return {
        "jobId": job.get("jobId"),
        "name": job.get("name"),
        "status": job.get("status") or "queued",
        "progress": job.get("progress") or "",
        "progressPct": pct,
        "error": job.get("error"),
        "outputName": job.get("outputName"),
        "names": list(job.get("names") or []),
        "startedAt": job.get("startedAt"),
    }


def seamless_jobs_for_health():
    now = time.time()
    out = []
    dead = []
    with SEAMLESS_JOBS_LOCK:
        for jid, job in SEAMLESS_JOBS.items():
            st = job.get("status") or ""
            if st in ("done", "error"):
                finished_at = float(job.get("finishedAt") or 0.0)
                if finished_at and (now - finished_at) > SEAMLESS_JOB_KEEP_FINISHED_SECS:
                    dead.append(jid)
                    continue
            out.append(seamless_job_snapshot(job))
        for jid in dead:
            SEAMLESS_JOBS.pop(jid, None)
    return out


def _set_seamless_job(job_id, **kwargs):
    with SEAMLESS_JOBS_LOCK:
        job = SEAMLESS_JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)


def _unique_seamless_dest(first_name):
    """<firstSegmentStem>-seamless.<ext> next to recordings; never overwrite natives."""
    stem = Path(first_name).stem
    ext = Path(first_name).suffix.lower() or ".ogg"
    if is_seamless_export_name(first_name):
        # Avoid -seamless-seamless
        import re as _re
        stem = _re.sub(r"-seamless(?:-\d+)?$", "", stem)
    base = f"{stem}-seamless{ext}"
    candidate = REC_DIR / base
    if not candidate.exists():
        return candidate
    n = 2
    while n < 1000:
        candidate = REC_DIR / f"{stem}-seamless-{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1
    return REC_DIR / f"{stem}-seamless-{int(time.time())}{ext}"


def _probe_same_codecs(paths):
    """Best-effort: True if ffmpeg can concat-copy; None if unknown."""
    # We try copy first and fall back — no probe required.
    return None


def _run_ffmpeg_concat(paths, dest, progress_cb):
    ff = which_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not installed — needed for seamless join")
    REC_DIR.mkdir(parents=True, exist_ok=True)
    list_path = None
    try:
        # concat demuxer list — paths are absolute under REC_DIR
        fd, list_name = tempfile.mkstemp(prefix="seamless-", suffix=".txt", dir=str(REC_DIR))
        os.close(fd)
        list_path = Path(list_name)
        lines = []
        for p in paths:
            # ffmpeg concat demuxer: escape single quotes
            ap = str(p.resolve()).replace("'", r"'\''")
            lines.append(f"file '{ap}'")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        def run(copy_mode):
            cmd = [ff, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path)]
            if copy_mode:
                progress_cb("Joining (stream copy)…")
                cmd += ["-c", "copy", str(dest)]
            else:
                progress_cb("Joining (remux/re-encode)…")
                # Prefer remux video + AAC audio; if that fails caller retries? Single remux attempt.
                cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest)]
            env = os.environ.copy()
            env["PYTHONWARNINGS"] = "ignore"
            return subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                timeout=None,
                env=env,
            )

        r = run(True)
        if r.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            return dest
        err1 = (r.stderr or b"").decode("utf-8", errors="replace")[-600:]
        log(f"seamless copy failed: {err1}")
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        r2 = run(False)
        if r2.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            return dest
        err2 = (r2.stderr or b"").decode("utf-8", errors="replace")[-600:]
        log(f"seamless remux failed: {err2}")
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        # Last resort: re-encode both streams so mismatched codecs still join.
        progress_cb("Joining (re-encode)…")
        cmd3 = [
            ff, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dest),
        ]
        env = os.environ.copy()
        env["PYTHONWARNINGS"] = "ignore"
        r3 = subprocess.run(
            cmd3,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            timeout=None,
            env=env,
        )
        if r3.returncode == 0 and dest.is_file() and dest.stat().st_size > 0:
            return dest
        err3 = (r3.stderr or b"").decode("utf-8", errors="replace")[-600:]
        raise RuntimeError(f"ffmpeg concat failed: {err3 or err2 or err1 or 'unknown'}")
    finally:
        if list_path is not None:
            try:
                list_path.unlink()
            except OSError:
                pass


def seamless_worker(job_id, names):
    try:
        _set_seamless_job(
            job_id, status="running", progress="Preparing…", progressPct=5
        )
        paths = []
        for name in names:
            target = resolve_download_path(name)
            if not target:
                raise RuntimeError(f"not found: {name}")
            if is_music_export_name(name) or is_seamless_export_name(name):
                raise RuntimeError(f"skip export sibling: {name}")
            abspath = os.path.abspath(str(target))
            with LOCK:
                reap_locked()
                busy = any(
                    (rec.get("file") or "")
                    and os.path.abspath(rec.get("file") or "") == abspath
                    for rec in ACTIVE.values()
                )
            if busy:
                raise RuntimeError(
                    f"still recording — wait for segment to finish: {name}"
                )
            if target.stat().st_size <= SCRUB_MAX_BYTES:
                raise RuntimeError(f"segment too small / empty: {name}")
            paths.append(target)

        if len(paths) < 2:
            raise RuntimeError("need at least 2 finished segments to join")

        def progress(msg, pct=None):
            if pct is not None:
                _set_seamless_job(job_id, progress=msg, progressPct=pct)
            else:
                # Default join phase ~50 when ffmpeg reports status without pct
                _set_seamless_job(job_id, progress=msg, progressPct=50)

        dest = _unique_seamless_dest(paths[0].name)
        _run_ffmpeg_concat(paths, dest, progress)
        out_name = dest.name
        _set_seamless_job(
            job_id,
            status="done",
            progress="Done",
            progressPct=100,
            outputName=out_name,
            outputUrl="/api/download/" + urllib.parse.quote(out_name),
            error=None,
            finishedAt=time.time(),
        )
        log(f"seamless done job={job_id} out={out_name} n={len(paths)}")
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"seamless error job={job_id}: {msg}")
        _set_seamless_job(
            job_id,
            status="error",
            progress="Error",
            error=msg,
            finishedAt=time.time(),
        )


def start_seamless(body):
    """Join finished segment files into <firstStem>-seamless.<ext>.

    Body: { "names": ["a.ogg", "b.ogg", ...] }
       or { "username": "foo" } using RECENT_SESSIONS / ACTIVE finished segments.
    Never overwrites native segment files. Requires ffmpeg.
    """
    body = body if isinstance(body, dict) else {}
    names = body.get("names")
    username = safe_username(body.get("username")) if body.get("username") else None

    if not names and username:
        prune_recent_sessions()
        with LOCK:
            reap_locked()
            rec = ACTIVE.get(username)
            if rec:
                # Finished only — exclude still-growing active file
                names = list(rec.get("sessionSegments") or [])
            else:
                info = RECENT_SESSIONS.get(username) or {}
                names = list(info.get("segments") or [])

    if not isinstance(names, list) or not names:
        return {"ok": False, "error": "names[] or username with session segments required"}, 400

    clean = []
    seen = set()
    for raw in names:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name or name in seen:
            continue
        if "/" in name or "\\" in name:
            return {"ok": False, "error": f"invalid name: {name}"}, 400
        if is_music_export_name(name) or is_seamless_export_name(name):
            continue
        seen.add(name)
        clean.append(name)

    if len(clean) < 2:
        return {
            "ok": False,
            "error": "need at least 2 finished segment files to join (got %d)" % len(clean),
        }, 400

    # Validate all exist and none are actively recording
    for name in clean:
        target = resolve_download_path(name)
        if not target:
            return {"ok": False, "error": f"not found: {name}"}, 404
        abspath = os.path.abspath(str(target))
        with LOCK:
            reap_locked()
            busy = any(
                (rec.get("file") or "")
                and os.path.abspath(rec.get("file") or "") == abspath
                for rec in ACTIVE.values()
            )
        if busy:
            return {
                "ok": False,
                "error": f"still recording — wait for segment to finish: {name}",
            }, 409

    # Reuse in-flight job for the same ordered name list
    key = "|".join(clean)
    with SEAMLESS_JOBS_LOCK:
        for jid, job in SEAMLESS_JOBS.items():
            if (job.get("status") or "") in ("queued", "running"):
                if "|".join(job.get("names") or []) == key:
                    return {
                        "ok": True,
                        "jobId": jid,
                        "alreadyRunning": True,
                    }, 200

    if not seamless_available():
        return {
            "ok": False,
            "error": "ffmpeg not installed — needed for seamless join",
            "ffmpeg": False,
            "seamless": False,
        }, 400

    # If seamless sibling already exists for this set's first stem, return already
    first = clean[0]
    stem = Path(first).stem
    ext = Path(first).suffix.lower() or ".ogg"
    existing = REC_DIR / f"{stem}-seamless{ext}"
    if existing.is_file():
        return {
            "ok": True,
            "already": True,
            "outputName": existing.name,
            "outputUrl": "/api/download/" + urllib.parse.quote(existing.name),
        }, 200

    job_id = uuid.uuid4().hex[:12]
    with SEAMLESS_JOBS_LOCK:
        SEAMLESS_JOBS[job_id] = {
            "jobId": job_id,
            "status": "queued",
            "progress": "Queued…",
            "progressPct": 0,
            "error": None,
            "name": first,  # key for UI map (like musicJobs[name])
            "names": list(clean),
            "outputName": None,
            "outputUrl": None,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    t = threading.Thread(
        target=seamless_worker,
        args=(job_id, list(clean)),
        daemon=True,
        name=f"seamless-{job_id}",
    )
    t.start()
    log(f"seamless started job={job_id} n={len(clean)} first={first}")
    return {"ok": True, "jobId": job_id, "names": clean}, 200


def seamless_status(job_id):
    if not job_id or not isinstance(job_id, str):
        return {"ok": False, "error": "invalid jobId"}, 400
    job_id = job_id.strip()
    if not job_id or "/" in job_id or "\\" in job_id or len(job_id) > 64:
        return {"ok": False, "error": "invalid jobId"}, 400
    with SEAMLESS_JOBS_LOCK:
        job = SEAMLESS_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "job not found"}, 404
        pct = job.get("progressPct")
        if pct is not None:
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                pct = None
        payload = {
            "ok": True,
            "jobId": job_id,
            "status": job.get("status") or "queued",
            "progress": job.get("progress") or "",
            "progressPct": pct,
            "error": job.get("error"),
            "name": job.get("name"),
            "names": list(job.get("names") or []),
            "outputName": job.get("outputName"),
            "outputUrl": job.get("outputUrl"),
        }
    return payload, 200


def _unique_upload_name(original_name):
    """Save upload under REC_DIR with a safe unique basename."""
    import re as _re
    base = Path(original_name or "upload").name
    base = _re.sub(r"[^\w.\-]+", "_", base).strip("._") or "upload"
    if len(base) > 180:
        stem, ext = Path(base).stem[:140], Path(base).suffix[:20]
        base = stem + ext
    ext = Path(base).suffix.lower()
    if ext not in MUSIC_UPLOAD_EXTS:
        base = base + ".bin"
        # reject later by ext check
    dest = REC_DIR / base
    if not dest.exists():
        return dest
    stem = Path(base).stem
    suf = Path(base).suffix
    n = 2
    while n < 1000:
        dest = REC_DIR / f"{stem}-{n}{suf}"
        if not dest.exists():
            return dest
        n += 1
    return REC_DIR / f"{stem}-{int(time.time())}{suf}"


def parse_multipart_file(handler, max_bytes=MUSIC_UPLOAD_MAX_BYTES):
    """Parse multipart/form-data and return (filename, data_bytes) for field 'file'."""
    ctype = handler.headers.get("Content-Type") or ""
    if "multipart/form-data" not in ctype.lower():
        return None, "expected multipart/form-data"
    import re as _re
    m = _re.search(r"boundary=([^;\s]+)", ctype, _re.I)
    if not m:
        return None, "missing multipart boundary"
    boundary = m.group(1).strip().strip('"')
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        return None, "empty upload"
    if length > max_bytes + 1024 * 1024:  # allow multipart overhead
        return None, f"upload too large (max {max_bytes // (1024*1024)} MB)"
    raw = handler.rfile.read(length)
    if not raw:
        return None, "empty upload"
    delim = b"--" + boundary.encode("utf-8")
    parts = raw.split(delim)
    for part in parts:
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"--"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"\n"):
            part = part[1:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        elif part.endswith(b"\n"):
            part = part[:-1]
        if b"\r\n\r\n" in part:
            header_blob, body = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            header_blob, body = part.split(b"\n\n", 1)
        else:
            continue
        headers = header_blob.decode("utf-8", errors="replace")
        if "filename=" not in headers.lower():
            continue
        # Prefer name="file"
        name_m = _re.search(r'name="([^"]+)"', headers, _re.I)
        field = name_m.group(1) if name_m else ""
        fn_m = _re.search(r'filename="([^"]*)"', headers, _re.I)
        if not fn_m:
            fn_m = _re.search(r"filename=([^;\s]+)", headers, _re.I)
        filename = (fn_m.group(1) if fn_m else "").strip()
        if field and field != "file" and not filename:
            continue
        if body.endswith(b"\r\n"):
            body = body[:-2]
        elif body.endswith(b"\n"):
            body = body[:-1]
        if len(body) > max_bytes:
            return None, f"upload too large (max {max_bytes // (1024*1024)} MB)"
        if len(body) == 0:
            return None, "empty file"
        return {"filename": filename or "upload.bin", "data": body}, None
    return None, "no file field found (use form field name=file)"


def save_music_upload(filename, data):
    """Write upload bytes under REC_DIR; return Path or raise."""
    ext = Path(filename or "").suffix.lower()
    if ext not in MUSIC_UPLOAD_EXTS:
        raise ValueError(
            "unsupported type — use mp4/mkv/ts/mp3/m4a/wav/webm/ogg/flac/mov"
        )
    if not data:
        raise ValueError("empty file")
    if len(data) > MUSIC_UPLOAD_MAX_BYTES:
        raise ValueError(f"upload too large (max {MUSIC_UPLOAD_MAX_BYTES // (1024*1024)} MB)")
    REC_DIR.mkdir(parents=True, exist_ok=True)
    dest = _unique_upload_name(filename)
    # Ensure extension preserved/valid
    if dest.suffix.lower() not in MUSIC_UPLOAD_EXTS:
        dest = dest.with_suffix(ext)
        if dest.exists():
            dest = _unique_upload_name(dest.name)
    dest.write_bytes(data)
    return dest


# ─────────────────────────────────────────────────────────────────────────────
# v6.35: Google Drive → Music only (remove voiceover)
#   A) Drive for Desktop (~/Library/CloudStorage/GoogleDrive-*/, /Volumes/GoogleDrive)
#   B) Paste a public Drive link (drive.usercontent.google.com confirm=t flow)
# Both feed the same MUSIC_JOBS queue / DEMUCS_SLOTS (one demucs at a time).
# Demucs runs on ~10 min chunks (htdemucs, --two-stems=vocals, segment 7, -j 1);
# no_vocals stems are joined in order and encoded to OGG.
# ─────────────────────────────────────────────────────────────────────────────
import re as _re_drive
import urllib.error  # noqa: E402

DRIVE_INSTALL_URL = "https://www.google.com/drive/download/"
DRIVE_MEDIA_EXTS = set(MUSIC_UPLOAD_EXTS) | {
    ".opus", ".aif", ".aiff", ".wma", ".mpeg", ".mpg", ".3gp", ".caf", ".mka",
}
DRIVE_MUSIC_SUBFOLDER = "Music only"
DRIVE_OUTPUT_MODES = ("subfolder", "sibling", "recordings")
DRIVE_TMP_DIR = REC_DIR / ".drive-tmp"
DRIVE_HISTORY_FILE = REC_DIR / ".drive-history.json"
DRIVE_HISTORY_MAX = 200
DRIVE_HISTORY_LOCK = threading.Lock()
DEMUCS_CHUNK_SECS = max(5, int(os.environ.get("TWITCH_RECORDER_CHUNK_SECS") or 600))
DEMUCS_SEGMENT = "7"
DRIVE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) TwitchAutoRecorder/{HELPER_VERSION}"
)
# Work dirs / temp files that belong to live jobs — orphan cleanup must skip them.
ACTIVE_WORK_PATHS = set()
ACTIVE_WORK_LOCK = threading.Lock()
# jobId -> running subprocess (Drive/link jobs only) so Cancel can stop it.
DRIVE_JOB_PROCS = {}
# Remember a failed MPS device so later chunks/jobs go straight to CPU.
_DEMUCS_DEVICE_STATE = {"mps_failed": False}
# Browser origins allowed to call Drive / reveal endpoints (Drive file names are private).
DRIVE_ALLOWED_ORIGINS = {
    "null",  # file:// page
    "https://ewanders1-web.github.io",
}


class _JobCancelled(Exception):
    pass


def _register_work_path(p):
    with ACTIVE_WORK_LOCK:
        ACTIVE_WORK_PATHS.add(os.path.abspath(str(p)))


def _unregister_work_path(p):
    with ACTIVE_WORK_LOCK:
        ACTIVE_WORK_PATHS.discard(os.path.abspath(str(p)))


def _is_active_work_path(p):
    with ACTIVE_WORK_LOCK:
        return os.path.abspath(str(p)) in ACTIVE_WORK_PATHS


def origin_allowed(origin):
    """Localhost / file:// / GitHub Pages only (or no Origin header, e.g. curl)."""
    if not origin:
        return True
    o = origin.strip().rstrip("/")
    if o in DRIVE_ALLOWED_ORIGINS:
        return True
    try:
        u = urllib.parse.urlparse(o)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and (u.hostname or "") in ("127.0.0.1", "localhost")


# ── Drive for Desktop detection / safe paths ────────────────────────────────

def _cloudstorage_bases():
    env = os.environ.get("TWITCH_RECORDER_CLOUDSTORAGE")
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    return [Path.home() / "Library" / "CloudStorage"]


def _legacy_drive_volumes():
    env = os.environ.get("TWITCH_RECORDER_DRIVE_VOLUMES")
    if env is not None:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    return [Path("/Volumes/GoogleDrive")]


def windows_drive_candidates(env=None, is_dir=None, letters="DEFGHIJKLMNOPQRSTUVWXYZ"):
    """v6.38: Google Drive for Desktop locations on Windows (pure, unit-testable).

    * Streaming mode: a drive letter (G: by default, any letter) whose root holds
      "My Drive" and/or "Shared drives"  → root "G:\\".
    * Mirror / legacy Backup & Sync: %USERPROFILE%\\Google Drive (may itself contain
      "My Drive") and %USERPROFILE%\\My Drive.
    Returns [{label, path, account, kind, shortcuts}] using Windows path syntax.
    """
    env = os.environ if env is None else env
    is_dir = is_dir or os.path.isdir
    out = []
    for letter in letters:
        root = f"{letter}:\\"
        subs = [sc for sc in ("My Drive", "Shared drives") if is_dir(ntpath.join(root, sc))]
        if subs:
            out.append({"label": f"Google Drive ({letter}:)", "path": root, "account": "",
                        "kind": "windows-letter",
                        "shortcuts": [{"label": sc, "path": ntpath.join(root, sc)} for sc in subs]})
    prof = env.get("USERPROFILE") or ""
    if prof:
        gd = ntpath.join(prof, "Google Drive")
        if is_dir(gd):
            inner = ntpath.join(gd, "My Drive")
            md = inner if is_dir(inner) else gd
            out.append({"label": "Google Drive (folder)", "path": gd, "account": "", "kind": "windows-folder",
                        "shortcuts": [{"label": "My Drive", "path": md}]})
        mdf = ntpath.join(prof, "My Drive")
        if is_dir(mdf):
            out.append({"label": "Google Drive (My Drive folder)", "path": mdf, "account": "",
                        "kind": "windows-folder", "shortcuts": [{"label": "My Drive", "path": mdf}]})
    return out


def detect_drive_roots():
    """Return list of {label, path, account, kind, shortcuts:[{label,path}]} (real paths)."""
    roots = []
    seen = set()
    if os.name == "nt":  # v6.38
        try:
            for r in windows_drive_candidates():
                real = os.path.realpath(r["path"])
                if os.path.normcase(real) in seen:
                    continue
                seen.add(os.path.normcase(real))
                r["path"] = real
                r["shortcuts"] = [{"label": sc["label"], "path": os.path.realpath(sc["path"])} for sc in r["shortcuts"]]
                roots.append(r)
        except OSError:
            pass
    for base in _cloudstorage_bases():
        try:
            if not base.is_dir():
                continue
            children = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            try:
                if not child.name.startswith("GoogleDrive-") or not child.is_dir():
                    continue
                real = os.path.realpath(str(child))
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            account = child.name[len("GoogleDrive-"):]
            roots.append(
                {
                    "label": f"Google Drive ({account})" if account else "Google Drive",
                    "path": real,
                    "account": account,
                    "kind": "cloudstorage",
                }
            )
    for vol in _legacy_drive_volumes():
        try:
            if not vol.is_dir():
                continue
            real = os.path.realpath(str(vol))
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        roots.append({"label": "Google Drive (legacy volume)", "path": real, "account": "", "kind": "volume"})
    for r in roots:
        if r.get("shortcuts") is not None:
            continue  # v6.38: Windows candidates come with their shortcuts
        shortcuts = []
        for sub in ("My Drive", "Shared drives"):
            p = Path(r["path"]) / sub
            try:
                if p.is_dir():
                    shortcuts.append({"label": sub, "path": os.path.realpath(str(p))})
            except OSError:
                continue
        r["shortcuts"] = shortcuts
    return roots


def _path_within(real, root):
    try:
        # v6.38: normcase so Windows drive letters / case differences compare equal
        r = os.path.normcase(os.path.normpath(root))
        return os.path.normcase(os.path.commonpath([real, root])) == r
    except ValueError:
        return False


def validate_drive_path(raw, roots=None):
    """Resolve raw absolute path; must stay inside a detected Drive root.

    Rejects relative paths, '..' components, NUL, and symlinks that escape.
    Returns (real_path_str, root_dict, None) or (None, None, error).
    """
    if roots is None:
        roots = detect_drive_roots()
    if not roots:
        return None, None, "Google Drive for Desktop not found"
    if not isinstance(raw, str) or not raw.strip():
        return None, None, "path required"
    s = raw.strip()
    if "\x00" in s:
        return None, None, "invalid path"
    if s.startswith("~"):
        s = os.path.expanduser(s)
    if not os.path.isabs(s):
        return None, None, "absolute path required"
    if any(part == ".." for part in Path(s).parts):
        return None, None, "'..' is not allowed in Drive paths"
    try:
        real = os.path.realpath(s)
    except OSError:
        return None, None, "invalid path"
    for r in roots:
        if _path_within(real, r["path"]):
            return real, r, None
    return None, None, "path is outside Google Drive"


def _drive_output_path(src_real, mode):
    stem = music_base_stem(Path(src_real).name)
    name = f"{stem}-music.ogg"
    if mode == "sibling":
        return Path(src_real).parent / name
    if mode == "recordings":
        return REC_DIR / name
    return Path(src_real).parent / DRIVE_MUSIC_SUBFOLDER / name


def _norm_out_mode(mode):
    return mode if mode in DRIVE_OUTPUT_MODES else "subfolder"


def drive_status_payload():
    roots = detect_drive_roots()
    return {
        "ok": True,
        "found": bool(roots),
        "roots": roots,
        "installUrl": DRIVE_INSTALL_URL,
        "platform": sys.platform,
        "outputModes": list(DRIVE_OUTPUT_MODES),
        "musicSubfolder": DRIVE_MUSIC_SUBFOLDER,
        "recordingsDir": str(REC_DIR),
        "chunkSecs": DEMUCS_CHUNK_SECS,
        "demucs": demucs_available(),
        "ffmpeg": bool(which_ffmpeg()),
    }


def drive_list_payload(raw_path, out_mode="subfolder"):
    roots = detect_drive_roots()
    out_mode = _norm_out_mode(out_mode)
    if not roots:
        return {
            "ok": False,
            "found": False,
            "error": "Google Drive for Desktop not found",
            "installUrl": DRIVE_INSTALL_URL,
        }, 404
    if not raw_path:
        # Virtual top level: My Drive / Shared drives per account (or the account root).
        dirs = []
        for r in roots:
            if r["shortcuts"]:
                for sc in r["shortcuts"]:
                    lbl = sc["label"] if len(roots) == 1 else f'{sc["label"]} — {r["account"] or r["label"]}'
                    dirs.append({"name": lbl, "path": sc["path"]})
            else:
                dirs.append({"name": r["label"], "path": r["path"]})
        return {
            "ok": True,
            "found": True,
            "path": "",
            "crumbs": [{"name": "Google Drive", "path": ""}],
            "dirs": dirs,
            "files": [],
            "outMode": out_mode,
        }, 200
    real, root, err = validate_drive_path(raw_path, roots)
    if err:
        return {"ok": False, "error": err}, 400
    if not os.path.isdir(real):
        return {"ok": False, "error": "folder not found"}, 404
    dirs = []
    files = []
    try:
        it = list(os.scandir(real))
    except OSError as e:
        return {"ok": False, "error": f"cannot list folder: {e}"}, 500
    for entry in it:
        name = entry.name
        if name.startswith(".") or name.startswith("~$") or name == "Icon\r":
            continue
        try:
            full = os.path.join(real, name)
            if entry.is_symlink():
                target = os.path.realpath(full)
                if not any(_path_within(target, r["path"]) for r in roots):
                    continue  # symlink escapes Drive — hide it
            if entry.is_dir(follow_symlinks=True):
                dirs.append({"name": name, "path": os.path.realpath(full)})
                continue
            if not entry.is_file(follow_symlinks=True):
                continue
            if Path(name).suffix.lower() not in DRIVE_MEDIA_EXTS:
                continue
            st = entry.stat(follow_symlinks=True)
        except OSError:
            continue
        real_file = os.path.realpath(full)
        is_export = is_music_export_name(name)
        music_path = None
        if not is_export:
            try:
                cand = _drive_output_path(real_file, out_mode)
                if cand.is_file():
                    music_path = str(cand)
            except OSError:
                music_path = None
        files.append(
            {
                "name": name,
                "path": real_file,
                "size": int(st.st_size),
                "mtime": int(st.st_mtime),
                "isMusicExport": is_export,
                "musicPath": music_path,
            }
        )
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    # Breadcrumbs: virtual top → root → each component.
    crumbs = [{"name": "Google Drive", "path": ""}]
    root_path = root["path"]
    crumbs.append({"name": root["label"], "path": root_path})
    rel = os.path.relpath(real, root_path)
    if rel != ".":
        acc = root_path
        for part in Path(rel).parts:
            acc = os.path.join(acc, part)
            crumbs.append({"name": part, "path": acc})
    return {
        "ok": True,
        "found": True,
        "path": real,
        "crumbs": crumbs,
        "dirs": dirs,
        "files": files,
        "outMode": out_mode,
    }, 200


# ── Drive link parsing / download ───────────────────────────────────────────

_DRIVE_ID_RE = r"[A-Za-z0-9_-]{10,}"
_DRIVE_HOSTS = ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")


def parse_drive_file_id(url):
    """Extract a Drive file id from /file/d/<id>/, open?id=, uc?id= links (or a bare id)."""
    s = (url or "").strip() if isinstance(url, str) else ""
    if not s:
        return None, "paste a Google Drive file link"
    if _re_drive.fullmatch(r"[A-Za-z0-9_-]{25,}", s):
        return s, None
    if not _re_drive.match(r"^https?://", s, _re_drive.I):
        s = "https://" + s
    try:
        u = urllib.parse.urlparse(s)
    except ValueError:
        return None, "not a valid link"
    host = (u.hostname or "").lower()
    if host not in _DRIVE_HOSTS:
        return None, "not a Google Drive link (expected drive.google.com/file/d/…)"
    m = _re_drive.search(r"/file/(?:u/\d+/)?d/(" + _DRIVE_ID_RE + ")", u.path)
    if m:
        return m.group(1), None
    q = urllib.parse.parse_qs(u.query)
    for key in ("id",):
        vals = q.get(key) or []
        if vals and _re_drive.fullmatch(_DRIVE_ID_RE, vals[0]):
            return vals[0], None
    if "/folders/" in u.path:
        return None, (
            "that is a folder link — paste a link to a single file, "
            "or use Drive for Desktop to browse the folder"
        )
    return None, "could not find a file id in that link"


def _content_disposition_filename(cd):
    if not cd:
        return None
    m = _re_drive.search(r"filename\*\s*=\s*([^']*)''([^;]+)", cd, _re_drive.I)
    if m:
        try:
            return urllib.parse.unquote(m.group(2).strip().strip('"'), encoding=m.group(1) or "utf-8")
        except (LookupError, ValueError):
            pass
    m = _re_drive.search(r'filename\s*=\s*"([^"]+)"', cd, _re_drive.I)
    if m:
        return m.group(1)
    m = _re_drive.search(r"filename\s*=\s*([^;]+)", cd, _re_drive.I)
    if m:
        return m.group(1).strip()
    return None


def _safe_download_name(name, file_id):
    n = (name or "").replace("\x00", "").strip()
    n = n.replace("/", "_").replace("\\", "_").replace(":", "_")
    n = n.lstrip(".").strip()
    if not n:
        n = f"drive-{file_id}"
    if len(n) > 180:
        stem, ext = os.path.splitext(n)
        n = stem[: 180 - len(ext)] + ext
    return n


class DrivePrivateError(RuntimeError):
    pass


DRIVE_PRIVATE_MSG = (
    "This Drive file is private (Google asked to sign in). Either use Drive for Desktop "
    "(browse it above), or set the file's sharing to \"Anyone with the link\" and try again."
)


def _looks_like_signin(html, final_url):
    low = (html or "").lower()
    fu = (final_url or "").lower()
    if "accounts.google.com" in fu or "servicelogin" in fu:
        return True
    return any(
        k in low
        for k in (
            "accounts.google.com/servicelogin",
            "accounts.google.com/v3/signin",
            "<title>sign in",
            "sign in - google accounts",
            "you need access",
            "request access",
        )
    )


def _drive_confirm_url_from_html(html, file_id):
    """Parse the virus-scan warning form (download-form) or a confirm= token."""
    m = _re_drive.search(r'<form[^>]+id="download-form"[^>]*action="([^"]+)"', html or "", _re_drive.I)
    if not m:
        m = _re_drive.search(r'<form[^>]+action="([^"]+)"[^>]*id="download-form"', html or "", _re_drive.I)
    if m:
        action = m.group(1).replace("&amp;", "&")
        params = {}
        for im in _re_drive.finditer(r"<input[^>]+>", html, _re_drive.I):
            tag = im.group(0)
            nm = _re_drive.search(r'name="([^"]+)"', tag)
            vm = _re_drive.search(r'value="([^"]*)"', tag)
            if nm and 'type="hidden"' in tag.lower():
                params[nm.group(1)] = (vm.group(1) if vm else "").replace("&amp;", "&")
        params.setdefault("id", file_id)
        params.setdefault("export", "download")
        params.setdefault("confirm", "t")
        if action.startswith("/"):
            action = "https://drive.usercontent.google.com" + action
        return action + ("&" if "?" in action else "?") + urllib.parse.urlencode(params)
    m = _re_drive.search(r"confirm=([0-9A-Za-z_-]+)", html or "")
    if m:
        u = (
            "https://drive.usercontent.google.com/download?"
            + urllib.parse.urlencode({"id": file_id, "export": "download", "confirm": m.group(1)})
        )
        um = _re_drive.search(r'name="uuid"\s+value="([^"]+)"', html or "")
        if um:
            u += "&uuid=" + urllib.parse.quote(um.group(1))
        return u
    return None


def drive_open_download(file_id, timeout=60):
    """Open a streaming response for a public Drive file. Raises DrivePrivateError / RuntimeError."""
    base = os.environ.get("TWITCH_RECORDER_DRIVE_DL_BASE") or "https://drive.usercontent.google.com/download"
    url = base + "?" + urllib.parse.urlencode({"id": file_id, "export": "download", "confirm": "t"})
    tried = set()
    for _attempt in range(3):
        tried.add(url)
        req = urllib.request.Request(url, headers={"User-Agent": DRIVE_USER_AGENT})
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read(200000).decode("utf-8", errors="replace")
            except Exception:
                pass
            if e.code in (401, 403) or _looks_like_signin(body, getattr(e, "url", "")):
                raise DrivePrivateError(DRIVE_PRIVATE_MSG)
            if e.code == 404:
                raise RuntimeError(
                    "Google Drive says the file was not found — check the link "
                    "(or it is private: use Drive for Desktop or \"Anyone with the link\")"
                )
            if e.code == 429:
                raise RuntimeError("Google Drive download quota exceeded for this file — try later or use Drive for Desktop")
            raise RuntimeError(f"Google Drive HTTP {e.code}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"could not reach Google Drive: {getattr(e, 'reason', e)}")
        ctype = (resp.headers.get("Content-Type") or "").lower()
        final = resp.geturl() or ""
        if "accounts.google.com" in final.lower():
            resp.close()
            raise DrivePrivateError(DRIVE_PRIVATE_MSG)
        if not ctype.startswith("text/html"):
            return resp
        html = resp.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        resp.close()
        if _looks_like_signin(html, final):
            raise DrivePrivateError(DRIVE_PRIVATE_MSG)
        low = html.lower()
        if "quota exceeded" in low or "too many users have viewed or downloaded" in low:
            raise RuntimeError("Google Drive download quota exceeded for this file — try later or use Drive for Desktop")
        nxt = _drive_confirm_url_from_html(html, file_id)
        if not nxt or nxt in tried:
            t = _re_drive.search(r"<title>([^<]{0,120})</title>", html, _re_drive.I)
            title = (t.group(1).strip() if t else "").replace("\n", " ")
            raise RuntimeError(
                "Google returned a web page instead of the file"
                + (f" ({title})" if title else "")
                + " — it may be private; use Drive for Desktop or \"Anyone with the link\""
            )
        url = nxt
    raise RuntimeError("Google Drive kept returning a confirmation page")


# ── job plumbing ─────────────────────────────────────────────────────────────

def _job_get(job_id, key, default=None):
    with MUSIC_JOBS_LOCK:
        job = MUSIC_JOBS.get(job_id) or {}
        return job.get(key, default)


def _check_cancel(job_id):
    if _music_job_is_cancelled(job_id):
        raise _JobCancelled()


def _job_run_proc(job_id, cmd, line_cb=None, env=None, timeout=6 * 3600):
    """Run a subprocess that Cancel can stop. Returns (rc, tail_text)."""
    _check_cancel(job_id)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        bufsize=0,
        env=env,
    )
    with MUSIC_JOBS_LOCK:
        DRIVE_JOB_PROCS[job_id] = proc
    tail = []

    def reader():
        buf = ""
        try:
            while True:
                chunk = proc.stdout.read(512)
                if not chunk:
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while True:
                    idx = [i for i in (buf.find("\n"), buf.find("\r")) if i >= 0]
                    if not idx:
                        break
                    i = min(idx)
                    line, buf = buf[:i], buf[i + 1:]
                    if line.strip():
                        tail.append(line)
                        if len(tail) > 120:
                            del tail[:-60]
                        if line_cb:
                            try:
                                line_cb(line)
                            except Exception:
                                pass
            if buf.strip():
                tail.append(buf)
                if line_cb:
                    try:
                        line_cb(buf)
                    except Exception:
                        pass
        except Exception:
            pass

    rt = threading.Thread(target=reader, daemon=True, name=f"drive-proc-{job_id}")
    rt.start()
    started = time.time()
    try:
        while proc.poll() is None:
            if _music_job_is_cancelled(job_id):
                terminate_proc(proc)
                rt.join(timeout=2)
                raise _JobCancelled()
            if time.time() - started > timeout:
                terminate_proc(proc)
                raise RuntimeError(f"{Path(cmd[0]).name} timed out")
            time.sleep(0.3)
        rt.join(timeout=5)
    finally:
        with MUSIC_JOBS_LOCK:
            if DRIVE_JOB_PROCS.get(job_id) is proc:
                DRIVE_JOB_PROCS.pop(job_id, None)
    if proc.returncode != 0 and _music_job_is_cancelled(job_id):
        raise _JobCancelled()  # Cancel killed the process — not an error
    text = "\n".join(tail[-40:])
    return proc.returncode, text


def _which_ffprobe():
    ff = which_ffmpeg()
    if ff:
        cand = Path(ff).with_name("ffprobe" + (".exe" if ff.lower().endswith(".exe") else ""))
        if cand.is_file():
            return str(cand)
    return shutil.which("ffprobe") or shutil.which("ffprobe.exe")


def probe_media(path):
    """ffprobe by content (never trusts the extension). Returns dict or raises."""
    fp = _which_ffprobe()
    if not fp:
        raise RuntimeError("ffprobe not found (install ffmpeg)")
    r = subprocess.run(
        [fp, "-v", "error", "-show_entries",
         "format=duration,format_name:stream=codec_type,codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,  # v6.51
    )
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError:
        data = {}
    fmt = data.get("format") or {}
    audio = [s for s in (data.get("streams") or []) if s.get("codec_type") == "audio"]
    if r.returncode != 0 or not audio:
        err = (r.stderr or "").strip()[-300:]
        raise RuntimeError(
            "ffmpeg found no audio stream in this file" + (f" ({err})" if err else "")
        )
    try:
        dur = float(fmt.get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    return {
        "duration": dur,
        "container": fmt.get("format_name") or "?",
        "audioCodec": audio[0].get("codec_name") or "?",
    }


def detect_leading_silence(path, max_scan=900):
    """Seconds of leading silence (info only), or 0."""
    ff = which_ffmpeg()
    if not ff:
        return 0.0
    try:
        r = subprocess.run(
            [ff, "-nostdin", "-hide_banner", "-t", str(max_scan), "-i", str(path),
             "-vn", "-af", "silencedetect=noise=-50dB:d=2", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,  # v6.51
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    txt = r.stderr or ""
    m_start = _re_drive.search(r"silence_start:\s*(-?[\d.]+)", txt)
    if not m_start or float(m_start.group(1)) > 0.05:
        return 0.0
    m_end = _re_drive.search(r"silence_end:\s*([\d.]+)", txt)
    if m_end:
        return float(m_end.group(1))
    return float(max_scan)  # silent for the whole scanned window


def _demucs_base_cmd():
    d = which_demucs()
    if not d:
        raise RuntimeError("demucs not installed — pip install demucs (first run downloads models)")
    return [sys.executable, "-m", "demucs"] if d == "python -m demucs" else [d]


def _demucs_devices():
    forced = (os.environ.get("TWITCH_RECORDER_DEMUCS_DEVICE") or "").strip()
    if forced:
        return [forced]
    if sys.platform == "darwin" and not _DEMUCS_DEVICE_STATE["mps_failed"]:
        return ["mps", "cpu"]
    # v6.38: Windows/Linux with an NVIDIA GPU — try CUDA first, CPU fallback on any failure
    if sys.platform != "darwin" and shutil.which("nvidia-smi") and not _DEMUCS_DEVICE_STATE.get("cuda_failed"):
        return ["cuda", "cpu"]
    return ["cpu"]


def _wait_disk_ok(job_id, progress):
    """Disk-low pause between chunks (cancellable). Never deletes anything."""
    paused = False
    while disk_block_error():
        if not paused:
            log(f"drive-music job={job_id}: paused — disk almost full")
            paused = True
        progress("Paused — disk almost full; free space to continue", None)
        _check_cancel(job_id)
        time.sleep(5)
    if paused:
        log(f"drive-music job={job_id}: disk recovered — resuming")


def chunked_demucs_no_vocals(job_id, src, work, duration, progress, pct_lo=20.0, pct_hi=90.0):
    """Split src (a path or list of paths, in order) into ~DEMUCS_CHUNK_SECS FLAC chunks,
    demucs each, return ordered no_vocals list."""
    srcs = list(src) if isinstance(src, (list, tuple)) else [src]
    ff = which_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    chunks_dir = Path(work) / "chunks"
    stems_dir = Path(work) / "stems"
    sep_dir = Path(work) / "sep"
    for d in (chunks_dir, stems_dir, sep_dir):
        d.mkdir(parents=True, exist_ok=True)
    est = max(1, int((duration or 0) // DEMUCS_CHUNK_SECS) + (1 if (duration or 0) % DEMUCS_CHUNK_SECS else 0))
    progress(f"Splitting into {DEMUCS_CHUNK_SECS // 60 or 1}-min chunks (~{est})…", pct_lo - 2)
    for si, one in enumerate(srcs):
        rc, tail = _job_run_proc(
            job_id,
            [ff, "-nostdin", "-v", "error", "-y", "-i", str(one), "-vn", "-map", "0:a:0",
             "-ac", "2", "-ar", "44100", "-c:a", "flac", "-f", "segment",
             "-segment_time", str(DEMUCS_CHUNK_SECS), "-reset_timestamps", "1",
             str(chunks_dir / f"s{si:03d}c%04d.flac")],
            timeout=3 * 3600,
        )
        if rc != 0:
            raise RuntimeError(f"ffmpeg split failed: {tail[-400:]}")
    chunks = sorted(chunks_dir.glob("s*c*.flac"))
    if not chunks:
        raise RuntimeError("ffmpeg split produced no chunks")
    n = len(chunks)
    base_cmd = demucs_cmd_base()  # v6.56
    span = pct_hi - pct_lo
    outs = []
    for i, chunk in enumerate(chunks):
        _check_cancel(job_id)
        _wait_disk_ok(job_id, progress)
        label = f"Demucs chunk {i + 1}/{n}"
        progress(f"{label}…", round(pct_lo + span * i / n, 1))
        out_dir = sep_dir / chunk.stem
        state = {"pct": 0.0}

        def on_line(line, i=i, label=label, state=state):
            _msg, p = _parse_demucs_progress_line(line)
            if p is not None and p >= state["pct"]:
                state["pct"] = p
                progress(f"{label} · {p:.0f}%", round(pct_lo + span * (i + p / 100.0) / n, 1))

        last_err = ""
        stem_wav = None
        for dev in _demucs_devices():
            shutil.rmtree(out_dir, ignore_errors=True)
            env = dict(os.environ)
            env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
            cmd = base_cmd + [
                "-n", "htdemucs", "--two-stems=vocals", "--segment", DEMUCS_SEGMENT,
                "-j", "1", "-d", dev, "--clip-mode", "clamp",
                "--filename", "{stem}.{ext}", "-o", str(out_dir), str(chunk),
            ]
            state["pct"] = 0.0
            rc, tail = _job_run_proc(job_id, cmd, line_cb=on_line, env=env)
            cand = out_dir / "htdemucs" / "no_vocals.wav"
            if rc == 0 and cand.is_file():
                stem_wav = cand
                break
            last_err = tail[-500:]
            if dev == "cuda":
                _DEMUCS_DEVICE_STATE["cuda_failed"] = True
                progress(f"{label}: CUDA failed, retrying on CPU…", None)
            if dev == "mps":
                _DEMUCS_DEVICE_STATE["mps_failed"] = True
                log(f"drive-music job={job_id}: demucs on mps failed — falling back to cpu")
                progress(f"{label}: MPS failed, retrying on CPU…", None)
        # v6.61: one demucs run dying (memory pressure, a killed process, a sleep/wake blip) no longer
        # throws away hours of finished chunks — the same chunk is tried again on the CPU first.
        tries = 0
        while stem_wav is None and tries < DEMUCS_CHUNK_RETRIES and not _pipeline_error_is_permanent(last_err):
            tries += 1
            log(f"drive-music job={job_id}: demucs chunk {i + 1}/{n} failed — trying it again on cpu "
                f"(retry {tries}/{DEMUCS_CHUNK_RETRIES}): {(last_err or 'unknown')[-200:]}")
            progress(f"{label} failed — trying that chunk again…", None)
            _CHUNK_RETRY_STATS["retries"] += 1
            t_end = time.time() + DEMUCS_CHUNK_RETRY_PAUSE_SECS
            while time.time() < t_end:
                _check_cancel(job_id)
                time.sleep(0.5)
            shutil.rmtree(out_dir, ignore_errors=True)
            env = dict(os.environ)
            env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
            cmd = base_cmd + [
                "-n", "htdemucs", "--two-stems=vocals", "--segment", DEMUCS_SEGMENT,
                "-j", "1", "-d", "cpu", "--clip-mode", "clamp",
                "--filename", "{stem}.{ext}", "-o", str(out_dir), str(chunk),
            ]
            state["pct"] = 0.0
            rc, tail = _job_run_proc(job_id, cmd, line_cb=on_line, env=env)
            cand = out_dir / "htdemucs" / "no_vocals.wav"
            if rc == 0 and cand.is_file():
                stem_wav = cand
                _CHUNK_RETRY_STATS["saved"] += 1
                log(f"drive-music job={job_id}: demucs chunk {i + 1}/{n} worked on retry {tries}")
            else:
                last_err = tail[-500:]
        if stem_wav is None:
            log(f"drive-music job={job_id}: demucs failed on chunk {i + 1}/{n}: {last_err or 'unknown'}")
            raise RuntimeError(friendly_demucs_error(last_err, f"demucs (chunk {i + 1}/{n})"))  # v6.56
        # Keep only no_vocals, as FLAC (saves ~45% disk on 4 h files).
        flac = stems_dir / f"{chunk.stem}.flac"
        rc, tail = _job_run_proc(
            job_id, [ff, "-nostdin", "-v", "error", "-y", "-i", str(stem_wav), "-c:a", "flac", str(flac)],
            timeout=1800,
        )
        if rc != 0 or not flac.is_file():
            raise RuntimeError(f"ffmpeg stem convert failed: {tail[-300:]}")
        shutil.rmtree(out_dir, ignore_errors=True)
        try:
            chunk.unlink()
        except OSError:
            pass
        outs.append(flac)
    return outs


def encode_joined_ogg(job_id, parts, out_path, duration, progress, pct_lo=90.0, pct_hi=99.0, codec_args=None):
    """Concat no_vocals parts in order → OGG (libvorbis q5, fallback libopus)."""
    ff = which_ffmpeg()
    lst = Path(out_path).with_suffix(".txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for p in parts:
            fh.write("file '" + str(p).replace("'", "'\\''") + "'\n")
    dur_us = max(1.0, float(duration or 0)) * 1_000_000

    def on_line(line):
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            try:
                v = float(line.split("=", 1)[1])
            except ValueError:
                return
            frac = max(0.0, min(1.0, v / dur_us))
            progress(f"Encoding OGG · {frac * 100:.0f}%", round(pct_lo + (pct_hi - pct_lo) * frac, 1))

    last = ""
    choices = [list(codec_args)] if codec_args else []
    choices += [["-c:a", "libvorbis", "-q:a", "5"], ["-c:a", "libopus", "-b:a", "128k"]]
    for args in choices:
        progress("Encoding OGG…", pct_lo)
        rc, tail = _job_run_proc(
            job_id,
            [ff, "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-vn", *args, "-progress", "pipe:1", "-nostats", str(out_path)],
            line_cb=on_line, timeout=3 * 3600,
        )
        if rc == 0 and Path(out_path).is_file() and Path(out_path).stat().st_size > 0:
            return Path(out_path)
        last = tail[-400:]
    raise RuntimeError(f"ffmpeg encode failed: {last}")


# ─── v6.45: VOICE-REMOVAL QUALITY — Fast (ffmpeg) vs Better (demucs) ─────
# Merged from Twitchy Too. One setting (Convert tab, saved in .drive-pipeline.json) drives
# History/Auto Music only, Convert upload, Drive → remove voice, Drive link, and the
# after-stream remove-voice → split → Drive pipeline. Requests may override per job.
#   better — demucs two-stems no_vocals (unchanged behavior; needs demucs)
#   fast   — ffmpeg center-cancel vocal reduce (no demucs; stereo sources only)
#   auto   — Better when demucs is installed, else Fast (default)
# Both still take the one-at-a-time voice-removal slot so jobs queue politely.
VOICE_QUALITIES = ("auto", "better", "fast")
FAST_VOICE_AF = "pan=stereo|c0=0.5*c0+-0.5*c1|c1=0.5*c0+-0.5*c1,afftdn=nf=-25"


class _MonoAudioError(RuntimeError):
    pass


def _norm_voice_quality(q):
    q = str(q or "").strip().lower()
    if q in ("better", "demucs", "high", "best"):
        return "better"
    if q in ("fast", "ffmpeg", "quick"):
        return "fast"
    if q == "auto":
        return "auto"
    return None


def voice_quality_pref():
    with PIPELINE_LOCK:
        return _norm_voice_quality(PIPELINE.get("voiceQuality")) or "auto"


def resolve_voice_quality(requested=None):
    """→ (effective 'better' | 'fast' | None, note). Better needs demucs; Fast needs ffmpeg."""
    q = _norm_voice_quality(requested)
    if q is None:
        q = voice_quality_pref()
    has_d = demucs_available()
    has_ff = bool(which_ffmpeg())
    if q == "auto":
        q = "better" if has_d else "fast"
    if q == "better":
        if has_d:
            return "better", None
        if has_ff:
            return "fast", "Better needs demucs (not installed) — using Fast (ffmpeg)"
        return None, "demucs not installed and ffmpeg not found — install ffmpeg (Fast) or pip install demucs (Better)"
    if not has_ff:
        return None, "ffmpeg not found — install ffmpeg (needed for Fast voice removal)"
    return "fast", None


def voice_quality_snapshot():
    eff, note = resolve_voice_quality(None)
    return {"pref": voice_quality_pref(), "effective": eff, "note": note,
            "demucs": demucs_available(), "ffmpeg": bool(which_ffmpeg())}


def voice_wait_msg(quality):
    return ("Waiting for demucs (1 at a time)…" if quality != "fast"
            else "Waiting for voice removal (1 at a time)…")


def is_voice_removed_name(name):
    """-music / -vocals exports, plus Twitchy Too -novoice outputs (never re-process those)."""
    if is_music_export_name(name):
        return True
    stem = Path(name or "").stem
    return bool(_re_drive.search(r"-novoice(?:-\d+)?$", stem))


def _audio_channels(path):
    fp = _which_ffprobe()
    if not fp:
        return 0
    try:
        r = subprocess.run(
            [fp, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,  # v6.51
        )
        return int((r.stdout or "").strip().splitlines()[0])
    except Exception:
        return 0


def fast_voice_reduce_parts(job_id, src, work, duration, progress, pct_lo=20.0, pct_hi=90.0):
    """Twitchy Too Fast path: ffmpeg center-cancel (+ light denoise) per source → ordered FLAC
    list (same shape as chunked_demucs_no_vocals, so encode/split/Drive steps are unchanged)."""
    srcs = list(src) if isinstance(src, (list, tuple)) else [src]
    ff = which_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    out_dir = Path(work) / "fast"
    out_dir.mkdir(parents=True, exist_ok=True)
    for one in srcs:
        if _audio_channels(one) == 1:
            raise _MonoAudioError(
                f"{Path(one).name}: mono audio — Fast (center-cancel) needs stereo; use Better (demucs)")
    durs = []
    for one in srcs:
        try:
            durs.append(float(probe_media(one)["duration"] or 0))
        except Exception:
            durs.append(0.0)
    total = sum(durs) or float(duration or 0) or 1.0
    span = pct_hi - pct_lo
    outs = []
    done_secs = 0.0
    for i, one in enumerate(srcs):
        _check_cancel(job_id)
        _wait_disk_ok(job_id, progress)
        n = len(srcs)
        label = "Fast voice removal" + (f" {i + 1}/{n}" if n > 1 else "")
        progress(f"{label} (ffmpeg center-cancel)…", round(pct_lo + span * done_secs / total, 1))
        flac = out_dir / f"s{i:03d}.flac"
        base_done = done_secs

        def on_line(line, base_done=base_done, label=label):
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    v = float(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    return
                frac = max(0.0, min(1.0, (base_done + v) / total))
                progress(f"{label} · {frac * 100:.0f}%", round(pct_lo + span * frac, 1))

        rc, tail = _job_run_proc(
            job_id,
            [ff, "-nostdin", "-v", "error", "-y", "-i", str(one), "-vn", "-map", "0:a:0",
             "-af", FAST_VOICE_AF, "-ac", "2", "-ar", "44100", "-c:a", "flac",
             "-progress", "pipe:1", "-nostats", str(flac)],
            line_cb=on_line, timeout=3 * 3600,
        )
        if rc != 0 or not flac.is_file() or flac.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg vocal reduce failed: {tail[-400:]}")
        done_secs += durs[i]
        outs.append(flac)
    return outs


def voice_removed_parts(job_id, src, work, duration, progress, quality, pct_lo=20.0, pct_hi=90.0):
    """Dispatch to Fast (ffmpeg) or Better (chunked demucs). Mono + Fast → demucs if installed."""
    if quality == "fast":
        try:
            return fast_voice_reduce_parts(job_id, src, work, duration, progress, pct_lo, pct_hi)
        except _MonoAudioError as e:
            if not demucs_available():
                raise RuntimeError(str(e))
            log(f"voice job={job_id}: {e} — switching to Better (demucs)")
            _set_music_job(job_id, quality="better", qualityNote="mono source — used Better (demucs)")
            progress("Mono audio — using Better (demucs) instead of Fast…", None)
    return chunked_demucs_no_vocals(job_id, src, work, duration, progress, pct_lo, pct_hi)


def atomic_publish(tmp_file, dest):
    """Write dest atomically (temp in dest folder, then rename) so Drive syncs one clean file."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / f".{dest.name}.partial-{uuid.uuid4().hex[:6]}"
    try:
        same_dev = os.stat(tmp_file).st_dev == os.stat(dest.parent).st_dev
    except OSError:
        same_dev = False
    try:
        if same_dev:
            os.replace(tmp_file, part)
        else:
            with open(tmp_file, "rb") as src, open(part, "wb") as out:
                shutil.copyfileobj(src, out, length=4 * 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
        os.replace(part, dest)
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
    return dest


def _stage_copy(job_id, src, dest, progress, pct_lo=0.0, pct_hi=15.0):
    """Copy a Drive for Desktop file to local temp (reading triggers download of online-only files)."""
    try:
        total = os.stat(src).st_size
    except OSError as e:
        raise RuntimeError(f"cannot read source: {e}")
    progress("Reading from Drive (online-only files download first)…", pct_lo)
    done = 0
    last_emit = 0.0
    with open(src, "rb") as fin, open(dest, "wb") as fout:
        while True:
            _check_cancel(job_id)
            buf = fin.read(4 * 1024 * 1024)
            if not buf:
                break
            fout.write(buf)
            done += len(buf)
            now = time.time()
            if now - last_emit > 0.5:
                last_emit = now
                frac = (done / total) if total else 0
                progress(
                    f"Copying from Drive · {frac * 100:.0f}% ({done // 1048576} / {total // 1048576} MB)",
                    round(pct_lo + (pct_hi - pct_lo) * min(1.0, frac), 1),
                )
    if total and done != total:
        raise RuntimeError(
            f"Drive sync hiccup: read {done} of {total} bytes — try again (Drive for Desktop may still be downloading)"
        )
    return done


def _drive_download(job_id, file_id, progress, redo, pct_lo=0.0, pct_hi=15.0):
    """Stream a public Drive file to REC_DIR/.drive-tmp. Returns (path, filename)."""
    DRIVE_TMP_DIR.mkdir(parents=True, exist_ok=True)
    progress("Contacting Google Drive…", pct_lo)
    resp = drive_open_download(file_id)
    try:
        fname = _safe_download_name(
            _content_disposition_filename(resp.headers.get("Content-Disposition")), file_id
        )
        _set_music_job(job_id, displayName=fname)
        out_final = REC_DIR / music_output_name(fname, ".ogg")
        _set_music_job(job_id, outputPath=str(out_final), outputName=None)
        if out_final.exists() and not redo:
            raise FileExistsError(str(out_final))
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except ValueError:
            total = 0
        part = DRIVE_TMP_DIR / f"{job_id}-{file_id}.part"
        _register_work_path(part)
        try:
            _stream_to_part(job_id, resp, part, total, progress, pct_lo, pct_hi)
        except BaseException:
            try:
                part.unlink()
            except OSError:
                pass
            _unregister_work_path(part)
            raise
    finally:
        try:
            resp.close()
        except Exception:
            pass
    final = DRIVE_TMP_DIR / f"{job_id}-{fname}"
    os.replace(part, final)
    _unregister_work_path(part)
    _register_work_path(final)
    return final, fname


def _stream_to_part(job_id, resp, part, total, progress, pct_lo, pct_hi):
    done = 0
    last_emit = 0.0
    with open(part, "wb") as fh:
        while True:
            _check_cancel(job_id)
            buf = resp.read(1024 * 1024)
            if not buf:
                break
            fh.write(buf)
            done += len(buf)
            now = time.time()
            if now - last_emit > 0.5:
                last_emit = now
                if disk_block_error():
                    raise RuntimeError("disk almost full — download stopped")
                if total:
                    frac = done / total
                    progress(
                        f"Downloading · {frac * 100:.0f}% ({done // 1048576} / {total // 1048576} MB)",
                        round(pct_lo + (pct_hi - pct_lo) * min(1.0, frac), 1),
                    )
                else:
                    progress(f"Downloading · {done // 1048576} MB", None)
    if total and done != total:
        raise RuntimeError(f"download incomplete ({done} of {total} bytes) — try again")
    if done == 0:
        raise RuntimeError("Google Drive returned an empty file")


def _unique_rec_path(fname):
    """REC_DIR/<fname> (spaces kept), adding -2, -3… before the extension on collision."""
    base = _safe_download_name(fname, "file")
    dest = REC_DIR / base
    if not dest.exists():
        return dest
    stem, suf = os.path.splitext(base)
    for n in range(2, 1000):
        dest = REC_DIR / f"{stem}-{n}{suf}"
        if not dest.exists():
            return dest
    return REC_DIR / f"{stem}-{int(time.time())}{suf}"


def _record_drive_history(entry):
    with DRIVE_HISTORY_LOCK:
        items = _read_drive_history_locked()
        items = [e for e in items if e.get("outputPath") != entry.get("outputPath")]
        items.insert(0, entry)
        items = items[:DRIVE_HISTORY_MAX]
        try:
            REC_DIR.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(DRIVE_HISTORY_FILE, json.dumps(items, indent=1).encode("utf-8"))
        except Exception as e:
            log(f"drive history write failed: {e}")


def _read_drive_history_locked():
    try:
        data = json.loads(DRIVE_HISTORY_FILE.read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def drive_history_payload():
    with DRIVE_HISTORY_LOCK:
        items = _read_drive_history_locked()
    out = []
    rec_real = os.path.realpath(str(REC_DIR))
    for e in items:
        p = e.get("outputPath") or ""
        try:
            st = os.stat(p)
        except OSError:
            continue
        in_rec = os.path.dirname(os.path.realpath(p)) == rec_real
        out.append(dict(e, size=int(st.st_size), mtime=int(st.st_mtime), inRecordings=in_rec,
                        outputName=os.path.basename(p)))
    return {"ok": True, "items": out}


def drive_music_worker(job_id):
    work = None
    slot_held = False
    dl_path = None
    source = _job_get(job_id, "source")
    redo = bool(_job_get(job_id, "redo"))
    keep_source = bool(_job_get(job_id, "keepSource"))

    def progress(msg, pct=None):
        if pct is not None:
            _set_music_job(job_id, progress=msg, progressPct=pct)
        else:
            _set_music_job(job_id, progress=msg)

    try:
        _check_cancel(job_id)
        REC_DIR.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="music-only-", dir=str(REC_DIR)))
        _register_work_path(work)
        _set_music_job(job_id, status="running", phase="fetch")
        if source == "link":
            try:
                dl_path, fname = _drive_download(job_id, _job_get(job_id, "fileId"), progress, redo)
            except FileExistsError as fe:
                out = Path(str(fe))
                _set_music_job(
                    job_id, status="done", progress="Already done (check Redo to re-run)",
                    progressPct=100, outputPath=str(out), outputName=out.name, already=True,
                    finishedAt=time.time(),
                )
                return
            staged = dl_path
        else:
            src = Path(_job_get(job_id, "srcPath"))
            staged = work / "source.media"  # no extension: ffmpeg probes the content
            _stage_copy(job_id, src, staged, progress)
        _check_cancel(job_id)
        progress("Probing audio…", 16)
        info = probe_media(staged)
        lead = detect_leading_silence(staged)
        note = f"{info['container']} / {info['audioCodec']}, {info['duration'] / 60:.1f} min"
        if lead >= 1.0:
            note += f" · leading silence {lead:.0f}s (info only)"
        _set_music_job(job_id, info=note, leadingSilence=round(lead, 1), duration=info["duration"])
        log(f"drive-music job={job_id}: {note}")
        # One demucs at a time (shared with History / Auto Music). v6.45: Fast waits too.
        quality = _job_get(job_id, "quality") or "better"
        _set_music_job(job_id, status="queued", phase="wait",
                       progress=voice_wait_msg(quality))
        while True:
            _check_cancel(job_id)
            if DEMUCS_SLOTS.acquire(timeout=0.5):
                slot_held = True
                break
        _check_cancel(job_id)
        _set_music_job(job_id, status="running", phase="demucs" if quality != "fast" else "fast")
        parts = voice_removed_parts(job_id, staged, work, info["duration"], progress, quality)
        _check_cancel(job_id)
        _set_music_job(job_id, phase="encode")
        tmp_out = encode_joined_ogg(job_id, parts, work / "out.ogg", info["duration"], progress)
        _check_cancel(job_id)
        dest = Path(_job_get(job_id, "outputPath"))
        if dest.exists() and not redo:
            # Something else wrote the same -music name meanwhile — never clobber it.
            stem = dest.stem
            for n in range(2, 1000):
                cand = dest.with_name(f"{stem}-{n}{dest.suffix}")
                if not cand.exists():
                    dest = cand
                    break
        progress("Saving…", 99)
        atomic_publish(tmp_out, dest)
        kept = None
        if source == "link" and dl_path is not None:
            if keep_source:
                keep_dest = _unique_rec_path(dl_path.name.split("-", 1)[1])
                os.replace(dl_path, keep_dest)
                kept = keep_dest.name
            else:
                try:
                    dl_path.unlink()
                except OSError:
                    pass
        _record_drive_history({
            "outputPath": str(dest),
            "source": source,
            "srcName": _job_get(job_id, "displayName"),
            "srcPath": _job_get(job_id, "srcPath"),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "info": _job_get(job_id, "info"),
            "keptSource": kept,
        })
        in_rec = os.path.dirname(os.path.realpath(str(dest))) == os.path.realpath(str(REC_DIR))
        _set_music_job(
            job_id, status="done", phase="done", progress="Done", progressPct=100,
            outputPath=str(dest), outputName=dest.name,
            outputUrl=("/api/download/" + urllib.parse.quote(dest.name)) if in_rec else None,
            keptSourceName=kept, error=None, finishedAt=time.time(),
        )
        log(f"drive-music done job={job_id} out={dest}")
    except _JobCancelled:
        _finalize_cancelled_music_job(job_id)
        log(f"drive-music cancelled job={job_id}")
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"drive-music error job={job_id}: {msg}")
        _set_music_job(job_id, status="error", progress="Error", error=msg, finishedAt=time.time())
    finally:
        if slot_held:
            try:
                DEMUCS_SLOTS.release()
            except Exception:
                pass
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
            _unregister_work_path(work)
        if dl_path is not None:
            _unregister_work_path(dl_path)
            st = _job_get(job_id, "status")
            # Downloaded source: delete unless Keep is on (then move it next to recordings).
            try:
                if dl_path.exists():
                    if keep_source and st != "cancelled":
                        os.replace(dl_path, _unique_rec_path(dl_path.name.split("-", 1)[1]))
                    else:
                        dl_path.unlink()
            except OSError:
                pass


def _new_drive_job(name, display, source, **extra):
    job_id = uuid.uuid4().hex[:12]
    job = {
        "jobId": job_id, "status": "queued", "progress": "Queued…", "progressPct": 0,
        "error": None, "name": name, "displayName": display, "source": source,
        "outputName": None, "outputUrl": None, "vocalsName": None, "vocalsUrl": None,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    job.update(extra)
    with MUSIC_JOBS_LOCK:
        MUSIC_JOBS[job_id] = job
    threading.Thread(target=drive_music_worker, args=(job_id,), daemon=True,
                     name=f"drive-music-{job_id}").start()
    return job_id


def _drive_preflight(quality=None):
    if not which_ffmpeg():
        return {"ok": False, "error": "ffmpeg not found — install ffmpeg"}, 400
    if not resolve_voice_quality(quality)[0]:  # v6.45: Fast works without demucs
        return {"ok": False, "error": resolve_voice_quality(quality)[1],
                "demucs": demucs_available()}, 400
    if disk_block_error():
        free, _t = disk_usage_for_rec_dir()
        return {"ok": False, "diskBlock": True,
                "error": f"disk almost full ({(free or 0) / 1048576:.0f} MB free) — free space before Music only / Demucs"}, 507
    return None


def _inflight_job_for(name):
    with MUSIC_JOBS_LOCK:
        for jid, job in MUSIC_JOBS.items():
            if job.get("name") == name and (job.get("status") or "") in ("queued", "running"):
                return jid
    return None


def start_drive_music(body):
    """POST /api/drive/music {paths:[...], redo, outMode} — Drive for Desktop files."""
    body = body if isinstance(body, dict) else {}
    paths = body.get("paths")
    if isinstance(body.get("path"), str):
        paths = [body.get("path")]
    if not isinstance(paths, list) or not paths:
        return {"ok": False, "error": "no files selected"}, 400
    if len(paths) > 100:
        return {"ok": False, "error": "too many files (max 100 per request)"}, 400
    pre = _drive_preflight(body.get("quality"))
    if pre:
        return pre
    eff_q, q_note = resolve_voice_quality(body.get("quality"))  # v6.45
    redo = bool(body.get("redo"))
    mode = _norm_out_mode(body.get("outMode"))
    roots = detect_drive_roots()
    if not roots:
        return {"ok": False, "found": False, "error": "Google Drive for Desktop not found",
                "installUrl": DRIVE_INSTALL_URL}, 404
    started, skipped = [], []
    for raw in paths:
        real, _root, err = validate_drive_path(raw, roots)
        if err:
            skipped.append({"path": raw, "reason": err})
            continue
        name = os.path.basename(real)
        if not os.path.isfile(real):
            skipped.append({"path": real, "reason": "not a file"})
            continue
        if Path(name).suffix.lower() not in DRIVE_MEDIA_EXTS:
            skipped.append({"path": real, "reason": "not an audio/video file"})
            continue
        if is_voice_removed_name(name):
            skipped.append({"path": real, "reason": "already a -music / -vocals / -novoice export"})
            continue
        dest = _drive_output_path(real, mode)
        if mode != "recordings":
            # Output folder must also stay inside Drive (e.g. a "Music only" symlink).
            parent_real = os.path.realpath(str(dest.parent)) if dest.parent.exists() else os.path.realpath(os.path.dirname(real))
            if not any(_path_within(parent_real, r["path"]) for r in roots):
                skipped.append({"path": real, "reason": "output folder escapes Google Drive"})
                continue
        if dest.exists() and not redo:
            skipped.append({"path": real, "reason": "already", "outputPath": str(dest)})
            continue
        key = "drive:" + real
        jid = _inflight_job_for(key)
        if jid:
            skipped.append({"path": real, "reason": "already running", "jobId": jid})
            continue
        jid = _new_drive_job(key, name, "drive", srcPath=real, outputPath=str(dest),
                             outMode=mode, redo=redo, quality=eff_q, qualityNote=q_note)
        started.append({"path": real, "jobId": jid, "outputPath": str(dest)})
        log(f"drive-music queued job={jid} src={real} out={dest} redo={redo} quality={eff_q}")
    return {"ok": True, "started": started, "skipped": skipped, "quality": eff_q, "qualityNote": q_note}, 200


def start_drive_fetch(body):
    """POST /api/drive/fetch {url, keepSource, redo} — public Drive link."""
    body = body if isinstance(body, dict) else {}
    file_id, err = parse_drive_file_id(body.get("url"))
    if err:
        return {"ok": False, "error": err}, 400
    pre = _drive_preflight(body.get("quality"))
    if pre:
        return pre
    eff_q, q_note = resolve_voice_quality(body.get("quality"))  # v6.45
    key = "link:" + file_id
    jid = _inflight_job_for(key)
    if jid:
        return {"ok": True, "jobId": jid, "alreadyRunning": True, "fileId": file_id}, 200
    jid = _new_drive_job(key, f"Drive file {file_id[:10]}…", "link", fileId=file_id,
                         keepSource=bool(body.get("keepSource")), redo=bool(body.get("redo")),
                         outMode="recordings", quality=eff_q, qualityNote=q_note)
    log(f"drive-fetch queued job={jid} id={file_id} quality={eff_q}")
    return {"ok": True, "jobId": jid, "fileId": file_id, "quality": eff_q, "qualityNote": q_note}, 200


def cancel_drive_job(job_id):
    """Cancel a Drive/link job in any phase (download, copy, demucs chunk, encode)."""
    with MUSIC_JOBS_LOCK:
        job = MUSIC_JOBS.get(job_id)
        if not job or job.get("source") not in ("drive", "link"):
            return False
        if (job.get("status") or "") not in ("queued", "running"):
            return False
        job["cancelRequested"] = True
        job["progress"] = "Cancelling…"
        if (job.get("status") or "") == "queued" and job.get("phase") != "fetch":
            job["status"] = "cancelled"
            job["progress"] = "Cancelled"
            job["finishedAt"] = time.time()
        proc = DRIVE_JOB_PROCS.get(job_id)
    if proc is not None:
        try:
            terminate_proc(proc)
        except Exception:
            pass
    log(f"drive-music cancel requested job={job_id}")
    return True


def api_reveal(body):
    """POST /api/reveal {name | path, mode: reveal|open} — Finder reveal / open with default app."""
    body = body if isinstance(body, dict) else {}
    mode = "open" if body.get("mode") == "open" else "reveal"
    target = None
    if body.get("name"):
        target = resolve_download_path(body.get("name"))
    elif body.get("path"):
        raw = str(body.get("path"))
        real, _root, err = validate_drive_path(raw)
        if real is None and "\x00" not in raw and ".." not in Path(raw).parts:
            # Also allow files directly under ~/TwitchRecordings by absolute path.
            rp = os.path.realpath(raw)
            if os.path.dirname(rp) == os.path.realpath(str(REC_DIR)):
                real = rp
            elif _path_within(rp, os.path.realpath(str(DRIVE_PARTS_DIR))):
                real = rp  # v6.36: split parts
        target = Path(real) if real else None
    if target is None or not target.is_file():
        return {"ok": False, "error": "file not found"}, 404
    if mode == "open" and target.suffix.lower() not in DRIVE_MEDIA_EXTS:
        return {"ok": False, "error": "only audio/video files can be opened"}, 400
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(target)] if mode == "reveal" else ["open", str(target)]
    elif os.name == "nt":
        cmd = ["explorer", "/select," + str(target)] if mode == "reveal" else ["explorer", str(target)]
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            return {"ok": False, "error": "no file opener on this system"}, 501
        cmd = [opener, str(target.parent if mode == "reveal" else target)]
    try:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return {"ok": False, "error": f"could not open: {e}"}, 500
    return {"ok": True, "mode": mode, "path": str(target)}, 200


def cleanup_drive_temps(reason="startup"):
    """Remove leftover .drive-tmp downloads and stale .*.partial-* files (never live ones)."""
    removed = 0
    freed = 0
    try:
        if DRIVE_TMP_DIR.is_dir():
            for p in DRIVE_TMP_DIR.iterdir():
                if _is_active_work_path(p):
                    continue
                try:
                    sz = p.stat().st_size if p.is_file() else _dir_byte_size(p)
                    if p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        p.unlink()
                    removed += 1
                    freed += sz
                except OSError:
                    continue
        now = time.time()
        for p in REC_DIR.glob(".*.partial-*"):
            try:
                if now - p.stat().st_mtime > 3600:
                    freed += p.stat().st_size
                    p.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    if removed:
        log(f"drive temp cleanup ({reason}): {removed} items, {freed} bytes")
    return removed, freed



# ─────────────────────────────────────────────────────────────────────────────
# v6.36: After stream ends → remove voice → split (<50 MB parts) → send to Drive
#   * Triggered server-side when a recording session finalizes (stop / give-up /
#     supervisor exit), so it works even if the recorder page is closed.
#   * Same Music-only queue + chunked Demucs as v6.35 (one demucs at a time).
#   * Parts: ~/TwitchRecordings/Drive parts/<base>/<base>-music-partNofM.ogg
#   * Delivery: atomic copy into a Drive for Desktop folder; "Waiting for Drive"
#     until Drive appears (checked periodically), then sent automatically.
# ─────────────────────────────────────────────────────────────────────────────
import math as _math_pipe

PART_TARGET_BYTES = int(os.environ.get("TWITCH_RECORDER_PART_TARGET_BYTES") or 45 * 1000 * 1000)
PART_MAX_BYTES = int(os.environ.get("TWITCH_RECORDER_PART_MAX_BYTES") or 50 * 1000 * 1000)
PART_MIN_SECS = float(os.environ.get("TWITCH_RECORDER_PART_MIN_SECS") or 600)
DELIVERY_POLL_SECS = max(2, int(os.environ.get("TWITCH_RECORDER_DELIVERY_SECS") or 60))
DELIVERY_ERROR_RETRY_SECS = 300
PIPELINE_START_DELAY_SECS = float(os.environ.get("TWITCH_RECORDER_PIPELINE_DELAY") or 3)
DRIVE_PARTS_DIR = REC_DIR / "Drive parts"
PIPELINE_FILE = REC_DIR / ".drive-pipeline.json"
PIPELINE_LOCK = threading.RLock()
PIPELINE = {"enabled": True, "users": {}, "target": "", "deliveries": {},
            "origEnabled": True, "origUsers": {}, "originals": {},  # v6.38 originals
            "keepAwake": True, "keepAwakeArmed": False,  # v6.40 keep computer awake
            "keepAwakeSchedule": True, "goLiveHistory": {}}  # v6.52 stay awake before usual go-live
DELIVERY_WAKE = threading.Event()
DELIVERY_STATE = {"busy": False, "force": set(), "base": None}  # v6.37: base while mid-copy
# v6.39: stale Drive .{name}.partial-{hex} under Auto-send target (+ Originals/)
DRIVE_PARTIAL_STALE_SECS = int(os.environ.get("TWITCH_RECORDER_DRIVE_PARTIAL_STALE_SECS") or 3600)
DRIVE_PARTIAL_IDLE_SECS = int(os.environ.get("TWITCH_RECORDER_DRIVE_PARTIAL_IDLE_SECS") or 300)
PIPELINE_SEEN = set()  # first-segment basenames already queued by this process
MUSIC_OGG_ARGS = ["-c:a", "libvorbis", "-q:a", "6", "-ar", "44100", "-ac", "2"]
AUTO_TARGET_NAMES = ("twitch", "twitchrecordings", "twitch recordings", "twitch-recordings")
DEFAULT_TARGET_REL = ("Twitch Recordings", "Music only")


def _pipeline_load():
    try:
        data = json.loads(PIPELINE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    with PIPELINE_LOCK:
        PIPELINE["enabled"] = data.get("enabled") is not False
        users = data.get("users") if isinstance(data.get("users"), dict) else {}
        PIPELINE["users"] = {str(k).lower(): bool(v) for k, v in users.items()}
        PIPELINE["target"] = data.get("target") if isinstance(data.get("target"), str) else ""
        dels = data.get("deliveries") if isinstance(data.get("deliveries"), dict) else {}
        for d in dels.values():
            if isinstance(d, dict) and d.get("status") == "sending":
                d["status"] = "waiting"  # helper stopped mid-copy — retry
        PIPELINE["deliveries"] = {k: v for k, v in dels.items() if isinstance(v, dict)}
        # v6.38: send-original prefs + per-file original deliveries
        PIPELINE["origEnabled"] = data.get("origEnabled") is not False
        ou = data.get("origUsers") if isinstance(data.get("origUsers"), dict) else {}
        PIPELINE["origUsers"] = {str(k).lower(): bool(v) for k, v in ou.items()}
        origs = data.get("originals") if isinstance(data.get("originals"), dict) else {}
        for d in origs.values():
            if isinstance(d, dict) and d.get("status") == "sending":
                d["status"] = "queued"
        PIPELINE["originals"] = {k: v for k, v in origs.items() if isinstance(v, dict)}
        # v6.40: keep-awake prefs (default: on while recording/processing; armed hold opt-in)
        PIPELINE["keepAwake"] = data.get("keepAwake") is not False
        PIPELINE["keepAwakeArmed"] = data.get("keepAwakeArmed") is True
        # v6.52: stay awake ahead of armed streamers' usual go-live times (default on)
        PIPELINE["keepAwakeSchedule"] = data.get("keepAwakeSchedule") is not False
        glh = data.get("goLiveHistory") if isinstance(data.get("goLiveHistory"), dict) else {}
        PIPELINE["goLiveHistory"] = {
            str(k).lower(): sorted({float(x) for x in v if isinstance(x, (int, float))})[-KA_SCHED_KEEP:]
            for k, v in glh.items() if isinstance(v, list) and safe_username(str(k).lower())}
        # v6.41: post this computer's status file into Drive (default on)
        PIPELINE["statusToDrive"] = data.get("statusToDrive") is not False
        # v6.43: install helper updates automatically when idle (default on)
        PIPELINE["autoUpdate"] = data.get("autoUpdate") is not False
        # v6.55: keep streamlink current (pip installs only; default on)
        PIPELINE["streamlinkUpdate"] = data.get("streamlinkUpdate") is not False
        # v6.49: mirror of solo-watch enabled (authoritative copy is .solo-watch.json)
        PIPELINE["soloWatch"] = data.get("soloWatch") is not False
        # v6.50: only one computer sends each stream's parts to Drive (default on)
        PIPELINE["oneCopy"] = data.get("oneCopy") is not False
        # v6.45: voice-removal quality (auto = Better when demucs installed, else Fast)
        PIPELINE["voiceQuality"] = _norm_voice_quality(data.get("voiceQuality")) or "auto"
        # v6.53: recording sessions in progress (a crash/reboot leaves them here → recovered)
        lsd = data.get("liveSessions") if isinstance(data.get("liveSessions"), dict) else {}
        PIPELINE["liveSessions"] = {str(k).lower(): v for k, v in lsd.items()
                                    if isinstance(v, dict) and isinstance(v.get("segments"), list)}
        # v6.44: after-stream jobs that were queued/running when the helper last stopped
        pj = data.get("pendingJobs") if isinstance(data.get("pendingJobs"), dict) else {}
        PIPELINE["pendingJobs"] = {str(k): v for k, v in pj.items()
                                   if isinstance(v, dict) and isinstance(v.get("segments"), list)}


def _pipeline_save():
    with PIPELINE_LOCK:
        blob = json.dumps(PIPELINE, indent=1).encode("utf-8")
    try:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(PIPELINE_FILE, blob)
    except Exception as e:
        log(f"pipeline state write failed: {e}")


# ─── v6.40: KEEP THE COMPUTER AWAKE WHILE RECORDING / PROCESSING ─────
# A Mac or PC that idle-sleeps mid-stream loses the rest of the recording, and one that sleeps
# right after the stream never removes the voice or sends the parts to Drive. While anything is
# recording, Demucs/split jobs are queued or running, a seamless join runs, or a Drive copy is in
# flight (plus a short grace after the last of these), the helper holds an idle/system-sleep
# assertion: macOS `caffeinate -i -s -w <helper pid>` (dies with the helper), Windows
# SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED), Linux `systemd-inhibit` if present.
# The display is still allowed to sleep. Optional: also hold while the page reports Auto-Rec
# streamers (heartbeat on /api/health?armed=…), so a sleeping computer can't miss a go-live.
KEEP_AWAKE_GRACE_SECS = int(os.environ.get("TWITCH_RECORDER_KEEP_AWAKE_GRACE_SECS") or 600)
KEEP_AWAKE_ARMED_HOLD_SECS = int(os.environ.get("TWITCH_RECORDER_KEEP_AWAKE_ARMED_HOLD_SECS") or 240)
KEEP_AWAKE_TICK_SECS = float(os.environ.get("TWITCH_RECORDER_KEEP_AWAKE_TICK_SECS") or 15)
KEEP_AWAKE_ENV_OFF = (os.environ.get("TWITCH_RECORDER_KEEP_AWAKE") or "").strip().lower() in (
    "0", "off", "false", "no")
KEEP_AWAKE_WAKE = threading.Event()
KEEP_AWAKE_LOCK = threading.Lock()
KEEP_AWAKE = {"active": False, "reason": None, "since": None, "error": None,
              "lastBusyAt": 0.0, "lastBusyReason": None,
              "armedAt": 0.0, "armedUsers": [], "proc": None}
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake_method():
    """Which sleep-blocker this computer supports (None = unavailable / disabled by env)."""
    if KEEP_AWAKE_ENV_OFF:
        return None
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        if shutil.which("caffeinate") or os.path.exists("/usr/bin/caffeinate"):
            return "caffeinate"
        return None
    if shutil.which("systemd-inhibit"):
        return "systemd-inhibit"
    return None


def note_armed_heartbeat(raw):
    """Page says these streamers have Auto-Rec on (comma list; empty = none armed)."""
    users = []
    for part in str(raw or "").split(","):
        u = safe_username(part)
        if u and u not in users:
            users.append(u)
        if len(users) >= 50:
            break
    with KEEP_AWAKE_LOCK:
        KEEP_AWAKE["armedUsers"] = users
        KEEP_AWAKE["armedAt"] = time.time() if users else 0.0


def _keep_awake_busy_reason():
    """Human reason the computer must stay awake right now, or None when idle."""
    with LOCK:
        reap_locked()
        rec = sorted(ACTIVE.keys())
    if rec:
        return "recording " + ", ".join(rec)
    info = music_demucs_queue_info()
    if int(info.get("running") or 0) > 0 or int(info.get("waiting") or 0) > 0:
        return "removing voice / splitting"
    if DELIVERY_STATE.get("busy"):
        return "sending to Google Drive"
    with SEAMLESS_JOBS_LOCK:
        if any((j.get("status") or "") in ("queued", "running") for j in SEAMLESS_JOBS.values()):
            return "joining segments"
    return None


def keep_awake_desired(now=None):
    now = time.time() if now is None else now
    with PIPELINE_LOCK:
        on = PIPELINE.get("keepAwake", True) is not False
        armed_on = bool(PIPELINE.get("keepAwakeArmed"))
    if not on:
        return None
    reason = _keep_awake_busy_reason()
    with KEEP_AWAKE_LOCK:
        if reason:
            KEEP_AWAKE["lastBusyAt"] = now
            KEEP_AWAKE["lastBusyReason"] = reason
            return reason
        last = float(KEEP_AWAKE.get("lastBusyAt") or 0.0)
        if last and now - last < KEEP_AWAKE_GRACE_SECS:
            # covers the gap between a stream ending and the after-stream jobs queueing
            return "just finished " + (KEEP_AWAKE.get("lastBusyReason") or "work") + " — waiting for after-stream steps"
        users = list(KEEP_AWAKE.get("armedUsers") or [])
        at = float(KEEP_AWAKE.get("armedAt") or 0.0)
    if armed_on and users and now - at < KEEP_AWAKE_ARMED_HOLD_SECS:
        return "Auto-Rec armed for " + ", ".join(users[:4]) + ("…" if len(users) > 4 else "")
    try:  # v6.52: usual go-live window for an armed streamer (learned from past recordings)
        return keep_awake_schedule_reason(now)
    except Exception as e:
        log(f"keep-awake schedule check failed: {e}")
        return None


# ─── v6.52: STAY AWAKE BEFORE ARMED STREAMERS' USUAL GO-LIVE TIMES ─────
# Keep-awake (v6.40) only held sleep off once a recording had started, or — opt-in — while the
# page was open with Auto-Rec armed. A Mac/PC left alone in the afternoon idle-slept before the
# evening stream and missed it entirely (solo-watch can't run on a sleeping computer). The helper
# already knows when each streamer usually goes live: every recording is named
# <user>-<Wkd>-YYYY-MM-DDTHH-MM-SS. From the last 8 weeks of recordings (plus a small history kept
# in .drive-pipeline.json so deleted files still count) it learns, per armed streamer and weekday,
# the usual first go-live time; on a weekday with at least 2 past streams it holds sleep off from
# ~2 h before the earliest usual start until ~1.5 h after the latest one. Starts before 5 AM count
# toward the previous evening's stream. Recording itself then takes over the hold. Only streamers
# that are Auto-Rec armed (fresh page heartbeat, else the persisted solo-watch list) count.
# Toggle keepAwakeSchedule (default on); env TWITCH_RECORDER_KEEP_AWAKE_SCHEDULE=0 hard-off.
KEEP_AWAKE_SCHEDULE_ENV_OFF = (os.environ.get("TWITCH_RECORDER_KEEP_AWAKE_SCHEDULE") or "").strip().lower() in (
    "0", "off", "false", "no")
KA_SCHED_LEAD_MINS = int(os.environ.get("TWITCH_RECORDER_KEEP_AWAKE_LEAD_MINS") or 120)
KA_SCHED_TAIL_MINS = int(os.environ.get("TWITCH_RECORDER_KEEP_AWAKE_TAIL_MINS") or 90)
KA_SCHED_LOOKBACK_DAYS = 56
KA_SCHED_MIN_DAYS = 2
KA_SCHED_MAX_SPREAD_MINS = 240  # starts scattered wider than this → use the median start only
KA_SCHED_DAY_SHIFT_MINS = 300  # a 1 AM start belongs to the previous evening's stream
KA_SCHED_CACHE_SECS = 600
KA_SCHED_MIN_BYTES = 1_000_000  # skip failed-establish crumbs
KA_SCHED_KEEP = 60
KA_SCHED_CACHE = {"at": 0.0, "starts": {}}
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def keep_awake_schedule_enabled():
    if KEEP_AWAKE_SCHEDULE_ENV_OFF:
        return False
    with PIPELINE_LOCK:
        return PIPELINE.get("keepAwake", True) is not False and PIPELINE.get("keepAwakeSchedule", True) is not False


def go_live_starts(now=None, force=False):
    """{user: [start epochs]} from recording filenames in REC_DIR + persisted history (cached)."""
    now = time.time() if now is None else now
    if not force and KA_SCHED_CACHE["at"] and now - KA_SCHED_CACHE["at"] < KA_SCHED_CACHE_SECS:
        return KA_SCHED_CACHE["starts"]
    cutoff = now - KA_SCHED_LOOKBACK_DAYS * 86400
    found = {}
    try:
        entries = list(os.scandir(REC_DIR))
    except OSError:
        entries = []
    for e in entries:
        if e.name.startswith("."):
            continue
        try:
            if not e.is_file() or e.stat().st_size < KA_SCHED_MIN_BYTES:
                continue
        except OSError:
            continue
        user, st = parse_recording_stamp(e.name)
        if user and st and cutoff <= st <= now + 60:
            found.setdefault(user, set()).add(float(int(st // 60) * 60))
    changed = False
    with PIPELINE_LOCK:
        hist = PIPELINE.setdefault("goLiveHistory", {})
        for user in set(found) | set(hist):
            old = {float(x) for x in hist.get(user) or []}
            merged = sorted(x for x in (old | found.get(user, set())) if x >= cutoff)[-KA_SCHED_KEEP:]
            if merged != sorted(old):
                changed = True
            if merged:
                hist[user] = merged
            else:
                hist.pop(user, None)
        starts = {u: list(v) for u, v in hist.items()}
    if changed:
        _pipeline_save()
    KA_SCHED_CACHE["at"] = now
    KA_SCHED_CACHE["starts"] = starts
    return starts


def _stream_day(ts):
    """(date of the stream evening, minutes since that evening's shifted midnight)."""
    dt = datetime.datetime.fromtimestamp(ts) - datetime.timedelta(minutes=KA_SCHED_DAY_SHIFT_MINS)
    return dt.date(), dt.hour * 60 + dt.minute


def _sched_clock_label(shifted_min):
    """v6.53: renamed — as `_clock_label` it replaced the restart-grace clock helper of the same
    name, so "helper keeps trying until …" showed a meaningless time."""
    m = int(shifted_min + KA_SCHED_DAY_SHIFT_MINS) % 1440
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d} {'AM' if h < 12 else 'PM'}"


def learned_go_live_windows(users, starts=None, now=None):
    """[{username, weekday, fromMin, toMin, typical, samples}] for weekdays with ≥2 past streams."""
    now = time.time() if now is None else now
    starts = go_live_starts(now) if starts is None else starts
    out = []
    for user in users:
        first = {}  # stream day -> earliest start minute (shifted)
        for ts in starts.get(user) or []:
            day, mins = _stream_day(ts)
            if day not in first or mins < first[day]:
                first[day] = mins
        by_wd = {}
        for day, mins in first.items():
            by_wd.setdefault(day.weekday(), []).append(mins)
        for wd, mins in sorted(by_wd.items()):
            if len(mins) < KA_SCHED_MIN_DAYS:
                continue
            mins.sort()
            med = mins[len(mins) // 2]
            lo, hi = mins[0], mins[-1]
            if hi - lo > KA_SCHED_MAX_SPREAD_MINS:
                lo = hi = med
            out.append({"username": user, "weekday": wd, "weekdayName": _WEEKDAYS[wd],
                        "fromMin": lo - KA_SCHED_LEAD_MINS, "toMin": hi + KA_SCHED_TAIL_MINS,
                        "from": _sched_clock_label(lo - KA_SCHED_LEAD_MINS),
                        "to": _sched_clock_label(hi + KA_SCHED_TAIL_MINS),
                        "typical": _sched_clock_label(med), "samples": len(mins)})
    return out


def _window_bounds_now(w, now):
    """(start, end) epochs of the occurrence of window w that contains now, else None."""
    today, _m = _stream_day(now)
    for off in (-1, 0, 1):
        day = today + datetime.timedelta(days=off)
        if day.weekday() != w["weekday"]:
            continue
        base = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(minutes=KA_SCHED_DAY_SHIFT_MINS)
        start = (base + datetime.timedelta(minutes=w["fromMin"])).timestamp()
        end = (base + datetime.timedelta(minutes=w["toMin"])).timestamp()
        if start <= now <= end:
            return start, end
    return None


def keep_awake_armed_users(now=None):
    """Auto-Rec armed streamers: the fresh page heartbeat, else the persisted solo-watch list."""
    now = time.time() if now is None else now
    with STATUS_FILE_LOCK:
        at = float(PAGE_SEEN.get("at") or 0.0)
        page_armed = list(PAGE_SEEN.get("armed") or [])
    if at and now - at <= PAGE_HEARTBEAT_STALE_SECS:
        return page_armed
    with SOLO_WATCH_LOCK:
        return list((SOLO_WATCH.get("users") or {}).keys())


def keep_awake_schedule_reason(now=None):
    now = time.time() if now is None else now
    if not keep_awake_schedule_enabled():
        return None
    users = keep_awake_armed_users(now)
    if not users:
        return None
    for w in learned_go_live_windows(users, now=now):
        if _window_bounds_now(w, now):
            return (f"{w['username']} usually goes live {w['weekdayName']} ~{w['typical']} "
                    f"— staying awake {w['from']}–{w['to']}")
    return None


def keep_awake_schedule_snapshot(now=None):
    now = time.time() if now is None else now
    users = keep_awake_armed_users(now)
    try:
        wins = learned_go_live_windows(users, now=now) if users else []
    except Exception as e:
        return {"enabled": keep_awake_schedule_enabled(), "error": str(e), "windows": []}
    return {"enabled": keep_awake_schedule_enabled(), "envOff": KEEP_AWAKE_SCHEDULE_ENV_OFF,
            "leadMins": KA_SCHED_LEAD_MINS, "tailMins": KA_SCHED_TAIL_MINS, "armedUsers": users,
            "windows": [{k: w[k] for k in ("username", "weekdayName", "from", "to", "typical", "samples")}
                        for w in wins],
            "activeNow": bool(any(_window_bounds_now(w, now) for w in wins))}


def _keep_awake_cmd(method):
    if method == "caffeinate":
        exe = shutil.which("caffeinate") or "/usr/bin/caffeinate"
        # -i idle sleep, -s system sleep on AC power; -w: exits on its own if the helper dies
        return [exe, "-i", "-s", "-w", str(os.getpid())]
    if method == "systemd-inhibit":
        return [shutil.which("systemd-inhibit") or "systemd-inhibit", "--what=idle:sleep",
                "--who=Twitch Auto-Recorder", "--why=Recording / processing a stream",
                "--mode=block", "sh", "-c",
                # exits on its own if the helper dies (same idea as caffeinate -w)
                f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done"]
    return None


def _keep_awake_stop_proc_locked():
    proc = KEEP_AWAKE.get("proc")
    KEEP_AWAKE["proc"] = None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except Exception:
        pass


def _keep_awake_apply(reason):
    """Hold or release the sleep assertion. Call only from keep_awake_loop (Windows state is per-thread)."""
    method = keep_awake_method()
    want = bool(reason)
    err = None
    with KEEP_AWAKE_LOCK:
        was = bool(KEEP_AWAKE["active"])
        if method == "windows":
            try:
                import ctypes  # noqa: WPS433 (Windows only)
                flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if want else 0)
                if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
                    err = "SetThreadExecutionState failed"
            except Exception as e:
                err = str(e) or type(e).__name__
        elif method in ("caffeinate", "systemd-inhibit"):
            proc = KEEP_AWAKE.get("proc")
            alive = proc is not None and proc.poll() is None
            if want and not alive:
                try:
                    KEEP_AWAKE["proc"] = subprocess.Popen(
                        _keep_awake_cmd(method), stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
                except Exception as e:
                    KEEP_AWAKE["proc"] = None
                    err = str(e) or type(e).__name__
            elif not want and (alive or proc is not None):
                _keep_awake_stop_proc_locked()
        elif want:
            err = "no sleep blocker on this computer"
        active = want and err is None and method is not None
        KEEP_AWAKE["active"] = active
        KEEP_AWAKE["error"] = err
        prev_reason = KEEP_AWAKE.get("reason")
        KEEP_AWAKE["reason"] = reason if active else None
        if active and not was:
            KEEP_AWAKE["since"] = time.time()
        elif not active:
            KEEP_AWAKE["since"] = None
    if active and not was:
        log(f"keep-awake ON via {method}: {reason}")
    elif was and not active:
        log("keep-awake OFF" + (f" ({err})" if err else " — computer may sleep normally"))
    elif active and reason != prev_reason:
        log(f"keep-awake still on: {reason}")
    elif err and want:
        log(f"keep-awake unavailable: {err}")


def keep_awake_release():
    """Drop the sleep blocker (restart / shutdown). Safe from any thread for caffeinate/inhibit."""
    with KEEP_AWAKE_LOCK:
        _keep_awake_stop_proc_locked()
        KEEP_AWAKE["active"] = False
        KEEP_AWAKE["reason"] = None
        KEEP_AWAKE["since"] = None


def keep_awake_loop():
    while True:
        try:
            _keep_awake_apply(keep_awake_desired())
        except Exception as e:
            log(f"keep-awake loop error: {e}")
        KEEP_AWAKE_WAKE.wait(KEEP_AWAKE_TICK_SECS)
        KEEP_AWAKE_WAKE.clear()


def keep_awake_snapshot():
    with PIPELINE_LOCK:
        on = PIPELINE.get("keepAwake", True) is not False
        armed_on = bool(PIPELINE.get("keepAwakeArmed"))
    with KEEP_AWAKE_LOCK:
        return {
            "supported": keep_awake_method() is not None,
            "method": keep_awake_method(),
            "enabled": on,
            "armedEnabled": armed_on,
            "active": bool(KEEP_AWAKE["active"]),
            "reason": KEEP_AWAKE.get("reason"),
            "since": KEEP_AWAKE.get("since"),
            "error": KEEP_AWAKE.get("error"),
            "armedUsers": list(KEEP_AWAKE.get("armedUsers") or []),
            "scheduleEnabled": keep_awake_schedule_enabled(),  # v6.52
            "schedule": keep_awake_schedule_snapshot(),
            "wake": wake_snapshot(),  # v6.58
        }


# ─── v6.58: WAKE A SLEEPING WINDOWS PC FOR ARMED STREAMERS' USUAL GO-LIVE TIMES ─────
# v6.52 keeps the computer awake from ~2 h before an armed streamer usually goes live, but only if
# it is still awake at that moment — a PC that idle-slept overnight (or in the afternoon) stayed
# asleep through whistleface's Tuesday-morning / Friday / Sunday stream and recorded nothing. On
# Windows the helper now registers one per-user Scheduled Task (no admin) whose one-shot triggers
# are the next learned window starts (next 8 days) with "Wake the computer to run this task" on.
# The task runs "<pythonw> twitch-recorder-server.py --wake": it resets the idle timer, pokes the
# helper (POST /api/keep-awake/wake → keep-awake + solo-watch re-check right away, so the v6.52 hold
# is in place long before Windows' 2-minute unattended-wake sleep), and starts the helper if it is
# not running. Re-registered every 15 min when the times change; removed when nothing is learned /
# armed or the go-live toggle is off. Windows' power plan must allow wake timers ("Allow wake timers"
# = Enable); the helper reads that setting (powercfg) and reports it, it never changes it. macOS can
# only schedule wakes as administrator (pmset), so on a Mac this is reported as unavailable.
WAKE_TASK = "TwitchRecorderWakeForStream"
WAKE_ENV_OFF = (os.environ.get("TWITCH_RECORDER_WAKE_FOR_STREAM") or "").strip().lower() in (
    "0", "off", "false", "no")
WAKE_HORIZON_DAYS = 8
WAKE_MAX_TRIGGERS = 24
WAKE_MIN_LEAD_SECS = 180  # a window starting in < 3 min: we're awake now, nothing to schedule
WAKE_REFRESH_SECS = max(60, int(os.environ.get("TWITCH_RECORDER_WAKE_REFRESH_SECS") or 900))
WAKE_LOCK = threading.Lock()
WAKE_STATE = {"task": None, "sig": None, "times": [], "registeredAt": None, "error": None,
              "wakeTimers": None, "wakeTimersCheckedAt": 0.0, "lastWakeAt": None, "lastWakeFrom": None,
              "checkedAt": None}
_PWR_SUB_SLEEP = "238c9fa8-0aad-41ed-83f4-97be242c8f20"
_PWR_RTCWAKE = "bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d"
WAKE_TIMERS_HINT = ("Windows wake timers are off, so the PC can't wake itself for the stream: Control Panel → "
                    "Power Options → Change plan settings → Change advanced power settings → Sleep → "
                    "Allow wake timers → Enable (Plugged in).")


def wake_for_stream_enabled():
    return (not WAKE_ENV_OFF) and keep_awake_schedule_enabled()


def upcoming_wake_times(users, now=None, wins=None):
    """[(epoch, label)] of the next learned window starts for these streamers (sorted, merged)."""
    now = time.time() if now is None else now
    if not users:
        return []
    wins = learned_go_live_windows(users, now=now) if wins is None else wins
    today, _m = _stream_day(now)
    found = {}
    for w in wins:
        for off in range(-1, WAKE_HORIZON_DAYS + 1):
            day = today + datetime.timedelta(days=off)
            if day.weekday() != w["weekday"]:
                continue
            base = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(minutes=KA_SCHED_DAY_SHIFT_MINS)
            st = (base + datetime.timedelta(minutes=w["fromMin"])).replace(second=0, microsecond=0)
            ts = st.timestamp()
            if ts < now + WAKE_MIN_LEAD_SECS or ts > now + WAKE_HORIZON_DAYS * 86400:
                continue
            lab = (f"{_WEEKDAYS[st.weekday()]} {st.strftime('%b')} {st.day} "
                   f"{_sched_clock_label(st.hour * 60 + st.minute - KA_SCHED_DAY_SHIFT_MINS)}")
            key = int(ts // 60)
            if key in found:
                if w["username"] not in found[key][2]:
                    found[key][2].append(w["username"])
            else:
                found[key] = [ts, lab, [w["username"]]]
    out = []
    for ts, lab, us in sorted(found.values()):
        if out and ts - out[-1][0] < 1800:  # two wakes within 30 min: the first one covers both
            continue
        out.append((ts, f"{lab} ({', '.join(us)})"))
    return out[:WAKE_MAX_TRIGGERS]


def _xml_esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def wake_task_xml(times, pyw, script):
    """Task Scheduler 1.2 XML: one-shot TimeTriggers (local time) that wake the computer."""
    trig = "".join(
        "    <TimeTrigger>\n"
        f"      <StartBoundary>{datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H:%M:%S')}</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "    </TimeTrigger>\n" for ts, _lab in times)
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Twitch Auto-Recorder v6.58: wake this PC before armed streamers usually go live "
        "(made by the helper; updated automatically).</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n" + trig + "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>false</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <IdleSettings>\n"
        "      <StopOnIdleEnd>false</StopOnIdleEnd>\n"
        "      <RestartOnIdle>false</RestartOnIdle>\n"
        "    </IdleSettings>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <RunOnlyIfIdle>false</RunOnlyIfIdle>\n"
        "    <WakeToRun>true</WakeToRun>\n"
        "    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_esc(pyw)}</Command>\n"
        f"      <Arguments>&quot;{_xml_esc(script)}&quot; --wake</Arguments>\n"
        f"      <WorkingDirectory>{_xml_esc(Path(script).parent)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n")


def parse_wake_timers(text):
    """powercfg /query … RTCWAKE output → 'enabled' | 'disabled' | 'important-only' | None (AC value).
    Language independent: the last two 0x… values are the AC and DC indexes."""
    vals = re.findall(r":\s*0x([0-9a-fA-F]+)\s*$", text or "", flags=re.M)
    if len(vals) < 2:
        return None
    try:
        ac = int(vals[-2], 16)
    except ValueError:
        return None
    return {0: "disabled", 1: "enabled", 2: "important-only"}.get(ac)


def check_wake_timers(run=None):
    if os.name != "nt" and run is None:
        return None
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                             errors="replace", timeout=30, **_no_window_kw()))
    try:
        r = run(["powercfg", "/query", "SCHEME_CURRENT", _PWR_SUB_SLEEP, _PWR_RTCWAKE])
        if getattr(r, "returncode", 1) != 0:
            return None
        return parse_wake_timers(getattr(r, "stdout", "") or "")
    except Exception:
        return None


def ensure_wake_task(times, run=None, tmpdir=None, script=None, pyw=None, force=False):
    """Register (times) or remove (no times) the wake task. Only touches schtasks when the set changed."""
    sig = "|".join(str(int(ts)) for ts, _l in times)
    with WAKE_LOCK:
        if not force and sig == WAKE_STATE.get("sig") and not WAKE_STATE.get("error"):
            WAKE_STATE["times"] = list(times)
            return dict(WAKE_STATE)
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                             errors="replace", timeout=60, **_no_window_kw()))
    err, task = None, None
    if not times:
        had = WAKE_STATE.get("task") or WAKE_STATE.get("sig") is None
        if had:
            try:
                run(["schtasks", "/Delete", "/F", "/TN", WAKE_TASK])  # fine if it never existed
            except Exception:
                pass
            if WAKE_STATE.get("task"):
                log("wake: no upcoming go-live to wake for — wake task removed")
    else:
        script = str(script or Path(__file__).resolve())
        pyw = pyw or windowless_python()
        import tempfile as _tmp_w
        fd, xml_path = _tmp_w.mkstemp(prefix="twitch-wake-", suffix=".xml", dir=tmpdir)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(wake_task_xml(times, pyw, script).encode("utf-16"))  # BOM + UTF-16LE
            r = run(["schtasks", "/Create", "/F", "/TN", WAKE_TASK, "/XML", xml_path])
            if getattr(r, "returncode", 1) == 0:
                task = WAKE_TASK
                log("wake: this PC will wake for " + "; ".join(l for _t, l in times[:4])
                    + (" …" if len(times) > 4 else ""))
            else:
                err = "Scheduled Task: " + ((getattr(r, "stderr", "") or getattr(r, "stdout", "") or "failed").strip()[:200])
        except Exception as e:
            err = f"Scheduled Task: {e}"
        finally:
            try:
                os.unlink(xml_path)
            except OSError:
                pass
        if err:
            log(f"wake: could not register the wake task — {err}")
    with WAKE_LOCK:
        WAKE_STATE.update({"task": task, "sig": sig if not err else None, "times": list(times) if task else [],
                           "registeredAt": time.time() if task else WAKE_STATE.get("registeredAt"),
                           "error": err, "checkedAt": time.time()})
        return dict(WAKE_STATE)


def wake_schedule_tick(now=None, run=None):
    now = time.time() if now is None else now
    times = []
    if wake_for_stream_enabled():
        times = upcoming_wake_times(keep_awake_armed_users(now), now=now)
    st = ensure_wake_task(times, run=run)
    with WAKE_LOCK:
        if run is not None or now - float(WAKE_STATE.get("wakeTimersCheckedAt") or 0) > 6 * 3600:
            WAKE_STATE["wakeTimersCheckedAt"] = now
            do_check = True
        else:
            do_check = False
    if do_check:
        wt = check_wake_timers(run=run)
        with WAKE_LOCK:
            prev = WAKE_STATE.get("wakeTimers")
            WAKE_STATE["wakeTimers"] = wt
        if wt in ("disabled", "important-only") and prev != wt and times:
            log("wake: " + WAKE_TIMERS_HINT)
    return st


def wake_schedule_loop():
    time.sleep(60)
    while True:
        try:
            wake_schedule_tick()
        except Exception as e:
            with WAKE_LOCK:
                WAKE_STATE["error"] = str(e)[:240]
            log(f"wake: tick error: {e}")
        time.sleep(WAKE_REFRESH_SECS)


def note_woken(source="task"):
    """POST /api/keep-awake/wake (from the --wake task): hold sleep off + re-check streamers now."""
    with WAKE_LOCK:
        WAKE_STATE["lastWakeAt"] = time.time()
        WAKE_STATE["lastWakeFrom"] = source
    log(f"wake: woken by the scheduled wake ({source}) — re-checking keep-awake and armed streamers now")
    KEEP_AWAKE_WAKE.set()
    try:
        SOLO_WATCH_WAKE.set()
    except NameError:
        pass
    reason = None
    try:
        reason = keep_awake_desired()
    except Exception:
        pass
    return {"ok": True, "keepAwake": reason}


def wake_snapshot(now=None):
    now = time.time() if now is None else now
    with WAKE_LOCK:
        st = dict(WAKE_STATE)
    if os.name != "nt":
        return {"supported": False, "enabled": wake_for_stream_enabled(),
                "note": ("macOS only lets an administrator schedule wakes (pmset), so keep this Mac from "
                         "sleeping (System Settings → Energy / Battery) on stream days."
                         if sys.platform == "darwin" else "Windows only")}
    wt = st.get("wakeTimers")
    return {"supported": True, "enabled": wake_for_stream_enabled(), "envOff": WAKE_ENV_OFF,
            "task": st.get("task"), "next": [l for _t, l in (st.get("times") or [])][:6],
            "nextAt": (st.get("times") or [[None]])[0][0] if st.get("times") else None,
            "registeredAt": st.get("registeredAt"), "error": st.get("error"),
            "wakeTimers": wt,
            "wakeTimersHint": WAKE_TIMERS_HINT if wt in ("disabled", "important-only") else None,
            "lastWakeAt": st.get("lastWakeAt")}


def wake_main(post_fn=None, alive_fn=None, start_fn=None, sleep_fn=None, tries=6):
    """--wake (run by the wake task): reset the idle timer, poke the helper, start it if it's not running."""
    if os.name == "nt":
        try:
            import ctypes  # noqa: WPS433
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_SYSTEM_REQUIRED)  # resets the idle timer once
        except Exception:
            pass
    sleep_fn = sleep_fn or time.sleep

    def _post():
        req = urllib.request.Request(f"http://{HOST}:{PORT}/api/keep-awake/wake", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    post_fn = post_fn or _post
    alive_fn = alive_fn or _helper_alive
    reached, started = False, False
    for i in range(max(1, tries)):
        try:
            if post_fn():
                reached = True
                break
        except Exception:
            pass
        if i == 1 and not started and not alive_fn():
            try:
                if start_fn:
                    start_fn()
                else:
                    logf = INSTALL_DIR / "helper.log"
                    out = open(logf, "ab")
                    try:
                        subprocess.Popen([windowless_python(), str(Path(__file__).resolve())], cwd=str(INSTALL_DIR),
                                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, close_fds=True,
                                         **({"creationflags": 0x00000008 | 0x00000200 | 0x08000000}
                                            if os.name == "nt" else {}))
                    finally:
                        out.close()
                started = True
            except Exception:
                pass
        sleep_fn(10)
    if start_fn is None:
        try:
            with open(INSTALL_DIR / "helper.log", "a", encoding="utf-8") as fh:
                fh.write(f"[wake {time.strftime('%Y-%m-%d %H:%M:%S')}] scheduled wake for a usual go-live — "
                         f"helper {'reached' if reached else 'not reached'}{' (started it)' if started else ''}\n")
        except OSError:
            pass
    return 0 if reached else 1


def _self_test_v658():
    """v6.58: wake times from learned windows, task XML, register/remove, wake-timer parse, --wake."""
    import tempfile as _tmp58
    import xml.dom.minidom as _md
    prev_state = dict(WAKE_STATE)
    try:
        now = datetime.datetime(2026, 10, 27, 6, 30).timestamp()  # Tue 6:30 AM local
        # whistleface: Tue usual ~10:00 AM (window 8:00 AM–), Fri ~6:55 PM (window 4:49 PM–)
        wins = [{"username": "whistleface", "weekday": 1, "fromMin": 10 * 60 - 300 - 120, "toMin": 10 * 60 - 300 + 90},
                {"username": "whistleface", "weekday": 4, "fromMin": 18 * 60 + 49 - 300 - 120, "toMin": 19 * 60 + 5 - 300 + 90},
                {"username": "otherguy", "weekday": 1, "fromMin": 10 * 60 + 10 - 300 - 120, "toMin": 12 * 60 - 300}]
        t = upcoming_wake_times(["whistleface"], now=now, wins=wins)
        labs = [l for _ts, l in t]
        assert labs[0] == "Tue Oct 27 8:00 AM (whistleface)", labs
        assert "Fri Oct 30 4:49 PM (whistleface)" in labs, labs
        assert any(l.startswith("Tue Nov 3 8:00 AM") for l in labs), labs  # next week's Tuesday too
        assert all(now + WAKE_MIN_LEAD_SECS <= ts <= now + WAKE_HORIZON_DAYS * 86400 for ts, _l in t)
        assert len(labs) == len(set(int(ts) for ts, _l in t))
        # 8:10 is within 30 min of 8:00 → merged into one wake
        assert not any("8:10 AM" in l for l in labs), labs
        # inside today's window already (9 AM) → today's Tuesday not scheduled
        t2 = upcoming_wake_times(["whistleface"], now=datetime.datetime(2026, 10, 27, 9, 0).timestamp(), wins=wins[:2])
        assert t2 and t2[0][1].startswith("Fri Oct 30 4:49 PM"), t2
        assert upcoming_wake_times([], now=now, wins=wins) == []
        xml = wake_task_xml(t, r"C:\Py\pythonw.exe", r"C:\Users\Eric\Twitch & Co\twitch-recorder-server.py")
        dom = _md.parseString(xml.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode("utf-8"))
        assert dom.getElementsByTagName("WakeToRun")[0].firstChild.data == "true"
        sb = [e.firstChild.data for e in dom.getElementsByTagName("StartBoundary")]
        assert len(sb) == len(t) and sb[0] == "2026-10-27T08:00:00", sb
        assert dom.getElementsByTagName("Arguments")[0].firstChild.data.endswith("--wake")
        assert "Twitch & Co" in dom.getElementsByTagName("Arguments")[0].firstChild.data
        calls = []

        class _R:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def run_ok(cmd):
            calls.append(list(cmd))
            if "/XML" in cmd:
                raw = Path(cmd[cmd.index("/XML") + 1]).read_bytes()
                assert raw[:2] in (b"\xff\xfe", b"\xfe\xff") and "WakeToRun" in raw.decode("utf-16")
            if cmd[0] == "powercfg":
                return _R(0, "Power Setting GUID: bd3b718a  (Allow wake timers)\n  Possible Setting Index: 000\n"
                             "Current AC Power Setting Index: 0x00000000\nCurrent DC Power Setting Index: 0x00000000\n")
            return _R(0)
        with _tmp58.TemporaryDirectory(prefix="tar-wake-") as td:
            WAKE_STATE.update({"task": None, "sig": None, "times": [], "error": None})
            st = ensure_wake_task(t, run=run_ok, tmpdir=td, script=r"C:\x\twitch-recorder-server.py", pyw="pythonw.exe")
            assert st["task"] == WAKE_TASK and calls[-1][:5] == ["schtasks", "/Create", "/F", "/TN", WAKE_TASK], calls
            assert not list(Path(td).iterdir())  # temp XML removed
            n = len(calls)
            ensure_wake_task(t, run=run_ok, tmpdir=td)  # unchanged → no schtasks call
            assert len(calls) == n
            ensure_wake_task([], run=run_ok, tmpdir=td)  # nothing to wake for → removed
            assert calls[-1][:2] == ["schtasks", "/Delete"] and WAKE_STATE["task"] is None
            n = len(calls)
            ensure_wake_task([], run=run_ok, tmpdir=td)
            assert len(calls) == n
            # schtasks refuses → error kept, retried next tick (sig cleared)
            st = ensure_wake_task(t, run=lambda c: (calls.append(c), _R(1, "", "ERROR: Access is denied."))[1], tmpdir=td)
            assert st["task"] is None and "Access is denied" in (st["error"] or "") and st["sig"] is None
        assert parse_wake_timers("Current AC Power Setting Index: 0x00000001\nCurrent DC Power Setting Index: 0x00000000\n") == "enabled"
        assert parse_wake_timers("Aktueller Wechselstrom-Einstellungsindex: 0x00000002\nAktueller Gleichstrom-Einstellungsindex: 0x00000000\n") == "important-only"
        assert parse_wake_timers("nothing here") is None
        assert check_wake_timers(run=run_ok) == "disabled"
        # --wake: helper answers on the 1st try; helper down → started once, then reached
        assert wake_main(post_fn=lambda: True, alive_fn=lambda: True, start_fn=lambda: None, sleep_fn=lambda s: None) == 0
        seq, starts = [False, False, False, True], []
        assert wake_main(post_fn=lambda: seq.pop(0), alive_fn=lambda: False, start_fn=lambda: starts.append(1),
                         sleep_fn=lambda s: None) == 0 and starts == [1]
        assert wake_main(post_fn=lambda: False, alive_fn=lambda: True, start_fn=lambda: starts.append(2),
                         sleep_fn=lambda s: None, tries=3) == 1 and starts == [1]
        r = note_woken("self-test")
        assert r["ok"] and WAKE_STATE["lastWakeAt"]
        KEEP_AWAKE_WAKE.clear()
        SOLO_WATCH_WAKE.clear()
        snap = wake_snapshot()
        json.dumps(snap)
        assert "supported" in snap
    finally:
        WAKE_STATE.clear()
        WAKE_STATE.update(prev_state)


def pipeline_enabled_for(username):
    u = (username or "").lower()
    with PIPELINE_LOCK:
        return bool(PIPELINE["enabled"]) and bool(PIPELINE["users"].get(u, True))


def set_pipeline_user_pref(username, on):
    u = safe_username(username) if username else None
    if not u:
        return
    with PIPELINE_LOCK:
        if PIPELINE["users"].get(u) == bool(on):
            return
        PIPELINE["users"][u] = bool(on)
    _pipeline_save()


def plan_split(size_bytes, duration):
    """Equal parts: count = ceil(size / 45 MB), capped so parts stay >= PART_MIN_SECS
    unless that would push a part over PART_MAX_BYTES (50 MB is the hard limit)."""
    try:
        size_bytes = float(size_bytes or 0)
        duration = float(duration or 0)
    except (TypeError, ValueError):
        return 1
    if size_bytes <= 0:
        return 1
    m = max(1, int(_math_pipe.ceil(size_bytes / PART_TARGET_BYTES)))
    if duration > 0 and PART_MIN_SECS > 0:
        cap = max(1, int(duration // PART_MIN_SECS))
        if m > cap and (size_bytes / cap) < PART_MAX_BYTES:
            m = cap
    return m


def _part_name(base, i, n):
    return f"{base}-music-part{i}of{n}.ogg"


def split_music_for_drive(job_id, src, progress=None):
    """Cut a -music file into equal-length parts (<50 MB). Returns (parts_dir, [names], part_secs)."""
    src = Path(src)
    ff = which_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    info = probe_media(src)
    dur = float(info["duration"] or 0)
    size = os.stat(src).st_size
    base = music_base_stem(src.name)
    copy_ok = info["audioCodec"] == "vorbis" and "ogg" in (info["container"] or "")
    codec_args = ["-c", "copy"] if copy_ok else MUSIC_OGG_ARGS
    DRIVE_PARTS_DIR.mkdir(parents=True, exist_ok=True)
    m = plan_split(size, dur)
    for attempt in range(6):
        if progress:
            progress(f"Splitting into {m} part{'s' if m != 1 else ''} (~{dur / max(1, m) / 60:.0f} min each)…", None)
        tmp = DRIVE_PARTS_DIR / f".{base}.splitting-{uuid.uuid4().hex[:6]}"
        tmp.mkdir(parents=True)
        _register_work_path(tmp)
        try:
            cmd = [ff, "-nostdin", "-v", "error", "-y", "-i", str(src), "-map", "0:a:0", "-vn", *codec_args]
            if m > 1:
                times = ",".join(f"{dur * i / m:.3f}" for i in range(1, m))
                cmd += ["-f", "segment", "-segment_format", "ogg", "-segment_times", times,
                        "-reset_timestamps", "1", "-segment_start_number", "1", str(tmp / "p%03d.ogg")]
            else:
                cmd += ["-f", "ogg", str(tmp / "p001.ogg")]
            if job_id:
                rc, tail = _job_run_proc(job_id, cmd, timeout=3 * 3600)
            else:
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=3 * 3600)  # v6.51
                rc, tail = r.returncode, (r.stderr or "")
            if rc != 0:
                raise RuntimeError(f"ffmpeg split failed: {tail[-400:]}")
            parts = sorted(p for p in tmp.glob("p*.ogg") if p.stat().st_size > 0)
            if not parts:
                raise RuntimeError("ffmpeg split produced no parts")
            biggest = max(p.stat().st_size for p in parts)
            if biggest >= PART_MAX_BYTES and attempt < 5:
                m += 1
                shutil.rmtree(tmp, ignore_errors=True)
                continue
            n = len(parts)
            names = []
            for i, p in enumerate(parts, 1):
                nm = _part_name(base, i, n)
                os.replace(p, tmp / nm)
                names.append(nm)
            out_dir = DRIVE_PARTS_DIR / base
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            os.replace(tmp, out_dir)
            return out_dir, names, (dur / n if n else dur)
        finally:
            _unregister_work_path(tmp)
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
    raise RuntimeError("could not split under the size limit")


def atomic_copy(src, dest):
    """Copy src → dest via a temp file in dest's folder + rename (source is kept)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / f".{dest.name}.partial-{uuid.uuid4().hex[:6]}"
    try:
        with open(src, "rb") as fin, open(part, "wb") as fout:
            shutil.copyfileobj(fin, fout, length=4 * 1024 * 1024)
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(part, dest)
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
    return dest


def drive_permission_message():
    """macOS privacy (TCC) blocks background apps from File Provider folders until allowed."""
    exe = os.path.realpath(sys.executable or "python3")
    if os.name == "nt":  # v6.38
        return ("Windows refused access to the Google Drive folder (permission denied). Make sure Google "
                "Drive for Desktop is running and signed in, the target folder is not read-only, and no "
                "file with the same name is open — the helper retries every 5 min.")
    try:  # v6.56: name every path macOS may check (framework Python re-launches Python.app)
        paths = fda_paths()
        exe = paths[0]
        also = (f" (if it stays blocked, also add {paths[1]})" if len(paths) > 1 else "")
    except Exception:
        also = ""
    try:  # v6.60: launchd runs the /usr/bin/python3 shim → FDA can't apply; helper repairs it itself
        lp = launcher_snapshot()
        if lp.get("shim") or lp.get("repaired"):
            return ("macOS is blocking the helper from Google Drive (Operation not permitted). The helper's "
                    f"login item started it through {lp.get('plistProgram')} (Apple's python3 shim), which "
                    "Full Disk Access can't cover — the helper will repair this itself and relaunch when idle "
                    f"(or restart the helper). If it stays blocked, add {exe} under Full Disk Access{also}.")
    except Exception:
        pass
    return ("macOS is blocking the helper from Google Drive (Operation not permitted — Full Disk Access). "
            "Open System Settings → Privacy & Security → Full Disk Access, press +, add "
            f"{exe} (⌘⇧G to type the path){also} and turn it on — the helper re-checks every minute "
            "and sends everything waiting on its own.")


def _my_drive_dir(root):
    for sc in root.get("shortcuts") or []:
        if sc.get("label") == "My Drive":
            return sc["path"]
    return root["path"]


def resolve_delivery_target(roots=None, create=False):
    """Configured target, else an existing My Drive folder named like the recordings
    folder ("Twitch", "TwitchRecordings", …), else My Drive/Twitch Recordings/Music only."""
    if roots is None:
        roots = detect_drive_roots()
    if not roots:
        return None, "auto", "Google Drive for Desktop not found"
    with PIPELINE_LOCK:
        configured = PIPELINE.get("target") or ""
    if configured:
        real, _root, err = validate_drive_path(configured, roots)
        if err:
            return None, "configured", f"target folder: {err}"
        if create:
            os.makedirs(real, exist_ok=True)
        return real, "configured", None
    for r in roots:
        md = _my_drive_dir(r)
        try:
            for entry in sorted(os.scandir(md), key=lambda e: e.name.lower()):
                if entry.name.lower() in AUTO_TARGET_NAMES and entry.is_dir(follow_symlinks=True):
                    real = os.path.realpath(entry.path)
                    if any(_path_within(real, rr["path"]) for rr in roots):
                        return real, "auto", None
        except PermissionError:
            return None, "auto", drive_permission_message()
        except OSError:
            continue
    target = os.path.join(_my_drive_dir(roots[0]), *DEFAULT_TARGET_REL)
    if create:
        os.makedirs(target, exist_ok=True)
        target = os.path.realpath(target)
        if not any(_path_within(target, rr["path"]) for rr in roots):
            return None, "auto", "default target escapes Google Drive"
    return target, "auto-default", None


def _delivery_update(base, **kw):
    with PIPELINE_LOCK:
        d = PIPELINE["deliveries"].get(base)
        if d is None:
            return None
        d.update(kw)
        d["updatedAt"] = time.time()
        snap = dict(d)
    _pipeline_save()
    if "status" in kw:
        _sync_jobs_with_delivery(base, snap)
    return snap


def _sync_jobs_with_delivery(base, snap):
    """Keep finished pipeline/split job text in step with the delivery state (waiting → sent)."""
    n = len(snap.get("parts") or [])
    pl = "s" if n != 1 else ""
    st = snap.get("status")
    if st == "sent":
        msg = f"Sent to Drive ({n} part{pl})"
        if snap.get("localPartsCleared"):
            msg += " · local parts cleared"
    elif st == "error":
        msg = f"{n} parts ready — Drive send failed: {snap.get('error')}"
    elif st == "waiting":
        msg = f"Waiting for Drive ({n} part{pl} ready)"
    elif st == "covered":  # v6.50
        msg = snap.get("note") or "Already in Drive from another computer — skipped the duplicate"
    else:
        return
    with MUSIC_JOBS_LOCK:
        for job in MUSIC_JOBS.values():
            if job.get("base") == base and job.get("status") == "done" and job.get("source") in ("pipeline", "split"):
                job["progress"] = msg
                job["deliveryStatus"] = st


def _safe_drive_parts_base(base):
    """Return a single path-safe folder name under DRIVE_PARTS_DIR, or None."""
    if not base or not isinstance(base, str):
        return None
    b = base.strip()
    if not b or b in (".", "..") or chr(0) in b:
        return None
    if "/" in b or "\\" in b:
        return None
    if os.path.basename(b) != b:
        return None
    return b


def clear_local_drive_parts(base):
    """Remove ~/TwitchRecordings/Drive parts/<base>/ after a successful send (or on delete).

    Path-safe: only under DRIVE_PARTS_DIR, no traversal. Tolerates missing files.
    Never touches the full -music.ogg in REC_DIR. Returns bytes freed.
    """
    b = _safe_drive_parts_base(base)
    if not b:
        return 0
    try:
        parts_root = DRIVE_PARTS_DIR.resolve()
        target = (DRIVE_PARTS_DIR / b).resolve()
    except OSError:
        return 0
    try:
        target.relative_to(parts_root)
    except ValueError:
        log(f"drive parts clear refused (escape): base={b!r}")
        return 0
    if target == parts_root:
        return 0
    try:
        if not target.exists() or not target.is_dir():
            return 0
    except OSError:
        return 0
    bytes_freed = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(str(target)):
            for fn in filenames:
                fp = Path(dirpath) / fn
                try:
                    bytes_freed += max(0, int(fp.stat().st_size))
                except OSError:
                    pass
        shutil.rmtree(str(target), ignore_errors=True)
        log(f"drive parts cleared base={b} freed={bytes_freed} bytes")
    except Exception as e:
        log(f"drive parts clear failed base={b}: {e}")
    return max(0, int(bytes_freed))


def clear_sent_local_drive_parts_backfill():
    """v6.37 startup: drop leftover Drive parts/<base>/ for deliveries already status=sent."""
    with PIPELINE_LOCK:
        bases = [
            b for b, d in PIPELINE["deliveries"].items()
            if isinstance(d, dict) and d.get("status") == "sent" and not d.get("localPartsCleared")
        ]
    if not bases:
        return
    total = 0
    for base in bases:
        freed = clear_local_drive_parts(base)
        total += freed
        _delivery_update(base, localPartsCleared=True, partsDir=None)
    log(f"drive parts sent-backfill: {len(bases)} delivery(ies), freed={total} bytes")


def remove_drive_parts_for_delete(base):
    """Clear Drive parts/<base>/ and update delivery state so UI does not claim parts ready.

    Waiting/error deliveries are dropped (parts gone — retry would need Re-split).
    Sent deliveries keep their history badge with localPartsCleared.
    Returns bytes freed.
    """
    b = _safe_drive_parts_base(base)
    if not b:
        return 0
    freed = clear_local_drive_parts(b)
    with PIPELINE_LOCK:
        d = PIPELINE["deliveries"].get(b)
        if d is not None:
            st = d.get("status")
            if st == "sent":
                d["localPartsCleared"] = True
                d["partsDir"] = None
                d["updatedAt"] = time.time()
            else:
                # waiting / error / anything else — parts no longer on disk
                PIPELINE["deliveries"].pop(b, None)
    _pipeline_save()
    return freed


def register_delivery(base, music_name, parts_dir, names, part_secs, source):
    sizes = []
    for nm in names:
        try:
            sizes.append(os.stat(Path(parts_dir) / nm).st_size)
        except OSError:
            sizes.append(0)
    now = time.time()
    with PIPELINE_LOCK:
        PIPELINE["deliveries"][base] = {
            "base": base, "musicName": music_name, "partsDir": str(parts_dir),
            "parts": list(names), "sizes": sizes, "partSecs": round(part_secs, 1),
            "status": "waiting", "target": None, "sentAt": None, "error": None,
            "source": source, "createdAt": now, "updatedAt": now, "lastAttempt": 0,
            "localPartsCleared": False,  # v6.37
        }
        # keep the state file small
        if len(PIPELINE["deliveries"]) > 300:
            old = sorted(PIPELINE["deliveries"].values(), key=lambda d: d.get("updatedAt") or 0)
            for d in old[: len(old) - 300]:
                PIPELINE["deliveries"].pop(d.get("base"), None)
    _pipeline_save()


def deliver_one(base):
    """Copy one base's parts into the Drive target. Returns the delivery snapshot."""
    with PIPELINE_LOCK:
        d = PIPELINE["deliveries"].get(base)
        if not d:
            return None
        d = dict(d)
    # v6.37: already sent (local parts may already be cleared) — do not re-copy
    # v6.50: covered = another computer already put this stream in Drive
    if d.get("status") in ("sent", "covered"):
        return d
    roots = detect_drive_roots()
    if not roots:
        return _delivery_update(base, status="waiting", error=None, lastAttempt=time.time(),
                                note="Waiting for Drive (Drive for Desktop not found)")
    try:
        target, _kind, err = resolve_delivery_target(roots, create=True)
    except PermissionError:
        target, err = None, drive_permission_message()
    except OSError as e:
        target, err = None, f"cannot create target folder: {e}"
    if err or not target:
        return _delivery_update(base, status="error", error=err or "no target", lastAttempt=time.time())
    # v6.50: one copy per stream across computers (manual Split for Drive always sends)
    one_copy = one_copy_enabled()
    cov_user, cov_start = parse_recording_stamp(base)
    cov_dur = float(d.get("partSecs") or 0) * len(d.get("parts") or [])
    if one_copy and d.get("source") == "stream":
        kind, doc = find_other_copy(target, cov_user, cov_start, cov_dur)
        if kind == "sent":
            return mark_delivery_covered(base, doc, target)
        if kind == "sending":
            return _delivery_update(
                base, status="waiting", error=None, target=target, lastAttempt=time.time(),
                note=(f"{doc.get('computer') or 'Another computer'} is sending this stream to Drive right now — "
                      "checking again in a minute (this computer sends its own parts if that doesn't finish)"))
    _delivery_update(base, status="sending", error=None, target=target, lastAttempt=time.time(), note=None)
    if one_copy:
        write_sent_manifest(target, base, "sending", duration=cov_dur, user=cov_user, start=cov_start)
    DELIVERY_STATE["busy"] = True
    DELIVERY_STATE["base"] = base
    placed = []
    try:
        try:
            for nm in d.get("parts") or []:
                src = Path(d["partsDir"]) / nm
                if not src.is_file():
                    raise RuntimeError(f"local part missing ({nm}) — use Split for Drive again")
                # v6.38: same name+size already in Drive → skip; different size → <name>-<Computer>
                # (a manual Re-split from this computer may replace its own earlier parts)
                dest, _act = place_in_drive(src, Path(target), nm, allow_replace=(d.get("source") == "manual"))
                try:
                    placed.append({"name": Path(dest).name, "size": int(src.stat().st_size)})
                except OSError:
                    placed.append({"name": Path(dest).name, "size": -1})
        except Exception as e:
            log(f"drive delivery error base={base}: {e}")
            msg = drive_permission_message() if isinstance(e, PermissionError) else (str(e) or type(e).__name__)
            if one_copy:  # v6.50: drop our "sending" claim so other computers don't wait on it
                write_sent_manifest(target, base, "error", parts=placed, duration=cov_dur, user=cov_user, start=cov_start)
            return _delivery_update(base, status="error", error=msg)
        # Every part is in Drive. Mark sent first so a crash mid-clear does not leave status=waiting
        # with missing parts; v6.51: nothing after this point may turn a finished send into "error".
        snap = _delivery_update(base, status="sent", sentAt=time.time(), target=target, error=None)
        try:
            if one_copy:  # v6.50: tell the other computers this stream is in Drive
                write_sent_manifest(target, base, "sent", parts=placed, duration=cov_dur, user=cov_user, start=cov_start)
            log(f"drive delivery sent base={base} parts={len(d.get('parts') or [])} → {target}")
            # v6.37: free local Drive parts after successful send (full -music.ogg kept)
            freed = clear_local_drive_parts(base)
            snap = _delivery_update(
                base, status="sent", localPartsCleared=True, partsDir=None,
            ) or snap
            if freed:
                log(f"drive parts post-send cleared base={base} freed={freed} bytes")
        except Exception as e:
            log(f"drive delivery base={base}: sent, but tidy-up after the send failed: {e}")
        return snap
    finally:
        DELIVERY_STATE["busy"] = False
        DELIVERY_STATE["base"] = None


def cleanup_drive_target_partials(target, reason="tick"):
    """Remove stale .{name}.partial-{hex} temps under the Auto-send Drive target
    and its Originals/ subfolder (one level each — not the whole Drive tree).

    Age rule: delete if mtime older than ~1 hour (match local REC_DIR cleanup).
    When delivery is not busy, also delete leftovers older than ~5 minutes.
    Never delete a partial that could belong to an in-flight atomic_copy.
    PermissionError/OSError (e.g. macOS FDA) are soft — log and continue.
    """
    removed = 0
    freed = 0
    if not target:
        return removed, freed
    root = Path(target)
    dirs = [root]
    try:
        originals = root / ORIGINALS_SUBDIR
        if originals.is_dir():
            dirs.append(originals)
        status_dir = root / STATUS_SUBDIR  # v6.41
        if status_dir.is_dir():
            dirs.append(status_dir)
            sent_dir = status_dir / SENT_MANIFEST_SUBDIR  # v6.50
            if sent_dir.is_dir():
                dirs.append(sent_dir)
    except OSError as e:
        log(f"drive target partial cleanup ({reason}): Originals check skipped: {e}")
    now = time.time()
    busy = bool(DELIVERY_STATE.get("busy"))
    min_age = DRIVE_PARTIAL_STALE_SECS if busy else DRIVE_PARTIAL_IDLE_SECS
    for d in dirs:
        try:
            for p in d.glob(".*.partial-*"):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                    age = now - float(st.st_mtime)
                    if age < min_age:
                        continue
                    sz = int(st.st_size)
                    p.unlink()
                    removed += 1
                    freed += sz
                except (PermissionError, OSError) as e:
                    log(f"drive target partial cleanup ({reason}): skip {p.name}: {e}")
                    continue
        except (PermissionError, OSError) as e:
            log(f"drive target partial cleanup ({reason}): scan {d}: {e}")
            continue
    if removed:
        log(f"drive target partial cleanup ({reason}): {removed} items, {freed} bytes")
    return removed, freed


def maybe_cleanup_drive_target_partials(reason="tick"):
    """Resolve Auto-send target (no create) and sweep stale .partial-* temps."""
    try:
        roots = detect_drive_roots()
        if not roots:
            return 0, 0
        target, _kind, err = resolve_delivery_target(roots, create=False)
        if err or not target:
            return 0, 0
        return cleanup_drive_target_partials(target, reason=reason)
    except (PermissionError, OSError) as e:
        log(f"drive target partial cleanup ({reason}) skipped: {e}")
        return 0, 0
    except Exception as e:
        log(f"drive target partial cleanup ({reason}) skipped: {e}")
        return 0, 0


def delivery_loop():
    """Deliver waiting parts whenever Drive for Desktop is (or becomes) available."""
    while True:
        DELIVERY_WAKE.wait(DELIVERY_POLL_SECS)
        DELIVERY_WAKE.clear()
        try:
            # v6.39: sweep stale Drive .partial-* under delivery target (+ Originals/)
            maybe_cleanup_drive_target_partials(reason="tick")
            forced = set(DELIVERY_STATE["force"])
            DELIVERY_STATE["force"].clear()
            now = time.time()
            # v6.38: originals first (they are queued before Demucs starts)
            orig_todo = originals_due(forced, now)
            if orig_todo:
                if not detect_drive_roots():
                    for name in orig_todo:
                        _orig_update(name, status="waiting", lastAttempt=now,
                                     note="Waiting for Drive (Drive for Desktop not found)")
                else:
                    for name in orig_todo:
                        deliver_original(name)
            with PIPELINE_LOCK:
                todo = []
                for base, d in PIPELINE["deliveries"].items():
                    st = d.get("status")
                    if st == "waiting" or base in forced or "*" in forced and st in ("waiting", "error"):
                        todo.append(base)
                    elif st == "error" and now - float(d.get("lastAttempt") or 0) > DELIVERY_ERROR_RETRY_SECS:
                        todo.append(base)
            if not todo:
                continue
            if not detect_drive_roots():
                for base in todo:
                    if base in forced:
                        _delivery_update(base, status="waiting", note="Waiting for Drive (Drive for Desktop not found)",
                                         lastAttempt=now)
                continue
            for base in todo:
                deliver_one(base)
        except Exception as e:
            log(f"delivery loop error: {e}")


def request_delivery(base=None):
    DELIVERY_STATE["force"].add(base or "*")
    DELIVERY_WAKE.set()


def deliveries_snapshot(limit=60):
    with PIPELINE_LOCK:
        items = sorted(PIPELINE["deliveries"].values(), key=lambda d: d.get("updatedAt") or 0, reverse=True)
        out = []
        for d in items[:limit]:
            out.append({k: d.get(k) for k in (
                "base", "musicName", "parts", "sizes", "partSecs", "status", "target",
                "sentAt", "error", "note", "source", "createdAt", "updatedAt",
                "localPartsCleared", "coveredBy")})
    return out


def pipeline_status_payload():
    roots = detect_drive_roots()
    target, kind, err = resolve_delivery_target(roots, create=False) if roots else (None, "auto", "Google Drive for Desktop not found")
    with PIPELINE_LOCK:
        prefs = {"enabled": PIPELINE["enabled"], "users": dict(PIPELINE["users"]), "target": PIPELINE["target"],
                 "origEnabled": bool(PIPELINE.get("origEnabled", True)),
                 "origUsers": dict(PIPELINE.get("origUsers") or {}),
                 "statusToDrive": PIPELINE.get("statusToDrive", True) is not False,
                 "autoUpdate": PIPELINE.get("autoUpdate", True) is not False,  # v6.43
                 "voiceQuality": _norm_voice_quality(PIPELINE.get("voiceQuality")) or "auto",  # v6.45
                 "oneCopy": PIPELINE.get("oneCopy", True) is not False}  # v6.50
    return {
        "ok": True, "driveFound": bool(roots), "prefs": prefs,
        "resolvedTarget": target, "targetKind": kind, "targetError": err,
        "defaultTargetHint": 'a My Drive folder named "Twitch" (or TwitchRecordings) if present, else My Drive/Twitch Recordings/Music only',
        "partTargetBytes": PART_TARGET_BYTES, "partMaxBytes": PART_MAX_BYTES, "partMinSecs": PART_MIN_SECS,
        "deliveries": deliveries_snapshot(200),
        "originals": originals_snapshot(200),  # v6.38
        "originalsFolder": (os.path.join(target, ORIGINALS_SUBDIR) if target else None),
        "computer": computer_name(),
        "statusFile": status_file_snapshot(),  # v6.41
        "voiceQuality": voice_quality_snapshot(),  # v6.45
        "oneCopy": one_copy_enabled(),  # v6.50
    }


def api_pipeline_prefs(body):
    body = body if isinstance(body, dict) else {}
    changed = False
    with PIPELINE_LOCK:
        if "enabled" in body:
            PIPELINE["enabled"] = bool(body.get("enabled"))
            changed = True
        users = body.get("users")
        if isinstance(users, dict):
            for k, v in users.items():
                u = safe_username(k)
                if u:
                    PIPELINE["users"][u] = bool(v)
                    changed = True
        if "origEnabled" in body:  # v6.38
            PIPELINE["origEnabled"] = bool(body.get("origEnabled"))
            changed = True
        if "keepAwake" in body:  # v6.40
            PIPELINE["keepAwake"] = bool(body.get("keepAwake"))
            changed = True
            KEEP_AWAKE_WAKE.set()
        if "keepAwakeArmed" in body:  # v6.40
            PIPELINE["keepAwakeArmed"] = bool(body.get("keepAwakeArmed"))
            changed = True
            KEEP_AWAKE_WAKE.set()
        if "keepAwakeSchedule" in body:  # v6.52
            PIPELINE["keepAwakeSchedule"] = bool(body.get("keepAwakeSchedule"))
            changed = True
            KEEP_AWAKE_WAKE.set()
        if "statusToDrive" in body:  # v6.41
            PIPELINE["statusToDrive"] = bool(body.get("statusToDrive"))
            changed = True
            STATUS_FILE_WAKE.set()
        if "voiceQuality" in body:  # v6.45
            vq = _norm_voice_quality(body.get("voiceQuality"))
            if vq and PIPELINE.get("voiceQuality") != vq:
                log(f"voice quality pref: {vq}")
            if vq:
                PIPELINE["voiceQuality"] = vq
                changed = True
        if "autoUpdate" in body:  # v6.43
            on = bool(body.get("autoUpdate"))
            if PIPELINE.get("autoUpdate", True) is not on:
                log(f"auto-update pref: {'on' if on else 'off'}")
                AUTO_UPDATE_WAKE.set()
                STATUS_FILE_WAKE.set()
            PIPELINE["autoUpdate"] = on
            changed = True
        if "streamlinkUpdate" in body:  # v6.55
            on = bool(body.get("streamlinkUpdate"))
            if PIPELINE.get("streamlinkUpdate", True) is not on:
                log(f"streamlink update pref: {'on' if on else 'off'}")
                SL_UPDATE_WAKE.set()
                STATUS_FILE_WAKE.set()
            PIPELINE["streamlinkUpdate"] = on
            changed = True
        if "oneCopy" in body:  # v6.50: only one computer sends each stream to Drive
            on = bool(body.get("oneCopy"))
            if PIPELINE.get("oneCopy", True) is not on:
                log(f"one-copy pref: {'on' if on else 'off'}")
                STATUS_FILE_WAKE.set()
            PIPELINE["oneCopy"] = on
            changed = True
        if "soloWatch" in body:  # v6.49: keep watching when page closed
            # Persist on SOLO_WATCH (dedicated file); also mirror into PIPELINE for status dumps
            on = bool(body.get("soloWatch"))
            PIPELINE["soloWatch"] = on
            changed = True
            try:
                solo_watch_set_enabled(on, save=True)
            except Exception as e:
                log(f"solo watch pref failed: {e}")
        ousers = body.get("origUsers")
        if isinstance(ousers, dict):
            for k, v in ousers.items():
                u = safe_username(k)
                if u:
                    PIPELINE.setdefault("origUsers", {})[u] = bool(v)
                    changed = True
    if "target" in body:
        t = body.get("target") or ""
        if t:
            real, _root, err = validate_drive_path(str(t))
            if err:
                return {"ok": False, "error": f"target folder: {err}"}, 400
            if not os.path.isdir(real):
                return {"ok": False, "error": "target folder not found"}, 404
            t = real
        with PIPELINE_LOCK:
            PIPELINE["target"] = t
        changed = True
        request_delivery(None)
    if changed:
        _pipeline_save()
    return pipeline_status_payload(), 200


def _pipeline_progress(job_id):
    def progress(msg, pct=None):
        if pct is not None:
            _set_music_job(job_id, progress=msg, progressPct=pct)
        else:
            _set_music_job(job_id, progress=msg)
    return progress


def _split_and_deliver(job_id, music_path, source, progress, pct=95):
    _set_music_job(job_id, phase="split")
    progress("Splitting for Drive…", pct)
    parts_dir, names, part_secs = split_music_for_drive(job_id, music_path, progress)
    base = music_base_stem(Path(music_path).name)
    register_delivery(base, Path(music_path).name, parts_dir, names, part_secs, source)
    _set_music_job(job_id, phase="deliver", base=base, partsCount=len(names), partSecs=round(part_secs, 1),
                   outputPath=str(Path(parts_dir) / names[0]))
    progress(f"{len(names)} parts ready — sending to Drive…", 99)
    snap = deliver_one(base) or {}
    st = snap.get("status")
    if st == "sent":
        msg = f"Sent to Drive ({len(names)} part{'s' if len(names) != 1 else ''})"
        if snap.get("localPartsCleared"):
            msg += " · local parts cleared"
    elif st == "error":
        msg = f"{len(names)} parts ready — Drive send failed: {snap.get('error')}"
    elif st == "covered":  # v6.50
        msg = snap.get("note") or "Already in Drive from another computer — skipped the duplicate"
    else:
        msg = f"Waiting for Drive ({len(names)} part{'s' if len(names) != 1 else ''} ready)"
    return base, names, part_secs, st, msg


def _pipeline_attempt(job_id):
    """One try at original-wait → voice removal → encode → split → Drive. Raises on failure
    (v6.61: pipeline_worker decides whether to try again)."""
    work = None
    slot_held = False
    progress = _pipeline_progress(job_id)
    segs = list(_job_get(job_id, "segments") or [])
    redo = bool(_job_get(job_id, "redo"))
    try:
        _check_cancel(job_id)
        paths = []
        for s in segs:
            p = REC_DIR / s
            try:
                if p.is_file() and p.stat().st_size > SCRUB_MAX_BYTES:
                    paths.append(p)
            except OSError:
                continue
        if not paths:
            raise RuntimeError("no finished segments on disk")
        wait_for_originals(job_id, [p.name for p in paths], progress)  # v6.38: original → Drive first
        existing = find_existing_sibling(paths[0].name, "music") if len(paths) == 1 else None
        made = _job_get(job_id, "musicMade")  # v6.61: an earlier try already finished voice removal
        if made and (REC_DIR / basename_of(made)).is_file():
            music_path = REC_DIR / basename_of(made)
            _set_music_job(job_id, status="running", info="reused the -music file from the earlier try")
        elif existing and not redo:
            music_path = existing
            _set_music_job(job_id, status="running", info="reused existing -music file")
        else:
            infos = [probe_media(p) for p in paths]
            total = sum(float(i["duration"] or 0) for i in infos)
            lead = detect_leading_silence(paths[0])
            note = f"{len(paths)} segment{'s' if len(paths) != 1 else ''}, {total / 60:.1f} min"
            if lead >= 1.0:
                note += f" · leading silence {lead:.0f}s (info only)"
            _set_music_job(job_id, info=note, duration=total)
            # v6.50: another computer already sent this stream's parts → skip hours of voice removal
            cov = one_copy_precheck(_job_get(job_id, "username"), paths[0].name, total)
            if cov:
                doc, cov_target = cov
                cbase = music_base_stem(paths[0].name)
                snap = register_covered_delivery(cbase, doc, cov_target)
                cmsg = (snap or {}).get("note") or "Already in Drive from another computer — skipped"
                _set_music_job(job_id, status="done", phase="done", progress=cmsg, progressPct=100,
                               deliveryStatus="covered", base=cbase, error=None, finishedAt=time.time())
                log(f"pipeline skipped job={job_id}: {cmsg}")
                return
            # Disk-low pause (Auto): wait while under diskWarn instead of starting demucs.
            paused = False
            while disk_status().get("diskWarn"):
                if not paused:
                    log(f"pipeline job={job_id}: paused — disk low")
                    paused = True
                _set_music_job(job_id, status="queued", progress="Paused — disk low (<2 GB free); waiting for space…")
                _check_cancel(job_id)
                time.sleep(5)
            quality = _job_get(job_id, "quality") or resolve_voice_quality(None)[0] or "better"  # v6.45
            _set_music_job(job_id, status="queued", phase="wait", progress=voice_wait_msg(quality))
            while True:
                _check_cancel(job_id)
                if DEMUCS_SLOTS.acquire(timeout=0.5):
                    slot_held = True
                    break
            _check_cancel(job_id)
            _set_music_job(job_id, status="running", phase="demucs" if quality != "fast" else "fast")
            work = Path(tempfile.mkdtemp(prefix="music-only-", dir=str(REC_DIR)))
            _register_work_path(work)
            parts = voice_removed_parts(job_id, paths, work, total, progress, quality, pct_lo=5.0, pct_hi=85.0)
            _set_music_job(job_id, phase="encode")
            tmp_out = encode_joined_ogg(job_id, parts, work / "out.ogg", total, progress,
                                        pct_lo=85.0, pct_hi=94.0, codec_args=MUSIC_OGG_ARGS)
            if redo and len(paths) == 1:
                delete_music_siblings(paths[0].name)
            dest = _unique_music_dest(paths[0].name)
            atomic_publish(tmp_out, dest)
            music_path = dest
            _set_music_job(job_id, outputName=dest.name, musicMade=dest.name,  # v6.61
                           outputUrl="/api/download/" + urllib.parse.quote(dest.name))
            _pending_job_note(segs[0], musicMade=dest.name)
            # Free the demucs slot before split/delivery so the next job can start.
            DEMUCS_SLOTS.release()
            slot_held = False
            shutil.rmtree(work, ignore_errors=True)
            _unregister_work_path(work)
            work = None
        _set_music_job(job_id, outputName=Path(music_path).name,
                       outputUrl="/api/download/" + urllib.parse.quote(Path(music_path).name))
        base, names, part_secs, st, msg = _split_and_deliver(job_id, music_path, "stream", progress)
        tries = int(_job_get(job_id, "autoRetries") or 0)
        if tries:
            msg += f" (worked on automatic try {tries + 1})"
            with PIPELINE_LOCK:
                PIPELINE_RETRY_STATS["recovered"] = int(PIPELINE_RETRY_STATS.get("recovered") or 0) + 1
        _set_music_job(job_id, status="done", phase="done", progress=msg, progressPct=100,
                       deliveryStatus=st, error=None, finishedAt=time.time(), retryAt=None)
        log(f"pipeline done job={job_id} music={Path(music_path).name} parts={len(names)} delivery={st}")
    finally:
        if slot_held:
            try:
                DEMUCS_SLOTS.release()
            except Exception:
                pass
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
            _unregister_work_path(work)


# ─── v6.61: AFTER-STREAM VOICE REMOVAL TRIES AGAIN BY ITSELF ─────────────────────────────────────
# A 4-hour stream is ~24 demucs runs. One of them dying (macOS memory pressure killing it, a sleep /
# wake blip, Drive for Desktop briefly locking a file, ffmpeg hiccup) used to fail the whole job:
# every finished chunk was thrown away, the job was marked "error", and that stream never got its
# music-only parts in Drive unless someone pressed Retry. Now (1) a failed chunk is tried once more on
# the CPU before giving up, and (2) a failed after-stream job waits and starts again on its own
# (10 min, then 1 h), keeping the -music file if voice removal had already finished. Errors that a
# retry can't fix (ffmpeg missing, torchaudio has no audio backend, recording gone) stop at once.
# The retry count is kept in .drive-pipeline.json pendingJobs, so a helper restart doesn't reset it.
def _parse_retry_delays(raw, default=(600.0, 3600.0)):
    out = []
    for x in str(raw or "").replace(";", ",").split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(max(0.0, float(x)))
        except ValueError:
            return tuple(default)
    return tuple(out) if out else tuple(default)


PIPELINE_RETRY_DELAYS = _parse_retry_delays(os.environ.get("TWITCH_RECORDER_PIPELINE_RETRY_SECS"))
try:
    PIPELINE_RETRIES = max(0, int(os.environ.get("TWITCH_RECORDER_PIPELINE_RETRIES") or 2))
except ValueError:
    PIPELINE_RETRIES = 2
try:
    DEMUCS_CHUNK_RETRIES = max(0, int(os.environ.get("TWITCH_RECORDER_DEMUCS_CHUNK_RETRIES") or 1))
except ValueError:
    DEMUCS_CHUNK_RETRIES = 1
DEMUCS_CHUNK_RETRY_PAUSE_SECS = _env_secs("TWITCH_RECORDER_DEMUCS_CHUNK_RETRY_PAUSE_SECS", 20, 0)
PIPELINE_RETRY_STATS = {"scheduled": 0, "recovered": 0, "gaveUp": 0, "permanent": 0,
                        "lastError": None, "lastAt": 0.0, "lastName": None}
_CHUNK_RETRY_STATS = {"retries": 0, "saved": 0}
_PERMANENT_PIPELINE_ERRORS = (
    "no finished segments on disk", "ffmpeg not found", "ffmpeg not installed",
    "no audio backend", "appropriate backend", "torchcodec", "could not save its audio file",
    "mono audio", "demucs not installed", "demucs is not installed",
)


def _pipeline_error_is_permanent(msg):
    low = str(msg or "").lower()
    return any(h in low for h in _PERMANENT_PIPELINE_ERRORS)


def _pipeline_retry_delay(n):
    """Seconds to wait before automatic try n+1 (n = retries already used, 0-based)."""
    if not PIPELINE_RETRY_DELAYS:
        return 600.0
    return float(PIPELINE_RETRY_DELAYS[min(n, len(PIPELINE_RETRY_DELAYS) - 1)])


def _pending_job_note(first, **kw):
    """Add fields to an existing pendingJobs entry (no-op when there isn't one). Never raises."""
    if not first:
        return
    try:
        with PIPELINE_LOCK:
            ent = (PIPELINE.get("pendingJobs") or {}).get(first)
            if not isinstance(ent, dict):
                return
            ent.update(kw)
        _pipeline_save()
    except Exception as e:
        log(f"pipeline pending-note failed: {e}")


def _pending_job_field(first, key, default=None):
    with PIPELINE_LOCK:
        ent = (PIPELINE.get("pendingJobs") or {}).get(first)
        return ent.get(key, default) if isinstance(ent, dict) else default


def _fmt_clock(ts):
    try:
        return time.strftime("%I:%M %p", time.localtime(ts)).lstrip("0")
    except Exception:
        return ""


def pipeline_worker(job_id):
    segs = list(_job_get(job_id, "segments") or [])
    first = segs[0] if segs else None
    retries = int(_pending_job_field(first, "errorRetries", 0) or 0)  # survives a helper restart
    made = _pending_job_field(first, "musicMade")
    if made and not _job_get(job_id, "musicMade"):
        _set_music_job(job_id, musicMade=made)
    _set_music_job(job_id, autoRetries=retries)
    keep_pending = False
    try:
        while True:
            try:
                _pipeline_attempt(job_id)
                return
            except _JobCancelled:
                _finalize_cancelled_music_job(job_id)
                log(f"pipeline cancelled job={job_id}")
                return
            except Exception as e:
                msg = str(e) or type(e).__name__
                log(f"pipeline error job={job_id}: {msg}")
                permanent = _pipeline_error_is_permanent(msg)
                if permanent or retries >= PIPELINE_RETRIES:
                    with PIPELINE_LOCK:
                        PIPELINE_RETRY_STATS["permanent" if permanent else "gaveUp"] += 1
                        PIPELINE_RETRY_STATS.update(lastError=msg[:300], lastAt=time.time(), lastName=first)
                    if retries and not permanent:
                        msg = f"{msg} (failed {retries + 1} times — press Retry to try again)"
                    _set_music_job(job_id, status="error", progress="Error", error=msg,
                                   finishedAt=time.time(), retryAt=None)
                    return
                delay = _pipeline_retry_delay(retries)
                retries += 1
                at = time.time() + delay
                with PIPELINE_LOCK:
                    PIPELINE_RETRY_STATS["scheduled"] += 1
                    PIPELINE_RETRY_STATS.update(lastError=msg[:300], lastAt=time.time(), lastName=first)
                _pending_job_note(first, errorRetries=retries, lastError=msg[:300], retryAt=at)
                short = msg if len(msg) <= 160 else msg[:157] + "…"
                wait_msg = (f"Voice removal failed ({short}) — trying again by itself at {_fmt_clock(at)} "
                            f"(try {retries + 1} of {PIPELINE_RETRIES + 1})")
                _set_music_job(job_id, status="queued", phase="retry-wait", progress=wait_msg,
                               lastError=msg, retryAt=at, autoRetries=retries, error=None)
                log(f"pipeline job={job_id}: retry {retries}/{PIPELINE_RETRIES} in {delay:.0f}s")
                keep_pending = True  # a restart during the wait resumes the job
                try:
                    while time.time() < at:
                        _check_cancel(job_id)
                        time.sleep(0.5)
                except _JobCancelled:
                    keep_pending = False
                    _finalize_cancelled_music_job(job_id)
                    log(f"pipeline cancelled job={job_id} (while waiting to retry)")
                    return
                keep_pending = False
                _set_music_job(job_id, status="queued", phase="wait", retryAt=None,
                               progress=f"Trying voice removal again (try {retries + 1} of {PIPELINE_RETRIES + 1})…")
    finally:
        # v6.44: job reached an end state in this process — no resume needed after a restart
        if not keep_pending:
            try:
                _pending_job_drop(first)
            except Exception:
                pass


def _pipeline_retry_snapshot():
    with PIPELINE_LOCK:
        out = dict(PIPELINE_RETRY_STATS)
    out.update(maxRetries=PIPELINE_RETRIES, delays=list(PIPELINE_RETRY_DELAYS),
               chunkRetries=DEMUCS_CHUNK_RETRIES, chunkRetriesUsed=_CHUNK_RETRY_STATS["retries"],
               chunksSaved=_CHUNK_RETRY_STATS["saved"])
    with MUSIC_JOBS_LOCK:
        out["waiting"] = [{"name": j.get("name"), "retryAt": j.get("retryAt"), "tries": j.get("autoRetries"),
                           "lastError": (j.get("lastError") or "")[:200]}
                          for j in MUSIC_JOBS.values()
                          if j.get("phase") == "retry-wait" and (j.get("status") or "") == "queued"]
    return out


def split_worker(job_id):
    progress = _pipeline_progress(job_id)
    try:
        _set_music_job(job_id, status="running")
        src = REC_DIR / _job_get(job_id, "musicName")
        base, names, part_secs, st, msg = _split_and_deliver(job_id, src, "manual", progress, pct=10)
        _set_music_job(job_id, status="done", phase="done", progress=msg, progressPct=100,
                       deliveryStatus=st, error=None, finishedAt=time.time())
    except _JobCancelled:
        _finalize_cancelled_music_job(job_id)
    except Exception as e:
        _set_music_job(job_id, status="error", progress="Error", error=str(e) or type(e).__name__,
                       finishedAt=time.time())


def _new_job(name, display, source, target, **extra):
    job_id = uuid.uuid4().hex[:12]
    job = {
        "jobId": job_id, "status": "queued", "progress": "Queued…", "progressPct": 0,
        "error": None, "name": name, "displayName": display, "source": source,
        "outputName": None, "outputUrl": None, "vocalsName": None, "vocalsUrl": None,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    job.update(extra)
    with MUSIC_JOBS_LOCK:
        MUSIC_JOBS[job_id] = job
    threading.Thread(target=target, args=(job_id,), daemon=True, name=f"{source}-{job_id}").start()
    return job_id


# ─── v6.44: RESUME AFTER-STREAM JOBS INTERRUPTED BY A HELPER RESTART ─────
# Voice removal runs for hours after a stream. If the helper quits mid-job (crash, reboot, power
# blip, macOS killing it, manual restart) the job used to vanish from memory and that stream was
# never voice-removed or sent to Drive. Queued/running stream jobs are now persisted in
# .drive-pipeline.json and re-queued on startup (max PIPELINE_RESUME_MAX tries, newer than
# PIPELINE_RESUME_MAX_AGE_SECS). Finished, cancelled or failed jobs are dropped from the list.
PIPELINE_RESUME_MAX = int(os.environ.get("TWITCH_RECORDER_PIPELINE_RESUME_MAX") or 3)
PIPELINE_RESUME_MAX_AGE_SECS = int(os.environ.get("TWITCH_RECORDER_PIPELINE_RESUME_MAX_AGE_SECS") or 3 * 86400)
PIPELINE_RESUME_DELAY_SECS = float(os.environ.get("TWITCH_RECORDER_PIPELINE_RESUME_DELAY") or 20)


def _pending_job_put(first, username, segs, redo, reason):
    with PIPELINE_LOCK:
        pj = PIPELINE.setdefault("pendingJobs", {})
        prev = pj.get(first) if isinstance(pj.get(first), dict) else {}
        pj[first] = {"segments": list(segs), "username": username, "redo": bool(redo),
                     "reason": reason, "queuedAt": prev.get("queuedAt") or time.time(),
                     "attempts": int(prev.get("attempts") or 0) + 1}
        for k in ("errorRetries", "lastError", "musicMade"):  # v6.61: keep the auto-retry state
            if prev.get(k) is not None:
                pj[first][k] = prev.get(k)
    _pipeline_save()


def _pending_job_drop(first):
    if not first:
        return
    with PIPELINE_LOCK:
        pj = PIPELINE.get("pendingJobs") or {}
        if first not in pj:
            return
        pj.pop(first, None)
    _pipeline_save()


def resume_pending_pipeline_jobs(now=None):
    """Startup: re-queue stream jobs interrupted by the last helper stop. Returns resumed names."""
    now = now or time.time()
    with PIPELINE_LOCK:
        items = list((PIPELINE.get("pendingJobs") or {}).items())
    resumed, dropped = [], []
    for first, ent in items:
        segs = [basename_of(x) for x in (ent.get("segments") or []) if isinstance(x, str)]
        on_disk = [x for x in segs if x and (REC_DIR / x).is_file()]
        age = now - float(ent.get("queuedAt") or 0)
        if (not on_disk or int(ent.get("attempts") or 0) >= PIPELINE_RESUME_MAX
                or age > PIPELINE_RESUME_MAX_AGE_SECS):
            dropped.append(first)
            continue
        user = ent.get("username") or ""
        jid, err = start_stream_pipeline(user, on_disk, redo=bool(ent.get("redo")),
                                         reason="resumed after helper restart")
        if jid:
            resumed.append(first)
        else:
            log(f"pipeline resume skipped {first}: {err}")
            if err == "no segments":
                dropped.append(first)
    for d in dropped:
        log(f"pipeline resume: dropping stale pending job {d}")
        _pending_job_drop(d)
    if resumed:
        log(f"pipeline resume: re-queued {len(resumed)} interrupted job(s): {resumed}")
    return resumed


def start_stream_pipeline(username, segments, redo=False, reason="stream-end"):
    segs = [basename_of(s) for s in (segments or []) if s]
    segs = [s for s in segs if s and not is_music_export_name(s) and not is_seamless_export_name(s)]
    if not segs:
        return None, "no segments"
    first = segs[0]
    with MUSIC_JOBS_LOCK:
        for jid, job in MUSIC_JOBS.items():
            if (job.get("status") or "") in ("queued", "running") and (
                job.get("name") == first or first in (job.get("segments") or [])
            ):
                return jid, "already running"
    if not which_ffmpeg():
        return None, "ffmpeg not installed"
    eff_q, q_note = resolve_voice_quality(None)  # v6.45: Fast needs no demucs
    if not eff_q:
        return None, q_note or "voice removal unavailable"
    PIPELINE_SEEN.add(first)
    # name = first segment so History / Auto Music see it as that recording's Music-only job
    jid = _new_job(first, first, "pipeline", pipeline_worker, segments=segs, username=username,
                   redo=bool(redo), reason=reason, quality=eff_q, qualityNote=q_note)
    try:
        _pending_job_put(first, username, segs, redo, reason)
    except Exception as e:
        log(f"pipeline pending-save failed: {e}")
    log(f"pipeline queued job={jid} user={username} segments={segs} quality={eff_q} ({reason})")
    return jid, None


def _on_session_finalized(username, segments, reason="stream-end"):
    """Called from remember_session_segments (maybe holding LOCK) — never blocks."""
    segs = [s for s in (segments or []) if s]
    if not segs or not (pipeline_enabled_for(username) or original_enabled_for(username)):
        return
    if segs[0] in PIPELINE_SEEN:
        return
    PIPELINE_SEEN.add(segs[0])

    def run():
        time.sleep(PIPELINE_START_DELAY_SECS)  # let streamlink finish flushing
        # v6.38: 1) original(s) → Drive straight away (local copy is never touched)
        if original_enabled_for(username):
            queued = [n for n in segs if register_original(n, username, "stream")]
            if queued:
                log(f"originals queued for Drive user={username}: {queued}")
                request_delivery(None)
        # 2) then remove voice → split → parts to Drive (the job waits for the originals)
        if not pipeline_enabled_for(username):
            return
        jid, err = start_stream_pipeline(username, segs, reason=reason)
        if err and not jid:
            log(f"pipeline not started for {username}: {err}")

    threading.Thread(target=run, daemon=True, name=f"pipeline-trigger-{username}").start()


# ─── v6.53: RECOVER RECORDINGS CUT OFF BY A HELPER CRASH / REBOOT / POWER LOSS ─────
# The after-stream steps (original → Drive, voice removal, ~35-min parts → Drive) only started when
# a recording session ended *inside the same helper process* (v6.44 only resumes jobs that had
# already been queued). If the helper was killed, crashed, or the computer rebooted / lost power /
# was force-restarted for Windows updates mid-stream, the segments recorded so far were never
# processed or sent — and when Auto-Rec / solo-watch picked the still-live stream back up, the new
# session's parts only covered the part after the restart. Now every established segment is noted
# in .drive-pipeline.json (liveSessions); a normal end clears it. On the next start:
#   • if the same streamer is recorded again within ~1 h of the cut-off segment's last write, the
#     old segments are put in front of the new session, so the parts cover the whole broadcast;
#   • otherwise, ~5 min after start (time for the page / solo-watch to reconnect), the cut-off
#     segments are finalized on their own — original + voice removal + parts to Drive as usual.
# Files still being written (a surviving streamlink) are waited on, never merged mid-write.
# Nothing is deleted. Entries older than 3 days, or whose files are gone / already processed, drop.
LIVE_RECOVER_DELAY_SECS = _env_secs("TWITCH_RECORDER_RECOVER_DELAY_SECS", 300, 0)
LIVE_RECOVER_MERGE_GAP_SECS = _env_secs("TWITCH_RECORDER_RECOVER_MERGE_GAP_SECS", 3600, 0)
LIVE_RECOVER_QUIET_SECS = _env_secs("TWITCH_RECORDER_RECOVER_QUIET_SECS", 120, 0)
LIVE_RECOVER_MAX_AGE_SECS = _env_secs("TWITCH_RECORDER_RECOVER_MAX_AGE_SECS", 3 * 86400, 60)
LIVE_RECOVER_TICK_SECS = _env_secs("TWITCH_RECORDER_RECOVER_TICK_SECS", 30, 1)
LIVE_RECOVER_LOG = []  # last few recover actions (shown in /api/health.recoveredSessions)
ORPHAN_SESSIONS = {}  # username -> entry from the previous process (claimed / finalized here)
ORPHAN_LOCK = threading.Lock()
MY_RUN_ID = f"{os.getpid()}-{int(time.time())}"


def _live_note(action, username, segs, detail=""):
    LIVE_RECOVER_LOG.append({"at": time.time(), "action": action, "username": username,
                             "segments": list(segs)[:20], "detail": detail})
    del LIVE_RECOVER_LOG[:-10]
    log(f"recover: {action} {username} {list(segs)}" + (f" — {detail}" if detail else ""))
    STATUS_FILE_WAKE.set()


def live_session_touch(username, gen):
    """Persist this session's segments (finished + current) so a crash can't orphan them."""
    try:
        with LOCK:
            rec = ACTIVE.get(username)
            if not rec or rec.get("gen") != gen:
                return
            names = [n for n in (rec.get("sessionSegments") or []) if n]
            cur = basename_of(rec.get("file") or "")
            if cur and cur not in names:
                names.append(cur)
            started = rec.get("sessionStartedAt") or ""
        names = [n for n in names if not is_music_export_name(n) and not is_seamless_export_name(n)]
        if not names:
            return
        with PIPELINE_LOCK:
            ls = PIPELINE.setdefault("liveSessions", {})
            ls[username] = {"segments": names, "run": MY_RUN_ID, "gen": gen,
                            "sessionStartedAt": started, "updatedAt": time.time()}
        _pipeline_save()
    except Exception as e:
        log(f"live-session note failed for {username}: {e}")


def live_session_forget(username, segs):
    """Session finalized normally. May run while LOCK is held → save on a side thread."""
    def run():
        with PIPELINE_LOCK:
            ls = PIPELINE.get("liveSessions") or {}
            ent = ls.get(username)
            if not ent or ent.get("run") != MY_RUN_ID:
                return
            if segs and not (set(ent.get("segments") or []) & set(segs)):
                return  # a newer session for this streamer already took the slot
            ls.pop(username, None)
        _pipeline_save()
    threading.Thread(target=run, daemon=True, name=f"live-forget-{username}").start()


def load_orphan_sessions():
    """Startup (after _pipeline_load): sessions a previous helper process never finished."""
    with PIPELINE_LOCK:
        ls = PIPELINE.get("liveSessions") if isinstance(PIPELINE.get("liveSessions"), dict) else {}
        found = {str(u).lower(): dict(e) for u, e in ls.items()
                 if isinstance(e, dict) and e.get("run") != MY_RUN_ID and safe_username(str(u).lower())
                 and isinstance(e.get("segments"), list)}
    with ORPHAN_LOCK:
        ORPHAN_SESSIONS.clear()
        ORPHAN_SESSIONS.update(found)
    if found:
        log(f"recover: {len(found)} recording session(s) were cut off when the helper last stopped — "
            + ", ".join(f"{u} ({len(e['segments'])} segment(s))" for u, e in found.items()))
    return list(found)


def _orphan_drop_persisted(username, ent):
    with PIPELINE_LOCK:
        ls = PIPELINE.get("liveSessions") or {}
        cur = ls.get(username)
        if cur and cur.get("run") == ent.get("run"):
            ls.pop(username, None)
    _pipeline_save()


def _orphan_files(ent):
    """[(name, size, mtime)] of the orphan's segments still on disk (crumbs skipped)."""
    out = []
    for n in ent.get("segments") or []:
        n = basename_of(n)
        if not n or is_music_export_name(n) or is_seamless_export_name(n):
            continue
        try:
            st = (REC_DIR / n).stat()
        except OSError:
            continue
        if st.st_size > SCRUB_MAX_BYTES:
            out.append((n, int(st.st_size), float(st.st_mtime)))
    return out


def _orphan_already_handled(username, names):
    if not names:
        return "no segments on disk"
    first = names[0]
    if first in PIPELINE_SEEN:
        return "already queued"
    with PIPELINE_LOCK:
        if first in (PIPELINE.get("pendingJobs") or {}):
            return "already queued"
        if music_base_stem(first) in (PIPELINE.get("deliveries") or {}):
            return "already processed"
    with MUSIC_JOBS_LOCK:
        for job in MUSIC_JOBS.values():
            if job.get("name") == first or first in (job.get("segments") or []):
                return "already queued"
    return None


def claim_orphan_segments(username, now=None):
    """A new session for `username` starts: hand back the cut-off segments of the same broadcast
    (quiet for LIVE_RECOVER_QUIET_SECS, last write within LIVE_RECOVER_MERGE_GAP_SECS)."""
    now = time.time() if now is None else now
    u = (username or "").lower()
    with ORPHAN_LOCK:
        ent = ORPHAN_SESSIONS.get(u)
    if not ent:
        return []
    files = _orphan_files(ent)
    names = [f[0] for f in files]
    if _orphan_already_handled(u, names):
        return []
    last = max(f[2] for f in files)
    if now - last < LIVE_RECOVER_QUIET_SECS:
        return []  # still being written (a surviving streamlink) — recovery loop handles it later
    if now - last > LIVE_RECOVER_MERGE_GAP_SECS:
        return []  # an older broadcast — finalized on its own by the recovery loop
    with ORPHAN_LOCK:
        if ORPHAN_SESSIONS.get(u) is not ent:
            return []
        ORPHAN_SESSIONS.pop(u, None)
    _orphan_drop_persisted(u, ent)
    _live_note("joined to new recording", u, names,
               f"cut off {int((now - last) / 60)} min ago; parts will cover the whole stream")
    return names


def recover_orphan_sessions(now=None, uptime=None, finalize=None):
    """Recovery tick: finalize cut-off sessions nobody picked back up. Returns [(user, action)]."""
    now = time.time() if now is None else now
    uptime = (now - HELPER_STARTED_AT) if uptime is None else uptime
    finalize = finalize or (lambda u, segs: remember_session_segments(
        u, segs, reason="recovered after helper/computer restart"))
    done = []
    with ORPHAN_LOCK:
        items = list(ORPHAN_SESSIONS.items())
    for u, ent in items:
        files = _orphan_files(ent)
        names = [f[0] for f in files]
        why = _orphan_already_handled(u, names)
        last = max((f[2] for f in files), default=0.0)
        if not why and now - last > LIVE_RECOVER_MAX_AGE_SECS:
            why = "older than 3 days"
        if why:
            with ORPHAN_LOCK:
                ORPHAN_SESSIONS.pop(u, None)
            _orphan_drop_persisted(u, ent)
            log(f"recover: dropping cut-off session {u}: {why}")
            done.append((u, "dropped"))
            continue
        if now - last < LIVE_RECOVER_QUIET_SECS:
            continue  # still growing — wait
        if uptime < LIVE_RECOVER_DELAY_SECS:
            with LOCK:
                recording_now = u in ACTIVE
            if not recording_now:
                continue  # give the page / solo-watch time to pick a still-live stream back up
        with LOCK:
            rec = ACTIVE.get(u)
            active_segs = set(rec.get("sessionSegments") or []) if rec else set()
        if active_segs & set(names):
            with ORPHAN_LOCK:
                ORPHAN_SESSIONS.pop(u, None)
            _orphan_drop_persisted(u, ent)
            done.append((u, "joined"))
            continue
        with ORPHAN_LOCK:
            if ORPHAN_SESSIONS.get(u) is not ent:
                continue
            ORPHAN_SESSIONS.pop(u, None)
        _orphan_drop_persisted(u, ent)
        _live_note("finalized cut-off recording", u, names,
                   "original + voice removal + parts to Drive (as if the stream had ended normally)")
        try:
            finalize(u, names)
        except Exception as e:
            log(f"recover: finalize failed for {u}: {e}")
        done.append((u, "finalized"))
    return done


def orphan_recovery_loop():
    while True:
        with ORPHAN_LOCK:
            pending = bool(ORPHAN_SESSIONS)
        if not pending:
            return
        time.sleep(LIVE_RECOVER_TICK_SECS)
        try:
            recover_orphan_sessions()
        except Exception as e:
            log(f"recover tick failed: {e}")


def recovery_snapshot():
    with ORPHAN_LOCK:
        pend = {u: len(e.get("segments") or []) for u, e in ORPHAN_SESSIONS.items()}
    return {"pending": pend, "recent": [dict(x) for x in LIVE_RECOVER_LOG[-5:]]}


def _self_test_orphan_recovery():
    """v6.53: cut-off sessions are joined to a new recording or finalized on their own."""
    import tempfile as _tmp_or
    global REC_DIR, PIPELINE_FILE
    prev_rec, prev_pf = REC_DIR, PIPELINE_FILE
    with PIPELINE_LOCK:
        prev_ls = dict(PIPELINE.get("liveSessions") or {})
    with ORPHAN_LOCK:
        prev_or = dict(ORPHAN_SESSIONS)
    prev_log = list(LIVE_RECOVER_LOG)
    try:
        with _tmp_or.TemporaryDirectory(prefix="tar-recover-") as td:
            REC_DIR = Path(td)
            PIPELINE_FILE = REC_DIR / ".drive-pipeline.json"
            now = time.time()
            a = "whistleface-Fri-2026-10-16T19-00-00.ogg"
            b = "whistleface-Fri-2026-10-16T20-10-00.ogg"
            for n in (a, b):
                (REC_DIR / n).write_bytes(b"x" * (SCRUB_MAX_BYTES + 10))
                os.utime(REC_DIR / n, (now - 600, now - 600))
            with PIPELINE_LOCK:
                PIPELINE["liveSessions"] = {
                    "whistleface": {"segments": [a, b], "run": "old-run", "gen": 1, "updatedAt": now - 900},
                    "ghostuser": {"segments": ["ghostuser-Fri-2026-10-16T19-00-00.ogg"], "run": "old-run"},
                    "mine": {"segments": [a], "run": MY_RUN_ID},  # this process — not an orphan
                }
            assert sorted(load_orphan_sessions()) == ["ghostuser", "whistleface"]
            # still being written → no merge
            os.utime(REC_DIR / b, (now - 30, now - 30))
            assert claim_orphan_segments("whistleface", now=now) == []
            os.utime(REC_DIR / b, (now - 600, now - 600))
            # too long ago for the same broadcast → no merge
            assert claim_orphan_segments("whistleface", now=now + 2 * 3600) == []
            # same broadcast → joined in order, persisted entry cleared
            assert claim_orphan_segments("whistleface", now=now) == [a, b]
            with PIPELINE_LOCK:
                assert "whistleface" not in PIPELINE["liveSessions"]
            assert claim_orphan_segments("whistleface", now=now) == []
            # finalize path: before the delay nothing happens; after it the session is finalized
            with ORPHAN_LOCK:
                ORPHAN_SESSIONS["whistleface"] = {"segments": [a, b], "run": "old-run"}
            got = []
            r = recover_orphan_sessions(now=now, uptime=10, finalize=lambda u, s: got.append((u, s)))
            assert ("ghostuser", "dropped") in r and not got, (r, got)  # files gone → dropped
            r = recover_orphan_sessions(now=now, uptime=LIVE_RECOVER_DELAY_SECS + 1,
                                        finalize=lambda u, s: got.append((u, s)))
            assert got == [("whistleface", [a, b])] and r == [("whistleface", "finalized")], (r, got)
            assert recovery_snapshot()["pending"] == {}
            # already processed (delivery exists) → dropped, not re-sent
            with ORPHAN_LOCK:
                ORPHAN_SESSIONS["whistleface"] = {"segments": [a], "run": "old-run"}
            with PIPELINE_LOCK:
                prev_del = PIPELINE["deliveries"].get(music_base_stem(a))
                PIPELINE["deliveries"][music_base_stem(a)] = {"status": "sent"}
            try:
                got.clear()
                r = recover_orphan_sessions(now=now, uptime=9999, finalize=lambda u, s: got.append((u, s)))
                assert r == [("whistleface", "dropped")] and not got, (r, got)
            finally:
                with PIPELINE_LOCK:
                    if prev_del is None:
                        PIPELINE["deliveries"].pop(music_base_stem(a), None)
                    else:
                        PIPELINE["deliveries"][music_base_stem(a)] = prev_del
        # restart-grace clock label is a wall-clock time again (v6.52 had shadowed it)
        lbl = _clock_label(time.mktime((2026, 10, 16, 21, 14, 0, 0, 0, -1)))
        assert lbl == "9:14 PM", lbl
        assert "until 9:14 PM" in grace_wait_msg("x", time.mktime((2026, 10, 16, 21, 14, 0, 0, 0, -1)), None)
    finally:
        REC_DIR, PIPELINE_FILE = prev_rec, prev_pf
        with PIPELINE_LOCK:
            PIPELINE["liveSessions"] = prev_ls
        with ORPHAN_LOCK:
            ORPHAN_SESSIONS.clear()
            ORPHAN_SESSIONS.update(prev_or)
        LIVE_RECOVER_LOG[:] = prev_log


def _self_test_claimed_unstarted():
    """v6.54: claimed cut-off segments are finalized when the resumed recording never starts,
    and are re-noted (liveSessions, this run) as soon as they are claimed."""
    import tempfile as _tmp_cu
    global REC_DIR, PIPELINE_FILE, RETRY_BACKOFFS, spawn_streamlink, remember_session_segments
    prev = (REC_DIR, PIPELINE_FILE, RETRY_BACKOFFS, spawn_streamlink, remember_session_segments)
    with PIPELINE_LOCK:
        prev_ls = dict(PIPELINE.get("liveSessions") or {})
    with ORPHAN_LOCK:
        prev_or = dict(ORPHAN_SESSIONS)
    prev_log = list(LIVE_RECOVER_LOG)
    u = "selftestclaim"
    got = []
    try:
        with _tmp_cu.TemporaryDirectory(prefix="tar-claim-") as td:
            REC_DIR = Path(td)
            PIPELINE_FILE = REC_DIR / ".drive-pipeline.json"
            now = time.time()
            a = f"{u}-Fri-2026-10-23T19-00-00.ogg"
            (REC_DIR / a).write_bytes(b"x" * (SCRUB_MAX_BYTES + 10))
            os.utime(REC_DIR / a, (now - 600, now - 600))
            with ORPHAN_LOCK:
                ORPHAN_SESSIONS.clear()
                ORPHAN_SESSIONS[u] = {"segments": [a], "run": "old-run"}
            with PIPELINE_LOCK:
                PIPELINE["liveSessions"] = {u: {"segments": [a], "run": "old-run"}}

            def _no_streamlink(cmd):
                raise OSError("self-test: streamlink unavailable")
            spawn_streamlink = _no_streamlink
            RETRY_BACKOFFS = ()  # give up on the first failed start
            remember_session_segments = lambda user, segs, reason="stream-end": got.append((user, list(segs), reason))
            with LOCK:
                ACTIVE.pop(u, None)
                gen = GEN.get(u, 0) + 1
                GEN[u] = gen
                ACTIVE[u] = {"proc": None, "file": "", "want": True, "gen": gen, "starting": True,
                             "retrying": False, "supervised": False, "sessionSegments": [],
                             "sessionStartedAt": ""}
            claimed = claim_orphan_segments(u)
            assert claimed == [a], claimed
            with LOCK:
                ACTIVE[u]["sessionSegments"] = list(claimed)
            live_session_touch(u, gen)
            with PIPELINE_LOCK:
                ent = dict((PIPELINE.get("liveSessions") or {}).get(u) or {})
            assert ent.get("run") == MY_RUN_ID and ent.get("segments") == [a], ent  # re-noted
            record_supervisor(u, "audio_only", gen)  # streamlink fails → slot dropped
            with LOCK:
                assert u not in ACTIVE
            assert got == [(u, [a], RECOVER_UNSTARTED_REASON)], got
            # no claimed segments → plain drop, nothing finalized
            got.clear()
            with LOCK:
                gen = GEN.get(u, 0) + 1
                GEN[u] = gen
                ACTIVE[u] = {"proc": None, "file": "", "want": True, "gen": gen, "starting": True,
                             "retrying": False, "supervised": False, "sessionSegments": []}
            record_supervisor(u, "audio_only", gen)
            with LOCK:
                assert u not in ACTIVE
            assert got == [], got
            # a slot of another generation is never touched
            with LOCK:
                ACTIVE[u] = {"gen": 99, "sessionSegments": [a]}
                assert _drop_slot_finalize_claimed_locked(u, 98) == [] and u in ACTIVE
                ACTIVE.pop(u, None)
    finally:
        REC_DIR, PIPELINE_FILE, RETRY_BACKOFFS, spawn_streamlink, remember_session_segments = prev
        with LOCK:
            ACTIVE.pop(u, None)
        LAST_ERROR.pop(u, None) if isinstance(globals().get("LAST_ERROR"), dict) else None
        with PIPELINE_LOCK:
            PIPELINE["liveSessions"] = prev_ls
        with ORPHAN_LOCK:
            ORPHAN_SESSIONS.clear()
            ORPHAN_SESSIONS.update(prev_or)
        LIVE_RECOVER_LOG[:] = prev_log


def api_pipeline_split(body):
    body = body if isinstance(body, dict) else {}
    target = resolve_download_path(body.get("name"))
    if not target:
        return {"ok": False, "error": "not found in ~/TwitchRecordings"}, 404
    if not is_music_export_name(target.name) or "-vocals" in target.stem:
        return {"ok": False, "error": "pick a -music file"}, 400
    if not which_ffmpeg():
        return {"ok": False, "error": "ffmpeg not found"}, 400
    key = "split:" + target.name
    jid = _inflight_job_for(key)
    if jid:
        return {"ok": True, "jobId": jid, "alreadyRunning": True}, 200
    jid = _new_job(key, target.name, "split", split_worker, musicName=target.name)
    return {"ok": True, "jobId": jid}, 200


def api_pipeline_deliver(body):
    body = body if isinstance(body, dict) else {}
    base = body.get("base")
    if base:
        with PIPELINE_LOCK:
            if base not in PIPELINE["deliveries"]:
                return {"ok": False, "error": "no parts for that recording — use Split for Drive"}, 404
    found = bool(detect_drive_roots())
    request_delivery(base or None)
    return {"ok": True, "driveFound": found,
            "message": "sending…" if found else "Waiting for Drive — Drive for Desktop not found"}, 200


# ─────────────────────────────────────────────────────────────────────────────
# v6.38: Send the ORIGINAL recording to Drive too (before Demucs)
#   * The local original in ~/TwitchRecordings is never deleted or moved.
#   * Copy goes to <Drive target>/Originals/<name> (atomic temp → rename), as-is,
#     any size. Same target, Waiting-for-Drive / retry and permission messaging
#     as the parts. Queued the moment a recording finalizes; the voice-removal job
#     waits (up to ORIGINAL_WAIT_SECS) until the originals are sent / waiting.
#   * No-clobber across computers (Mac + Windows backup): a file already in Drive
#     with the same name AND size counts as sent (skipped); same name but a
#     different size is written as <stem>-<ComputerName><ext> instead of replacing.
# ─────────────────────────────────────────────────────────────────────────────
ORIGINALS_SUBDIR = "Originals"
ORIGINAL_WAIT_SECS = float(os.environ.get("TWITCH_RECORDER_ORIGINAL_WAIT_SECS") or 1800)


def computer_name():
    raw = (os.environ.get("TWITCH_RECORDER_COMPUTER_NAME") or os.environ.get("COMPUTERNAME")
           or socket.gethostname() or "computer")
    raw = raw.split(".")[0]
    clean = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in raw).strip("-_")
    return (clean or "computer")[:40]


def _host_variant(name):
    stem, ext = os.path.splitext(name)
    return f"{stem}-{computer_name()}{ext}"


def place_in_drive(src, dest_dir, name, allow_replace=False):
    """Copy src into dest_dir/name without clobbering another computer's file.

    Returns (dest_path, action) with action in copied | skipped | renamed | replaced.
    """
    src = Path(src)
    dest_dir = Path(dest_dir)
    size = src.stat().st_size
    for i, nm in enumerate((name, _host_variant(name))):
        dest = dest_dir / nm
        try:
            exists = dest.is_file()
            same = exists and dest.stat().st_size == size
        except OSError:
            exists, same = False, False
        if same:
            return dest, "skipped"  # identical name + size already in Drive (other computer / earlier run)
        if not exists:
            atomic_copy(src, dest)
            return dest, ("copied" if i == 0 else "renamed")
        if allow_replace and i == 0:
            atomic_copy(src, dest)
            return dest, "replaced"
    # both names taken by different-size files: replace our own host-named copy
    dest = dest_dir / _host_variant(name)
    atomic_copy(src, dest)
    return dest, "replaced"


# ─── v6.50: ONE COPY PER STREAM ACROSS COMPUTERS ─────────────────────────────
# The same stream is recorded on several computers as a backup (Mac, PC, Twitch rig). Each used to
# remove the voice and send its own full set of parts, so Drive got two or three sets (the later ones
# as …-<Computer>.ogg). Now a computer that sends a stream's parts also drops a small manifest in
# <Drive target>/Recorder status/Sent parts/<base>__<computer>.json ("sending" while copying, "sent"
# with the exact Drive names + sizes when done). Before voice removal, and again right before sending,
# a computer reads those manifests: when another computer already has a complete set in Drive for the
# same streamer and overlapping time that is at least as long as ours (within max(3 min, 2 %)), this
# computer skips the duplicate (status "covered"). A shorter set — that computer missed part of the
# stream — never blocks a longer one, a missing/renamed part file means "not covered", and a fresh
# "sending" claim just makes us wait (it goes stale after 30 min). Manual Split for Drive always
# sends. Originals → Drive are unchanged. Settings toggle oneCopy (default on); env
# TWITCH_RECORDER_ONE_COPY=0 hard-off.
SENT_MANIFEST_SUBDIR = "Sent parts"
ONE_COPY_CLAIM_STALE_SECS = float(os.environ.get("TWITCH_RECORDER_ONE_COPY_CLAIM_SECS") or 1800)
ONE_COPY_TOL_SECS = float(os.environ.get("TWITCH_RECORDER_ONE_COPY_TOL_SECS") or 180)
ONE_COPY_ENV_OFF = (os.environ.get("TWITCH_RECORDER_ONE_COPY") or "").strip() == "0"
_REC_STAMP_RE = _re_drive.compile(
    r"^(?P<user>[a-z0-9_]+)-(?:[A-Za-z]{3}-)?(?P<d>\d{4}-\d{2}-\d{2})T(?P<t>\d{2}-\d{2}-\d{2})")


def one_copy_enabled():
    if ONE_COPY_ENV_OFF:
        return False
    with PIPELINE_LOCK:
        return PIPELINE.get("oneCopy", True) is not False


def parse_recording_stamp(name):
    """(username, start epoch) from username-Wkd-YYYY-MM-DDTHH-MM-SS…; (None, None) if not ours."""
    m = _REC_STAMP_RE.match(os.path.basename(str(name or "")))
    if not m:
        return None, None
    try:
        st = time.strptime(m.group("d") + " " + m.group("t"), "%Y-%m-%d %H-%M-%S")
        return m.group("user"), float(time.mktime(st))
    except (ValueError, OverflowError):
        return m.group("user"), None


def _sent_manifest_dir(target):
    return Path(target) / STATUS_SUBDIR / SENT_MANIFEST_SUBDIR


def write_sent_manifest(target, base, state, parts=None, duration=None, user=None, start=None):
    """Write <target>/Recorder status/Sent parts/<base>__<computer>.json. Soft: never raises."""
    try:
        if user is None or start is None:
            u2, s2 = parse_recording_stamp(base)
            user = user or u2
            start = start if start is not None else s2
        doc = {"kind": "twitch-recorder-sent-parts", "v": 1, "helperVersion": HELPER_VERSION,
               "computer": computer_name(), "base": base, "username": user, "startEpoch": start,
               "startedAt": _iso_local(start) if start else None,
               "durationSecs": round(float(duration or 0), 1), "state": state,
               "parts": list(parts or []), "updatedAt": time.time()}
        d = _sent_manifest_dir(target)
        d.mkdir(parents=True, exist_ok=True)
        _status_atomic_write(d / f"{base}__{computer_name()}.json",
                             json.dumps(doc, indent=1).encode("utf-8"))
        return True
    except Exception as e:
        log(f"one-copy: manifest write failed base={base}: {e}")
        return False


def _read_sent_manifests(target):
    out = []
    d = _sent_manifest_dir(target)
    try:
        if not d.is_dir():
            return out
        for p in d.glob("*.json"):
            if p.name.startswith("."):
                continue
            try:
                doc = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("kind") == "twitch-recorder-sent-parts":
                out.append(doc)
    except OSError as e:
        log(f"one-copy: cannot read {d}: {e}")
    return out


def _manifest_parts_present(target, doc):
    """Every part the other computer listed is still in the target with the same size."""
    parts = doc.get("parts") or []
    if not parts:
        return False
    for p in parts:
        if not isinstance(p, dict):
            return False
        nm = os.path.basename(str(p.get("name") or ""))
        if not nm or nm in (".", ".."):
            return False
        try:
            if (Path(target) / nm).stat().st_size != int(p.get("size")):
                return False
        except (OSError, TypeError, ValueError):
            return False
    return True


def find_other_copy(target, user, start, duration, now=None):
    """('sent', doc) when another computer's complete set in Drive covers this stream,
    ('sending', doc) when another computer is sending one right now, else (None, None)."""
    try:
        duration = float(duration or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if not target or not user or not start or duration <= 0:
        return None, None
    now = time.time() if now is None else now
    me = computer_name().lower()
    tol = max(ONE_COPY_TOL_SECS, 0.02 * duration)
    sending = (None, None)
    for doc in _read_sent_manifests(target):
        if str(doc.get("computer") or "").lower() == me:
            continue
        if str(doc.get("username") or "").lower() != str(user).lower():
            continue
        try:
            o_start = float(doc.get("startEpoch"))
            o_dur = float(doc.get("durationSecs"))
        except (TypeError, ValueError):
            continue
        if o_dur + tol < duration:
            continue  # theirs is shorter (missed part of the stream) — ours is worth sending
        overlap = min(start + duration, o_start + o_dur) - max(start, o_start)
        if overlap < 0.5 * duration:
            continue  # a different broadcast
        st = doc.get("state")
        if st == "sent" and _manifest_parts_present(target, doc):
            return "sent", doc
        if st == "sending" and now - float(doc.get("updatedAt") or 0) < ONE_COPY_CLAIM_STALE_SECS:
            sending = ("sending", doc)
    return sending


def _covered_note(doc):
    comp = doc.get("computer") or "another computer"
    n = len(doc.get("parts") or [])
    mins = float(doc.get("durationSecs") or 0) / 60
    return (f"Already in Drive from {comp} ({n} part{'s' if n != 1 else ''}, {mins:.0f} min) — "
            "skipped the duplicate")


def _covered_by(doc):
    return {"computer": doc.get("computer"), "base": doc.get("base"),
            "parts": len(doc.get("parts") or []), "durationSecs": doc.get("durationSecs"),
            "partNames": [p.get("name") for p in (doc.get("parts") or []) if isinstance(p, dict)][:40]}


def mark_delivery_covered(base, doc, target):
    note = _covered_note(doc)
    snap = _delivery_update(base, status="covered", coveredBy=_covered_by(doc), note=note,
                            target=target, error=None, lastAttempt=time.time())
    log(f"one-copy: {base} already in Drive from {doc.get('computer')} ({doc.get('base')}) — not sending duplicate parts")
    freed = clear_local_drive_parts(base)  # full -music.ogg is kept; Re-split for Drive can send anyway
    snap = _delivery_update(base, localPartsCleared=True, partsDir=None) or snap
    if freed:
        log(f"one-copy: cleared local parts base={base} freed={freed} bytes")
    STATUS_FILE_WAKE.set()
    return snap


def register_covered_delivery(base, doc, target):
    """Pre-voice-removal skip: record a delivery entry so History / status file show why."""
    now = time.time()
    with PIPELINE_LOCK:
        prev = PIPELINE["deliveries"].get(base)
        if prev and prev.get("status") in ("sent", "sending"):
            return dict(prev)
        PIPELINE["deliveries"][base] = {
            "base": base, "musicName": None, "partsDir": None, "parts": [], "sizes": [], "partSecs": 0,
            "status": "covered", "target": target, "sentAt": None, "error": None,
            "note": _covered_note(doc), "coveredBy": _covered_by(doc), "source": "stream",
            "createdAt": now, "updatedAt": now, "lastAttempt": now, "localPartsCleared": True,
        }
        snap = dict(PIPELINE["deliveries"][base])
    _pipeline_save()
    STATUS_FILE_WAKE.set()
    return snap


def one_copy_precheck(username, first_name, duration):
    """Before voice removal: (doc, target) when another computer already sent this stream."""
    if not one_copy_enabled():
        return None
    try:
        roots = detect_drive_roots()
        if not roots:
            return None
        target, _kind, err = resolve_delivery_target(roots, create=False)
        if err or not target:
            return None
        u, start = parse_recording_stamp(first_name)
        user = (username or u or "").lower()
        kind, doc = find_other_copy(target, user, start, duration)
        if kind == "sent":
            return doc, target
    except Exception as e:
        log(f"one-copy precheck skipped: {e}")
    return None


def original_enabled_for(username):
    u = (username or "").lower()
    with PIPELINE_LOCK:
        return bool(PIPELINE.get("origEnabled", True)) and bool((PIPELINE.get("origUsers") or {}).get(u, True))


def set_original_user_pref(username, on):
    u = safe_username(username) if username else None
    if not u:
        return
    with PIPELINE_LOCK:
        users = PIPELINE.setdefault("origUsers", {})
        if users.get(u) == bool(on):
            return
        users[u] = bool(on)
    _pipeline_save()


def _orig_update(name, **kw):
    with PIPELINE_LOCK:
        d = PIPELINE.setdefault("originals", {}).get(name)
        if d is None:
            return None
        d.update(kw)
        d["updatedAt"] = time.time()
        snap = dict(d)
    _pipeline_save()
    return snap


def _file_is_recording(path):
    try:
        ap = os.path.abspath(str(path))
    except OSError:
        return False
    with LOCK:
        for rec in ACTIVE.values():
            f = rec.get("file") or ""
            if f and os.path.abspath(f) == ap:
                return True
    return False


def register_original(name, username=None, source="stream", force=False):
    """Queue one finished native recording for copy to Drive/Originals. Idempotent."""
    name = basename_of(name)
    if not name or is_music_export_name(name) or is_seamless_export_name(name):
        return None
    p = REC_DIR / name
    try:
        size = p.stat().st_size
    except OSError:
        return None
    now = time.time()
    with PIPELINE_LOCK:
        origs = PIPELINE.setdefault("originals", {})
        d = origs.get(name)
        if d and not force and d.get("status") in ("queued", "sending", "sent", "waiting"):
            return dict(d)
        if d and d.get("status") == "sent" and force and d.get("size") == size:
            return dict(d)  # already in Drive, unchanged
        origs[name] = {
            "name": name, "base": Path(name).stem, "username": username or name.split("-")[0],
            "size": size, "status": "queued", "target": None, "dest": None, "action": None,
            "sentAt": None, "error": None, "note": "Queued — sending before voice removal",
            "source": source, "createdAt": (d or {}).get("createdAt") or now, "updatedAt": now,
            "lastAttempt": 0, "computer": computer_name(),
        }
        if len(origs) > 400:
            old = sorted(origs.values(), key=lambda x: x.get("updatedAt") or 0)
            for x in old[: len(old) - 400]:
                origs.pop(x.get("name"), None)
        snap = dict(origs[name])
    _pipeline_save()
    return snap


def deliver_original(name):
    """Copy one original into <target>/Originals. Never touches the local file."""
    with PIPELINE_LOCK:
        d = (PIPELINE.get("originals") or {}).get(name)
        if not d:
            return None
        d = dict(d)
    if d.get("status") == "sent":
        return d
    src = REC_DIR / name
    if not src.is_file():
        return _orig_update(name, status="error", error="local original is missing (deleted?)",
                            lastAttempt=time.time())
    if _file_is_recording(src):
        return _orig_update(name, status="waiting", note="Still recording — will send when it finishes",
                            lastAttempt=time.time())
    roots = detect_drive_roots()
    if not roots:
        return _orig_update(name, status="waiting", error=None, lastAttempt=time.time(),
                            note="Waiting for Drive (Drive for Desktop not found)")
    try:
        target, _kind, err = resolve_delivery_target(roots, create=True)
    except PermissionError:
        target, err = None, drive_permission_message()
    except OSError as e:
        target, err = None, f"cannot create target folder: {e}"
    if err or not target:
        return _orig_update(name, status="error", error=err or "no target", lastAttempt=time.time())
    dest_dir = Path(target) / ORIGINALS_SUBDIR
    _orig_update(name, status="sending", error=None, target=str(dest_dir), lastAttempt=time.time(),
                 note=None)
    DELIVERY_STATE["busy"] = True
    DELIVERY_STATE["base"] = Path(name).stem
    try:
        try:
            dest, action = place_in_drive(src, dest_dir, name)
            size = src.stat().st_size
        except Exception as e:
            log(f"drive original error {name}: {e}")
            msg = drive_permission_message() if isinstance(e, PermissionError) else (str(e) or type(e).__name__)
            return _orig_update(name, status="error", error=msg)
        # v6.51: the copy is in Drive — a failing log line can no longer mark it "error"
        snap = _orig_update(name, status="sent", sentAt=time.time(), dest=str(dest), action=action,
                            size=size, error=None)
        log(f"drive original {action}: {name} → {dest}")
        return snap
    finally:
        DELIVERY_STATE["busy"] = False
        DELIVERY_STATE["base"] = None


def originals_due(forced, now):
    with PIPELINE_LOCK:
        todo = []
        for name, d in (PIPELINE.get("originals") or {}).items():
            st = d.get("status")
            if st in ("queued", "waiting") or name in forced or ("*" in forced and st == "error"):
                todo.append(name)
            elif st == "error" and now - float(d.get("lastAttempt") or 0) > DELIVERY_ERROR_RETRY_SECS:
                todo.append(name)
    return todo


def originals_snapshot(limit=60):
    with PIPELINE_LOCK:
        items = sorted((PIPELINE.get("originals") or {}).values(),
                       key=lambda d: d.get("updatedAt") or 0, reverse=True)
        return [{k: d.get(k) for k in (
            "name", "base", "username", "size", "status", "target", "dest", "action", "sentAt",
            "error", "note", "source", "computer", "createdAt", "updatedAt")} for d in items[:limit]]


def forget_original_if_unsent(name):
    with PIPELINE_LOCK:
        origs = PIPELINE.get("originals") or {}
        d = origs.get(name)
        if d is None or d.get("status") == "sent":
            return
        origs.pop(name, None)
    _pipeline_save()


def wait_for_originals(job_id, names, progress):
    """Hold the voice-removal job until its originals are sent / waiting / failed."""
    deadline = time.time() + ORIGINAL_WAIT_SECS
    announced = False
    while time.time() < deadline:
        with PIPELINE_LOCK:
            origs = PIPELINE.get("originals") or {}
            pending = [n for n in names if (origs.get(n) or {}).get("status") in ("queued", "sending")]
        if not pending:
            return
        if not announced:
            progress("Sending the original to Drive first…", 2)
            _set_music_job(job_id, phase="original")
            announced = True
            request_delivery(None)
        _check_cancel(job_id)
        time.sleep(1)
    log(f"pipeline job={job_id}: originals still sending after {ORIGINAL_WAIT_SECS:.0f}s — continuing")


def api_pipeline_original(body):
    body = body if isinstance(body, dict) else {}
    target = resolve_download_path(body.get("name"))
    if not target:
        return {"ok": False, "error": "not found in ~/TwitchRecordings"}, 404
    if is_music_export_name(target.name) or is_seamless_export_name(target.name):
        return {"ok": False, "error": "pick an original recording (not a -music / seamless export)"}, 400
    if _file_is_recording(target):
        return {"ok": False, "error": "still recording — it is sent automatically when it finishes"}, 409
    snap = register_original(target.name, source="manual", force=True)
    if not snap:
        return {"ok": False, "error": "could not queue that file"}, 400
    if snap.get("status") != "sent":
        _orig_update(target.name, status="queued")
    found = bool(detect_drive_roots())
    request_delivery(target.name)
    return {"ok": True, "driveFound": found, "original": snap,
            "message": "sending…" if found else "Waiting for Drive — Drive for Desktop not found"}, 200


def files_payload():
    REC_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK:
        reap_locked()
        active_paths = {}
        for user, rec in ACTIVE.items():
            path = rec.get("file") or ""
            if path:
                active_paths[os.path.abspath(path)] = user
    entries = []
    try:
        names = list(REC_DIR.iterdir())
    except OSError as e:
        return {"ok": False, "error": str(e), "files": []}
    for p in names:
        # v6.35: hide dotfiles (.drive-history.json, .partial temps, .DS_Store)
        if p.name.startswith("."):
            continue
        try:
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        abspath = os.path.abspath(str(p))
        recording = abspath in active_paths
        entries.append(
            {
                "name": p.name,
                "path": str(p),
                "size": int(st.st_size),
                "mtime": int(st.st_mtime),
                "recording": recording,
                "url": "/api/download/" + urllib.parse.quote(p.name),
            }
        )
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    entries = entries[:FILES_CAP]
    return {"ok": True, "dir": str(REC_DIR), "files": entries}


# ─── v6.41: STATUS FILE IN GOOGLE DRIVE (REMOTE HEARTBEAT) ─────
# Eric records on several computers (and one of them can't run anything but the helper), and
# nobody can tell from outside whether a given helper is up, armed, recording or stuck on a
# Drive send. Every few minutes — and right away when something changes (a recording starts or
# stops, a delivery is sent or fails) — the helper writes a small status file for THIS computer
# into the Drive target via Drive for Desktop:
#   <Drive target>/Recorder status/<computer>.txt   (plain-English summary)
#   <Drive target>/Recorder status/<computer>.json  (same facts, machine-readable)
# Anyone (or any assistant) with Drive access can read them; a file that stops updating means
# that computer is off, asleep, offline, or its helper isn't running. Writes are atomic
# (temp + rename) and never block recording; failures are soft and show in /api/health.
STATUS_SUBDIR = "Recorder status"
STATUS_FILE_INTERVAL_SECS = max(30, int(os.environ.get("TWITCH_RECORDER_STATUS_SECS") or 300))
STATUS_FILE_TICK_SECS = max(1.0, float(os.environ.get("TWITCH_RECORDER_STATUS_TICK_SECS") or 15))
STATUS_FILE_MIN_GAP_SECS = max(0.0, float(os.environ.get("TWITCH_RECORDER_STATUS_MIN_GAP_SECS") or 20))
STATUS_FILE_WAKE = threading.Event()
STATUS_FILE_LOCK = threading.Lock()
STATUS_FILE = {"lastWrittenAt": 0.0, "lastAttemptAt": 0.0, "folder": None, "txtPath": None,
               "jsonPath": None, "error": None, "sig": None, "writes": 0,
               # v6.47: local mirror under REC_DIR (no FDA) + Drive write tracked separately
               "localLastWrittenAt": 0.0, "localTxtPath": None, "localError": None}
PAGE_SEEN = {"at": 0.0, "armed": []}
HELPER_STARTED_AT = time.time()

# ─── v6.49: HELPER SOLO-WATCH WHEN THE RECORDER PAGE IS CLOSED ─────────
# Persist the last Auto-Rec armed list (+ per-streamer /api/record options) so a reboot
# or closed Chrome tab does not leave the helper idle while an armed streamer goes live.
def _solo_watch_path():
    return REC_DIR / ".solo-watch.json"

SOLO_WATCH_LOCK = threading.Lock()
SOLO_WATCH = {
    "enabled": True,  # default on; older pages that never send the toggle stay on
    "users": {},  # username -> {quality, afterStream, sendOriginal}
    "active": False,  # True while the background loop considers the page gone
    "lastCheckAt": 0.0,
    "lastError": None,
    "startedByHelper": [],  # usernames this process started via solo-watch (still ACTIVE)
    "loggedWatching": False,
}
SOLO_WATCH_WAKE = threading.Event()


def _solo_default_opts(username=None):
    u = (username or "").lower()
    with PIPELINE_LOCK:
        after = bool(PIPELINE.get("enabled", True)) and bool(PIPELINE.get("users", {}).get(u, True))
        send_o = bool(PIPELINE.get("origEnabled", True)) and bool(
            (PIPELINE.get("origUsers") or {}).get(u, True)
        )
    return {"quality": "audio_only", "afterStream": after, "sendOriginal": send_o}


def _solo_normalize_opts(opts, username=None):
    base = _solo_default_opts(username)
    if not isinstance(opts, dict):
        return base
    q = opts.get("quality")
    base["quality"] = q if q in ("audio_only", "best") else "audio_only"
    if "afterStream" in opts:
        base["afterStream"] = bool(opts.get("afterStream"))
    if "sendOriginal" in opts:
        base["sendOriginal"] = bool(opts.get("sendOriginal"))
    return base


def solo_watch_load():
    """Load ~/TwitchRecordings/.solo-watch.json (armed list + options + enabled)."""
    try:
        data = json.loads(_solo_watch_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    users = {}
    raw_users = data.get("users") if isinstance(data.get("users"), dict) else {}
    for k, v in raw_users.items():
        u = safe_username(k)
        if not u:
            continue
        users[u] = _solo_normalize_opts(v if isinstance(v, dict) else {}, u)
        if len(users) >= 50:
            break
    with SOLO_WATCH_LOCK:
        # missing key = default on (older pages / first run)
        SOLO_WATCH["enabled"] = data.get("enabled") is not False
        SOLO_WATCH["users"] = users
    if users:
        log(f"solo watch: loaded {len(users)} armed streamer(s) from disk — "
            + ", ".join(list(users.keys())[:8]) + ("…" if len(users) > 8 else ""))


def solo_watch_save_locked():
    """Caller holds SOLO_WATCH_LOCK. Atomic temp+rename like .drive-pipeline.json."""
    blob = json.dumps({
        "enabled": bool(SOLO_WATCH.get("enabled", True)),
        "users": dict(SOLO_WATCH.get("users") or {}),
        "updatedAt": time.time(),
    }, indent=1).encode("utf-8")
    try:
        REC_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(_solo_watch_path(), blob)
    except Exception as e:
        log(f"solo watch state write failed: {e}")


def solo_watch_set_enabled(on, save=True):
    on = bool(on)
    with SOLO_WATCH_LOCK:
        if SOLO_WATCH.get("enabled") is on:
            return False
        SOLO_WATCH["enabled"] = on
        if save:
            solo_watch_save_locked()
    log(f"solo watch: {'on' if on else 'OFF'} (keep recording armed streamers if the page is closed)")
    SOLO_WATCH_WAKE.set()
    STATUS_FILE_WAKE.set()
    return True


def solo_watch_remember_options(username, quality=None, after_stream=None, send_original=None):
    """Remember last /api/record options for a streamer (used when the page is gone).

    Only updates (or adds) when the streamer is already in the persisted armed list or
    the latest page heartbeat lists them as Auto-Rec armed — a manual Start on someone
    not armed must not enroll them for solo-watch.
    """
    u = safe_username(username)
    if not u:
        return
    with STATUS_FILE_LOCK:
        page_armed = [str(a).lower() for a in (PAGE_SEEN.get("armed") or [])]
    with SOLO_WATCH_LOCK:
        users = dict(SOLO_WATCH.get("users") or {})
        if u not in users and u not in page_armed:
            return
        cur = dict(users.get(u) or _solo_default_opts(u))
        if quality in ("audio_only", "best"):
            cur["quality"] = quality
        if after_stream is not None:
            cur["afterStream"] = bool(after_stream)
        if send_original is not None:
            cur["sendOriginal"] = bool(send_original)
        users[u] = cur
        SOLO_WATCH["users"] = users
        solo_watch_save_locked()


def solo_watch_set_armed_users(usernames, explicit_empty=False):
    """Update persisted armed list from a fresh page heartbeat.

    Non-empty list replaces the set of users (options kept when already known).
    Explicit empty (page said armed= with nobody) clears the list — user turned Auto-Rec off.
    """
    cleaned = []
    for part in usernames or []:
        u = safe_username(part)
        if u and u not in cleaned:
            cleaned.append(u)
        if len(cleaned) >= 50:
            break
    with SOLO_WATCH_LOCK:
        prev = dict(SOLO_WATCH.get("users") or {})
        if not cleaned and explicit_empty:
            if not prev:
                return False
            SOLO_WATCH["users"] = {}
            solo_watch_save_locked()
            changed = True
        elif cleaned:
            next_users = {}
            for u in cleaned:
                next_users[u] = dict(prev.get(u) or _solo_default_opts(u))
            if next_users == prev:
                return False
            SOLO_WATCH["users"] = next_users
            solo_watch_save_locked()
            changed = True
        else:
            return False
    if changed:
        SOLO_WATCH_WAKE.set()
        STATUS_FILE_WAKE.set()
    return changed


def page_heartbeat_is_stale(now=None):
    """True when no fresh page heartbeat (never seen, or older than PAGE_HEARTBEAT_STALE_SECS)."""
    now = time.time() if now is None else now
    with STATUS_FILE_LOCK:
        seen_at = float(PAGE_SEEN.get("at") or 0.0)
    if not seen_at:
        return True
    try:
        stale = float(PAGE_HEARTBEAT_STALE_SECS)
    except (TypeError, ValueError):
        stale = 120.0
    if stale <= 0:
        return False
    return (now - seen_at) >= stale


def solo_watch_should_run(now=None, helper_started_at=None):
    """Decision logic for the background loop (no network). Self-test covers this.

    Active when: enabled, interval > 0, page heartbeat stale (or never seen), a persisted
    armed list exists, and the startup delay has elapsed (so a rebooting page can reconnect).
    """
    now = time.time() if now is None else now
    started = HELPER_STARTED_AT if helper_started_at is None else float(helper_started_at)
    with SOLO_WATCH_LOCK:
        enabled = SOLO_WATCH.get("enabled") is not False
        users = list((SOLO_WATCH.get("users") or {}).keys())
    if not enabled:
        return False, "disabled"
    if SOLO_WATCH_SECS <= 0:
        return False, "env-off"
    if not users:
        return False, "no-armed"
    if (now - started) < SOLO_WATCH_STARTUP_DELAY_SECS:
        # Only gate on startup delay when the page has never reported in (reboot race).
        with STATUS_FILE_LOCK:
            seen_at = float(PAGE_SEEN.get("at") or 0.0)
        if not seen_at:
            return False, "startup-delay"
    if not page_heartbeat_is_stale(now):
        return False, "page-open"
    return True, "page-closed"


def solo_watch_snapshot():
    with SOLO_WATCH_LOCK:
        users = sorted((SOLO_WATCH.get("users") or {}).keys())
        started = list(SOLO_WATCH.get("startedByHelper") or [])
        return {
            "enabled": SOLO_WATCH.get("enabled") is not False,
            "active": bool(SOLO_WATCH.get("active")),
            "users": users,
            "lastCheckAt": float(SOLO_WATCH.get("lastCheckAt") or 0.0) or None,
            "lastError": SOLO_WATCH.get("lastError"),
            "startedByHelper": started,
            "intervalSecs": SOLO_WATCH_SECS,
            "startupDelaySecs": SOLO_WATCH_STARTUP_DELAY_SECS,
        }


def twitch_gql_is_live(username):
    """Lightweight live check via Twitch public GQL (no user credentials). Returns True/False.
    Raises on transport/parse errors so the caller can back off."""
    u = safe_username(username)
    if not u:
        return False
    body = json.dumps({
        "query": (
            "query($login:String!){ user(login:$login){ id login stream { id type } } }"
        ),
        "variables": {"login": u},
    }).encode("utf-8")
    req = urllib.request.Request(
        TWITCH_GQL_URL,
        data=body,
        headers={
            "Client-ID": TWITCH_GQL_CLIENT_ID,
            "Content-Type": "application/json",
            "User-Agent": f"TwitchAutoRecorder/{HELPER_VERSION}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("gql: unexpected response")
    if data.get("errors"):
        # Soft: treat GraphQL errors as not-live rather than crashing the loop
        err0 = data["errors"][0] if isinstance(data["errors"], list) and data["errors"] else {}
        msg = (err0.get("message") if isinstance(err0, dict) else None) or "gql error"
        raise RuntimeError(f"gql: {msg}")
    user = ((data.get("data") or {}).get("user") if isinstance(data.get("data"), dict) else None)
    if not user:
        return False
    stream = user.get("stream") if isinstance(user, dict) else None
    return bool(stream and (stream.get("id") or stream.get("type")))


def twitch_streamlink_is_live(username):
    """Fallback live probe: streamlink --json (no credentials). True/False; raises on hard fail."""
    u = safe_username(username)
    sl = which_streamlink()
    if not u or not sl:
        return False
    try:
        r = subprocess.run(
            [sl, "--json", f"https://twitch.tv/{u}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,  # v6.51
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("streamlink probe timed out") from e
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "")[:200].lower()
        if any(h in err for h in ("no playable streams", "no streams found", "is offline", "offline")):
            return False
        raise RuntimeError(f"streamlink probe failed (exit {r.returncode})")
    try:
        data = json.loads(r.stdout or "{}")
    except ValueError as e:
        raise RuntimeError("streamlink --json parse failed") from e
    streams = data.get("streams") if isinstance(data, dict) else None
    return bool(streams)


def streamer_is_live_public(username):
    """Prefer GQL; fall back to streamlink --json. Returns (live:bool, method:str)."""
    try:
        return bool(twitch_gql_is_live(username)), "gql"
    except Exception as gql_err:
        try:
            return bool(twitch_streamlink_is_live(username)), "streamlink"
        except Exception as sl_err:
            raise RuntimeError(f"gql: {gql_err}; streamlink: {sl_err}") from sl_err


def _solo_already_busy(username):
    """True if recording / starting / in restart grace — never double-start."""
    u = (username or "").lower()
    with LOCK:
        reap_locked()
        rec = ACTIVE.get(u)
        if not rec:
            return False
        if rec.get("want"):
            return True
        if float(rec.get("graceUntil") or 0) > time.time():
            return True
        return True  # any ACTIVE slot for this user


def solo_watch_start_user(username, opts):
    """Start via the same path as /api/record (supervisor, pipeline prefs, dedupe)."""
    u = safe_username(username)
    if not u:
        return None
    opts = _solo_normalize_opts(opts, u)
    # Mirror page /api/record: stamp pipeline prefs before start
    set_pipeline_user_pref(u, bool(opts.get("afterStream")))
    set_original_user_pref(u, bool(opts.get("sendOriginal")))
    result = start_record(u, opts.get("quality") or "audio_only", started_by="helper")
    if result.get("ok") and not result.get("already"):
        with SOLO_WATCH_LOCK:
            started = list(SOLO_WATCH.get("startedByHelper") or [])
            if u not in started:
                started.append(u)
            SOLO_WATCH["startedByHelper"] = started
        STATUS_FILE_WAKE.set()
        log(f"solo watch: {u} live — starting recording")
    return result


def solo_watch_prune_started():
    with LOCK:
        active = set(ACTIVE.keys())
    with SOLO_WATCH_LOCK:
        started = [u for u in (SOLO_WATCH.get("startedByHelper") or []) if u in active]
        SOLO_WATCH["startedByHelper"] = started


def solo_watch_tick():
    """One solo-watch cycle. Quiet on errors (backoff via lastError)."""
    solo_watch_prune_started()
    now = time.time()
    should, reason = solo_watch_should_run(now)
    with SOLO_WATCH_LOCK:
        was_active = bool(SOLO_WATCH.get("active"))
        SOLO_WATCH["active"] = bool(should)
        users = dict(SOLO_WATCH.get("users") or {})
        if should and not was_active:
            names = ", ".join(sorted(users.keys())[:8])
            more = "…" if len(users) > 8 else ""
            log(f"solo watch: page closed — watching {names}{more}")
            SOLO_WATCH["loggedWatching"] = True
        elif not should and was_active:
            log(f"solo watch: idle ({reason})")
            SOLO_WATCH["loggedWatching"] = False
    if not should:
        return
    SOLO_WATCH_WAKE.clear()
    err = None
    for u, opts in list(users.items()):
        if _solo_already_busy(u):
            continue
        try:
            live, method = streamer_is_live_public(u)
            with SOLO_WATCH_LOCK:
                SOLO_WATCH["lastCheckAt"] = time.time()
                SOLO_WATCH["lastError"] = None
            if not live:
                continue
            log(f"solo watch: {u} live ({method}) — starting recording")
            solo_watch_start_user(u, opts)
        except Exception as e:
            err = str(e)[:240]
            with SOLO_WATCH_LOCK:
                SOLO_WATCH["lastCheckAt"] = time.time()
                # Don't spam: only log when the error text changes
                prev = SOLO_WATCH.get("lastError")
                SOLO_WATCH["lastError"] = err
            if err != prev:
                log(f"solo watch: live-check failed for {u}: {err}")
            # Back off the rest of this tick on transport errors
            break
    STATUS_FILE_WAKE.set()


def solo_watch_loop():
    """Background: every SOLO_WATCH_SECS when the page is gone, check armed streamers."""
    # Startup delay before first decision (page reconnect race after reboot)
    deadline = time.time() + SOLO_WATCH_STARTUP_DELAY_SECS
    while time.time() < deadline:
        SOLO_WATCH_WAKE.wait(timeout=min(5.0, max(0.5, deadline - time.time())))
        SOLO_WATCH_WAKE.clear()
        # If the page already heartbeated, no need to sit out the full delay for "page-open"
        with STATUS_FILE_LOCK:
            if float(PAGE_SEEN.get("at") or 0.0):
                break
    while True:
        try:
            solo_watch_tick()
        except Exception as e:
            log(f"solo watch: tick error: {e}")
            with SOLO_WATCH_LOCK:
                SOLO_WATCH["lastError"] = str(e)[:240]
        interval = SOLO_WATCH_SECS if SOLO_WATCH_SECS > 0 else 60.0
        SOLO_WATCH_WAKE.wait(timeout=max(5.0, interval))
        SOLO_WATCH_WAKE.clear()


def status_file_enabled():
    with PIPELINE_LOCK:
        return PIPELINE.get("statusToDrive", True) is not False


def note_page_seen(raw_armed):
    users = []
    for part in str(raw_armed or "").split(","):
        u = safe_username(part)
        if u and u not in users:
            users.append(u)
        if len(users) >= 50:
            break
    with STATUS_FILE_LOCK:
        changed = users != PAGE_SEEN.get("armed")
        PAGE_SEEN["at"] = time.time()
        PAGE_SEEN["armed"] = users
    # v6.49: persist armed list for solo-watch (empty = user cleared Auto-Rec — clear disk too)
    try:
        solo_watch_set_armed_users(users, explicit_empty=True)
    except Exception as e:
        log(f"solo watch armed sync failed: {e}")
    if changed:
        STATUS_FILE_WAKE.set()
        SOLO_WATCH_WAKE.set()


def _iso_local(ts):
    try:
        if not ts:
            return None
        return datetime.datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _short(text, limit=300):
    t = str(text or "")
    return t if len(t) <= limit else t[: limit - 1] + "…"


def _status_change_sig():
    """Cheap fingerprint of the things worth an immediate status write (no Drive I/O)."""
    with LOCK:
        reap_locked()
        active = tuple(sorted((u, basename_of(r.get("file") or ""), bool(r.get("retrying") or r.get("starting")))
                              for u, r in ACTIVE.items()))
        errs = tuple(sorted(LAST_ERROR.keys()))
    with PIPELINE_LOCK:
        dels = tuple(sorted((k, d.get("status")) for k, d in PIPELINE["deliveries"].items()))
        origs = tuple(sorted((k, d.get("status")) for k, d in (PIPELINE.get("originals") or {}).items()))
    q = music_demucs_queue_info()
    with STATUS_FILE_LOCK:
        armed = tuple(PAGE_SEEN.get("armed") or [])
    au = auto_update_snapshot()  # v6.43
    sw = solo_watch_snapshot()  # v6.49
    return repr((active, errs, dels, origs, int(q.get("running") or 0), int(q.get("waiting") or 0),
                 armed, pending_restart_needed(), au.get("enabled"), au.get("state"), au.get("lastError"),
                 au.get("remoteVersion"), sw.get("enabled"), sw.get("active"), tuple(sw.get("users") or [])))


def _drive_access_kind(err):
    """Classify a Drive write/resolve error for UI banners (fda / missing / other)."""
    e = str(err or "").lower()
    if "full disk access" in e or "operation not permitted" in e:
        return "fda"
    if "google drive for desktop not found" in e:
        return "missing"
    if "not found" in e and "drive" in e:
        return "missing"
    return "other"


def drive_access_snapshot():
    """v6.56: base snapshot + exact Full Disk Access path(s), settings link and the stuck backlog."""
    snap = _drive_access_snapshot_base()
    try:
        snap.update(drive_access_extra(snap.get("kind")))
    except Exception as e:
        snap["extraError"] = str(e)
    return snap


def _drive_access_snapshot_base():
    """Whether Drive status/delivery writes can succeed. Prefer STATUS_FILE.error from
    write_status_file (the real probe); when no error yet, refresh from Drive roots + target."""
    with STATUS_FILE_LOCK:
        err = STATUS_FILE.get("error")
        last = float(STATUS_FILE.get("lastWrittenAt") or 0.0)
    if err:
        return {"ok": False, "error": err, "kind": _drive_access_kind(err)}
    roots = detect_drive_roots()
    if not roots:
        return {"ok": False, "error": "Google Drive for Desktop not found", "kind": "missing"}
    try:
        target, _kind, terr = resolve_delivery_target(roots, create=False)
    except PermissionError:
        msg = drive_permission_message()
        return {"ok": False, "error": msg, "kind": _drive_access_kind(msg)}
    except Exception as e:
        msg = str(e) or type(e).__name__
        return {"ok": False, "error": msg, "kind": _drive_access_kind(msg)}
    if terr or not target:
        msg = terr or "Drive target unavailable"
        return {"ok": False, "error": msg, "kind": _drive_access_kind(msg)}
    # Drive found + writable path resolved; lastWrittenAt may still be 0 before first write
    return {"ok": True, "error": None, "kind": "ok"}


def status_local_paths():
    """Local Recorder status mirror under REC_DIR (no Full Disk Access needed)."""
    folder = REC_DIR / STATUS_SUBDIR
    name = computer_name()
    return folder, folder / f"{name}.txt", folder / f"{name}.json"


def build_status_doc(now=None):
    now = time.time() if now is None else now
    with LOCK:
        reap_locked()
        active_items = [(u, dict(r)) for u, r in ACTIVE.items()]
        last_errors = dict(LAST_ERROR)
    disk = disk_status()
    with STATUS_FILE_LOCK:
        page_at = float(PAGE_SEEN.get("at") or 0.0)
        armed = list(PAGE_SEEN.get("armed") or [])
    page_open = bool(page_at) and now - page_at < PAGE_HEARTBEAT_STALE_SECS  # v6.46
    recording = []
    for u, a in sorted(active_items):
        path = a.get("file") or ""
        recording.append({
            "username": u,
            "file": basename_of(path),
            "startedAt": a.get("sessionStartedAt") or a.get("startedAt") or "",
            "bytes": int(file_size(path) or 0) if path else 0,
            "segments": len(a.get("sessionSegments") or []) + 1,
            "retrying": bool(a.get("retrying") or a.get("starting")),
            "waitingForRestartUntil": _iso_local(a.get("graceUntil")) if float(a.get("graceUntil") or 0) > now else None,
        })
    dels = deliveries_snapshot(6)
    origs = originals_snapshot(6)
    with PIPELINE_LOCK:
        all_dels = list(PIPELINE["deliveries"].values())
        all_origs = list((PIPELINE.get("originals") or {}).values())
        pipe_on = bool(PIPELINE.get("enabled", True))
        orig_on = bool(PIPELINE.get("origEnabled", True))
    roots = detect_drive_roots()
    target, _kind, terr = resolve_delivery_target(roots, create=False) if roots else (None, "auto", "Google Drive for Desktop not found")
    recent = []
    try:
        for f in (files_payload().get("files") or []):
            if f.get("recording"):
                continue
            recent.append({"name": f.get("name"), "bytes": int(f.get("size") or 0),
                           "modifiedAt": _iso_local(f.get("mtime"))})
            if len(recent) >= 8:
                break
    except Exception:
        pass
    q = music_demucs_queue_info()
    ka = keep_awake_snapshot()
    au = auto_update_snapshot()  # v6.43
    doc = {
        "computer": computer_name(),
        "updatedAt": _iso_local(now),
        "updatedAtEpoch": int(now),
        "nextUpdateBy": _iso_local(now + STATUS_FILE_INTERVAL_SECS),
        "staleAfterSecs": STATUS_FILE_INTERVAL_SECS * 3,
        "helperVersion": HELPER_VERSION,
        "diskVersion": disk_file_version(),
        "pendingRestart": pending_restart_needed(),
        "helperStartedAt": _iso_local(HELPER_STARTED_AT),
        "platform": sys.platform,
        "streamlink": bool(which_streamlink()),
        "ffmpeg": bool(which_ffmpeg()),
        "demucs": demucs_available(),
        "diskFreeBytes": disk.get("diskFree"),
        "diskWarn": bool(disk.get("diskWarn")),
        "diskBlock": bool(disk.get("diskBlock")),
        "recorderPageOpen": page_open,
        "recorderPageLastSeen": _iso_local(page_at) if page_at else None,
        "autoRecArmed": armed if page_open else [],
        "recording": recording,
        "lastErrors": {u: _short(e) for u, e in last_errors.items()},
        "voiceRemoval": {"running": int(q.get("running") or 0), "waiting": int(q.get("waiting") or 0)},
        "drive": {
            "found": bool(roots),
            "target": target,
            "targetError": terr,
            "autoSend": pipe_on,
            "sendOriginals": orig_on,
            "oneCopyPerStream": one_copy_enabled(),  # v6.50
            "partsSkippedAsDuplicate": sum(1 for d in all_dels if d.get("status") == "covered"),
            "partsWaiting": sum(1 for d in all_dels if d.get("status") == "waiting"),
            "partsSending": sum(1 for d in all_dels if d.get("status") == "sending"),
            "partsErrors": sum(1 for d in all_dels if d.get("status") == "error"),
            "originalsWaiting": sum(1 for d in all_origs if d.get("status") in ("waiting", "queued")),
            "originalsErrors": sum(1 for d in all_origs if d.get("status") == "error"),
            "recentDeliveries": [{
                "base": d.get("base"), "status": d.get("status"), "parts": len(d.get("parts") or []),
                "sentAt": _iso_local(d.get("sentAt")), "note": _short(d.get("note"), 200),
                "error": _short(d.get("error"), 300) or None} for d in dels],
            "recentOriginals": [{
                "name": o.get("name"), "status": o.get("status"),
                "sentAt": _iso_local(o.get("sentAt")), "note": _short(o.get("note"), 200),
                "error": _short(o.get("error"), 300) or None} for o in origs],
        },
        # v6.53: recordings cut off by a helper crash / reboot and what happened to them
        "recovered": [f"{_iso_local(x.get('at'))} {x.get('username')}: {x.get('action')} "
                      f"({len(x.get('segments') or [])} segment(s))" for x in LIVE_RECOVER_LOG[-3:]],
        "recoverPending": sorted(ORPHAN_SESSIONS.keys()),
        "keepAwake": {"active": bool(ka.get("active")), "reason": ka.get("reason"),
                      "supported": bool(ka.get("supported")), "error": ka.get("error"),
                      # v6.52: learned go-live windows (ready check can see them)
                      "scheduleEnabled": bool(ka.get("scheduleEnabled")),
                      "goLiveWindows": [f"{w['username']} {w['weekdayName']} {w['from']}–{w['to']}"
                                        for w in ((ka.get("schedule") or {}).get("windows") or [])],
                      # v6.58: scheduled wake from sleep (Windows)
                      "wake": ka.get("wake")},
        # v6.43: unattended helper updates (ready check can see on/off, last check, last error)
        "autoUpdate": {"enabled": au.get("enabled"), "state": au.get("state"),
                       "lastCheckAt": _iso_local(au.get("lastCheckAt")), "lastResult": au.get("lastResult"),
                       "lastError": _short(au.get("lastError"), 300) or None,
                       "remoteVersion": au.get("remoteVersion"), "waitingFor": au.get("waitingFor"),
                       "lastAppliedVersion": au.get("lastAppliedVersion"),
                       "lastAppliedAt": _iso_local(au.get("lastAppliedAt"))},
        # v6.55: installed vs newest streamlink (outdated streamlink = Twitch recordings fail)
        "streamlinkUpdate": {k: v for k, v in streamlink_update_snapshot().items()
                             if k in ("enabled", "state", "installed", "kind", "pythonVersion", "latest",
                                      "latestCompatible", "latestRequiresPython", "installedReleasedAt",
                                      "outdated", "hint", "waitingFor", "lastResult", "lastError",
                                      "lastUpgradeFrom", "lastUpgradeTo", "private")},
        "streamlinkLine": streamlink_status_line(streamlink_update_snapshot()),
        "recentRecordings": recent,
        # v6.49: helper watching armed streamers while the recorder page is closed
        "soloWatch": solo_watch_snapshot(),
    }
    # v6.47: local mirror path + Drive write error so the local JSON is useful when Drive is blocked
    local_folder, local_txt, _local_js = status_local_paths()
    with STATUS_FILE_LOCK:
        drive_err = STATUS_FILE.get("error")
    doc["statusLocalPath"] = str(local_txt)
    doc["driveStatusError"] = drive_err
    doc["driveAccess"] = drive_access_snapshot()
    return doc


def _fmt_mb(n):
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "?"
    if v >= 1e9:
        return f"{v / 1e9:.1f} GB"
    return f"{v / 1e6:.0f} MB"


def render_status_text(doc):
    L = []
    L.append(f"Twitch Auto-Recorder status — {doc['computer']}")
    L.append(f"Updated {doc['updatedAt']} (rewritten every {STATUS_FILE_INTERVAL_SECS // 60} min while the helper runs;"
             f" if this is more than {doc['staleAfterSecs'] // 60} min old, this computer is off, asleep, offline,"
             " or the helper isn't running)")
    ver = f"Helper v{doc['helperVersion']}"
    if doc.get("pendingRestart"):
        ver += f" (v{doc.get('diskVersion')} installed, restart pending)"
    ver += f", running since {doc.get('helperStartedAt')}"
    L.append(ver)
    tools = [("streamlink", doc["streamlink"]), ("ffmpeg", doc["ffmpeg"]), ("demucs", doc["demucs"])]
    L.append("Tools: " + ", ".join(f"{n} {'ok' if ok else 'MISSING'}" for n, ok in tools))
    if doc.get("streamlinkLine"):  # v6.55
        L.append(doc["streamlinkLine"])
    disk = f"Disk free: {_fmt_mb(doc.get('diskFreeBytes'))}"
    if doc.get("diskBlock"):
        disk += " — ALMOST FULL, new recordings blocked"
    elif doc.get("diskWarn"):
        disk += " — low"
    L.append(disk)
    if doc.get("recorderPageOpen"):
        armed = doc.get("autoRecArmed") or []
        L.append("Recorder page: open; Auto-Rec armed for " + (", ".join(armed) if armed else "nobody"))
    else:
        seen = doc.get("recorderPageLastSeen")
        sw = doc.get("soloWatch") or {}
        sw_users = sw.get("users") or []
        if sw.get("enabled") is not False and sw_users:
            who = ", ".join(sw_users[:8]) + ("…" if len(sw_users) > 8 else "")
            L.append("Recorder page: NOT open" + (f" (last seen {seen})" if seen else "") +
                     f" — helper watching {who} on its own")
        elif sw.get("enabled") is False:
            L.append("Recorder page: NOT open" + (f" (last seen {seen})" if seen else "") +
                     " — solo watch OFF (Auto-Rec will not start without the page)")
        else:
            L.append("Recorder page: NOT open" + (f" (last seen {seen})" if seen else "") +
                     " — no armed streamers persisted for solo watch")
    rec = doc.get("recording") or []
    if rec:
        for r in rec:
            L.append(f"RECORDING {r['username']} since {r['startedAt']} — {r['file']}, {_fmt_mb(r['bytes'])}"
                     + (f", segment {r['segments']}" if r.get("segments", 1) > 1 else "")
                     + (" (waiting for the stream to come back, until " + r["waitingForRestartUntil"][11:16] + ")"
                        if r.get("retrying") and r.get("waitingForRestartUntil")
                        else (" (reconnecting)" if r.get("retrying") else "")))
    else:
        L.append("Recording: nothing right now")
    for u, e in (doc.get("lastErrors") or {}).items():
        L.append(f"Last error for {u}: {e}")
    vr = doc.get("voiceRemoval") or {}
    if vr.get("running") or vr.get("waiting"):
        L.append(f"Voice removal: {vr.get('running', 0)} running, {vr.get('waiting', 0)} waiting")
    d = doc.get("drive") or {}
    if not d.get("found"):
        L.append("Google Drive: Drive for Desktop not found")
    else:
        L.append(f"Google Drive target: {d.get('target') or '?'}" + (f" — {d['targetError']}" if d.get("targetError") else ""))
    # v6.47: one clear line for local mirror + Drive status-write problems (FDA / missing Drive)
    local_p = doc.get("statusLocalPath")
    drive_werr = doc.get("driveStatusError")
    if local_p or drive_werr:
        line = "Status file: "
        if local_p:
            line += f"local mirror {local_p}"
        if drive_werr:
            line += ("; " if local_p else "") + f"Drive write FAILED — {drive_werr}"
        elif local_p:
            line += " (also posted to Drive when reachable)"
        L.append(line)
    L.append(f"Drive sends: {d.get('partsWaiting', 0)} part sets waiting, {d.get('partsErrors', 0)} failed;"
             f" {d.get('originalsWaiting', 0)} originals waiting, {d.get('originalsErrors', 0)} failed")
    if "oneCopyPerStream" in d:  # v6.50
        L.append("One copy per stream: " + (
            "on — this computer skips voice removal / parts when another computer already sent that stream"
            if d.get("oneCopyPerStream") else "OFF — this computer sends its own parts for every stream"))
    for x in d.get("recentDeliveries") or []:
        line = f"  parts {x['base']}: {x['status']}"
        if x.get("sentAt"):
            line += f" at {x['sentAt']}"
        if x.get("error"):
            line += f" — {x['error']}"
        elif x.get("note") and x.get("status") != "sent":
            line += f" — {x['note']}"
        L.append(line)
    for x in d.get("recentOriginals") or []:
        line = f"  original {x['name']}: {x['status']}"
        if x.get("sentAt"):
            line += f" at {x['sentAt']}"
        if x.get("error"):
            line += f" — {x['error']}"
        elif x.get("note") and x.get("status") != "sent":
            line += f" — {x['note']}"
        L.append(line)
    au = doc.get("autoUpdate") or {}  # v6.43
    if au:
        if not au.get("enabled"):
            line = "Helper auto-update: OFF (updates only via Check for updates → Install update)"
        else:
            line = "Helper auto-update: on"
            st = au.get("state")
            if st == "waiting-idle":
                line += f" — v{au.get('remoteVersion')} available, installing when idle ({au.get('waitingFor')})"
            elif st == "restart-pending":
                line += " — update installed, restarting when idle" + (
                    f" ({au.get('waitingFor')})" if au.get("waitingFor") else "")
            elif st in ("installing", "restarting", "checking"):
                line += f" — {st}"
        if au.get("lastCheckAt"):
            line += f"; last checked {au['lastCheckAt']}" + (f" ({au['lastResult']})" if au.get("lastResult") else "")
        else:
            line += "; not checked yet (first check ~2 min after the helper starts)"
        L.append(line)
        if au.get("lastError"):
            L.append(f"Helper auto-update problem: {au['lastError']}")
        if au.get("lastAppliedVersion"):
            L.append(f"Last auto-installed: v{au['lastAppliedVersion']} at {au.get('lastAppliedAt')}")
    ka = doc.get("keepAwake") or {}
    if ka.get("active"):
        L.append(f"Keeping the computer awake: {ka.get('reason')}")
    elif ka.get("error"):
        L.append(f"Keep awake problem: {ka.get('error')}")
    if ka.get("goLiveWindows"):  # v6.52
        L.append("Stays awake before usual go-live: " + "; ".join(ka["goLiveWindows"]))
    elif ka.get("scheduleEnabled") is False:
        L.append("Stay awake before usual go-live times: OFF")
    wk = ka.get("wake") or {}
    if wk.get("supported"):  # v6.58
        if wk.get("next"):
            L.append("Wakes from sleep for: " + "; ".join(wk["next"][:4]))
        if wk.get("error"):
            L.append(f"Wake-from-sleep problem: {wk['error']}")
        if wk.get("wakeTimersHint") and wk.get("next"):
            L.append(wk["wakeTimersHint"])
    if doc.get("recoverPending"):  # v6.53
        L.append("Cut-off recording waiting to be recovered: " + ", ".join(doc["recoverPending"]))
    for x in doc.get("recovered") or []:
        L.append(f"Recovered after restart: {x}")
    rr = doc.get("recentRecordings") or []
    if rr:
        L.append("Recent files in TwitchRecordings:")
        for f in rr:
            L.append(f"  {f['name']} — {_fmt_mb(f['bytes'])}, {f.get('modifiedAt')}")
    return "\n".join(L) + "\n"


def _status_atomic_write(dest, data):
    """Temp named like atomic_copy (.{name}.partial-{hex}) so the v6.39 sweep can clean leftovers."""
    dest = Path(dest)
    part = dest.parent / f".{dest.name}.partial-{uuid.uuid4().hex[:6]}"
    try:
        with open(part, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(part, dest)
    finally:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass


def write_status_file(reason="tick"):
    """Build status once → always mirror under REC_DIR/Recorder status/ → try Drive.

    Returns True only when the Drive write succeeded (callers that care about Drive).
    Local fields update even when Drive is blocked (FDA / missing Drive).
    STATUS_FILE.error stays the Drive error so UI / ready check see the block.
    """
    now = time.time()
    with STATUS_FILE_LOCK:
        STATUS_FILE["lastAttemptAt"] = now
    doc = build_status_doc(now)
    doc["reason"] = reason
    local_folder, local_txt, local_js = status_local_paths()
    doc["statusLocalPath"] = str(local_txt)
    with STATUS_FILE_LOCK:
        doc["driveStatusError"] = STATUS_FILE.get("error")
    doc["driveAccess"] = drive_access_snapshot()

    # ── local mirror (no FDA needed) ──
    try:
        local_folder.mkdir(parents=True, exist_ok=True)
        _status_atomic_write(local_txt, render_status_text(doc).encode("utf-8"))
        _status_atomic_write(local_js, (json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
        with STATUS_FILE_LOCK:
            STATUS_FILE.update(localLastWrittenAt=now, localTxtPath=str(local_txt), localError=None)
    except Exception as e:
        local_err = f"could not write local status mirror: {e}"
        with STATUS_FILE_LOCK:
            if local_err != STATUS_FILE.get("localError"):
                log(f"status file local ({reason}): {local_err}")
            STATUS_FILE["localError"] = local_err

    # ── Drive write (same folder layout as before) ──
    err = None
    try:
        roots = detect_drive_roots()
        if not roots:
            err = "Google Drive for Desktop not found"
        else:
            target, _kind, terr = resolve_delivery_target(roots, create=True)
            if terr or not target:
                err = terr or "Drive target unavailable"
            else:
                folder = Path(target) / STATUS_SUBDIR
                folder.mkdir(parents=True, exist_ok=True)
                name = computer_name()
                txt = folder / f"{name}.txt"
                js = folder / f"{name}.json"
                doc["driveStatusError"] = None
                doc["driveAccess"] = {"ok": True, "error": None, "kind": "ok"}
                _status_atomic_write(txt, render_status_text(doc).encode("utf-8"))
                _status_atomic_write(js, (json.dumps(doc, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
                with STATUS_FILE_LOCK:
                    STATUS_FILE.update(lastWrittenAt=now, folder=str(folder), txtPath=str(txt),
                                       jsonPath=str(js), error=None, writes=int(STATUS_FILE["writes"]) + 1)
                return True
    except PermissionError:
        err = drive_permission_message()
    except OSError as e:
        err = f"could not write status file: {e}"
    except Exception as e:
        err = f"status file error: {e}"
    with STATUS_FILE_LOCK:
        if err != STATUS_FILE.get("error"):
            log(f"status file ({reason}): {err}")
        STATUS_FILE["error"] = err
    return False



def status_file_loop():
    """Write on start, on meaningful change (min gap), and at least every STATUS_FILE_INTERVAL_SECS."""
    first = True
    while True:
        try:
            if status_file_enabled():
                now = time.time()
                sig = _status_change_sig()
                with STATUS_FILE_LOCK:
                    last_ok = float(STATUS_FILE["lastWrittenAt"] or 0.0)
                    last_try = float(STATUS_FILE["lastAttemptAt"] or 0.0)
                    prev_sig = STATUS_FILE.get("sig")
                    had_err = bool(STATUS_FILE.get("error"))
                changed = sig != prev_sig
                if first:
                    go = True
                elif had_err:
                    # after a failure (e.g. Drive missing / macOS Full Disk Access), retry on the interval
                    go = now - last_try >= STATUS_FILE_INTERVAL_SECS
                else:
                    go = (now - last_ok >= STATUS_FILE_INTERVAL_SECS
                          or (changed and now - last_ok >= STATUS_FILE_MIN_GAP_SECS))
                if go:
                    reason = "helper started" if first else ("change" if changed else "heartbeat")
                    write_status_file(reason)
                    with STATUS_FILE_LOCK:
                        STATUS_FILE["sig"] = sig
                    first = False
        except Exception as e:
            log(f"status file loop error: {e}")
        STATUS_FILE_WAKE.wait(STATUS_FILE_TICK_SECS)
        STATUS_FILE_WAKE.clear()


def status_file_snapshot():
    with STATUS_FILE_LOCK:
        last = STATUS_FILE["lastWrittenAt"] or None
        err = STATUS_FILE.get("error")
        local_last = STATUS_FILE.get("localLastWrittenAt") or None
        return {
            "enabled": status_file_enabled(),
            "intervalSecs": STATUS_FILE_INTERVAL_SECS,
            "lastWrittenAt": last,
            "folder": STATUS_FILE.get("folder"),
            "txtPath": STATUS_FILE.get("txtPath"),
            "error": err,
            "computer": computer_name(),
            # v6.47: local REC_DIR mirror (always when statusToDrive on)
            "localLastWrittenAt": local_last or None,
            "localTxtPath": STATUS_FILE.get("localTxtPath"),
            "localError": STATUS_FILE.get("localError"),
            "driveOk": bool(last) and not err,
        }


def reaper_loop():
    while True:
        time.sleep(1.5)
        with LOCK:
            reap_locked()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log(f"{self.address_string()} {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file_download(self, filepath):
        """Stream a recording from REC_DIR as Content-Disposition: attachment."""
        try:
            size = filepath.stat().st_size
            fh = open(filepath, "rb")
        except OSError:
            self.send_json({"ok": False, "error": "not found"}, status=404)
            return
        ctype = DOWNLOAD_TYPES.get(filepath.suffix.lower(), "application/octet-stream")
        # Keep filename ASCII-safe for Content-Disposition; recordings use [a-z0-9_-]+ stamps.
        fname = filepath.name.replace('"', "")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.end_headers()
        try:
            shutil.copyfileobj(fh, self.wfile, length=256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            fh.close()

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html", "/twitch-auto-recorder.html"):
            html_path = HERE / HTML_NAME
            if not html_path.is_file():
                msg = (
                    f"404: {HTML_NAME} not found next to twitch-recorder-server.py "
                    f"(looked in {HERE}). Copy the HTML file next to the helper and retry."
                )
                self.send_text(msg, status=404)
                return
            try:
                data = html_path.read_bytes()
            except OSError as e:
                self.send_text(f"Could not read {HTML_NAME}: {e}", status=500)
                return
            self.send_text(data, status=200, content_type="text/html; charset=utf-8")
            return
        if path == "/api/health":
            # v6.40: ?armed=a,b — page heartbeat of Auto-Rec streamers (keep-awake while armed)
            # v6.49: ?soloWatch=0|1 — Settings toggle; persisted so it works with the page closed
            try:
                q = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)
                if "armed" in q:
                    note_armed_heartbeat(q.get("armed", [""])[0])
                    note_page_seen(q.get("armed", [""])[0])  # v6.41 status file
                if "soloWatch" in q:
                    raw_sw = (q.get("soloWatch") or ["1"])[0]
                    on = str(raw_sw).strip().lower() not in ("0", "false", "off", "no")
                    solo_watch_set_enabled(on, save=True)
            except Exception as e:
                log(f"armed heartbeat parse failed: {e}")
            self.send_json(health_payload())
            return
        if path == "/api/update/check":
            res = check_update()
            note_update_check(res, "page")  # v6.43
            res["autoUpdate"] = auto_update_snapshot()
            self.send_json(res)
            return
        if path == "/api/update/auto":  # v6.43
            self.send_json({"ok": True, "autoUpdate": auto_update_snapshot()})
            return
        if path == "/api/streamlink/update":  # v6.55
            self.send_json({"ok": True, "streamlinkUpdate": streamlink_update_snapshot()})
            return
        if path == "/api/status":
            self.send_json(status_payload())
            return
        if path == "/api/files":
            self.send_json(files_payload())
            return
        if path == "/api/pipeline/status":
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            self.send_json(pipeline_status_payload())
            return
        if path.startswith("/api/drive/"):
            # v6.35: Drive endpoints expose private file names — local pages only.
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if path == "/api/drive/status":
                self.send_json(drive_status_payload())
                return
            if path == "/api/drive/list":
                payload, status = drive_list_payload(
                    (qs.get("path") or [""])[0], (qs.get("outMode") or ["subfolder"])[0]
                )
                self.send_json(payload, status=status)
                return
            if path == "/api/drive/history":
                self.send_json(drive_history_payload())
                return
        if path.startswith("/api/music-only/"):
            job_id = path[len("/api/music-only/") :]
            job_id = urllib.parse.unquote(job_id)
            payload, status = music_only_status(job_id)
            self.send_json(payload, status=status)
            return
        if path.startswith("/api/seamless/"):
            job_id = path[len("/api/seamless/") :]
            job_id = urllib.parse.unquote(job_id)
            payload, status = seamless_status(job_id)
            self.send_json(payload, status=status)
            return
        if path.startswith("/api/download/"):
            raw = path[len("/api/download/") :]
            name = urllib.parse.unquote(raw)
            target = resolve_download_path(name)
            if not target:
                self.send_json({"ok": False, "error": "not found"}, status=404)
                return
            # Fixed Content-Length would truncate a still-growing file — refuse clearly.
            abspath = os.path.abspath(str(target))
            with LOCK:
                reap_locked()
                busy = any(
                    (rec.get("file") or "")
                    and os.path.abspath(rec.get("file") or "") == abspath
                    for rec in ACTIVE.values()
                )
            if busy:
                self.send_json(
                    {
                        "ok": False,
                        "error": "still recording — stop first or wait for this segment to finish",
                    },
                    status=409,
                )
                return
            self.send_file_download(target)
            return
        self.send_json({"ok": False, "error": f"not found: {path}"}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/api/update":
            result = apply_update(source="page")
            self.send_json(result, status=200 if result.get("ok") else 400)
            return
        if path == "/api/update/auto":  # v6.43: {enabled?, checkNow?}
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            payload, status = api_update_auto(self.read_json())
            self.send_json(payload, status=status)
            return
        if path == "/api/streamlink/update":  # v6.55: {enabled?, checkNow?}
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            payload, status = api_streamlink_update(self.read_json())
            self.send_json(payload, status=status)
            return
        if path == "/api/keep-awake/wake":  # v6.58: the --wake task after a scheduled wake
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            self.read_json()
            self.send_json(note_woken("task"))
            return
        if path in ("/api/drive/access/check", "/api/drive/access/open-settings"):  # v6.56
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            self.read_json()
            if path.endswith("/check"):
                self.send_json(drive_access_recheck("page"))
            else:
                self.send_json(open_fda_settings())
            return
        if path == "/api/restart":
            body = self.read_json()
            force = bool((body or {}).get("force")) if isinstance(body, dict) else False
            result, status = restart_helper(force=force)
            self.send_json(result, status=status)
            return
        if path == "/api/record":
            body = self.read_json()
            username = safe_username(body.get("username"))
            if not username:
                self.send_json({"ok": False, "error": "invalid username"}, status=400)
                return
            quality = body.get("quality") or "audio_only"
            if "afterStream" in body:  # v6.36: per-streamer "remove voice → Drive" pref
                set_pipeline_user_pref(username, bool(body.get("afterStream")))
            if "sendOriginal" in body:  # v6.38: per-streamer "send original to Drive" pref
                set_original_user_pref(username, bool(body.get("sendOriginal")))
            # v6.49: remember start options so solo-watch can reuse them with the page closed
            try:
                solo_watch_remember_options(
                    username,
                    quality=quality if quality in ("audio_only", "best") else "audio_only",
                    after_stream=body.get("afterStream") if "afterStream" in body else None,
                    send_original=body.get("sendOriginal") if "sendOriginal" in body else None,
                )
            except Exception as e:
                log(f"solo watch remember options failed: {e}")
            result = start_record(username, quality, started_by="page")
            self.send_json(result, status=200 if result.get("ok") else 400)
            return
        if path == "/api/stop":
            body = self.read_json()
            username = safe_username(body.get("username"))
            if not username:
                self.send_json({"ok": False, "error": "invalid username"}, status=400)
                return
            with LOCK:
                reap_locked()
                result = stop_one_locked(username)
            self.send_json(result, status=200 if result.get("ok") else 400)
            return
        if path == "/api/grace/release":  # v6.48: {username, reason?}
            body = self.read_json()
            body = body if isinstance(body, dict) else {}
            payload, status = release_restart_grace(body.get("username"), body.get("reason"))
            self.send_json(payload, status=status)
            return
        if path == "/api/stop-all":
            with LOCK:
                results = stop_all_locked()
            self.send_json({"ok": True, "stopped": results})
            return
        if path == "/api/errors/clear":
            body = self.read_json()
            raw_user = (body or {}).get("username") if isinstance(body, dict) else None
            if raw_user:
                username = safe_username(raw_user)
                if not username:
                    self.send_json({"ok": False, "error": "invalid username"}, status=400)
                    return
                with LOCK:
                    result = clear_errors(username)
            else:
                with LOCK:
                    result = clear_errors(None)
            self.send_json(result)
            return
        if path == "/api/delete":
            body = self.read_json()
            payload, status = api_delete_recordings(body if isinstance(body, dict) else {})
            self.send_json(payload, status=status)
            return
        if path in ("/api/pipeline/prefs", "/api/pipeline/split", "/api/pipeline/deliver", "/api/pipeline/original"):
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            body = self.read_json()
            if path == "/api/pipeline/prefs":
                payload, status = api_pipeline_prefs(body)
            elif path == "/api/pipeline/split":
                payload, status = api_pipeline_split(body)
            elif path == "/api/pipeline/original":
                payload, status = api_pipeline_original(body)
            else:
                payload, status = api_pipeline_deliver(body)
            self.send_json(payload, status=status)
            return
        if path in ("/api/drive/music", "/api/drive/fetch", "/api/reveal"):
            if not origin_allowed(self.headers.get("Origin")):
                self.send_json({"ok": False, "error": "origin not allowed"}, status=403)
                return
            body = self.read_json()
            if path == "/api/drive/music":
                payload, status = start_drive_music(body)
            elif path == "/api/drive/fetch":
                payload, status = start_drive_fetch(body)
            else:
                payload, status = api_reveal(body)
            self.send_json(payload, status=status)
            return
        if path == "/api/music-only/cancel":
            body = self.read_json()
            payload, status = cancel_music_only(body if isinstance(body, dict) else {})
            self.send_json(payload, status=status)
            return
        if path == "/api/music-only":
            body = self.read_json()
            name = (body or {}).get("name") if isinstance(body, dict) else None
            redo = bool((body or {}).get("redo")) if isinstance(body, dict) else False
            quality = (body or {}).get("quality") if isinstance(body, dict) else None  # v6.45
            payload, status = start_music_only(name, redo=redo, quality=quality)
            self.send_json(payload, status=status)
            return
        if path == "/api/seamless":
            body = self.read_json()
            payload, status = start_seamless(body)
            self.send_json(payload, status=status)
            return
        if path == "/api/music-only-upload":
            up_quality = (urllib.parse.parse_qs(parsed.query or "").get("quality") or [None])[0]  # v6.45
            with AUTO_UPDATE_LOCK:
                UPLOADS_INFLIGHT[0] += 1  # v6.43: don't auto-restart mid-upload
            try:
                parsed, err = parse_multipart_file(self)
            finally:
                with AUTO_UPDATE_LOCK:
                    UPLOADS_INFLIGHT[0] -= 1
            if err:
                self.send_json({"ok": False, "error": err}, status=400)
                return
            try:
                dest = save_music_upload(parsed["filename"], parsed["data"])
            except ValueError as e:
                self.send_json({"ok": False, "error": str(e)}, status=400)
                return
            except OSError as e:
                self.send_json({"ok": False, "error": f"save failed: {e}"}, status=500)
                return
            if not resolve_voice_quality(up_quality)[0]:
                self.send_json(
                    {
                        "ok": False,
                        "error": resolve_voice_quality(up_quality)[1],
                        "demucs": demucs_available(),
                        "savedName": dest.name,
                        "savedAs": dest.name,
                        "path": str(dest),
                    },
                    status=400,
                )
                return
            # v6.30: refuse Demucs start on diskBlock (file already saved above)
            disk_err = disk_block_error()
            if disk_err:
                free, _total = disk_usage_for_rec_dir()
                mb = (free or 0) / (1024 * 1024)
                self.send_json(
                    {
                        "ok": False,
                        "error": (
                            f"disk almost full ({mb:.0f} MB free) — "
                            f"free space before Music only / Demucs"
                        ),
                        "diskBlock": True,
                        "savedName": dest.name,
                        "savedAs": dest.name,
                        "path": str(dest),
                    },
                    status=507,
                )
                return
            payload, status = start_music_only(dest.name, redo=False, quality=up_quality)
            if payload.get("ok"):
                payload = dict(payload)
                payload["savedName"] = dest.name
                payload["savedAs"] = dest.name
                payload["path"] = str(dest)
                payload["name"] = dest.name
                payload["uploaded"] = True
            else:
                # Still expose where the upload landed even if demucs start failed
                payload = dict(payload) if isinstance(payload, dict) else {"ok": False}
                payload["savedName"] = dest.name
                payload["savedAs"] = dest.name
                payload["path"] = str(dest)
            self.send_json(payload, status=status)
            return
        self.send_json({"ok": False, "error": f"not found: {path}"}, status=404)


# ─── v6.56: macOS Full Disk Access for Google Drive (loud failure + auto backlog retry) ──────────
# Why there is no silent workaround: Google Drive for Desktop on current macOS keeps BOTH stream and
# mirror mode under ~/Library/CloudStorage (a File Provider folder). macOS only lets a background
# process such as the LaunchAgent helper's python write there with Full Disk Access; copying via
# Finder / AppleScript needs a different privacy approval (Automation) that a background helper
# can't ask for either. So the helper makes the block loud, shows the exact path to add, re-checks
# on its own once a minute and sends the backlog as soon as the block is lifted.
FDA_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
DRIVE_ACCESS_PROBE_SECS = max(15, int(os.environ.get("TWITCH_RECORDER_DRIVE_PROBE_SECS") or 60))
DRIVE_ACCESS_LOCK = threading.Lock()
DRIVE_ACCESS = {"lastProbeAt": 0.0, "lastProbeOk": None, "lastProbeError": None, "lastProbeKind": None,
                "blockedSince": 0.0, "restoredAt": 0.0, "lastKick": None}


def fda_paths(executable=None, base_prefix=None, exists=None):
    """Paths to add under Full Disk Access (pure-ish, unit-testable).

    First the real python binary (e.g. …/Python3.framework/Versions/3.9/bin/python3.9); for a
    framework Python also its Python.app, because that stub re-launches Python.app and macOS may
    check either one."""
    exists = exists or os.path.exists
    exe = os.path.realpath(executable or sys.executable or "python3")
    out = [exe]
    if executable is None:  # v6.60: the binary launchd really runs (plist ProgramArguments[0]) first
        try:
            lp = launcher_snapshot()
            if lp.get("shim") and lp.get("realInterpreter"):
                out = [lp["realInterpreter"]] + [x for x in out if x != lp["realInterpreter"]]
            elif lp.get("plistProgram"):
                rp = os.path.realpath(lp["plistProgram"])
                out = [rp] + [x for x in out if x != rp]
        except Exception:
            pass
    pre = base_prefix if base_prefix is not None else getattr(sys, "base_prefix", "") or ""
    if pre:
        app = os.path.join(pre, "Resources", "Python.app")
        if exists(app) and app not in out:
            out.append(app)
    return out


# ─── v6.60: macOS LaunchAgent must run the REAL python, never the /usr/bin/python3 shim ──────────
# /usr/bin/python3 is Apple's xcode-select shim; it execs CommandLineTools' python3.9, but macOS TCC
# attributes the launchd job to the shim, so Full Disk Access granted to python3.9 / Python.app does
# not apply and Google Drive writes fail with EPERM. The helper repairs its own plist on startup.
LAUNCH_AGENT_LABEL = "com.ewanders.twitch-recorder"
LAUNCHER_FIX_ENV_OFF = (os.environ.get("TWITCH_RECORDER_FIX_LAUNCHER") or "").strip().lower() in (
    "0", "false", "no", "off")
LAUNCHER_STATE = {"plistPath": None, "plistProgram": None, "realInterpreter": None,
                  "shim": False, "repaired": False, "kickScheduled": False, "error": None}
LAUNCHER_LOCK = threading.Lock()


def is_python_shim(path):
    """True for /usr/bin/python3 (xcode-select tool shim) or anything else under /usr/bin."""
    if not path:
        return False
    p = os.path.normpath(str(path))
    if p.startswith("/usr/bin/"):
        return True
    try:
        rp = os.path.realpath(p)
    except OSError:
        rp = p
    return rp.startswith("/usr/bin/") or "xcode_select" in os.path.basename(rp)


def real_interpreter(executable=None, base_executable=None, exists=None):
    """The real python binary to put in a LaunchAgent (never a shim); None if unknown."""
    exists = exists or os.path.exists
    cands = []
    for c in (base_executable if base_executable is not None else getattr(sys, "_base_executable", None),
              executable if executable is not None else sys.executable):
        if c:
            cands.append(os.path.realpath(c))
    if base_executable is None and executable is None:
        pre = getattr(sys, "base_prefix", "") or ""
        if pre:
            v = f"{sys.version_info[0]}.{sys.version_info[1]}"
            cands.append(os.path.join(pre, "bin", f"python{v}"))
    for c in cands:
        if c and not is_python_shim(c) and exists(c):
            return c
    return None


def find_launch_agent_plist(home=None):
    d = Path(home or Path.home()) / "Library" / "LaunchAgents"
    p = d / f"{LAUNCH_AGENT_LABEL}.plist"
    if p.exists():
        return p
    try:
        import plistlib
        for f in sorted(d.glob("*.plist")):
            try:
                with open(f, "rb") as fh:
                    doc = plistlib.load(fh)
                if any("twitch-recorder-server" in str(a) for a in (doc.get("ProgramArguments") or [])):
                    return f
            except Exception:
                continue
    except OSError:
        pass
    return None


def repair_launch_agent_plist(plist_path, real=None, log_fn=None):
    """Rewrite ProgramArguments[0] if it is a shim. Returns dict(plistProgram, realInterpreter, shim, repaired, label)."""
    import plistlib
    plist_path = Path(plist_path)
    with open(plist_path, "rb") as fh:
        doc = plistlib.load(fh)
    args = list(doc.get("ProgramArguments") or [])
    prog = args[0] if args else None
    real = real or real_interpreter()
    out = {"plistProgram": prog, "realInterpreter": real, "shim": is_python_shim(prog),
           "repaired": False, "label": doc.get("Label") or LAUNCH_AGENT_LABEL}
    if out["shim"] and real and not is_python_shim(real):
        bak = plist_path.with_name(plist_path.name + ".bak")
        if not bak.exists():
            shutil.copy2(plist_path, bak)
        args[0] = real
        doc["ProgramArguments"] = args
        tmp = plist_path.with_name(f".{plist_path.name}.tmp-{uuid.uuid4().hex[:6]}")
        with open(tmp, "wb") as fh:
            plistlib.dump(doc, fh)
        os.replace(tmp, plist_path)
        out.update(repaired=True, plistProgram=real, shim=False, previous=prog)
        if log_fn:
            log_fn(f"launcher: LaunchAgent ran {prog} (xcode-select shim — macOS Full Disk Access "
                   f"can't apply); now runs {real} (backup {bak.name}); takes effect on next launch")
    return out


def launcher_snapshot():
    with LAUNCHER_LOCK:
        d = dict(LAUNCHER_STATE)
    return {k: d.get(k) for k in ("plistProgram", "realInterpreter", "shim", "repaired")}


def launcher_startup_repair():
    """macOS: fix the LaunchAgent once at startup; relaunch via launchctl if Drive is FDA-blocked and idle."""
    if sys.platform != "darwin":
        return
    try:
        pl = find_launch_agent_plist()
        if not pl:
            with LAUNCHER_LOCK:
                LAUNCHER_STATE.update(realInterpreter=real_interpreter())
            return
        if LAUNCHER_FIX_ENV_OFF:
            import plistlib
            with open(pl, "rb") as fh:
                prog = (plistlib.load(fh).get("ProgramArguments") or [None])[0]
            with LAUNCHER_LOCK:
                LAUNCHER_STATE.update(plistPath=str(pl), plistProgram=prog,
                                      realInterpreter=real_interpreter(), shim=is_python_shim(prog))
            return
        r = repair_launch_agent_plist(pl, log_fn=log)
        with LAUNCHER_LOCK:
            LAUNCHER_STATE.update(plistPath=str(pl), **{k: r[k] for k in
                                  ("plistProgram", "realInterpreter", "shim", "repaired")})
        if r["repaired"]:
            threading.Thread(target=_launcher_kick_loop, args=(r["label"],), daemon=True,
                             name="launcher-kick").start()
    except Exception as e:
        with LAUNCHER_LOCK:
            LAUNCHER_STATE["error"] = str(e)
        log(f"launcher repair failed: {e}")


def _launcher_kick_loop(label):
    """After a repair: once Drive is FDA-blocked and the helper is idle, respawn via launchd (fresh TCC)."""
    time.sleep(90)  # let the first Drive probe run
    for _ in range(24 * 60):
        try:
            if _drive_access_snapshot_base().get("kind") != "fda":
                return  # Drive fine → next natural relaunch picks the fix up
            if not update_busy_reason():
                with LAUNCHER_LOCK:
                    LAUNCHER_STATE["kickScheduled"] = True
                log(f"launcher: Drive blocked by Full Disk Access and helper idle — relaunching via launchd")
                subprocess.Popen(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
        except Exception as e:
            log(f"launcher kick: {e}")
        time.sleep(60)


def _self_test_v661():
    """v6.61: a failed demucs chunk is retried; a failed after-stream job waits and tries again,
    keeps its retry count in pendingJobs, gives up after the limit, and stops at once on errors a
    retry can't fix. Temp recordings dir; no real demucs / ffmpeg runs."""
    global REC_DIR, PIPELINE_FILE, PIPELINE_RETRY_DELAYS, _pipeline_attempt, _job_run_proc
    global which_ffmpeg, demucs_cmd_base, _demucs_devices, DEMUCS_CHUNK_RETRY_PAUSE_SECS
    import tempfile as _tmp661
    assert _parse_retry_delays("") == (600.0, 3600.0)
    assert _parse_retry_delays("5, 30") == (5.0, 30.0)
    assert _parse_retry_delays("x") == (600.0, 3600.0)
    assert _pipeline_error_is_permanent("no finished segments on disk")
    assert _pipeline_error_is_permanent("Voice removal could not save its audio file (torchaudio has no audio backend …)")
    assert not _pipeline_error_is_permanent("demucs (chunk 3/24) failed: Killed: 9")
    prev = (REC_DIR, PIPELINE_FILE, PIPELINE_RETRY_DELAYS, _pipeline_attempt, _job_run_proc,
            which_ffmpeg, demucs_cmd_base, _demucs_devices, DEMUCS_CHUNK_RETRY_PAUSE_SECS)
    with PIPELINE_LOCK:
        prev_pj = dict(PIPELINE.get("pendingJobs") or {})
        prev_stats = dict(PIPELINE_RETRY_STATS)
    try:
        with _tmp661.TemporaryDirectory(prefix="tar-661-") as td:
            REC_DIR = Path(td)
            PIPELINE_FILE = REC_DIR / ".drive-pipeline.json"
            PIPELINE_RETRY_DELAYS = (0.0, 0.0)
            DEMUCS_CHUNK_RETRY_PAUSE_SECS = 0.0
            # (1) chunk retry: first demucs run dies, the retry on cpu works
            calls = {"demucs": 0}

            def fake_proc(job_id, cmd, line_cb=None, env=None, timeout=None):
                if "segment" in cmd:
                    out = Path(cmd[-1].replace("%04d", "0000"))
                    out.write_bytes(b"f")
                    return 0, ""
                if "--two-stems=vocals" in cmd:
                    calls["demucs"] += 1
                    if calls["demucs"] == 1:
                        return -9, "Killed: 9"
                    o = Path(cmd[cmd.index("-o") + 1]) / "htdemucs"
                    o.mkdir(parents=True, exist_ok=True)
                    (o / "no_vocals.wav").write_bytes(b"w")
                    return 0, ""
                Path(cmd[-1]).write_bytes(b"c")
                return 0, ""
            _job_run_proc = fake_proc
            which_ffmpeg = lambda: "ffmpeg"
            demucs_cmd_base = lambda: ["demucs"]
            _demucs_devices = lambda: ["cpu"]
            jid = uuid.uuid4().hex[:12]
            with MUSIC_JOBS_LOCK:
                MUSIC_JOBS[jid] = {"jobId": jid, "status": "running"}
            work = REC_DIR / "work"
            work.mkdir()
            src = REC_DIR / "x.ogg"
            src.write_bytes(b"x")
            outs = chunked_demucs_no_vocals(jid, src, work, 60, lambda *a, **k: None)
            assert len(outs) == 1 and calls["demucs"] == 2, (outs, calls)
            # (2) job retry: fail, then succeed
            seq = {"n": 0}
            first = "selftest661-Fri-2026-10-30T19-00-00.ogg"

            def attempt(job_id, plan):
                seq["n"] += 1
                if plan[seq["n"] - 1]:
                    raise RuntimeError(plan[seq["n"] - 1])
                _set_music_job(job_id, status="done", phase="done", progress="Sent to Drive (1 part)")

            def run(plan):
                seq["n"] = 0
                j = uuid.uuid4().hex[:12]
                with MUSIC_JOBS_LOCK:
                    MUSIC_JOBS[j] = {"jobId": j, "status": "queued", "segments": [first], "name": first}
                with PIPELINE_LOCK:
                    PIPELINE.setdefault("pendingJobs", {})[first] = {"segments": [first], "attempts": 1,
                                                                    "queuedAt": time.time()}
                globals()["_pipeline_attempt"] = lambda job_id: attempt(job_id, plan)
                pipeline_worker(j)
                with MUSIC_JOBS_LOCK:
                    job = dict(MUSIC_JOBS.pop(j))
                with PIPELINE_LOCK:
                    left = first in (PIPELINE.get("pendingJobs") or {})
                return job, left
            job, left = run(["demucs (chunk 2/24) failed: Killed: 9", None])
            assert job["status"] == "done" and seq["n"] == 2 and job["autoRetries"] == 1 and not left, job
            job, left = run(["boom", "boom", "boom", None])
            assert job["status"] == "error" and seq["n"] == 3 and "failed 3 times" in job["error"] and not left, job
            job, left = run(["ffmpeg not found", None])
            assert job["status"] == "error" and seq["n"] == 1, job
            # pendingJobs keeps the retry count across a re-put (helper restart → resume)
            with PIPELINE_LOCK:
                PIPELINE.setdefault("pendingJobs", {})[first] = {"segments": [first], "attempts": 1,
                                                                "errorRetries": 1, "musicMade": "m.ogg"}
            _pending_job_put(first, "selftest661", [first], False, "resumed after helper restart")
            with PIPELINE_LOCK:
                ent = dict(PIPELINE["pendingJobs"][first])
            assert ent["errorRetries"] == 1 and ent["musicMade"] == "m.ogg" and ent["attempts"] == 2, ent
            snap = _pipeline_retry_snapshot()
            assert snap["maxRetries"] == PIPELINE_RETRIES and snap["scheduled"] >= 3 and snap["gaveUp"] >= 1, snap
            with MUSIC_JOBS_LOCK:
                MUSIC_JOBS.pop(jid, None)
    finally:
        (REC_DIR, PIPELINE_FILE, PIPELINE_RETRY_DELAYS, _pipeline_attempt, _job_run_proc,
         which_ffmpeg, demucs_cmd_base, _demucs_devices, DEMUCS_CHUNK_RETRY_PAUSE_SECS) = prev
        with PIPELINE_LOCK:
            PIPELINE["pendingJobs"] = prev_pj
            PIPELINE_RETRY_STATS.clear(); PIPELINE_RETRY_STATS.update(prev_stats)


def _self_test_v660():
    """v6.60: shim detection + plist rewrite on a temp plist."""
    import plistlib
    import tempfile
    assert is_python_shim("/usr/bin/python3")
    assert not is_python_shim("/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3.9")
    assert not is_python_shim(None)
    real = "/opt/fake/Versions/3.9/bin/python3.9"
    assert real_interpreter(executable="/usr/bin/python3", base_executable=real,
                            exists=lambda p: True) == real
    assert real_interpreter(executable="/usr/bin/python3", base_executable="/usr/bin/python3",
                            exists=lambda p: True) is None
    with tempfile.TemporaryDirectory() as td:
        pl = Path(td) / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
        pl.parent.mkdir(parents=True)
        doc = {"Label": LAUNCH_AGENT_LABEL, "RunAtLoad": True, "KeepAlive": True,
               "ProgramArguments": ["/usr/bin/python3", "/x/twitch-recorder-server.py"],
               "EnvironmentVariables": {"A": "1"}}
        with open(pl, "wb") as fh:
            plistlib.dump(doc, fh)
        assert find_launch_agent_plist(home=td) == pl
        r = repair_launch_agent_plist(pl, real=real)
        assert r["repaired"] and r["plistProgram"] == real and not r["shim"], r
        with open(pl, "rb") as fh:
            nd = plistlib.load(fh)
        assert nd["ProgramArguments"] == [real, "/x/twitch-recorder-server.py"], nd
        assert nd["RunAtLoad"] is True and nd["KeepAlive"] is True and nd["EnvironmentVariables"] == {"A": "1"}
        bak = pl.with_name(pl.name + ".bak")
        with open(bak, "rb") as fh:
            assert plistlib.load(fh)["ProgramArguments"][0] == "/usr/bin/python3"
        r2 = repair_launch_agent_plist(pl, real=real)
        assert not r2["repaired"] and not r2["shim"], r2
        with open(bak, "rb") as fh:  # backup written once
            assert plistlib.load(fh)["ProgramArguments"][0] == "/usr/bin/python3"
    with LAUNCHER_LOCK:
        saved = dict(LAUNCHER_STATE)
        LAUNCHER_STATE.update(plistProgram="/usr/bin/python3", realInterpreter=real, shim=True)
    try:
        msg = drive_permission_message()
        if sys.platform == "darwin" or os.name != "nt":
            ps = fda_paths()
            assert ps[0] == real, ps
            assert "repair" in msg, msg
    finally:
        with LAUNCHER_LOCK:
            LAUNCHER_STATE.clear(); LAUNCHER_STATE.update(saved)


def drive_stuck_counts():
    with PIPELINE_LOCK:
        origs = list((PIPELINE.get("originals") or {}).values())
        dels = list(PIPELINE["deliveries"].values())
    return {"originals": sum(1 for d in origs if d.get("status") in ("error", "waiting")),
            "parts": sum(1 for d in dels if d.get("status") in ("error", "waiting"))}


def drive_access_probe():
    """Really try a write in the Drive target: (ok, error, kind). Leaves nothing behind."""
    roots = detect_drive_roots()
    if not roots:
        return False, "Google Drive for Desktop not found", "missing"
    try:
        target, _kind, terr = resolve_delivery_target(roots, create=True)
        if terr or not target:
            msg = terr or "Drive target unavailable"
            return False, msg, _drive_access_kind(msg)
        probe = Path(target) / f".tar-access-check-{os.getpid()}"
        probe.write_bytes(b"ok")
        try:
            probe.unlink()
        except OSError:
            pass
        return True, None, "ok"
    except PermissionError:
        msg = drive_permission_message()
        return False, msg, _drive_access_kind(msg)
    except OSError as e:
        msg = str(e) or type(e).__name__
        if getattr(e, "errno", None) in (1, 13):  # EPERM / EACCES
            msg = drive_permission_message()
        return False, msg, _drive_access_kind(msg)


def _drive_blocked_now():
    """True when the last status write or any stuck original/part failed on a Drive permission block."""
    with STATUS_FILE_LOCK:
        err = STATUS_FILE.get("error")
    if err and _drive_access_kind(err) == "fda":
        return True
    with PIPELINE_LOCK:
        items = list((PIPELINE.get("originals") or {}).values()) + list(PIPELINE["deliveries"].values())
    return any(d.get("status") == "error" and _drive_access_kind(d.get("error")) == "fda" for d in items)


def drive_access_recheck(source="auto"):
    """Probe Drive now. When it works and something was stuck, clear the old error and send the
    backlog (place_in_drive skips files already in Drive with the same name+size → no duplicates)."""
    was_blocked = _drive_blocked_now()
    ok, err, kind = drive_access_probe()
    now = time.time()
    kicked = None
    with DRIVE_ACCESS_LOCK:
        prev_ok = DRIVE_ACCESS.get("lastProbeOk")
        DRIVE_ACCESS.update(lastProbeAt=now, lastProbeOk=ok, lastProbeError=err, lastProbeKind=kind)
        if not ok and (prev_ok is not False or not DRIVE_ACCESS.get("blockedSince")):
            DRIVE_ACCESS["blockedSince"] = now
    if ok:
        with STATUS_FILE_LOCK:
            had = STATUS_FILE.get("error")
            STATUS_FILE["error"] = None
            if had:
                STATUS_FILE["lastWrittenAt"] = 0.0  # status loop writes on its next pass
        stuck = drive_stuck_counts()
        if was_blocked or had or stuck["originals"] or stuck["parts"]:
            kicked = dict(stuck)
            with DRIVE_ACCESS_LOCK:
                if was_blocked or had:
                    DRIVE_ACCESS["restoredAt"] = now
                    DRIVE_ACCESS["blockedSince"] = 0.0
                DRIVE_ACCESS["lastKick"] = {"at": now, "source": source, **stuck}
            if was_blocked or had:
                log(f"drive access OK again ({source}) — retrying {stuck['originals']} original(s) and "
                    f"{stuck['parts']} part set(s) waiting for Drive")
            request_delivery(None)
    elif source != "auto":
        log(f"drive access check ({source}): still blocked — {err}")
    return {"ok": ok, "error": err, "kind": kind, "kicked": kicked, "access": drive_access_snapshot()}


def drive_access_loop():
    """Re-check once a minute while Drive is blocked so the backlog goes out minutes after access works."""
    time.sleep(20)
    while True:
        try:
            if _drive_blocked_now():
                with DRIVE_ACCESS_LOCK:
                    last = float(DRIVE_ACCESS.get("lastProbeAt") or 0.0)
                if time.time() - last >= DRIVE_ACCESS_PROBE_SECS:
                    drive_access_recheck("auto")
        except Exception as e:
            log(f"drive access loop: {e}")
        time.sleep(15)


def drive_access_extra(kind):
    """Extra fields for /api/health.driveAccess (banner: exact path, settings link, backlog)."""
    with DRIVE_ACCESS_LOCK:
        da = dict(DRIVE_ACCESS)
    out = {"platform": sys.platform, "lastProbeAt": da.get("lastProbeAt") or None,
           "blockedSince": da.get("blockedSince") or None, "restoredAt": da.get("restoredAt") or None,
           "lastKick": da.get("lastKick"), "stuck": drive_stuck_counts(),
           "probeEverySecs": DRIVE_ACCESS_PROBE_SECS}
    if kind == "fda" or sys.platform == "darwin":
        paths = fda_paths()
        out.update(fdaPath=paths[0], fdaPaths=paths, settingsUrl=FDA_SETTINGS_URL)
    if sys.platform == "darwin" or kind == "fda":
        out["launcher"] = launcher_snapshot()  # v6.60
    return out


def open_fda_settings(reveal=True):
    """macOS: open Privacy & Security → Full Disk Access and show the python to add in Finder."""
    if sys.platform != "darwin":
        return {"ok": False, "error": "Full Disk Access is a macOS setting"}
    opened = []
    try:
        subprocess.Popen(["open", FDA_SETTINGS_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opened.append("settings")
        if reveal:
            subprocess.Popen(["open", "-R", fda_paths()[0]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opened.append("finder")
    except OSError as e:
        return {"ok": False, "error": str(e), "opened": opened}
    return {"ok": True, "opened": opened, "fdaPaths": fda_paths(), "settingsUrl": FDA_SETTINGS_URL}


# ─── v6.56: demucs can save without a torchaudio audio backend ───────────────────────────────────
# demucs saves vocals.wav / no_vocals.wav with torchaudio.save. On the Mac torchaudio had no backend
# ("Couldn't find appropriate backend to handle uri …"), newer torchaudio wants TorchCodec. demucs now
# runs through a tiny launcher that keeps torchaudio.save but falls back to soundfile, then to the
# standard-library wave writer (16/24-bit / float WAV) — nothing extra to install.
DEMUCS_SAVE_SHIM = r'''
import sys
def _tar_patch():
    try:
        import torchaudio as _ta
    except Exception:
        return
    _orig = getattr(_ta, "save", None)
    _said = []
    def _wave_write(path, arr, sr, bits, encoding):
        import wave, numpy as np
        a = np.asarray(arr, dtype="float32")
        if a.ndim == 1:
            a = a[None, :]
        a = a.T  # frames x channels
        enc = str(encoding or "").upper()
        if bits == 24:
            v = np.ascontiguousarray((np.clip(a, -1.0, 1.0 - 1.0 / 8388608) * 8388608.0).astype("<i4"))
            raw = v.view("u1").reshape(-1, 4)[:, :3].tobytes()
            width = 3
        else:
            v = np.ascontiguousarray((np.clip(a, -1.0, 1.0 - 1.0 / 32768) * 32768.0).astype("<i2"))
            raw = v.tobytes()
            width = 2
        # (wave can't write float WAV; 16/24-bit PCM is plenty for the instrumental)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(a.shape[1]); w.setsampwidth(width); w.setframerate(int(sr)); w.writeframes(raw)
    def _save(uri, src, sample_rate, *a, **kw):
        err = None
        if _orig is not None:
            try:
                return _orig(uri, src, sample_rate, *a, **kw)
            except Exception as e:
                err = e
        arr = src.detach().cpu().float().numpy() if hasattr(src, "detach") else src
        bits = kw.get("bits_per_sample"); enc = kw.get("encoding")
        try:
            import soundfile as sf
            sub = "PCM_24" if bits == 24 else ("FLOAT" if "FLOAT" in str(enc or "").upper() else "PCM_16")
            sf.write(str(uri), arr.T if getattr(arr, "ndim", 1) == 2 else arr, int(sample_rate), subtype=sub)
            how = "soundfile"
        except Exception as e2:
            if not str(uri).lower().endswith(".wav"):
                raise err or e2
            _wave_write(uri, arr, sample_rate, bits, enc)
            how = "wave"
        if not _said:
            _said.append(1)
            sys.stderr.write("[twitch-recorder] torchaudio could not save (%s) - saved with %s instead\n"
                             % (str(err or "no torchaudio.save").splitlines()[0][:160], how))
    _ta.save = _save
_tar_patch()
from demucs.separate import main as _main
sys.argv = ["demucs"] + sys.argv[1:]
_main()
'''
_DEMUCS_PY_CACHE = {}


def demucs_python(demucs=None):
    """Interpreter that can import demucs (the demucs script's own Python, else this one), or None."""
    demucs = demucs if demucs is not None else which_demucs()
    if not demucs:
        return None
    if demucs == "python -m demucs":
        return sys.executable
    if demucs in _DEMUCS_PY_CACHE:
        return _DEMUCS_PY_CACHE[demucs]
    found = None
    for py in _sl_python_candidates(demucs):
        try:
            r = subprocess.run([py, "-c", "import demucs.separate"], capture_output=True, timeout=120,
                               stdin=subprocess.DEVNULL, **_no_window_kw())
            if r.returncode == 0:
                found = py
                break
        except (OSError, subprocess.TimeoutExpired):
            continue
    _DEMUCS_PY_CACHE[demucs] = found
    return found


def demucs_cmd_base():
    """[python, -c, shim] when demucs' Python is known (saves through a backend fallback), else the CLI."""
    d = which_demucs()
    if not d:
        raise RuntimeError("demucs not installed — pip install demucs (first run downloads models)")
    if os.environ.get("TWITCH_RECORDER_DEMUCS_SHIM", "1") != "0":
        py = demucs_python(d)
        if py:
            return [py, "-c", DEMUCS_SAVE_SHIM]
    return [sys.executable, "-m", "demucs"] if d == "python -m demucs" else [d]


def friendly_demucs_error(tail, where="demucs"):
    """Short, plain message for the page instead of a raw traceback tail."""
    t = str(tail or "").strip()
    low = t.lower()
    if "appropriate backend" in low or "torchcodec" in low or "no audio i/o backend" in low:
        py = demucs_python() or sys.executable
        return (f"Voice removal could not save its audio file (torchaudio has no audio backend on this computer). "
                f"Fix: {py} -m pip install soundfile — then Retry.")
    if "out of memory" in low or "mps backend out of memory" in low:
        return f"{where}: ran out of memory — close other apps and Retry (Settings → voice quality Fast uses less)."
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    last = lines[-1] if lines else "unknown error"
    return f"{where} failed: {last[-300:]}"


def _no_window_kw():
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


# ─── v6.56: Windows helper starts on login and is restarted if it stops ──────────────────────────
# The helper registers itself (no admin needed): a Startup-folder launcher + a per-user Scheduled
# Task every 5 minutes, both starting "<pythonw> twitch-recorder-server.py --watchdog". The watchdog
# (one at a time) checks http://127.0.0.1:8765/api/health every 30 s and starts the helper hidden
# when it has been unreachable twice in a row (backoff up to 5 min if it keeps crashing).
AUTOSTART_TASK = "TwitchRecorderHelperWatchdog"
AUTOSTART_STARTUP_NAME = "Twitch Recorder helper.vbs"
WATCHDOG_LOCK_PORT = int(os.environ.get("TWITCH_RECORDER_WATCHDOG_PORT") or (PORT + 1000))
WATCHDOG_CHECK_SECS = max(5, int(os.environ.get("TWITCH_RECORDER_WATCHDOG_SECS") or 30))
AUTOSTART_STATE = {"enabled": None, "startup": None, "task": None, "watchdog": None, "error": None, "checkedAt": 0.0}


def autostart_enabled():
    if os.environ.get("TWITCH_RECORDER_AUTOSTART", "1") == "0":
        return False
    with PIPELINE_LOCK:
        return PIPELINE.get("autostart", True) is not False


def windowless_python(exe=None):
    exe = exe or sys.executable or "python"
    p = Path(exe)
    if p.name.lower() == "python.exe":
        w = p.with_name("pythonw.exe")
        if w.is_file():
            return str(w)
    return str(exe)


def autostart_launcher_vbs(pyw, script):
    """Startup-folder VBScript: start the watchdog hidden (no console window)."""
    def q(s):
        return '""' + str(s).replace('"', '') + '""'
    return ('\' Twitch Auto-Recorder v6.56: start the helper watchdog at login (made by the helper)\r\n'
            'CreateObject("WScript.Shell").Run "' + q(pyw) + ' ' + q(script) + ' --watchdog", 0, False\r\n')


def autostart_task_cmd(pyw, script):
    tr = f'"{pyw}" "{script}" --watchdog'
    return ["schtasks", "/Create", "/F", "/TN", AUTOSTART_TASK, "/SC", "MINUTE", "/MO", "5", "/TR", tr]


def ensure_windows_autostart(startup_dir=None, run=None, script=None, pyw=None):
    """Idempotent. Returns AUTOSTART_STATE-like dict. Never raises."""
    st = {"enabled": autostart_enabled(), "startup": None, "task": None, "error": None, "checkedAt": time.time()}
    if os.name != "nt" and startup_dir is None:
        st["error"] = "Windows only"
        return st
    if not st["enabled"]:
        return st
    script = str(script or Path(__file__).resolve())
    pyw = pyw or windowless_python()
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60, **_no_window_kw()))
    try:
        sd = Path(startup_dir) if startup_dir else (Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
                                                     / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup")
        sd.mkdir(parents=True, exist_ok=True)
        f = sd / AUTOSTART_STARTUP_NAME
        body = autostart_launcher_vbs(pyw, script)
        if not f.is_file() or f.read_text(encoding="utf-8", errors="replace") != body:
            f.write_text(body, encoding="utf-8")
            log(f"autostart: Startup launcher written → {f}")
        st["startup"] = str(f)
    except Exception as e:
        st["error"] = f"Startup folder: {e}"
    try:
        r = run(autostart_task_cmd(pyw, script))
        st["task"] = AUTOSTART_TASK if getattr(r, "returncode", 1) == 0 else None
        if st["task"] is None:
            st["error"] = (st["error"] + "; " if st["error"] else "") + "Scheduled Task: " + (
                (getattr(r, "stderr", "") or getattr(r, "stdout", "") or "failed").strip()[:200])
    except Exception as e:
        st["error"] = (st["error"] + "; " if st["error"] else "") + f"Scheduled Task: {e}"
    return st


def _helper_alive(timeout=4):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def start_watchdog_detached():
    """Start a hidden watchdog next to a running helper (it exits at once if one is already running)."""
    if os.name != "nt" or not autostart_enabled():
        return False
    try:
        subprocess.Popen([windowless_python(), str(Path(__file__).resolve()), "--watchdog"],
                         cwd=str(INSTALL_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=0x00000008 | 0x00000200 | 0x08000000)  # DETACHED|NEW_GROUP|NO_WINDOW
        return True
    except Exception as e:
        log(f"autostart: could not start watchdog: {e}")
        return False


def watchdog_decide(fails, alive, restarts_recent):
    """(new_fails, start_now, sleep_secs). Two misses in a row → start; back off if it keeps dying."""
    if alive:
        return 0, False, WATCHDOG_CHECK_SECS
    fails += 1
    if fails < 2:
        return fails, False, WATCHDOG_CHECK_SECS
    backoff = min(300, WATCHDOG_CHECK_SECS * (2 ** max(0, restarts_recent - 1))) if restarts_recent else WATCHDOG_CHECK_SECS
    return 0, True, max(WATCHDOG_CHECK_SECS, backoff)


def watchdog_main(max_loops=None, alive_fn=None, start_fn=None, sleep_fn=None):
    """--watchdog: single instance (lock port), restarts the helper when it is unreachable."""
    import socket as _sock
    lock = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", WATCHDOG_LOCK_PORT))
        lock.listen(1)
    except OSError:
        return 0  # another watchdog is already running
    alive_fn = alive_fn or _helper_alive
    sleep_fn = sleep_fn or time.sleep
    logf = INSTALL_DIR / "helper.log"

    def _start():
        args = [windowless_python(), str(Path(__file__).resolve())]
        out = open(logf, "ab")
        try:
            subprocess.Popen(args, cwd=str(INSTALL_DIR), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                             close_fds=True, **({"creationflags": 0x00000008 | 0x00000200 | 0x08000000}
                                                if os.name == "nt" else {}))
        finally:
            out.close()
    log_starts = start_fn is None  # self-test passes its own start_fn: don't touch the real log
    start_fn = start_fn or _start
    fails, starts, loops = 0, [], 0
    try:
        sleep_fn(5)
        while max_loops is None or loops < max_loops:
            loops += 1
            now = time.time()
            starts = [t for t in starts if now - t < 1800]
            fails, go, wait = watchdog_decide(fails, alive_fn(), len(starts))
            if go:
                try:
                    start_fn()
                    starts.append(now)
                    try:
                        if not log_starts:
                            raise OSError("self-test")
                        with open(logf, "a", encoding="utf-8") as fh:
                            fh.write(f"[watchdog {time.strftime('%Y-%m-%d %H:%M:%S')}] helper unreachable — started it again\n")
                    except OSError:
                        pass
                except Exception:
                    pass
            sleep_fn(wait)
    finally:
        lock.close()
    return 0


def autostart_loop():
    """Windows: register login start + watchdog once at start and every 6 h (repairs a deleted entry)."""
    time.sleep(10)
    while True:
        try:
            st = ensure_windows_autostart()
            AUTOSTART_STATE.update(st)
            AUTOSTART_STATE["watchdog"] = start_watchdog_detached()
            if st.get("error") and st.get("enabled"):
                log(f"autostart: {st['error']}")
        except Exception as e:
            AUTOSTART_STATE["error"] = str(e)
        time.sleep(6 * 3600)


def autostart_snapshot():
    if os.name != "nt":
        return None
    return dict(AUTOSTART_STATE, enabled=autostart_enabled())


def _self_test_v656():
    """v6.56: FDA paths + Drive block → recheck → backlog sent once; demucs save fallback; Windows autostart."""
    import tempfile as _tmp
    global REC_DIR, DRIVE_PARTS_DIR, PIPELINE_FILE
    # Fix 1a: exact paths for Full Disk Access (CLT Python 3.9 layout)
    pre = "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9"
    ps = fda_paths(pre + "/bin/python3.9", pre, exists=lambda p: True)
    assert ps[0].endswith("/Versions/3.9/bin/python3.9") and ps[1].endswith("/Resources/Python.app"), ps
    assert _drive_access_kind("macOS is blocking the helper from Google Drive (Operation not permitted)") == "fda"
    # Fix 1b: blocked Drive → stuck original → access returns → recheck kicks backlog → sent once, no dup
    saved = {k: os.environ.get(k) for k in ("TWITCH_RECORDER_CLOUDSTORAGE", "TWITCH_RECORDER_DRIVE_VOLUMES")}
    prev_g = (REC_DIR, DRIVE_PARTS_DIR, PIPELINE_FILE)
    with PIPELINE_LOCK:
        prev_p = {k: (json.loads(json.dumps(PIPELINE.get(k))) if k in PIPELINE else None)
                  for k in ("deliveries", "originals", "target", "oneCopy")}
    with STATUS_FILE_LOCK:
        prev_sf = dict(STATUS_FILE)
    prev_da = dict(DRIVE_ACCESS)
    real_req = globals()["request_delivery"]
    kicks = []
    try:
        with _tmp.TemporaryDirectory(prefix="tar-656-") as td:
            td = Path(td)
            REC_DIR = td / "rec"
            DRIVE_PARTS_DIR = REC_DIR / "Drive parts"
            PIPELINE_FILE = REC_DIR / ".drive-pipeline.json"
            tgt = td / "CloudStorage" / "GoogleDrive-selftest@example.com" / "My Drive" / "Twitch"
            (tgt / ORIGINALS_SUBDIR).mkdir(parents=True)
            REC_DIR.mkdir(parents=True)
            os.environ["TWITCH_RECORDER_CLOUDSTORAGE"] = str(td / "CloudStorage")
            os.environ["TWITCH_RECORDER_DRIVE_VOLUMES"] = ""
            orig = "selftestfda-Fri-2026-10-23T19-00-00.ogg"
            (REC_DIR / orig).write_bytes(b"f" * 4000)
            with PIPELINE_LOCK:
                PIPELINE["deliveries"] = {}
                PIPELINE["originals"] = {}
                PIPELINE["target"] = os.path.realpath(str(tgt))
            register_original(orig, "selftestfda", "stream")
            globals()["request_delivery"] = lambda base=None: kicks.append(base)
            can_block = hasattr(os, "geteuid") and os.geteuid() != 0
            if can_block:
                os.chmod(tgt / ORIGINALS_SUBDIR, 0o500)
                os.chmod(tgt, 0o500)
                try:
                    snap = deliver_original(orig) or {}
                    assert snap.get("status") == "error" and _drive_access_kind(snap.get("error")) == "fda", snap
                    assert _drive_blocked_now()
                    r = drive_access_recheck("self-test")
                    assert r["ok"] is False and r["kind"] == "fda" and not kicks, r
                    assert drive_access_extra("fda")["stuck"]["originals"] == 1
                finally:
                    os.chmod(tgt, 0o755)
                    os.chmod(tgt / ORIGINALS_SUBDIR, 0o755)
            else:
                _orig_update(orig, status="error", error=drive_permission_message(), lastAttempt=time.time())
            r = drive_access_recheck("self-test")
            assert r["ok"] is True and kicks and r["kicked"]["originals"] == 1, r
            assert not list(tgt.glob(".tar-access-check-*"))  # probe leaves nothing behind
            todo = originals_due({"*"}, time.time())
            assert orig in todo, todo
            for nm in todo:
                deliver_original(nm)
            dest = tgt / ORIGINALS_SUBDIR / orig
            assert dest.is_file() and dest.stat().st_size == 4000
            # a second retry (e.g. the next kick) must not create a second copy
            _orig_update(orig, status="error", error="x")
            deliver_original(orig)
            assert len(list((tgt / ORIGINALS_SUBDIR).iterdir())) == 1, list((tgt / ORIGINALS_SUBDIR).iterdir())
    finally:
        globals()["request_delivery"] = real_req
        REC_DIR, DRIVE_PARTS_DIR, PIPELINE_FILE = prev_g
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with PIPELINE_LOCK:
            for k, v in prev_p.items():
                if v is None:
                    PIPELINE.pop(k, None)
                else:
                    PIPELINE[k] = v
        with STATUS_FILE_LOCK:
            STATUS_FILE.clear()
            STATUS_FILE.update(prev_sf)
        DRIVE_ACCESS.clear()
        DRIVE_ACCESS.update(prev_da)
    # Fix 2: the demucs launcher saves WAV when torchaudio.save has no backend
    compile(DEMUCS_SAVE_SHIM, "<demucs-shim>", "exec")
    m = friendly_demucs_error("RuntimeError: Couldn't find appropriate backend to handle uri /x/vocals.wav and format None.")
    assert "soundfile" in m and "Traceback" not in m and len(m) < 400, m
    assert friendly_demucs_error("a\nValueError: boom").endswith("ValueError: boom")
    try:
        import numpy as _np  # noqa: F401
        has_np = True
    except ImportError:
        has_np = False
    if has_np:
        with _tmp.TemporaryDirectory(prefix="tar-656d-") as td:
            td = Path(td)
            fake = td / "fake"
            (fake / "torchaudio").mkdir(parents=True)
            (fake / "demucs").mkdir(parents=True)
            (fake / "torchaudio" / "__init__.py").write_text(
                "def save(uri, src, sr, **kw):\n"
                "    raise RuntimeError(\"Couldn't find appropriate backend to handle uri %s and format None.\" % uri)\n")
            (fake / "demucs" / "__init__.py").write_text("")
            (fake / "demucs" / "separate.py").write_text(
                "import sys, numpy as np, torchaudio as ta\n"
                "def main():\n"
                "    out = sys.argv[sys.argv.index('-o') + 1]\n"
                "    a = np.zeros((2, 4410), dtype='float32'); a[0, :100] = 0.5\n"
                "    ta.save(out + '/vocals.wav', a, 44100, bits_per_sample=16)\n"
                "    ta.save(out + '/no_vocals.wav', a, 44100, bits_per_sample=24)\n")
            env = dict(os.environ, PYTHONPATH=str(fake))
            r = subprocess.run([sys.executable, "-c", DEMUCS_SAVE_SHIM, "-o", str(td)], capture_output=True,
                               text=True, env=env, timeout=120)
            assert r.returncode == 0, r.stderr[-800:]
            import wave as _wave
            for nm, w in (("vocals.wav", 2), ("no_vocals.wav", 3)):
                with _wave.open(str(td / nm), "rb") as wf:
                    assert wf.getnchannels() == 2 and wf.getsampwidth() == w and wf.getnframes() == 4410, nm
            assert "saved with" in r.stderr, r.stderr
    # Fix 3: Windows autostart files + watchdog decisions (simulated; no registry / schtasks touched)
    with _tmp.TemporaryDirectory(prefix="tar-656w-") as td:
        calls = []

        class _R:
            returncode = 0
            stdout = "SUCCESS"
            stderr = ""
        st = ensure_windows_autostart(startup_dir=td, run=lambda c: calls.append(c) or _R(),
                                      script=r"C:\Users\x\AppData\Local\TwitchRecorder\twitch-recorder-server.py",
                                      pyw=r"C:\Python312\pythonw.exe")
        body = (Path(td) / AUTOSTART_STARTUP_NAME).read_text(encoding="utf-8")
        assert '--watchdog", 0, False' in body and "pythonw.exe" in body, body
        assert st["task"] == AUTOSTART_TASK and calls and "/SC" in calls[0] and "MINUTE" in calls[0], (st, calls)
        st2 = ensure_windows_autostart(startup_dir=td, run=lambda c: _R(), script="s.py", pyw="p.exe")
        assert st2["startup"]

        class _Bad:
            returncode = 1
            stdout = ""
            stderr = "ERROR: Access is denied."
        st3 = ensure_windows_autostart(startup_dir=td, run=lambda c: _Bad(), script="s.py", pyw="p.exe")
        assert st3["startup"] and st3["task"] is None and "Access is denied" in (st3["error"] or ""), st3
    assert watchdog_decide(0, True, 0) == (0, False, WATCHDOG_CHECK_SECS)
    assert watchdog_decide(0, False, 0)[1] is False  # one miss (e.g. restart in progress) → wait
    f, go, _w = watchdog_decide(1, False, 0)
    assert go and f == 0
    assert watchdog_decide(1, False, 4)[2] > WATCHDOG_CHECK_SECS  # keeps crashing → back off
    started, alive_seq = [], iter([False, False, True, True])
    global WATCHDOG_LOCK_PORT
    prev_port = WATCHDOG_LOCK_PORT
    import socket as _s
    probe = _s.socket()
    probe.bind(("127.0.0.1", 0))
    WATCHDOG_LOCK_PORT = probe.getsockname()[1]
    probe.close()
    try:
        watchdog_main(max_loops=4, alive_fn=lambda: next(alive_seq), start_fn=lambda: started.append(1),
                      sleep_fn=lambda s: None)
        assert started == [1], started
        # second instance while the lock is held exits at once
        hold = _s.socket()
        hold.bind(("127.0.0.1", WATCHDOG_LOCK_PORT))
        hold.listen(1)
        try:
            assert watchdog_main(max_loops=3, alive_fn=lambda: False, start_fn=lambda: started.append(2),
                                 sleep_fn=lambda s: None) == 0 and started == [1]
        finally:
            hold.close()
    finally:
        WATCHDOG_LOCK_PORT = prev_port


def main():
    REC_DIR.mkdir(parents=True, exist_ok=True)
    # v6.29: clear orphan music-only-* temp dirs left by mid-demucs crashes
    try:
        cleanup_orphan_temps(reason="startup")
    except Exception as e:
        log(f"orphan cleanup startup failed: {e}")
    threading.Thread(target=reaper_loop, daemon=True).start()
    # v6.36: after-stream Drive delivery state + periodic "Waiting for Drive" retry
    _pipeline_load()
    # v6.37: free leftover local Drive parts for deliveries already sent (v6.36 leftovers)
    try:
        clear_sent_local_drive_parts_backfill()
    except Exception as e:
        log(f"drive parts sent-backfill failed: {e}")
    # v6.39: sweep stale Drive .partial-* under Auto-send target (+ Originals/)
    try:
        maybe_cleanup_drive_target_partials(reason="startup")
    except Exception as e:
        log(f"drive target partial cleanup startup failed: {e}")
    threading.Thread(target=delivery_loop, daemon=True, name="drive-delivery").start()
    request_delivery(None)
    # v6.53: recordings a crash / reboot / power loss cut off → join the next session or finalize
    try:
        if load_orphan_sessions():
            threading.Thread(target=orphan_recovery_loop, daemon=True, name="orphan-recovery").start()
    except Exception as e:
        log(f"recover load failed: {e}")

    # v6.44: re-queue voice-removal/split jobs a crash/reboot/restart interrupted
    def _resume_later():
        time.sleep(PIPELINE_RESUME_DELAY_SECS)
        try:
            resume_pending_pipeline_jobs()
        except Exception as e:
            log(f"pipeline resume failed: {e}")
    threading.Thread(target=_resume_later, daemon=True, name="pipeline-resume").start()
    threading.Thread(target=meter_loop, daemon=True, name="meter").start()
    # v6.40: keep the computer from sleeping while recording / removing voice / sending to Drive
    threading.Thread(target=keep_awake_loop, daemon=True, name="keep-awake").start()
    # v6.41: write <Drive target>/Recorder status/<computer>.txt + .json every few minutes
    threading.Thread(target=status_file_loop, daemon=True, name="status-file").start()
    # v6.49: keep watching armed streamers when the recorder page is closed
    try:
        solo_watch_load()
    except Exception as e:
        log(f"solo watch load failed: {e}")
    threading.Thread(target=solo_watch_loop, daemon=True, name="solo-watch").start()
    httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    # v6.43: unattended GitHub update check (~2 min after start, then every ~3 h; installs when idle)
    threading.Thread(target=auto_update_loop, daemon=True, name="auto-update").start()
    # v6.55: keep streamlink current (PyPI check a few min after start, then every ~12 h; upgrades when idle)
    threading.Thread(target=streamlink_update_loop, daemon=True, name="streamlink-update").start()
    # v6.56: re-check a blocked Drive every minute and send the backlog once access works
    threading.Thread(target=drive_access_loop, daemon=True, name="drive-access").start()
    launcher_startup_repair()  # v6.60: LaunchAgent → real python, not the /usr/bin/python3 shim
    # v6.56: Windows — start on login + watchdog that restarts the helper if it stops
    if os.name == "nt":
        threading.Thread(target=autostart_loop, daemon=True, name="autostart").start()
        # v6.58: wake the PC from sleep before armed streamers' usual go-live times
        threading.Thread(target=wake_schedule_loop, daemon=True, name="wake-schedule").start()
    sl = which_streamlink()
    ff = which_ffmpeg()
    html_path = HERE / HTML_NAME
    log(f"helper v{HELPER_VERSION} listening on http://{HOST}:{PORT}/  (localhost only)")
    log(f"recordings dir: {REC_DIR}")
    log(f"streamlink: {sl or 'NOT FOUND — pip install streamlink'}")
    log(f"ffmpeg: {ff or 'not found (will write .ts; seamless join needs ffmpeg)'}")
    log(f"seamless: {'ready' if ff else 'unavailable (install ffmpeg)'}")
    dm = which_demucs()
    log(f"demucs: {dm or 'NOT FOUND — pip install demucs (Music only)'}")
    try:
        _droots = detect_drive_roots()
        log("google drive: " + (", ".join(r["path"] for r in _droots) if _droots else "Drive for Desktop not found (paste-a-link still works)"))
    except Exception as e:
        log(f"google drive detect failed: {e}")
    _ka = keep_awake_method()
    log("keep awake: " + (f"{_ka} (while recording / removing voice / sending to Drive)" if _ka else "unavailable on this computer"))
    log(f"html: {html_path if html_path.is_file() else 'MISSING — ' + HTML_NAME + ' not next to this script'}")
    if AUTO_UPDATE_ENV_OFF:
        log("auto-update: disabled by TWITCH_RECORDER_AUTO_UPDATE=0")
    else:
        log(f"auto-update: {'on' if auto_update_enabled() else 'OFF (Settings)'} — first GitHub check in "
            f"{int(AUTO_UPDATE_FIRST_SECS)}s, then every {AUTO_UPDATE_EVERY_SECS / 3600:g} h; installs only when idle")
    if SL_UPDATE_ENV_OFF:
        log("streamlink update: disabled by TWITCH_RECORDER_STREAMLINK_UPDATE=0 (version still reported)")
    else:
        log(f"streamlink update: {'on' if streamlink_update_enabled() else 'OFF (Settings)'} — PyPI check in "
            f"{int(SL_UPDATE_FIRST_SECS)}s, then every {SL_UPDATE_EVERY_SECS / 3600:g} h; upgrades pip installs only when idle")
    sw = solo_watch_snapshot()
    if SOLO_WATCH_SECS <= 0:
        log("solo watch: disabled by TWITCH_RECORDER_SOLO_WATCH_SECS=0")
    else:
        log(f"solo watch: {'on' if sw.get('enabled') else 'OFF'} — when the page is closed, check armed "
            f"streamers every {int(SOLO_WATCH_SECS)}s"
            + (f" (persisted: {', '.join(sw.get('users') or [])})" if sw.get("users") else " (no armed list yet)"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down…")
        with LOCK:
            stop_all_locked()
        keep_awake_release()
        httpd.server_close()
        log("bye")


if __name__ == "__main__":
    _harden_stdio()  # v6.51: Windows cp1252 output can't make log lines raise
    if "--self-test" in sys.argv[1:]:  # v6.43: used by apply_update before swapping in a download
        sys.exit(self_test())
    if "--watchdog" in sys.argv[1:]:  # v6.56: Windows login start / crash restart
        sys.exit(watchdog_main())
    if "--wake" in sys.argv[1:]:  # v6.58: run by the wake-for-stream Scheduled Task
        sys.exit(wake_main())
    main()
