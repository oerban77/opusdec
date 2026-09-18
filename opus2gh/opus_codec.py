#!/usr/bin/env python3
"""
Konversi audio → opus_stream (kompatibel xiaozhi-amls905x / mcp_music.py).

Pipeline (mengikuti pola opusdex.py / mcp_music.py):
  1. Download audio YouTube via yt-dlp (bestaudio)
  2. ffmpeg decode → PCM s16le 16 kHz mono + loudnorm
  3. Encode PCM → Opus 16 kbps, frame 60 ms (960 samples)
  4. Tulis format opus_stream: [uint16_be: packet_len][opus_packet]...

Dependencies: yt-dlp, opuslib (fallback: ffmpeg libopus + OGG extract)
"""

import glob
import logging
import os
import platform
import shutil
import struct
import subprocess
from pathlib import Path

logger = logging.getLogger('opus2gh.codec')

# ── Konfigurasi codec ─────────────────────────────────────────
OPUS_BITRATE = 16000          # 16 kbps (sesuai permintaan)
OPUS_SR = 16000               # 16 kHz mono
FRAME_SAMPLES = 960           # 60 ms @ 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2


def find_ffmpeg() -> str:
    """Cari executable ffmpeg di lokasi umum, fallback ke PATH."""
    this_dir = Path(__file__).resolve().parent.parent
    system = platform.system()
    if system == 'Windows':
        candidates = [
            r'D:\master\ffmpeg-8.1-full_build\bin\ffmpeg.exe',
            r'D:\master\tools\ffmpeg\bin\ffmpeg.exe',
            r'C:\ffmpeg\bin\ffmpeg.exe',
            r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
            str(this_dir / 'ffmpeg' / 'ffmpeg.exe'),
        ]
    else:
        candidates = [
            '/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg',
            '/opt/homebrew/bin/ffmpeg',
            str(this_dir / 'ffmpeg' / 'ffmpeg'),
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return shutil.which('ffmpeg') or 'ffmpeg'


FFMPEG = find_ffmpeg()


def _find_opus_dll_dirs() -> list[str]:
    """Cari folder yang berisi opus.dll / libopus (untuk opuslib via ctypes)."""
    this_dir = Path(__file__).resolve().parent.parent
    candidates = [
        str(this_dir / 'ffmpeg' / 'bin'),
        str(this_dir / 'opus'),
        r'D:\master\tools\wireshark-portable\app',
        r'D:\master\tools\FreeCAD_weekly-2025.08.13-Windows-x86_64-py311\bin',
        r'C:\ffmpeg\bin',
        r'C:\Program Files\ffmpeg\bin',
    ]
    found = []
    for d in candidates:
        if os.path.isfile(os.path.join(d, 'opus.dll')):
            found.append(d)
    return found


def _preload_opus_dll():
    """Tambah folder opus.dll ke PATH sebelum import opuslib (Windows only)."""
    if platform.system() != 'Windows':
        return
    for d in _find_opus_dll_dirs():
        if d not in os.environ.get('PATH', ''):
            os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
        logger.debug("opus.dll dir ditambahkan ke PATH: %s", d)


def decode_to_pcm(src_file: str, pcm_file: str) -> bool:
    """Decode audio apa pun → PCM s16le 16 kHz mono + loudness normalization."""
    cmd = [
        FFMPEG, '-y', '-i', src_file,
        '-vn', '-map_metadata', '-1',
        '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11',
        '-ar', str(OPUS_SR), '-ac', '1', '-f', 's16le', pcm_file,
    ]
    logger.info("Decode ke PCM %d Hz mono (loudnorm)...", OPUS_SR)
    r = subprocess.run(cmd, capture_output=True, timeout=600)
    ok = (r.returncode == 0 and os.path.exists(pcm_file)
          and os.path.getsize(pcm_file) > 0)
    if not ok:
        logger.error("ffmpeg PCM error: %s",
                     r.stderr.decode('utf-8', 'replace')[:300])
    return ok


def pcm_to_opus_stream(pcm_file: str, opus_file: str) -> bool:
    """
    Encode PCM s16le 16 kHz mono → opus_stream.
    Prioritas: opuslib → ffmpeg ogg extract.
    """
    # Prioritas 1: opuslib
    try:
        _preload_opus_dll()
        import opuslib
        encoder = opuslib.Encoder(OPUS_SR, 1, opuslib.APPLICATION_AUDIO)
        encoder.bitrate = OPUS_BITRATE
        with open(pcm_file, 'rb') as fin, open(opus_file, 'wb') as fout:
            while True:
                data = fin.read(FRAME_BYTES)
                if not data:
                    break
                if len(data) < FRAME_BYTES:
                    data += b'\x00' * (FRAME_BYTES - len(data))
                pkt = encoder.encode(data, FRAME_SAMPLES)
                fout.write(struct.pack('>H', len(pkt)))
                fout.write(pkt)
        logger.info("Encoded via opuslib")
        return True
    except ImportError:
        logger.debug("opuslib tidak tersedia, fallback ke ffmpeg")
    except Exception as e:
        logger.warning("opuslib encode error: %s", e)

    # Prioritas 2: ffmpeg → ogg → extract raw opus packets
    try:
        ogg_file = opus_file.replace('.opus_stream', '.ogg')
        cmd = [
            FFMPEG, '-y',
            '-f', 's16le', '-ar', str(OPUS_SR), '-ac', '1', '-i', pcm_file,
            '-c:a', 'libopus', '-b:a', f'{OPUS_BITRATE // 1000}k',
            '-frame_duration', '60', ogg_file,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=600)
        if r.returncode == 0 and os.path.exists(ogg_file):
            extract_opus_from_ogg(ogg_file, opus_file)
            os.remove(ogg_file)
            logger.info("Encoded via ffmpeg+ogg")
            return True
    except Exception as e:
        logger.warning("ffmpeg ogg encode error: %s", e)

    return False


def extract_opus_from_ogg(ogg_path: str, out_path: str):
    """
    Extract raw Opus packets dari container OGG.
    Setiap packet ditulis: [uint16_be: len][packet_bytes]
    Skip header OpusHead/OpusTags.
    """
    with open(ogg_path, 'rb') as f, open(out_path, 'wb') as out:
        while True:
            capture = f.read(4)
            if len(capture) < 4 or capture != b'OggS':
                break
            f.read(22)  # header + granule pos + serial + seq
            n_segs = struct.unpack('B', f.read(1))[0]
            seg_table = list(struct.unpack(f'{n_segs}B', f.read(n_segs)))
            pkt_data = b''
            for seg_size in seg_table:
                pkt_data += f.read(seg_size)
                if seg_size < 255:  # lacing value < 255 = akhir packet
                    if (pkt_data and not pkt_data.startswith(b'OpusHead')
                            and not pkt_data.startswith(b'OpusTags')):
                        out.write(struct.pack('>H', len(pkt_data)))
                        out.write(pkt_data)
                    pkt_data = b''


def encode_file(src_file: str, opus_file: str, tmp_dir: str) -> bool:
    """Pipeline lengkap: file audio apa pun → opus_stream. Return True sukses."""
    os.makedirs(tmp_dir, exist_ok=True)
    pcm_file = os.path.join(tmp_dir, 'encode.pcm')
    try:
        if not decode_to_pcm(src_file, pcm_file):
            return False
        return pcm_to_opus_stream(pcm_file, opus_file)
    finally:
        cleanup(pcm_file)
        for f in glob.glob(os.path.join(tmp_dir, 'encode.ogg')):
            cleanup(f)


def cleanup(*files):
    """Hapus file jika ada, abaikan error."""
    for f in files:
        try:
            if f and os.path.exists(f):
                os.remove(f)
        except Exception:
            pass
