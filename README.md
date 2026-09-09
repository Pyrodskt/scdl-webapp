# SoundCloud Playlist Downloader

This small Flask app lets you add a SoundCloud playlist and download each track.

## Logic

- If a track exposes a free download URL, it is downloaded directly from that link.
- Otherwise the app falls back to the direct SoundCloud MP3 stream.

## Run

```bash
python -m pip install -r requirements.txt
python app.py
```

Then open:

```text
http://localhost:5000
```

## Notes

- The app stores playlist and downloaded files in the local `downloads` folder.
- When no free download link is available, the app resolves the direct MP3 stream from SoundCloud instead of invoking the unstable `scdl` CLI.
- This avoids the Windows certificate / `yt-dlp` client-id issues that were breaking the fallback path.
