# Future Proof Notes Hub (Python)

This folder contains `notes0.py`, a full FastAPI web app + API for managing notes, datasets, videos, and social collaboration.

## What the Script Does

`notes0.py` runs a local content hub with:

- **Notes**: create, view, edit, delete markdown notes
- **Locks/Privacy**: password-lock notes, mark uploads private/public
- **Datasets**: upload/view/edit/delete CSV and JSON files
- **Videos**: upload/stream/delete videos with thumbnail generation
- **Public Imports**:
  - search/import public-domain text files (Gutendex/Gutenberg)
  - search/import YouTube videos (`yt-dlp`), including progress polling jobs
- **Accounts/Auth**: register/login/logout, session cookies, account password changes
- **Social**: profile pages, public names, follow/unfollow, direct messages, notifications
- **Recommendations**: feed of public uploads from users you follow
- **Admin Controls**: admin dashboard and user creation (including admin accounts)
- **Global UI Controls**: light/dark mode, layout presets, custom layout sliders saved in browser localStorage

## Storage Model

The app is filesystem-first and stores content under `~/.notes/`:

```text
~/.notes/
  notes/
  datasets/
  videos/
  thumbnails/
  auth.db
```

- `auth.db` stores users, sessions, follows, messages, file visibility metadata, notifications, and note locks.

## Requirements

## Terminal Program Requirements (bash/zsh)

Programs users should have available in terminal:

### Required

- `python3` (run the app)
- `pip3` or `python3 -m pip` (install Python packages)

### Optional (feature-dependent)

- `yt-dlp` (YouTube search/import)
- `ffmpeg` (video thumbnail generation)

Check everything quickly:

```bash
command -v python3
command -v pip3
command -v yt-dlp
command -v ffmpeg
```

Install optional tools on macOS:

```bash
brew install yt-dlp ffmpeg
```

Install Python packages used by the app:

Install Python dependencies first:

```bash
pip install fastapi uvicorn markdown2 pandas python-multipart
```

Optional but recommended tools:

- `yt-dlp` (YouTube search/import)
- `ffmpeg` (video thumbnails)

macOS example:

```bash
brew install yt-dlp ffmpeg
```

## Run the App

From this directory:

```bash
python3 notes0.py
```

Then open:

- http://127.0.0.1:8080

The script starts Uvicorn with reload on port `8080`.

## What a User Can Do (Web UI)

On the home page (`/`), users can:

- Create notes (public, private, or locked)
- Upload datasets (CSV/JSON)
- Upload videos (MP4/MOV/M4V/WEBM/AVI/MKV)
- Search/import public text files
- Search/import YouTube videos (with progress bar)
- See latest headlines from major news sources (CNN, NYT, WSJ, The Economist, Google News, Apple News, BBC, NBC)
- Refresh news manually or let it auto-refresh every 5 minutes
- See a relative update indicator (for example: "Updated 2m ago")
- Search existing library content
- Open social sections (chat, follow lists, profile links)
- Play mini games (Tetris style, Frogger style, Word Guess, Hangman)
- View shared game leaderboards across users
- Personalize home sections (hide/show panels) and keep preferences when signed in
- Use display controls (theme/layout/custom presets)

Additional pages:

- `/auth/register`, `/auth/login`, `/auth/logout`
- `/account` (change password)
- `/profile` and `/u/{username}`
- `/messages`, `/notifications`
- `/admin`, `/admin/users` (admin-only)
- `/notes/{filename}`
- `/datasets/{filename}/full`, `/datasets/{filename}/edit`
- `/videos/{filename}`

## API Overview

Auth APIs:

- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/me`

Notes APIs:

- `GET /api/notes`
- `GET /api/notes/{filename}`
- `POST /api/notes`

Messaging APIs:

- `GET /api/messages`
- `POST /api/messages`

Admin API:

- `POST /api/admin/users` (admin bearer token required)

For API auth, send `Authorization: Bearer <token>` from login/register response.

## Troubleshooting & Common Errors

### 1) App will not start

- Error like `ModuleNotFoundError`: install requirements again:

```bash
python3 -m pip install --upgrade pip
python3 -m pip install fastapi uvicorn markdown2 pandas python-multipart
```

- Error opening browser page: confirm app is running and open `http://127.0.0.1:8080`.

