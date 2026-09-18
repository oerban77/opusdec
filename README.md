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

## Catatan

- `settings.json` berisi token GitHub — **jangan commit file ini**.
  Tambahkan ke `.gitignore` jika project ini sendiri di-git.
