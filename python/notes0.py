#!/usr/bin/env python3
"""Future Proof Notes Hub.

Developer overview:
- FastAPI monolith serving HTML pages and API endpoints.
- Filesystem-first storage for notes, datasets, and videos under ~/.notes.
- SQLite auth DB for users/sessions/locks/messages/follows/notifications/file metadata.
- Social layer: follow, notifications, profiles, recommendations.
- Data/video import layer: local upload + external text/YouTube import.
- Global UX layer: theme/layout toolbar persisted in localStorage.

Navigation guide for maintainers:
1) Initialization and config
2) Core helpers (storage, auth, social, import)
3) Shared UI style/script block (COMMON_STYLE)
4) Routes grouped by feature (auth/account/admin/api/home/content)

Function index (quick lookup):
- Initialization/config: setup, init_auth_db
- Notes/datasets: parse_note, save_note, get_dataset_info, validate_dataset_content
- Public imports: search_public_texts, import_public_text_as_note
- YouTube flow: search_youtube_videos, import_youtube_video_with_progress,
  run_youtube_import_job, start_youtube_import_route, youtube_import_progress_route
- Auth/session: create_user_account, create_session, get_current_user, get_api_user
- Privacy/social: upsert_file_record, file_visible_to_user, follow_user,
  notify_followers_public_upload
- Main web routes: web_home, view_note, view_full_dataset, view_video
- Admin/API routes: admin_control_page, admin_users_page, api_login, api_me,
  api_list_notes, api_messages
"""

import os, re, sys, markdown2, subprocess, datetime, shutil
import html, secrets
import sqlite3, hashlib
import io
import random
import threading
import uuid
import time
import concurrent.futures
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, List
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request as UrlRequest, urlopen
import json
from fastapi import FastAPI, HTTPException, Form, File, UploadFile, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, JSONResponse
import pandas as pd

# --- 1. INITIALIZATION ---
def setup():
    """Create base filesystem directories and return runtime paths."""
    base = Path.home() / ".notes"
    notes, data = base / "notes", base / "datasets"
    videos, thumbs = base / "videos", base / "thumbnails"
    notes.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)
    thumbs.mkdir(parents=True, exist_ok=True)
    return {
        "root": base,
        "notes": notes,
        "datasets": data,
        "videos": videos,
        "thumbnails": thumbs,
        "auth_db": base / "auth.db",
    }

config = setup()
app = FastAPI(title="Note & Data Hub")
YOUTUBE_IMPORT_JOBS = {}
YOUTUBE_IMPORT_JOBS_LOCK = threading.Lock()
GAME_TYPES = ("tetris", "frogger", "word_guess", "hangman")
HOME_PANEL_IDS = [
    "uploads",
    "text-search",
    "youtube-search",
    "notes",
    "data",
    "videos",
    "setup",
    "recommendations",
    "news",
    "games",
    "chat",
    "social",
]
NEWS_SOURCES = [
    ("CNN", "http://rss.cnn.com/rss/edition.rss"),
    ("The New York Times", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("The Wall Street Journal", "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("The Economist", "https://www.economist.com/international/rss.xml"),
    ("Google News", "https://news.google.com/rss"),
    ("Apple News", "https://www.apple.com/newsroom/rss-feed.rss"),
    ("BBC News", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("NBC News", "https://feeds.nbcnews.com/nbcnews/public/news"),
]
NEWS_CACHE_LOCK = threading.Lock()
NEWS_CACHE = {"expires_at": 0.0, "items": []}
MARKETPLACE_ITEM_TYPES = {
    "vehicle": {
        "label": "Vehicle",
        "detail_labels": ["Year", "Make/Brand", "Trim/Fuel"],
    },
    "electronics": {
        "label": "Electronics",
        "detail_labels": ["Brand", "Model", "Condition"],
    },
    "furniture": {
        "label": "Furniture",
        "detail_labels": ["Material", "Style", "Condition"],
    },
    "clothing": {
        "label": "Clothing",
        "detail_labels": ["Brand", "Size", "Condition"],
    },
    "other": {
        "label": "Other",
        "detail_labels": ["Detail 1", "Detail 2", "Detail 3"],
    },
}

# --- 2. CORE LOGIC ---
def parse_note(f: Path):
    """Parse note frontmatter/body from a markdown file."""
    if not f.exists(): return {"title": f.stem, "tags": []}, ""
    content = f.read_text(encoding='utf-8')
    # Match YAML-style frontmatter
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
    if not match: return {"title": f.stem, "tags": []}, content
    meta = {}
    for line in match.group(1).strip().splitlines():
        if ':' in line:
            k, v = [item.strip() for item in line.split(':', 1)]
            if k.lower() == 'tags':
                v = [t.strip() for t in v.strip('[]').split(',') if t.strip()]
            meta[k.lower()] = v
    return meta, match.group(2).strip()

def save_note(f: Path, meta: dict, body: str):
    """Persist note metadata and markdown body using simple frontmatter."""
    if isinstance(meta.get('tags'), list): 
        meta['tags'] = f"[{', '.join(meta['tags'])}]"
    head = "\n".join([f"{k}: {v}" for k, v in meta.items()])
    f.write_text(f"---\n{head}\n---\n\n{body}", encoding='utf-8')

def get_dataset_info(name: str, rows_limit: int = 3):
    """Read a dataset file and return column/preview metadata for rendering."""
    f = config["datasets"] / name
    if not f.exists(): return None
    try:
        df = pd.read_csv(f) if f.suffix == '.csv' else pd.read_json(f)
        return {
            "id": name, "rows": len(df), "cols": list(df.columns),
            "preview": df.head(rows_limit).to_dict(orient="records")
        }
    except: return {"id": name, "rows": 0, "preview": []}

def validate_dataset_content(filename: str, content: str):
    """Validate edited dataset text by parsing it according to file extension."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        pd.read_csv(io.StringIO(content))
        return
    if suffix == ".json":
        pd.read_json(io.StringIO(content))
        return
    raise HTTPException(status_code=400, detail="Unsupported dataset format")

def sanitize_note_basename(title: str) -> str:
    """Create a filesystem-safe note base name from a title string."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", (title or "").strip()).strip("_") or "note"

def choose_plain_text_url(formats: dict) -> Optional[str]:
    """Select the best plain-text URL from a Gutendex formats map."""
    preferred_keys = [
        "text/plain; charset=utf-8",
        "text/plain; charset=us-ascii",
        "text/plain",
    ]
    for key in preferred_keys:
        candidate = formats.get(key)
        if candidate:
            return candidate
    for key, value in formats.items():
        if key.lower().startswith("text/plain") and value:
            return value
    return None

def search_public_texts(query: str, limit: int = 8) -> List[dict]:
    """Search Gutendex and return normalized public-domain text results."""
    term = (query or "").strip()
    if not term:
        return []
    endpoint = f"https://gutendex.com/books?search={quote(term)}"
    req = UrlRequest(endpoint, headers={"User-Agent": "future-proof-notes/1.0"})
    with urlopen(req, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))

    results = []
    for item in payload.get("results", []):
        source_url = choose_plain_text_url(item.get("formats", {}))
        if not source_url:
            continue
        authors = ", ".join([a.get("name", "") for a in item.get("authors", []) if a.get("name")]) or "Unknown"
        title = (item.get("title") or "Untitled").strip()
        results.append({"title": title, "authors": authors, "source_url": source_url})
        if len(results) >= limit:
            break
    return results

def validate_public_text_url(source_url: str) -> str:
    """Allow only safe Gutenberg-hosted plain-text URLs for importing."""
    parsed = urlparse((source_url or "").strip())
    host = (parsed.netloc or "").lower()
    allowed_hosts = ("gutenberg.org", "www.gutenberg.org", "aleph.gutenberg.org")
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Invalid source URL")
    if host not in allowed_hosts and not host.endswith(".gutenberg.org"):
        raise HTTPException(status_code=400, detail="Unsupported source host")
    if ".txt" not in (parsed.path or "").lower():
        raise HTTPException(status_code=400, detail="Source must be a plain text file")
    return source_url.strip()

def download_public_text(source_url: str, max_bytes: int = 2_500_000) -> str:
    """Download a public text file with size and encoding safeguards."""
    req = UrlRequest(source_url, headers={"User-Agent": "future-proof-notes/1.0"})
    with urlopen(req, timeout=20) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=400, detail="Text file is too large")
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")

def import_public_text_as_note(title: str, source_url: str) -> str:
    """Import a public text URL as a new markdown note file."""
    clean_title = (title or "").strip() or "Imported Text"
    validated_url = validate_public_text_url(source_url)
    text_body = download_public_text(validated_url)

    base = sanitize_note_basename(clean_title)
    candidate = f"{base}.md"
    attempt = 2
    while (config["notes"] / candidate).exists():
        candidate = f"{base}_{attempt}.md"
        attempt += 1

    note_body = f"# {clean_title}\n\n_Source: {validated_url}_\n\n{text_body}"
    save_note(config["notes"] / candidate, {"title": clean_title}, note_body)
    return candidate

def get_yt_dlp_path() -> Optional[str]:
    """Return the local yt-dlp executable path if available."""
    return shutil.which("yt-dlp")

def get_setup_checks() -> List[dict]:
    """Return optional dependency checks for UI setup guidance."""
    checks = [
        {
            "name": "yt-dlp",
            "ok": bool(get_yt_dlp_path()),
            "required_for": "YouTube search and YouTube video import",
            "install": "brew install yt-dlp",
        },
        {
            "name": "ffmpeg",
            "ok": bool(shutil.which("ffmpeg")),
            "required_for": "Video thumbnails in library grid",
            "install": "brew install ffmpeg",
        },
    ]
    return checks

def normalize_youtube_url(value: str) -> str:
    """Validate and normalize user-entered YouTube IDs/URLs."""
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="YouTube URL is required")
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return f"https://www.youtube.com/watch?v={raw}"
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower().replace("www.", "")
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    if host not in {"youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}:
        raise HTTPException(status_code=400, detail="Unsupported video host")
    return raw

def search_youtube_videos(query: str, limit: int = 6) -> List[dict]:
    """Search YouTube via yt-dlp and return simplified result objects."""
    term = (query or "").strip()
    if not term:
        return []
    yt_dlp_path = get_yt_dlp_path()
    if not yt_dlp_path:
        raise RuntimeError("yt-dlp is not installed")

    command = [
        yt_dlp_path,
        "--flat-playlist",
        "--dump-single-json",
        f"ytsearch{max(1, min(limit, 10))}:{term}",
    ]
    run = subprocess.run(command, capture_output=True, text=True, check=False)
    if run.returncode != 0:
        raise RuntimeError((run.stderr or "YouTube search failed").strip())

    payload = json.loads(run.stdout or "{}")
    entries = payload.get("entries", []) or []
    results = []
    for entry in entries:
        video_id = (entry or {}).get("id")
        if not video_id:
            continue
        title = (entry.get("title") or "Untitled video").strip()
        uploader = (entry.get("uploader") or "Unknown channel").strip()
        duration = entry.get("duration")
        results.append(
            {
                "title": title,
                "uploader": uploader,
                "duration": duration,
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    return results

def import_youtube_video(video_url: str) -> str:
    """Download a YouTube video file and return its saved filename."""
    yt_dlp_path = get_yt_dlp_path()
    if not yt_dlp_path:
        raise HTTPException(status_code=500, detail="yt-dlp is not installed")
    normalized_url = normalize_youtube_url(video_url)

    command = [
        yt_dlp_path,
        "--no-playlist",
        "-f",
        "mp4/best[ext=mp4]/best",
        "--restrict-filenames",
        "--paths",
        str(config["videos"]),
        "-o",
        "%(title).120B_[%(id)s].%(ext)s",
        "--print",
        "after_move:filepath",
        normalized_url,
    ]
    run = subprocess.run(command, capture_output=True, text=True, check=False)
    if run.returncode != 0:
        detail = (run.stderr or run.stdout or "Video download failed").strip()
        raise HTTPException(status_code=400, detail=detail)

    lines = [line.strip() for line in (run.stdout or "").splitlines() if line.strip()]
    file_path = Path(lines[-1]) if lines else None
    if not file_path or not file_path.exists():
        raise HTTPException(status_code=500, detail="Video downloaded but file path not found")

    filename = safe_name(file_path.name)
    generate_video_thumbnail(filename)
    return filename

def import_youtube_video_with_progress(video_url: str, progress_callback=None) -> str:
    """Download a YouTube video while emitting progress updates to a callback."""
    yt_dlp_path = get_yt_dlp_path()
    if not yt_dlp_path:
        raise HTTPException(status_code=500, detail="yt-dlp is not installed")
    normalized_url = normalize_youtube_url(video_url)

    command = [
        yt_dlp_path,
        "--newline",
        "--no-playlist",
        "-f",
        "mp4/best[ext=mp4]/best",
        "--restrict-filenames",
        "--paths",
        str(config["videos"]),
        "-o",
        "%(title).120B_[%(id)s].%(ext)s",
        "--print",
        "after_move:filepath",
        normalized_url,
    ]

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if progress_callback:
        progress_callback(1.0, "Starting download...")

    output_lines = []
    percent_pattern = re.compile(r"(\d+(?:\.\d+)?)%")
    for raw_line in iter(proc.stdout.readline, ""):
        line = raw_line.strip()
        if not line:
            continue
        output_lines.append(line)
        match = percent_pattern.search(line)
        if match and progress_callback:
            try:
                progress_callback(float(match.group(1)), line)
            except Exception:
                pass

    proc.wait()
    if proc.returncode != 0:
        detail = "\n".join(output_lines[-8:]).strip() or "Video download failed"
        raise HTTPException(status_code=400, detail=detail)

    file_path = None
    for line in reversed(output_lines):
        candidate = Path(line)
        if candidate.exists() and candidate.is_file():
            file_path = candidate
            break
    if not file_path:
        raise HTTPException(status_code=500, detail="Video downloaded but file path not found")

    filename = safe_name(file_path.name)
    generate_video_thumbnail(filename)
    if progress_callback:
        progress_callback(100.0, "Download complete")
    return filename

def finalize_imported_video_for_user(user, filename: str, private_upload: bool):
    """Save ownership/visibility metadata and follower notifications for an imported video."""
    is_public = not bool(private_upload)
    upsert_file_record("video", filename, user["id"] if user else None, is_public)
    if user:
        notify_followers_public_upload(user, "video", filename)

def set_youtube_job(job_id: str, **updates):
    """Thread-safe update helper for in-memory YouTube import jobs."""
    with YOUTUBE_IMPORT_JOBS_LOCK:
        job = YOUTUBE_IMPORT_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)

def run_youtube_import_job(job_id: str, video_url: str, user_snapshot, private_upload: bool):
    """Background worker that performs YouTube import and records job state."""
    try:
        set_youtube_job(job_id, status="downloading", progress=1.0, message="Starting download...")

        def callback(percent: float, message: str):
            safe_percent = max(0.0, min(100.0, float(percent)))
            set_youtube_job(job_id, progress=safe_percent, message=message)

        imported_file = import_youtube_video_with_progress(video_url, callback)
        finalize_imported_video_for_user(user_snapshot, imported_file, private_upload)
        set_youtube_job(job_id, status="completed", progress=100.0, filename=imported_file, message="Import complete")
    except Exception as ex:
        set_youtube_job(job_id, status="error", message=str(ex))

def fetch_rss_headline(source_name: str, rss_url: str) -> dict:
    """Fetch one latest headline/link from a news RSS feed."""
    try:
        req = UrlRequest(rss_url, headers={"User-Agent": "future-proof-notes/1.0"})
        with urlopen(req, timeout=4) as response:
            xml_text = response.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_text)

        item = root.find("./channel/item")
        if item is not None:
            title = (item.findtext("title") or "Latest headline").strip()
            link = (item.findtext("link") or "").strip()
            if title and link:
                return {"source": source_name, "title": title, "link": link, "ok": True}

        entry = root.find("{http://www.w3.org/2005/Atom}entry")
        if entry is not None:
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title") or "Latest headline").strip()
            link_node = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_node.attrib.get("href", "") if link_node is not None else "").strip()
            if title and link:
                return {"source": source_name, "title": title, "link": link, "ok": True}

    except Exception:
        pass
    return {"source": source_name, "title": "News temporarily unavailable", "link": "", "ok": False}

def fetch_latest_news(force_refresh: bool = False) -> List[dict]:
    """Fetch and cache one headline per configured news source."""
    now = time.time()
    with NEWS_CACHE_LOCK:
        if not force_refresh and NEWS_CACHE["items"] and NEWS_CACHE["expires_at"] > now:
            return NEWS_CACHE["items"]

    collected = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(fetch_rss_headline, source_name, rss_url): source_name
            for source_name, rss_url in NEWS_SOURCES
        }
        for future in concurrent.futures.as_completed(future_map):
            try:
                collected.append(future.result())
            except Exception:
                source_name = future_map[future]
                collected.append({"source": source_name, "title": "News temporarily unavailable", "link": "", "ok": False})

    order = {name: idx for idx, (name, _) in enumerate(NEWS_SOURCES)}
    collected.sort(key=lambda item: order.get(item["source"], 999))

    with NEWS_CACHE_LOCK:
        NEWS_CACHE["items"] = collected
        NEWS_CACHE["expires_at"] = now + 300
    return collected

def render_news_rows_html(news_items: List[dict]) -> str:
    """Render latest-news entries as home-card HTML rows."""
    rows = ""
    for item in news_items:
        if item.get("ok") and item.get("link"):
            rows += f"<div class='note-item'><div><strong>{h(item['source'])}</strong><br><a href='{h(item['link'])}' target='_blank' rel='noopener'>{h(item['title'])}</a></div></div>"
        else:
            rows += f"<div class='note-item'><div><strong>{h(item['source'])}</strong><br><small>{h(item['title'])}</small></div></div>"
    return rows

def autotempest_is_listing_link(link: str) -> bool:
    """Return True when a URL looks like a real vehicle listing destination."""
    try:
        parsed = urlparse(link)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.netloc or "").lower()
    if not host:
        return False
    if host.endswith("autotempest.com"):
        return False
    path = (parsed.path or "").lower()
    query = (parsed.query or "").lower()
    listing_signatures = (
        "/vehicle/",
        "/vehicledetail/",
        "/itm/",
        "/details/cars/",
        "request=adlink",
        "ad=",
    )
    haystack = f"{path}?{query}"
    return any(sig in haystack for sig in listing_signatures)

def normalize_space(value: str) -> str:
    """Collapse whitespace for cleaner text extraction from HTML snippets."""
    return re.sub(r"\s+", " ", (value or "")).strip()

def extract_autotempest_listings(
    make: str,
    model: str,
    zip_code: str,
    radius: int,
    max_price: Optional[int],
    limit: int = 36,
) -> List[dict]:
    """Scrape listing links from AutoTempest search results with lightweight metadata extraction."""
    clean_make = (make or "").strip().lower() or "toyota"
    clean_model = (model or "").strip().lower() or "camry"
    clean_zip = re.sub(r"[^0-9]", "", (zip_code or "").strip())[:5] or "30301"
    clean_radius = int(radius) if str(radius).strip() else 50
    clean_radius = max(10, min(500, clean_radius))
    clean_max_price = None
    if max_price is not None and str(max_price).strip():
        clean_max_price = max(500, int(max_price))

    params = {
        "make": clean_make,
        "model": clean_model,
        "zip": clean_zip,
        "radius": str(clean_radius),
    }
    if clean_max_price is not None:
        params["maxprice"] = str(clean_max_price)

    source_url = f"https://www.autotempest.com/results?{urlencode(params)}"
    req = UrlRequest(source_url, headers={"User-Agent": "future-proof-notes/1.0"})
    with urlopen(req, timeout=10) as response:
        page_html = response.read().decode("utf-8", errors="replace")

    listings = []
    seen = set()
    anchor_re = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)

    for match in anchor_re.finditer(page_html):
        href = html.unescape(match.group(1) or "").strip()
        if not href:
            continue
        if href.startswith("/"):
            href = f"https://www.autotempest.com{href}"
        if not autotempest_is_listing_link(href):
            continue

        parsed = urlparse(href)
        dedupe_key = f"{parsed.netloc.lower()}|{parsed.path}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        raw_title = re.sub(r"<[^>]+>", " ", match.group(2) or "")
        title = normalize_space(html.unescape(raw_title))
        if len(title) < 6:
            continue

        nearby = page_html[max(0, match.start() - 320): min(len(page_html), match.end() + 520)]
        nearby_clean = normalize_space(re.sub(r"<[^>]+>", " ", nearby))

        price_match = re.search(r"\$\s?[\d,]{2,}", nearby_clean)
        miles_match = re.search(r"\b([\d,]{1,7})\s*mi\.?\b", nearby_clean, re.IGNORECASE)
        location_match = re.search(r"\b([A-Za-z][A-Za-z .'-]+,\s?[A-Z]{2})\b", nearby_clean)
        image_match = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", nearby, re.IGNORECASE)

        source_label = (parsed.netloc or "listing").lower().replace("www.", "")
        image_url = html.unescape(image_match.group(1)).strip() if image_match else ""
        if image_url.startswith("/"):
            image_url = f"https://www.autotempest.com{image_url}"

        listings.append(
            {
                "title": title,
                "link": href,
                "source": source_label,
                "price": price_match.group(0).replace(" ", "") if price_match else "Price not shown",
                "miles": f"{miles_match.group(1)} mi" if miles_match else "",
                "location": location_match.group(1) if location_match else "",
                "image": image_url,
            }
        )
        if len(listings) >= limit:
            break

    return listings

def h(value) -> str:
    """HTML-escape any value for safe interpolation in templates."""
    return html.escape(str(value), quote=True)

def u(value: str) -> str:
    """URL-encode path/query values for safe link construction."""
    return quote(value, safe="")

def safe_name(name: str) -> str:
    """Return basename only, stripping any parent directory components."""
    return Path(name or "").name

def ensure_safe_filename(name: str) -> str:
    """Validate filename safety and reject traversal or ambiguous names."""
    cleaned = safe_name(name)
    if not cleaned or cleaned != name or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return cleaned

def get_or_create_csrf_token(request: Request) -> str:
    """Reuse existing CSRF cookie token or generate a new one."""
    existing = request.cookies.get("csrf_token")
    if existing and len(existing) >= 16:
        return existing
    return secrets.token_urlsafe(24)

def validate_csrf(request: Request, csrf_token: str):
    """Validate submitted CSRF token against cookie value."""
    cookie = request.cookies.get("csrf_token")
    if not csrf_token or not cookie or not secrets.compare_digest(csrf_token, cookie):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