### 2) Port already in use (`8080`)

Run with a different port:

```bash
uvicorn notes0:app --reload --port 8081
```

Then open `http://127.0.0.1:8081`.

### 3) YouTube search/import not working

- Check tool install:

```bash
which yt-dlp
```

- If not found, install:

```bash
brew install yt-dlp
```

- Restart the app after installation.

### 4) Video thumbnails are missing

- Check `ffmpeg`:

```bash
which ffmpeg
```

- If missing:

```bash
brew install ffmpeg
```

- Existing videos regenerate thumbnails when viewed or reprocessed.

### 5) "Invalid CSRF token" on form submit

- Refresh the page and submit again.
- Make sure cookies are enabled in your browser.
- If you restarted the app, reload all open app tabs before submitting forms.

### 6) Cannot access private/locked content

- Private files are only visible to owner/admin.
- Locked notes require the note password unless you're owner/admin.
- If locked note access fails, unlock it again from the note page.

### 7) "Recipient not found" or follow errors

- Usernames are lowercase in storage; check spelling.
- You cannot follow yourself or message yourself.

### 8) SQLite or file-permission issues

- Ensure your user can write to `~/.notes/`.
- Verify directories and DB exist after first run.
- If needed, back up `~/.notes/` and remove only corrupted files.

### 9) Dataset save/upload errors

- CSV/JSON must be valid parseable content.
- Keep filename extensions correct (`.csv` or `.json`).
- For JSON edits, ensure proper brackets/quotes.

### 10) Reset local display preferences

Theme/layout/preset settings are stored in browser localStorage.
If UI settings seem broken, clear localStorage for the site and reload.

## Support Checklist (Before Reporting a Bug)

Run these commands from terminal and include the output in your bug report:

```bash
echo "== System =="
uname -a

echo "== Python/Pip =="
python3 --version
python3 -m pip --version

echo "== Installed tools =="
command -v python3 || echo "missing: python3"
command -v pip3 || echo "missing: pip3"
command -v yt-dlp || echo "missing: yt-dlp (optional for YouTube)"
command -v ffmpeg || echo "missing: ffmpeg (optional for thumbnails)"

echo "== Python packages =="
python3 -m pip show fastapi uvicorn markdown2 pandas python-multipart

echo "== App data dir =="
ls -la ~/.notes
ls -la ~/.notes/notes ~/.notes/datasets ~/.notes/videos ~/.notes/thumbnails
```

If `8080` fails, test a different port:

```bash
uvicorn notes0:app --reload --port 8081
```

### Notify Admins In-App

After collecting diagnostics, notify admins directly in the app:

1. Sign in and click **Report Issue** (top bar or chat card), or open `/messages?compose=issue`.
2. In the recipient dropdown, choose a user labeled `(admin)`.
3. Paste your bug details and checklist output.

Suggested message format:

```text
Bug report:
- Summary:
- Expected behavior:
- Actual behavior:
- Steps to reproduce:
- Time observed:
- Browser/OS:

Diagnostics:
[paste Support Checklist output]
```

## Bug Report Template

Copy/paste this template when reporting issues:

```text
Title: <short issue title>

Summary:
<what is wrong in one or two sentences>

Expected behavior:
<what you expected to happen>

Actual behavior:
<what actually happened>

Steps to reproduce:
1) ...
2) ...
3) ...

Environment:
- App URL:
- Date/time:
- OS:
- Browser:

Diagnostics:
<paste Support Checklist output>
```

## Notes for Maintainers

- The app is intentionally monolithic (`notes0.py`) for rapid iteration and teaching/demo use.
- CSRF checks are enforced on web form POST routes.
- Visibility rules are centralized through file metadata + lock logic.
- Social notifications are sent only for **public** uploads.
