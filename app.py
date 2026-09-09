import glob
import json
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import certifi
import requests
from flask import Flask, flash, redirect, render_template, request, send_file, url_for

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("CURL_CA_BUNDLE", certifi.where())

try:
    from curl_cffi import requests as curl_requests

    _original_curl_request = curl_requests.Session.request

    def _patched_curl_request(self, *args, **kwargs):
        kwargs.setdefault("verify", False)
        return _original_curl_request(self, *args, **kwargs)

    curl_requests.Session.request = _patched_curl_request
except Exception:
    pass

from soundcloud import SoundCloud

app = Flask(__name__)
app.secret_key = "demo-secret-key"

DOWNLOADS_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

PLAYLISTS = []
PROGRESS = {}
DEFAULT_AUDIO_FORMAT = "flac"
SUPPORTED_AUDIO_FORMATS = {"flac", "mp3", "wav"}


def normalize_audio_format(value: str | None) -> str:
    candidate = (value or DEFAULT_AUDIO_FORMAT).strip().lower()
    if candidate not in SUPPORTED_AUDIO_FORMATS:
        return DEFAULT_AUDIO_FORMAT
    return candidate


def resolve_download_dir(playlist_name: str, custom_root: str | None = None) -> Path:
    root = DOWNLOADS_DIR
    if custom_root:
        custom_root = custom_root.strip().strip("\\/")
        if custom_root:
            root = DOWNLOADS_DIR / custom_root
    folder = root / sanitize_filename(playlist_name)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", value or "track")
    cleaned = re.sub(r'_+', lambda match: "___" if len(match.group(0)) > 1 else "_", cleaned)
    cleaned = cleaned.strip() or "track"
    return cleaned


def attach_client_id(url: str | None, client_id: str) -> str | None:
    if not url:
        return None
    if "client_id=" in url:
        return url

    signed_markers = ("expires=", "Signature=", "Policy=")
    if any(marker in url for marker in signed_markers):
        return url

    separator = "&" if "?" in url else "?"
    return f"{url}{separator}client_id={client_id}"


def choose_download_source(track: dict, client_id: str) -> dict:
    direct_url = track.get("download_url")
    if track.get("downloadable") and direct_url:
        return {"mode": "free-download", "url": attach_client_id(direct_url, client_id)}

    permalink = track.get("permalink_url") or track.get("url")
    if permalink:
        return {"mode": "scdl-mp3", "url": permalink}

    raise ValueError(f"Track {track.get('title', 'unknown')} has no downloadable source.")


def create_soundcloud_client() -> SoundCloud:
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("CURL_CA_BUNDLE", certifi.where())
    return SoundCloud()


def resolve_track_object(track, client) -> object:
    if track is None:
        return None

    candidate = getattr(track, "track", track)
    if getattr(candidate, "title", None) and getattr(candidate, "permalink_url", None):
        return candidate

    track_id = getattr(candidate, "id", None)
    if track_id is not None:
        resolved = client.get_track(track_id)
        if resolved is not None:
            return resolved

    return candidate


def normalize_track(track, client_id: str) -> dict:
    track_obj = resolve_track_object(track, create_soundcloud_client()) if not hasattr(track, "title") else track
    user = getattr(track_obj, "user", None)
    direct_url = getattr(track_obj, "download_url", None)
    permalink = getattr(track_obj, "permalink_url", None)
    title = getattr(track_obj, "title", None) or "Unknown track"

    return {
        "id": getattr(track_obj, "id", None),
        "title": title,
        "artist": getattr(user, "username", None) or "Unknown artist",
        "permalink_url": permalink,
        "download_url": attach_client_id(direct_url, client_id),
        "downloadable": bool(getattr(track_obj, "downloadable", False) and direct_url),
        "source": "free-download" if bool(getattr(track_obj, "downloadable", False) and direct_url) else "scdl-mp3",
    }


def fetch_playlist(url: str) -> dict:
    client = create_soundcloud_client()
    playlist = client.resolve(url)
    if playlist is None:
        raise ValueError("Playlist not found or not public.")

    tracks = getattr(playlist, "tracks", []) or []
    normalized_tracks = []
    for track in tracks:
        resolved = resolve_track_object(track, client)
        if resolved is None:
            continue
        if not getattr(resolved, "title", None):
            continue
        normalized_tracks.append(normalize_track(resolved, client.client_id))

    return {
        "name": getattr(playlist, "title", "SoundCloud playlist"),
        "url": url,
        "tracks": normalized_tracks,
    }


def resolve_ffmpeg_path() -> str | None:
    ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if ffmpeg:
        return ffmpeg

    candidates = []
    user_local = os.environ.get("LOCALAPPDATA")
    if user_local:
        candidates.append(os.path.join(user_local, "Microsoft", "WinGet", "Packages", "**", "ffmpeg.exe"))
        candidates.append(os.path.join(user_local, "Microsoft", "WinGet", "Packages", "**", "bin", "ffmpeg.exe"))

    for base in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        r"C:\Program Files",
        r"C:\Program Files (x86)",
    ):
        if base:
            candidates.append(os.path.join(base, "**", "ffmpeg.exe"))
            candidates.append(os.path.join(base, "**", "bin", "ffmpeg.exe"))

    for pattern in candidates:
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]

    return None


