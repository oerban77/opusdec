#!/usr/bin/env python3
"""
Player untuk opus2gh:
  - Lagu lokal (opus_stream) → decode → WAV temp → putar via ffplay
  - Lagu YouTube online → resolve URL stream via yt-dlp → putar via ffplay

Player berjalan sebagai subprocess ffplay (daemon thread memantau).
"""

import logging
import os
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from opus2gh.opus_codec import FFMPEG, OPUS_SR

logger = logging.getLogger('opus2gh.player')

THIS_DIR = Path(__file__).resolve().parent.parent
if getattr(sys, 'frozen', False):
    THIS_DIR = Path(sys.executable).resolve().parent
FFPLAY = FFMPEG.replace('ffmpeg', 'ffplay')


def _find_ffplay() -> str:
    """Cari ffplay di folder yang sama dengan ffmpeg (termasuk saat frozen)."""
    d = os.path.dirname(FFMPEG)
    for name in ('ffplay.exe', 'ffplay'):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    # Saat frozen, binary di-extract ke _MEIPASS/bin
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        for name in ('ffplay.exe', 'ffplay'):
            p = os.path.join(meipass, 'bin', name)
            if os.path.isfile(p):
                return p
    return 'ffplay'


FFPLAY = _find_ffplay()


class Player:
    """Wrapper player ffplay — satu lagu pada satu waktu."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._watcher: threading.Thread | None = None
        self._on_stop_cb = None
        self._tmp_wav: str | None = None
        self._playing_meta: dict | None = None

    # ── Public API ────────────────────────────────────────────
    @property
    def playing(self) -> bool:
        """True jika sedang memutar lagu."""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def playing_meta(self) -> dict | None:
        return self._playing_meta

    def play_local(self, opus_file: str, meta: dict | None = None,
                   on_stop=None):
        """Putar file opus_stream lokal: decode → WAV temp → ffplay."""
        self.stop()
        wav = self._decode_to_wav(opus_file)
        if not wav:
            raise RuntimeError(f"Gagal decode '{opus_file}'")
        self._playing_meta = meta
        self._on_stop_cb = on_stop
        self._start_ffplay(wav, is_temp=True)

    def play_youtube(self, vid_id: str, meta: dict | None = None,
                     on_stop=None):
        """Putar lagu YouTube online: resolve stream URL → ffplay."""
        self.stop()
        url = self._resolve_stream_url(vid_id)
        self._playing_meta = meta
        self._on_stop_cb = on_stop
        self._start_ffplay(url, is_temp=False)

    def stop(self):
        """Hentikan player."""
        # Ambil referensi ke proses dan watcher thread di dalam lock
        with self._lock:
            proc = self._proc
            watcher = self._watcher
            self._proc = None
            self._watcher = None
            self._playing_meta = None
            self._on_stop_cb = None
        
        # Hentikan proses ffplay jika masih berjalan
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
        
        # Tunggu watcher thread selesai (maksimal 0.5 detik)
        if watcher and watcher.is_alive():
            watcher.join(timeout=0.5)
        
        self._cleanup_tmp()

    # ── Internal ───────────────────────────────────────────────
    def _start_ffplay(self, src: str, is_temp: bool):
        """Start ffplay subprocess + watcher thread."""
        cmd = [
            FFPLAY,
            '-nodisp',              # tanpa window video
            '-autoexit',            # exit saat selesai
            '-loglevel', 'quiet',
            '-hide_banner',
            src,
        ]
        with self._lock:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            if is_temp:
                self._tmp_wav = src

        self._watcher = threading.Thread(target=self._watch_proc,
                                         daemon=True)
        self._watcher.start()

    def _watch_proc(self):
        """Pantau proses ffplay — panggil callback saat selesai/stop."""
        proc = self._proc
        if not proc:
            return
        proc.wait()
        cb = self._on_stop_cb
        self._on_stop_cb = None
        self._cleanup_tmp()
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _decode_to_wav(self, opus_file: str) -> str | None:
        """Decode opus_stream → WAV temp (via opuslib atau ffmpeg)."""
        # Prioritas 1: opuslib (decode native)
        try:
            from opus2gh.opus_codec import _preload_opus_dll
            _preload_opus_dll()
            import opuslib

            decoder = opuslib.Decoder(OPUS_SR, 1)
            fd, wav_path = tempfile.mkstemp(suffix='.wav',
                                            prefix='opus2gh_play_')
            os.close(fd)

            import wave
            with open(opus_file, 'rb') as fin, \
                    wave.open(wav_path, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(OPUS_SR)
                while True:
                    hdr = fin.read(2)
                    if len(hdr) < 2:
                        break
                    (pkt_len,) = struct.unpack('>H', hdr)
                    pkt = fin.read(pkt_len)
                    if len(pkt) < pkt_len:
                        break
                    pcm = decoder.decode(pkt, 960 * 2)
                    wav.writeframes(pcm)
            return wav_path
        except ImportError:
            pass
        except Exception as e:
            logger.warning("opuslib decode gagal: %s — fallback ffmpeg", e)

        # Prioritas 2: ffmpeg — opus_stream bukan container standar,
        # jadi decode manual: baca frame, pipe PCM ke ffmpeg → WAV
        try:
            return self._decode_via_ffmpeg(opus_file)
        except Exception as e:
            logger.error("ffmpeg decode gagal: %s", e)
            return None

    def _decode_via_ffmpeg(self, opus_file: str) -> str | None:
        """Decode opus_stream via ffmpeg: extract packets → raw opus → WAV."""
        # Ekstrak semua packet opus ke file raw (tanpa header)
        packets = []
        with open(opus_file, 'rb') as f:
            while True:
                hdr = f.read(2)
                if len(hdr) < 2:
                    break
                (pkt_len,) = struct.unpack('>H', hdr)
                pkt = f.read(pkt_len)
                if len(pkt) < pkt_len:
                    break
                packets.append(pkt)

        if not packets:
            return None

        # Gabungkan packet → satu file raw opus (annexb-like)
        fd, raw_path = tempfile.mkstemp(suffix='.opusraw',
                                        prefix='opus2gh_')
        os.close(fd)
        with open(raw_path, 'wb') as f:
            for pkt in packets:
                f.write(pkt)

        # ffmpeg bisa decode raw opus? Tidak langsung — perlu OGG container.
        # Buat OGG page manual terlalu kompleks; gunakan pendekatan lain:
        # encode ulang via opuslib sudah gagal, jadi gunakan ffmpeg dengan
        # format 'opus' (asumsi self-framing opus stream).
        wav_path = raw_path.replace('.opusraw', '.wav')
        cmd = [
            FFMPEG, '-y',
            '-f', 'opus', '-i', raw_path,
            '-ar', str(OPUS_SR), '-ac', '1',
            wav_path,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=120)
        try:
            os.remove(raw_path)
        except OSError:
            pass
        if r.returncode == 0 and os.path.exists(wav_path):
            return wav_path
        logger.error("ffmpeg opus decode error: %s",
                     r.stderr.decode('utf-8', 'replace')[:300])
        return None

    def _resolve_stream_url(self, vid_id: str) -> str:
        """Resolve URL stream audio YouTube via yt-dlp."""
        from opus2gh.yt_search import build_ytdlp_opts

        import yt_dlp
        opts = build_ytdlp_opts({
            'format': 'bestaudio[abr<=96]/bestaudio/worstaudio',
            'quiet': True,
            'no_warnings': True,
        })
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={vid_id}",
                download=False,
            )
        url = info.get('url')
        if not url:
            raise RuntimeError("URL stream tidak ditemukan")
        return url

    def _cleanup_tmp(self):
        """Hapus WAV temp setelah selesai."""
        wav = self._tmp_wav
        self._tmp_wav = None
        if wav:
            try:
                os.remove(wav)
            except OSError:
                pass


# ── Instance global ───────────────────────────────────────────
player = Player()
