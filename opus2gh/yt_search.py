#!/usr/bin/env python3
"""
Pencarian & download audio dari YouTube via yt-dlp.

Mengikuti pola opusdex.py:
  - cookies.txt opsional (anti-bot YouTube)
  - HTTP headers anti-bot
  - retry settings
"""

import glob
import hashlib
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger('opus2gh.yt')

if getattr(sys, 'frozen', False):
    THIS_DIR = Path(sys.executable).resolve().parent
else:
    THIS_DIR = Path(__file__).resolve().parent.parent
MAX_DURATION_S = 600          # tolak lagu lebih dari 10 menit


def get_cookies_path() -> str | None:
    """
    Cari file cookies.txt untuk yt-dlp (anti-bot YouTube).
    Urutan: folder project/cookies.txt → parent/cookies.txt
    """
    for p in (THIS_DIR / 'cookies.txt', THIS_DIR.parent / 'cookies.txt'):
        if p.exists():
            logger.info("Cookies: %s", p)
            return str(p)
    logger.debug("Tidak ada cookies.txt")
    return None


def build_ytdlp_opts(base_opts: dict) -> dict:
    """Tambahkan cookies + options anti-bot ke yt-dlp opts."""
    opts = dict(base_opts)

    cookies = get_cookies_path()
    if cookies:
        opts['cookiefile'] = cookies

    # Anti-bot headers
    opts['http_headers'] = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Accept-Language': 'id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7',
    }

    # Retry settings
    opts.setdefault('retries', 5)
    opts.setdefault('fragment_retries', 5)
    opts.setdefault('sleep_interval', 1)
    opts.setdefault('max_sleep_interval', 3)

    return opts


def search_youtube(query: str, max_results: int = 20) -> list[dict]:
    """
    Cari lagu di YouTube, return list metadata hasil pencarian.
    Setiap item: {vid_id, title, channel, duration, url, duration_str}
    """
    results = []
    for r in search_youtube_stream(query, max_results):
        results.append(r)
    return results


def search_youtube_stream(query: str, max_results: int = 20,
                          stop_check=None):
    """
    Cari lagu di YouTube — generator, hasil muncul bertahap per halaman
    (20 entri/halaman) tanpa menunggu semua selesai.

    stop_check: callable() -> bool — jika return True, pencarian dihentikan.
    Setiap item: {vid_id, title, channel, duration, duration_str, url}
    """
    import yt_dlp

    base_opts = {
        'format': 'bestaudio[abr<=96]/bestaudio/worstaudio',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': 'in_playlist',   # cepat: tanpa resolve per video
    }
    opts = build_ytdlp_opts(base_opts)

    last_err = None
    search_queries = [
        f"ytsearch{max_results}:{query}",
        f"ytsearch{max_results}:audio {query}",
    ]

    for sq in search_queries:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                # process=False → entries adalah generator lazy,
                # parse halaman demi halaman saat diiterasi
                info = ydl.extract_info(sq, download=False, process=False)
                if not info:
                    continue
                entries = info.get('entries')
                if not entries:
                    continue
                for e in entries:
                    if stop_check and stop_check():
                        logger.info("Search dihentikan oleh user")
                        return
                    if not e:
                        continue
                    duration = e.get('duration') or 0
                    yield {
                        'vid_id': e.get('id', ''),
                        'title': e.get('title', ''),
                        'channel': e.get('uploader', '') or e.get('channel', ''),
                        'duration': duration,
                        'duration_str': _fmt_dur(duration),
                        'url': f"https://www.youtube.com/watch?v={e.get('id', '')}",
                    }
                return  # sukses → tidak perlu fallback query

        except Exception as e:
            last_err = e
            logger.warning("Search '%s' failed: %s", sq, e)
            continue

    raise RuntimeError(f"'{query}' tidak ditemukan di YouTube: {last_err}")


def _fmt_dur(d: int | None) -> str:
    if not d:
        return '-'
    return f"{d // 60}:{d % 60:02d}"


def download_audio(meta: dict, tmp_dir: str, progress_hook=None) -> str:
    """
    Download audio dari YouTube ke tmp_dir.
    Return path file audio yang didownload.
    """
    import yt_dlp

    vid_id = meta['vid_id']
    os.makedirs(tmp_dir, exist_ok=True)

    base_dl_opts = {
        'format': 'bestaudio[abr<=96]/bestaudio/worstaudio',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'outtmpl': os.path.join(tmp_dir, f'{vid_id}.%(ext)s'),
        'retries': 5,
        'fragment_retries': 5,
    }
    if progress_hook:
        base_dl_opts['progress_hooks'] = [progress_hook]

    dl_opts = build_ytdlp_opts(base_dl_opts)

    logger.info("Downloading: %s [%s]", meta.get('title', ''), vid_id)

    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={vid_id}"])

    downloaded = [
        f for f in glob.glob(os.path.join(tmp_dir, f'{vid_id}.*'))
        if not f.endswith('.part')
    ]
    if not downloaded:
        raise RuntimeError("File download tidak ditemukan")

    # Ambil file terbesar (jika ada beberapa)
    downloaded.sort(key=os.path.getsize, reverse=True)
    return downloaded[0]


def fetch_metadata(vid_id: str) -> dict:
    """Ambil metadata satu video berdasarkan video ID."""
    import yt_dlp

    base_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    opts = build_ytdlp_opts(base_opts)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid_id}",
                                download=False)

    duration = info.get('duration') or 0
    return {
        'vid_id': vid_id,
        'title': info.get('title', vid_id),
        'channel': info.get('uploader', '') or info.get('channel', ''),
        'duration': duration,
        'duration_str': _fmt_dur(duration),
        'url': f"https://www.youtube.com/watch?v={vid_id}",
    }


def make_meta(query_fallback: str = '') -> dict:
    """Buat metadata dummy untuk fallback."""
    return {
        'vid_id': hashlib.md5(query_fallback.encode()).hexdigest()[:11],
        'title': query_fallback,
        'channel': '',
        'duration': 0,
        'duration_str': '-',
        'url': '',
    }
