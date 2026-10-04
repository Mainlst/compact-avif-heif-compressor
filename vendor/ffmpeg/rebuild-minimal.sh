#!/usr/bin/env bash
# Rebuild the unmodified, shared Windows image-encoding runtime from pinned sources.
# Linux prerequisites: git, curl, make, cmake, nasm, pkgconf/pkg-config, and
# gcc/g++-mingw-w64-x86-64-posix (GCC 13.2, mingw-w64 11.0.1 in this build).
# Sources are retained in the repository; their URLs/hashes are in manifest.json.
# SOURCE_DIR may point to a writable source-archive directory containing the
# supplied SHA256SUMS file. Missing archives are fetched from official sources.
# An optional first argument chooses the build-parent directory. No installed
# vendor files are replaced: the result is printed as a new work.*/runtime path.
set -euo pipefail

task_bundle=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
task_sources=${SOURCE_DIR:-"$task_bundle/sources"}
task_parent=${1:-"${TMPDIR:-/tmp}/image-compressor-ffmpeg-rebuild"}
task_jobs=${JOBS:-4}
task_cross=${CROSS_PREFIX:-x86_64-w64-mingw32-}
task_cc=$(command -v "${task_cross}gcc-posix" || command -v "${task_cross}gcc")
task_cxx=$(command -v "${task_cross}g++-posix" || command -v "${task_cross}g++")
task_rc=$(command -v "${task_cross}windres")
task_strip=$(command -v "${task_cross}strip")
task_pkgconfig=$(command -v pkgconf || command -v pkg-config)

mkdir -p "$task_parent"
task_work=$(mktemp -d "$task_parent/work.XXXXXX")
if [[ "$task_work" =~ [[:space:]] ]]; then
    printf 'Choose a build-parent directory without spaces: %s\n' "$task_work" >&2
    exit 1
fi
task_prefix="$task_work/prefix"
mkdir -p "$task_prefix/include/AMF" "$task_prefix/lib" "$task_work/runtime"

task_git_archive() {
    local task_name=$1 task_commit=$2 task_url=$3
    shift 3
    local task_archive="$task_sources/$task_name-$task_commit.tar.gz"
    if [[ ! -f "$task_archive" ]]; then
        local task_checkout="$task_work/fetch-$task_name"
        git init --quiet "$task_checkout"
        git -C "$task_checkout" remote add origin "$task_url"
        git -C "$task_checkout" fetch --depth=1 --filter=blob:none origin "$task_commit"
        git -C "$task_checkout" archive --format=tar \
            --prefix="$task_name-$task_commit/" FETCH_HEAD "$@" | gzip -n > "$task_archive"
    fi
}
mkdir -p "$task_sources"
if [[ ! -f "$task_sources/FFmpeg-46d8f462eeb87ee1f704d8c44a0ee24fca471ad1.tar.gz" ]]; then
    curl --fail --location https://github.com/FFmpeg/FFmpeg/archive/46d8f462eeb87ee1f704d8c44a0ee24fca471ad1.tar.gz \
        --output "$task_sources/FFmpeg-46d8f462eeb87ee1f704d8c44a0ee24fca471ad1.tar.gz"
fi
task_git_archive nv-codec-headers 1889e62e2d35ff7aa9baca2bceb14f053785e6f1 https://github.com/FFmpeg/nv-codec-headers.git
task_git_archive libvpl 674d015bcb294bc39fa276e99a652ea045423e82 https://github.com/intel/libvpl.git
task_git_archive AMF-headers 8c648005e07d4309033282bfd9947df2c7e76104 https://github.com/GPUOpen-LibrariesAndSDKs/AMF.git amf/public/include LICENSE.txt
if [[ ! -f "$task_sources/zlib-1.3.2.tar.gz" ]]; then
    curl --fail --location https://zlib.net/fossils/zlib-1.3.2.tar.gz --output "$task_sources/zlib-1.3.2.tar.gz"
fi
(cd "$task_sources" && sha256sum --check SHA256SUMS)

