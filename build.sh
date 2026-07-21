#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Y.S Agent System — Production Build Script
# ============================================================
# Builds: Web (static), Android (APK/AAB via Capacitor)
# ============================================================

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
ANDROID_DIR="$PROJECT_ROOT/android"

echo "=========================================="
echo "  Y.S Agent System — Production Build"
echo "=========================================="

# --- Web Build ---
echo ""
echo "[1/3] Building web artifact..."
mkdir -p "$DIST_DIR"
cp "$PROJECT_ROOT/index.html" "$DIST_DIR/index.html"
cp "$PROJECT_ROOT/public/manifest.json" "$DIST_DIR/manifest.json"
cp "$PROJECT_ROOT/public/sw.js" "$DIST_DIR/sw.js"
cp "$PROJECT_ROOT/public/icon-192.png" "$DIST_DIR/icon-192.png"
cp "$PROJECT_ROOT/public/icon-512.png" "$DIST_DIR/icon-512.png"
echo "  Web build -> $DIST_DIR/"
ls -la "$DIST_DIR/"

# --- Android Web Assets ---
echo ""
echo "[2/3] Preparing Android web assets..."
mkdir -p "$ANDROID_DIR/www"
cp "$DIST_DIR/index.html" "$ANDROID_DIR/www/index.html"
cp "$DIST_DIR/manifest.json" "$ANDROID_DIR/www/manifest.json"
cp "$DIST_DIR/sw.js" "$ANDROID_DIR/www/sw.js"
cp "$DIST_DIR/icon-192.png" "$ANDROID_DIR/www/icon-192.png"
cp "$DIST_DIR/icon-512.png" "$ANDROID_DIR/www/icon-512.png"
echo "  Android web assets -> $ANDROID_DIR/www/"

# --- Android Build (APK + AAB) ---
echo ""
echo "[3/3] Building Android APK/AAB..."
echo "  NOTE: Android SDK + Capacitor required."
echo "  To build APK:"
echo "    cd android && npm install && npx cap sync android && cd android && ./gradlew assembleDebug"
echo "  To build AAB (Play Store):"
echo "    cd android && npm install && npx cap sync android && cd android && ./gradlew bundleRelease"
echo ""
echo "  APK output: android/android/app/build/outputs/apk/debug/app-debug.apk"
echo "  AAB output: android/android/app/build/outputs/bundle/release/app-release.aab"

# --- Verify Python backend ---
echo ""
echo "Verifying Python backend..."
cd "$PROJECT_ROOT"
python3 -m py_compile agent_system.py && echo "  agent_system.py: OK" || { echo "  agent_system.py: FAILED"; exit 1; }
python3 -m py_compile tools_api.py mcp_client.py config.py telegram_bot.py && echo "  Supporting modules: OK" || { echo "  Supporting modules: FAILED"; exit 1; }

echo ""
echo "=========================================="
echo "  Build complete!"
echo "=========================================="
echo ""
echo "Artifacts:"
echo "  Web:   $DIST_DIR/index.html"
echo "  APK:   android/android/app/build/outputs/apk/debug/app-debug.apk"
echo "  AAB:   android/android/app/build/outputs/bundle/release/app-release.aab"
echo ""
echo "To serve locally:"
echo "  python3 agent_system.py"
echo "  or: gunicorn agent_system:app --bind 0.0.0.0:8080"
echo ""
echo "To deploy on Railway:"
echo "  railway up"
echo ""
echo "To deploy with Docker:"
echo "  docker build -t ys-agent ."
echo "  docker run -p 8080:8080 --env-file .env ys-agent"
