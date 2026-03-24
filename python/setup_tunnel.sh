#!/bin/bash
# CloudFlare Tunnel Setup Script

set -e

TUNNEL_NAME="future-proof-notes"
APP_PORT="8000"
APP_HOST="127.0.0.1"

echo "🚀 Setting up CloudFlare Tunnel for future-proof-notes..."

# Create tunnel
echo "📍 Creating tunnel '$TUNNEL_NAME'..."
cloudflared tunnel create "$TUNNEL_NAME" || echo "Tunnel may already exist"

# Get tunnel ID
TUNNEL_ID=$(cloudflared tunnel list --output json 2>/dev/null | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4 || echo "")

if [ -z "$TUNNEL_ID" ]; then
  TUNNEL_ID=$(cloudflared tunnel list 2>&1 | grep "$TUNNEL_NAME" | awk '{print $1}' || echo "")
fi

if [ -z "$TUNNEL_ID" ]; then
  echo "⚠️  Could not find tunnel ID automatically"
  echo "Please find tunnel ID from: cloudflared tunnel list"
  read -p "Enter Tunnel ID: " TUNNEL_ID
fi

echo "✅ Tunnel ID: $TUNNEL_ID"

# Create config directory
mkdir -p ~/.cloudflared

# Create config file
CONFIG_FILE=~/.cloudflared/config.yml
cat > "$CONFIG_FILE" << EOF
tunnel: $TUNNEL_ID
credentials-file: ~/.cloudflared/$TUNNEL_ID.json

ingress:
  - hostname: "*.trycloudflare.io"
    service: http://$APP_HOST:$APP_PORT
  - service: http://$APP_HOST:$APP_PORT
EOF

echo "✅ Config file created: $CONFIG_FILE"

# Route tunnel to domain
echo ""
echo "🌐 To access your app:"
echo "  1. Run: cloudflared tunnel route dns $TUNNEL_NAME"
echo "  2. Or use the trycloudflare.io temporary URL from tunnel output"
echo ""
echo "📝 To start the tunnel:"
echo "  cloudflared tunnel run $TUNNEL_NAME"
echo ""
echo "💡 To set FP_CLOUDFLARE_URL environment variable,"
echo "   run the tunnel first and copy the trycloudflare.io URL"
echo ""
