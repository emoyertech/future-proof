#!/usr/bin/env python3
import os, re, sys, markdown2, subprocess, datetime, shutil
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Form, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import pandas as pd

# --- 1. SETUP ---
def setup():
    base = Path.home() / ".notes"
    notes, data = base / "notes", base / "datasets"
    notes.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    return {"root": base, "notes": notes, "datasets": data}

config = setup()
app = FastAPI(title="Note & Data Hub")

# --- 2. CORE LOGIC ---
def parse_note(f: Path):
    if not f.exists(): return {"title": f.stem, "tags": []}, ""
    try:
        content = f.read_text(encoding='utf-8')
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
        if not match: return {"title": f.stem, "tags": []}, content
        meta = {}
        for line in match.group(1).strip().splitlines():
            if ':' in line:
                k, v = [item.strip() for item in line.split(':', 1)]
                meta[k.lower()] = [t.strip() for t in v.strip('[]').split(',')] if k.lower() == 'tags' else v
        return meta, match.group(2).strip()
    except Exception:
        return {"title": f.stem, "tags": []}, ""

def save_note(f: Path, meta: dict, body: str):
    if isinstance(meta.get('tags'), list): 
        meta['tags'] = f"[{', '.join(meta['tags'])}]"
    head = "\n".join([f"{k}: {v}" for k, v in meta.items()])
    f.write_text(f"---\n{head}\n---\n\n{body}", encoding='utf-8')

def get_dataset_info(name: str):
    f = config["datasets"] / name
    if not f.exists(): return None
    ext = f.suffix.lower()[1:]
    try:
        # Robust loading to prevent internal errors on empty files
        df = pd.read_csv(f) if ext == 'csv' else pd.read_json(f)
        if df.empty: return {"id": name, "rows": 0, "preview": []}
        return {
            "id": name, "format": ext, "rows": len(df),
            "preview": df.head(3).to_dict(orient="records")
        }
    except Exception:
        return {"id": name, "rows": 0, "preview": []}

# --- 3. HELPERS FOR WEB ---
def get_all_notes():
    return [{"id": f.name, "meta": parse_note(f)[0]} for f in config["notes"].glob("*.md")]

def get_all_datasets():
    return [get_dataset_info(f.name) for f in config["datasets"].glob("*") if f.suffix in ['.csv', '.json']]

# --- 4. WEB INTERFACE ---
COMMON_STYLE = """
<style>
    body { font-family: -apple-system, sans-serif; max-width: 850px; margin: auto; background: #f8f9fa; padding: 30px; color: #212529; }
    .card { background: #fff; padding: 20px; margin-bottom: 25px; border-radius: 10px; border: 1px solid #e9ecef; box-shadow: 0 4px 6px rgba(0,0,0,0.05); }
    .note-item { display: flex; justify-content: space-between; align-items: center; padding: 12px 0; border-bottom: 1px solid #f1f3f5; }
    .note-item:last-child { border-bottom: none; }
    .btn { text-decoration: none; padding: 8px 16px; border-radius: 6px; font-weight: 500; font-size: 0.9em; border: none; cursor: pointer; display: inline-block; }
    .btn-primary { background: #007bff; color: white; }
    .btn-success { background: #28a745; color: white; }
    .preview-box { overflow-x: auto; max-height: 160px; border: 1px solid #dee2e6; border-radius: 6px; margin-top: 12px; background: #fff; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8em; }
    th, td { border: 1px solid #dee2e6; padding: 8px; text-align: left; white-space: nowrap; }
    th { background: #f1f3f5; color: #495057; font-weight: 600; }
    input[type="text"], input[type="file"] { padding: 10px; border: 1px solid #ced4da; border-radius: 6px; flex-grow: 1; }
    form { display: flex; gap: 12px; align-items: center; }
    h2 { font-size: 1.4em; margin-bottom: 15px; color: #343a40; }
</style>
"""

@app.get("/", response_class=HTMLResponse)
def web_home():
    actions_html = f"""
    <div class='card'>
        <h2 style='margin-top:0'>New Note</h2>
        <form action='/notes/create' method='post'>
            <input type='text' name='filename' placeholder='Note title...' required>
            <button type='submit' class='btn btn-primary'>Create</button>
        </form>
        <hr style='margin: 25px 0; border: 0; border-top: 1px solid #e9ecef;'>
        <h2>Upload Data</h2>
        <form action='/datasets/import' method='post' enctype='multipart/form-data'>
            <input type='file' name='file' accept='.csv,.json' required>
            <button type='submit' class='btn btn-success'>Upload</button>
        </form>
    </div>"""

    notes_html = "".join([f"<div class='note-item'><span>{n['meta'].get('title', n['id'])}</span> <a href='/notes/{n['id']}' class='btn btn-primary'>View</a></div>" for n in get_all_notes()])
    
    datasets_html = ""
    for d in get_all_datasets():
        if d is None: continue
        headers = "".join([f"<th>{k}</th>" for k in d['preview'][0].keys()]) if d['preview'] else ""
        rows = "".join([f"<tr>{''.join([f'<td>{v}</td>' for v in r.values()])}</tr>" for r in d['preview']])
        datasets_html += f"""
        <div class='card'>
            <strong>📊 {d['id']}</strong> <small style='color:#6c757d'>({d['rows']} rows)</small>
            <div class='preview-box'><table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></div>
        </div>"""

    return f"""<html><head>{COMMON_STYLE}</head><body>
        <h1>🚀 Note & Data Hub</h1>
        {actions_html}
        <div class='card'><h2>📝 My Notes</h2>{notes_html or '<p style="color:#6c757d">No notes found.</p>'}</div>
        <h2 style='padding-left:10px'>📊 My Datasets</h2>{datasets_html or '<div class="card"><p style="color:#6c757d">No datasets found.</p></div>'}
    </body></html>"""

@app.post("/notes/create")
def web_create_note(filename: str = Form(...)):
    clean_name = re.sub(r'[^a-zA-Z0-9]', '_', filename).lower() + ".md"
    path = config["notes"] / clean_name
    save_note(path, {"title": filename, "tags": []}, f"# {filename}")
    return RedirectResponse("/", status_code=303)

@app.post("/datasets/import")
async def web_import_dataset(file: UploadFile = File(...)):
    if not (file.filename.endswith('.csv') or file.filename.endswith('.json')):
        raise HTTPException(400, "Only CSV/JSON allowed.")
    path = config["datasets"] / file.filename
    with path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    return RedirectResponse("/", status_code=303)

if __name__ == "__main__":
    file_name = Path(__file__).stem
    print(f"Starting server on http://127.0.0.1:8080")
    subprocess.run(["uvicorn", f"{file_name}:app", "--reload", "--port", "8080"])
