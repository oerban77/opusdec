#!/usr/bin/env python3
"""
opus2gh — YouTube → Opus 16kbps → GitHub

GUI aplikasi (2 tab + player):
  1. Tab Pencarian: cari lagu di YouTube → pilih → Convert ke opus_stream 16 kbps
  2. Tab Lagu Lokal: daftar lagu yang sudah dikonversi di folder music/
  3. Player: putar lagu lokal (opus_stream) atau YouTube online (double-click / tombol ▶)
  4. Pilih lagu (dari tab mana saja) → Export terpilih ke GitHub repo

Format opus_stream (kompatibel xiaozhi-amls905x / mcp_music.py):
  [uint16_be: packet_len][opus_packet_bytes]...
  16 kHz, mono, 60 ms frame (960 samples), 16 kbps

Usage:
  python app.py

Dependencies:
  pip install yt-dlp opuslib
  ffmpeg harus tersedia (di PATH atau lokasi umum)
"""

import json
import logging
import os
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

from opus2gh import settings as settings_mgr
from opus2gh.github_export import (GitHubError, GitHubExporter,
                                   GitHubRepoNotFoundError)
from opus2gh.opus_codec import encode_file
from opus2gh.player import player
from opus2gh.yt_search import download_audio, search_youtube_stream

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('opus2gh')

# ── Path — folder data relatif ke lokasi exe saat frozen ─────
if getattr(sys, 'frozen', False):
    # Berjalan sebagai exe → data di samping exe, bukan di _internal
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

THIS_DIR = BASE_DIR
MUSIC_DIR = BASE_DIR / 'music'
TMP_DIR = MUSIC_DIR / '.tmp'

