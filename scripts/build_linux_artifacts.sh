#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ID="aurora-iptv"
APP_NAME="Aurora IPTV"
BINARY_NAME="AuroraIPTV"
ARCH="${ARCH:-x86_64}"
VERSION="${VERSION:-${GITHUB_REF_NAME:-local}}"
SAFE_VERSION="${VERSION//\//-}"
DEB_VERSION="${VERSION#v}"
DEB_VERSION="$(printf '%s' "$DEB_VERSION" | tr -cd '0-9A-Za-z.+:~')"
if [[ ! "$DEB_VERSION" =~ ^[0-9] ]]; then
    DEB_VERSION="0.0.0+${DEB_VERSION:-local}"
fi

DIST_DIR="$ROOT_DIR/dist"
BUILD_DIR="$ROOT_DIR/build"
WORK_DIR="$ROOT_DIR/package/linux"
OUT_DIR="$ROOT_DIR/release"
VERSION_FILE="$BUILD_DIR/aurora_version.txt"

rm -rf "$DIST_DIR" "$BUILD_DIR/pyinstaller" "$WORK_DIR" "$OUT_DIR"
mkdir -p "$OUT_DIR" "$WORK_DIR" "$BUILD_DIR"
printf '%s\n' "$VERSION" > "$VERSION_FILE"

python -m PyInstaller \
    --name "$BINARY_NAME" \
    --onefile \
    --windowed \
    --icon "$ROOT_DIR/packaging/$APP_ID.ico" \
    --clean \
    --add-data "$VERSION_FILE:." \
    --add-data "$ROOT_DIR/packaging/$APP_ID.png:packaging" \
    --add-data "$ROOT_DIR/vendor/stalker_studio:vendor/stalker_studio" \
    --add-data "$ROOT_DIR/vendor/balkan_iptv:vendor/balkan_iptv" \
    --collect-all PyQt6 \
    --hidden-import PyQt6.sip \
    --distpath "$DIST_DIR" \
    --workpath "$BUILD_DIR/pyinstaller" \
    "$ROOT_DIR/main.py"

APPDIR="$WORK_DIR/AppDir"
install -Dm755 "$DIST_DIR/$BINARY_NAME" "$APPDIR/usr/bin/$BINARY_NAME"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.desktop" "$APPDIR/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.png" "$APPDIR/$APP_ID.png"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.png" "$APPDIR/usr/share/icons/hicolor/512x512/apps/$APP_ID.png"

cat > "$APPDIR/AppRun" <<'APP_RUN'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/AuroraIPTV" "$@"
APP_RUN
chmod +x "$APPDIR/AppRun"

APPIMAGETOOL="${APPIMAGETOOL:-$ROOT_DIR/appimagetool}"
if [[ ! -x "$APPIMAGETOOL" ]]; then
    echo "appimagetool nije pronadjen: $APPIMAGETOOL" >&2
    exit 1
fi
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$APPIMAGETOOL" \
    "$APPDIR" "$OUT_DIR/Aurora-IPTV-$SAFE_VERSION-linux-$ARCH.AppImage"

DEB_ROOT="$WORK_DIR/deb/$APP_ID"
install -Dm755 "$DIST_DIR/$BINARY_NAME" "$DEB_ROOT/usr/bin/$BINARY_NAME"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.desktop" "$DEB_ROOT/usr/share/applications/$APP_ID.desktop"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.png" "$DEB_ROOT/usr/share/icons/hicolor/512x512/apps/$APP_ID.png"
install -Dm644 "$ROOT_DIR/README.md" "$DEB_ROOT/usr/share/doc/$APP_ID/README.md"
mkdir -p "$DEB_ROOT/DEBIAN"
cat > "$DEB_ROOT/DEBIAN/control" <<EOF
Package: $APP_ID
Version: $DEB_VERSION
Section: utils
Priority: optional
Architecture: amd64
Maintainer: Aurora IPTV <noreply@example.com>
Depends: libc6 (>= 2.31), libgl1, libxcb-cursor0, libxkbcommon-x11-0
Description: Aurora IPTV desktop toolkit
 Unified desktop tool for IPTV list analysis, checking, export and archive workflows.
EOF
fakeroot dpkg-deb --build "$DEB_ROOT" "$OUT_DIR/Aurora-IPTV-$SAFE_VERSION-linux-amd64.deb"

TAR_ROOT="$WORK_DIR/tar/Aurora-IPTV"
install -Dm755 "$DIST_DIR/$BINARY_NAME" "$TAR_ROOT/$BINARY_NAME"
install -Dm644 "$ROOT_DIR/README.md" "$TAR_ROOT/README.md"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.desktop" "$TAR_ROOT/$APP_ID.desktop"
install -Dm644 "$ROOT_DIR/packaging/$APP_ID.png" "$TAR_ROOT/$APP_ID.png"
tar -C "$WORK_DIR/tar" -czf "$OUT_DIR/Aurora-IPTV-$SAFE_VERSION-linux-$ARCH.tar.gz" Aurora-IPTV

ls -lh "$OUT_DIR"
