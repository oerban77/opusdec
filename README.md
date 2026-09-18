# opus2gh — YouTube → Opus 16 kbps → GitHub

Aplikasi GUI untuk mencari lagu di YouTube, mengonversinya ke format
**opus_stream 16 kbps** (kompatibel dengan `xiaozhi-amls905x` / `mcp_music.py`),
lalu mengekspornya beserta metadata-nya ke GitHub repository.

## Fitur

- 🔍 **Cari lagu di YouTube** — masukkan judul lagu atau nama penyanyi,
  hasil pencarian ditampilkan dalam list
- 🎵 **Convert ke Opus 16 kbps** — pilih lagu dari list, otomatis
  didownload & dikonversi (16 kHz mono, frame 60 ms)
- ⬆ **Export ke GitHub** — lagu (`.opus_stream`) + metadata (`.meta.json`)
  di-commit ke repo via GitHub REST API
- ☁ **Repo GitHub tab** — kelola repo musik langsung dari aplikasi:
  - 🔄 **Daftar Lagu di Repo** — ambil semua lagu + metadata dari repo,
    yang sudah ada di lokal ditandai ✅
  - 🗑 **Hapus dari Repo** — hapus `.opus_stream` + `.meta.json` terpilih,
    `catalog.json` di-sync ulang otomatis
  - ⬇ **Import ke Lokal** — download ulang lagu dari repo ke `music/`
  - 📋 **Sync Catalog** — bangun ulang `catalog.json` (di root repo) dari
    semua lagu di folder `music/`
- ⚙ **Settings** — kredensial GitHub (PAT, owner, repo, branch) dengan
  tombol **Test Koneksi**

## Format opus_stream

```
[uint16_be: packet_len][opus_packet_bytes][uint16_be: packet_len]...
```

- 16 kHz, mono, 60 ms frame (960 samples), **16 kbps**
- Kompatibel dengan player `mcp_music.py` di xiaozhi-amls905x

## Instalasi

```bash
pip install -r requirements.txt
```

**ffmpeg** harus tersedia — otomatis dicari di lokasi umum
(`D:\master\ffmpeg-8.1-full_build\bin`, `C:\ffmpeg\bin`, PATH) atau
letakkan di subfolder `ffmpeg/`.

## Menjalankan

```bash
python app.py
```

## Setup GitHub

1. Buat repository di GitHub (public/private)
2. Buat Personal Access Token:
   - **Classic**: github.com/settings/tokens → centang scope `repo`
   - **Fine-grained**: pilih repo → permission *Contents: Read and write*
3. Buka **⚙ Settings** di aplikasi → isi token, owner, repo, branch →
   klik **🔌 Test Koneksi** → Simpan

## Struktur file

```
app.py                  # GUI utama (Tkinter)
opus2gh/
  yt_search.py          # Pencarian & download YouTube (yt-dlp)
  opus_codec.py         # Konversi PCM → opus_stream 16 kbps
  github_export.py      # Export ke GitHub via REST API
  settings.py           # Settings manager (settings.json)
music/                  # Cache lokal lagu hasil konversi
  <video_id>.opus_stream
  <video_id>.meta.json
```

## Anti-bot YouTube

Jika pencarian/download gagal karena verifikasi YouTube, letakkan
`cookies.txt` di folder project (export dari browser Chrome/Firefox
menggunakan extension **"Get cookies.txt LOCALLY"**).

## Build menjadi satu file EXE (standalone)

Semua dependency — Python, modul `opus2gh`, `ffmpeg`/`ffplay`/`ffprobe`
+ DLL-nya, dan `opus.dll` untuk opuslib — di-bundle menjadi **satu** file
`opus2gh.exe` tanpa folder `_internal`:

```bash
build_onefile.bat
```

Hasil: `dist\opus2gh.exe` (~138 MB). File ini bisa di-copy ke folder mana
saja dan dijalankan langsung di Windows tanpa install apa pun.

Yang **tetap di luar** exe (dibuat otomatis di samping exe saat dijalankan):

| File/Folder | Isi |
|---|---|
| `settings.json` | Konfigurasi GitHub (token, owner, repo, branch) |
| `music/` | Cache lagu hasil konversi |
| `catalog.json` | Hasil sync katalog repo (berada di **root repo**, bukan di folder `music/`) |
| `cookies.txt` | Opsional, anti-bot YouTube |

> Jika lokasi `ffmpeg` atau `opus.dll` berbeda, set environment variable
> `FFMPEG_BIN` dan `OPUS_DLL` sebelum build, atau edit path di
> `opus2gh_onefile.spec`.

## Catatan

- `settings.json` berisi token GitHub — **jangan commit file ini**.
  Tambahkan ke `.gitignore` jika project ini sendiri di-git.
