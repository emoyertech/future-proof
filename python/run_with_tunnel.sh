#!/bin/bash
# Quick start the notes app with CloudFlare tunnel
# Usage: ./run_with_tunnel.sh

cd /Users/ethan/Projects/future-proof/python

echo "🚀 Starting future-proof app with CloudFlare tunnel..."
echo ""
echo "⚠️  Note: Without authentication, this tunnel is temporary (quick tunnel mode)"
echo "For permanent tunnels, complete auth: https://dash.cloudflare.com/argotunnel"
echo ""

# Start the FastAPI app in background
echo "📱 Starting FastAPI server on port 8000..."
python3 notes0.py &
APP_PID=$!

sleep 2

# Start CloudFlare tunnel to expose it publicly
echo "🌐 Starting CloudFlare tunnel..."
echo ""
echo "Your public URL will appear below:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Run tunnel in foreground - you'll see the trycloudflare.io URL
cloudflared tunnel --url http://127.0.0.1:8000 --no-autoupdate

# Cleanup
kill $APP_PID 2>/dev/null || true
