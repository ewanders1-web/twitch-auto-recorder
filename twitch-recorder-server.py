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
HELPER_VERSION = "6.42"
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
    # v6.36: recording finalized → remove voice → split → send to Drive (if enabled)
    try:
        _on_session_finalized(username, segs)
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


def restart_grace_applies(username):
    """v6.42: keep retrying through a stream restart? True when the recorder page's latest
    heartbeat lists this streamer as Auto-Rec armed, or no page has reported in since the
    helper started (helper running alone). False when the page says they are not armed.

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

        tries += 1
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
                    rec["graceUntil"] = 0.0
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
        now = time.time()
        if first_fail_at is None:
            first_fail_at = now
        if attempt >= len(RETRY_BACKOFFS):
            # v6.42: stream restart / slow playlist — keep trying for the grace window
            until = first_fail_at + RESTART_GRACE_SECS
            if now < until and restart_grace_applies(username):
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
                        rec = ACTIVE.get(username)
                        if rec and rec.get("gen") == gen:
                            ACTIVE.pop(username, None)
                    return
                continue
            if grace_idx:
                set_last_error(username, f"stopped retrying {username} after {tries} tries over "
                                         f"{int(round((now - first_fail_at) / 60.0))} min — {err}")
            with LOCK:
                rec = ACTIVE.get(username)
                if rec and rec.get("gen") == gen:
                    ACTIVE.pop(username, None)
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
    # v6.42: when the last segment ended; restart grace runs from here
    seg_ended_at = 0.0
    grace_ok = False
    grace_idx = 0
    with LOCK:
        rec0 = ACTIVE.get(username) or {}
        path0 = rec0.get("file") or ""
    last_size = file_size(path0)
    last_growth = time.time()

    def in_grace():
        """v6.42: still inside the restart window for this ended segment?"""
        return grace_ok and time.time() < seg_ended_at + RESTART_GRACE_SECS

    def grace_or_give_up(fails, last_err):
        """Out of normal retries: keep waiting through a restart, or give up. True = keep going."""
        nonlocal grace_ok
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
                        if not rec.get("sessionStartedAt"):
                            rec["sessionStartedAt"] = started
                log(f"recording {username} (segment resume) pid={new_proc.pid} -> {out}"
                    + (f" after {int(time.time() - seg_ended_at)}s restart wait" if grace_idx else ""))
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
    KEEP_AWAKE_WAKE.set()  # v6.40: hold sleep off right away
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
    with LOCK:
        reap_locked()
        active = list(ACTIVE.keys())
    if active:
        return
    if music_jobs_busy():
        return
    _auto_restart_scheduled = True
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
        "sending": bool(DELIVERY_STATE.get("busy")),
        "driveFound": bool(detect_drive_roots()),
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
        _register_work_path(work)  # v6.35: protect from diskWarn orphan cleanup

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
            _unregister_work_path(work)


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

    if not demucs_available():
        return {
            "ok": False,
            "error": "demucs not installed — pip install demucs (first run downloads models)",
            "demucs": False,
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
        capture_output=True, text=True, timeout=180,
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
            capture_output=True, text=True, timeout=600,
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
    base_cmd = _demucs_base_cmd()
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
        if stem_wav is None:
            raise RuntimeError(f"demucs failed on chunk {i + 1}/{n}: {last_err or 'unknown'}")
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
        # One demucs at a time (shared with History / Auto Music).
        _set_music_job(job_id, status="queued", phase="wait",
                       progress="Waiting for demucs (1 at a time)…")
        while True:
            _check_cancel(job_id)
            if DEMUCS_SLOTS.acquire(timeout=0.5):
                slot_held = True
                break
        _check_cancel(job_id)
        _set_music_job(job_id, status="running", phase="demucs")
        parts = chunked_demucs_no_vocals(job_id, staged, work, info["duration"], progress)
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


def _drive_preflight():
    if not which_ffmpeg():
        return {"ok": False, "error": "ffmpeg not found — install ffmpeg"}, 400
    if not demucs_available():
        return {"ok": False, "error": "demucs not installed — pip install demucs (first run downloads models)",
                "demucs": False}, 400
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
    pre = _drive_preflight()
    if pre:
        return pre
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
        if is_music_export_name(name):
            skipped.append({"path": real, "reason": "already a -music / -vocals export"})
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
                             outMode=mode, redo=redo)
        started.append({"path": real, "jobId": jid, "outputPath": str(dest)})
        log(f"drive-music queued job={jid} src={real} out={dest} redo={redo}")
    return {"ok": True, "started": started, "skipped": skipped}, 200


def start_drive_fetch(body):
    """POST /api/drive/fetch {url, keepSource, redo} — public Drive link."""
    body = body if isinstance(body, dict) else {}
    file_id, err = parse_drive_file_id(body.get("url"))
    if err:
        return {"ok": False, "error": err}, 400
    pre = _drive_preflight()
    if pre:
        return pre
    key = "link:" + file_id
    jid = _inflight_job_for(key)
    if jid:
        return {"ok": True, "jobId": jid, "alreadyRunning": True, "fileId": file_id}, 200
    jid = _new_drive_job(key, f"Drive file {file_id[:10]}…", "link", fileId=file_id,
                         keepSource=bool(body.get("keepSource")), redo=bool(body.get("redo")),
                         outMode="recordings")
    log(f"drive-fetch queued job={jid} id={file_id}")
    return {"ok": True, "jobId": jid, "fileId": file_id}, 200


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
            "keepAwake": True, "keepAwakeArmed": False}  # v6.40 keep computer awake
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
        # v6.41: post this computer's status file into Drive (default on)
        PIPELINE["statusToDrive"] = data.get("statusToDrive") is not False


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
    return None


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
        }


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
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600)
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
    return ("macOS is blocking the helper from Google Drive (Operation not permitted). "
            "Open System Settings → Privacy & Security → Full Disk Access, press +, add "
            f"{exe} (⌘⇧G to type the path) and turn it on — the helper retries every 5 min "
            "and sends the waiting parts on its own.")


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
    if d.get("status") == "sent":
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
    _delivery_update(base, status="sending", error=None, target=target, lastAttempt=time.time(), note=None)
    DELIVERY_STATE["busy"] = True
    DELIVERY_STATE["base"] = base
    try:
        for nm in d.get("parts") or []:
            src = Path(d["partsDir"]) / nm
            if not src.is_file():
                raise RuntimeError(f"local part missing ({nm}) — use Split for Drive again")
            # v6.38: same name+size already in Drive → skip; different size → <name>-<Computer>
            # (a manual Re-split from this computer may replace its own earlier parts)
            place_in_drive(src, Path(target), nm, allow_replace=(d.get("source") == "manual"))
        # Mark sent first so a crash mid-clear does not leave status=waiting with missing parts
        snap = _delivery_update(base, status="sent", sentAt=time.time(), target=target, error=None)
        log(f"drive delivery sent base={base} parts={len(d.get('parts') or [])} → {target}")
        # v6.37: free local Drive parts after successful send (full -music.ogg kept)
        freed = clear_local_drive_parts(base)
        snap = _delivery_update(
            base, status="sent", localPartsCleared=True, partsDir=None,
        ) or snap
        if freed:
            log(f"drive parts post-send cleared base={base} freed={freed} bytes")
        return snap
    except Exception as e:
        log(f"drive delivery error base={base}: {e}")
        msg = drive_permission_message() if isinstance(e, PermissionError) else (str(e) or type(e).__name__)
        return _delivery_update(base, status="error", error=msg)
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
                "localPartsCleared")})
    return out


def pipeline_status_payload():
    roots = detect_drive_roots()
    target, kind, err = resolve_delivery_target(roots, create=False) if roots else (None, "auto", "Google Drive for Desktop not found")
    with PIPELINE_LOCK:
        prefs = {"enabled": PIPELINE["enabled"], "users": dict(PIPELINE["users"]), "target": PIPELINE["target"],
                 "origEnabled": bool(PIPELINE.get("origEnabled", True)),
                 "origUsers": dict(PIPELINE.get("origUsers") or {}),
                 "statusToDrive": PIPELINE.get("statusToDrive", True) is not False}
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
        if "statusToDrive" in body:  # v6.41
            PIPELINE["statusToDrive"] = bool(body.get("statusToDrive"))
            changed = True
            STATUS_FILE_WAKE.set()
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
    else:
        msg = f"Waiting for Drive ({len(names)} part{'s' if len(names) != 1 else ''} ready)"
    return base, names, part_secs, st, msg


def pipeline_worker(job_id):
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
        if existing and not redo:
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
            # Disk-low pause (Auto): wait while under diskWarn instead of starting demucs.
            paused = False
            while disk_status().get("diskWarn"):
                if not paused:
                    log(f"pipeline job={job_id}: paused — disk low")
                    paused = True
                _set_music_job(job_id, status="queued", progress="Paused — disk low (<2 GB free); waiting for space…")
                _check_cancel(job_id)
                time.sleep(5)
            _set_music_job(job_id, status="queued", phase="wait", progress="Waiting for demucs (1 at a time)…")
            while True:
                _check_cancel(job_id)
                if DEMUCS_SLOTS.acquire(timeout=0.5):
                    slot_held = True
                    break
            _check_cancel(job_id)
            _set_music_job(job_id, status="running", phase="demucs")
            work = Path(tempfile.mkdtemp(prefix="music-only-", dir=str(REC_DIR)))
            _register_work_path(work)
            parts = chunked_demucs_no_vocals(job_id, paths, work, total, progress, pct_lo=5.0, pct_hi=85.0)
            _set_music_job(job_id, phase="encode")
            tmp_out = encode_joined_ogg(job_id, parts, work / "out.ogg", total, progress,
                                        pct_lo=85.0, pct_hi=94.0, codec_args=MUSIC_OGG_ARGS)
            if redo and len(paths) == 1:
                delete_music_siblings(paths[0].name)
            dest = _unique_music_dest(paths[0].name)
            atomic_publish(tmp_out, dest)
            music_path = dest
            _set_music_job(job_id, outputName=dest.name,
                           outputUrl="/api/download/" + urllib.parse.quote(dest.name))
            # Free the demucs slot before split/delivery so the next job can start.
            DEMUCS_SLOTS.release()
            slot_held = False
            shutil.rmtree(work, ignore_errors=True)
            _unregister_work_path(work)
            work = None
        _set_music_job(job_id, outputName=Path(music_path).name,
                       outputUrl="/api/download/" + urllib.parse.quote(Path(music_path).name))
        base, names, part_secs, st, msg = _split_and_deliver(job_id, music_path, "stream", progress)
        _set_music_job(job_id, status="done", phase="done", progress=msg, progressPct=100,
                       deliveryStatus=st, error=None, finishedAt=time.time())
        log(f"pipeline done job={job_id} music={Path(music_path).name} parts={len(names)} delivery={st}")
    except _JobCancelled:
        _finalize_cancelled_music_job(job_id)
        log(f"pipeline cancelled job={job_id}")
    except Exception as e:
        msg = str(e) or type(e).__name__
        log(f"pipeline error job={job_id}: {msg}")
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
    if not demucs_available() or not which_ffmpeg():
        return None, "demucs/ffmpeg not installed"
    PIPELINE_SEEN.add(first)
    # name = first segment so History / Auto Music see it as that recording's Music-only job
    jid = _new_job(first, first, "pipeline", pipeline_worker, segments=segs, username=username,
                   redo=bool(redo), reason=reason)
    log(f"pipeline queued job={jid} user={username} segments={segs} ({reason})")
    return jid, None


def _on_session_finalized(username, segments):
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
        jid, err = start_stream_pipeline(username, segs)
        if err and not jid:
            log(f"pipeline not started for {username}: {err}")

    threading.Thread(target=run, daemon=True, name=f"pipeline-trigger-{username}").start()


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
        dest, action = place_in_drive(src, dest_dir, name)
        snap = _orig_update(name, status="sent", sentAt=time.time(), dest=str(dest), action=action,
                            size=src.stat().st_size, error=None)
        log(f"drive original {action}: {name} → {dest}")
        return snap
    except Exception as e:
        log(f"drive original error {name}: {e}")
        msg = drive_permission_message() if isinstance(e, PermissionError) else (str(e) or type(e).__name__)
        return _orig_update(name, status="error", error=msg)
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
               "jsonPath": None, "error": None, "sig": None, "writes": 0}
PAGE_SEEN = {"at": 0.0, "armed": []}
HELPER_STARTED_AT = time.time()


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
    if changed:
        STATUS_FILE_WAKE.set()


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
    return repr((active, errs, dels, origs, int(q.get("running") or 0), int(q.get("waiting") or 0),
                 armed, pending_restart_needed()))


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
    page_open = bool(page_at) and now - page_at < 120
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
        "keepAwake": {"active": bool(ka.get("active")), "reason": ka.get("reason"),
                      "supported": bool(ka.get("supported")), "error": ka.get("error")},
        "recentRecordings": recent,
    }
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
        L.append("Recorder page: NOT open" + (f" (last seen {seen})" if seen else "") +
                 " — Auto-Rec only starts recordings while the page is open")
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
    L.append(f"Drive sends: {d.get('partsWaiting', 0)} part sets waiting, {d.get('partsErrors', 0)} failed;"
             f" {d.get('originalsWaiting', 0)} originals waiting, {d.get('originalsErrors', 0)} failed")
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
    ka = doc.get("keepAwake") or {}
    if ka.get("active"):
        L.append(f"Keeping the computer awake: {ka.get('reason')}")
    elif ka.get("error"):
        L.append(f"Keep awake problem: {ka.get('error')}")
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
    """Write this computer's status into <Drive target>/Recorder status/. Returns True on success."""
    now = time.time()
    with STATUS_FILE_LOCK:
        STATUS_FILE["lastAttemptAt"] = now
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
                doc = build_status_doc(now)
                doc["reason"] = reason
                name = computer_name()
                txt = folder / f"{name}.txt"
                js = folder / f"{name}.json"
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
        return {
            "enabled": status_file_enabled(),
            "intervalSecs": STATUS_FILE_INTERVAL_SECS,
            "lastWrittenAt": STATUS_FILE["lastWrittenAt"] or None,
            "folder": STATUS_FILE.get("folder"),
            "txtPath": STATUS_FILE.get("txtPath"),
            "error": STATUS_FILE.get("error"),
            "computer": computer_name(),
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
            try:
                q = urllib.parse.parse_qs(parsed.query or "", keep_blank_values=True)
                if "armed" in q:
                    note_armed_heartbeat(q.get("armed", [""])[0])
                    note_page_seen(q.get("armed", [""])[0])  # v6.41 status file
            except Exception as e:
                log(f"armed heartbeat parse failed: {e}")
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
            result = apply_update()
            self.send_json(result, status=200 if result.get("ok") else 400)
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
    threading.Thread(target=meter_loop, daemon=True, name="meter").start()
    # v6.40: keep the computer from sleeping while recording / removing voice / sending to Drive
    threading.Thread(target=keep_awake_loop, daemon=True, name="keep-awake").start()
    # v6.41: write <Drive target>/Recorder status/<computer>.txt + .json every few minutes
    threading.Thread(target=status_file_loop, daemon=True, name="status-file").start()
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
    try:
        _droots = detect_drive_roots()
        log("google drive: " + (", ".join(r["path"] for r in _droots) if _droots else "Drive for Desktop not found (paste-a-link still works)"))
    except Exception as e:
        log(f"google drive detect failed: {e}")
    _ka = keep_awake_method()
    log("keep awake: " + (f"{_ka} (while recording / removing voice / sending to Drive)" if _ka else "unavailable on this computer"))
    log(f"html: {html_path if html_path.is_file() else 'MISSING — ' + HTML_NAME + ' not next to this script'}")
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
    main()
