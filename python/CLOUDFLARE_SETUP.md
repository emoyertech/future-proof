# CloudFlare Tunnel Setup Guide

## What's Configured

✅ **cloudflared CLI** - Already installed (version 2026.3.0)
✅ **Environment variable support** - App checks `FP_CLOUDFLARE_URL` 
✅ **CloudFlare switch link** - Available at bottom of app

## Two Setup Options

### Option 1: Quick Tunnel (Temporary - No Auth Needed) ⚡

Works immediately, but the URL changes each time. Best for quick testing.

```bash
cd /Users/ethan/Projects/future-proof/python
bash run_with_tunnel.sh
```

**What happens:**
- FastAPI server starts on port 8000
- CloudFlare tunnel exposes it publicly
- You get a temporary `*.trycloudflare.io` URL
- Copy that URL and set it as `FP_CLOUDFLARE_URL` in your environment

**To use the CloudFlare switch in the app:**
```bash
export FP_CLOUDFLARE_URL="https://your-tunnel-url.trycloudflare.io"
python3 notes0.py
```

---

### Option 2: Persistent Tunnel (Requires Auth) 🔐

Permanent custom domain with authentication. Better for deployment.

**Requirements:**
1. Complete CloudFlare authentication (browser login)
2. This creates `~/.cloudflared/cert.pem`

**Browser login page:** (already opened in your browser)
- https://dash.cloudflare.com/argotunnel

**Once authenticated, run:**
```bash
cd /Users/ethan/Projects/future-proof/python
bash setup_tunnel.sh
```

This will:
- Create a tunnel named `future-proof-notes`
- Generate a config file at `~/.cloudflared/config.yml`
- Show you the tunnel ID

**Then start the tunnel:**
```bash
cloudflared tunnel run future-proof-notes
```

**To connect a custom domain:**
```bash
cloudflared tunnel route dns future-proof-notes your-domain.com
```

---

## Next Steps

**Which option would you like?**

**For quick testing now:** Use Option 1
```bash
bash run_with_tunnel.sh
```

**For permanent setup:** 
1. Complete auth in the browser (https://dash.cloudflare.com/argotunnel)
2. Then use Option 2 above

---

## Troubleshooting

**Error: "Cannot determine default origin certificate path"**
- You need to complete authentication first at https://dash.cloudflare.com/argotunnel

**Can't find the tunnel URL?**
- It will be displayed in the terminal output
- Looks like: `https://xxxxx.trycloudflare.io`

**App not accessible through tunnel?**
- Make sure FastAPI is running on port 8000
- Check: `curl http://127.0.0.1:8000` returns 200 OK

**Set environment variable for switch:**
```bash
export FP_CLOUDFLARE_URL="https://your-tunnel-url.trycloudflare.io"
```

---

## Current Status

- ✅ CloudFlare CLI installed
- ✅ App has CloudFlare switch built-in
- ⏳ Awaiting your choice: Quick Tunnel or Persistent Tunnel?
