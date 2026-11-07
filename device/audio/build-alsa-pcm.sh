#!/bin/bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "$0")/../.." && pwd)
SRC=${PIPEWIRE_SRC:-"$ROOT/tools/pipewire/pipewire-1.0.5"}
BUILD=${BUILD_DIR:-"$ROOT/work/alsa-pcm-build"}
OUTPUT="$ROOT/outputs/audio"

# PipeWire 1.0.5 ALSA PCM nodes plus alsa-pcm-watchdog.patch; build on the host, not the device.
rm -rf "$BUILD"
mkdir -p "$BUILD" "$OUTPUT/licenses"
for name in alsa-pcm.c alsa-pcm.h alsa-pcm-sink.c alsa-pcm-source.c alsa.h; do
    cp "$SRC/spa/plugins/alsa/$name" "$BUILD/$name"
done
patch --directory "$BUILD" -p1 < "$ROOT/device/audio/alsa-pcm-watchdog.patch"
# CPPFLAGS/LDFLAGS may supply the target's ALSA headers and libraries when cross compiling.
"${CC:-cc}" ${CPPFLAGS:-} ${CFLAGS:--O2} -shared -fPIC -fvisibility=hidden \
    -I"$BUILD" -I"$SRC/spa/include" \
    "$ROOT/device/audio/alsa-pcm-plugin.c" "$BUILD/alsa-pcm.c" "$BUILD/alsa-pcm-sink.c" "$BUILD/alsa-pcm-source.c" \
    ${LDFLAGS:-} -lasound -lm -Wl,--no-undefined \
    -o "$OUTPUT/libspa-alsa-thinksmart.so"
cp "$SRC/COPYING" "$OUTPUT/licenses/PipeWire.txt"
