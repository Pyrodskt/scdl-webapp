import types
from pathlib import Path

import app
from app import choose_download_source, resolve_download_dir, sanitize_filename


def test_index_template_has_global_search_field_and_playlist_toggle():
    client = app.app.test_client()
    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Recherche globale" in page
    assert "data-global-search" in page
    assert "toggle-playlist" in page


def test_choose_download_source_prefers_free_download_when_available():
    track = {
        "title": "My Track",
        "download_url": "https://example.com/free-download.mp3",
        "downloadable": True,
        "stream_url": "https://example.com/stream",
    }

    assert choose_download_source(track, "client-123") == {
        "mode": "free-download",
        "url": "https://example.com/free-download.mp3?client_id=client-123",
    }


def test_attach_client_id_keeps_signed_hls_urls_unchanged():
    signed_url = "https://playback.media-streaming.soundcloud.cloud/foo/playlist.m3u8?expires=123&Signature=abc&Key-Pair-Id=xyz"
    assert app.attach_client_id(signed_url, "client-123") == signed_url


def test_choose_download_source_uses_scdl_for_mp3_when_no_free_download():
    track = {
        "title": "My Track",
        "permalink_url": "https://soundcloud.com/user/my-track",
        "download_url": None,
        "downloadable": False,
    }

    assert choose_download_source(track, "client-123") == {
        "mode": "scdl-mp3",
        "url": "https://soundcloud.com/user/my-track",
    }


def test_sanitize_filename_removes_invalid_characters():
    assert sanitize_filename('A/B:C?*"<>| track') == 'A_B_C_ track'


def test_playlist_download_path_uses_playlist_folder_direct_file():
    result = app.playlist_download_path("My Playlist", "My Track", output_format="flac")
    assert result == Path(app.DOWNLOADS_DIR) / "My Playlist" / "My Track.flac"


def test_resolve_download_dir_uses_custom_root_and_playlist_folder():
    result = resolve_download_dir("My Playlist", "custom-root")
    assert result == Path(app.DOWNLOADS_DIR) / "custom-root" / "My Playlist"


def test_fetch_playlist_enriches_minimal_track_entries(monkeypatch):
    class FakeClient:
        client_id = "client-123"

        def resolve(self, url):
            return types.SimpleNamespace(
                title="My playlist",
                tracks=(types.SimpleNamespace(id=42),),
            )

        def get_track(self, track_id):
            assert track_id == 42
            return types.SimpleNamespace(
                id=42,
                title="Real title",
                permalink_url="https://soundcloud.com/user/real-title",
                user=types.SimpleNamespace(username="Artist"),
                downloadable=False,
                download_url=None,
            )

    monkeypatch.setattr(app, "create_soundcloud_client", lambda: FakeClient())

    playlist = app.fetch_playlist("https://soundcloud.com/user/my-playlist")

    assert playlist["tracks"][0]["title"] == "Real title"
    assert playlist["tracks"][0]["artist"] == "Artist"


def test_download_media_response_handles_hls_playlists(monkeypatch, tmp_path):
    playlist_url = "https://example.com/playlist.m3u8"

    class FakeResponse:
        def __init__(self, text="", content=b"", headers=None):
            self.text = text
            self.content = content
            self.headers = headers or {}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield self.content

    monkeypatch.setattr(app.requests, "get", lambda url, stream=True, timeout=None, verify=None: FakeResponse(text="#EXTM3U\n#EXTINF:1.0,\nsegment-1.aac\n", headers={"Content-Type": "application/vnd.apple.mpegurl"}))
    monkeypatch.setattr(app, "resolve_ffmpeg_path", lambda: "C:/ffmpeg/bin/ffmpeg.exe")

    def fake_run(command, *args, **kwargs):
        output_path = Path(command[-1])
        output_path.write_bytes(b"abc")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    target = tmp_path / "track.aac"
    result = app.download_media_response(playlist_url, target)

    assert result == tmp_path / "track.flac"
    assert result.exists()
    assert result.read_bytes() == b"abc"


def test_download_mp3_fallback_uses_direct_soundcloud_stream(monkeypatch, tmp_path):
    track = {"title": "Downloaded Track", "permalink_url": "https://soundcloud.com/user/downloaded-track"}

    monkeypatch.setattr(app, "resolve_soundcloud_mp3_url", lambda url: "https://example.com/stream.mp3")

    class FakeResponse:
        headers = {}

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=8192):
            yield b"abc"

    monkeypatch.setattr(app.requests, "get", lambda url, stream, timeout, verify: FakeResponse())
    monkeypatch.setattr(app.shutil, "which", lambda name: "C:/ffmpeg/bin/ffmpeg.exe")

    def fake_run(*args, **kwargs):
        output_path = Path(args[0][-1])
        output_path.write_bytes(b"abc")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    result = app.download_mp3_via_scdl(track, tmp_path)

    assert result == tmp_path / "Downloaded Track.flac"
    assert result.exists()
    assert result.read_bytes() == b"abc"


def test_resolve_ffmpeg_path_finds_winget_install(monkeypatch):
    monkeypatch.setattr(app.shutil, "which", lambda name: None)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\demo\AppData\Local")
    monkeypatch.setattr(app.glob, "glob", lambda pattern, recursive=True: [r"C:\Users\demo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"])

    assert app.resolve_ffmpeg_path() == r"C:\Users\demo\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.1-full_build\bin\ffmpeg.exe"


def test_convert_audio_to_format_defaults_to_flac(monkeypatch, tmp_path):
    source = tmp_path / "track.aac"
    source.write_bytes(b"abc")
    monkeypatch.setattr(app, "resolve_ffmpeg_path", lambda: "C:/ffmpeg/bin/ffmpeg.exe")

    def fake_run(command, *args, **kwargs):
        output_path = Path(command[-1])
        output_path.write_bytes(b"abc")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    result = app.convert_audio_to_format(source, tmp_path / "track.mp3")

    assert result == tmp_path / "track.flac"
    assert result.read_bytes() == b"abc"


def test_convert_to_mp3_requires_ffmpeg_for_mp3_output(monkeypatch, tmp_path):
    source = tmp_path / "track.aac"
    source.write_bytes(b"abc")
    monkeypatch.setattr(app, "resolve_ffmpeg_path", lambda: None)

    try:
        app.convert_to_mp3(source, tmp_path / "track.mp3")
        assert False, "Expected ValueError when FFmpeg is missing"
    except ValueError as exc:
        assert "FFmpeg" in str(exc)
