#!/usr/bin/env python3
"""Twitch Auto-Recorder local helper — streamlink on 127.0.0.1:8765 only."""

import http.server
import json
import sys
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
HELPER_VERSION = "6.29"
REC_DIR = Path.home() / "TwitchRecordings"
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
# Mid-resume: consecutive establish failures before giving up (stream likely offline /
# page closed so Twitch poll cannot /api/stop). Auto-Rec can POST /api/record again if still live.
MAX_MID_QUICK_FAILS = 5
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

# Seamless join (ffmpeg concat) async jobs — same shape as music for UI reuse.
SEAMLESS_JOBS = {}
SEAMLESS_JOBS_LOCK = threading.Lock()
SEAMLESS_JOB_KEEP_FINISHED_SECS = 15 * 60
# username -> {segments: [basenames], at: epoch} after stop / give-up / supervisor exit
RECENT_SESSIONS = {}
RECENT_SESSIONS_KEEP_SECS = 6 * 60 * 60


def which_streamlink():
    return shutil.which("streamlink") or shutil.which("streamlink.exe")


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


def remember_session_segments(username, segments):
    """Stash finished segment basenames after stop/give-up for POST /api/seamless {username}.

    Safe to call while holding LOCK (does not re-enter LOCK).
    """
    segs = [s for s in (segments or []) if s and isinstance(s, str)]
    if not segs:
        return
    RECENT_SESSIONS[username] = {"segments": list(segs), "at": time.time()}


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


def log(msg):
    print(f"[recorder] {msg}", flush=True)


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
    """One cleanup per diskWarn episode (in addition to startup)."""
    global _ORPHAN_DISKWARN_DONE
    if not disk or not disk.get("diskWarn"):
        _ORPHAN_DISKWARN_DONE = False
        return None
    if _ORPHAN_DISKWARN_DONE:
        return None
    _ORPHAN_DISKWARN_DONE = True
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


