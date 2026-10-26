#!/usr/bin/env python3
"""Independent Twitch audio backup. Originals are always retained."""
import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import site
os.environ["PATH"] = str(Path(site.USER_BASE) / "bin") + os.pathsep + os.environ.get("PATH", "")
import time

STOP = False
ACTIVE = None

def stop(signum, frame):
    global STOP
    STOP = True
    if ACTIVE and ACTIVE.poll() is None:
        ACTIVE.send_signal(signal.SIGINT)

def run(args, log):
    global ACTIVE
    with open(log, 'ab') as out:
        ACTIVE = subprocess.Popen([str(a) for a in args], stdout=out, stderr=out)
        code = ACTIVE.wait()
        ACTIVE = None
    if code:
        raise RuntimeError('Command failed; see ' + str(log))

def save(folder, state, **extra):
    path = folder / 'status.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    data.update(state=state, updated=dt.datetime.now().isoformat(), **extra)
    tmp = folder / 'status.tmp'
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    print(folder.name + ': ' + state, flush=True)

def duration(path):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                       capture_output=True, text=True, check=True)
    value = float(p.stdout.strip())
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError('No usable audio duration: ' + str(path))
    return value

def split(music, folder, seconds=1800):
    """Encode continuous audio once; ffmpeg cuts on audio packet boundaries."""
    staging = folder / 'segments-in-progress'
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', music, '-map', '0:a:0',
         '-c:a', 'libopus', '-b:a', '160k', '-f', 'segment', '-segment_time', seconds,
         '-reset_timestamps', '1', staging / 'chunk-%05d.ogg'], folder / 'processing.log')
    chunks = sorted(staging.glob('chunk-*.ogg'))
    if not chunks:
        raise RuntimeError('No segments were produced')
    total = len(chunks)
    files = []
    for i, chunk in enumerate(chunks, 1):
        length = duration(chunk)
        name = '{}-music-part{}of{}.ogg'.format(folder.name, i, total)
        chunk.rename(staging / name)
        files.append(dict(file=name, part=i, total=total, seconds=length))
    (staging / 'manifest.json').write_text(json.dumps(files, indent=2))
    target = folder / 'music-parts'
    if target.exists():
        raise RuntimeError('Final parts already exist; originals and existing parts retained')
    staging.rename(target)
    return files

def process(folder):
    folder = folder.resolve()
    if (folder / 'music-parts' / 'manifest.json').exists():
        print('Already processed: ' + str(folder))
        return
    source = folder / 'original.ts'
    if not source.exists() or source.stat().st_size == 0:
        raise RuntimeError('Original recording is empty or missing')
    try:
        duration(source)
        save(folder, 'extracting audio')
        wav = folder / 'audio.wav'
        run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', source, '-vn',
             '-ac', '2', '-ar', '44100', '-c:a', 'pcm_s16le', wav], folder / 'processing.log')
        save(folder, 'removing vocals')
        music = folder / 'separated' / 'htdemucs' / 'audio' / 'no_vocals.wav'
        run([sys.executable, '-m', 'demucs', '--two-stems=vocals', '-n', 'htdemucs',
             '-d', 'cpu', '-o', folder / 'separated', wav], folder / 'processing.log')
        if not music.exists():
            raise RuntimeError('Demucs did not create the instrumental file')
        save(folder, 'splitting into 30-minute parts')
        files = split(music, folder)
        save(folder, 'complete', parts=len(files), error=None)
    except Exception as exc:
        save(folder, 'processing failed', error=str(exc))
        raise

def check():
    missing = [name for name in ('ffmpeg', 'ffprobe', 'streamlink') if not shutil.which(name)]
    p = subprocess.run([sys.executable, '-c', 'import demucs; import torch'], capture_output=True)
    if p.returncode:
        missing.append('Demucs / PyTorch')
    if missing:
        raise RuntimeError('Missing: ' + ', '.join(missing) + '. Run Install.command first.')

def watch(channel, root, interval):
    global ACTIVE
    channel = re.sub(r'^https?://(?:www\.)?twitch\.tv/', '', channel).strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,25}', channel):
        raise RuntimeError('Enter a Twitch channel name or its twitch.tv URL')
    root.mkdir(parents=True, exist_ok=True)
    lock = root / (channel.lower() + '.lock')
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError('Channel lock exists: ' + str(lock) + '. If no backup is running, remove this lock and restart.')
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    try:
        while not STOP:
            print('Watching ' + channel + ' …', flush=True)
            probe = subprocess.run(['streamlink', '--stream-url', 'https://twitch.tv/' + channel, 'audio_only,best'], capture_output=True, text=True)
            if probe.returncode:
                print('Offline or unavailable; checking again shortly. ' + probe.stderr[-300:], flush=True)
            else:
                name = channel.lower() + '-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                folder = root / name
                folder.mkdir()
                save(folder, 'recording', channel=channel)
                try:
                    run(['streamlink', '--retry-streams', '5', '--retry-max', '3',
                         '--stream-segment-attempts', '5', '-o', folder / 'original.ts',
                         'https://twitch.tv/' + channel, 'audio_only,best'], folder / 'recording.log')
                except RuntimeError as exc:
                    print(str(exc), flush=True)
                save(folder, 'recorded; ready to process')
                # Processing runs separately so watching/backup capture can resume immediately.
                with open(folder / 'worker.log', 'ab') as log:
                    worker = [sys.executable, str(Path(__file__).resolve()), 'process', str(folder)]
                    if shutil.which('caffeinate'):
                        worker = ['caffeinate', '-i'] + worker
                    subprocess.Popen(worker, stdout=log, stderr=log, start_new_session=True)
            for _ in range(interval):
                if STOP:
                    break
                time.sleep(1)
    finally:
        lock.unlink(missing_ok=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    w = sub.add_parser('watch')
    w.add_argument('channel')
    w.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'Recordings')
    w.add_argument('--poll', type=int, default=30)
    p = sub.add_parser('process')
    p.add_argument('folder', type=Path)
    sub.add_parser('check')
    args = parser.parse_args()
    if args.mode == 'process':
        # One CPU separation at a time, including across separate channel watchers.
        import fcntl
        with open(Path(__file__).resolve().parent / '.processing.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            process(args.folder)
    else:
        check()
        if args.mode == 'watch':
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            watch(args.channel, args.output.resolve(), max(5, args.poll))
        else:
            print('All recording and vocal-removal dependencies are ready.')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        sys.exit(1)
