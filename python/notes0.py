#!/usr/bin/env python3
import os, re, sys, markdown2, subprocess, datetime, shutil
import html, secrets
from pathlib import Path
from typing import Optional, List
from urllib.parse import quote
from fastapi import FastAPI, HTTPException, Form, File, UploadFile, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
import pandas as pd

# --- 1. INITIALIZATION ---
def setup():
    base = Path.home() / ".notes"
    notes, data = base / "notes", base / "datasets"
    videos, thumbs = base / "videos", base / "thumbnails"
    notes.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    videos.mkdir(parents=True, exist_ok=True)
    thumbs.mkdir(parents=True, exist_ok=True)
    return {"root": base, "notes": notes, "datasets": data, "videos": videos, "thumbnails": thumbs}

config = setup()
app = FastAPI(title="Note & Data Hub")

# --- 2. CORE LOGIC ---
def parse_note(f: Path):
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
    if isinstance(meta.get('tags'), list): 
        meta['tags'] = f"[{', '.join(meta['tags'])}]"
    head = "\n".join([f"{k}: {v}" for k, v in meta.items()])
    f.write_text(f"---\n{head}\n---\n\n{body}", encoding='utf-8')

def get_dataset_info(name: str, rows_limit: int = 3):
    f = config["datasets"] / name
    if not f.exists(): return None
    try:
        df = pd.read_csv(f) if f.suffix == '.csv' else pd.read_json(f)
        return {
            "id": name, "rows": len(df), "cols": list(df.columns),
            "preview": df.head(rows_limit).to_dict(orient="records")
        }
    except: return {"id": name, "rows": 0, "preview": []}

def h(value) -> str:
    return html.escape(str(value), quote=True)

def u(value: str) -> str:
    return quote(value, safe="")

def safe_name(name: str) -> str:
    return Path(name or "").name