def get_db_connection():
    """Open a SQLite connection configured to return row objects."""
    conn = sqlite3.connect(config["auth_db"])
    conn.row_factory = sqlite3.Row
    return conn

def get_user_count() -> int:
    """Return the total number of registered user accounts."""
    conn = get_db_connection()
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    conn.close()
    return count

def validate_new_account_input(clean_user: str, password: str):
    """Enforce username format and minimum password strength policy."""
    if not re.fullmatch(r"[a-z0-9_.-]{3,32}", clean_user):
        raise HTTPException(status_code=400, detail="Invalid username format")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

def create_user_account(clean_user: str, password: str, role: str = "user", public_name: Optional[str] = None):
    """Create a user account row and return the newly created user profile."""
    if role not in {"user", "admin"}:
        raise HTTPException(status_code=400, detail="Invalid role")
    validate_new_account_input(clean_user, password)
    name_value = (public_name or clean_user).strip() or clean_user
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, public_name, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (clean_user, name_value, hash_password(password), role, datetime.datetime.utcnow().isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Username already exists")
    user_row = conn.execute(
        "SELECT id, username, public_name, role, created_at FROM users WHERE username = ?",
        (clean_user,),
    ).fetchone()
    conn.close()
    return user_row

def init_auth_db():
    """Initialize auth/social tables and apply lightweight schema migrations."""
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS note_locks (
            filename TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            owner_user_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(owner_user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_user_id INTEGER NOT NULL,
            recipient_user_id INTEGER NOT NULL,
            message_text TEXT NOT NULL,
            read_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(sender_user_id) REFERENCES users(id),
            FOREIGN KEY(recipient_user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS follows (
            follower_user_id INTEGER NOT NULL,
            followed_user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (follower_user_id, followed_user_id),
            FOREIGN KEY(follower_user_id) REFERENCES users(id),
            FOREIGN KEY(followed_user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS file_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_type TEXT NOT NULL,
            filename TEXT NOT NULL,
            owner_user_id INTEGER,
            is_public INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(file_type, filename),
            FOREIGN KEY(owner_user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            actor_user_id INTEGER NOT NULL,
            file_type TEXT NOT NULL,
            filename TEXT NOT NULL,
            message_text TEXT NOT NULL,
            read_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(actor_user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS game_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            game_name TEXT NOT NULL,
            score INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS user_preferences (
            user_id INTEGER PRIMARY KEY,
            home_hidden_panels TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS marketplace_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            item_type TEXT NOT NULL DEFAULT 'other',
            item_details_json TEXT NOT NULL DEFAULT '{}',
            price INTEGER NOT NULL,
            location TEXT NOT NULL,
            mileage INTEGER,
            description TEXT,
            image_url TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            is_sold INTEGER NOT NULL DEFAULT 0,
            sold_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(seller_user_id) REFERENCES users(id)
        )
    ''')
    msg_cols = [row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()]
    user_cols = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    listing_cols = [row["name"] for row in conn.execute("PRAGMA table_info(marketplace_listings)").fetchall()]
    if "public_name" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN public_name TEXT")
        conn.execute("UPDATE users SET public_name = username WHERE public_name IS NULL OR trim(public_name) = ''")
    if "read_at" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN read_at TEXT")
    if "item_type" not in listing_cols:
        conn.execute("ALTER TABLE marketplace_listings ADD COLUMN item_type TEXT NOT NULL DEFAULT 'other'")
    if "item_details_json" not in listing_cols:
        conn.execute("ALTER TABLE marketplace_listings ADD COLUMN item_details_json TEXT NOT NULL DEFAULT '{}' ")
    if "is_sold" not in listing_cols:
        conn.execute("ALTER TABLE marketplace_listings ADD COLUMN is_sold INTEGER NOT NULL DEFAULT 0")
    if "sold_at" not in listing_cols:
        conn.execute("ALTER TABLE marketplace_listings ADD COLUMN sold_at TEXT")
    conn.commit()
    conn.close()

def hash_password(password: str) -> str:
    """Hash a password with per-password salt using SHA-256."""
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${digest}"

def verify_password(password: str, stored_hash: str) -> bool:
    """Verify plain password against stored salted SHA-256 hash."""
    try:
        salt, digest = stored_hash.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return secrets.compare_digest(calc, digest)

def create_session(user_id: int) -> str:
    """Create a persistent auth session token for a user."""
    token = secrets.token_urlsafe(32)
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
        (token, user_id, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return token

def clear_session(token: str):
    """Delete a session token from the sessions table."""
    conn = get_db_connection()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

def get_current_user(request: Request):
    """Resolve current user from auth cookie token for web routes."""
    token = request.cookies.get("auth_token")
    if not token:
        return None
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT users.id, users.username, users.public_name, users.role
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
        """,
        (token,),
    ).fetchone()
    conn.close()
    return row

def get_user_by_session_token(token: str):
    """Resolve user row from session token for API authentication."""
    if not token:
        return None
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT users.id, users.username, users.public_name, users.role
        FROM sessions
        JOIN users ON users.id = sessions.user_id
        WHERE sessions.token = ?
        """,
        (token,),
    ).fetchone()
    conn.close()
    return row

def get_api_user(request: Request):
    """Authenticate API request via Bearer token and return user/token."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = header.split(" ", 1)[1].strip()
    user = get_user_by_session_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
    return user, token

def get_unread_message_count(user_id: int) -> int:
    """Count unread inbox messages for a user."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE recipient_user_id = ? AND read_at IS NULL",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["c"] if row else 0

def get_unread_notification_count(user_id: int) -> int:
    """Count unread social notifications for a user."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["c"] if row else 0

def normalize_home_hidden_panels(value) -> List[str]:
    """Normalize and validate hidden panel ids for home-dashboard preferences."""
    if not isinstance(value, list):
        return []
    allowed = set(HOME_PANEL_IDS)
    clean = []
    for item in value:
        panel_id = str(item).strip()
        if panel_id in allowed and panel_id not in clean:
            clean.append(panel_id)
    return clean

def get_user_home_hidden_panels(user_id: int) -> List[str]:
    """Return account-level home hidden-panel preferences."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT home_hidden_panels FROM user_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    if not row:
        return []
    try:
        parsed = json.loads(row["home_hidden_panels"] or "[]")
    except Exception:
        parsed = []
    return normalize_home_hidden_panels(parsed)

def set_user_home_hidden_panels(user_id: int, hidden_panels: List[str]):
    """Persist account-level home hidden-panel preferences."""
    safe_panels = normalize_home_hidden_panels(hidden_panels)
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO user_preferences (user_id, home_hidden_panels, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            home_hidden_panels=excluded.home_hidden_panels,
            updated_at=excluded.updated_at
        """,
        (user_id, json.dumps(safe_panels), datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def normalize_marketplace_image_url(image_url: str) -> str:
    """Normalize and validate optional image URL for marketplace listings."""
    value = (image_url or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Image URL must be a valid http(s) URL")
    return value

def normalize_marketplace_item_type(item_type: str) -> str:
    """Normalize/validate marketplace item type key."""
    key = (item_type or "").strip().lower()
    if key not in MARKETPLACE_ITEM_TYPES:
        return "other"
    return key

def build_marketplace_item_details(item_type: str, detail_a: str, detail_b: str, detail_c: str) -> dict:
    """Build sanitized marketplace details dictionary from generic detail fields."""
    clean_type = normalize_marketplace_item_type(item_type)
    labels = MARKETPLACE_ITEM_TYPES[clean_type]["detail_labels"]
    values = [(detail_a or "").strip(), (detail_b or "").strip(), (detail_c or "").strip()]
    details = {}
    for label, value in zip(labels, values):
        if value:
            details[label] = value[:80]
    return details

def parse_marketplace_item_details(raw_json: str) -> dict:
    """Parse item details JSON safely for template rendering."""
    try:
        parsed = json.loads(raw_json or "{}")
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}

def create_marketplace_listing(
    seller_user_id: int,
    title: str,
    item_type: str,
    detail_a: str,
    detail_b: str,
    detail_c: str,
    price: int,
    location: str,
    mileage: Optional[int],
    description: str,
    image_url: str,
):
    """Persist a user-created marketplace listing in the auth DB."""
    clean_title = (title or "").strip()
    clean_location = (location or "").strip()
    clean_description = (description or "").strip()
    clean_image_url = normalize_marketplace_image_url(image_url)
    clean_item_type = normalize_marketplace_item_type(item_type)
    item_details = build_marketplace_item_details(clean_item_type, detail_a, detail_b, detail_c)

    if len(clean_title) < 4 or len(clean_title) > 140:
        raise HTTPException(status_code=400, detail="Title must be between 4 and 140 characters")
    if len(clean_location) < 2 or len(clean_location) > 80:
        raise HTTPException(status_code=400, detail="Location must be between 2 and 80 characters")

    safe_price = max(100, min(int(price), 5_000_000))
    safe_mileage = None
    if mileage is not None and str(mileage).strip():
        safe_mileage = max(0, min(int(mileage), 2_000_000))

    if len(clean_description) > 2000:
        raise HTTPException(status_code=400, detail="Description is too long")

    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO marketplace_listings
            (seller_user_id, title, item_type, item_details_json, price, location, mileage, description, image_url, is_active, is_sold, sold_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, NULL, ?)
        """,
        (
            seller_user_id,
            clean_title,
            clean_item_type,
            json.dumps(item_details),
            safe_price,
            clean_location,
            safe_mileage,
            clean_description,
            clean_image_url,
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

def get_recent_marketplace_listings(limit: int = 40, sold_only: bool = False):
    """Return newest active marketplace listings; optionally only sold listings."""
    conn = get_db_connection()
    sold_filter = 1 if sold_only else 0
    rows = conn.execute(
                """
                SELECT
                        marketplace_listings.id,
                        marketplace_listings.seller_user_id,
                        marketplace_listings.title,
                    marketplace_listings.item_type,
                    marketplace_listings.item_details_json,
                        marketplace_listings.price,
                        marketplace_listings.location,
                        marketplace_listings.mileage,
                        marketplace_listings.description,
                        marketplace_listings.image_url,
                        marketplace_listings.is_sold,
                        marketplace_listings.sold_at,
                        marketplace_listings.created_at,
                        users.username AS seller_username,
                        users.public_name AS seller_public_name
                FROM marketplace_listings
                JOIN users ON users.id = marketplace_listings.seller_user_id
                WHERE marketplace_listings.is_active = 1
                    AND marketplace_listings.is_sold = ?
                ORDER BY marketplace_listings.id DESC
                LIMIT ?
                """,
        (sold_filter, max(1, min(limit, 120))),
    ).fetchall()
    conn.close()
    return rows

def mark_marketplace_listing_sold(listing_id: int, seller_user_id: int):
    """Mark one listing as sold if it belongs to the authenticated seller."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT id, is_sold FROM marketplace_listings WHERE id = ? AND seller_user_id = ? AND is_active = 1",
        (listing_id, seller_user_id),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Listing not found")
    if row["is_sold"]:
        conn.close()
        return
    conn.execute(
        "UPDATE marketplace_listings SET is_sold = 1, sold_at = ? WHERE id = ?",
        (datetime.datetime.utcnow().isoformat(), listing_id),
    )
    conn.commit()
    conn.close()

def submit_game_score(user_id: int, game_name: str, score: int):
    """Persist a user's score for a supported mini game."""
    if game_name not in GAME_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported game")
    safe_score = max(0, min(int(score), 1_000_000))
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO game_scores (user_id, game_name, score, created_at) VALUES (?, ?, ?, ?)",
        (user_id, game_name, safe_score, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def get_game_leaderboard(game_name: str, limit: int = 10):
    """Return top scores for one game with username/display metadata."""
    if game_name not in GAME_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported game")
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT game_scores.score, game_scores.created_at, users.username, users.public_name
        FROM game_scores
        JOIN users ON users.id = game_scores.user_id
        WHERE game_scores.game_name = ?
        ORDER BY game_scores.score DESC, game_scores.id ASC
        LIMIT ?
        """,
        (game_name, max(1, min(limit, 50))),
    ).fetchall()
    conn.close()
    return rows

def get_leaderboard_snapshot(limit_each: int = 5):
    """Return top scores grouped by game for multi-game leaderboard views."""
    snapshot = {}
    for game_name in GAME_TYPES:
        snapshot[game_name] = get_game_leaderboard(game_name, limit_each)
    return snapshot

def upsert_file_record(file_type: str, filename: str, owner_user_id: Optional[int], is_public: bool):
    """Insert or update ownership/visibility metadata for a stored file."""
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO file_records (file_type, filename, owner_user_id, is_public, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(file_type, filename) DO UPDATE SET
            owner_user_id=excluded.owner_user_id,
            is_public=excluded.is_public,
            created_at=excluded.created_at
        """,
        (file_type, filename, owner_user_id, 1 if is_public else 0, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def get_file_record(file_type: str, filename: str):
    """Fetch a single file metadata row by type and filename."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM file_records WHERE file_type = ? AND filename = ?",
        (file_type, filename),
    ).fetchone()
    conn.close()
    return row

def file_is_public(file_type: str, filename: str) -> bool:
    """Determine public visibility for a file using metadata and lock fallback."""
    row = get_file_record(file_type, filename)
    if row:
        return bool(row["is_public"])
    if file_type == "note":
        return get_note_lock(filename) is None
    return True

def file_visible_to_user(file_type: str, filename: str, user) -> bool:
    """Check whether a viewer can access a file given privacy/ownership rules."""
    row = get_file_record(file_type, filename)
    if row:
        if row["is_public"]:
            return True
        if not user:
            return False
        if user["role"] == "admin":
            return True
        return row["owner_user_id"] == user["id"]
    if file_type == "note":
        lock_row = get_note_lock(filename)
        if not lock_row:
            return True
        return user_can_bypass_lock(user, lock_row)
    return True

def notify_followers_public_upload(actor_user, file_type: str, filename: str):
    """Send notifications to followers when a user uploads a public file."""
    if not actor_user:
        return
    if not file_is_public(file_type, filename):
        return
    conn = get_db_connection()
    followers = conn.execute(
        "SELECT follower_user_id FROM follows WHERE followed_user_id = ?",
        (actor_user["id"],),
    ).fetchall()
    if not followers:
        conn.close()
        return
    created_at = datetime.datetime.utcnow().isoformat()
    message = f"{actor_user['username']} uploaded a public {file_type}: {filename}"
    conn.executemany(
        """
        INSERT INTO notifications
            (user_id, actor_user_id, file_type, filename, message_text, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (row["follower_user_id"], actor_user["id"], file_type, filename, message, created_at)
            for row in followers
            if row["follower_user_id"] != actor_user["id"]
        ],
    )
    conn.commit()
    conn.close()

def display_name(user_row) -> str:
    """Return preferred display name (public name fallback to username)."""
    if not user_row:
        return ""
    value = user_row["public_name"] if "public_name" in user_row.keys() else None
    value = (value or "").strip()
    return value or user_row["username"]

def get_following_rows(user_id: int):
    """List users currently followed by the given user."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT users.id, users.username, users.public_name
        FROM follows
        JOIN users ON users.id = follows.followed_user_id
        WHERE follows.follower_user_id = ?
        ORDER BY lower(users.username) ASC
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return rows

def get_follower_rows(user_id: int):
    """List users who follow the given user."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT users.id, users.username, users.public_name
        FROM follows
        JOIN users ON users.id = follows.follower_user_id
        WHERE follows.followed_user_id = ?
        ORDER BY lower(users.username) ASC
        """,
        (user_id,),
    ).fetchall()
    conn.close()
    return rows

def is_following(follower_user_id: int, followed_user_id: int) -> bool:
    """Return whether follower currently follows target user."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT 1 FROM follows WHERE follower_user_id = ? AND followed_user_id = ?",
        (follower_user_id, followed_user_id),
    ).fetchone()
    conn.close()
    return bool(row)

def file_exists_by_type(file_type: str, filename: str) -> bool:
    """Check whether a file exists in its type-specific storage directory."""
    if file_type == "note":
        return (config["notes"] / filename).exists()
    if file_type == "dataset":
        return (config["datasets"] / filename).exists()
    if file_type == "video":
        return (config["videos"] / filename).exists()
    return False

def file_link_by_type(file_type: str, filename: str) -> Optional[str]:
    """Build canonical web URL for a file based on type."""
    name_u = u(filename)
    if file_type == "note":
        return f"/notes/{name_u}"
    if file_type == "dataset":
        return f"/datasets/{name_u}/full"
    if file_type == "video":
        return f"/videos/{name_u}"
    return None

def get_public_uploads_for_user(user_id: int, limit: int = 12):
    """Return recent public uploads for a specific user profile."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT file_type, filename, created_at
        FROM file_records
        WHERE owner_user_id = ? AND is_public = 1
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [row for row in rows if file_exists_by_type(row["file_type"], row["filename"])]

def get_recommended_public_files_for_user(user_id: int, limit: int = 18):
    """Return recent public files from accounts followed by the user."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT file_records.file_type, file_records.filename, file_records.created_at,
               users.username AS owner_username, users.public_name AS owner_public_name
        FROM file_records
        JOIN follows ON follows.followed_user_id = file_records.owner_user_id
        JOIN users ON users.id = file_records.owner_user_id
        WHERE follows.follower_user_id = ? AND file_records.is_public = 1
        ORDER BY datetime(file_records.created_at) DESC, file_records.id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [row for row in rows if file_exists_by_type(row["file_type"], row["filename"])]

def follow_user(follower_user_id: int, target_user_id: int):
    """Create a follow relationship while preventing self-follow."""
    if follower_user_id == target_user_id:
        raise HTTPException(status_code=400, detail="Cannot follow yourself")
    conn = get_db_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO follows (follower_user_id, followed_user_id, created_at)
        VALUES (?, ?, ?)
        """,
        (follower_user_id, target_user_id, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()

def unfollow_user(follower_user_id: int, target_user_id: int):
    """Remove a follow relationship if it exists."""
    conn = get_db_connection()
    conn.execute(
        "DELETE FROM follows WHERE follower_user_id = ? AND followed_user_id = ?",
        (follower_user_id, target_user_id),
    )
    conn.commit()
    conn.close()

def get_note_lock(filename: str):
    """Fetch lock metadata for a note if present."""
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM note_locks WHERE filename = ?", (filename,)).fetchone()
    conn.close()
    return row

def set_note_lock(filename: str, password: str, owner_user_id: Optional[int]):
    """Create or update a note lock and ownership binding."""
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO note_locks (filename, password_hash, owner_user_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(filename) DO UPDATE SET
            password_hash=excluded.password_hash,
            owner_user_id=excluded.owner_user_id,
            created_at=excluded.created_at
        """,
        (
            filename,
            hash_password(password),
            owner_user_id,
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

def remove_note_lock(filename: str):
    """Remove lock metadata from a note."""
    conn = get_db_connection()
    conn.execute("DELETE FROM note_locks WHERE filename = ?", (filename,))
    conn.commit()
    conn.close()

def parse_unlocked_cookie(request: Request) -> set:
    """Parse unlocked-note cookie into a set of unlocked filenames."""
    raw = request.cookies.get("unlocked_notes", "")
    return {item for item in raw.split("|") if item}

def user_can_bypass_lock(user, lock_row) -> bool:
    """Allow lock bypass for admins or original lock owners."""
    if not user:
        return False
    if user["role"] == "admin":
        return True
    return bool(lock_row and lock_row["owner_user_id"] == user["id"])

def build_issue_report_template(user) -> str:
    """Return a ready-to-send issue template for support messages."""
    stamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    username = user["username"] if user else "unknown"
    return (
        "Bug report:\n"
        "- Summary: \n"
        "- Expected behavior: \n"
        "- Actual behavior: \n"
        "- Steps to reproduce: \n"
        f"- Time observed: {stamp}\n"
        "- Browser/OS: \n"
        f"- Reporting user: {username}\n\n"
        "Diagnostics:\n"
        "[paste Support Checklist output]"
    )

init_auth_db()

def thumbnail_path(video_name: str) -> Path:
    """Return thumbnail path for a given video filename."""
    return config["thumbnails"] / f"{Path(video_name).stem}.jpg"

def generate_video_thumbnail(video_name: str) -> Optional[Path]:
    """Generate or reuse a JPEG thumbnail for a stored video."""
    video_file = config["videos"] / safe_name(video_name)
    if not video_file.exists():
        return None
    thumb_file = thumbnail_path(video_name)
    if thumb_file.exists() and thumb_file.stat().st_mtime >= video_file.stat().st_mtime:
        return thumb_file
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return None
    try:
        subprocess.run([
            ffmpeg_path,
            "-y",
            "-i", str(video_file),
            "-ss", "00:00:01",
            "-vframes", "1",
            str(thumb_file)
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None
    return thumb_file if thumb_file.exists() else None

# --- 3. UI STYLES ---
COMMON_STYLE = """
<style>
    body { font-family: -apple-system, sans-serif; max-width: 900px; margin: auto; padding: 20px; line-height: 1.45; background: #f8f9fa; color: #333; transition: background .2s ease, color .2s ease; }
    .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; transition: background .2s ease, border-color .2s ease, box-shadow .2s ease; }
    body.theme-dark { background: #121212; color: #e8e8e8; }
    body.theme-dark .card { background: #1f1f1f; color: #e8e8e8; box-shadow: 0 2px 4px rgba(0,0,0,0.45); }
    body.theme-dark a { color: #9ecbff; }
    body.theme-dark .helper, body.theme-dark .chat-meta, body.theme-dark .social-username { color: #b7b7b7; }
    body.theme-dark .note-item, body.theme-dark .social-user, body.theme-dark th, body.theme-dark td { border-color: #343434; }
    body.theme-dark th { background: #2a2a2a; color: #ddd; }
    body.theme-dark .preview-box, body.theme-dark .social-col, body.theme-dark input[type="text"], body.theme-dark input[type="password"], body.theme-dark input[type="file"], body.theme-dark textarea, body.theme-dark select { background: #181818; color: #eee; border-color: #3c3c3c; }
    body.layout-wide { max-width: 1250px; }
    body.layout-focus .home-grid { grid-template-columns: 1fr; }
    body.layout-compact { font-size: 0.95em; }
    body.layout-compact .card { padding: 14px; margin-bottom: 14px; }
    body.layout-custom {
        max-width: var(--fp-custom-max-width, 1100px);
        font-size: var(--fp-custom-font-scale, 1);
    }
    body.layout-custom .card { padding: var(--fp-custom-card-pad, 20px); }
    body.layout-custom .home-grid { grid-template-columns: var(--fp-custom-home-cols, 2fr 1fr); }
    .nav-pills { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
    .helper { color: #666; font-size: 0.85em; margin-top: 6px; }
    .notice-ok { color:#155724; background:#d4edda; padding:8px; border-radius:4px; }
    .notice-bad { color:#721c24; background:#f8d7da; padding:8px; border-radius:4px; }
    .progress-wrap { margin-top: 10px; }
    .progress-label { font-size: 0.85em; color: #444; margin-bottom: 4px; }
    .progress-value { width: 100%; height: 14px; }
    .display-controls { display:flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    .display-controls select { min-width: 160px; padding: 8px; border-radius: 4px; border: 1px solid #ddd; }
    .global-display-toolbar { position: sticky; top: 8px; z-index: 50; background: rgba(255,255,255,0.92); border: 1px solid #ddd; border-radius: 8px; padding: 8px 10px; margin: 0 0 12px auto; width: fit-content; backdrop-filter: blur(4px); }
    .global-display-toolbar .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .global-display-toolbar select { padding: 6px 8px; border-radius: 4px; border: 1px solid #ddd; }
    .global-display-toolbar button { padding: 6px 10px; border: 0; border-radius: 4px; background: #6c757d; color: #fff; cursor: pointer; }
    .global-display-toolbar .custom-row { display: none; margin-top: 8px; gap: 10px; align-items: center; flex-wrap: wrap; }
    .global-display-toolbar .custom-row label { font-size: 0.78em; display: flex; gap: 6px; align-items: center; }
    .global-display-toolbar .custom-row input[type="range"] { width: 120px; }
    .global-display-toolbar .custom-val { font-size: 0.78em; min-width: 42px; display: inline-block; text-align: right; }
    .global-display-toolbar .preset-row { margin-top: 8px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .global-display-toolbar .preset-row input { padding: 6px 8px; border-radius: 4px; border: 1px solid #ddd; }
    body.theme-dark .global-display-toolbar { background: rgba(25,25,25,0.92); border-color: #3c3c3c; }
    body.theme-dark .global-display-toolbar select { background: #181818; color: #eee; border-color: #3c3c3c; }
    .btn { padding: 8px 16px; border-radius: 4px; text-decoration: none; font-weight: bold; cursor: pointer; border: none; display: inline-block; font-size: 0.9em; }
    .btn-primary { background: #007bff; color: white; }
    .btn-success { background: #28a745; color: white; }
    .btn-danger { background: #dc3545; color: white; }
    .btn-secondary { background: #6c757d; color: white; }
    .home-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; align-items: start; }
    .home-grid > .stack { min-width: 0; }
    .stack { display: flex; flex-direction: column; gap: 12px; }
    .home-col-primary { gap: 14px; }
    .home-col-secondary { gap: 14px; }
    .home-col-secondary-sticky { position: sticky; top: 72px; align-self: start; }
    .stack .card { margin-bottom: 0; }
    .section-card h2, .section-card h3 { margin-top: 0; }
    .section-card h2 { margin-bottom: 10px; }
    .section-title { margin: 0 0 8px; font-size: 1.05em; }
    .home-toolbar-card { padding-top: 14px; padding-bottom: 12px; }
    .home-toolbar-card .nav-pills { margin-bottom: 2px; }
    .dashboard-panel { position: relative; }
    .panel-toggle-btn {
        padding: 4px 8px;
        border: 1px solid #d9d9d9;
        border-radius: 999px;
        background: #fff;
        color: #555;
        font-size: 0.74em;
        cursor: pointer;
        margin-left: 8px;
    }
    .panel-toggle-btn:hover { background: #f3f3f3; }
    .panel-header-anchor {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        min-height: 28px;
    }
    .panel-collapsed .panel-body { display: none; }
    .panel-collapsed { border: 1px solid #e8e8e8; }
    body.theme-dark .panel-toggle-btn { background: #232323; color: #ddd; border-color: #3b3b3b; }
    body.theme-dark .panel-toggle-btn:hover { background: #2c2c2c; }
    body.theme-dark .panel-collapsed { border-color: #3a3a3a; }
    .chat-list { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
    .chat-item { border: 1px solid #eee; border-radius: 6px; padding: 10px; background: #fafafa; }
    .chat-meta { color: #666; font-size: 0.75em; margin-bottom: 4px; }
    .social-stats { display: flex; gap: 10px; flex-wrap: wrap; margin: 10px 0 14px; }
    .stat-pill { background: #f4f6f8; border: 1px solid #e4e7eb; border-radius: 999px; padding: 8px 12px; font-size: 0.85em; }
    .social-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .social-col { border: 1px solid #eee; border-radius: 8px; padding: 8px 12px; background: #fff; }
    .social-title { margin: 6px 0 8px; font-size: 0.9em; color: #444; }
    .social-user { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid #f1f1f1; }
    .social-user:last-child { border-bottom: none; }
    .social-username { color: #666; font-size: 0.82em; }
    .note-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #eee; }
    .badge-lock { font-size: 0.72em; color: #fff; background: #6c757d; border-radius: 10px; padding: 2px 8px; margin-left: 8px; }
    .preview-box { overflow-x: auto; max-height: 150px; border: 1px solid #eee; border-radius: 4px; margin-top: 10px; }
    .video-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }
    .video-card { border: 1px solid #eee; border-radius: 8px; background: #fff; padding: 10px; }
    .video-thumb { width: 100%; height: 130px; object-fit: cover; border-radius: 6px; background: #f0f0f0; border: 1px solid #ddd; }
    .video-title { font-size: 0.85em; margin: 8px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .video-actions { display: flex; align-items: center; justify-content: flex-start; gap: 14px; margin-top: 10px; }
    .video-actions form { display: flex; margin: 0; }
    .video-actions .btn,
    .video-actions button.btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        box-sizing: border-box;
        width: 112px;
        min-width: 112px;
        height: 36px;
        padding: 0 10px;
        margin: 0;
        border: 0;
        font-size: 0.82em;
        line-height: 1;
        -webkit-appearance: none;
        appearance: none;
    }
    .inline-form { display: inline-flex; gap: 8px; align-items: center; }
    .player-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 12px; }
    .player-controls .btn { min-width: 120px; }
    .player-controls .inline-form { margin-left: auto; }
    .volume-range { width: min(220px, 100%); }
    @media (max-width: 640px) {
        .home-grid { grid-template-columns: 1fr; }
        .home-col-secondary-sticky { position: static; }
        .social-grid { grid-template-columns: 1fr; }
        .video-actions { flex-direction: column; align-items: stretch; gap: 10px; }
        .video-actions .btn, .video-actions button.btn { width: 100%; min-width: 0; }
        .player-controls .inline-form { margin-left: 0; }
        .player-controls .btn, .player-controls .inline-form button { width: 100%; }
    }
    @media (max-width: 980px) {
        .home-grid { grid-template-columns: 1fr; }
        .home-col-secondary-sticky { position: static; }
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.75em; }
    th, td { border: 1px solid #eee; padding: 6px; text-align: left; white-space: nowrap; }
    th { background: #f9f9f9; color: #666; }
    input[type="text"], input[type="password"], input[type="file"], textarea { padding: 10px; border: 1px solid #ddd; border-radius: 4px; }
    form { display: flex; gap: 10px; align-items: center; }
</style>
<script>
    (function() {
        // Persisted display preference keys.
        const themeKey = 'fp_theme';
        const layoutKey = 'fp_layout';
        const customKey = 'fp_layout_custom';
        const presetsKey = 'fp_layout_presets';

        function applyView(theme, layout) {
            // Toggle body-level classes so all pages inherit the same visual mode.
            document.body.classList.toggle('theme-dark', theme === 'dark');
            document.body.classList.remove('layout-wide', 'layout-focus', 'layout-compact', 'layout-custom');
            if (layout === 'wide') document.body.classList.add('layout-wide');
            if (layout === 'focus') document.body.classList.add('layout-focus');
            if (layout === 'compact') document.body.classList.add('layout-compact');
            if (layout === 'custom') document.body.classList.add('layout-custom');
        }

        function readCustomSettings() {
            const fallback = { width: 1100, fontScale: 100, cardPad: 20, leftRatio: 67 };
            try {
                const parsed = JSON.parse(localStorage.getItem(customKey) || '{}');
                return {
                    width: Number(parsed.width) || fallback.width,
                    fontScale: Number(parsed.fontScale) || fallback.fontScale,
                    cardPad: Number(parsed.cardPad) || fallback.cardPad,
                    leftRatio: Number(parsed.leftRatio) || fallback.leftRatio,
                };
            } catch (_) {
                return fallback;
            }
        }

        function applyCustomSettings(settings) {
            const rightRatio = Math.max(20, 100 - settings.leftRatio);
            document.body.style.setProperty('--fp-custom-max-width', `${settings.width}px`);
            document.body.style.setProperty('--fp-custom-font-scale', `${settings.fontScale / 100}`);
            document.body.style.setProperty('--fp-custom-card-pad', `${settings.cardPad}px`);
            document.body.style.setProperty('--fp-custom-home-cols', `${settings.leftRatio}fr ${rightRatio}fr`);
        }

        function readPresets() {
            try {
                const parsed = JSON.parse(localStorage.getItem(presetsKey) || '{}');
                if (parsed && typeof parsed === 'object') return parsed;
            } catch (_) {}
            return {};
        }

        function writePresets(presets) {
            localStorage.setItem(presetsKey, JSON.stringify(presets));
        }

        function buildToolbar() {
            // Inject one global toolbar per page load.
            if (document.getElementById('globalDisplayToolbar')) return;
            const wrapper = document.createElement('div');
            wrapper.className = 'global-display-toolbar';
            wrapper.id = 'globalDisplayToolbar';
            wrapper.innerHTML = `
                <div class='row'>
                    <strong style='font-size:0.85em;'>Display</strong>
                    <label style='font-size:0.8em;'>Theme
                        <select id='globalThemeSelect'>
                            <option value='light'>Light</option>
                            <option value='dark'>Dark</option>
                        </select>
                    </label>
                    <label style='font-size:0.8em;'>Layout
                        <select id='globalLayoutSelect'>
                            <option value='standard'>Standard</option>
                            <option value='wide'>Wide</option>
                            <option value='focus'>Focus</option>
                            <option value='compact'>Compact</option>
                            <option value='custom'>Custom</option>
                        </select>
                    </label>
                    <button id='globalDisplayResetBtn' type='button'>Reset</button>
                </div>
                <div id='globalCustomLayoutRow' class='custom-row'>
                    <label>Width
                        <input id='customWidthRange' type='range' min='900' max='1600' step='10'>
                        <span id='customWidthVal' class='custom-val'></span>
                    </label>
                    <label>Text
                        <input id='customFontRange' type='range' min='90' max='115' step='1'>
                        <span id='customFontVal' class='custom-val'></span>
                    </label>
                    <label>Card Padding
                        <input id='customPadRange' type='range' min='10' max='30' step='1'>
                        <span id='customPadVal' class='custom-val'></span>
                    </label>
                    <label>Main Column
                        <input id='customColsRange' type='range' min='55' max='80' step='1'>
                        <span id='customColsVal' class='custom-val'></span>
                    </label>
                </div>
                <div class='preset-row'>
                    <label style='font-size:0.8em;'>Preset
                        <select id='globalPresetSelect'>
                            <option value=''>Saved presets</option>
                        </select>
                    </label>
                    <input id='globalPresetName' type='text' placeholder='Preset name'>
                    <button id='globalPresetSaveBtn' type='button'>Save Preset</button>
                    <button id='globalPresetDeleteBtn' type='button'>Delete Preset</button>
                </div>
            `;
            document.body.insertBefore(wrapper, document.body.firstChild);

            const themeSelect = document.getElementById('globalThemeSelect');
            const layoutSelect = document.getElementById('globalLayoutSelect');
            const resetBtn = document.getElementById('globalDisplayResetBtn');
            const customRow = document.getElementById('globalCustomLayoutRow');
            const widthRange = document.getElementById('customWidthRange');
            const fontRange = document.getElementById('customFontRange');
            const padRange = document.getElementById('customPadRange');
            const colsRange = document.getElementById('customColsRange');
            const widthVal = document.getElementById('customWidthVal');
            const fontVal = document.getElementById('customFontVal');
            const padVal = document.getElementById('customPadVal');
            const colsVal = document.getElementById('customColsVal');
            const presetSelect = document.getElementById('globalPresetSelect');
            const presetNameInput = document.getElementById('globalPresetName');
            const presetSaveBtn = document.getElementById('globalPresetSaveBtn');
            const presetDeleteBtn = document.getElementById('globalPresetDeleteBtn');

            const savedTheme = localStorage.getItem(themeKey) || 'light';
            const savedLayout = localStorage.getItem(layoutKey) || 'standard';
            let custom = readCustomSettings();

            function syncCustomUi() {
                widthRange.value = String(custom.width);
                fontRange.value = String(custom.fontScale);
                padRange.value = String(custom.cardPad);
                colsRange.value = String(custom.leftRatio);
                widthVal.textContent = `${custom.width}px`;
                fontVal.textContent = `${custom.fontScale}%`;
                padVal.textContent = `${custom.cardPad}px`;
                colsVal.textContent = `${custom.leftRatio}%`;
            }

            function saveAndApplyCustom() {
                // Keep custom sliders persistent and immediately reflected in CSS vars.
                localStorage.setItem(customKey, JSON.stringify(custom));
                applyCustomSettings(custom);
            }

            function captureCurrentPreset() {
                return {
                    theme: themeSelect.value,
                    layout: layoutSelect.value,
                    custom: { ...custom },
                };
            }

            function refreshPresetSelect(selectedName = '') {
                const presets = readPresets();
                const names = Object.keys(presets).sort((a, b) => a.localeCompare(b));
                presetSelect.innerHTML = "<option value=''>Saved presets</option>";
                for (const name of names) {
                    const opt = document.createElement('option');
                    opt.value = name;
                    opt.textContent = name;
                    presetSelect.appendChild(opt);
                }
                if (selectedName && presets[selectedName]) presetSelect.value = selectedName;
            }

            function applyPreset(name) {
                // Presets bundle theme, layout, and custom slider values.
                const presets = readPresets();
                const preset = presets[name];
                if (!preset) return;
                if (preset.custom && typeof preset.custom === 'object') {
                    custom = {
                        width: Number(preset.custom.width) || custom.width,
                        fontScale: Number(preset.custom.fontScale) || custom.fontScale,
                        cardPad: Number(preset.custom.cardPad) || custom.cardPad,
                        leftRatio: Number(preset.custom.leftRatio) || custom.leftRatio,
                    };
                    localStorage.setItem(customKey, JSON.stringify(custom));
                }
                themeSelect.value = preset.theme || 'light';
                layoutSelect.value = preset.layout || 'standard';
                localStorage.setItem(themeKey, themeSelect.value);
                localStorage.setItem(layoutKey, layoutSelect.value);
                applyCustomSettings(custom);
                syncCustomUi();
                customRow.style.display = layoutSelect.value === 'custom' ? 'flex' : 'none';
                applyView(themeSelect.value, layoutSelect.value);
            }

            themeSelect.value = savedTheme;
            layoutSelect.value = savedLayout;
            applyView(savedTheme, savedLayout);
            applyCustomSettings(custom);
            syncCustomUi();
            customRow.style.display = savedLayout === 'custom' ? 'flex' : 'none';
            refreshPresetSelect();

            themeSelect.addEventListener('change', function() {
                localStorage.setItem(themeKey, themeSelect.value);
                applyView(themeSelect.value, layoutSelect.value);
            });
            layoutSelect.addEventListener('change', function() {
                localStorage.setItem(layoutKey, layoutSelect.value);
                applyView(themeSelect.value, layoutSelect.value);
                customRow.style.display = layoutSelect.value === 'custom' ? 'flex' : 'none';
            });

            widthRange.addEventListener('input', function() {
                custom.width = Number(widthRange.value);
                saveAndApplyCustom();
                syncCustomUi();
            });
            fontRange.addEventListener('input', function() {
                custom.fontScale = Number(fontRange.value);
                saveAndApplyCustom();
                syncCustomUi();
            });
            padRange.addEventListener('input', function() {
                custom.cardPad = Number(padRange.value);
                saveAndApplyCustom();
                syncCustomUi();
            });
            colsRange.addEventListener('input', function() {
                custom.leftRatio = Number(colsRange.value);
                saveAndApplyCustom();
                syncCustomUi();
            });

            resetBtn.addEventListener('click', function() {
                localStorage.setItem(themeKey, 'light');
                localStorage.setItem(layoutKey, 'standard');
                custom = { width: 1100, fontScale: 100, cardPad: 20, leftRatio: 67 };
                localStorage.setItem(customKey, JSON.stringify(custom));
                themeSelect.value = 'light';
                layoutSelect.value = 'standard';
                customRow.style.display = 'none';
                syncCustomUi();
                applyCustomSettings(custom);
                applyView('light', 'standard');
            });

            presetSaveBtn.addEventListener('click', function() {
                const rawName = (presetNameInput.value || '').trim();
                const name = rawName || `Preset ${new Date().toLocaleString()}`;
                const presets = readPresets();
                presets[name] = captureCurrentPreset();
                writePresets(presets);
                refreshPresetSelect(name);
                presetNameInput.value = '';
            });

            presetDeleteBtn.addEventListener('click', function() {
                const name = presetSelect.value;
                if (!name) return;
                const presets = readPresets();
                delete presets[name];
                writePresets(presets);
                refreshPresetSelect();
            });

            presetSelect.addEventListener('change', function() {
                if (!presetSelect.value) return;
                applyPreset(presetSelect.value);
            });
        }

        document.addEventListener('DOMContentLoaded', function() {
            // Apply saved display settings before users interact with page content.
            try {
                const savedTheme = localStorage.getItem(themeKey) || 'light';
                const savedLayout = localStorage.getItem(layoutKey) || 'standard';
                applyView(savedTheme, savedLayout);
                buildToolbar();
            } catch (_) {
                applyView('light', 'standard');
            }
        });
    })();
</script>
"""

# --- 4. ROUTES ---
# Authentication and account lifecycle routes
@app.get("/auth/register", response_class=HTMLResponse)
def register_page(request: Request):
    """Render the web registration page with CSRF protection."""
    csrf_token = get_or_create_csrf_token(request)
    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <div class='card' style='max-width:460px;margin:30px auto;'>
        <h2>Create account</h2>
        <form action='/auth/register' method='post' style='flex-direction:column;align-items:stretch;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='text' name='username' placeholder='Username' required>
            <input type='password' name='password' placeholder='Password' required>
            <button type='submit' class='btn btn-primary'>Register</button>
        </form>
        <p class='helper'>The first account created becomes an admin automatically.</p>
    </div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/auth/register")
def register_route(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    """Create a new account from form input and sign the user in."""
    validate_csrf(request, csrf_token)
    clean_user = username.strip().lower()
    role = "admin" if get_user_count() == 0 else "user"
    user_row = create_user_account(clean_user, password, role=role, public_name=clean_user)
    token = create_session(user_row["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("auth_token", token, httponly=True, samesite="lax")
    return response

@app.get("/auth/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Render the web login page with CSRF token injection."""
    csrf_token = get_or_create_csrf_token(request)
    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <div class='card' style='max-width:460px;margin:30px auto;'>
        <h2>Sign in</h2>
        <form action='/auth/login' method='post' style='flex-direction:column;align-items:stretch;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='text' name='username' placeholder='Username' required>
            <input type='password' name='password' placeholder='Password' required>
            <button type='submit' class='btn btn-primary'>Login</button>
        </form>
    </div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/auth/login")
def login_route(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form("")):
    """Authenticate user credentials and issue an auth session cookie."""
    validate_csrf(request, csrf_token)
    clean_user = username.strip().lower()
    conn = get_db_connection()
    user = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (clean_user,)).fetchone()
    conn.close()
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_session(user["id"])
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("auth_token", token, httponly=True, samesite="lax")
    return response

@app.get("/auth/logout")
def logout_route(request: Request):
    """Clear the active auth session and redirect to home."""
    token = request.cookies.get("auth_token")
    if token:
        clear_session(token)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("auth_token")
    return response

@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, status: Optional[str] = None):
    """Render account security page and password change form."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    csrf_token = get_or_create_csrf_token(request)
    conn = get_db_connection()
    full_user = conn.execute(
        "SELECT id, username, role, created_at FROM users WHERE id = ?",
        (user["id"],),
    ).fetchone()
    conn.close()
    status_html = ""
    if status == "ok":
        status_html = "<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Password updated.</p>"
    elif status == "badpass":
        status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>Current password is incorrect.</p>"
    elif status == "short":
        status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>New password must be at least 6 characters.</p>"
    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <h1>Account</h1>
    <div class='card'>
        <p><strong>Username:</strong> {h(full_user['username'])}</p>
        <p><strong>Role:</strong> {h(full_user['role'])}</p>
        <p><strong>Created:</strong> {h(full_user['created_at'])}</p>
    </div>
    <div class='card' style='max-width:520px;'>
        <h3>Change Password</h3>
        {status_html}
        <form action='/account/password' method='post' style='flex-direction:column;align-items:stretch;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='password' name='current_password' placeholder='Current password' required>
            <input type='password' name='new_password' placeholder='New password' required>
            <button type='submit' class='btn btn-primary'>Update Password</button>
        </form>
    </div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/account/password")
def account_password_route(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    csrf_token: str = Form(""),
):
    """Change the signed-in user's password after validating current password."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    if len(new_password) < 6:
        return RedirectResponse("/account?status=short", status_code=303)
    conn = get_db_connection()
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not row or not verify_password(current_password, row["password_hash"]):
        conn.close()
        return RedirectResponse("/account?status=badpass", status_code=303)
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/account?status=ok", status_code=303)

@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, status: Optional[str] = None, follow_status: Optional[str] = None):
    """Render signed-in user's profile, follow lists, and inbox summary."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)

    csrf_token = get_or_create_csrf_token(request)
    followers = get_follower_rows(user["id"])
    following = get_following_rows(user["id"])

    conn = get_db_connection()
    recent_inbox = conn.execute(
        """
        SELECT messages.message_text, messages.created_at, users.username AS sender_username, users.public_name AS sender_public_name
        FROM messages
        JOIN users ON users.id = messages.sender_user_id
        WHERE messages.recipient_user_id = ?
        ORDER BY messages.id DESC
        LIMIT 8
        """,
        (user["id"],),
    ).fetchall()
    conn.close()

    status_html = ""
    if status == "saved":
        status_html = "<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Public name updated.</p>"
    elif status == "invalid":
        status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>Public name must be 2-40 chars.</p>"

    follow_status_html = ""
    if follow_status == "ok":
        follow_status_html = "<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Followed user.</p>"
    elif follow_status == "removed":
        follow_status_html = "<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Unfollowed user.</p>"
    elif follow_status == "notfound":
        follow_status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>User not found.</p>"
    elif follow_status == "invalid":
        follow_status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>Cannot follow that user.</p>"

    follower_rows = "".join([
        f"<div class='note-item'><span><a href='/u/{u(row['username'])}'><strong>{h((row['public_name'] or '').strip() or row['username'])}</strong></a> <small>@{h(row['username'])}</small></span></div>"
        for row in followers
    ]) or "<p>No followers yet.</p>"
    following_rows = "".join([
        f"<div class='note-item'><span><a href='/u/{u(row['username'])}'><strong>{h((row['public_name'] or '').strip() or row['username'])}</strong></a> <small>@{h(row['username'])}</small></span><form action='/unfollow' method='post' style='margin:0;'><input type='hidden' name='csrf_token' value='{h(csrf_token)}'><input type='hidden' name='next_path' value='/profile'><input type='hidden' name='username' value='{h(row['username'])}'><button type='submit' class='btn btn-secondary'>Unfollow</button></form></div>"
        for row in following
    ]) or "<p>Not following anyone yet.</p>"
    messages_rows = "".join([
        f"<div class='chat-item'><div class='chat-meta'>From {h((msg['sender_public_name'] or '').strip() or msg['sender_username'])} · {h(msg['created_at'])}</div><div>{h(msg['message_text'])}</div></div>"
        for msg in recent_inbox
    ]) or "<p>No messages yet.</p>"

    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <h1>Profile</h1>
    <div class='card'>
        <h2>{h(display_name(user))}</h2>
        <p><small>@{h(user['username'])}</small></p>
        <div style='display:flex;gap:12px;flex-wrap:wrap;'>
            <span class='btn btn-secondary'>{len(followers)} followers</span>
            <span class='btn btn-secondary'>{len(following)} following</span>
            <a href='/messages' class='btn btn-primary'>Open Messages</a>
        </div>
    </div>
    <div class='card'>
        <h3>Edit Public Name</h3>
        {status_html}
        <form action='/profile/public-name' method='post' style='flex-wrap:wrap;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='text' name='public_name' value='{h(display_name(user))}' maxlength='40' style='flex-grow:1' required>
            <button type='submit' class='btn btn-primary'>Save</button>
        </form>
    </div>
    <div class='card'>
        <h3>Follow Someone</h3>
        {follow_status_html}
        <form action='/follow' method='post' style='flex-wrap:wrap;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='hidden' name='next_path' value='/profile'>
            <input type='text' name='username' placeholder='Username to follow' style='flex-grow:1' required>
            <button type='submit' class='btn btn-primary'>Follow</button>
        </form>
    </div>
    <div class='home-grid'>
        <div class='card'><h3>Followers</h3>{follower_rows}</div>
        <div class='card'><h3>Following</h3>{following_rows}</div>
    </div>
    <div class='card'><h3>Recent Messages</h3>{messages_rows}</div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/profile/public-name")
def update_public_name_route(request: Request, public_name: str = Form(...), csrf_token: str = Form("")):
    """Update current user's public profile name."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    clean = " ".join(public_name.strip().split())
    if len(clean) < 2 or len(clean) > 40:
        return RedirectResponse("/profile?status=invalid", status_code=303)
    conn = get_db_connection()
    conn.execute("UPDATE users SET public_name = ? WHERE id = ?", (clean, user["id"]))
    conn.commit()
    conn.close()
    return RedirectResponse("/profile?status=saved", status_code=303)

@app.get("/u/{username}", response_class=HTMLResponse)
def public_user_profile(request: Request, username: str):
    """Render a public profile page with follow controls and public uploads."""
    viewer = get_current_user(request)
    csrf_token = get_or_create_csrf_token(request)
    clean_user = username.strip().lower()

    conn = get_db_connection()
    target = conn.execute(
        "SELECT id, username, public_name, created_at FROM users WHERE lower(username) = lower(?)",
        (clean_user,),
    ).fetchone()
    conn.close()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    followers = get_follower_rows(target["id"])
    following = get_following_rows(target["id"])
    uploads = get_public_uploads_for_user(target["id"], limit=20)

    follow_action_html = ""
    if viewer and viewer["id"] != target["id"]:
        following_now = is_following(viewer["id"], target["id"])
        if following_now:
            follow_action_html = f"""
            <form action='/unfollow' method='post' style='margin:0;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <input type='hidden' name='next_path' value='/u/{u(target['username'])}'>
                <input type='hidden' name='username' value='{h(target['username'])}'>
                <button type='submit' class='btn btn-secondary'>Unfollow</button>
            </form>
            """
        else:
            follow_action_html = f"""
            <form action='/follow' method='post' style='margin:0;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <input type='hidden' name='next_path' value='/u/{u(target['username'])}'>
                <input type='hidden' name='username' value='{h(target['username'])}'>
                <button type='submit' class='btn btn-primary'>Follow</button>
            </form>
            """

    follower_rows = "".join([
        f"<div class='social-user'><a href='/u/{u(row['username'])}'><strong>{h((row['public_name'] or '').strip() or row['username'])}</strong></a><span class='social-username'>@{h(row['username'])}</span></div>"
        for row in followers[:20]
    ]) or "<p>No followers yet.</p>"
    following_rows = "".join([
        f"<div class='social-user'><a href='/u/{u(row['username'])}'><strong>{h((row['public_name'] or '').strip() or row['username'])}</strong></a><span class='social-username'>@{h(row['username'])}</span></div>"
        for row in following[:20]
    ]) or "<p>Not following anyone yet.</p>"

    upload_rows = ""
    for item in uploads:
        link = file_link_by_type(item["file_type"], item["filename"])
        if not link:
            continue
        upload_rows += f"<div class='note-item'><div><strong>{h(item['filename'])}</strong><br><small>{h(item['file_type'])} · {h(item['created_at'])}</small></div><a class='btn btn-primary' href='{h(link)}'>Open</a></div>"
    if not upload_rows:
        upload_rows = "<p>No public uploads yet.</p>"

    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <h1>{h((target['public_name'] or '').strip() or target['username'])}</h1>
    <div class='card'>
        <p><small>@{h(target['username'])}</small></p>
        <div class='social-stats'>
            <span class='stat-pill'><strong>{len(followers)}</strong> followers</span>
            <span class='stat-pill'><strong>{len(following)}</strong> following</span>
            {follow_action_html}
        </div>
    </div>
    <div class='social-grid'>
        <div class='card'><h3>Followers</h3>{follower_rows}</div>
        <div class='card'><h3>Following</h3>{following_rows}</div>
    </div>
    <div class='card'><h3>Public Uploads</h3>{upload_rows}</div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, status: Optional[str] = None):
    """Render admin user-management table and account creation form."""
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    csrf_token = get_or_create_csrf_token(request)
    conn = get_db_connection()
    users = conn.execute("SELECT id, username, public_name, role, created_at FROM users ORDER BY id ASC").fetchall()
    conn.close()
    rows = "".join([
        f"<tr><td>{h(urow['id'])}</td><td>{h(urow['username'])}</td><td>{h((urow['public_name'] or '').strip() or urow['username'])}</td><td>{h(urow['role'])}</td><td>{h(urow['created_at'])}</td></tr>"
        for urow in users
    ])
    status_html = ""
    if status == "created":
        status_html = "<p class='notice-ok'>Account created.</p>"
    elif status == "error":
        status_html = "<p class='notice-bad'>Could not create account. Check fields and try again.</p>"
    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/admin'>← Back to Admin Control</a>
    <h1>Admin Users</h1>
    <div class='card'>
        <h3>Create User</h3>
        {status_html}
        <form action='/admin/users/create' method='post' style='flex-wrap:wrap;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='text' name='username' placeholder='Username' required>
            <input type='password' name='password' placeholder='Password (min 6 chars)' required>
            <label style='display:flex;align-items:center;gap:6px;'>
                <input type='checkbox' name='make_admin' value='1'>
                Create as admin
            </label>
            <button type='submit' class='btn btn-primary'>Create Account</button>
        </form>
    </div>
    <div class='card'><table><thead><tr><th>ID</th><th>Username</th><th>Public Name</th><th>Role</th><th>Created</th></tr></thead><tbody>{rows}</tbody></table></div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.get("/admin", response_class=HTMLResponse)
def admin_control_page(request: Request):
    """Render top-level admin dashboard with platform summaries and shortcuts."""
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    conn = get_db_connection()
    user_count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    admin_count = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'").fetchone()["c"]
    unread_notifications = conn.execute("SELECT COUNT(*) AS c FROM notifications WHERE read_at IS NULL").fetchone()["c"]
    conn.close()

    note_count = len([f for f in config["notes"].glob("*") if f.suffix in [".md", ".txt"]])
    dataset_count = len([f for f in config["datasets"].glob("*") if f.suffix in [".csv", ".json"]])
    video_count = len([f for f in config["videos"].glob("*") if f.suffix.lower() in [".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"]])

    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back to Library</a>
    <h1>🛠️ Admin Control</h1>

    <div class='card'>
        <div class='social-stats'>
            <span class='stat-pill'><strong>{h(user_count)}</strong> users</span>
            <span class='stat-pill'><strong>{h(admin_count)}</strong> admins</span>
            <span class='stat-pill'><strong>{h(note_count)}</strong> notes</span>
            <span class='stat-pill'><strong>{h(dataset_count)}</strong> datasets</span>
            <span class='stat-pill'><strong>{h(video_count)}</strong> videos</span>
            <span class='stat-pill'><strong>{h(unread_notifications)}</strong> unread notifications</span>
        </div>
    </div>

    <div class='card'>
        <h3>User & Access</h3>
        <div class='nav-pills'>
            <a href='/admin/users' class='btn btn-primary'>Manage Users</a>
            <a href='/messages' class='btn btn-secondary'>Open Messages</a>
            <a href='/notifications' class='btn btn-secondary'>Open Notifications</a>
            <a href='/profile' class='btn btn-secondary'>My Profile</a>
        </div>
        <p class='helper'>Create regular users or admin users from the Manage Users page.</p>
    </div>

    <div class='card'>
        <h3>Content Shortcuts</h3>
        <div class='nav-pills'>
            <a href='/#notes' class='btn btn-secondary'>Notes</a>
            <a href='/#data' class='btn btn-secondary'>Datasets</a>
            <a href='/#videos' class='btn btn-secondary'>Videos</a>
            <a href='/#social' class='btn btn-secondary'>Social</a>
        </div>
    </div>
    </body></html>
    """
    return HTMLResponse(page)

@app.post("/admin/users/create")
def admin_create_user_route(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    make_admin: Optional[str] = Form(None),
    csrf_token: str = Form(""),
):
    """Allow admins to create user/admin accounts from web control panel."""
    validate_csrf(request, csrf_token)
    current = get_current_user(request)
    if not current or current["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    role = "admin" if make_admin else "user"
    clean_user = username.strip().lower()
    try:
        create_user_account(clean_user, password, role=role, public_name=clean_user)
    except HTTPException:
        return RedirectResponse("/admin/users?status=error", status_code=303)
    return RedirectResponse("/admin/users?status=created", status_code=303)

@app.post("/api/auth/register")
def api_register(username: str = Form(...), password: str = Form(...)):
    """Register a user account via API and return an auth token."""
    clean_user = username.strip().lower()
    role = "admin" if get_user_count() == 0 else "user"
    user_row = create_user_account(clean_user, password, role=role, public_name=clean_user)
    token = create_session(user_row["id"])
    return {"token": token, "user": {"id": user_row["id"], "username": user_row["username"], "public_name": user_row["public_name"], "role": user_row["role"]}}

@app.post("/api/admin/users")
def api_admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("user"),
    public_name: str = Form(""),
):
    """Create a user via admin API endpoint using bearer-token auth."""
    user, _ = get_api_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    clean_user = username.strip().lower()
    created = create_user_account(clean_user, password, role=role.strip().lower(), public_name=(public_name or clean_user).strip())
    return {
        "user": {
            "id": created["id"],
            "username": created["username"],
            "public_name": created["public_name"],
            "role": created["role"],
            "created_at": created["created_at"],
        }
    }

@app.post("/api/auth/login")
def api_login(username: str = Form(...), password: str = Form(...)):
    """Authenticate via API and return token plus user profile."""
    clean_user = username.strip().lower()
    conn = get_db_connection()
    user = conn.execute("SELECT id, username, public_name, role, password_hash FROM users WHERE username = ?", (clean_user,)).fetchone()
    conn.close()
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_session(user["id"])
    return {"token": token, "user": {"id": user["id"], "username": user["username"], "public_name": user["public_name"], "role": user["role"]}}

@app.get("/api/me")
def api_me(request: Request):
    """Return authenticated user's profile and unread counters."""
    user, _ = get_api_user(request)
    return {
        "id": user["id"],
        "username": user["username"],
        "public_name": user["public_name"],
        "role": user["role"],
        "unread_messages": get_unread_message_count(user["id"]),
        "unread_notifications": get_unread_notification_count(user["id"]),
    }

@app.get("/api/notes")
def api_list_notes(request: Request):
    """List visible notes and lock metadata for authenticated API caller."""
    user, _ = get_api_user(request)
    note_files = sorted([f.name for f in config["notes"].glob("*") if f.suffix in [".md", ".txt"]])
    items = []
    for name in note_files:
        lock_row = get_note_lock(name)
        can_access_without_password = not lock_row or user_can_bypass_lock(user, lock_row)
        items.append({"filename": name, "locked": bool(lock_row), "can_access_without_password": can_access_without_password})
    return {"notes": items}

@app.get("/api/notes/{filename}")
def api_get_note(request: Request, filename: str, note_password: Optional[str] = None):
    """Return a note body through API, enforcing lock checks when needed."""
    user, _ = get_api_user(request)
    name = ensure_safe_filename(filename)
    file_path = config["notes"] / name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Note not found")
    lock_row = get_note_lock(name)
    if lock_row and not user_can_bypass_lock(user, lock_row):
        if not note_password or not verify_password(note_password, lock_row["password_hash"]):
            raise HTTPException(status_code=403, detail="Note is locked")
    meta, body = parse_note(file_path)
    return {"filename": name, "meta": meta, "content": body, "locked": bool(lock_row)}

@app.post("/api/notes")
def api_create_note(
    request: Request,
    title: str = Form(...),
    content: str = Form(""),
    lock_password: str = Form(""),
    private_note: str = Form(""),
):
    """Create a new note via API with optional locking and privacy settings."""
    user, _ = get_api_user(request)
    clean_title = title.strip()
    if not clean_title:
        raise HTTPException(status_code=400, detail="Title is required")
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_title).strip("_") or "note"
    name = base + ".md"
    save_note(config["notes"] / name, {"title": clean_title}, content or f"# {clean_title}")
    is_public = not bool(private_note)
    if lock_password:
        if len(lock_password) < 4:
            raise HTTPException(status_code=400, detail="Lock password must be at least 4 characters")
        set_note_lock(name, lock_password, user["id"])
        is_public = False
    upsert_file_record("note", name, user["id"], is_public)
    notify_followers_public_upload(user, "note", name)
    return {"filename": name, "locked": bool(lock_password)}

@app.get("/api/messages")
def api_messages(request: Request, mark_read: bool = True):
    """Return inbox/sent message history and optionally mark inbox as read."""
    user, _ = get_api_user(request)
    conn = get_db_connection()
    unread_before = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE recipient_user_id = ? AND read_at IS NULL",
        (user["id"],),
    ).fetchone()["c"]
    if mark_read:
        conn.execute(
            "UPDATE messages SET read_at = ? WHERE recipient_user_id = ? AND read_at IS NULL",
            (datetime.datetime.utcnow().isoformat(), user["id"]),
        )
        conn.commit()
    inbox = conn.execute(
        """
        SELECT messages.id, messages.message_text, messages.created_at, messages.read_at, users.username AS sender_username
        FROM messages
        JOIN users ON users.id = messages.sender_user_id
        WHERE messages.recipient_user_id = ?
        ORDER BY messages.id DESC
        """,
        (user["id"],),
    ).fetchall()
    sent = conn.execute(
        """
        SELECT messages.id, messages.message_text, messages.created_at, messages.read_at, users.username AS recipient_username
        FROM messages
        JOIN users ON users.id = messages.recipient_user_id
        WHERE messages.sender_user_id = ?
        ORDER BY messages.id DESC
        """,
        (user["id"],),
    ).fetchall()
    conn.close()
    return {
        "unread_before_mark": unread_before,
        "inbox": [
            {"id": m["id"], "from": m["sender_username"], "text": m["message_text"], "created_at": m["created_at"], "read_at": m["read_at"]}
            for m in inbox
        ],
        "sent": [
            {"id": m["id"], "to": m["recipient_username"], "text": m["message_text"], "created_at": m["created_at"], "read_at": m["read_at"], "read": bool(m["read_at"])}
            for m in sent
        ],
    }

@app.post("/api/messages")
def api_send_message(request: Request, recipient_username: str = Form(...), message_text: str = Form(...)):
    """Send a direct message via API from authenticated user to recipient."""
    user, _ = get_api_user(request)
    target_name = recipient_username.strip().lower()
    body = message_text.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(body) > 2000:
        raise HTTPException(status_code=400, detail="Message is too long")
    conn = get_db_connection()
    recipient = conn.execute(
        "SELECT id, username FROM users WHERE lower(username) = lower(?)",
        (target_name,),
    ).fetchone()
    if not recipient:
        conn.close()
        raise HTTPException(status_code=404, detail="Recipient not found")
    if recipient["id"] == user["id"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot message yourself")
    conn.execute(
        "INSERT INTO messages (sender_user_id, recipient_user_id, message_text, read_at, created_at) VALUES (?, ?, ?, NULL, ?)",
        (user["id"], recipient["id"], body, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    message_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    return {"id": message_id, "to": recipient["username"], "text": body}

@app.get("/messages", response_class=HTMLResponse)
def messages_page(
    request: Request,
    compose: Optional[str] = None,
    recipient_username: Optional[str] = None,
    message_text: Optional[str] = None,
):
    """Render messaging UI with send form, inbox, and sent history."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    csrf_token = get_or_create_csrf_token(request)
    conn = get_db_connection()
    conn.execute(
        "UPDATE messages SET read_at = ? WHERE recipient_user_id = ? AND read_at IS NULL",
        (datetime.datetime.utcnow().isoformat(), user["id"]),
    )
    conn.commit()
    recipients = conn.execute(
        "SELECT id, username, role FROM users WHERE id != ? ORDER BY username ASC",
        (user["id"],),
    ).fetchall()
    inbox = conn.execute(
        """
        SELECT messages.id, messages.message_text, messages.created_at, users.username AS sender_username
        FROM messages
        JOIN users ON users.id = messages.sender_user_id
        WHERE messages.recipient_user_id = ?
        ORDER BY messages.id DESC
        """,
        (user["id"],),
    ).fetchall()
    sent = conn.execute(
        """
        SELECT messages.id, messages.message_text, messages.created_at, messages.read_at, users.username AS recipient_username
        FROM messages
        JOIN users ON users.id = messages.recipient_user_id
        WHERE messages.sender_user_id = ?
        ORDER BY messages.id DESC
        """,
        (user["id"],),
    ).fetchall()
    conn.close()

    admin_recipient = next((row["username"] for row in recipients if row["role"] == "admin"), "")
    selected_recipient = (recipient_username or "").strip().lower()
    if compose == "issue" and not selected_recipient:
        selected_recipient = admin_recipient

    prefilled_message = (message_text or "").strip()
    if compose == "issue" and not prefilled_message:
        prefilled_message = build_issue_report_template(user)

    recipient_options = "".join([
        f"<option value='{h(r['username'])}'{' selected' if r['username'] == selected_recipient else ''}>{h(r['username'])}{' (admin)' if r['role'] == 'admin' else ''}</option>"
        for r in recipients
    ])

    issue_helper = ""
    if compose == "issue":
        issue_helper = "<p class='helper'>Issue mode: admin recipient is preselected and a report template is prefilled. Add details and click Send.</p>"
    inbox_html = "".join([
        f"<div class='note-item'><div><strong>From:</strong> {h(m['sender_username'])}<br><span>{h(m['message_text'])}</span><br><small>{h(m['created_at'])}</small></div></div>"
        for m in inbox
    ]) or "<p>No messages yet.</p>"
    sent_html = "".join([
        f"<div class='note-item'><div><strong>To:</strong> {h(m['recipient_username'])} · <strong>{'Read' if m['read_at'] else 'Unread'}</strong><br><span>{h(m['message_text'])}</span><br><small>Sent: {h(m['created_at'])}{' · Read: ' + h(m['read_at']) if m['read_at'] else ''}</small></div></div>"
        for m in sent
    ]) or "<p>No sent messages.</p>"

    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <h1>Messages</h1>
    <div class='card'>
        <h3>Send Message</h3>
        {issue_helper}
        <form action='/messages/send' method='post' style='flex-wrap:wrap;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <select name='recipient_username' required style='padding:10px;border:1px solid #ddd;border-radius:4px;'>
                <option value=''>Select recipient</option>
                {recipient_options}
            </select>
            <textarea name='message_text' placeholder='Write a message...' style='flex-grow:1;min-height:120px;' required>{h(prefilled_message)}</textarea>
            <button type='submit' class='btn btn-primary'>Send</button>
        </form>
    </div>
    <div class='card'><h3>Inbox</h3>{inbox_html}</div>
    <div class='card'><h3>Sent</h3>{sent_html}</div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/messages/send")
def send_message_route(
    request: Request,
    recipient_username: str = Form(...),
    message_text: str = Form(...),
    csrf_token: str = Form(""),
):
    """Handle web message submission and persist message in database."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)

    target_name = recipient_username.strip().lower()
    body = message_text.strip()
    if not body:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    if len(body) > 2000:
        raise HTTPException(status_code=400, detail="Message is too long")

    conn = get_db_connection()
    recipient = conn.execute(
        "SELECT id, username FROM users WHERE lower(username) = lower(?)",
        (target_name,),
    ).fetchone()
    if not recipient:
        conn.close()
        raise HTTPException(status_code=404, detail="Recipient not found")
    if recipient["id"] == user["id"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot message yourself")

    conn.execute(
        "INSERT INTO messages (sender_user_id, recipient_user_id, message_text, read_at, created_at) VALUES (?, ?, ?, NULL, ?)",
        (user["id"], recipient["id"], body, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/messages", status_code=303)

@app.post("/follow")
def follow_route(
    request: Request,
    username: str = Form(...),
    next_path: str = Form("/"),
    csrf_token: str = Form(""),
):
    """Create follow relationship from web form and redirect to caller page."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    if not next_path.startswith("/"):
        next_path = "/"
    target_name = username.strip().lower()
    conn = get_db_connection()
    target = conn.execute(
        "SELECT id FROM users WHERE lower(username) = lower(?)",
        (target_name,),
    ).fetchone()
    conn.close()
    if not target:
        return RedirectResponse(f"{next_path}?follow_status=notfound", status_code=303)
    try:
        follow_user(user["id"], target["id"])
    except HTTPException:
        return RedirectResponse(f"{next_path}?follow_status=invalid", status_code=303)
    return RedirectResponse(f"{next_path}?follow_status=ok", status_code=303)

@app.post("/unfollow")
def unfollow_route(
    request: Request,
    username: str = Form(...),
    next_path: str = Form("/"),
    csrf_token: str = Form(""),
):
    """Remove follow relationship from web form and redirect to caller page."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    if not next_path.startswith("/"):
        next_path = "/"
    target_name = username.strip().lower()
    conn = get_db_connection()
    target = conn.execute(
        "SELECT id FROM users WHERE lower(username) = lower(?)",
        (target_name,),
    ).fetchone()
    conn.close()
    if target:
        unfollow_user(user["id"], target["id"])
    return RedirectResponse(f"{next_path}?follow_status=removed", status_code=303)

@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    """Render notifications page and mark unread notifications as read."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    csrf_token = get_or_create_csrf_token(request)
    conn = get_db_connection()
    conn.execute(
        "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
        (datetime.datetime.utcnow().isoformat(), user["id"]),
    )
    conn.commit()
    rows = conn.execute(
        """
        SELECT notifications.message_text, notifications.created_at, users.username AS actor_name
        FROM notifications
        JOIN users ON users.id = notifications.actor_user_id
        WHERE notifications.user_id = ?
        ORDER BY notifications.id DESC
        LIMIT 100
        """,
        (user["id"],),
    ).fetchall()
    conn.close()
    items = "".join([
        f"<div class='note-item'><div><strong>{h(n['actor_name'])}</strong><br>{h(n['message_text'])}<br><small>{h(n['created_at'])}</small></div></div>"
        for n in rows
    ]) or "<p>No notifications yet.</p>"
    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <h1>Notifications</h1>
    <div class='card'>{items}</div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.get("/news/latest")
def latest_news_route(force: int = 0):
    """Return latest news headlines for dashboard refresh calls."""
    items = fetch_latest_news(force_refresh=bool(force))
    return {
        "fetched_at": datetime.datetime.utcnow().isoformat(),
        "items": items,
    }

@app.get("/marketplace", response_class=HTMLResponse)
def marketplace_page(
    request: Request,
    make: str = "toyota",
    model: str = "camry",
    zip_code: str = "30301",
    radius: int = 50,
    max_price: Optional[int] = None,
    mp_status: Optional[str] = None,
):
    """Render a Facebook-style marketplace view with local and AutoTempest listings."""
    csrf_token = get_or_create_csrf_token(request)
    user = get_current_user(request)
    error_message = ""
    web_listings = []
    item_type_options_html = "".join(
        [f"<option value='{h(key)}'>{h(value['label'])}</option>" for key, value in MARKETPLACE_ITEM_TYPES.items()]
    )
    item_type_config_json = json.dumps(MARKETPLACE_ITEM_TYPES)
    try:
        web_listings = extract_autotempest_listings(make, model, zip_code, radius, max_price)
    except Exception:
        error_message = "Could not load AutoTempest listings right now. Please try again."

    community_rows = get_recent_marketplace_listings(limit=48, sold_only=False)
    sold_rows = get_recent_marketplace_listings(limit=48, sold_only=True)
    community_cards_html = ""
    for row in community_rows:
        seller_name = (row["seller_public_name"] or "").strip() or row["seller_username"]
        image_html = ""
        if row["image_url"]:
            image_html = f"<img class='video-thumb' src='{h(row['image_url'])}' alt='Listing image for {h(row['title'])}'>"
        mileage_label = f"{int(row['mileage']):,} mi" if row["mileage"] is not None else ""
        meta_bits = [bit for bit in [mileage_label, row["location"], row["created_at"][:10]] if bit]
        type_label = MARKETPLACE_ITEM_TYPES.get(row["item_type"], MARKETPLACE_ITEM_TYPES["other"])["label"]
        details_dict = parse_marketplace_item_details(row["item_details_json"])
        details_line = " · ".join([str(v) for v in details_dict.values() if str(v).strip()])
        meta_line = " · ".join(meta_bits)
        description = (row["description"] or "").strip()
        if len(description) > 140:
            description = description[:137] + "..."
        seller_actions = ""
        if user and user["id"] == row["seller_user_id"]:
            seller_actions = f"""
            <form action='/marketplace/listings/{int(row['id'])}/sold' method='post' style='margin:0;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <button type='submit' class='btn btn-success'>Mark Sold</button>
            </form>
            """
        community_cards_html += f"""
        <div class='video-card'>
            {image_html}
            <div style='font-size:1.1em;font-weight:700;'>${int(row['price']):,}</div>
            <div class='video-title'>{h(row['title'])}</div>
            <div class='helper'><strong>{h(type_label)}</strong>{(' · ' + h(details_line)) if details_line else ''}</div>
            <div class='helper'>{h(meta_line)}</div>
            <div class='helper' style='min-height:2.3em;'>{h(description)}</div>
            <div class='video-actions'>
                <a href='/u/{u(row['seller_username'])}' class='btn btn-secondary'>Seller: {h(seller_name)}</a>
                <a href='/marketplace/listings/{int(row['id'])}/message' class='btn btn-primary'>{'Message' if user else 'Sign In to Message'}</a>
                {seller_actions}
            </div>
        </div>
        """

    sold_cards_html = ""
    for row in sold_rows:
        seller_name = (row["seller_public_name"] or "").strip() or row["seller_username"]
        image_html = ""
        if row["image_url"]:
            image_html = f"<img class='video-thumb' src='{h(row['image_url'])}' alt='Listing image for {h(row['title'])}'>"
        sold_date = (row["sold_at"] or row["created_at"] or "")[:10]
        mileage_label = f"{int(row['mileage']):,} mi" if row["mileage"] is not None else ""
        type_label = MARKETPLACE_ITEM_TYPES.get(row["item_type"], MARKETPLACE_ITEM_TYPES["other"])["label"]
        details_dict = parse_marketplace_item_details(row["item_details_json"])
        details_line = " · ".join([str(v) for v in details_dict.values() if str(v).strip()])
        meta_bits = [bit for bit in [mileage_label, row["location"], f"Sold {sold_date}" if sold_date else "Sold"] if bit]
        meta_line = " · ".join(meta_bits)
        sold_cards_html += f"""
        <div class='video-card'>
            {image_html}
            <div style='font-size:1.1em;font-weight:700;'>${int(row['price']):,} <span class='helper' style='font-weight:600;'>(SOLD)</span></div>
            <div class='video-title'>{h(row['title'])}</div>
            <div class='helper'><strong>{h(type_label)}</strong>{(' · ' + h(details_line)) if details_line else ''}</div>
            <div class='helper'>{h(meta_line)}</div>
            <div class='video-actions'>
                <a href='/u/{u(row['seller_username'])}' class='btn btn-secondary'>Seller: {h(seller_name)}</a>
            </div>
        </div>
        """

    web_cards_html = ""
    for row in web_listings:
        image_html = ""
        if row.get("image"):
            image_html = f"<a href='{h(row['link'])}' target='_blank' rel='noopener'><img class='video-thumb' src='{h(row['image'])}' alt='Listing image for {h(row['title'])}'></a>"
        meta_bits = [bit for bit in [row.get("price"), row.get("miles"), row.get("location")] if bit]
        meta_line = " · ".join(meta_bits)
        web_cards_html += f"""
        <div class='video-card'>
            {image_html}
            <div style='font-size:1.1em;font-weight:700;'>{h(row['price'])}</div>
            <div class='video-title'>{h(row['title'])}</div>
            <div class='helper'>{h(meta_line)}</div>
            <div class='video-actions'>
                <span class='helper'>{h(row['source'])}</span>
                <a href='{h(row['link'])}' target='_blank' rel='noopener' class='btn btn-primary'>Open Listing ↗</a>
            </div>
        </div>
        """

    if not community_cards_html:
        community_cards_html = "<div class='card'><p>No local listings yet. Be the first to post one.</p></div>"
    if not sold_cards_html:
        sold_cards_html = "<div class='card'><p>No sold listings yet.</p></div>"
    if not web_cards_html and not error_message:
        web_cards_html = "<div class='card'><p>No AutoTempest listings were found for this search.</p></div>"

    notice_lines = []
    if mp_status == "created":
        notice_lines.append("<div class='card notice-ok'>Your listing is live.</div>")
    elif mp_status == "sold":
        notice_lines.append("<div class='card notice-ok'>Listing marked as sold.</div>")
    elif mp_status == "error":
        notice_lines.append("<div class='card notice-bad'>Could not create listing. Check your input and try again.</div>")
    if error_message:
        notice_lines.append(f"<div class='card notice-bad'>{h(error_message)}</div>")
    notice_html = "".join(notice_lines)

    create_listing_html = ""
    if user:
        create_listing_html = f"""
        <div class='card'>
            <h2>Create Listing</h2>
            <form action='/marketplace/listings/create' method='post' style='display:flex;gap:8px;flex-wrap:wrap;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <input type='text' name='title' placeholder='Listing title (e.g., 2021 Camry SE)' style='flex:2;min-width:220px;' required>
                <select name='item_type' id='mpItemType' style='min-width:180px;padding:10px;border:1px solid #ddd;border-radius:4px;'>
                    {item_type_options_html}
                </select>
                <input type='text' id='mpDetailA' name='detail_a' placeholder='Detail 1' style='min-width:160px;'>
                <input type='text' id='mpDetailB' name='detail_b' placeholder='Detail 2' style='min-width:160px;'>
                <input type='text' id='mpDetailC' name='detail_c' placeholder='Detail 3' style='min-width:160px;'>
                <input type='number' name='price' min='100' max='5000000' step='100' placeholder='Price' style='width:130px;' required>
                <input type='text' name='location' placeholder='City, ST' style='width:180px;' required>
                <input type='number' name='mileage' min='0' max='2000000' step='100' placeholder='Mileage' style='width:120px;'>
                <input type='text' name='image_url' placeholder='Image URL (optional)' style='flex:2;min-width:220px;'>
                <textarea name='description' rows='2' placeholder='Description (optional)' style='flex:1 1 100%;'></textarea>
                <button type='submit' class='btn btn-success'>Post Listing</button>
            </form>
        </div>
        """
    else:
        create_listing_html = "<div class='card'><h2>Create Listing</h2><p>Sign in to post your own listing.</p><a href='/auth/login' class='btn btn-secondary'>Sign In</a></div>"

    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/'>← Back</a>
    <h1>🛒 Marketplace</h1>
    <div class='card'>
        <h2>Browse Marketplace</h2>
        <form action='/marketplace' method='get' style='display:flex;gap:8px;flex-wrap:wrap;'>
            <input type='text' name='make' value='{h(make)}' placeholder='Make (e.g., toyota)' required>
            <input type='text' name='model' value='{h(model)}' placeholder='Model (e.g., camry)' required>
            <input type='text' name='zip_code' value='{h(zip_code)}' placeholder='ZIP code' required>
            <input type='number' name='radius' value='{h(radius)}' min='10' max='500' step='5' placeholder='Radius'>
            <input type='number' name='max_price' value='{h(max_price or "")}' min='500' step='500' placeholder='Max price'>
            <button type='submit' class='btn btn-primary'>Search Auto Listings</button>
        </form>
        <div class='helper' style='margin-top:8px;'>Facebook-style feed: local community listings + live AutoTempest listings.</div>
    </div>
    {create_listing_html}
    {notice_html}
    <div class='card'>
        <h2>Local Community Listings</h2>
        <div class='video-grid'>
            {community_cards_html}
        </div>
    </div>
    <div class='card'>
        <h2>Sold Listings</h2>
        <div class='helper'>Listings sellers mark as sold stay visible here.</div>
        <div class='video-grid'>
            {sold_cards_html}
        </div>
    </div>
    <div class='card'>
        <h2>AutoTempest Listings</h2>
        <div class='helper'>External listings scraped from AutoTempest based on your search filters.</div>
    </div>
    <div class='video-grid'>
        {web_cards_html}
    </div>
    <script>
    (function() {{
        const itemTypeSelect = document.getElementById('mpItemType');
        const detailA = document.getElementById('mpDetailA');
        const detailB = document.getElementById('mpDetailB');
        const detailC = document.getElementById('mpDetailC');
        const config = {item_type_config_json};
        function updateDetailPlaceholders() {{
            if (!itemTypeSelect || !detailA || !detailB || !detailC) return;
            const key = itemTypeSelect.value || 'other';
            const labels = (config[key] && config[key].detail_labels) ? config[key].detail_labels : ['Detail 1','Detail 2','Detail 3'];
            detailA.placeholder = labels[0] || 'Detail 1';
            detailB.placeholder = labels[1] || 'Detail 2';
            detailC.placeholder = labels[2] || 'Detail 3';
        }}
        if (itemTypeSelect) {{
            itemTypeSelect.addEventListener('change', updateDetailPlaceholders);
            updateDetailPlaceholders();
        }}
    }})();
    </script>
    </body></html>
    """

    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/marketplace/listings/create")
def marketplace_create_listing_route(
    request: Request,
    csrf_token: str = Form(""),
    title: str = Form(...),
    item_type: str = Form("other"),
    detail_a: str = Form(""),
    detail_b: str = Form(""),
    detail_c: str = Form(""),
    price: int = Form(...),
    location: str = Form(...),
    mileage: Optional[int] = Form(None),
    description: str = Form(""),
    image_url: str = Form(""),
):
    """Create a user-owned marketplace listing and redirect back to marketplace."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    try:
        create_marketplace_listing(
            seller_user_id=user["id"],
            title=title,
            item_type=item_type,
            detail_a=detail_a,
            detail_b=detail_b,
            detail_c=detail_c,
            price=price,
            location=location,
            mileage=mileage,
            description=description,
            image_url=image_url,
        )
    except Exception:
        return RedirectResponse("/marketplace?mp_status=error", status_code=303)
    return RedirectResponse("/marketplace?mp_status=created", status_code=303)

@app.post("/marketplace/listings/{listing_id}/sold")
def marketplace_mark_sold_route(
    listing_id: int,
    request: Request,
    csrf_token: str = Form(""),
):
    """Allow a seller to mark their own marketplace listing as sold."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/auth/login", status_code=303)
    try:
        mark_marketplace_listing_sold(listing_id, user["id"])
    except Exception:
        return RedirectResponse("/marketplace?mp_status=error", status_code=303)
    return RedirectResponse("/marketplace?mp_status=sold", status_code=303)

@app.post("/home/panels")
def save_home_panel_preferences_route(
    request: Request,
    hidden_panels_json: str = Form("[]"),
    csrf_token: str = Form(""),
):
    """Persist signed-in user's home panel visibility preferences."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    try:
        hidden_panels = json.loads(hidden_panels_json)
    except Exception:
        hidden_panels = []
    set_user_home_hidden_panels(user["id"], hidden_panels)
    return {"ok": True, "hidden_panels": normalize_home_hidden_panels(hidden_panels)}

# Main web home/dashboard route
@app.get("/", response_class=HTMLResponse)
def web_home(
    request: Request,
    q: Optional[str] = None,
    text_q: Optional[str] = None,
    text_status: Optional[str] = None,
    imported_name: Optional[str] = None,
    yt_q: Optional[str] = None,
    yt_status: Optional[str] = None,
    imported_video: Optional[str] = None,
    follow_status: Optional[str] = None,
    data_status: Optional[str] = None,
):
    """Render the main dashboard aggregating uploads, discovery, and social features."""
    csrf_token = get_or_create_csrf_token(request)
    user = get_current_user(request)
    account_hidden_panels = get_user_home_hidden_panels(user["id"]) if user else []
    account_hidden_panels_json = json.dumps(account_hidden_panels)
    setup_checks = get_setup_checks()
    missing_checks = [item for item in setup_checks if not item["ok"]]
    auth_html = ""
    if user:
        unread_count = get_unread_message_count(user["id"])
        unread_notifications = get_unread_notification_count(user["id"])
        admin_link = ""
        if user["role"] == "admin":
            admin_link = "<a href='/admin' class='btn btn-secondary'>Admin</a>"
        msg_label = f"Messages ({unread_count})" if unread_count else "Messages"
        notif_label = f"Notifications ({unread_notifications})" if unread_notifications else "Notifications"
        auth_html = f"<div class='card' style='display:flex;justify-content:space-between;align-items:center;'><div>Signed in as <strong>{h(display_name(user))}</strong> <small>@{h(user['username'])}</small> ({h(user['role'])})</div><div style='display:flex;gap:8px;'><a href='/profile' class='btn btn-secondary'>Profile</a><a href='/notifications' class='btn btn-secondary'>{h(notif_label)}</a><a href='/messages?compose=issue' class='btn btn-danger'>Report Issue</a><a href='/messages' class='btn btn-secondary'>{h(msg_label)}</a><a href='/account' class='btn btn-secondary'>Security</a>{admin_link}<a href='/auth/logout' class='btn btn-secondary'>Logout</a></div></div>"
    else:
        auth_html = "<div class='card' style='display:flex;justify-content:space-between;align-items:center;'><div>Browsing as guest</div><div style='display:flex;gap:8px;'><a href='/auth/login' class='btn btn-secondary'>Login</a><a href='/auth/register' class='btn btn-primary'>Register</a></div></div>"

    global_notice_html = ""
    if data_status == "uploaded":
        global_notice_html = "<div class='card notice-ok'>Dataset uploaded.</div>"
    elif data_status == "deleted":
        global_notice_html = "<div class='card notice-ok'>Dataset deleted.</div>"
    elif data_status == "saved":
        global_notice_html = "<div class='card notice-ok'>Dataset saved.</div>"
    elif data_status == "error":
        global_notice_html = "<div class='card notice-bad'>Dataset action failed.</div>"

    quick_nav_html = """
    <div class='card home-toolbar-card'>
        <div class='nav-pills'>
            <a href='#uploads' class='btn btn-secondary'>Uploads</a>
            <a href='/marketplace' class='btn btn-secondary'>Marketplace</a>
            <a href='#recommendations' class='btn btn-secondary'>Recommendations</a>
            <a href='#news' class='btn btn-secondary'>News</a>
            <a href='#notes' class='btn btn-secondary'>Notes</a>
            <a href='#data' class='btn btn-secondary'>Data</a>
            <a href='#videos' class='btn btn-secondary'>Videos</a>
            <a href='#social' class='btn btn-secondary'>Social</a>
            <a href='#games' class='btn btn-secondary'>Games</a>
            <button id='resetPanelsBtn' type='button' class='btn btn-secondary'>Reset Panels</button>
            <span id='collapsedPanelsCount' class='helper' style='margin:0 0 0 auto;'>0 hidden</span>
        </div>
    </div>
    """

    # Action Forms
    actions_html = f"""
    <div class='card' id='uploads'>
        <h2>Quick Uploads</h2>
        <form action='/notes/create' method='post' style='margin-bottom:15px;flex-wrap:wrap;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='text' name='filename' placeholder='New note title...' style='flex-grow:1' required>
            <label style='display:flex;align-items:center;gap:6px;font-size:0.9em;'>
                <input type='checkbox' name='private_note' value='1'>
                Private note
            </label>
            <label style='display:flex;align-items:center;gap:6px;font-size:0.9em;'>
                <input type='checkbox' name='lock_note' value='1'>
                Locked note
            </label>
            <input type='password' name='lock_password' placeholder='Lock password (optional unless locked)'>
            <button type='submit' class='btn btn-primary'>+ Create Note</button>
        </form>
        <div class='helper'>Use Private or Locked for files that should not notify followers.</div>
        <form action='/datasets/import' method='post' enctype='multipart/form-data'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='file' name='file' accept='.csv,.json' style='flex-grow:1' required>
            <label style='display:flex;align-items:center;gap:6px;font-size:0.9em;'>
                <input type='checkbox' name='private_upload' value='1'>
                Private
            </label>
            <button type='submit' class='btn btn-success'>📥 Upload Data</button>
        </form>
        <div class='helper'>CSV and JSON are supported. You can edit or delete datasets after upload.</div>
        <form action='/videos/import' method='post' enctype='multipart/form-data' style='margin-top:10px;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='file' name='file' accept='video/*,.mp4,.mov,.m4v,.webm,.avi,.mkv' style='flex-grow:1' required>
            <label style='display:flex;align-items:center;gap:6px;font-size:0.9em;'>
                <input type='checkbox' name='private_upload' value='1'>
                Private
            </label>
            <button type='submit' class='btn btn-success'>🎬 Upload Video</button>
        </form>
    </div>
    <div class='card'>
        <form action='/' method='get' style='flex-wrap:wrap;'>
            <input type='text' name='text_q' placeholder='Search public text files (books)...' style='flex-grow:1' value='{h(text_q or "")}'>
            <button type='submit' class='btn btn-success'>🔎 Search Public Texts</button>
        </form>
    </div>
    <div class='card'>
        <form action='/' method='get' style='flex-wrap:wrap;'>
            <input type='text' name='yt_q' placeholder='Search YouTube videos...' style='flex-grow:1' value='{h(yt_q or "")}'>
            <button type='submit' class='btn btn-success'>🎥 Search YouTube</button>
        </form>
        <form action='/videos/import-youtube' method='post' style='margin-top:10px;flex-wrap:wrap;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='text' name='video_url' placeholder='Or paste YouTube URL to import directly...' style='flex-grow:1' required>
            <label style='display:flex;align-items:center;gap:6px;font-size:0.9em;'>
                <input type='checkbox' name='private_upload' value='1'>
                Private
            </label>
            <button type='submit' class='btn btn-primary'>Import URL</button>
        </form>
        <div id='ytProgressWrap' class='progress-wrap' style='display:none;'>
            <div id='ytProgressLabel' class='progress-label'>Preparing download...</div>
            <progress id='ytProgressBar' class='progress-value' value='0' max='100'></progress>
        </div>
    </div>
    <form action='/' method='get' style='margin-bottom:20px;'>
        <input type='text' name='q' placeholder='Search notes, tags, or content...' style='flex-grow:1' value='{h(q or "")}'>
        <button type='submit' class='btn btn-primary'>Search</button>
    </form>"""

    text_search_status_html = ""
    if text_status == "imported":
        text_search_status_html = f"<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Imported as {h(imported_name or 'note')}.</p>"
    elif text_status == "error":
        text_search_status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>Could not import that text file.</p>"

    text_search_results = []
    text_search_error = None
    if text_q:
        try:
            text_search_results = search_public_texts(text_q)
        except Exception:
            text_search_error = "Search failed. Please try again."

    text_results_html = ""
    for item in text_search_results:
        title = item["title"]
        source_url = item["source_url"]
        text_results_html += f"""
        <div class='note-item'>
            <div>
                <strong>{h(title)}</strong><br>
                <small>{h(item['authors'])}</small> · <a href='{h(source_url)}' target='_blank' rel='noopener'>Preview source</a>
            </div>
            <form action='/texts/import' method='post' style='margin:0;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <input type='hidden' name='source_url' value='{h(source_url)}'>
                <input type='hidden' name='title' value='{h(title)}'>
                <input type='hidden' name='text_q' value='{h(text_q or "")}'>
                <label style='display:flex;align-items:center;gap:6px;font-size:0.85em;'>
                    <input type='checkbox' name='private_upload' value='1'> Private
                </label>
                <button type='submit' class='btn btn-primary'>Import as Note</button>
            </form>
        </div>"""

    text_search_html = ""
    if text_q or text_status:
        if text_search_error:
            text_results_html = f"<p>{h(text_search_error)}</p>"
        elif not text_results_html and text_q:
            text_results_html = "<p>No public text files found.</p>"
        text_search_html = f"<div class='card dashboard-panel' id='text-search'><h2>📚 Public Text Search</h2>{text_search_status_html}{text_results_html}</div>"

    yt_search_status_html = ""
    if yt_status == "imported":
        yt_search_status_html = f"<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Imported video {h(imported_video or '')}.</p>"
    elif yt_status == "error":
        yt_search_status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>Could not search or import YouTube video. Ensure yt-dlp is installed.</p>"

    yt_results_html = ""
    if yt_q:
        try:
            yt_results = search_youtube_videos(yt_q)
            for item in yt_results:
                duration_label = ""
                if isinstance(item.get("duration"), int):
                    total = item["duration"]
                    duration_label = f" · {total // 60}:{total % 60:02d}"
                yt_results_html += f"""
                <div class='note-item'>
                    <div>
                        <strong>{h(item['title'])}</strong><br>
                        <small>{h(item['uploader'])}{h(duration_label)}</small>
                    </div>
                    <form action='/videos/import-youtube' method='post' class='yt-import-form' style='margin:0;'>
                        <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                        <input type='hidden' name='video_url' value='{h(item['video_url'])}'>
                        <input type='hidden' name='yt_q' value='{h(yt_q or "")}'>
                        <label style='display:flex;align-items:center;gap:6px;font-size:0.85em;'>
                            <input type='checkbox' name='private_upload' value='1'> Private
                        </label>
                        <button type='submit' class='btn btn-primary'>Import Video</button>
                    </form>
                </div>
                """
            if not yt_results_html:
                yt_results_html = "<p>No YouTube videos found.</p>"
        except Exception:
            yt_results_html = "<p>Search failed. Ensure yt-dlp is installed and try again.</p>"

    yt_search_html = ""
    if yt_q or yt_status:
        yt_search_html = f"<div class='card dashboard-panel' id='youtube-search'><h2>🎥 YouTube Search</h2>{yt_search_status_html}{yt_results_html}</div>"

    setup_html = ""
    if missing_checks:
        rows = "".join([
            f"<div class='note-item'><div><strong>{h(item['name'])}</strong><br><small>{h(item['required_for'])}</small></div><code>{h(item['install'])}</code></div>"
            for item in missing_checks
        ])
        setup_html = f"""
        <div class='card dashboard-panel' id='setup'>
            <h2>⚙️ Setup Required</h2>
            <p>Install the missing tools below to enable all website features:</p>
            {rows}
        </div>
        """
    elif text_q or yt_q or text_status or yt_status:
        setup_html = "<div class='card dashboard-panel' id='setup'><h2>⚙️ Setup</h2><p>All optional tooling is available.</p></div>"

    recommendations_html = ""
    if user:
        recs = get_recommended_public_files_for_user(user["id"], limit=18)
        if recs:
            rec_rows = ""
            for row in recs:
                link = file_link_by_type(row["file_type"], row["filename"])
                if not link:
                    continue
                owner_name = (row["owner_public_name"] or "").strip() or row["owner_username"]
                rec_rows += f"""
                <div class='note-item'>
                    <div>
                        <strong>{h(row['filename'])}</strong><br>
                        <small>{h(row['file_type'])} from <a href='/u/{u(row['owner_username'])}'>{h(owner_name)}</a> · {h(row['created_at'])}</small>
                    </div>
                    <a class='btn btn-primary' href='{h(link)}'>Open</a>
                </div>
                """
            if rec_rows:
                recommendations_html = f"<div class='card dashboard-panel' id='recommendations'><h2>✨ From People You Follow</h2>{rec_rows}</div>"
        elif get_following_rows(user["id"]):
            recommendations_html = "<div class='card dashboard-panel' id='recommendations'><h2>✨ From People You Follow</h2><p>No recent public uploads from people you follow.</p></div>"
        else:
            recommendations_html = "<div class='card dashboard-panel' id='recommendations'><h2>✨ From People You Follow</h2><p>Follow users to get personalized recommendations here.</p></div>"
    else:
        recommendations_html = "<div class='card dashboard-panel' id='recommendations'><h2>✨ From People You Follow</h2><p>Sign in and follow users to get personalized recommendations.</p></div>"

    # Build latest-news card content (cached server-side; refreshed client-side).
    news_items = fetch_latest_news()
    news_rows = render_news_rows_html(news_items)
    news_html = f"""
    <div class='card dashboard-panel' id='news'>
        <div style='display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;'>
            <h2 style='margin:0;'>🗞️ Latest News</h2>
            <div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;'>
                <small id='newsUpdatedLabel' class='helper' style='margin:0;'>Updated just now · Auto-refresh every 5 minutes</small>
                <button id='refreshNewsBtn' type='button' class='btn btn-secondary'>Refresh News</button>
            </div>
        </div>
        <div id='newsRows'>{news_rows}</div>
    </div>
    """

    games_html = """
    <div class='card dashboard-panel' id='games'>
        <h2>🎮 Mini Games</h2>
        <div class='nav-pills'>
            <a href='/games/tetris' class='btn btn-primary'>Tetris Style</a>
            <a href='/games/frogger' class='btn btn-success'>Frogger Style</a>
            <a href='/games/word-guess' class='btn btn-secondary'>Word Guess</a>
            <a href='/games/hangman' class='btn btn-secondary'>Hangman</a>
            <a href='/games/leaderboard' class='btn btn-secondary'>Leaderboard</a>
            <a href='/games' class='btn btn-secondary'>Games Hub</a>
        </div>
        <p class='helper'>Quick break mode: play directly in your browser and return to your library anytime.</p>
    </div>
    """

    # Library Content
    notes_raw = sorted([f.name for f in config["notes"].glob("*") if f.suffix in ['.md', '.txt'] and file_visible_to_user("note", f.name, user)])
    datasets_raw = sorted([f.name for f in config["datasets"].glob("*") if f.suffix in ['.csv', '.json'] and file_visible_to_user("dataset", f.name, user)])
    videos_raw = sorted([f.name for f in config["videos"].glob("*") if f.suffix.lower() in ['.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'] and file_visible_to_user("video", f.name, user)])

    if q:
        q = q.lower()
        notes_raw = [n for n in notes_raw if q in n.lower() or q in (config["notes"]/n).read_text().lower()]
        datasets_raw = [d for d in datasets_raw if q in d.lower()]
        videos_raw = [v for v in videos_raw if q in v.lower()]

    chat_html = ""
    if user:
        unread_count = get_unread_message_count(user["id"])
        conn = get_db_connection()
        recipients = conn.execute(
            "SELECT username, role FROM users WHERE id != ? ORDER BY username ASC",
            (user["id"],),
        ).fetchall()
        recent_inbox = conn.execute(
            """
            SELECT messages.message_text, messages.created_at, users.username AS sender_username
            FROM messages
            JOIN users ON users.id = messages.sender_user_id
            WHERE messages.recipient_user_id = ?
            ORDER BY messages.id DESC
            LIMIT 5
            """,
            (user["id"],),
        ).fetchall()
        conn.close()

        recipient_options = "".join([
            f"<option value='{h(r['username'])}'>{h(r['username'])}{' (admin)' if r['role'] == 'admin' else ''}</option>" for r in recipients
        ])
        recent_html = "".join([
            f"<div class='chat-item'><div class='chat-meta'>From {h(msg['sender_username'])} · {h(msg['created_at'])}</div><div>{h(msg['message_text'])}</div></div>"
            for msg in recent_inbox
        ]) or "<p>No recent messages.</p>"

        chat_html = f"""
        <div class='card'>
            <h2>💬 Chat {f'({unread_count} unread)' if unread_count else ''}</h2>
            <form action='/messages/send' method='post' style='flex-wrap:wrap;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <select name='recipient_username' required style='padding:10px;border:1px solid #ddd;border-radius:4px;'>
                    <option value=''>Recipient</option>
                    {recipient_options}
                </select>
                <input type='text' name='message_text' placeholder='Type a message...' style='flex-grow:1' required>
                <button type='submit' class='btn btn-primary'>Send</button>
            </form>
            <div class='chat-list'>{recent_html}</div>
            <div style='margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;'><a href='/messages' class='btn btn-secondary'>Open Full Chat</a><a href='/messages?compose=issue' class='btn btn-danger'>Report Issue</a></div>
        </div>
        """
    else:
        chat_html = """
        <div class='card'>
            <h2>💬 Chat</h2>
            <p>Sign in to message other users.</p>
            <a href='/auth/login' class='btn btn-secondary'>Sign In</a>
        </div>
        """

    follow_html = ""
    if user:
        following = get_following_rows(user["id"])
        followers = get_follower_rows(user["id"])

        following_rows = "".join([
            f"<div class='social-user'><div><div><a href='/u/{u(row['username'])}'><strong>{h((row['public_name'] or '').strip() or row['username'])}</strong></a></div><div class='social-username'>@{h(row['username'])}</div></div><form action='/unfollow' method='post' style='margin:0;'><input type='hidden' name='csrf_token' value='{h(csrf_token)}'><input type='hidden' name='username' value='{h(row['username'])}'><button type='submit' class='btn btn-secondary'>Unfollow</button></form></div>"
            for row in following[:8]
        ]) or "<p>No following yet.</p>"

        follower_rows = "".join([
            f"<div class='social-user'><div><div><a href='/u/{u(row['username'])}'><strong>{h((row['public_name'] or '').strip() or row['username'])}</strong></a></div><div class='social-username'>@{h(row['username'])}</div></div></div>"
            for row in followers[:8]
        ]) or "<p>No followers yet.</p>"

        follow_status_html = ""
        if follow_status == "ok":
            follow_status_html = "<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Followed user.</p>"
        elif follow_status == "removed":
            follow_status_html = "<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Unfollowed user.</p>"
        elif follow_status == "notfound":
            follow_status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>User not found.</p>"
        elif follow_status == "invalid":
            follow_status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>Cannot follow that user.</p>"

        follow_html = f"""
        <div class='card' id='social'>
            <h2>📸 Social</h2>
            {follow_status_html}
            <div class='social-stats'>
                <span class='stat-pill'><strong>{len(followers)}</strong> followers</span>
                <span class='stat-pill'><strong>{len(following)}</strong> following</span>
                <a href='/profile' class='btn btn-secondary'>Open Profile</a>
            </div>
            <form action='/follow' method='post' style='flex-wrap:wrap;margin-bottom:10px;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <input type='text' name='username' placeholder='Username to follow' style='flex-grow:1' required>
                <button type='submit' class='btn btn-primary'>Follow</button>
            </form>
            <div class='social-grid'>
                <div class='social-col'><div class='social-title'>Followers</div>{follower_rows}</div>
                <div class='social-col'><div class='social-title'>Following</div>{following_rows}</div>
            </div>
        </div>
        """

    notes_html_parts = []
    for n in notes_raw:
        lock_row = get_note_lock(n)
        lock_badge = "<span class='badge-lock'>Locked</span>" if lock_row else ""
        notes_html_parts.append(f"<div class='note-item'><span>{h(n)} {lock_badge}</span><a href='/notes/{u(n)}' class='btn btn-primary'>View</a></div>")
    notes_html = "".join(notes_html_parts)
    
    datasets_html = ""
    for d_name in datasets_raw:
        info = get_dataset_info(d_name)
        if not info: continue
        headers = "".join([f"<th>{h(k)}</th>" for k in info['cols']])
        rows = "".join([f"<tr>{''.join([f'<td>{h(v)}</td>' for v in r.values()])}</tr>" for r in info['preview']])
        d_name_u = u(d_name)
        datasets_html += f"""
        <div class='card'>
            <div style='display:flex;justify-content:space-between;align-items:center;'>
                <strong>📊 {h(d_name)}</strong>
                <div style='display:flex;gap:8px;'>
                    <a href='/datasets/{d_name_u}/full' class='btn btn-primary' style='font-size:0.7em;'>Full View ↗</a>
                    <a href='/datasets/{d_name_u}/edit' class='btn btn-secondary' style='font-size:0.7em;'>Edit</a>
                    <form action='/datasets/{d_name_u}/delete' method='post' style='margin:0;' onsubmit='return confirm("Delete dataset?")'>
                        <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                        <button type='submit' class='btn btn-danger' style='font-size:0.7em;'>Delete</button>
                    </form>
                </div>
            </div>
            <div class='preview-box'><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div>
        </div>"""

    videos_html = ""
    for v_name in videos_raw:
        v_name_u = u(v_name)
        videos_html += f"""
        <div class='video-card'>
            <a href='/videos/{v_name_u}'><img class='video-thumb' src='/videos/{v_name_u}/thumbnail' alt='thumbnail for {h(v_name)}'></a>
            <div class='video-title'>{h(v_name)}</div>
            <div class='video-actions'>
                <a href='/videos/{v_name_u}' class='btn btn-primary'>Watch ▶</a>
                <form action='/videos/{v_name_u}/delete' method='post' onsubmit='return confirm("Delete video?")'>
                    <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                    <button type='submit' class='btn btn-danger'>Delete</button>
                </form>
            </div>
        </div>"""

    # Home dashboard layout: primary content on left, contextual/social panels on right.
    primary_column_html = f"""
    <div class='stack home-col-primary'>
        <div class='card section-card'><h3 class='section-title'>Work Area</h3><p class='helper'>Create and search content, then manage notes, data, and videos.</p></div>
        {actions_html.replace("<div class='card' id='uploads'>", "<div class='card dashboard-panel section-card' id='uploads'>")}
        {text_search_html}
        {yt_search_html}
        <div class='card section-card dashboard-panel' id='notes'><h2>📝 Notes</h2>{notes_html or '<p>No notes found.</p>'}</div>
        <div class='card section-card dashboard-panel' id='data'><h2>📊 Data</h2>{datasets_html or '<p>No data found.</p>'}</div>
        <div class='card section-card dashboard-panel' id='videos'><h2>🎬 Videos</h2><div class='video-grid'>{videos_html or '<p>No videos found.</p>'}</div></div>
    </div>
    """
    secondary_column_html = f"""
    <div class='stack home-col-secondary home-col-secondary-sticky'>
        <div class='card section-card'><h3 class='section-title'>Updates & Community</h3><p class='helper'>Setup status, recommendations, news, games, chat, and social activity.</p></div>
        {setup_html}
        {recommendations_html}
        {news_html}
        {games_html}
        {chat_html.replace("<div class='card'>", "<div class='card dashboard-panel' id='chat'>", 1)}
        {follow_html.replace("<div class='card' id='social'>", "<div class='card dashboard-panel' id='social'>")}
    </div>
    """

    page = f"""<html><head>{COMMON_STYLE}</head><body><h1>🚀 Library</h1>{auth_html}{global_notice_html}{quick_nav_html}<div class='home-grid'>{primary_column_html}{secondary_column_html}</div></body><script>
    (function() {{
        // Dashboard client helpers: YouTube import polling + latest-news refresh UI.
        const progressWrap = document.getElementById('ytProgressWrap');
        const progressBar = document.getElementById('ytProgressBar');
        const progressLabel = document.getElementById('ytProgressLabel');
        const forms = Array.from(document.querySelectorAll("form[action='/videos/import-youtube']"));
        const refreshNewsBtn = document.getElementById('refreshNewsBtn');
        const newsRows = document.getElementById('newsRows');
        const newsUpdatedLabel = document.getElementById('newsUpdatedLabel');
        const resetPanelsBtn = document.getElementById('resetPanelsBtn');
        const collapsedPanelsCount = document.getElementById('collapsedPanelsCount');
        const panelStorageKey = 'fp_hidden_home_panels';
        const isSignedInUser = """ + ("true" if user else "false") + """;
        const accountHiddenPanels = """ + account_hidden_panels_json + """;
        const allPanelIds = """ + json.dumps(HOME_PANEL_IDS) + """;
        let newsLastUpdatedMs = Date.now();

        function escapeHtml(value) {{
            return String(value || '')
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#39;');
        }}

        function renderNewsRows(items) {{
            if (!newsRows) return;
            newsRows.innerHTML = (items || []).map(function(item) {{
                const source = escapeHtml((item && item.source) ? item.source : 'Source');
                const title = escapeHtml((item && item.title) ? item.title : 'News temporarily unavailable');
                const link = (item && item.link) ? item.link : '';
                if (item && item.ok && link) {{
                    return "<div class='note-item'><div><strong>" + source + "</strong><br><a href='" + link + "' target='_blank' rel='noopener'>" + title + "</a></div></div>";
                }}
                return "<div class='note-item'><div><strong>" + source + "</strong><br><small>" + title + "</small></div></div>";
            }}).join('');
        }}

        function formatRelativeAge(msSinceUpdate) {{
            const seconds = Math.floor(msSinceUpdate / 1000);
            if (seconds < 10) return 'just now';
            if (seconds < 60) return seconds + 's ago';
            const minutes = Math.floor(seconds / 60);
            if (minutes < 60) return minutes + 'm ago';
            const hours = Math.floor(minutes / 60);
            return hours + 'h ago';
        }}

        function updateNewsLabel(prefix) {{
            if (!newsUpdatedLabel) return;
            const age = formatRelativeAge(Date.now() - newsLastUpdatedMs);
            const head = prefix ? (prefix + ' · ') : '';
            newsUpdatedLabel.textContent = head + 'Updated ' + age + ' · Auto-refresh every 5 minutes';
        }}

        async function refreshNews(force) {{
            if (!newsRows) return;
            try {{
                // `force=1` bypasses server cache for manual refresh button clicks.
                const suffix = force ? '?force=1' : '';
                const resp = await fetch('/news/latest' + suffix);
                if (!resp.ok) return;
                const data = await resp.json();
                renderNewsRows(data.items || []);
                newsLastUpdatedMs = Date.now();
                updateNewsLabel(force ? 'Refreshed' : 'Auto-refreshed');
            }} catch (_) {{
                if (newsUpdatedLabel) newsUpdatedLabel.textContent = 'News refresh failed. Auto-retry in 5 minutes.';
            }}
        }}

        function readHiddenPanels() {{
            if (isSignedInUser) return new Set(accountHiddenPanels);
            try {{
                const parsed = JSON.parse(localStorage.getItem(panelStorageKey) || '[]');
                return new Set(Array.isArray(parsed) ? parsed : []);
            }} catch (_) {{
                return new Set();
            }}
        }}

        function writeHiddenPanels(hiddenSet) {{
            localStorage.setItem(panelStorageKey, JSON.stringify(Array.from(hiddenSet)));
        }}

        function updateCollapsedPanelsCount() {{
            if (!collapsedPanelsCount) return;
            const hidden = document.querySelectorAll('.dashboard-panel.panel-collapsed').length;
            collapsedPanelsCount.textContent = hidden + (hidden === 1 ? ' panel hidden' : ' panels hidden');
        }}

        async function syncHiddenPanelsToAccount(hiddenSet) {{
            if (!isSignedInUser) return;
            try {{
                const body = new URLSearchParams();
                body.set('csrf_token', '""" + h(csrf_token) + """');
                body.set('hidden_panels_json', JSON.stringify(Array.from(hiddenSet)));
                await fetch('/home/panels', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
                    body: body.toString(),
                }});
            }} catch (_) {{}}
        }}

        function initDashboardPanels() {{
            const manageableIds = new Set(allPanelIds);
            const hiddenPanels = readHiddenPanels();
            const cards = Array.from(document.querySelectorAll('.dashboard-panel'));

            for (const card of cards) {{
                const panelId = card.id;
                if (!panelId || !manageableIds.has(panelId)) continue;

                const directChildren = Array.from(card.children);
                if (!directChildren.length) continue;

                let headerNode = directChildren.find((node) => node.querySelector && node.querySelector('h2,h3')) || directChildren[0];
                if (headerNode && (headerNode.tagName === 'H2' || headerNode.tagName === 'H3')) {{
                    const anchor = document.createElement('div');
                    anchor.className = 'panel-header-anchor';
                    headerNode.parentNode.insertBefore(anchor, headerNode);
                    anchor.appendChild(headerNode);
                    headerNode = anchor;
                }} else if (headerNode && headerNode.classList) {{
                    headerNode.classList.add('panel-header-anchor');
                }}

                let body = card.querySelector(':scope > .panel-body');
                if (!body) {{
                    body = document.createElement('div');
                    body.className = 'panel-body';
                    const childrenNow = Array.from(card.children);
                    const headerIndex = childrenNow.indexOf(headerNode);
                    for (let idx = headerIndex + 1; idx < childrenNow.length; idx++) {{
                        body.appendChild(childrenNow[idx]);
                    }}
                    card.appendChild(body);
                }}

                if (!card.querySelector(':scope > .panel-header-anchor .panel-toggle-btn, :scope > .panel-toggle-btn')) {{
                    const toggleBtn = document.createElement('button');
                    toggleBtn.type = 'button';
                    toggleBtn.className = 'panel-toggle-btn';
                    const applyState = (collapsed) => {{
                        card.classList.toggle('panel-collapsed', collapsed);
                        toggleBtn.textContent = collapsed ? 'Show' : 'Hide';
                        toggleBtn.setAttribute('aria-expanded', String(!collapsed));
                    }};

                    applyState(hiddenPanels.has(panelId));
                    toggleBtn.addEventListener('click', function() {{
                        const nextCollapsed = !card.classList.contains('panel-collapsed');
                        if (nextCollapsed) {{
                            const visibleCount = document.querySelectorAll('.dashboard-panel:not(.panel-collapsed)').length;
                            if (visibleCount <= 3) {{
                                if (collapsedPanelsCount) collapsedPanelsCount.textContent = 'Keep at least 3 panels visible';
                                return;
                            }}
                        }}
                        applyState(nextCollapsed);
                        if (nextCollapsed) hiddenPanels.add(panelId); else hiddenPanels.delete(panelId);
                        writeHiddenPanels(hiddenPanels);
                        syncHiddenPanelsToAccount(hiddenPanels);
                        updateCollapsedPanelsCount();
                    }});

                    if (headerNode && headerNode.appendChild) headerNode.appendChild(toggleBtn);
                    else card.insertBefore(toggleBtn, card.firstChild);
                }}
            }}

            if (resetPanelsBtn) {{
                resetPanelsBtn.addEventListener('click', function() {{
                    localStorage.removeItem(panelStorageKey);
                    for (const card of document.querySelectorAll('.dashboard-panel.panel-collapsed')) {{
                        card.classList.remove('panel-collapsed');
                        const btn = card.querySelector('.panel-toggle-btn');
                        if (btn) {{
                            btn.textContent = 'Hide';
                            btn.setAttribute('aria-expanded', 'true');
                        }}
                    }}
                    const cleared = new Set();
                    writeHiddenPanels(cleared);
                    syncHiddenPanelsToAccount(cleared);
                    updateCollapsedPanelsCount();
                }});
            }}

            updateCollapsedPanelsCount();
        }}

        async function startImport(form) {{
            // Start job, then poll progress endpoint until completion/error.
            const formData = new FormData(form);
            const ytQ = formData.get('yt_q') || '';
            progressWrap.style.display = 'block';
            progressBar.value = 1;
            progressLabel.textContent = 'Starting download...';

            const startResp = await fetch('/videos/import-youtube/start', {{ method: 'POST', body: formData }});
            if (!startResp.ok) {{
                progressLabel.textContent = 'Could not start download.';
                return;
            }}
            const startData = await startResp.json();
            const jobId = startData.job_id;

            const intervalId = setInterval(async () => {{
                try {{
                    const pollResp = await fetch(`/videos/import-youtube/progress/${{encodeURIComponent(jobId)}}`);
                    if (!pollResp.ok) return;
                    const data = await pollResp.json();
                    progressBar.value = data.progress || 0;
                    if (data.message) progressLabel.textContent = data.message;

                    if (data.status === 'completed') {{
                        clearInterval(intervalId);
                        const imported = encodeURIComponent(data.filename || 'video');
                        const q = encodeURIComponent(ytQ);
                        window.location.href = `/?yt_q=${{q}}&yt_status=imported&imported_video=${{imported}}`;
                    }} else if (data.status === 'error') {{
                        clearInterval(intervalId);
                        const q = encodeURIComponent(ytQ);
                        window.location.href = `/?yt_q=${{q}}&yt_status=error`;
                    }}
                }} catch (err) {{
                    clearInterval(intervalId);
                    progressLabel.textContent = 'Download check failed.';
                }}
            }}, 900);
        }}

        for (const form of forms) {{
            form.classList.add('yt-import-form');
            form.addEventListener('submit', function(e) {{
                e.preventDefault();
                startImport(form);
            }});
        }}

        if (refreshNewsBtn) {{
            refreshNewsBtn.addEventListener('click', function() {{
                refreshNews(true);
            }});
        }}
        // Keep the relative “updated X ago” text fresh between fetches.
        updateNewsLabel('Loaded');
        initDashboardPanels();
        setInterval(function() {{
            updateNewsLabel('Loaded');
        }}, 10000);
        // Pull fresh headlines every 5 minutes.
        setInterval(function() {{
            refreshNews(false);
        }}, 300000);
    }})();
    </script></html>"""
    response = HTMLResponse(content=page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/texts/import")
def import_text_route(
    request: Request,
    source_url: str = Form(...),
    title: str = Form(...),
    text_q: str = Form(""),
    private_upload: Optional[str] = Form(None),
    csrf_token: str = Form(""),
):
    """Import a selected public text into notes and apply visibility metadata."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if private_upload and not user:
        return RedirectResponse(f"/?text_q={u(text_q)}&text_status=error", status_code=303)
    try:
        note_name = import_public_text_as_note(title, source_url)
    except HTTPException:
        return RedirectResponse(f"/?text_q={u(text_q)}&text_status=error", status_code=303)
    except Exception:
        return RedirectResponse(f"/?text_q={u(text_q)}&text_status=error", status_code=303)
    is_public = not bool(private_upload)
    upsert_file_record("note", note_name, user["id"] if user else None, is_public)
    if user:
        notify_followers_public_upload(user, "note", note_name)
    return RedirectResponse(
        f"/?text_q={u(text_q)}&text_status=imported&imported_name={u(note_name)}",
        status_code=303,
    )

@app.get("/notes/{filename}", response_class=HTMLResponse)
def view_note(request: Request, filename: str, edit: bool = False):
    """Render a note viewer/editor page, including lock-unlock flow."""
    name = ensure_safe_filename(filename)
    csrf_token = get_or_create_csrf_token(request)
    user = get_current_user(request)
    filepath = config["notes"] / name
    if not filepath.exists(): raise HTTPException(404)
    if not file_visible_to_user("note", name, user):
        raise HTTPException(status_code=403, detail="This note is private")
    meta, body = parse_note(filepath)
    lock_row = get_note_lock(name)
    unlocked_set = parse_unlocked_cookie(request)
    can_bypass = user_can_bypass_lock(user, lock_row)
    is_unlocked = name in unlocked_set
    if lock_row and not can_bypass and not is_unlocked:
        page = f"""
        <html><head>{COMMON_STYLE}</head><body>
        <a href='/'>← Back</a>
        <h1>{h(name)}</h1>
        <div class='card'>
            <h3>🔒 Locked note</h3>
            <p>Enter the note password to open this file.</p>
            <form action='/notes/{u(name)}/unlock' method='post' style='flex-wrap:wrap;'>
                <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
                <input type='password' name='note_password' placeholder='Note password' required>
                <button type='submit' class='btn btn-primary'>Unlock</button>
            </form>
        </div>
        </body></html>
        """
        response = HTMLResponse(page)
        response.set_cookie("csrf_token", csrf_token, samesite="lax")
        return response

    name_u = u(name)
    if edit:
        content = f"<form action='/notes/{name_u}/save' method='post'><input type='hidden' name='csrf_token' value='{h(csrf_token)}'><textarea name='content' style='width:100%;height:450px;'>{h(body)}</textarea><br><br><button type='submit' class='btn btn-primary'>Save</button></form>"
    else:
        rendered = markdown2.markdown(body, safe_mode="escape")
        content = f"<div class='card'>{rendered}</div><div style='display:flex;gap:10px;'><a href='?edit=true' class='btn btn-primary'>Edit</a><form action='/notes/{name_u}/delete' method='post' class='inline-form' onsubmit='return confirm(\"Delete?\")'><input type='hidden' name='csrf_token' value='{h(csrf_token)}'><button type='submit' class='btn btn-danger'>Delete</button></form></div>"
    page = f"<html><head>{COMMON_STYLE}</head><body><a href='/'>← Back</a><h1>{h(name)}</h1>{content}</body></html>"
    response = HTMLResponse(content=page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/notes/{filename}/unlock")
def unlock_note_route(request: Request, filename: str, note_password: str = Form(...), csrf_token: str = Form("")):
    """Validate note password and store unlocked state in cookie."""
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    lock_row = get_note_lock(name)
    if not lock_row:
        return RedirectResponse(f"/notes/{u(name)}", status_code=303)
    if not verify_password(note_password, lock_row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid note password")
    unlocked = parse_unlocked_cookie(request)
    unlocked.add(name)
    response = RedirectResponse(f"/notes/{u(name)}", status_code=303)
    response.set_cookie("unlocked_notes", "|".join(sorted(unlocked)), samesite="lax")
    return response

@app.get("/datasets/{filename}/full", response_class=HTMLResponse)
def view_full_dataset(request: Request, filename: str):
    """Render full dataset preview table for authorized viewers."""
    name = ensure_safe_filename(filename)
    user = get_current_user(request)
    if not file_visible_to_user("dataset", name, user):
        raise HTTPException(status_code=403, detail="This dataset is private")
    csrf_token = get_or_create_csrf_token(request)
    info = get_dataset_info(name, rows_limit=1000) # Load up to 1000 rows
    if not info: raise HTTPException(404)
    headers = "".join([f"<th>{h(k)}</th>" for k in info['cols']])
    rows = "".join([f"<tr>{''.join([f'<td>{h(v)}</td>' for v in r.values()])}</tr>" for r in info['preview']])
    name_u = u(name)
    page = f"""
    <html><head>{COMMON_STYLE}</head><body style='max-width:100%'>
    <a href='/'>← Back</a>
    <h1>📊 {h(name)}</h1>
    <div style='display:flex;gap:10px;margin-bottom:12px;'>
        <a href='/datasets/{name_u}/edit' class='btn btn-secondary'>Edit Dataset</a>
        <form action='/datasets/{name_u}/delete' method='post' style='margin:0;' onsubmit='return confirm("Delete dataset?")'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <button type='submit' class='btn btn-danger'>Delete Dataset</button>
        </form>
    </div>
    <div class='card' style='overflow:auto;'><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.get("/datasets/{filename}/edit", response_class=HTMLResponse)
def edit_dataset_page(request: Request, filename: str, status: Optional[str] = None):
    """Render raw dataset editor with validation/save status messages."""
    name = ensure_safe_filename(filename)
    user = get_current_user(request)
    if not file_visible_to_user("dataset", name, user):
        raise HTTPException(status_code=403, detail="This dataset is private")
    file_path = config["datasets"] / name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Dataset not found")
    csrf_token = get_or_create_csrf_token(request)
    content = file_path.read_text(encoding="utf-8")
    status_html = ""
    if status == "saved":
        status_html = "<p style='color:#155724;background:#d4edda;padding:8px;border-radius:4px;'>Dataset saved.</p>"
    elif status == "invalid":
        status_html = "<p style='color:#721c24;background:#f8d7da;padding:8px;border-radius:4px;'>Invalid CSV/JSON format. Fix and try again.</p>"
    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/datasets/{u(name)}/full'>← Back to Full View</a>
    <h1>Edit Dataset: {h(name)}</h1>
    <div class='card'>
        {status_html}
        <form action='/datasets/{u(name)}/save' method='post' style='flex-direction:column;align-items:stretch;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <textarea name='content' style='width:100%;height:520px;font-family:ui-monospace, SFMono-Regular, Menlo, monospace;'>{h(content)}</textarea>
            <div style='display:flex;gap:10px;margin-top:10px;'><button type='submit' class='btn btn-primary'>Save Dataset</button></div>
        </form>
        <form action='/datasets/{u(name)}/delete' method='post' style='margin-top:10px;' onsubmit='return confirm("Delete dataset?")'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <button type='submit' class='btn btn-danger'>Delete Dataset</button>
        </form>
    </div>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/datasets/{filename}/save")
def save_dataset_route(request: Request, filename: str, content: str = Form(...), csrf_token: str = Form("")):
    """Validate and save edited dataset content back to disk."""
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    user = get_current_user(request)
    if not file_visible_to_user("dataset", name, user):
        raise HTTPException(status_code=403, detail="This dataset is private")
    file_path = config["datasets"] / name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Dataset not found")
    try:
        validate_dataset_content(name, content)
    except Exception:
        return RedirectResponse(f"/datasets/{u(name)}/edit?status=invalid", status_code=303)
    file_path.write_text(content, encoding="utf-8")
    return RedirectResponse(f"/datasets/{u(name)}/edit?status=saved", status_code=303)

@app.post("/datasets/{filename}/delete")
def delete_dataset_route(request: Request, filename: str, csrf_token: str = Form("")):
    """Delete a dataset and associated metadata record."""
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    user = get_current_user(request)
    if not file_visible_to_user("dataset", name, user):
        raise HTTPException(status_code=403, detail="This dataset is private")
    (config["datasets"] / name).unlink(missing_ok=True)
    conn = get_db_connection()
    conn.execute("DELETE FROM file_records WHERE file_type = ? AND filename = ?", ("dataset", name))
    conn.commit()
    conn.close()
    return RedirectResponse("/?data_status=deleted", status_code=303)

@app.post("/notes/{filename}/save")
def save_note_route(request: Request, filename: str, content: str = Form(...), csrf_token: str = Form("")):
    """Save note edits while enforcing lock access rules."""
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    user = get_current_user(request)
    lock_row = get_note_lock(name)
    if lock_row and not user_can_bypass_lock(user, lock_row) and name not in parse_unlocked_cookie(request):
        raise HTTPException(status_code=403, detail="Note is locked")
    filepath = config["notes"] / name
    meta, _ = parse_note(filepath)
    save_note(filepath, meta, content)
    return RedirectResponse(f"/notes/{u(name)}", status_code=303)

@app.post("/notes/{filename}/delete")
def delete_note_route(request: Request, filename: str, csrf_token: str = Form("")):
    """Delete a note file and remove any lock metadata."""
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    user = get_current_user(request)
    lock_row = get_note_lock(name)
    if lock_row and not user_can_bypass_lock(user, lock_row) and name not in parse_unlocked_cookie(request):
        raise HTTPException(status_code=403, detail="Note is locked")
    (config["notes"] / name).unlink(missing_ok=True)
    remove_note_lock(name)
    return RedirectResponse("/", status_code=303)

@app.post("/notes/create")
def create_note_route(
    request: Request,
    filename: str = Form(...),
    lock_note: Optional[str] = Form(None),
    private_note: Optional[str] = Form(None),
    lock_password: str = Form(""),
    csrf_token: str = Form(""),
):
    """Create a new note from dashboard form with optional lock/privacy."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    title = filename.strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "note"
    name = base + ".md"
    save_note(config["notes"] / name, {"title": title}, f"# {title}")
    is_public = not bool(private_note)
    if lock_note:
        if not user:
            raise HTTPException(status_code=403, detail="Login required to create locked notes")
        if len(lock_password) < 4:
            raise HTTPException(status_code=400, detail="Lock password must be at least 4 characters")
        set_note_lock(name, lock_password, user["id"])
        is_public = False
    if private_note and not user:
        raise HTTPException(status_code=403, detail="Login required to create private notes")
    upsert_file_record("note", name, user["id"] if user else None, is_public)
    if user:
        notify_followers_public_upload(user, "note", name)
    return RedirectResponse("/", status_code=303)

@app.post("/datasets/import")
async def import_dataset_route(
    request: Request,
    file: UploadFile = File(...),
    private_upload: Optional[str] = Form(None),
    csrf_token: str = Form(""),
):
    """Upload dataset file to storage and record ownership/visibility metadata."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if private_upload and not user:
        raise HTTPException(status_code=403, detail="Login required to upload private files")
    filename = ensure_safe_filename(file.filename)
    if Path(filename).suffix.lower() not in {'.csv', '.json'}:
        raise HTTPException(status_code=400, detail="Unsupported dataset format")
    with (config["datasets"] / filename).open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    is_public = not bool(private_upload)
    upsert_file_record("dataset", filename, user["id"] if user else None, is_public)
    if user:
        notify_followers_public_upload(user, "dataset", filename)
    return RedirectResponse("/?data_status=uploaded", status_code=303)

@app.post("/videos/import")
async def import_video_route(
    request: Request,
    file: UploadFile = File(...),
    private_upload: Optional[str] = Form(None),
    csrf_token: str = Form(""),
):
    """Upload local video file, generate thumbnail, and save visibility metadata."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if private_upload and not user:
        raise HTTPException(status_code=403, detail="Login required to upload private files")
    filename = ensure_safe_filename(file.filename)
    if Path(filename).suffix.lower() not in {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}:
        raise HTTPException(status_code=400, detail="Unsupported video format")
    with (config["videos"] / filename).open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    generate_video_thumbnail(filename)
    is_public = not bool(private_upload)
    upsert_file_record("video", filename, user["id"] if user else None, is_public)
    if user:
        notify_followers_public_upload(user, "video", filename)
    return RedirectResponse("/", status_code=303)

@app.post("/videos/import-youtube")
def import_youtube_video_route(
    request: Request,
    video_url: str = Form(...),
    yt_q: str = Form(""),
    private_upload: Optional[str] = Form(None),
    csrf_token: str = Form(""),
):
    """Import YouTube video synchronously from form submission."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if private_upload and not user:
        return RedirectResponse(f"/?yt_q={u(yt_q)}&yt_status=error", status_code=303)
    try:
        imported_file = import_youtube_video(video_url)
    except Exception:
        return RedirectResponse(f"/?yt_q={u(yt_q)}&yt_status=error", status_code=303)
    finalize_imported_video_for_user(user, imported_file, bool(private_upload))
    return RedirectResponse(
        f"/?yt_q={u(yt_q)}&yt_status=imported&imported_video={u(imported_file)}",
        status_code=303,
    )

@app.post("/videos/import-youtube/start")
def start_youtube_import_route(
    request: Request,
    video_url: str = Form(...),
    private_upload: Optional[str] = Form(None),
    csrf_token: str = Form(""),
):
    """Start asynchronous YouTube import job and return job identifier."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if private_upload and not user:
        raise HTTPException(status_code=403, detail="Login required to upload private files")

    job_id = str(uuid.uuid4())
    user_snapshot = None
    if user:
        user_snapshot = {"id": user["id"], "username": user["username"], "role": user["role"], "public_name": user.get("public_name") if hasattr(user, "get") else user["public_name"]}
    with YOUTUBE_IMPORT_JOBS_LOCK:
        YOUTUBE_IMPORT_JOBS[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "message": "Queued",
            "filename": None,
            "created_at": datetime.datetime.utcnow().isoformat(),
        }

    worker = threading.Thread(
        target=run_youtube_import_job,
        args=(job_id, video_url, user_snapshot, bool(private_upload)),
        daemon=True,
    )
    worker.start()
    return JSONResponse({"job_id": job_id, "status": "queued"})

@app.get("/videos/import-youtube/progress/{job_id}")
def youtube_import_progress_route(job_id: str):
    """Return current status/progress for a background YouTube import job."""
    with YOUTUBE_IMPORT_JOBS_LOCK:
        job = YOUTUBE_IMPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return JSONResponse({
        "status": job.get("status"),
        "progress": job.get("progress", 0.0),
        "message": job.get("message", ""),
        "filename": job.get("filename"),
    })

@app.get("/videos/{filename}", response_class=HTMLResponse)
def view_video(request: Request, filename: str):
    """Render video player page with delete and volume controls."""
    name = ensure_safe_filename(filename)
    csrf_token = get_or_create_csrf_token(request)
    user = get_current_user(request)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    if not file_visible_to_user("video", name, user):
        raise HTTPException(status_code=403, detail="This video is private")
    name_u = u(name)
    page = f"""<html><head>{COMMON_STYLE}</head><body><a href='/'>← Back</a><h1>🎬 {h(name)}</h1><div class='card'><video id='player' controls style='width:100%;max-height:70vh;' src='/videos/{name_u}/stream'></video><div class='player-controls'><label for='volume'>Volume</label><input id='volume' class='volume-range' type='range' min='0' max='1' step='0.05' value='1'><button id='muteBtn' class='btn btn-primary' type='button'>Mute</button><form class='inline-form' action='/videos/{name_u}/delete' method='post' onsubmit='return confirm("Delete video?")'><input type='hidden' name='csrf_token' value='{h(csrf_token)}'><button type='submit' class='btn btn-danger'>Delete Video</button></form></div></div><script>const player=document.getElementById('player');const volume=document.getElementById('volume');const muteBtn=document.getElementById('muteBtn');volume.addEventListener('input',()=>{{player.volume=parseFloat(volume.value);if(player.volume>0)player.muted=false;muteBtn.textContent=player.muted?'Unmute':'Mute';}});muteBtn.addEventListener('click',()=>{{player.muted=!player.muted;muteBtn.textContent=player.muted?'Unmute':'Mute';if(!player.muted&&player.volume===0){{player.volume=0.5;volume.value='0.5';}}}});</script></body></html>"""
    response = HTMLResponse(content=page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/videos/{filename}/delete")
def delete_video_route(request: Request, filename: str, csrf_token: str = Form("")):
    """Delete a video file and its generated thumbnail."""
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    video_file.unlink(missing_ok=True)
    thumbnail_path(name).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)

@app.get("/videos/{filename}/stream")
def stream_video(request: Request, filename: str):
    """Stream a stored video file to authorized clients."""
    name = ensure_safe_filename(filename)
    request_user = get_current_user(request)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    if not file_visible_to_user("video", name, request_user):
        raise HTTPException(status_code=403, detail="This video is private")
    return FileResponse(video_file)

@app.get("/videos/{filename}/thumbnail")
def video_thumbnail(request: Request, filename: str):
    """Serve generated thumbnail or SVG fallback for a video."""
    name = ensure_safe_filename(filename)
    user = get_current_user(request)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    if not file_visible_to_user("video", name, user):
        raise HTTPException(status_code=403, detail="This video is private")
    thumb = generate_video_thumbnail(name)
    if thumb and thumb.exists():
        return FileResponse(thumb)
    fallback_svg = """<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360'><rect width='100%' height='100%' fill='#f0f0f0'/><circle cx='320' cy='180' r='48' fill='#d9d9d9'/><polygon points='305,155 305,205 350,180' fill='#9e9e9e'/><text x='50%' y='300' text-anchor='middle' fill='#7a7a7a' font-family='Arial' font-size='22'>No thumbnail available</text></svg>"""
    return Response(content=fallback_svg, media_type="image/svg+xml")

@app.get("/games", response_class=HTMLResponse)
def games_hub_page(request: Request):
    """Render mini games launcher page."""
    page = """
    <html><head>""" + COMMON_STYLE + """</head><body>
    <a href='/'>← Back</a>
    <h1>🎮 Mini Games</h1>
    <div class='card'>
        <div class='nav-pills'>
            <a href='/games/tetris' class='btn btn-primary'>Tetris Style</a>
            <a href='/games/frogger' class='btn btn-success'>Frogger Style</a>
            <a href='/games/word-guess' class='btn btn-secondary'>Word Guess</a>
            <a href='/games/hangman' class='btn btn-secondary'>Hangman</a>
            <a href='/games/leaderboard' class='btn btn-secondary'>Leaderboard</a>
        </div>
        <p class='helper'>Use arrow keys for arcade games. Word games are keyboard/button based.</p>
    </div>
    </body></html>
    """
    return HTMLResponse(page)

@app.get("/games/leaderboard", response_class=HTMLResponse)
def games_leaderboard_page(request: Request):
    """Render leaderboard page visible to all users."""
    snapshot = get_leaderboard_snapshot(limit_each=10)

    def render_rows(game_name: str) -> str:
        rows = snapshot.get(game_name, [])
        if not rows:
            return "<p class='helper'>No scores yet.</p>"
        html_rows = ""
        for idx, row in enumerate(rows, start=1):
            player = (row["public_name"] or "").strip() or row["username"]
            html_rows += f"<tr><td>{idx}</td><td>{h(player)}</td><td>{h(row['score'])}</td><td>{h(row['created_at'])}</td></tr>"
        return f"<table><thead><tr><th>#</th><th>Player</th><th>Score</th><th>When</th></tr></thead><tbody>{html_rows}</tbody></table>"

    page = f"""
    <html><head>{COMMON_STYLE}</head><body>
    <a href='/games'>← Games Hub</a>
    <h1>🏆 Leaderboard</h1>
    <div class='card'><h3>Tetris Style</h3>{render_rows('tetris')}</div>
    <div class='card'><h3>Frogger Style</h3>{render_rows('frogger')}</div>
    <div class='card'><h3>Word Guess</h3>{render_rows('word_guess')}</div>
    <div class='card'><h3>Hangman</h3>{render_rows('hangman')}</div>
    </body></html>
    """
    return HTMLResponse(page)

@app.get("/games/leaderboard-data/{game_name}")
def game_leaderboard_data(game_name: str, limit: int = 10):
    """Return JSON leaderboard rows for a specific game."""
    rows = get_game_leaderboard(game_name, limit=limit)
    return {
        "game": game_name,
        "rows": [
            {
                "rank": idx,
                "player": ((row["public_name"] or "").strip() or row["username"]),
                "score": row["score"],
                "created_at": row["created_at"],
            }
            for idx, row in enumerate(rows, start=1)
        ],
    }

@app.post("/games/score")
def submit_game_score_route(
    request: Request,
    game_name: str = Form(...),
    score: int = Form(...),
    csrf_token: str = Form(""),
):
    """Submit authenticated user's game score and return latest leaderboard slice."""
    validate_csrf(request, csrf_token)
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required to submit scores")
    submit_game_score(user["id"], game_name, score)
    rows = get_game_leaderboard(game_name, limit=10)
    return {
        "ok": True,
        "game": game_name,
        "rows": [
            {
                "rank": idx,
                "player": ((row["public_name"] or "").strip() or row["username"]),
                "score": row["score"],
                "created_at": row["created_at"],
            }
            for idx, row in enumerate(rows, start=1)
        ],
    }

@app.get("/games/tetris", response_class=HTMLResponse)
def tetris_style_game_page(request: Request):
    """Render a simple Tetris-style falling blocks game."""
    csrf_token = get_or_create_csrf_token(request)
    signed_in = "true" if get_current_user(request) else "false"
    page = """
    <html><head>""" + COMMON_STYLE + """</head><body>
    <a href='/games'>← Games Hub</a>
    <h1>🧩 Tetris Style</h1>
    <div class='card'>
        <p class='helper'>Controls: ← → move, ↑ rotate, ↓ drop faster, Space hard drop.</p>
        <canvas id='tetris' width='300' height='600' style='border:1px solid #ccc;background:#111;max-width:100%;'></canvas>
        <p id='tetrisStatus' class='helper'>Score: 0</p>
        <div id='tetrisBoard'></div>
    </div>
    <script>
    const csrfToken = '""" + h(csrf_token) + """';
    const signedIn = """ + signed_in + """;
    const canvas = document.getElementById('tetris');
    const ctx = canvas.getContext('2d');
    const statusEl = document.getElementById('tetrisStatus');
    const boardEl = document.getElementById('tetrisBoard');
    const cols = 10, rows = 20, size = 30;
    const grid = Array.from({length: rows}, () => Array(cols).fill(0));
    const colors = ['#000', '#39f', '#f63', '#fd3', '#3d6', '#d6f'];
    const shapes = [
      [[1,1,1,1]], [[2,0],[2,0],[2,2]], [[0,3],[0,3],[3,3]], [[4,4],[4,4]], [[0,5,5],[5,5,0]]
    ];
    let score = 0, piece = null, over = false, submitted = false;

    async function refreshLeaderboard(){
      const r = await fetch('/games/leaderboard-data/tetris');
      const d = await r.json();
      const rows = d.rows || [];
      boardEl.innerHTML = rows.length ? ('<h4>Leaderboard</h4><table><thead><tr><th>#</th><th>Player</th><th>Score</th></tr></thead><tbody>' + rows.map(x => '<tr><td>'+x.rank+'</td><td>'+x.player+'</td><td>'+x.score+'</td></tr>').join('') + '</tbody></table>') : '<p class="helper">No leaderboard scores yet.</p>';
    }
    async function submitScore(){
      if (!signedIn || submitted) return;
      submitted = true;
      const form = new URLSearchParams();
      form.set('csrf_token', csrfToken); form.set('game_name', 'tetris'); form.set('score', String(score));
      await fetch('/games/score', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: form.toString()});
      refreshLeaderboard();
    }
    function cloneShape(shape){ return shape.map(r => r.slice()); }
    function spawn(){ const shape = cloneShape(shapes[Math.floor(Math.random()*shapes.length)]); piece = {shape, x: Math.floor((cols-shape[0].length)/2), y: 0}; if (collides(piece.x,piece.y,piece.shape)) over = true; }
    function rotate(shape){ return shape[0].map((_, i) => shape.map(row => row[i]).reverse()); }
    function collides(nx, ny, shape){ for (let y=0; y<shape.length; y++) for (let x=0; x<shape[y].length; x++) { if (!shape[y][x]) continue; const gx = nx + x, gy = ny + y; if (gx < 0 || gx >= cols || gy >= rows) return true; if (gy >= 0 && grid[gy][gx]) return true; } return false; }
    function lock(){ for (let y=0; y<piece.shape.length; y++) for (let x=0; x<piece.shape[y].length; x++) { const v = piece.shape[y][x]; if (v && piece.y+y >= 0) grid[piece.y+y][piece.x+x] = v; } clearLines(); spawn(); }
    function clearLines(){ let lines = 0; for (let y=rows-1; y>=0; y--) { if (grid[y].every(Boolean)) { grid.splice(y,1); grid.unshift(Array(cols).fill(0)); lines++; y++; } } if (lines) score += lines * 100; }
    function drawCell(x,y,v){ ctx.fillStyle = colors[v]; ctx.fillRect(x*size, y*size, size-1, size-1); }
    function draw(){
      ctx.clearRect(0,0,canvas.width,canvas.height);
      for (let y=0; y<rows; y++) for (let x=0; x<cols; x++) if (grid[y][x]) drawCell(x,y,grid[y][x]);
      if (piece) for (let y=0; y<piece.shape.length; y++) for (let x=0; x<piece.shape[y].length; x++) { const v = piece.shape[y][x]; if (v) drawCell(piece.x+x, piece.y+y, v); }
      statusEl.textContent = over ? 'Game over. Score: ' + score + (signedIn ? '' : ' (sign in to submit)') : 'Score: ' + score;
    }
    function tick(){ if (over) { draw(); submitScore(); return; } if (!collides(piece.x,piece.y+1,piece.shape)) piece.y++; else lock(); draw(); }
    document.addEventListener('keydown', (e) => {
      if (!piece || over) return;
      if (e.key === 'ArrowLeft' && !collides(piece.x-1,piece.y,piece.shape)) piece.x--;
      if (e.key === 'ArrowRight' && !collides(piece.x+1,piece.y,piece.shape)) piece.x++;
      if (e.key === 'ArrowDown' && !collides(piece.x,piece.y+1,piece.shape)) piece.y++;
      if (e.key === 'ArrowUp') { const r = rotate(piece.shape); if (!collides(piece.x,piece.y,r)) piece.shape = r; }
      if (e.code === 'Space') { while (!collides(piece.x,piece.y+1,piece.shape)) piece.y++; lock(); }
      draw();
    });
    spawn(); draw(); refreshLeaderboard(); setInterval(tick, 450);
    </script>
    </body></html>
    """
    response = HTMLResponse(page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.get("/games/frogger", response_class=HTMLResponse)
def frogger_style_game_page(request: Request):
        """Render a simple Frogger-style crossing game."""
        csrf_token = get_or_create_csrf_token(request)
        signed_in = "true" if get_current_user(request) else "false"
        page = """
        <html><head>""" + COMMON_STYLE + """</head><body>
        <a href='/games'>← Games Hub</a>
        <h1>🐸 Frogger Style</h1>
        <div class='card'>
                <p class='helper'>Controls: Arrow keys to move. 60-second round, avoid cars and reach the top.</p>
                <canvas id='frogger' width='520' height='520' style='border:1px solid #ccc;background:#0d1a0d;max-width:100%;'></canvas>
                <p id='froggerStatus' class='helper'>Wins: 0 | Time: 60</p>
                <div id='froggerBoard'></div>
        </div>
        <script>
        const csrfToken = '""" + h(csrf_token) + """';
        const signedIn = """ + signed_in + """;
        const canvas = document.getElementById('frogger');
        const ctx = canvas.getContext('2d');
        const statusEl = document.getElementById('froggerStatus');
        const boardEl = document.getElementById('froggerBoard');
        const laneH = 52;
        let wins = 0, timeLeft = 60, gameOver = false, submitted = false;
        const frog = {x: 260, y: 494, size: 18};
        const cars = [];
        const lanes = [1,2,3,4,5,6,7,8].map(i => ({y: i*laneH, speed: (i%2===0 ? 1 : -1) * (1.5 + (i%3)), count: 3}));
        lanes.forEach((lane, idx) => { for (let i=0; i<lane.count; i++) cars.push({x: 70 + i*180 + idx*11, y: lane.y + 10, w: 72, h: 30, speed: lane.speed}); });
        async function refreshLeaderboard(){
            const r = await fetch('/games/leaderboard-data/frogger');
            const d = await r.json();
            const rows = d.rows || [];
            boardEl.innerHTML = rows.length ? ('<h4>Leaderboard</h4><table><thead><tr><th>#</th><th>Player</th><th>Score</th></tr></thead><tbody>' + rows.map(x => '<tr><td>'+x.rank+'</td><td>'+x.player+'</td><td>'+x.score+'</td></tr>').join('') + '</tbody></table>') : '<p class="helper">No leaderboard scores yet.</p>';
        }
        async function submitScore(){
            if (!signedIn || submitted) return;
            submitted = true;
            const finalScore = wins * 100;
            const form = new URLSearchParams();
            form.set('csrf_token', csrfToken); form.set('game_name', 'frogger'); form.set('score', String(finalScore));
            await fetch('/games/score', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: form.toString()});
            refreshLeaderboard();
        }
        function resetFrog(){ frog.x = 260; frog.y = 494; }
        function hit(a,b){ return a.x < b.x+b.w && a.x+a.size > b.x && a.y < b.y+b.h && a.y+a.size > b.y; }
        function update(){
            if (gameOver) return;
            for (const c of cars) {
                c.x += c.speed;
                if (c.speed > 0 && c.x > 560) c.x = -90;
                if (c.speed < 0 && c.x < -100) c.x = 560;
                if (hit(frog, c)) resetFrog();
            }
            if (frog.y <= 8) { wins++; resetFrog(); }
            statusEl.textContent = 'Wins: ' + wins + ' | Time: ' + timeLeft + (gameOver ? (signedIn ? ' | Round over' : ' | Round over (sign in to submit)') : '');
        }
        function draw(){
            ctx.clearRect(0,0,520,520);
            for (let y=0; y<10; y++) { ctx.fillStyle = (y===0 || y===9) ? '#153a15' : '#2f2f2f'; ctx.fillRect(0,y*laneH,520,laneH-2); }
            for (const c of cars) { ctx.fillStyle = '#e25555'; ctx.fillRect(c.x,c.y,c.w,c.h); }
            ctx.fillStyle = '#6aff6a'; ctx.fillRect(frog.x,frog.y,frog.size,frog.size);
        }
        document.addEventListener('keydown', (e) => {
            if (gameOver) return;
            const step = laneH;
            if (e.key === 'ArrowLeft') frog.x = Math.max(0, frog.x-step);
            if (e.key === 'ArrowRight') frog.x = Math.min(520-frog.size, frog.x+step);
            if (e.key === 'ArrowUp') frog.y = Math.max(0, frog.y-step);
            if (e.key === 'ArrowDown') frog.y = Math.min(520-frog.size, frog.y+step);
        });
        setInterval(() => { if (gameOver) return; timeLeft--; if (timeLeft <= 0) { timeLeft = 0; gameOver = true; submitScore(); } }, 1000);
        function loop(){ update(); draw(); requestAnimationFrame(loop); }
        refreshLeaderboard();
        loop();
        </script>
        </body></html>
        """
        response = HTMLResponse(page)
        response.set_cookie("csrf_token", csrf_token, samesite="lax")
        return response

@app.get("/games/word-guess", response_class=HTMLResponse)
def word_guess_game_page(request: Request):
        """Render a browser word guess game."""
        csrf_token = get_or_create_csrf_token(request)
        signed_in = "true" if get_current_user(request) else "false"
        words = ["library", "profile", "dataset", "notebook", "python", "message", "future", "upload"]
        secret = random.choice(words)
        page = """
        <html><head>""" + COMMON_STYLE + """</head><body>
        <a href='/games'>← Games Hub</a>
        <h1>🔤 Word Guess</h1>
        <div class='card'>
                <p class='helper'>Guess letters to reveal the word. 8 wrong guesses allowed.</p>
                <p id='wgWord' style='font-size:1.6em;letter-spacing:6px;'></p>
                <p id='wgMeta' class='helper'></p>
                <form id='wgForm' style='flex-wrap:wrap;'>
                        <input id='wgInput' maxlength='1' placeholder='letter' required style='width:90px;'>
                        <button class='btn btn-primary' type='submit'>Guess</button>
                        <button class='btn btn-secondary' type='button' id='wgReset'>Reset</button>
                </form>
                <div id='wgBoard'></div>
        </div>
        <script>
        const csrfToken = '""" + h(csrf_token) + """';
        const signedIn = """ + signed_in + """;
        const words = ['library','profile','dataset','notebook','python','message','future','upload'];
        let secret = '""" + secret + """';
        let guessed = new Set(), wrong = 0, submitted = false;
        const maxWrong = 8;
        const wordEl = document.getElementById('wgWord');
        const metaEl = document.getElementById('wgMeta');
        const input = document.getElementById('wgInput');
        const boardEl = document.getElementById('wgBoard');
        function masked(){ return secret.split('').map(ch => guessed.has(ch) ? ch : '_').join(' '); }
        function won(){ return secret.split('').every(ch => guessed.has(ch)); }
        async function refreshLeaderboard(){
            const r = await fetch('/games/leaderboard-data/word_guess');
            const d = await r.json();
            const rows = d.rows || [];
            boardEl.innerHTML = rows.length ? ('<h4>Leaderboard</h4><table><thead><tr><th>#</th><th>Player</th><th>Score</th></tr></thead><tbody>' + rows.map(x => '<tr><td>'+x.rank+'</td><td>'+x.player+'</td><td>'+x.score+'</td></tr>').join('') + '</tbody></table>') : '<p class="helper">No leaderboard scores yet.</p>';
        }
        async function submitScore(finalScore){
            if (!signedIn || submitted) return;
            submitted = true;
            const form = new URLSearchParams();
            form.set('csrf_token', csrfToken); form.set('game_name', 'word_guess'); form.set('score', String(finalScore));
            await fetch('/games/score', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: form.toString()});
            refreshLeaderboard();
        }
        function render(){
            wordEl.textContent = masked();
            metaEl.textContent = 'Wrong: ' + wrong + '/' + maxWrong + ' | Guessed: ' + (Array.from(guessed).sort().join(', ') || 'none');
            if (won()) {
                const finalScore = Math.max(0, (maxWrong - wrong) * 15 + secret.length * 20);
                metaEl.textContent += ' | You win! Score: ' + finalScore + (signedIn ? '' : ' (sign in to submit)');
                submitScore(finalScore);
            }
            if (wrong >= maxWrong) metaEl.textContent += ' | You lose. Word: ' + secret;
        }
        document.getElementById('wgForm').addEventListener('submit', (e) => {
            e.preventDefault();
            if (won() || wrong >= maxWrong) return;
            const ch = (input.value || '').toLowerCase().trim();
            input.value = '';
            if (!/^[a-z]$/.test(ch) || guessed.has(ch)) return;
            guessed.add(ch);
            if (!secret.includes(ch)) wrong++;
            render();
        });
        document.getElementById('wgReset').addEventListener('click', () => {
            secret = words[Math.floor(Math.random()*words.length)];
            guessed = new Set(); wrong = 0; submitted = false; render();
        });
        render(); refreshLeaderboard();
        </script>
        </body></html>
        """
        response = HTMLResponse(page)
        response.set_cookie("csrf_token", csrf_token, samesite="lax")
        return response

@app.get("/games/hangman", response_class=HTMLResponse)
def hangman_game_page(request: Request):
        """Render a browser hangman game."""
        csrf_token = get_or_create_csrf_token(request)
        signed_in = "true" if get_current_user(request) else "false"
        words = ["keyboard", "archive", "session", "network", "terminal", "science", "fastapi", "thumbnails"]
        secret = random.choice(words)
        page = """
        <html><head>""" + COMMON_STYLE + """</head><body>
        <a href='/games'>← Games Hub</a>
        <h1>🪢 Hangman</h1>
        <div class='card'>
                <p id='hmDrawing' style='font-family:ui-monospace, SFMono-Regular, Menlo, monospace;white-space:pre;line-height:1.1;'></p>
                <p id='hmWord' style='font-size:1.5em;letter-spacing:6px;'></p>
                <p id='hmMeta' class='helper'></p>
                <form id='hmForm' style='flex-wrap:wrap;'>
                        <input id='hmInput' maxlength='1' placeholder='letter' required style='width:90px;'>
                        <button class='btn btn-primary' type='submit'>Guess</button>
                        <button class='btn btn-secondary' type='button' id='hmReset'>Reset</button>
                </form>
                <div id='hmBoard'></div>
        </div>
        <script>
        const csrfToken = '""" + h(csrf_token) + """';
        const signedIn = """ + signed_in + """;
        const words = ['keyboard','archive','session','network','terminal','science','fastapi','thumbnails'];
        let secret = '""" + secret + """';
        let guessed = new Set(), wrong = 0, submitted = false;
        const maxWrong = 6;
        const stages = [
`\n +---+\n |   |\n     |\n     |\n     |\n     |\n=======`,
`\n +---+\n |   |\n O   |\n     |\n     |\n     |\n=======`,
`\n +---+\n |   |\n O   |\n |   |\n     |\n     |\n=======`,
`\n +---+\n |   |\n O   |\n/|   |\n     |\n     |\n=======`,
`\n +---+\n |   |\n O   |\n/|\\  |\n     |\n     |\n=======`,
`\n +---+\n |   |\n O   |\n/|\\  |\n/    |\n     |\n=======`,
`\n +---+\n |   |\n O   |\n/|\\  |\n/ \\  |\n     |\n=======`
        ];
        const drawEl = document.getElementById('hmDrawing');
        const wordEl = document.getElementById('hmWord');
        const metaEl = document.getElementById('hmMeta');
        const input = document.getElementById('hmInput');
        const boardEl = document.getElementById('hmBoard');
        function masked(){ return secret.split('').map(ch => guessed.has(ch) ? ch : '_').join(' '); }
        function won(){ return secret.split('').every(ch => guessed.has(ch)); }
        async function refreshLeaderboard(){
            const r = await fetch('/games/leaderboard-data/hangman');
            const d = await r.json();
            const rows = d.rows || [];
            boardEl.innerHTML = rows.length ? ('<h4>Leaderboard</h4><table><thead><tr><th>#</th><th>Player</th><th>Score</th></tr></thead><tbody>' + rows.map(x => '<tr><td>'+x.rank+'</td><td>'+x.player+'</td><td>'+x.score+'</td></tr>').join('') + '</tbody></table>') : '<p class="helper">No leaderboard scores yet.</p>';
        }
        async function submitScore(finalScore){
            if (!signedIn || submitted) return;
            submitted = true;
            const form = new URLSearchParams();
            form.set('csrf_token', csrfToken); form.set('game_name', 'hangman'); form.set('score', String(finalScore));
            await fetch('/games/score', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body: form.toString()});
            refreshLeaderboard();
        }
        function render(){
            drawEl.textContent = stages[wrong];
            wordEl.textContent = masked();
            metaEl.textContent = 'Wrong: ' + wrong + '/' + maxWrong + ' | Guessed: ' + (Array.from(guessed).sort().join(', ') || 'none');
            if (won()) {
                const finalScore = Math.max(0, (maxWrong - wrong) * 25 + secret.length * 15);
                metaEl.textContent += ' | You win! Score: ' + finalScore + (signedIn ? '' : ' (sign in to submit)');
                submitScore(finalScore);
            }
            if (wrong >= maxWrong) metaEl.textContent += ' | You lose. Word: ' + secret;
        }
        document.getElementById('hmForm').addEventListener('submit', (e) => {
            e.preventDefault();
            if (won() || wrong >= maxWrong) return;
            const ch = (input.value || '').toLowerCase().trim();
            input.value = '';
            if (!/^[a-z]$/.test(ch) || guessed.has(ch)) return;
            guessed.add(ch);
            if (!secret.includes(ch)) wrong++;
            render();
        });
        document.getElementById('hmReset').addEventListener('click', () => {
            secret = words[Math.floor(Math.random()*words.length)];
            guessed = new Set(); wrong = 0; submitted = false; render();
        });
        render(); refreshLeaderboard();
        </script>
        </body></html>
        """
        response = HTMLResponse(page)
        response.set_cookie("csrf_token", csrf_token, samesite="lax")
        return response

if __name__ == "__main__":
    subprocess.run(["uvicorn", f"{Path(__file__).stem}:app", "--reload", "--port", "8080"])