os.makedirs(MUSIC_DIR, exist_ok=True)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("opus2gh — YouTube → Opus 16kbps → GitHub")
        self.geometry("900x640")
        self.minsize(760, 560)

        # State
        self.settings = settings_mgr.load()
        self.search_results: list[dict] = []
        self.local_songs: list[dict] = []      # lagu lokal di folder music/
        self.converted: dict[str, dict] = {}   # vid_id → {opus_file, meta_file, meta}
        # Sort state
        self.results_sort_key: str | None = None
        self.results_sort_reverse: bool = False
        self.local_sort_key: str | None = None
        self.local_sort_reverse: bool = False
        self.worker: threading.Thread | None = None
        self.busy = False
        self.stop_flag = threading.Event()    # stop pencarian / operasi

        self._build_ui()
        self._refresh_local_list()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI ────────────────────────────────────────────────────
    def _build_ui(self):
        # Header
        header = ttk.Frame(self, padding=(10, 8))
        header.pack(fill='x')
        ttk.Label(header, text="🎵 YouTube → Opus 16 kbps → GitHub",
                 font=('Segoe UI', 13, 'bold')).pack(side='left')
        ttk.Button(header, text="⚙ Settings", command=self._open_settings
                  ).pack(side='right')

        # ── Notebook: tab Pencarian + tab Lagu Lokal ────────────
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=(4, 4))

        # Tab 1: Pencarian YouTube
        search_tab = ttk.Frame(self.notebook, padding=(10, 6))
        self.notebook.add(search_tab, text="🔍 Pencarian YouTube")
        search_tab.columnconfigure(1, weight=1)
        search_tab.rowconfigure(2, weight=1)

        ttk.Label(search_tab, text="Judul / Penyanyi:").grid(
            row=0, column=0, sticky='w')
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(search_tab, textvariable=self.query_var)
        self.query_entry.grid(row=0, column=1, sticky='ew', padx=6)
        self.query_entry.bind('<Return>', lambda e: self._do_search())

        self.search_btn = ttk.Button(search_tab, text="🔍 Cari",
                                     command=self._do_search)
        self.search_btn.grid(row=0, column=2, padx=(6, 0))

        self.stop_btn = ttk.Button(search_tab, text="⏹ Stop",
                                   command=self._do_stop, state='disabled')
        self.stop_btn.grid(row=0, column=3, padx=(6, 0))

        ttk.Label(search_tab, foreground='gray',
                  text="Pilih lagu → Convert. Yang sudah lokal ditandai ✅ "
                       "(ada di tab Lagu Lokal)."
                  ).grid(row=1, column=0, columnspan=4, sticky='w',
                         pady=(4, 2))

        cols = ('title', 'channel', 'duration')
        self.results_tree = ttk.Treeview(search_tab, columns=cols,
                                         show='headings', selectmode='extended')
        # Set up column headings with sort command
        self.results_tree.heading('title', text='Judul',
                                  command=lambda: self._sort_results('title'))
        self.results_tree.heading('channel', text='Channel',
                                  command=lambda: self._sort_results('channel'))
        self.results_tree.heading('duration', text='Durasi',
                                  command=lambda: self._sort_results('duration'))
        self.results_tree.column('title', width=420, anchor='w')
        self.results_tree.column('channel', width=180, anchor='w')
        self.results_tree.column('duration', width=70, anchor='center')
        self.results_tree.grid(row=2, column=0, columnspan=4, sticky='nsew')

        rscroll = ttk.Scrollbar(search_tab, orient='vertical',
                                command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=rscroll.set)
        rscroll.grid(row=2, column=4, sticky='ns')

        self.results_tree.bind('<<TreeviewSelect>>',
                               self._on_selection_changed)
        # Double-click → play lagu (online / lokal jika sudah ada)
        self.results_tree.bind('<Double-Button-1>',
                               self._on_results_double_click)
        # Tab 2: Lagu Lokal
        local_tab = ttk.Frame(self.notebook, padding=(10, 6))
        self.notebook.add(local_tab, text="🎵 Lagu Lokal")
        local_tab.columnconfigure(0, weight=1)
        local_tab.rowconfigure(2, weight=1)

        local_header = ttk.Frame(local_tab)
        local_header.grid(row=0, column=0, columnspan=2, sticky='ew',
                          pady=(0, 2))
        self.play_local_btn = ttk.Button(local_header, text="▶ Putar",
                                         command=self._do_play_local,
                                         state='disabled')
        self.play_local_btn.pack(side='left')
        ttk.Button(local_header, text="🔄 Refresh",
                   command=self._refresh_local_list).pack(side='left',
                                                          padx=(6, 0))
        self.delete_local_btn = ttk.Button(local_header, text="🗑 Hapus",
                                           command=self._delete_local_songs,
                                           state='disabled')
        self.delete_local_btn.pack(side='left', padx=(6, 0))
        self.local_count_var = tk.StringVar(value="0 lagu")
        ttk.Label(local_header, textvariable=self.local_count_var,
                  foreground='gray').pack(side='left', padx=8)

        ttk.Label(local_tab, foreground='gray',
                  text="Lagu yang sudah dikonversi di folder music/ — "
                       "pilih lalu klik Export Terpilih."
                  ).grid(row=1, column=0, columnspan=2, sticky='w',
                         pady=(4, 2))

        lcols = ('title', 'channel', 'duration', 'size')
        self.local_tree = ttk.Treeview(local_tab, columns=lcols,
                                       show='headings', selectmode='extended')
        self.local_tree.heading('title', text='Judul',
                                command=lambda: self._sort_local('title'))
        self.local_tree.heading('channel', text='Channel',
                                command=lambda: self._sort_local('channel'))
        self.local_tree.heading('duration', text='Durasi',
                                command=lambda: self._sort_local('duration'))
        self.local_tree.heading('size', text='Ukuran',
                                command=lambda: self._sort_local('size'))
        self.local_tree.column('title', width=380, anchor='w')
        self.local_tree.column('channel', width=160, anchor='w')
        self.local_tree.column('duration', width=70, anchor='center')
        self.local_tree.column('size', width=80, anchor='e')
        self.local_tree.grid(row=2, column=0, sticky='nsew')

        lscroll = ttk.Scrollbar(local_tab, orient='vertical',
                                command=self.local_tree.yview)
        self.local_tree.configure(yscrollcommand=lscroll.set)
        lscroll.grid(row=2, column=1, sticky='ns')

        self.local_tree.bind('<<TreeviewSelect>>',
                             self._on_local_selection_changed)
        # Double-click → play lagu lokal
        self.local_tree.bind('<Double-Button-1>',
                             self._on_local_double_click)

        # Progress + status
        status_frame = ttk.Frame(self, padding=(10, 4))
        status_frame.pack(fill='x')
        self.convert_btn = ttk.Button(status_frame, text="🎵 Convert Terpilih",
                                      command=self._do_convert_selected,
                                      state='disabled')
        self.convert_btn.pack(side='left')
        ttk.Button(status_frame, text="Pilih Semua",
                   command=self._select_all).pack(side='left', padx=(6, 0))
        self.status_var = tk.StringVar(value="Siap. Cari lagu untuk mulai.")
        ttk.Label(status_frame, textvariable=self.status_var,
                  anchor='w').pack(side='left', fill='x', expand=True, padx=8)
        self.export_btn = ttk.Button(status_frame, text="⬆ Export Terpilih",
                                     command=self._do_export, state='disabled')
        self.export_btn.pack(side='right')
        self.play_stop_btn = ttk.Button(status_frame, text="⏹ Stop Musik",
                                        command=self._do_stop_play,
                                        state='disabled')
        self.play_stop_btn.pack(side='right', padx=(0, 6))

        self.progress = ttk.Progressbar(self, mode='indeterminate')
        self.progress.pack(fill='x', padx=10, pady=(0, 4))

        # Log
        log_frame = ttk.LabelFrame(self, text="Log", padding=(10, 6))
        log_frame.pack(fill='both', padx=10, pady=(4, 10))
        self.log_text = tk.Text(log_frame, height=8, state='disabled',
                                font=('Consolas', 9))
        self.log_text.pack(side='left', fill='both', expand=True)
        lscroll = ttk.Scrollbar(log_frame, orient='vertical',
                                command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lscroll.set)
        lscroll.pack(side='right', fill='y')

    # ── Helpers ───────────────────────────────────────────────
    def log(self, msg: str):
        """Thread-safe append ke log widget."""
        def _append():
            self.log_text.configure(state='normal')
            self.log_text.insert('end', f"{msg}\n")
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        self.after(0, _append)

    def set_status(self, msg: str):
        def _set():
            self.status_var.set(msg)
        self.after(0, _set)

    def set_busy(self, busy: bool, status: str | None = None):
        """Enable/disable tombol saat operasi berjalan."""
        def _set():
            self.busy = busy
            self._update_buttons()
            if status:
                self.status_var.set(status)
            if busy:
                self.progress.start(12)
            else:
                # stop() saja sering meninggalkan bar terisi di mode
                # indeterminate (Windows) → reset value & mode eksplisit
                self.progress.stop()
                self.progress.config(mode='determinate')
                self.progress['value'] = 0
                self.progress.config(mode='indeterminate')
        self.after(0, _set)

    def _update_buttons(self):
        """Update state semua tombol sesuai kondisi (busy, hasil, converted)."""
        self.search_btn.configure(
            state='disabled' if self.busy else 'normal')
        self.stop_btn.configure(
            state='normal' if self.busy else 'disabled')
        self.convert_btn.configure(
            state='disabled' if (self.busy or not self.search_results) else 'normal')
        # Export aktif jika ada lagu lokal atau hasil convert
        has_songs = bool(self.converted) or bool(self.local_songs)
        self.export_btn.configure(
            state='disabled' if (self.busy or not has_songs) else 'normal')
        # Player: tombol stop aktif jika sedang memutar
        self.play_stop_btn.configure(
            state='normal' if player.playing else 'disabled')
        # Tombol ▶ di tab lokal: aktif jika ada selection di tab lokal
        self.play_local_btn.configure(
            state='normal' if self.local_tree.selection() else 'disabled')
        # Tombol 🗑 Hapus di tab lokal: aktif jika ada selection, nonaktif saat sibuk
        has_selection = bool(self.local_tree.selection())
        self.delete_local_btn.configure(
            state='normal' if (has_selection and not self.busy) else 'disabled')

    def run_async(self, fn, stoppable=False):
        """Jalankan fn di background thread dengan busy state.
        stoppable=True → operasi bisa dihentikan via tombol Stop."""
        if self.busy:
            messagebox.showwarning("Sibuk", "Operasi lain sedang berjalan.")
            return
        if stoppable:
            self.stop_flag.clear()
        self.set_busy(True)
        def _run():
            try:
                fn()
            except Exception as e:
                logger.error("❌ %s", e)
                self.log(f"❌ ERROR: {e}")
                if os.environ.get('OPUS2GH_DEBUG'):
                    self.log(traceback.format_exc())
            finally:
                # Selalu reset busy + progress, walau fn() return lebih awal
                # (mis. "semua lagu sudah lokal") — schedule ulang agar pasti
                # diproses main thread walau worker sudah selesai.
                self.busy = False
                self.set_busy(False)
        self.worker = threading.Thread(target=_run, daemon=True)
        self.worker.start()

    def _do_stop(self):
        """Stop pencarian / operasi yang sedang berjalan."""
        self.stop_flag.set()
        self.set_status("⏹ Menghentikan...")
        self.log("⏹ Stop diminta — menghentikan pencarian...")

    # ── Player ──────────────────────────────────────────────────
    def _on_local_selection_changed(self, _event=None):
        """Selection di tab lokal berubah → update tombol ▶ dan status."""
        self._update_buttons()
        self._on_selection_changed(_event)

    def _on_results_double_click(self, _event=None):
        """Double-click hasil pencarian → putar (online, atau lokal jika ada)."""
        sel = self.results_tree.selection()
        if not sel:
            return
        vid_id = sel[0]
        r = next((x for x in self.search_results
                  if x['vid_id'] == vid_id), None)
        if not r:
            return
        if r.get('local'):
            self._play_local_song(vid_id, r)
        else:
            self._play_youtube_song(vid_id, r)

    def _on_local_double_click(self, _event=None):
        """Double-click lagu lokal → putar."""
        sel = self.local_tree.selection()
        if not sel:
            return
        vid_id = sel[0]
        r = next((x for x in self.local_songs
                  if x['vid_id'] == vid_id), None)
        if r:
            self._play_local_song(vid_id, r)

    def _do_play_local(self):
        """Tombol ▶ Putar di tab Lagu Lokal."""
        sel = self.local_tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Pilih lagu lokal dulu "
                                "(klik satu baris).")
            return
        vid_id = sel[0]
        r = next((x for x in self.local_songs
                  if x['vid_id'] == vid_id), None)
        if r:
            self._play_local_song(vid_id, r)

    def _play_local_song(self, vid_id: str, r: dict):
        """Putar lagu lokal (opus_stream) di background thread."""
        opus_file = MUSIC_DIR / f'{vid_id}.opus_stream'
        if not opus_file.exists():
            messagebox.showerror("Error", f"File tidak ditemukan:\n{opus_file}")
            return
        title = r.get('title', vid_id)
        self.log(f"▶ Memutar (lokal): {title}")

        def _on_stop():
            self.after(0, lambda: self._on_play_finished(title))

        def _task():
            try:
                player.play_local(str(opus_file), meta=r, on_stop=_on_stop)
                self.set_status(f"▶ Memutar: {title[:60]}")
                self.after(0, self._update_buttons)
            except Exception as e:
                self.log(f"❌ Play gagal: {e}")
                self.set_status("Play gagal")

        threading.Thread(target=_task, daemon=True).start()

    def _play_youtube_song(self, vid_id: str, r: dict):
        """Putar lagu YouTube online (stream) di background thread."""
        title = r.get('title', vid_id)
        self.log(f"▶ Memutar (YouTube): {title}")
        self.set_status(f"⏳ Resolving stream: {title[:50]}...")

        def _on_stop():
            self.after(0, lambda: self._on_play_finished(title))

        def _task():
            try:
                player.play_youtube(vid_id, meta=r, on_stop=_on_stop)
                self.set_status(f"▶ Memutar (online): {title[:60]}")
                self.after(0, self._update_buttons)
            except Exception as e:
                self.log(f"❌ Play gagal: {e}")
                self.set_status("Play gagal")

        threading.Thread(target=_task, daemon=True).start()

    def _on_play_finished(self, title: str):
        """Callback saat lagu selesai / dihentikan."""
        self.set_status(f"⏹ Selesai memutar: {title[:50]}")
        self._update_buttons()

    def _do_stop_play(self):
        """Tombol ⏹ Stop Musik."""
        player.stop()
        self.log("⏹ Musik dihentikan")
        self._update_buttons()

    def _on_close(self):
        """Tutup aplikasi — hentikan player."""
        try:
            player.stop()
        except Exception:
            pass
        self.destroy()

    # ── Local library ─────────────────────────────────────────
    def _refresh_local_list(self):
        """Refresh tab 'Lagu Lokal' dari folder music/."""
        for iid in self.local_tree.get_children():
            self.local_tree.delete(iid)
        self.local_songs = []

        # Semua opus_stream di folder music/ (meta.json opsional)
        for opus_f in sorted(MUSIC_DIR.glob('*.opus_stream')):
            vid_id = opus_f.stem
            meta = {}
            meta_f = MUSIC_DIR / f'{vid_id}.meta.json'
            if meta_f.exists():
                try:
                    with open(meta_f, encoding='utf-8') as f:
                        meta = json.load(f)
                except Exception:
                    meta = {}
            dur = int(meta.get('dur_s') or 0)
            size_kb = opus_f.stat().st_size // 1024
            self.local_songs.append({
                'vid_id': vid_id,
                'title': meta.get('title', vid_id),
                'channel': meta.get('channel', '-'),
                'duration': dur,
                'duration_str': f"{dur // 60}:{dur % 60:02d}" if dur else '-',
                'size_kb': size_kb,
                'url': f"https://www.youtube.com/watch?v={vid_id}",
                'local': True,
            })

        # Terapkan urutan sort aktif (jika user pernah mengklik header)
        if self.local_sort_key:
            self.local_songs.sort(key=self._local_sort_key,
                                  reverse=self.local_sort_reverse)

        for r in self.local_songs:
            self.local_tree.insert('', 'end', iid=r['vid_id'],
                                   values=(r['title'], r['channel'],
                                           r['duration_str'],
                                           f"{r['size_kb']} KB"))
        n = len(self.local_songs)
        self.notebook.tab(1, text=f"🎵 Lagu Lokal ({n})")
        self.local_count_var.set(f"{n} lagu tersimpan di folder music/")
        if not self.busy:
            self.set_status(f"{n} lagu lokal — pilih lalu Export, "
                            f"atau cari lagu baru")
        self._update_buttons()

    def _delete_local_songs(self):
        """Hapus lagu lokal yang dipilih di tab 'Lagu Lokal'."""
        if self.busy:
            return
        sel = self.local_tree.selection()
        if not sel:
            messagebox.showinfo(
                "Info", "Pilih lagu yang mau dihapus di tab 'Lagu Lokal' dulu "
                "(Ctrl+klik / Shift+klik untuk banyak).")
            return

        names = []
        for vid_id in sel:
            r = next((x for x in self.local_songs
                      if x['vid_id'] == vid_id), None)
            names.append(r['title'] if r else vid_id)
        preview = "\n".join(f"• {n[:50]}" for n in names)
        more = f"\n... dan {len(names) - 5} lainnya" if len(names) > 5 else ""

        # Jika sedang memutar salah satu yang akan dihapus → hentikan dulu
        playing_meta = player.playing_meta
        if playing_meta and playing_meta.get('vid_id') in sel:
            player.stop()
            self.log("⏹ Musik dihentikan (lagu dihapus)")

        if not messagebox.askyesno(
                "Hapus lagu lokal",
                f"Hapus {len(names)} lagu dari folder music/ ?\n\n"
                f"{preview}{more}\n\n"
                "File .opus_stream dan .meta.json akan dihapus permanen."):
            return

        ok, fail = 0, 0
        for vid_id in sel:
            opus_f = MUSIC_DIR / f'{vid_id}.opus_stream'
            meta_f = MUSIC_DIR / f'{vid_id}.meta.json'
            removed = False
            try:
                if opus_f.exists():
                    opus_f.unlink()
                    removed = True
                if meta_f.exists():
                    meta_f.unlink()
                self.converted.pop(vid_id, None)
                self._unmark_local(vid_id)
                ok += 1
            except OSError as e:
                fail += 1
                self.log(f"❌ Gagal hapus {opus_f.name}: {e}")
        self.log(f"🗑 {ok} lagu dihapus" + (f", {fail} gagal" if fail else ""))
        self.set_status(f"🗑 {ok} lagu lokal dihapus"
                        + (f", {fail} gagal" if fail else ""))
        self._refresh_local_list()

    # ── Search ────────────────────────────────────────────────
    def _do_search(self):
        query = self.query_var.get().strip()
        if not query:
            messagebox.showinfo("Info", "Masukkan judul lagu atau nama penyanyi.")
            return

        def _task():
            self.log(f"🔍 Mencari: {query} ...")
            self.set_status(f"Mencari '{query}' di YouTube...")

            # Reset hasil pencarian, hasil baru muncul bertahap
            self.search_results = []
            self.after(0, self._render_results)

            count = 0
            try:
                for r in search_youtube_stream(query, max_results=20,
                                               stop_check=self.stop_flag.is_set):
                    # Tandai yang sudah ada lokal (✅)
                    opus_f = MUSIC_DIR / f"{r['vid_id']}.opus_stream"
                    r['local'] = opus_f.exists()
                    self.search_results.append(r)
                    count += 1
                    # Pertahankan urutan sort yang aktif selama streaming
                    if self.results_sort_key:
                        self.search_results.sort(
                            key=self._results_sort_key,
                            reverse=self.results_sort_reverse)
                    # Render incremental — hasil langsung tampil
                    self.after(0, self._render_results)
                    self.set_status(f"{count} hasil — mencari... (⏹ untuk stop)")
            except Exception as e:
                self.log(f"❌ Pencarian gagal: {e}")
                self.set_status("Pencarian gagal")
                return

            if self.stop_flag.is_set():
                self.set_status(f"⏹ Dihentikan — {count} hasil ditampilkan")
                self.log(f"⏹ Pencarian dihentikan ({count} hasil)")
            else:
                self.set_status(f"{count} hasil untuk '{query}' — "
                                f"pilih lagu lalu klik Convert")
                self.log(f"✅ {count} hasil ditemukan")

        self.run_async(_task, stoppable=True)

    def _render_results(self, keep_selection=True):
        """Render list hasil pencarian.
        keep_selection=True → pertahankan selection user (untuk render incremental)."""
        sel = set(self.results_tree.selection()) if keep_selection else set()

        # Jika sort aktif → bangun ulang seluruh list sesuai urutan sort
        if self.results_sort_key:
            for iid in self.results_tree.get_children():
                self.results_tree.delete(iid)
            for r in self.search_results:
                prefix = "✅ " if r.get('local') else ""
                self.results_tree.insert('', 'end', iid=r['vid_id'],
                                         values=(prefix + r['title'],
                                                 r['channel'],
                                                 r['duration_str']))
        else:
            existing = set(self.results_tree.get_children())

            # Hapus baris yang tidak lagi ada (mis. saat reset pencarian baru)
            for iid in existing - {r['vid_id'] for r in self.search_results}:
                self.results_tree.delete(iid)

            # Insert baris baru di akhir (hasil streaming muncul bertahap)
            for r in self.search_results:
                if r['vid_id'] in existing:
                    continue
                prefix = "✅ " if r.get('local') else ""
                self.results_tree.insert('', 'end', iid=r['vid_id'],
                                         values=(prefix + r['title'],
                                                 r['channel'],
                                                 r['duration_str']))

        # Restore selection
        valid_sel = sel & set(self.results_tree.get_children())
        if valid_sel:
            self.results_tree.selection_set(list(valid_sel))
        self._update_buttons()

    # ── Sort ─────────────────────────────────────────────────
    def _results_sort_key(self, r: dict):
        """Key sort untuk hasil pencarian (durasi numerik, teks case-insensitive)."""
        if self.results_sort_key == 'duration':
            return int(r.get('duration') or 0)
        return str(r.get(self.results_sort_key, '')).lower()

    def _sort_results(self, key: str):
        """Sort hasil pencarian per kolom Judul/Channel/Durasi.
        Klik lagi → toggle naik/turun."""
        if self.results_sort_key == key:
            self.results_sort_reverse = not self.results_sort_reverse
        else:
            self.results_sort_key = key
            self.results_sort_reverse = False
        self.search_results.sort(key=self._results_sort_key,
                                 reverse=self.results_sort_reverse)
        arrow = '↓' if self.results_sort_reverse else '↑'
        self.log(f"↕ Sort hasil {key} {arrow}")
        self._update_sort_headings()
        self._render_results()

    def _local_sort_key(self, r: dict):
        """Key sort untuk lagu lokal (durasi/ukuran numerik, teks case-insensitive)."""
        if self.local_sort_key == 'duration':
            return int(r.get('duration') or 0)
        if self.local_sort_key == 'size':
            return int(r.get('size_kb') or 0)
        return str(r.get(self.local_sort_key, '')).lower()

    def _sort_local(self, key: str):
        """Sort daftar lagu lokal per kolom Judul/Channel/Durasi/Ukuran."""
        if self.local_sort_key == key:
            self.local_sort_reverse = not self.local_sort_reverse
        else:
            self.local_sort_key = key
            self.local_sort_reverse = False
        self.local_songs.sort(key=self._local_sort_key,
                              reverse=self.local_sort_reverse)
        arrow = '↓' if self.local_sort_reverse else '↑'
        self.log(f"↕ Sort lokal {key} {arrow}")
        self._update_sort_headings()
        self._refresh_local_list()

    def _update_sort_headings(self):
        """Tampilkan panah ↑/↓ pada kolom yang sedang di-sort."""
        results_map = {'title': 'Judul', 'channel': 'Channel',
                       'duration': 'Durasi'}
        local_map = {'title': 'Judul', 'channel': 'Channel',
                     'duration': 'Durasi', 'size': 'Ukuran'}
        # Reset semua heading hasil
        for k, text in results_map.items():
            self.results_tree.heading(k, text=text,
                                      command=lambda k=k:
                                      self._sort_results(k))
        if self.results_sort_key:
            text = results_map[self.results_sort_key]
            arrow = '↓' if self.results_sort_reverse else '↑'
            self.results_tree.heading(self.results_sort_key,
                                      text=f"{text} {arrow}",
                                      command=lambda:
                                      self._sort_results(
                                          self.results_sort_key))
        # Reset semua heading lokal
        for k, text in local_map.items():
            self.local_tree.heading(k, text=text,
                                    command=lambda k=k:
                                    self._sort_local(k))
        if self.local_sort_key:
            text = local_map[self.local_sort_key]
            arrow = '↓' if self.local_sort_reverse else '↑'
            self.local_tree.heading(self.local_sort_key,
                                    text=f"{text} {arrow}",
                                    command=lambda:
                                    self._sort_local(
                                        self.local_sort_key))

    # ── Convert ───────────────────────────────────────────────
    def _on_selection_changed(self, _event=None):
        """Update status saat selection berubah (di salah satu tab)."""
        n = (len(self.results_tree.selection())
             + len(self.local_tree.selection()))
        if n == 1:
            self.status_var.set("1 lagu dipilih — Convert atau Export")
        elif n > 1:
            self.status_var.set(f"{n} lagu dipilih — Convert atau Export")

    def _active_tree(self) -> ttk.Treeview:
        """Tree dari tab yang sedang aktif."""
        try:
            if self.notebook.index(self.notebook.select()) == 1:
                return self.local_tree
        except Exception:
            pass
        return self.results_tree

    def _select_all(self):
        """Pilih semua lagu di tab yang aktif."""
        tree = self._active_tree()
        children = tree.get_children()
        if children:
            tree.selection_set(children)

    def _do_convert_selected(self):
        """Convert semua lagu yang dipilih (Ctrl+klik / Shift+klik untuk banyak)."""
        if self.busy:
            return
        sel = self.results_tree.selection()
        if not sel:
            messagebox.showinfo(
                "Info", "Pilih lagu dulu.\n"
                "Klik untuk satu, Ctrl+klik / Shift+klik untuk banyak.")
            return

        selected = [r for r in self.search_results if r['vid_id'] in sel]
        to_convert = [r for r in selected if not r.get('local')]
        already = len(selected) - len(to_convert)

        # Lagu yang sudah lokal → langsung register untuk export
        for r in selected:
            if r.get('local'):
                self._register_local(r['vid_id'], r)
        if already:
            self.log(f"✅ {already} lagu sudah ada lokal (skip download)")

        if not to_convert:
            self.set_status("Semua lagu terpilih sudah dikonversi — siap export")
            return

        total = len(to_convert)

        def _task():
            ok_count = 0
            for i, result in enumerate(to_convert, 1):
                if self.stop_flag.is_set():
                    self.log(f"⏹ Convert dihentikan ({ok_count}/{total} selesai)")
                    break
                self.set_status(f"[{i}/{total}] {result['title'][:50]}")
                try:
                    if self._convert_song(result):
                        ok_count += 1
                except Exception as e:
                    self.log(f"❌ {result['title']}: {e}")
            msg = f"✅ Convert selesai: {ok_count}/{total}"
            if already:
                msg += f" (+{already} sudah ada)"
            self.set_status(msg)
            self.log(f"🎉 {msg}")

        self.run_async(_task, stoppable=True)

    def _convert_song(self, result: dict) -> bool:
        """Download + convert satu lagu ke opus_stream 16 kbps. Return True sukses."""
        vid_id = result['vid_id']
        title = result['title']
        self.set_status(f"⬇ Downloading: {title[:50]}...")
        self.log(f"⬇ Downloading: {title}")

        os.makedirs(TMP_DIR, exist_ok=True)

        def _hook(d):
            if d['status'] == 'downloading':
                pct = d.get('_percent_str', '').strip()
                self.set_status(f"⬇ {pct} — {title[:50]}")

        try:
            src_file = download_audio(result, str(TMP_DIR), progress_hook=_hook)
        except Exception as e:
            err = str(e)
            self.log(f"❌ Download gagal: {err}")
            low = err.lower()
            if 'sign in' in low or 'bot' in low or 'cookies' in low:
                self.log("   YouTube memerlukan verifikasi. Letakkan cookies.txt "
                         "di folder project (export dari browser dengan "
                         "extension 'Get cookies.txt LOCALLY').")
            return False

        # Convert ke opus_stream
        self.set_status(f"🎵 Converting ke Opus 16 kbps: {title[:50]}...")
        self.log(f"🎵 Convert ke opus_stream 16 kbps...")
        opus_file = MUSIC_DIR / f"{vid_id}.opus_stream"
        ok = encode_file(src_file, str(opus_file), str(TMP_DIR))
        if not ok:
            self.log("❌ Convert gagal (ffmpeg/opuslib error)")
            return False

        # Simpan metadata
        meta = {
            'title': title,
            'channel': result.get('channel', ''),
            'dur_s': int(result.get('duration') or 0),
        }
        meta_file = MUSIC_DIR / f'{vid_id}.meta.json'
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        # Bersihkan tmp
        try:
            os.remove(src_file)
        except OSError:
            pass

        size_kb = opus_file.stat().st_size // 1024
        dur = meta['dur_s']
        self.log(f"✅ Saved: {opus_file.name} ({size_kb} KB, "
                 f"{dur // 60}:{dur % 60:02d})")
        self.set_status(f"✅ {title[:50]} — siap di-export")

        # Register untuk export
        self.converted[vid_id] = {
            'opus_file': str(opus_file),
            'meta_file': str(meta_file),
            'meta': meta,
        }
        self.after(0, lambda: self._mark_local(vid_id))
        return True

    def _register_local(self, vid_id: str, result: dict):
        """Register lagu lokal (sudah ada) untuk export."""
        opus_file = MUSIC_DIR / f'{vid_id}.opus_stream'
        meta_file = MUSIC_DIR / f'{vid_id}.meta.json'
        if opus_file.exists():
            self.converted[vid_id] = {
                'opus_file': str(opus_file),
                'meta_file': str(meta_file),
                'meta': {
                    'title': result['title'],
                    'channel': result.get('channel', ''),
                    'dur_s': int(result.get('duration') or 0),
                },
            }
            self.after(0, self._update_buttons)

    def _mark_local(self, vid_id: str):
        """Update tanda ✅ di list pencarian + refresh tab Lagu Lokal."""
        for r in self.search_results:
            if r['vid_id'] == vid_id:
                r['local'] = True
        for iid in self.results_tree.get_children():
            if iid == vid_id:
                vals = self.results_tree.item(iid, 'values')
                title = vals[0]
                if not title.startswith('✅'):
                    self.results_tree.item(iid, values=('✅ ' + title, *vals[1:]))
        self._refresh_local_list()

    def _unmark_local(self, vid_id: str):
        """Hilangkan tanda ✅ di list pencarian (lagu lokal dihapus)."""
        for r in self.search_results:
            if r['vid_id'] == vid_id:
                r['local'] = False
        for iid in self.results_tree.get_children():
            if iid == vid_id:
                vals = self.results_tree.item(iid, 'values')
                title = vals[0]
                if title.startswith('✅ '):
                    self.results_tree.item(iid,
                                           values=(title[2:], *vals[1:]))

    # ── Export ────────────────────────────────────────────────
    def _song_record(self, vid_id: str) -> dict | None:
        """Record export untuk vid_id: dari converted, atau dari file
        lokal (opus_stream + meta.json) di folder music/."""
        if vid_id in self.converted:
            return self.converted[vid_id]
        opus_file = MUSIC_DIR / f'{vid_id}.opus_stream'
        if not opus_file.exists():
            return None
        meta_file = MUSIC_DIR / f'{vid_id}.meta.json'
        meta = {}
        if meta_file.exists():
            try:
                with open(meta_file, encoding='utf-8') as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        else:
            # meta.json hilang → buat ulang minimal agar upload tetap jalan
            r = next((x for x in self.search_results + self.local_songs
                      if x['vid_id'] == vid_id), None)
            meta = {
                'title': (r['title'] if r else vid_id),
                'channel': (r.get('channel', '') if r else ''),
                'dur_s': int(r.get('duration') or 0) if r else 0,
            }
            try:
                with open(meta_file, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            except OSError:
                pass
        return {
            'opus_file': str(opus_file),
            'meta_file': str(meta_file),
            'meta': meta,
        }

    def _do_export(self):
        if self.busy:
            return
        # Selection gabungan dari kedua tab (pencarian + lagu lokal)
        sel = set(self.results_tree.selection()) | set(
            self.local_tree.selection())
        if not sel:
            messagebox.showinfo(
                "Info", "Pilih lagu yang mau di-export dulu.\n"
                "Klik untuk satu, Ctrl+klik / Shift+klik untuk banyak.\n"
                "Lagu di tab 'Lagu Lokal' bisa langsung di-export.")
            return

        cfg = self.settings
        if not (cfg.get('github_token') and cfg.get('github_owner')
                and cfg.get('github_repo')):
            messagebox.showwarning(
                "Settings belum lengkap",
                "Isi GitHub token, owner, dan repo di Settings dulu.")
            self._open_settings()
            return

        # Bangun daftar export dari selection: lagu lokal (✅) langsung
        # siap, hasil cari harus sudah pernah dikonversi
        songs = []
        for vid_id in sel:
            rec = self._song_record(vid_id)
            if rec:
                songs.append(rec)
            else:
                r = next((x for x in self.search_results + self.local_songs
                          if x['vid_id'] == vid_id), None)
                title = r['title'] if r else vid_id
                self.log(f"⏭ Skip '{title[:40]}' — belum dikonversi "
                         f"(convert dulu)")
        if not songs:
            self.set_status("Tidak ada lagu siap export — convert dulu")
            return
        n = len(songs)

        def _task():
            exporter = GitHubExporter(
                token=cfg['github_token'],
                owner=cfg['github_owner'],
                repo=cfg['github_repo'],
                branch=cfg.get('github_branch', 'main'),
            )
            repo_dir = cfg.get('repo_dir', 'music')
            ok_count = 0
            skip_count = 0
            for i, song in enumerate(songs, 1):
                title = song['meta'].get('title', '?')
                self.set_status(f"⬆ Export {i}/{n}: {title[:50]}...")
                self.log(f"⬆ Export: {title}")
                try:
                    result = exporter.upload_song(
                        song['opus_file'], song['meta_file'],
                        repo_dir=repo_dir,
                        commit_message=f"music: add {Path(song['opus_file']).stem}",
                    )
                    if result.get('skipped'):
                        self.log(f"⏭ Sudah ada (identik) → "
                                 f"{result['opus_path']}")
                        skip_count += 1
                    else:
                        self.log(f"✅ Uploaded → {result['opus_path']}")
                        ok_count += 1
                except Exception as e:
                    self.log(f"❌ Export gagal: {e}")

            # Sinkronkan catalog.json di repo (daftar semua lagu di repo)
            if ok_count:
                try:
                    self.set_status("📋 Sinkron catalog.json...")
                    cat = exporter.sync_catalog(
                        repo_dir=repo_dir,
                        catalog_path=f"{repo_dir}/catalog.json",
                        local_file=str(BASE_DIR / 'catalog.json'),
                    )
                    self.log(f"📋 catalog.json: {cat['count']} lagu "
                             f"tersinkron")
                except Exception as e:
                    self.log(f"⚠ Sinkron catalog gagal: {e}")

            self.set_status(
                f"✅ {ok_count} baru / {skip_count} skip / {n} dipilih → "
                f"{cfg['github_owner']}/{cfg['github_repo']}")
            self.log(f"🎉 Export selesai: {ok_count} baru, {skip_count} "
                     f"skip (sudah ada), {n} dipilih")
            if ok_count:
                url = (f"https://github.com/{cfg['github_owner']}/"
                       f"{cfg['github_repo']}/tree/{cfg.get('github_branch', 'main')}/{repo_dir}")
                self.log(f"🔗 {url}")

        self.run_async(_task)

    # ── Settings ───────────────────────────────────────────────
    def _open_settings(self):
        dialog = SettingsDialog(self, self.settings)
        self.wait_window(dialog)
        if dialog.result:
            self.settings = settings_mgr.save(dialog.result)
            self.log("⚙ Settings tersimpan")


class SettingsDialog(tk.Toplevel):
    """Dialog settings GitHub credentials + test koneksi."""

    def __init__(self, parent, current: dict):
        super().__init__(parent)
        self.title("Settings — GitHub")
        self.geometry("560x480")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: dict | None = None
        self.testing = False

        frm = ttk.Frame(self, padding=16)
        frm.pack(fill='both', expand=True)

        # Token
        ttk.Label(frm, text="Personal Access Token (PAT):",
                  font=('Segoe UI', 10, 'bold')).grid(
            row=0, column=0, columnspan=2, sticky='w', pady=(0, 2))
        self.token_var = tk.StringVar(value=current.get('github_token', ''))
        self.token_entry = ttk.Entry(frm, textvariable=self.token_var,
                                     show='•')
        self.token_entry.grid(row=1, column=0, sticky='ew', pady=(0, 8))
        ttk.Label(frm, text="(scope: repo — github.com/settings/tokens)",
                  foreground='gray').grid(row=2, column=0, sticky='w')

        # Owner
        ttk.Label(frm, text="Owner / Username:",
                  font=('Segoe UI', 10, 'bold')).grid(
            row=3, column=0, sticky='w', pady=(8, 2))
        self.owner_var = tk.StringVar(value=current.get('github_owner', ''))
        ttk.Entry(frm, textvariable=self.owner_var).grid(
            row=4, column=0, sticky='ew', pady=(0, 8))

        # Repo
        ttk.Label(frm, text="Repository:",
                  font=('Segoe UI', 10, 'bold')).grid(
            row=5, column=0, sticky='w', pady=(0, 2))
        self.repo_var = tk.StringVar(value=current.get('github_repo', ''))
        ttk.Entry(frm, textvariable=self.repo_var).grid(
            row=6, column=0, sticky='ew', pady=(0, 8))

        # Branch
        ttk.Label(frm, text="Branch:",
                  font=('Segoe UI', 10, 'bold')).grid(
            row=7, column=0, sticky='w', pady=(0, 2))
        self.branch_var = tk.StringVar(
            value=current.get('github_branch', 'main'))
        ttk.Entry(frm, textvariable=self.branch_var).grid(
            row=8, column=0, sticky='ew', pady=(0, 8))

        # Repo dir
        ttk.Label(frm, text="Folder di repo:",
                  font=('Segoe UI', 10, 'bold')).grid(
            row=9, column=0, sticky='w', pady=(0, 2))
        self.dir_var = tk.StringVar(value=current.get('repo_dir', 'music'))
        ttk.Entry(frm, textvariable=self.dir_var).grid(
            row=10, column=0, sticky='ew', pady=(0, 10))

        frm.columnconfigure(0, weight=1)

        # Test connection
        test_frame = ttk.Frame(frm)
        test_frame.grid(row=11, column=0, sticky='ew', pady=(0, 4))
        self.test_btn = ttk.Button(test_frame, text="🔌 Test Koneksi",
                                   command=self._do_test)
        self.test_btn.pack(side='left')
        self.test_result = tk.Text(test_frame, height=4, width=44,
                                  font=('Consolas', 9), state='disabled',
                                  background=self['background'])
        self.test_result.pack(side='left', padx=(8, 0), fill='both')

        # Buttons
        btns = ttk.Frame(frm)
        btns.grid(row=12, column=0, sticky='ew', pady=(10, 0))
        ttk.Button(btns, text="Simpan", command=self._save).pack(
            side='right', padx=(6, 0))
        ttk.Button(btns, text="Batal", command=self._cancel).pack(
            side='right')

        # Petunjuk
        help_text = (
            "Cara membuat token:\n"
            "  1. Buka github.com/settings/tokens (Fine-grained atau classic)\n"
            "  2. Classic: centang scope 'repo' → Generate\n"
            "  3. Fine-grained: pilih repo → Contents: Read and write"
        )
        ttk.Label(frm, text=help_text, foreground='gray',
                  justify='left').grid(row=13, column=0, sticky='w',
                                       pady=(10, 0))

    def _set_test_result(self, msg: str, ok: bool | None = None):
        self.test_result.configure(state='normal')
        self.test_result.delete('1.0', 'end')
        tag = ('ok' if ok else 'err') if ok is not None else None
        if tag:
            self.test_result.tag_configure(tag, foreground=(
                '#0a7d24' if ok else '#c62828'))
            self.test_result.insert('end', msg, tag)
        else:
            self.test_result.insert('end', msg)
        self.test_result.configure(state='disabled')

    def _do_test(self):
        if self.testing:
            return
        token = self.token_var.get().strip()
        owner = self.owner_var.get().strip()
        repo = self.repo_var.get().strip()
        branch = self.branch_var.get().strip() or 'main'

        if not (token and owner and repo):
            self._set_test_result(
                "Isi token, owner, dan repo dulu sebelum test.", False)
            return

        self.testing = True
        self.test_btn.configure(state='disabled')
        self._set_test_result("⏳ Menguji koneksi...")

        def _run():
            try:
                exporter = GitHubExporter(token, owner, repo, branch)
                info = exporter.test_connection()
                self.after(0, lambda: self._set_test_result(
                    f"✅ {info['message']}", True))
            except GitHubRepoNotFoundError as e:
                # Repo belum ada → tawarkan buat otomatis
                def _offer_create():
                    self._set_test_result(f"❌ {e}", False)
                    answer = messagebox.askyesno(
                        "Repo tidak ditemukan",
                        f"Repo '{owner}/{repo}' belum ada di GitHub.\n\n"
                        f"Buat repo ini sekarang otomatis?\n"
                        f"(private, dengan README awal agar branch siap)",
                        parent=self)
                    if answer:
                        self._create_repo(token, owner, repo, branch)
                self.after(0, _offer_create)
            except GitHubError as e:
                self.after(0, lambda: self._set_test_result(f"❌ {e}", False))
            except Exception as e:
                self.after(0, lambda: self._set_test_result(f"❌ {e}", False))
            finally:
                self.after(0, self._finish_test)

        threading.Thread(target=_run, daemon=True).start()

    def _create_repo(self, token: str, owner: str, repo: str, branch: str):
        """Buat repo baru via API lalu test ulang koneksinya."""
        self._set_test_result("⏳ Membuat repo...")

        def _run():
            try:
                exporter = GitHubExporter(token, owner, repo, branch)
                result = exporter.create_repo(private=True, auto_init=True)
                full_name = result.get('full_name', f'{owner}/{repo}')
                default_branch = result.get('default_branch', branch)
                msg = (f"✅ Repo dibuat: {full_name} "
                       f"(branch default: '{default_branch}')")
                self.after(0, lambda: self._set_test_result(msg, True))
                # Jika branch default beda dengan input user, samakan
                if default_branch and default_branch != branch:
                    self.after(0, lambda: self.branch_var.set(default_branch))
            except GitHubError as e:
                self.after(0, lambda: self._set_test_result(
                    f"❌ Gagal membuat repo: {e}", False))
            except Exception as e:
                self.after(0, lambda: self._set_test_result(
                    f"❌ Gagal membuat repo: {e}", False))

        threading.Thread(target=_run, daemon=True).start()

    def _finish_test(self):
        self.testing = False
        self.test_btn.configure(state='normal')

    def _collect(self) -> dict:
        return {
            'github_token': self.token_var.get().strip(),
            'github_owner': self.owner_var.get().strip(),
            'github_repo': self.repo_var.get().strip(),
            'github_branch': self.branch_var.get().strip() or 'main',
            'repo_dir': self.dir_var.get().strip() or 'music',
        }

    def _save(self):
        self.result = self._collect()
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