def ensure_safe_filename(name: str) -> str:
    cleaned = safe_name(name)
    if not cleaned or cleaned != name or cleaned in {".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return cleaned

def get_or_create_csrf_token(request: Request) -> str:
    existing = request.cookies.get("csrf_token")
    if existing and len(existing) >= 16:
        return existing
    return secrets.token_urlsafe(24)

def validate_csrf(request: Request, csrf_token: str):
    cookie = request.cookies.get("csrf_token")
    if not csrf_token or not cookie or not secrets.compare_digest(csrf_token, cookie):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

def thumbnail_path(video_name: str) -> Path:
    return config["thumbnails"] / f"{Path(video_name).stem}.jpg"

def generate_video_thumbnail(video_name: str) -> Optional[Path]:
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
    body { font-family: -apple-system, sans-serif; max-width: 900px; margin: auto; padding: 20px; background: #f8f9fa; color: #333; }
    .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; }
    .btn { padding: 8px 16px; border-radius: 4px; text-decoration: none; font-weight: bold; cursor: pointer; border: none; display: inline-block; font-size: 0.9em; }
    .btn-primary { background: #007bff; color: white; }
    .btn-success { background: #28a745; color: white; }
    .btn-danger { background: #dc3545; color: white; }
    .note-item { display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #eee; }
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
        .video-actions { flex-direction: column; align-items: stretch; gap: 10px; }
        .video-actions .btn, .video-actions button.btn { width: 100%; min-width: 0; }
        .player-controls .inline-form { margin-left: 0; }
        .player-controls .btn, .player-controls .inline-form button { width: 100%; }
    }
    table { width: 100%; border-collapse: collapse; font-size: 0.75em; }
    th, td { border: 1px solid #eee; padding: 6px; text-align: left; white-space: nowrap; }
    th { background: #f9f9f9; color: #666; }
    input[type="text"], input[type="file"], textarea { padding: 10px; border: 1px solid #ddd; border-radius: 4px; }
    form { display: flex; gap: 10px; align-items: center; }
</style>
"""

# --- 4. ROUTES ---
@app.get("/", response_class=HTMLResponse)
def web_home(request: Request, q: Optional[str] = None):
    csrf_token = get_or_create_csrf_token(request)
    # Action Forms
    actions_html = f"""
    <div class='card'>
        <form action='/notes/create' method='post' style='margin-bottom:15px;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='text' name='filename' placeholder='New note title...' style='flex-grow:1' required>
            <button type='submit' class='btn btn-primary'>+ Create Note</button>
        </form>
        <form action='/datasets/import' method='post' enctype='multipart/form-data'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='file' name='file' accept='.csv,.json' style='flex-grow:1' required>
            <button type='submit' class='btn btn-success'>📥 Upload Data</button>
        </form>
        <form action='/videos/import' method='post' enctype='multipart/form-data' style='margin-top:10px;'>
            <input type='hidden' name='csrf_token' value='{h(csrf_token)}'>
            <input type='file' name='file' accept='video/*,.mp4,.mov,.m4v,.webm,.avi,.mkv' style='flex-grow:1' required>
            <button type='submit' class='btn btn-success'>🎬 Upload Video</button>
        </form>
    </div>
    <form action='/' method='get' style='margin-bottom:20px;'>
        <input type='text' name='q' placeholder='Search notes, tags, or content...' style='flex-grow:1' value='{h(q or "")}'>
        <button type='submit' class='btn btn-primary'>Search</button>
    </form>"""

    # Library Content
    notes_raw = sorted([f.name for f in config["notes"].glob("*") if f.suffix in ['.md', '.txt']])
    datasets_raw = sorted([f.name for f in config["datasets"].glob("*") if f.suffix in ['.csv', '.json']])
    videos_raw = sorted([f.name for f in config["videos"].glob("*") if f.suffix.lower() in ['.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv']])

    if q:
        q = q.lower()
        notes_raw = [n for n in notes_raw if q in n.lower() or q in (config["notes"]/n).read_text().lower()]
        datasets_raw = [d for d in datasets_raw if q in d.lower()]
        videos_raw = [v for v in videos_raw if q in v.lower()]

    notes_html = "".join([f"<div class='note-item'><span>{h(n)}</span><a href='/notes/{u(n)}' class='btn btn-primary'>View</a></div>" for n in notes_raw])
    
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
                <a href='/datasets/{d_name_u}/full' class='btn btn-primary' style='font-size:0.7em;'>Full View ↗</a>
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

    page = f"<html><head>{COMMON_STYLE}</head><body><h1>🚀 Library</h1>{actions_html}<div class='card'><h2>📝 Notes</h2>{notes_html or '<p>No notes found.</p>'}</div><h2>📊 Data</h2>{datasets_html or '<p>No data found.</p>'}<div class='card'><h2>🎬 Videos</h2><div class='video-grid'>{videos_html or '<p>No videos found.</p>'}</div></div></body></html>"
    response = HTMLResponse(content=page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.get("/notes/{filename}", response_class=HTMLResponse)
def view_note(request: Request, filename: str, edit: bool = False):
    name = ensure_safe_filename(filename)
    csrf_token = get_or_create_csrf_token(request)
    filepath = config["notes"] / name
    if not filepath.exists(): raise HTTPException(404)
    meta, body = parse_note(filepath)
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

@app.get("/datasets/{filename}/full", response_class=HTMLResponse)
def view_full_dataset(filename: str):
    name = ensure_safe_filename(filename)
    info = get_dataset_info(name, rows_limit=1000) # Load up to 1000 rows
    if not info: raise HTTPException(404)
    headers = "".join([f"<th>{h(k)}</th>" for k in info['cols']])
    rows = "".join([f"<tr>{''.join([f'<td>{h(v)}</td>' for v in r.values()])}</tr>" for r in info['preview']])
    return f"<html><head>{COMMON_STYLE}</head><body style='max-width:100%'><a href='/'>← Back</a><h1>📊 {h(name)}</h1><div class='card' style='overflow:auto;'><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div></body></html>"

@app.post("/notes/{filename}/save")
def save_note_route(request: Request, filename: str, content: str = Form(...), csrf_token: str = Form("")):
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    filepath = config["notes"] / name
    meta, _ = parse_note(filepath)
    save_note(filepath, meta, content)
    return RedirectResponse(f"/notes/{u(name)}", status_code=303)

@app.post("/notes/{filename}/delete")
def delete_note_route(request: Request, filename: str, csrf_token: str = Form("")):
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    (config["notes"] / name).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)

@app.post("/notes/create")
def create_note_route(request: Request, filename: str = Form(...), csrf_token: str = Form("")):
    validate_csrf(request, csrf_token)
    title = filename.strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("_") or "note"
    name = base + ".md"
    save_note(config["notes"] / name, {"title": title}, f"# {title}")
    return RedirectResponse("/", status_code=303)

@app.post("/datasets/import")
async def import_dataset_route(request: Request, file: UploadFile = File(...), csrf_token: str = Form("")):
    validate_csrf(request, csrf_token)
    filename = ensure_safe_filename(file.filename)
    if Path(filename).suffix.lower() not in {'.csv', '.json'}:
        raise HTTPException(status_code=400, detail="Unsupported dataset format")
    with (config["datasets"] / filename).open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return RedirectResponse("/", status_code=303)

@app.post("/videos/import")
async def import_video_route(request: Request, file: UploadFile = File(...), csrf_token: str = Form("")):
    validate_csrf(request, csrf_token)
    filename = ensure_safe_filename(file.filename)
    if Path(filename).suffix.lower() not in {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}:
        raise HTTPException(status_code=400, detail="Unsupported video format")
    with (config["videos"] / filename).open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    generate_video_thumbnail(filename)
    return RedirectResponse("/", status_code=303)

@app.get("/videos/{filename}", response_class=HTMLResponse)
def view_video(request: Request, filename: str):
    name = ensure_safe_filename(filename)
    csrf_token = get_or_create_csrf_token(request)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    name_u = u(name)
    page = f"""<html><head>{COMMON_STYLE}</head><body><a href='/'>← Back</a><h1>🎬 {h(name)}</h1><div class='card'><video id='player' controls style='width:100%;max-height:70vh;' src='/videos/{name_u}/stream'></video><div class='player-controls'><label for='volume'>Volume</label><input id='volume' class='volume-range' type='range' min='0' max='1' step='0.05' value='1'><button id='muteBtn' class='btn btn-primary' type='button'>Mute</button><form class='inline-form' action='/videos/{name_u}/delete' method='post' onsubmit='return confirm("Delete video?")'><input type='hidden' name='csrf_token' value='{h(csrf_token)}'><button type='submit' class='btn btn-danger'>Delete Video</button></form></div></div><script>const player=document.getElementById('player');const volume=document.getElementById('volume');const muteBtn=document.getElementById('muteBtn');volume.addEventListener('input',()=>{{player.volume=parseFloat(volume.value);if(player.volume>0)player.muted=false;muteBtn.textContent=player.muted?'Unmute':'Mute';}});muteBtn.addEventListener('click',()=>{{player.muted=!player.muted;muteBtn.textContent=player.muted?'Unmute':'Mute';if(!player.muted&&player.volume===0){{player.volume=0.5;volume.value='0.5';}}}});</script></body></html>"""
    response = HTMLResponse(content=page)
    response.set_cookie("csrf_token", csrf_token, samesite="lax")
    return response

@app.post("/videos/{filename}/delete")
def delete_video_route(request: Request, filename: str, csrf_token: str = Form("")):
    validate_csrf(request, csrf_token)
    name = ensure_safe_filename(filename)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    video_file.unlink(missing_ok=True)
    thumbnail_path(name).unlink(missing_ok=True)
    return RedirectResponse("/", status_code=303)

@app.get("/videos/{filename}/stream")
def stream_video(filename: str):
    name = ensure_safe_filename(filename)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    return FileResponse(video_file)

@app.get("/videos/{filename}/thumbnail")
def video_thumbnail(filename: str):
    name = ensure_safe_filename(filename)
    video_file = config["videos"] / name
    if not video_file.exists():
        raise HTTPException(404)
    thumb = generate_video_thumbnail(name)
    if thumb and thumb.exists():
        return FileResponse(thumb)
    fallback_svg = """<svg xmlns='http://www.w3.org/2000/svg' width='640' height='360'><rect width='100%' height='100%' fill='#f0f0f0'/><circle cx='320' cy='180' r='48' fill='#d9d9d9'/><polygon points='305,155 305,205 350,180' fill='#9e9e9e'/><text x='50%' y='300' text-anchor='middle' fill='#7a7a7a' font-family='Arial' font-size='22'>No thumbnail available</text></svg>"""
    return Response(content=fallback_svg, media_type="image/svg+xml")

if __name__ == "__main__":
    subprocess.run(["uvicorn", f"{Path(__file__).stem}:app", "--reload", "--port", "8080"])
