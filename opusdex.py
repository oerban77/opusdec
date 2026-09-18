#!/usr/bin/env python3
"""
opusdex — YouTube Audio Downloader → Opus Stream Encoder

Download audio dari YouTube berdasarkan judul lagu, lalu decode/encode
menjadi format opus_stream dan simpan di folder ./music.

Format opus_stream (kompatibel dengan mcp_music.py / xiaozhi-amls905x):
  [uint16_be: packet_len][opus_packet_bytes][uint16_be: packet_len]...
  - 16 kHz, mono, 60 ms frame (960 samples), 32 kbps

Usage:
  python opusdex.py "judul lagu"           # cari lokal dulu, download jika belum ada
  python opusdex.py --force "judul lagu"   # download ulang walau sudah ada
  python opusdex.py --list                 # daftar lagu di folder music
  python opusdex.py --info                 # info ukuran cache
  python opusdex.py --clean                # evict cache melebihi batas

Dependencies:
  pip install yt-dlp opuslib
  ffmpeg harus tersedia (di PATH atau lokasi umum)
"""

import argparse
import glob
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('opusdex')

# ── Konfigurasi ───────────────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent
MUSIC_DIR = THIS_DIR / 'music'
TMP_DIR = MUSIC_DIR / '.tmp'

CACHE_MAX_MB = 500
CACHE_MAX_BYTES = CACHE_MAX_MB * 1024 * 1024
MAX_DURATION_S = 600          # tolak lagu lebih dari 10 menit
OPUS_BITRATE = 32000          # 32 kbps
OPUS_SR = 16000                # 16 kHz mono
FRAME_SAMPLES = 960           # 60 ms @ 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2

# Kata-kata umum yang bukan bagian dari nama lagu (stop words + command words)
_SKIP_WORDS = {
    'putar', 'main', 'play', 'lagu', 'song', 'musik', 'music', 'dari', 'from',
    'untuk', 'for', 'oleh', 'by', 'diputarkan', 'diputar', 'playing',
    'nyalakan', 'hidupkan', 'jalankan', 'run', 'start', 'dong', 'yuk',
    'kita', 'we', 'saya', 'i', 'aku', 'kamu', 'you', 'dia', 'him', 'her',
    'download', 'unduh', 'unduhkan', 'downloadkan', 'simpan', 'save',
}


