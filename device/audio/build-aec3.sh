#!/bin/bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "$0")/../.." && pwd)
SRC="$ROOT/tools/webrtc-audio-processing/webrtc-audio-processing-1.3"
PIPEWIRE="$ROOT/tools/pipewire/pipewire-1.0.5"
BUILD=${BUILD_DIR:-"$ROOT/work/aec3-build"}
OUTPUT="$ROOT/outputs/audio"
MESON=(meson)
if [ -n "${MESON_SCRIPT:-}" ]; then MESON=(python3 "$MESON_SCRIPT"); fi
CROSS=()
if [ -n "${CROSS_FILE:-}" ]; then CROSS=(--cross-file "$CROSS_FILE"); fi

# Upstream WebRTC APM 1.3 and PipeWire 1.0.5; build on the host, not the device.
# WebRTC archive SHA256: 95552fc17faa0202133707bbb3727e8c2cf64d4266fe31bfdb2298d769c1db75
"${MESON[@]}" setup "$BUILD" "$SRC" "${CROSS[@]}" \
    --buildtype release --default-library static -Db_staticpic=true -Dneon=yes
ninja -C "$BUILD" -j "${JOBS:-4}"
cp "$PIPEWIRE/spa/plugins/aec/aec-webrtc.cpp" "$BUILD/aec-webrtc.cpp"
patch --directory "$BUILD" -p1 < "$ROOT/device/audio/aec3.patch"
printf '#pragma once\n' > "$BUILD/config.h"
mapfile -t LIBS < <(find "$BUILD" -name '*.a' -type f)
mkdir -p "$OUTPUT/licenses"
"${CXX:-c++}" -std=c++17 -O2 -shared -fPIC -DWEBRTC_POSIX \
    -I"$BUILD" -I"$PIPEWIRE/spa/include" -I"$SRC/webrtc" \
    -I"$SRC/subprojects/abseil-cpp-20230125.1" \
    "$BUILD/aec-webrtc.cpp" -Wl,--start-group "${LIBS[@]}" -Wl,--end-group \
    -pthread -lm -Wl,--no-undefined -Wl,--exclude-libs,ALL \
    -o "$OUTPUT/libspa-aec-thinksmart.so"
"${STRIP:-strip}" --strip-unneeded "$OUTPUT/libspa-aec-thinksmart.so"
readelf -h "$OUTPUT/libspa-aec-thinksmart.so" | grep -q 'Machine:.*AArch64'
cp "$PIPEWIRE/COPYING" "$OUTPUT/licenses/PipeWire.txt"
cp "$SRC/COPYING" "$OUTPUT/licenses/WebRTC.txt"
cp "$SRC/subprojects/abseil-cpp-20230125.1/LICENSE" "$OUTPUT/licenses/Abseil.txt"
for name in webrtc/LICENSE webrtc/third_party/rnnoise/COPYING \
    webrtc/third_party/pffft/LICENSE webrtc/modules/third_party/fft/LICENSE \
    webrtc/common_audio/third_party/spl_sqrt_floor/LICENSE \
    webrtc/common_audio/third_party/ooura/LICENSE; do
    cp "$SRC/$name" "$OUTPUT/licenses/${name//\//_}.txt"
done
if [ -n "${GCC_RUNTIME_LICENSE:-}" ]; then
    cp "$GCC_RUNTIME_LICENSE" "$OUTPUT/licenses/GCC-runtime.txt"
fi
