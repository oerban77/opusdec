#!/usr/bin/env python3
"""
Export lagu (opus_stream + metadata) ke GitHub repository via GitHub REST API.

Menggunakan Personal Access Token (PAT) — tidak perlu git binary.
Operasi:
  - test_connection : verifikasi kredensial & akses repo
  - list_repo_songs : daftar lagu yang sudah tersimpan di repo
  - upload_song     : upload/commit file opus_stream + .meta.json
  - delete_song     : hapus file dari repo
"""

import base64
import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger('opus2gh.github')

API_BASE = 'https://api.github.com'


class GitHubError(Exception):
    """Error koneksi / API GitHub. code = HTTP status code (jika ada)."""

    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code


class GitHubRepoNotFoundError(GitHubError):
    """Repo tidak ditemukan (404) — belum dibuat, atau token tanpa akses."""


class GitHubExporter:
    def __init__(self, token: str, owner: str, repo: str, branch: str = 'main'):
        self.token = token.strip()
        self.owner = owner.strip()
        self.repo = repo.strip()
        self.branch = branch.strip() or 'main'

    # ── Low-level API ────────────────────────────────────────
    def _api(self, method: str, path: str, body: dict | None = None,
             expect_json: bool = True) -> dict | int:
        # path kosong → jangan tambah trailing slash (GitHub API 404
        # untuk /repos/{owner}/{repo}/)
        url = f"{API_BASE}/repos/{self.owner}/{self.repo}"
        if path:
            url += f"/{path}"
        return self._request(method, url, body, expect_json)

    def _request(self, method: str, url: str, body: dict | None = None,
                 expect_json: bool = True) -> dict | int:
        data = None
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'opus2gh',
        }
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                if expect_json:
                    return json.loads(raw)
                return resp.status
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode('utf-8', 'replace')
                msg = json.loads(err_body).get('message', err_body)
            except Exception:
                msg = str(e)
            raise GitHubError(f"HTTP {e.code}: {msg}", code=e.code) from e
        except urllib.error.URLError as e:
            raise GitHubError(f"Koneksi gagal: {e.reason}") from e

    # ── Public API ───────────────────────────────────────────
    def whoami(self) -> str:
        """Verifikasi token → return login username."""
        req = urllib.request.Request(
            f"{API_BASE}/user",
            headers={
                'Authorization': f'Bearer {self.token}',
                'Accept': 'application/vnd.github+json',
                'User-Agent': 'opus2gh',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read()).get('login', '')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise GitHubError(
                    "Token tidak valid atau kadaluarsa (401/403). "
                    "Pastikan token punya scope 'repo'.",
                    code=e.code,
                ) from e
            raise GitHubError(f"HTTP {e.code} saat cek token",
                              code=e.code) from e
        except urllib.error.URLError as e:
            raise GitHubError(f"Koneksi gagal: {e.reason}") from e

    def test_connection(self) -> dict:
        """
        Test koneksi & kredensial: token → repo → branch.
        Return {ok, user, repo_full, repo_private, default_branch, message}
        Raise GitHubRepoNotFoundError jika repo belum ada / tanpa akses.
        """
        # 1. Verifikasi token
        login = self.whoami()

        # 2. Verifikasi repo
        try:
            repo_info = self._api('GET', '')
        except GitHubError as e:
            if e.code == 404:
                raise GitHubRepoNotFoundError(
                    f"Repo '{self.owner}/{self.repo}' tidak ditemukan (404). "
                    f"Repo belum dibuat, atau token tidak punya akses ke "
                    f"repo ini."
                ) from e
            raise GitHubError(
                f"Token valid (user: {login}), tapi repo "
                f"'{self.owner}/{self.repo}' tidak bisa diakses: {e}",
                code=e.code,
            ) from e

        # 3. Verifikasi branch
        default_branch = repo_info.get('default_branch', 'main')
        try:
            self._api('GET', f'branches/{self.branch}')
        except GitHubError as e:
            if e.code == 404:
                raise GitHubError(
                    f"Token & repo OK, tapi branch '{self.branch}' tidak "
                    f"ada (branch default repo: '{default_branch}'). "
                    f"Perbaiki kolom Branch di settings. Jika repo baru "
                    f"dibuat tanpa initial commit, branch belum ada — "
                    f"centang 'Add a README' saat membuat repo."
                ) from e
            raise

        return {
            'ok': True,
            'user': login,
            'repo_full': f"{self.owner}/{self.repo}",
            'repo_private': repo_info.get('private', False),
            'default_branch': default_branch,
            'message': (
                f"Berhasil! Login sebagai {login}, repo "
                f"'{self.owner}/{self.repo}' "
                f"({'private' if repo_info.get('private') else 'public'}), "
                f"branch '{self.branch}' OK"
            ),
        }

    def create_repo(self, private: bool = True, auto_init: bool = True) -> dict:
        """
        Buat repo baru via API.
        - owner == user login → POST /user/repos
        - owner lain (org)    → POST /orgs/{owner}/repos
        auto_init=True → commit awal (README) agar branch default langsung ada.
        """
        try:
            login = self.whoami()
        except GitHubError:
            login = None

        if login and login.lower() == self.owner.lower():
            url = f"{API_BASE}/user/repos"
        else:
            url = f"{API_BASE}/orgs/{self.owner}/repos"

        body = {
            'name': self.repo,
            'private': private,
            'auto_init': auto_init,
            'description': 'opus_stream music library (opus2gh)',
        }
        result = self._request('POST', url, body)
        logger.info("Repo dibuat: %s",
                    result.get('full_name', f'{self.owner}/{self.repo}'))
        return result

    def _get_ref(self) -> dict | None:
        """Ambil HEAD commit sha dari branch, None jika branch belum ada."""
        try:
            return self._api('GET', f'branches/{self.branch}')
        except GitHubError:
            return None

    def _ensure_repo_ready(self) -> dict:
        """Pastikan repo ada & branch valid. Return info branch."""
        branch_info = self._get_ref()
        if branch_info is None:
            # Cek default branch repo
            repo_info = self._api('GET', '')
            default = repo_info.get('default_branch', 'main')
            if default == self.branch:
                raise GitHubError(
                    f"Branch '{self.branch}' tidak ditemukan di repo"
                )
            # Branch berbeda dari default tapi tidak ada → error jelas
            raise GitHubError(
                f"Branch '{self.branch}' tidak ditemukan "
                f"(default: '{default}')"
            )
        return branch_info

    def list_repo_songs(self, music_dir: str = 'music') -> list[dict]:
        """
        Daftar lagu di repo (folder music_dir).
        Return list {filename, title, channel, dur_s, size_kb, sha}
        """
        self._ensure_repo_ready()
        tree = self._api('GET', f'git/trees/{self.branch}?recursive=1')

        songs = []
        for item in tree.get('tree', []):
            path = item.get('path', '')
            if not path.startswith(f'{music_dir}/'):
                continue
            if item.get('type') != 'blob':
                continue
            if path.endswith('.opus_stream'):
                vid_id = path.split('/')[-1][:-len('.opus_stream')]
                songs.append({
                    'vid_id': vid_id,
                    'path': path,
                    'sha': item.get('sha', ''),
                    'size_kb': (item.get('size', 0) or 0) // 1024,
                    'title': vid_id,
                    'channel': '',
                    'dur_s': 0,
                })
        return songs

    # ── Catalog ─────────────────────────────────────────────
    def build_catalog(self, repo_dir: str = 'music') -> dict:
        """
        Bangun catalog dari SEMUA opus_stream di folder repo_dir.
        Metadata (title/artist/dur_s) diambil dari .meta.json yang ada di
        repo; jika meta tidak ada → fallback: title=vid_id, artist='', 0.

        Return {"version":1, "count":N, "tracks":[{file,title,artist,dur_s}]}
        """
        self._ensure_repo_ready()
        tree = self._api('GET', f'git/trees/{self.branch}?recursive=1')
        if tree.get('truncated'):
            logger.warning("Tree repo terpotong (terlalu besar) — "
                           "catalog mungkin tidak lengkap")

        prefix = f'{repo_dir}/'
        streams: list[str] = []
        metas: dict[str, str] = {}   # vid_id -> blob sha
        for item in tree.get('tree', []):
            path = item.get('path', '')
            if not path.startswith(prefix) or item.get('type') != 'blob':
                continue
            name = path[len(prefix):]
            if name.endswith('.opus_stream'):
                streams.append(name[:-len('.opus_stream')])
            elif name.endswith('.meta.json'):
                metas[name[:-len('.meta.json')]] = item.get('sha', '')

        tracks = []
        for vid_id in sorted(streams):
            title, artist, dur_s = vid_id, '', 0
            sha = metas.get(vid_id)
            if sha:
                try:
                    blob = self._api('GET', f'git/blobs/{sha}')
                    raw = base64.b64decode(blob.get('content', '') or '')
                    meta = json.loads(raw.decode('utf-8'))
                    title = meta.get('title') or vid_id
                    artist = (meta.get('artist')
                              or meta.get('channel') or '')
                    dur_s = int(meta.get('dur_s')
                                or meta.get('duration') or 0)
                except Exception as e:
                    logger.warning("Gagal baca meta %s: %s", vid_id, e)
            tracks.append({
                'file': vid_id,
                'title': title,
                'artist': artist,
                'dur_s': dur_s,
            })
        return {'version': 1, 'count': len(tracks), 'tracks': tracks}

    def sync_catalog(self, repo_dir: str = 'music',
                     catalog_path: str = 'catalog.json',
                     commit_message: str | None = None,
                     local_file: str | None = None) -> dict:
        """
        Sinkronkan catalog.json di repo agar sesuai dengan semua lagu
        yang ada di folder repo_dir (hapus/ubah/tambah ikut repo).

        local_file: jika diisi, salinan catalog juga ditulis ke file lokal.

        Return {ok, catalog_path, count, commit_sha}
        """
        catalog = self.build_catalog(repo_dir)
        payload = json.dumps(catalog, ensure_ascii=False, indent=2)

        if local_file:
            import os
            os.makedirs(os.path.dirname(os.path.abspath(local_file)),
                        exist_ok=True)
            with open(local_file, 'w', encoding='utf-8') as f:
                f.write(payload)

        # Tulis ke temp file untuk di-upload via _put_contents
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix='.json', prefix='catalog_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(payload)
            commit_sha = self._put_contents(
                catalog_path, tmp,
                commit_message or f"catalog: sync {catalog['count']} tracks")
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

        logger.info("catalog.json di-sync: %s lagu → %s",
                    catalog['count'], catalog_path)
        return {
            'ok': True,
            'catalog_path': catalog_path,
            'count': catalog['count'],
            'commit_sha': commit_sha,
        }

    def _get_repo_blob(self, repo_path: str) -> bytes | None:
        """Ambil konten file di repo. None jika belum ada / gagal baca."""
        try:
            info = self._api('GET', f'contents/{repo_path}?ref={self.branch}')
        except GitHubError:
            return None  # file belum ada
        if not isinstance(info, dict) or info.get('encoding') != 'base64':
            return None
        try:
            # validate=False → newline di base64 GitHub diabaikan
            return base64.b64decode(info.get('content', '') or '')
        except Exception:
            return None

    def _local_blob(self, local_file: str) -> bytes | None:
        """Baca file lokal sebagai bytes, None jika gagal."""
        try:
            with open(local_file, 'rb') as f:
                return f.read()
        except OSError as e:
            logger.warning("Gagal baca %s: %s", local_file, e)
            return None

    def upload_song(self, opus_file: str, meta_file: str,
                    repo_dir: str = 'music',
                    commit_message: str | None = None) -> dict:
        """
        Upload satu lagu (opus_stream + meta.json) ke repo.
        Jika konten sudah identik dengan yang ada di repo → skip (tidak
        disimpan ulang).

        Return {ok, skipped, opus_path, meta_path, commit_sha}
        """
        import os

        self._ensure_repo_ready()

        opus_name = os.path.basename(opus_file)
        meta_name = os.path.basename(meta_file)
        opus_path = f"{repo_dir}/{opus_name}"
        meta_path = f"{repo_dir}/{meta_name}"

        if not commit_message:
            commit_message = f"music: add {opus_name}"

        # ── Cek duplikat: bandingkan konten lokal vs repo ──
        local_opus = self._local_blob(opus_file)
        repo_opus = self._get_repo_blob(opus_path)
        opus_identical = (local_opus is not None
                          and repo_opus is not None
                          and repo_opus == local_opus)

        local_meta = self._local_blob(meta_file)
        repo_meta = self._get_repo_blob(meta_path)
        meta_identical = (local_meta is not None
                          and repo_meta is not None
                          and repo_meta == local_meta)

        if opus_identical and meta_identical:
            logger.info("Skip %s — sudah identik di repo", opus_name)
            return {
                'ok': True,
                'skipped': True,
                'opus_path': opus_path,
                'meta_path': meta_path,
                'commit_sha': '',
            }

        # Upload opus_stream (skip jika sudah identik)
        if opus_identical:
            logger.info("Skip opus %s — audio sudah identik", opus_name)
            opus_sha = ''
        else:
            opus_sha = self._put_contents(opus_path, opus_file,
                                          commit_message)

        # Upload metadata (skip jika sudah identik)
        if meta_identical:
            logger.info("Skip meta %s — metadata sudah identik", meta_name)
            meta_sha = None
        else:
            try:
                meta_sha = self._put_contents(meta_path, meta_file, None)
            except GitHubError as e:
                logger.warning("Gagal upload metadata: %s", e)
                meta_sha = None

        return {
            'ok': True,
            'skipped': False,
            'opus_path': opus_path,
            'meta_path': meta_path,
            'commit_sha': opus_sha,
        }

    def _put_contents(self, repo_path: str, local_file: str,
                      commit_message: str | None) -> str:
        """Upload/commit satu file. Return commit sha."""
        import os

        with open(local_file, 'rb') as f:
            content = base64.b64encode(f.read()).decode('ascii')

        # Cek apakah file sudah ada (untuk sha existing → update)
        existing_sha = None
        try:
            info = self._api('GET', f'contents/{repo_path}?ref={self.branch}')
            if isinstance(info, dict) and info.get('sha'):
                existing_sha = info['sha']
        except GitHubError:
            pass  # file belum ada

        body = {
            'message': commit_message or f"update {repo_path}",
            'content': content,
            'branch': self.branch,
        }
        if existing_sha:
            body['sha'] = existing_sha

        result = self._api('PUT', f'contents/{repo_path}', body)
        commit = result.get('commit', {})
        return commit.get('sha', '')

    def delete_song(self, repo_path: str, commit_message: str | None = None) -> dict:
        """Hapus file dari repo. Return {ok, commit_sha}."""
        self._ensure_repo_ready()

        # Ambil sha file
        info = self._api('GET', f'contents/{repo_path}?ref={self.branch}')
        if not isinstance(info, dict) or not info.get('sha'):
            raise GitHubError(f"File '{repo_path}' tidak ditemukan di repo")

        body = {
            'message': commit_message or f"music: delete {repo_path}",
            'sha': info['sha'],
            'branch': self.branch,
        }
        result = self._api('DELETE', f'contents/{repo_path}', body)
        commit = result.get('commit', {})
        return {'ok': True, 'commit_sha': commit.get('sha', '')}