# ── Util ───────────────────────────────────────────────────────
def find_ffmpeg() -> str:
    """Cari executable ffmpeg di lokasi umum, fallback ke PATH."""
    system = platform.system()
    if system == 'Windows':
        candidates = [
            r'D:\master\ffmpeg-8.1-full_build\bin\ffmpeg.exe',
            r'C:\ffmpeg\bin\ffmpeg.exe',
            r'C:\Program Files\ffmpeg\bin\ffmpeg.exe',
            str(THIS_DIR / 'ffmpeg' / 'ffmpeg.exe'),
        ]
    else:
        candidates = [
            '/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg',
            '/opt/homebrew/bin/ffmpeg',
            str(THIS_DIR / 'ffmpeg' / 'ffmpeg'),
        ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return shutil.which('ffmpeg') or 'ffmpeg'


FFMPEG = find_ffmpeg()


def get_cookies_path() -> str | None:
    """
    Cari file cookies.txt untuk yt-dlp (anti-bot YouTube).
    Urutan: argumen --cookies → opusdex dir/cookies.txt → parent/cookies.txt
    """
    # 1. Dari argumen CLI (diset via global _OPT_COOKIES)
    if _OPT_COOKIES:
        p = os.path.abspath(_OPT_COOKIES)
        if os.path.exists(p):
            logger.info(f"🍪 Cookies dari argumen: {p}")
            return p
        logger.warning(f"⚠️  cookies '{p}' tidak ditemukan")

    # 2. Folder script
    p = THIS_DIR / 'cookies.txt'
    if p.exists():
        logger.info(f"🍪 Cookies: {p}")
        return str(p)

    # 3. Parent folder
    p = (THIS_DIR.parent / 'cookies.txt').resolve()
    if p.exists():
        logger.info(f"🍪 Cookies: {p}")
        return str(p)

    logger.debug("🍪 Tidak ada cookies.txt")
    return None


_OPT_COOKIES: str | None = None  # di-set dari argparse di main()


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


def normalize(s: str) -> str:
    """Normalisasi judul untuk matching: lowercase, buang tanda baca."""
    return re.sub(r'[^\w\s]', '', s.lower()).strip()


def match_query(query: str, title: str, channel: str = '') -> bool:
    """
    Cek apakah query cocok dengan judul+artis — fleksibel.
    Filter stop words, lalu cek kata signifikan ada di title/channel.
    """
    q_words = [w for w in normalize(query).split() if w not in _SKIP_WORDS]
    if not q_words:
        return False

    target = normalize(f"{title} {channel}")
    matches = sum(1 for w in q_words if w in target)
    if len(q_words) <= 2:
        return matches >= 1
    return matches >= 2  # jika banyak kata, butuh minimal 2 cocok


# ── Pencarian lokal ───────────────────────────────────────────
def find_local(query: str) -> Path | None:
    """
    Cari file opus_stream lokal yang cocok dengan query.
    Prioritas: metadata .meta.json (judul+artis), lalu nama file.
    """
    q_filtered = ' '.join(w for w in normalize(query).split() if w not in _SKIP_WORDS)

    # 1. Metadata (paling akurat)
    for meta_f in glob.glob(str(MUSIC_DIR / '*.meta.json')):
        try:
            with open(meta_f, encoding='utf-8') as f:
                meta = json.load(f)
            title = meta.get('title', '')
            channel = meta.get('channel', '')
            if match_query(query, title, channel):
                vid_id = os.path.basename(meta_f)[:-len('.meta.json')]
                opus_file = MUSIC_DIR / f'{vid_id}.opus_stream'
                if opus_file.exists():
                    return opus_file
        except Exception:
            pass

    # 2. Fallback: nama file (mp3, m4a, dll)
    for ext in ('*.opus_stream', '*.opus', '*.mp3', '*.m4a', '*.ogg', '*.flac', '*.wav'):
        for f in glob.glob(str(MUSIC_DIR / ext)):
            name = normalize(os.path.splitext(os.path.basename(f))[0])
            if q_filtered and q_filtered in name:
                return Path(f)

    return None


# ── YouTube: search & download ────────────────────────────────
def fetch_metadata(query: str) -> dict:
    """Cari lagu di YouTube berdasarkan judul, return metadata."""
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("yt-dlp tidak terinstall. Jalankan: pip install yt-dlp")

    base_opts = {
        'format': 'bestaudio[abr<=96]/bestaudio/worstaudio',
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
    }
    opts = build_ytdlp_opts(base_opts)

    last_err = None
    search_queries = [
        f"ytsearch1:{query}",
        f"ytsearch3:{query}",  # fallback: ambil lebih banyak lalu pilih pertama
    ]

    for sq in search_queries:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(sq, download=False)
            if not info:
                continue

            entries = info.get('entries', [info])
            entry = next((e for e in entries if e), None)
            if not entry:
                continue

            title = entry.get('title', query)
            duration = entry.get('duration', 0) or 0
            vid_id = entry.get('id', hashlib.md5(query.encode()).hexdigest()[:8])

            if duration > MAX_DURATION_S:
                raise RuntimeError(
                    f"Durasi terlalu panjang ({duration // 60} menit, "
                    f"maks {MAX_DURATION_S // 60} menit). Coba query lebih spesifik."
                )

            return {
                'vid_id': vid_id,
                'title': title,
                'channel': entry.get('uploader', ''),
                'duration': duration,
                'opus_file': MUSIC_DIR / f"{vid_id}.opus_stream",
            }

        except RuntimeError:
            raise
        except Exception as e:
            last_err = e
            logger.warning(f"⚠️  Search '{sq}' failed: {e}")
            continue

    raise RuntimeError(f"'{query}' tidak ditemukan di YouTube: {last_err}")


def _progress_hook(d):
    """Tampilkan progress download yt-dlp satu baris."""
    if d['status'] == 'downloading':
        pct = d.get('_percent_str', '').strip()
        speed = d.get('_speed_str', '').strip()
        eta = d.get('_eta_str', '').strip()
        sys.stdout.write(f"\r⬇️  {pct} @ {speed} (ETA {eta})   ")
        sys.stdout.flush()
    elif d['status'] == 'finished':
        sys.stdout.write("\r⬇️  Download selesai.                    \n")


def do_download_encode(meta: dict) -> Path:
    """
    Download audio dari YouTube → PCM 16kHz mono (ffmpeg + loudnorm)
    → encode ke opus_stream → simpan di MUSIC_DIR + metadata.
    """
    import yt_dlp

    vid_id = meta['vid_id']
    opus_file = Path(meta['opus_file'])
    os.makedirs(TMP_DIR, exist_ok=True)

    try:
        base_dl_opts = {
            'format': 'bestaudio[abr<=96]/bestaudio/worstaudio',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'outtmpl': str(TMP_DIR / f'{vid_id}.%(ext)s'),
            'retries': 5,
            'fragment_retries': 5,
            'progress_hooks': [_progress_hook],
        }
        dl_opts = build_ytdlp_opts(base_dl_opts)

        logger.info(f"🎵 Downloading: {meta['title']} [{vid_id}]")

        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={vid_id}"])

        downloaded = [
            f for f in glob.glob(str(TMP_DIR / f'{vid_id}.*'))
            if not f.endswith('.part')
        ]
        if not downloaded:
            raise RuntimeError("File download tidak ditemukan")
        src_file = downloaded[0]
        logger.info(f"🎵 Downloaded: {os.path.basename(src_file)}")

        # ── Decode ke PCM 16kHz mono + loudness normalization ──
        pcm_file = TMP_DIR / f'{vid_id}.pcm'
        cmd_pcm = [
            FFMPEG, '-y', '-i', src_file,
            '-vn', '-map_metadata', '-1',
            '-af', 'loudnorm=I=-14:TP=-1.5:LRA=11',
            '-ar', str(OPUS_SR), '-ac', '1', '-f', 's16le', str(pcm_file),
        ]
        logger.info(f"🎵 Decode ke PCM {OPUS_SR}Hz mono (loudnorm)...")
        r = subprocess.run(cmd_pcm, capture_output=True, timeout=300)
        if r.returncode != 0 or not pcm_file.exists() or pcm_file.stat().st_size == 0:
            raise RuntimeError(
                f"ffmpeg PCM error: {r.stderr.decode('utf-8', 'replace')[:200]}"
            )

        # ── Encode PCM → opus_stream ──
        logger.info("🎵 Encode ke opus_stream...")
        ok = pcm_to_opus_stream(str(pcm_file), str(opus_file))
        if not ok or not opus_file.exists() or opus_file.stat().st_size < 100:
            raise RuntimeError("Gagal encode ke opus_stream")

        # ── Simpan metadata ──
        meta_file = MUSIC_DIR / f'{vid_id}.meta.json'
        try:
            with open(meta_file, 'w', encoding='utf-8') as mf:
                json.dump({
                    'title': meta['title'],
                    'channel': meta['channel'],
                    'dur_s': meta['duration'],
                }, mf, ensure_ascii=False, indent=2)
        except Exception:
            pass

        size_kb = opus_file.stat().st_size // 1024
        dur = int(meta['duration'])
        logger.info(
            f"✅ Saved: {opus_file.name} ({size_kb}KB, "
            f"{dur // 60}:{dur % 60:02d}) → {opus_file}"
        )
        evict_cache()
        return opus_file

    except Exception as e:
        err_str = str(e)
        logger.error(f"❌ Download error [{vid_id}]: {err_str}")

        # Deteksi error bot/auth dan beri pesan informatif
        low = err_str.lower()
        if 'sign in' in low or 'bot' in low or 'cookies' in low:
            logger.error(
                "YouTube memerlukan verifikasi. Letakkan cookies.txt di folder "
                "script atau parent folder. Export dari browser Chrome/Firefox "
                "menggunakan extension 'Get cookies.txt LOCALLY'."
            )
        raise

    finally:
        cleanup(TMP_DIR / f'{vid_id}.pcm')
        for f in glob.glob(str(TMP_DIR / f'{vid_id}.*')):
            cleanup(f)


# ── Encoder opus_stream ───────────────────────────────────────
def pcm_to_opus_stream(pcm_file: str, opus_file: str) -> bool:
    """
    Encode PCM s16le 16kHz mono → opus_stream.
    Prioritas: opuslib → ffmpeg ogg extract.
    """
    # Prioritas 1: opuslib
    try:
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
        logger.info("🎵 Encoded via opuslib")
        return True
    except ImportError:
        logger.debug("opuslib tidak tersedia, fallback ke ffmpeg")
    except Exception as e:
        logger.warning(f"opuslib encode error: {e}")

    # Prioritas 2: ffmpeg → ogg → extract raw opus packets
    try:
        ogg_file = opus_file.replace('.opus_stream', '.ogg')
        cmd = [
            FFMPEG, '-y',
            '-f', 's16le', '-ar', str(OPUS_SR), '-ac', '1', '-i', pcm_file,
            '-c:a', 'libopus', '-b:a', f'{OPUS_BITRATE // 1000}k',
            '-frame_duration', '60', ogg_file,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode == 0 and os.path.exists(ogg_file):
            extract_opus_from_ogg(ogg_file, opus_file)
            os.remove(ogg_file)
            logger.info("🎵 Encoded via ffmpeg+ogg")
            return True
    except Exception as e:
        logger.warning(f"ffmpeg ogg encode error: {e}")

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


# ── Cache management ──────────────────────────────────────────
def cleanup(*files):
    """Hapus file jika ada, abaikan error."""
    for f in files:
        try:
            if f and os.path.exists(f):
                os.remove(f)
        except Exception:
            pass


def evict_cache():
    """Hapus file opus_stream + meta tertua jika total melebihi CACHE_MAX_BYTES."""
    files = []
    for f in glob.glob(str(MUSIC_DIR / '*.opus_stream')):
        try:
            files.append((os.path.getmtime(f), os.path.getsize(f), f))
        except OSError:
            pass
    total = sum(s for _, s, _ in files)
    if total <= CACHE_MAX_BYTES:
        return

    files.sort()  # tertua dulu
    freed = 0
    for mtime, size, opus_path in files:
        if total - freed <= CACHE_MAX_BYTES:
            break
        vid_id = os.path.basename(opus_path)[:-len('.opus_stream')]
        try:
            os.remove(opus_path)
            freed += size
            logger.info(f"🗑️  Evicted: {os.path.basename(opus_path)}")
        except OSError:
            pass
        cleanup(os.path.join(MUSIC_DIR, f'{vid_id}.meta.json'))


def list_music() -> list[dict]:
    """Daftar semua lagu di folder music (opus_stream + format lain)."""
    items = []
    for f in sorted(glob.glob(str(MUSIC_DIR / '*.opus_stream'))):
        vid_id = os.path.basename(f)[:-len('.opus_stream')]
        size_mb = os.path.getsize(f) / (1024 * 1024)
        meta = {}
        meta_f = MUSIC_DIR / f'{vid_id}.meta.json'
        if meta_f.exists():
            try:
                with open(meta_f, encoding='utf-8') as mf:
                    meta = json.load(mf)
            except Exception:
                pass
        dur_s = int(meta.get('dur_s', 0))
        items.append({
            'filename': os.path.basename(f),
            'title': meta.get('title', vid_id),
            'channel': meta.get('channel', '-'),
            'duration': f"{dur_s // 60}:{dur_s % 60:02d}" if dur_s else '-',
            'size_mb': f"{size_mb:.1f}",
        })
    for ext in ('*.mp3', '*.m4a', '*.ogg', '*.flac', '*.wav', '*.opus'):
        for f in sorted(glob.glob(str(MUSIC_DIR / ext))):
            name = os.path.splitext(os.path.basename(f))[0].replace('_', ' ')
            items.append({
                'filename': os.path.basename(f),
                'title': name,
                'channel': '-',
                'duration': '-',
                'size_mb': f"{os.path.getsize(f) / (1024 * 1024):.1f}",
            })
    return items


def cache_info() -> str:
    total = sum(os.path.getsize(f)
                for f in glob.glob(str(MUSIC_DIR / '*.opus_stream'))
                if os.path.isfile(f))
    pct = total / CACHE_MAX_BYTES * 100
    return f"{total // 1024 // 1024}MB / {CACHE_MAX_BYTES // 1024 // 1024}MB ({pct:.0f}%)"


# ── Main ──────────────────────────────────────────────────────
def download(query: str, force: bool = False) -> Path | None:
    """
    Entry point utama: cari lokal dulu (berdasarkan judul), jika belum ada
    → download dari YouTube → encode opus_stream → simpan di music/.
    """
    os.makedirs(MUSIC_DIR, exist_ok=True)

    if not force:
        local = find_local(query)
        if local:
            logger.info(f"✅ Sudah ada lokal: {local.name}")
            return local

    meta = fetch_metadata(query)
    vid_id = meta['vid_id']

    # Jika file sudah ada (match by vid_id), skip download
    if not force and os.path.exists(meta['opus_file']) \
            and os.path.getsize(meta['opus_file']) > 1000:
        logger.info(f"✅ Sudah ada lokal: {os.path.basename(meta['opus_file'])}")
        return Path(meta['opus_file'])

    return do_download_encode(meta)


def main() -> int:
    global _OPT_COOKIES, MUSIC_DIR, CACHE_MAX_BYTES, MAX_DURATION_S

    parser = argparse.ArgumentParser(
        prog='opusdex',
        description='Download audio YouTube berdasarkan judul → encode opus_stream → simpan di folder music',
    )
    parser.add_argument('query', nargs='*', help='judul lagu / query pencarian')
    parser.add_argument('-m', '--music-dir', default=str(MUSIC_DIR),
                        help=f'folder output (default: {MUSIC_DIR})')
    parser.add_argument('--max-duration', type=int, default=MAX_DURATION_S,
                        help=f'durasi maksimal detik (default: {MAX_DURATION_S})')
    parser.add_argument('--cache-max-mb', type=int, default=CACHE_MAX_MB,
                        help=f'batas cache MB (default: {CACHE_MAX_MB})')
    parser.add_argument('--cookies', default=None,
                        help='path ke cookies.txt untuk yt-dlp (anti-bot)')
    parser.add_argument('--force', action='store_true',
                        help='download ulang walau sudah ada di lokal')
    parser.add_argument('--list', action='store_true', help='daftar lagu di folder music')
    parser.add_argument('--info', action='store_true', help='info ukuran cache')
    parser.add_argument('--clean', action='store_true', help='evict cache melebihi batas')
    parser.add_argument('-v', '--verbose', action='store_true', help='log debug')
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    MUSIC_DIR = Path(args.music_dir).resolve()
    CACHE_MAX_BYTES = args.cache_max_mb * 1024 * 1024
    _OPT_COOKIES = args.cookies
    MAX_DURATION_S = args.max_duration

    try:
        if args.list:
            items = list_music()
            if not items:
                print(f"Folder music kosong: {MUSIC_DIR}")
                return 0
            print(f"🎵 Music folder: {MUSIC_DIR}  ({len(items)} lagu)\n")
            print(f"{'TITLE':<40} {'CHANNEL':<20} {'DUR':>5} {'MB':>6}  FILE")
            print('-' * 100)
            for it in items:
                print(f"{it['title'][:38]:<40} {it['channel'][:18]:<20} "
                      f"{it['duration']:>5} {it['size_mb']:>6}  {it['filename']}")
            return 0

        if args.info:
            print(f"📦 Cache: {cache_info()}")
            return 0

        if args.clean:
            evict_cache()
            print(f"🧹 Setelah clean: {cache_info()}")
            return 0

        if not args.query:
            parser.print_help()
            return 1

        query = ' '.join(args.query)
        result = download(query, force=args.force)
        if result:
            print(f"\n🎉 Selesai: {result}")
            return 0
        return 1

    except KeyboardInterrupt:
        print("\n⏹️  Dibatalkan")
        return 130
    except Exception as e:
        logger.error(f"❌ {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
