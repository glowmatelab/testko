import os
import asyncio
import glob
import logging
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Music Download API", version="1.0.0")

API_KEY = os.getenv("API_KEY", "")
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

COOKIES_FILE = "cookies.txt"
PO_TOKEN = os.getenv("PO_TOKEN", "")
VISITOR_DATA = os.getenv("VISITOR_DATA", "")


def verify_key(api_key: str):
    if API_KEY and api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_ydl_opts(video: bool = False, cookies: bool = True) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }

    if cookies and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    if PO_TOKEN and VISITOR_DATA:
        opts["extractor_args"] = {
            "youtube": {
                "po_token": [f"web+{PO_TOKEN}"],
                "visitor_data": [VISITOR_DATA],
            }
        }

    if video:
        opts.update({
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
        })
    else:
        opts.update({
            "format": "bestaudio/best",
            "outtmpl": str(DOWNLOADS_DIR / "%(id)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })

    return opts


def cleanup_file(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning(f"Cleanup failed for {path}: {e}")


def find_file(video_id: str, video: bool = False) -> Optional[str]:
    ext = "mp4" if video else "mp3"
    direct = str(DOWNLOADS_DIR / f"{video_id}.{ext}")
    if os.path.exists(direct):
        return direct
    # fallback glob
    candidates = [
        p for p in glob.glob(str(DOWNLOADS_DIR / f"{video_id}*"))
        if not p.endswith((".part", ".ytdl", ".info.json"))
    ]
    return candidates[0] if candidates else None


@app.get("/")
async def root():
    return {"status": "ok", "message": "Music API is running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/download")
async def download(
    url: str = Query(..., description="YouTube video ID or URL"),
    type: str = Query("audio", description="audio or video"),
    api_key: str = Query("", description="API key"),
):
    verify_key(api_key)

    is_video = type == "video"
    video_id = url.strip()

    # already downloaded?
    cached = find_file(video_id, is_video)
    if cached:
        ext = "mp4" if is_video else "mp3"
        media_type = "video/mp4" if is_video else "audio/mpeg"
        return FileResponse(
            cached,
            media_type=media_type,
            filename=f"{video_id}.{ext}",
        )

    yt_url = f"https://www.youtube.com/watch?v={video_id}" if not video_id.startswith("http") else video_id

    opts = get_ydl_opts(video=is_video)

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _download_sync, yt_url, opts)
    except Exception as e:
        logger.error(f"Download failed for {video_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)[:200]}")

    file_path = find_file(video_id, is_video)
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=500, detail="File not found after download")

    ext = "mp4" if is_video else "mp3"
    media_type = "video/mp4" if is_video else "audio/mpeg"

    return FileResponse(
        file_path,
        media_type=media_type,
        filename=f"{video_id}.{ext}",
        background=BackgroundTask(cleanup_file, file_path),
    )


@app.get("/live")
async def live_stream(
    url: str = Query(..., description="YouTube video ID"),
    type: str = Query("live", description="stream type"),
    api_key: str = Query("", description="API key"),
):
    verify_key(api_key)

    video_id = url.strip()
    yt_url = f"https://www.youtube.com/watch?v={video_id}" if not video_id.startswith("http") else video_id

    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "best[protocol^=m3u8]/best",
        "skip_download": True,
    }

    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    if PO_TOKEN and VISITOR_DATA:
        opts["extractor_args"] = {
            "youtube": {
                "po_token": [f"web+{PO_TOKEN}"],
                "visitor_data": [VISITOR_DATA],
            }
        }

    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _extract_info, yt_url, opts)
        stream_url = info.get("url") or info.get("manifest_url")
        if not stream_url:
            # try formats
            for fmt in info.get("formats", []):
                if fmt.get("protocol", "").startswith("m3u8"):
                    stream_url = fmt.get("url")
                    break
        if not stream_url:
            raise HTTPException(status_code=500, detail="Could not get stream URL")
        return JSONResponse({"stream_url": stream_url})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Live stream failed for {video_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Stream failed: {str(e)[:200]}")


def _download_sync(url: str, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


def _extract_info(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)