def convert_audio_to_format(input_path: Path, output_path: Path, output_format: str = DEFAULT_AUDIO_FORMAT) -> Path:
    target_format = normalize_audio_format(output_format)
    ffmpeg = resolve_ffmpeg_path()
    if ffmpeg is None:
        raise ValueError(f"FFmpeg is required to convert audio to {target_format.upper()} output.")

    output_path = output_path.with_suffix(f".{target_format}") if output_path.suffix.lower() != f".{target_format}" else output_path

    if output_path.exists():
        output_path.unlink()

    codec_map = {
        "mp3": "libmp3lame",
        "flac": "flac",
        "wav": "pcm_s16le",
    }
    codec = codec_map.get(target_format, "libmp3lame")
    result = subprocess.run(
        [ffmpeg, "-y", "-i", str(input_path), "-vn", "-acodec", codec, str(output_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"{target_format.upper()} conversion failed: {result.stderr.strip() or result.stdout.strip() or 'unknown ffmpeg error'}")

    if input_path.exists() and input_path != output_path:
        input_path.unlink()

    return output_path


def convert_to_mp3(input_path: Path, output_path: Path) -> Path:
    return convert_audio_to_format(input_path, output_path, "mp3")


def convert_to_flac(input_path: Path, output_path: Path) -> Path:
    return convert_audio_to_format(input_path, output_path, "flac")


def download_media_response(url: str, target: Path, target_format: str = DEFAULT_AUDIO_FORMAT) -> Path:
    response = requests.get(url, stream=True, timeout=60, verify=False)
    response.raise_for_status()

    headers = getattr(response, "headers", {}) or {}
    content_type = (headers.get("Content-Type") or "").lower()
    payload = getattr(response, "text", None) or ""
    is_hls_playlist = ("#EXTM3U" in payload) or (".m3u8" in url.lower()) or ("mpegurl" in content_type)
    final_format = normalize_audio_format(target_format)

    if is_hls_playlist:
        ffmpeg = resolve_ffmpeg_path()
        if ffmpeg is None:
            raise ValueError(f"FFmpeg is required to convert audio to {final_format.upper()} output.")

        output_file = target.with_suffix(f".{final_format}") if target.suffix.lower() != f".{final_format}" else target
        if output_file.exists():
            output_file.unlink()

        codec_map = {"mp3": "libmp3lame", "flac": "flac", "wav": "pcm_s16le"}
        codec = codec_map.get(final_format, "flac")
        result = subprocess.run(
            [ffmpeg, "-y", "-i", url, "-vn", "-acodec", codec, str(output_file)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError(f"{final_format.upper()} conversion failed: {result.stderr.strip() or result.stdout.strip() or 'unknown ffmpeg error'}")
        return output_file

    with open(target, "wb") as handle:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                handle.write(chunk)

    if target.suffix.lower() != f".{final_format}":
        target_file = target.with_suffix(f".{final_format}")
        return convert_audio_to_format(target, target_file, final_format)

    return target


def download_free_track(track: dict, destination_dir: Path, target_format: str = DEFAULT_AUDIO_FORMAT) -> Path:
    decision = choose_download_source(track, track.get("client_id") or "")
    format_name = normalize_audio_format(target_format)

    extension = f".{format_name}"
    parsed_path = urlparse(decision["url"]).path
    if parsed_path:
        suffix = Path(parsed_path).suffix.lower()
        if suffix and suffix in {".mp3", ".flac", ".wav"}:
            extension = suffix

    target = destination_dir / f"{sanitize_filename(track['title'])}{extension}"
    if "playlist.m3u8" in decision["url"].lower() or "m3u8" in decision["url"].lower():
        target = destination_dir / f"{sanitize_filename(track['title'])}.aac"

    result = download_media_response(decision["url"], target, target_format=format_name)
    if result.suffix.lower() == f".{format_name}":
        return result
    return convert_audio_to_format(result, destination_dir / f"{sanitize_filename(track['title'])}.{format_name}", format_name)


def resolve_soundcloud_mp3_url(track_url: str) -> str:
    client = create_soundcloud_client()
    track = client.resolve(track_url)
    if track is None:
        raise ValueError("Unable to resolve this SoundCloud track.")

    stream_url = getattr(track, "stream_url", None)
    if stream_url:
        return attach_client_id(stream_url, client.client_id)

    media = getattr(track, "media", None)
    transcodings = getattr(media, "transcodings", []) if media else []
    for transcoding in transcodings:
        transcoding_url = getattr(transcoding, "url", None)
        if not transcoding_url:
            continue
        response = requests.get(
            transcoding_url,
            params={"client_id": client.client_id},
            verify=False,
            timeout=60,
        )
        if response.status_code >= 400:
            continue
        payload = response.json()
        if isinstance(payload, dict):
            stream = payload.get("url") or payload.get("location")
            if stream:
                return stream

    raise ValueError("No SoundCloud MP3 stream was found for this track.")


def download_mp3_via_scdl(track: dict, destination_dir: Path, target_format: str = DEFAULT_AUDIO_FORMAT) -> Path:
    """Download the direct SoundCloud stream and convert it to a chosen DJ-friendly format."""
    url = track.get("permalink_url")
    if not url:
        raise ValueError("Missing SoundCloud track URL for MP3 fallback.")

    format_name = normalize_audio_format(target_format)
    candidate = destination_dir / f"{sanitize_filename(track['title'])}.{format_name}"
    stream_url = resolve_soundcloud_mp3_url(url)
    if "playlist.m3u8" in stream_url.lower() or "m3u8" in stream_url.lower():
        candidate = destination_dir / f"{sanitize_filename(track['title'])}.aac"

    result = download_media_response(stream_url, candidate, target_format=format_name)
    if result.suffix.lower() == f".{format_name}":
        return result
    return convert_audio_to_format(result, destination_dir / f"{sanitize_filename(track['title'])}.{format_name}", format_name)


def playlist_download_path(playlist_name: str, track_title: str, custom_root: str | None = None, output_format: str = DEFAULT_AUDIO_FORMAT) -> Path:
    format_name = normalize_audio_format(output_format)
    return resolve_download_dir(playlist_name, custom_root) / f"{sanitize_filename(track_title)}.{format_name}"


def download_single_track(playlist: dict, track: dict, playlist_index: int | str) -> Path:
    output_format = normalize_audio_format(playlist.get("audio_format", DEFAULT_AUDIO_FORMAT))
    destination_dir = resolve_download_dir(playlist["name"], playlist.get("download_root"))
    destination_dir.mkdir(parents=True, exist_ok=True)
    file_path = playlist_download_path(playlist["name"], track["title"], playlist.get("download_root"), output_format)

    if file_path.exists():
        return file_path

    if track["source"] == "free-download":
        file_path = download_free_track({**track, "client_id": create_soundcloud_client().client_id}, destination_dir, output_format)
    else:
        file_path = download_mp3_via_scdl(track, destination_dir, output_format)

    return file_path


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        playlist_url = request.form.get("playlist_url", "").strip()
        if not playlist_url:
            flash("Please provide a SoundCloud playlist URL.")
            return redirect(url_for("index"))

        try:
            playlist = fetch_playlist(playlist_url)
            playlist["playlist_index"] = len(PLAYLISTS)
            playlist["download_root"] = request.form.get("output_dir", "").strip()
            playlist["audio_format"] = normalize_audio_format(request.form.get("audio_format"))
            PLAYLISTS.insert(0, playlist)
        except Exception as exc:  # pragma: no cover - UI feedback path
            flash(f"Impossible to read this playlist: {exc}")
            return redirect(url_for("index"))

    return render_template("index.html", playlists=PLAYLISTS)


@app.route("/download/<playlist_index>/<int:track_index>")
def download_track(playlist_index, track_index):
    try:
        playlist = PLAYLISTS[int(playlist_index)]
        track = playlist["tracks"][track_index]
    except (IndexError, ValueError):
        flash("This track is not available anymore.")
        return redirect(url_for("index"))

    try:
        file_path = download_single_track(playlist, track, int(playlist_index))
    except Exception as exc:
        flash(f"Download failed: {exc}")
        return redirect(url_for("index"))

    return send_file(file_path, as_attachment=True, download_name=file_path.name)


@app.route("/download-all/<int:playlist_index>", methods=["POST"])
def download_all_tracks(playlist_index):
    try:
        playlist = PLAYLISTS[playlist_index]
    except IndexError:
        return json.dumps({"ok": False, "message": "Playlist not found."}), 404

    total = len(playlist["tracks"])
    PROGRESS[playlist_index] = {"done": 0, "total": total, "status": "running"}

    def worker():
        try:
            for index, track in enumerate(playlist["tracks"]):
                if (PROGRESS.get(playlist_index) or {}).get("status") == "cancelled":
                    break
                try:
                    download_single_track(playlist, track, playlist_index)
                except Exception:
                    pass
                PROGRESS[playlist_index] = {"done": index + 1, "total": total, "status": "running"}
            PROGRESS[playlist_index] = {"done": total, "total": total, "status": "done"}
        except Exception as exc:
            PROGRESS[playlist_index] = {"done": PROGRESS.get(playlist_index, {}).get("done", 0), "total": total, "status": "error", "message": str(exc)}

    threading.Thread(target=worker, daemon=True).start()
    return json.dumps({"ok": True, "playlist_index": playlist_index})


@app.route("/progress/<int:playlist_index>")
def progress(playlist_index):
    payload = PROGRESS.get(playlist_index, {"done": 0, "total": 0, "status": "idle"})
    return json.dumps(payload)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