task_extract() {
    mkdir -p "$task_work/$2"
    tar -xzf "$task_sources/$1" -C "$task_work/$2" --strip-components=1
}
task_extract FFmpeg-46d8f462eeb87ee1f704d8c44a0ee24fca471ad1.tar.gz ffmpeg
task_extract nv-codec-headers-1889e62e2d35ff7aa9baca2bceb14f053785e6f1.tar.gz nv-codec-headers
task_extract AMF-headers-8c648005e07d4309033282bfd9947df2c7e76104.tar.gz AMF
task_extract libvpl-674d015bcb294bc39fa276e99a652ea045423e82.tar.gz libvpl
task_extract zlib-1.3.2.tar.gz zlib

make -C "$task_work/nv-codec-headers" install PREFIX="$task_prefix"
cp -R "$task_work/AMF/amf/public/include/." "$task_prefix/include/AMF/"
cmake -S "$task_work/libvpl" -B "$task_work/libvpl-build" \
    -DCMAKE_SYSTEM_NAME=Windows -DCMAKE_C_COMPILER="$task_cc" \
    -DCMAKE_CXX_COMPILER="$task_cxx" -DCMAKE_RC_COMPILER="$task_rc" \
    -DCMAKE_INSTALL_PREFIX="$task_prefix" -DCMAKE_BUILD_TYPE=MinSizeRel \
    -DBUILD_SHARED_LIBS=ON -DBUILD_TESTS=OFF -DBUILD_EXAMPLES=OFF \
    '-DCMAKE_SHARED_LINKER_FLAGS=-static-libgcc -static-libstdc++ -Wl,--gc-sections'
cmake --build "$task_work/libvpl-build" --parallel "$task_jobs"
cmake --install "$task_work/libvpl-build"
make -C "$task_work/zlib" -f win32/Makefile.gcc PREFIX="$task_cross" \
    CC="$task_cc" libz.a -j"$task_jobs"
cp "$task_work/zlib/libz.a" "$task_prefix/lib/"
cp "$task_work/zlib/zlib.h" "$task_work/zlib/zconf.h" "$task_prefix/include/"

export PKG_CONFIG_LIBDIR="$task_prefix/lib/pkgconfig"
(
    cd "$task_work/ffmpeg"
    ./configure \
        --arch=x86_64 --target-os=mingw32 --cross-prefix="$task_cross" \
        --cc="$task_cc" --cxx="$task_cxx" --pkg-config="$task_pkgconfig" \
        --enable-cross-compile --prefix="$task_work/install" \
        --disable-autodetect --disable-everything --enable-ffmpeg --disable-ffprobe \
        --disable-doc --disable-debug --enable-small --enable-shared --disable-static \
        --disable-avdevice --disable-swresample --disable-network --enable-w32threads \
        --enable-swscale --enable-zlib --enable-ffnvcodec --enable-nvenc --enable-cuda \
        --enable-amf --enable-libvpl --enable-d3d11va --enable-dxva2 \
        --enable-decoder=png --enable-parser=png,hevc,av1 \
        --enable-demuxer=image2,image2pipe \
        --enable-encoder=av1_nvenc,hevc_nvenc,av1_qsv,hevc_qsv,mjpeg_qsv,av1_amf,hevc_amf \
        --enable-muxer=avif,mp4,image2 --enable-protocol=file \
        --enable-filter=buffer,buffersink,scale,format,setparams,null,hwupload \
        --extra-cflags="-I$task_prefix/include" \
        --extra-ldflags="-L$task_prefix/lib -static-libgcc -Wl,--gc-sections"
    make -j"$task_jobs"
)
cp "$task_work/ffmpeg/ffmpeg.exe" "$task_prefix/bin/libvpl.dll" "$task_work/runtime/"
for task_library in avcodec avfilter avformat avutil swscale; do
    cp "$task_work/ffmpeg/lib$task_library/"*-*.dll "$task_work/runtime/"
done
task_pthread=$("$task_cxx" -print-file-name=libwinpthread-1.dll)
cp "$task_pthread" "$task_work/runtime/"
for task_dll in "$task_work/runtime/"*.dll; do
    "$task_strip" --strip-unneeded "$task_dll"
done
printf '\nRebuilt runtime: %s\n' "$task_work/runtime"
printf 'Keep ffmpeg.exe and all adjacent DLLs together. Validate on Windows before replacing vendor files.\n'
