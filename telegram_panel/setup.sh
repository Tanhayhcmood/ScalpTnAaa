#!/usr/bin/env bash
# GoldScalperPro Telegram Control Panel — Setup Script
# Run from the project root: bash telegram_panel/setup.sh

set -e

echo ""
echo "═══════════════════════════════════════════════"
echo "  GoldScalperPro Telegram Control Panel Setup"
echo "═══════════════════════════════════════════════"
echo ""

# ── Check Python version ──────────────────────────────────────────────────
python3 -c "import sys; exit(0) if sys.version_info >= (3,11) else exit(1)" || {
    echo "❌ Python 3.11+ is required."
    echo "   Current: $(python3 --version)"
    exit 1
}
echo "✅ Python version: $(python3 --version)"

# ── Create data directories ───────────────────────────────────────────────
mkdir -p telegram_panel/storage/data/logs
echo "✅ Data directories created"

# ── Install dependencies ──────────────────────────────────────────────────
echo ""
echo "📦 Installing dependencies..."
pip install -r telegram_panel/requirements.txt --quiet
echo "✅ Dependencies installed"

# ── Copy config if not exists ─────────────────────────────────────────────
if [ ! -f telegram_panel/config/panel.json ]; then
    cp telegram_panel/config/panel.json.example telegram_panel/config/panel.json
    echo "✅ Config created: telegram_panel/config/panel.json"
    echo "   ⚠️  Edit it with your bot token and owner ID before running."
else
    echo "ℹ️  Config already exists: telegram_panel/config/panel.json"
fi

# ── Generate encryption key if not set ───────────────────────────────────
if [ -z "$PANEL_ENCRYPTION_KEY" ]; then
    echo ""
    echo "🔑 Generating encryption key..."
    python3 -m telegram_panel.main --generate-key
    echo ""
    echo "   Add PANEL_ENCRYPTION_KEY to your environment or panel.json"
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  Setup Complete!"
echo "═══════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo "  1. Edit telegram_panel/config/panel.json"
echo "     - Set your bot token (from @BotFather)"
echo "     - Set your Telegram user ID (from @userinfobot)"
echo ""
echo "  2. Start the panel:"
echo "     python -m telegram_panel.main"
echo ""
echo "  3. Open Telegram and send /start to your bot"
echo ""