def resolve_download_path(raw_name):
    """Strict basename under REC_DIR only. No path traversal. None if missing/unsafe."""
    if not raw_name or not isinstance(raw_name, str):
        return None
    name = raw_name.strip()
    if not name or name in (".", "..") or chr(0) in name:
        return None
    if "/" in name or "\\" in name:
        return None
    if os.path.basename(name) != name:
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
    sl = which_streamlink()
    ff = which_ffmpeg()
    # Supported OGG path is audio_only (video→ogg is unreliable / not wanted).
    sl_quality = "audio_only"
    if ff:
        out = unique_recording_path(username, "ogg")
        cmd = [
            sl,
            "--force",
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
        cmd = [sl, "--force", "--output", str(out), f"twitch.tv/{username}", sl_quality]
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
    while time.time() < deadline:
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
        return False, with_stderr(
            f"no output file growth within {QUICK_FAIL_SECS}s — retrying",
            proc,
        )
    return True, None


def interruptible_backoff(username, gen, seconds):
    """Sleep up to seconds; return False if stop/gen cancelled."""
    end = time.time() + seconds
    while time.time() < end:
        if not still_wanted(username, gen):
            return False
        time.sleep(0.25)
    return still_wanted(username, gen)


def mid_resume_backoff(resume_idx):
    """Backoff for mid-stream resume attempt resume_idx (0-based). Never gives up."""
    if resume_idx < len(MID_RESUME_BACKOFFS):
        return MID_RESUME_BACKOFFS[resume_idx]
    return MID_RESUME_BACKOFFS[-1]


def record_supervisor(username, quality, gen):
    """Background: spawn streamlink, retry early failures, then mid-stream auto-resume."""
    attempt = 0
    max_attempts = 1 + len(RETRY_BACKOFFS)
    while attempt < max_attempts:
        if not still_wanted(username, gen):
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    ACTIVE.pop(username, None)
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
                    rec = ACTIVE.get(username)
                    if rec and rec.get("gen") == gen:
                        ACTIVE.pop(username, None)
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
                    rec = ACTIVE.get(username)
                    if rec and rec.get("gen") == gen:
                        ACTIVE.pop(username, None)
                return
            continue

        started = time.strftime("%Y-%m-%dT%H:%M:%S")
        with LOCK:
            rec = ACTIVE.get(username)
            if not rec or rec.get("gen") != gen or not rec.get("want", True):
                terminate_proc(proc)
                if rec and rec.get("gen") == gen:
                    ACTIVE.pop(username, None)
                return
            rec["proc"] = proc
            rec["file"] = str(out)
            rec["startedAt"] = started
            rec["quality"] = quality
            rec["pid"] = proc.pid
            rec["starting"] = True
            rec["retrying"] = attempt > 0

        ok, err = monitor_quick_fail(username, gen, proc, out)
        if not still_wanted(username, gen):
            terminate_proc(proc)
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    ACTIVE.pop(username, None)
            return
        if ok:
            set_last_error(username, None)
            keep = False
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    rec["starting"] = False
                    rec["retrying"] = False
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
        if attempt >= len(RETRY_BACKOFFS):
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    ACTIVE.pop(username, None)
            log(f"gave up on {username} after {max_attempts} attempts")
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
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    ACTIVE.pop(username, None)
            return

    with LOCK:
        rec = ACTIVE.get(username)
        if rec and rec.get("gen") == gen:
            ACTIVE.pop(username, None)


def watch_and_resume(username, quality, gen, proc):
    """After establish: detect mid-stream exit/stall, keep segment, start new file while wanted.

    Gives up after MAX_MID_QUICK_FAILS consecutive establish failures (or sooner when
    stderr clearly says offline), so a closed recorder page cannot leave a forever-retrying
    supervisor after the broadcast ends. If the stream is still live, the HTML Auto-Rec
    poll can POST /api/record again.
    """
    resume_idx = 0
    consecutive_quick_fails = 0
    with LOCK:
        rec0 = ACTIVE.get(username) or {}
        path0 = rec0.get("file") or ""
    last_size = file_size(path0)
    last_growth = time.time()

    def begin_mid_resume(err_msg):
        """Mark slot retrying after a finished segment; caller owns resume loop."""
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
            if now - last_growth < STALL_SECS:
                time.sleep(0.75)
                continue
            err = with_stderr(
                f"streamlink stalled (no file growth for {STALL_SECS}s) — resuming",
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
            backoff = mid_resume_backoff(resume_idx)
            log(f"mid-resume {username} in {backoff}s (resume #{resume_idx + 1})")
            if not interruptible_backoff(username, gen, backoff):
                break
            if not still_wanted(username, gen):
                break

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
                    give_up(
                        f"gave up after {consecutive_quick_fails} mid-resume fails — "
                        "stream likely offline (Auto-Rec will restart if still live)"
                    )
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
                        if not rec.get("sessionStartedAt"):
                            rec["sessionStartedAt"] = started
                log(f"recording {username} (segment resume) pid={new_proc.pid} -> {out}")
                proc = new_proc
                resume_idx = 0
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
                give_up(
                    f"gave up after {consecutive_quick_fails} mid-resume fails — "
                    "stream likely offline (Auto-Rec will restart if still live)"
                    + (f" — {qerr}" if qerr else "")
                )
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



def start_record(username, quality):
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
        }
    REC_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK:
        gen = ACTIVE[username]["gen"]
    t = threading.Thread(
        target=record_supervisor,
        args=(username, quality, gen),
        daemon=True,
        name=f"rec-{username}",
    )
    t.start()
    return {
        "ok": True,
        "file": "",
        "pid": None,
        "retrying": True,
        "lastError": LAST_ERROR.get(username),
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


def local_helper_version():
    """Prefer HELPER_VERSION; fall back to VERSION file next to the script."""
    ver = str(HELPER_VERSION or "").strip()
    if ver:
        return ver
    try:
        p = INSTALL_DIR / "VERSION"
        if p.is_file():
            return p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except OSError:
        pass
    return ""


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


def apply_update():
    """Download UPDATE_FILES into INSTALL_DIR. Never self-kill; always needsRestart."""
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
    try:
        for name in UPDATE_FILES:
            url = UPDATE_RAW_BASE + name
            data = fetch_url_bytes(url, timeout=45)
            if not data:
                raise RuntimeError(f"empty download: {name}")
            dest = INSTALL_DIR / name
            atomic_write_bytes(dest, data)
            written.append(name)
            log(f"update wrote {dest} ({len(data)} bytes)")
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "localVersion": local,
            "remoteVersion": remote,
            "written": written,
            "restarted": False,
            "needsRestart": False,
        }

    with LOCK:
        reap_locked()
        active = list(ACTIVE.keys())

    note = (
        "Update files written. Restart the helper (LaunchAgent / Desktop launcher / "
        "stop+start twitch-recorder-server.py), then hard-refresh the recorder page."
    )
    if active:
        note = (
            f"Update written while recording {', '.join(active)} — process left running. "
            "Restart the helper after recordings finish, then hard-refresh the page."
        )

    return {
        "ok": True,
        "version": remote,
        "localVersion": local,
        "remoteVersion": remote,
        "written": written,
        "installDir": str(INSTALL_DIR),
        "active": active,
        "restarted": False,
        "needsRestart": True,
        "note": note,
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
    return {
        "ok": True,
        "version": HELPER_VERSION,
        "streamlink": bool(sl),
        "ffmpeg": bool(which_ffmpeg()),
        "seamless": seamless_available(),
        "demucs": demucs_available(),
        # v6.24: install hint for HTML sticky banner / Convert / History "Need demucs"
        "demucsHint": None if demucs_available() else "pip install demucs (first run downloads models)",
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
        "musicDemucsQueue": music_demucs_queue_info(),
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
    if demucs == "python -m demucs":
        cmd = [sys.executable, "-m", "demucs"]
    else:
        cmd = [demucs]
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
        raise RuntimeError(f"demucs failed (exit {rc}): {err or 'unknown'}")
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
    try:
        # v6.28: stay queued until a demucs slot is free (one at a time).
        # v6.29: waiting is interruptible — Cancel waiting / Clear waiting Music.
        _set_music_job(
            job_id,
            status="queued",
            progress="Waiting for demucs (1 at a time)…",
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

        def progress(msg, pct=None):
            # pct=None → update text only (hold last progressPct).
            if pct is not None:
                _set_music_job(job_id, progress=msg, progressPct=pct)
            else:
                _set_music_job(job_id, progress=msg)

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
            f"music-only done job={job_id} out={out_name}"
            + (f" vocals={vocals_name}" if vocals_name else "")
        )
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


def start_music_only(raw_name, redo=False):
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
    if is_music_export_name(target.name):
        return {
            "ok": False,
            "error": "pick the native recording — not a -music / -vocals export",
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
            if (job.get("name") or "") == target.name and (job.get("status") or "") in (
                "queued",
                "running",
            ):
                return {
                    "ok": True,
                    "jobId": jid,
                    "alreadyRunning": True,
                    "redo": bool(job.get("redo")),
                }, 200

    if not demucs_available():
        return {
            "ok": False,
            "error": "demucs not installed — pip install demucs (first run downloads models)",
            "demucs": False,
        }, 400

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
                "Waiting for demucs (1 at a time)…"
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
        }
    t = threading.Thread(
        target=music_only_worker,
        args=(job_id, str(target)),
        daemon=True,
        name=f"music-only-{job_id}",
    )
    t.start()
    log(
        f"music-only started job={job_id} file={target.name} redo={bool(redo)}"
        + (f" waitingBehind={ahead}" if ahead else "")
    )
    return {
        "ok": True,
        "jobId": job_id,
        "redo": bool(redo),
        "waitingBehind": bool(ahead),
        "queueAhead": int(ahead),
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

    with MUSIC_JOBS_LOCK:
        if waiting_all:
            targets = [
                jid
                for jid, job in MUSIC_JOBS.items()
                if (job.get("status") or "") == "queued"
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
            self.send_json(health_payload())
            return
        if path == "/api/update/check":
            self.send_json(check_update())
            return
        if path == "/api/status":
            self.send_json(status_payload())
            return
        if path == "/api/files":
            self.send_json(files_payload())
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
            result = apply_update()
            self.send_json(result, status=200 if result.get("ok") else 400)
            return
        if path == "/api/record":
            body = self.read_json()
            username = safe_username(body.get("username"))
            if not username:
                self.send_json({"ok": False, "error": "invalid username"}, status=400)
                return
            quality = body.get("quality") or "audio_only"
            result = start_record(username, quality)
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
        if path == "/api/music-only/cancel":
            body = self.read_json()
            payload, status = cancel_music_only(body if isinstance(body, dict) else {})
            self.send_json(payload, status=status)
            return
        if path == "/api/music-only":
            body = self.read_json()
            name = (body or {}).get("name") if isinstance(body, dict) else None
            redo = bool((body or {}).get("redo")) if isinstance(body, dict) else False
            payload, status = start_music_only(name, redo=redo)
            self.send_json(payload, status=status)
            return
        if path == "/api/seamless":
            body = self.read_json()
            payload, status = start_seamless(body)
            self.send_json(payload, status=status)
            return
        if path == "/api/music-only-upload":
            parsed, err = parse_multipart_file(self)
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
            if not demucs_available():
                self.send_json(
                    {
                        "ok": False,
                        "error": "demucs not installed — pip install demucs (first run downloads models)",
                        "demucs": False,
                        "savedName": dest.name,
                        "savedAs": dest.name,
                        "path": str(dest),
                    },
                    status=400,
                )
                return
            payload, status = start_music_only(dest.name, redo=False)
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


def main():
    REC_DIR.mkdir(parents=True, exist_ok=True)
    # v6.29: clear orphan music-only-* temp dirs left by mid-demucs crashes
    try:
        cleanup_orphan_temps(reason="startup")
    except Exception as e:
        log(f"orphan cleanup startup failed: {e}")
    threading.Thread(target=reaper_loop, daemon=True).start()
    threading.Thread(target=meter_loop, daemon=True, name="meter").start()
    httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
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
    log(f"html: {html_path if html_path.is_file() else 'MISSING — ' + HTML_NAME + ' not next to this script'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down…")
        with LOCK:
            stop_all_locked()
        httpd.server_close()
        log("bye")


if __name__ == "__main__":
    main()
