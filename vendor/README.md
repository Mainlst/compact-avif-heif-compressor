# 同梱ツールと出典

画像変換に使う外部ツールと、そのライセンス・出典・チェックサムを保存しています。アプリの Python 依存関係は、リポジトリー直下の [requirements.txt](../requirements.txt) からインストールします。

| フォルダー | 用途 | 保存している情報 |
| --- | --- | --- |
| [ffmpeg](ffmpeg/) | Windows の GPU エンコード。AV1・HEVC の NVENC / QSV / AMF と JPEG の QSV を備える最小構成の共有ライブラリービルド | 実行ファイルと隣接 DLL、対応ソース、再ビルドスクリプト、ライセンス、ハッシュ |
| [libavif](libavif/) | Windows・x86-64 Linux の CPU AVIF エンコード | 未変更の公式 `avifenc`、ライセンス、上流リリース・ソースリビジョン・ハッシュ |
| [heif](heif/) | `pillow-heif` の Windows wheel に含まれる HEIF ライブラリーの出典管理 | ライセンス、wheel メタデータ、対応ソースへの参照、上流ビルドレシピ、実際の DLL のハッシュ |

Windows では `ffmpeg/windows/ffmpeg.exe` と同フォルダーの全 DLL、および `libavif/windows/avifenc.exe` を一緒に保存してください。HEIF の実行用 DLL は `requirements.txt` に指定した `pillow-heif` のインストールで取得し、EXE 作成時に同梱します。`vendor/heif` 自体には実行用 DLL を複製していません。Linux の GPU エンコードでは PATH 上の FFmpeg を使用します。

FFmpeg は未変更の対応ソースと依存ソースを [ffmpeg/sources](ffmpeg/sources/) に保存しています。[manifest.json](ffmpeg/manifest.json) に固定リビジョン・SHA-256・構成を記録し、[rebuild-minimal.sh](ffmpeg/rebuild-minimal.sh) で Linux の MinGW-w64 クロスツールチェーンから Windows 版を再ビルドできます。

```bash
bash vendor/ffmpeg/rebuild-minimal.sh
```

必要なツールと `SOURCE_DIR` の指定方法はスクリプト冒頭にあります。ソースが不足している場合は、公式の固定リビジョンから取得してハッシュを検証します。新しい作業フォルダーに出力し、既存の同梱ツールは上書きしません。大きなソースアーカイブは Git リポジトリーに保存し、Windows アプリの配布物にはライセンス・manifest・スクリプト・`SHA256SUMS` を含めています。

libavif の公式ソースとリリースのビルド情報は [manifest.json](libavif/manifest.json)・[NOTICE.txt](libavif/NOTICE.txt) の参照先から取得できます。HEIF は [manifest.json](heif/manifest.json) と [build-recipes](heif/build-recipes/) に対応する core ライブラリーのソースリビジョン・上流レシピを記録しています。HEIF のソースアーカイブはこのフォルダーには収録しておらず、一部の MinGW ランタイムの厳密なビルドリビジョンは公式 wheel のメタデータから確定できません。上流 wheel メタデータは原文のまま保存しています。

各ツールのライセンス本文と出典は、それぞれの `NOTICE.txt` と `LICENSE*`・`COPYING*`・`licenses` に保存しています。FFmpeg は LGPL-2.1-or-later の共有ライブラリービルド、HEIF の上流 Windows wheel は x265 などの同梱ライブラリーにより GPL の条件も含みます。これらは外部ツールとライブラリーのライセンスです。

仮想環境、開発用ビルド、作業キャッシュは `vendor` に含めません。同梱ファイルを更新する場合は、対応ソース・ライセンス・manifest のハッシュも一緒に確認してください。
