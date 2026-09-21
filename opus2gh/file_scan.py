#!/usr/bin/env python3
"""
Scan folder file audio (semua format yang didukung ffmpeg) → daftar lagu
siap dikonversi ke opus_stream.

Setiap entri: {song_id, path, name, title, artist, dur_s, duration_str,
               size_bytes, local, status}

song_id bersifat stabil & pendek (11 karakter, gaya YouTube ID:
campuran huruf besar/kecil + angka) — dihitung dari hash konten parsial
(head+tail+ukuran), sehingga file yang sama di-scan ulang menghasilkan
ID yang sama, dan file yang sudah dikonversi / di-upload tidak
diduplikasi. Nama file hasil: <song_id>.opus_stream / <song_id>.meta.json
"""

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

from opus2gh.opus_codec import FFPROBE

logger = logging.getLogger('opus2gh.scan')

# Ekstensi yang dikenali (filter cepat; ffmpeg yang menentukan dekode
# sebenarnya, jadi file rusak tetap akan gagal saat convert).
AUDIO_EXTENSIONS = {
    # audio lossy / lossless
    '.mp3', '.mp2', '.mp1', '.wav', '.flac', '.aac', '.m4a', '.m4b',
    '.m4r', '.ogg', '.oga', '.opus', '.wma', '.asf', '.aiff', '.aif',
    '.aifc', '.alac', '.ape', '.wv', '.wvp', '.amr', '.awb', '.au',
    '.snd', '.mid', '.midi', '.kar', '.rmi', '.mka', '.weba', '.ac3',
    '.dts', '.dtshd', '.eac3', '.truehd', '.mlp', '.shn', '.tta',
    '.ofr', '.ofs', '.spx', '.vox', '.raw', '.gsm', '.g722', '.g726',
    # container yang umum berisi audio (audio-nya di-extract)
    '.webm', '.mp4', '.m4v', '.mkv', '.avi', '.mov', '.qt', '.flv',
    '.f4v', '.f4a', '.3gp', '.3g2', '.3ga', '.ts', '.m2ts', '.mts',
    '.mpg', '.mpeg', '.mpv', '.mpe', '.vob', '.ogv', '.wmv', '.asx',
    '.divx', '.xvid', '.rm', '.rmvb', '.ra', '.ram', '.dat',
}


def content_hash(path: str, head: int = 1 << 20,
                 tail: int = 1 << 20) -> str:
    """
    Hash parsial konten file: ukuran + 1 MB pertama + 1 MB terakhir.
    Cepat (tidak baca seluruh file) tapi praktis unik per konten.
    """
    h = hashlib.sha1()
    size = os.path.getsize(path)
    h.update(str(size).encode('utf-8'))
    with open(path, 'rb') as f:
        first = f.read(head)
        h.update(first)
        remaining = size - len(first)
        if remaining > tail:
            f.seek(-tail, os.SEEK_END)
            h.update(f.read(tail))
        else:
            h.update(f.read(remaining))
    return h.hexdigest()


# Alfabet ID gaya YouTube: huruf besar/kecil + angka (tidak ada karakter
# yang butuh escaping di path / URL).
_ID_ALPHABET = '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'


def _short_id(hex_digest: str, length: int = 11) -> str:
    """Ubah hex digest → ID pendek gaya YouTube (mis. 'Ii1jvubIC8g')."""
    num = int(hex_digest, 16)
    out = []
    while num and len(out) < length:
        num, rem = divmod(num, len(_ID_ALPHABET))
        out.append(_ID_ALPHABET[rem])
    # Padding jika digest terlalu pendek (tidak terjadi untuk sha1)
    while len(out) < length:
        out.append(_ID_ALPHABET[0])
    return ''.join(out)


def song_id_for(path: str) -> str:
    """ID pendek & stabil untuk file: 11 karakter hash konten parsial."""
    return _short_id(content_hash(path))


def _to_int(v) -> int:
    """Konversi durasi (bisa '218.5' / 218 / None) ke int detik."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def probe_media(path: str, timeout: int = 30) -> dict:
    """
    Ambil metadata file via ffprobe.

    Return {title, artist, dur_s, audio} — audio=False jika ffprobe tidak
    menemukan stream audio (file rusak / bukan media).
    """
    result = {'title': '', 'artist': '', 'dur_s': 0, 'audio': False}
    cmd = [FFPROBE, '-v', 'quiet', '-print_format', 'json',
           '-show_format', '-show_streams', path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if r.returncode != 0 or not r.stdout:
            return result
        info = json.loads(r.stdout.decode('utf-8', 'replace'))
    except Exception as e:
        logger.debug("ffprobe gagal %s: %s", path, e)
        return result

    fmt = info.get('format') or {}
    tags = fmt.get('tags') or {}

    for stream in info.get('streams') or []:
        if (stream.get('codec_type') or '').lower() != 'audio':
            continue
        result['audio'] = True
        result['dur_s'] = _to_int(stream.get('duration')) or _to_int(
            fmt.get('duration'))
        stags = stream.get('tags') or {}
        result['title'] = (stags.get('title') or '').strip()
        result['artist'] = (stags.get('artist')
                            or stags.get('album_artist')
                            or stags.get('composer') or '').strip()
        break

    if not result['dur_s']:
        result['dur_s'] = _to_int(fmt.get('duration'))
    if not result['title']:
        result['title'] = (tags.get('title') or '').strip()
    if not result['artist']:
        result['artist'] = (tags.get('artist')
                            or tags.get('album_artist')
                            or tags.get('composer') or '').strip()
    return result


def fmt_duration(d: int | None) -> str:
    if not d:
        return '-'
    return f"{d // 60}:{d % 60:02d}"


def fmt_size(n: int) -> str:
    if not n:
        return '-'
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    return f"{n >> 10} KB"


def scan_folder(folder: str, recursive: bool = True, stop_check=None):
    """
    Generator: scan folder → entri lagu, di-yield per file (progressif,
    bisa dihentikan via stop_check()).

    folder      : path folder sumber
    recursive   : True → sertakan semua subfolder
    stop_check  : callable() -> bool; True → berhenti scan
    """
    root = Path(folder)
    if not root.is_dir():
        return
    if recursive:
        paths = sorted((p for p in root.rglob('*') if p.is_file()))
    else:
        paths = sorted((p for p in root.iterdir() if p.is_file()))

    for p in paths:
        if stop_check and stop_check():
            logger.info("Scan dihentikan oleh user")
            return
        if p.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue

        probe = probe_media(str(p))
        if not probe['audio']:
            logger.debug("skip (tanpa stream audio): %s", p)
            continue

        yield {
            'song_id': song_id_for(str(p)),
            'path': str(p),
            'name': p.name,
            'title': probe['title'] or p.stem,
            'artist': probe['artist'],
            'dur_s': probe['dur_s'],
            'duration_str': fmt_duration(probe['dur_s']),
            'size_bytes': size,
            'local': False,
            'status': '',
        }
