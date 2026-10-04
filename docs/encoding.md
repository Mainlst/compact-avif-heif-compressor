# 画像変換の詳細

[README に戻る](../README.md)

## 設定と対応形式

| 設定 | 動作 |
| --- | --- |
| 出力形式 | AVIF / HEIF / JPEG / WebP / PNG。完全保持は AVIF・HEIF・WebP・PNG。 |
| 完全保持（可逆） | 初期設定で有効。画素・透明度・画像サイズ・ICC カラープロファイルを保持します。品質とサイズの変更は無効です。 |
| 品質 | 完全保持を外した場合、1〜100 で AVIF・HEIF・JPEG・WebP の画質と容量を調整します。PNG と透明度付き HEIF は可逆で保存します。 |
| 圧縮速度 | AVIF・HEIF で高速・標準・高圧縮を選べます。高圧縮ほど処理時間が長くなる傾向があります。 |
| サイズ | 完全保持を外した場合、100 / 90 / 80 / 75 / 50 / 25 / 10% を指定し、縦横を同率に縮小します。端数は四捨五入し、各辺は最低 1 px を確保します。100% は元のサイズです。 |
| 保存先 | 元画像と同じフォルダー、または指定したフォルダー。 |
| 元ファイルを上書き | メイン画面の出力形式のすぐ下に表示します。初期状態では無効。有効にすると元画像のフォルダーへ保存し、別形式への変換でも元画像を置き換えます。 |
| 撮影情報（EXIF）を残す | 有効にすると撮影日時・位置情報などの EXIF を保持します。標準では無効です。 |
| 通知音 | 「詳細」で設定します。初期状態では無効。一括処理が完了したとき、エラーがある場合も1回鳴ります。中止、画像がない場合、終了中は鳴りません。 |
| 圧縮後自動終了 | 「詳細」で設定します。初期状態では無効。全画像の処理が成功した場合だけ、設定とウィンドウ位置を保存して終了します。中止やエラーがある場合は画面を残します。 |
| 常に最前面 | 「詳細」で設定します。初期状態では無効。メイン画面・詳細設定・結果一覧に、切り替え直後から適用します。 |

入力できる形式は JPEG・PNG・WebP・AVIF・HEIF・HEIC・BMP・TIFF・GIF です。アニメーションや複数ページ画像は対象外です。HEIF は `.heif` と `.heic` を読み込み、`.heif` で保存します。

## 完全保持と色情報

初期設定の「完全保持（可逆）」では AVIF・HEIF・WebP・PNG を CPU で可逆保存し、保存した画像を読み直して画素と ICC カラープロファイルの一致を確認します。AVIF の可逆保存には libavif の `avifenc --lossless`、HEIF には libheif の可逆設定を使用します。品質とサイズの変更は無効で、ファイルサイズが元画像より大きくなる場合があります。

「標準」「高画質」「サイズ優先」または「カスタム」を選ぶと非可逆保存に切り替わり、品質やサイズ、JPEG 出力を選べます。「詳細」の「全画素を完全保持（可逆）」でも切り替えられます。非可逆保存では全画素の完全一致を保証しません。PNG は常に可逆で保存しますが、サイズを変更した場合は縮小後の画素になります。

可逆保存と非可逆 AVIF・HEIF 保存では、対応できない色空間・ビット深度を誤って変換せずエラーを表示します。可逆保存では検証で画素や色情報の不一致が見つかった場合も出力しません。

CPU の非可逆 AVIF では、RGB 画像に 4:4:4・フルレンジ・identity 行列を指定し、色の間引きや RGB と YUV の往復変換による色の偏りを避けます。ICC カラープロファイルがあれば保持し、ICC のない通常の画像は sRGB と明示します。透明度は可逆で保存します。

HEIF は HEVC で圧縮する静止画像形式です。CPU は libheif の HEVC エンコーダーを使い、RGB 画像に 4:4:4・identity を指定します。透明度付き画像は画像全体を可逆で保存します。

JPEG 出力では透明部分を白で塗ります。標準では EXIF と XMP を出力から除去します。EXIF 保持を選んだ場合も XMP は保持しません。表示方向は EXIF を反映して保存し、反映済みの Orientation タグは除去します。縮小時は保存する EXIF 内の画像寸法も更新します。

## GPU の自動使用

GPU の検出は変換ワーカーで実際に画像をエンコードして確認するため、画面操作を止めません。GPU 非対応・実行失敗時は CPU に自動で切り替わります。結果一覧のファイルをダブルクリックすると、実際に使った GPU または CPU を確認できます。

| 出力 | GPU エンコーダー |
| --- | --- |
| 非可逆 AVIF | NVIDIA NVENC / Intel QSV / AMD AMF / Linux VAAPI（対応する AV1 ハードウェアが必要） |
| 非可逆 HEIF | NVIDIA NVENC / Intel QSV / AMD AMF / Linux VAAPI（対応する HEVC ハードウェアが必要） |
| JPEG | Intel QSV / Linux VAAPI |

非可逆 AVIF・HEIF の GPU 保存では、通常の RGB 画像を 4:2:0 に圧縮し、sRGB・フルレンジ・BT.709 の色情報を明示して保存結果を確認します。一般的なハードウェアエンコーダーに合わせた色の間引きです。透明度・ICC カラープロファイル・保持対象の EXIF 付き画像は CPU に切り替えます。完全保持（可逆）、WebP、PNG も CPU で処理します。

JPEG の GPU 保存では ICC・EXIF の設定も保持します。GPU で作った HEVC フレームは画像を再圧縮せず HEIF の静止画コンテナーへ格納し、libheif で読み戻して確認します。開発環境の RTX 3070 では HEIF の GPU エンコードを実機確認しています。

Windows の配布物には FFmpeg を同梱しています。ソース実行では同梱版、または PATH 上の FFmpeg を使います。Linux VAAPI は使用する FFmpeg とデバイスの対応に依存します。

## 保存名と元ファイルの置き換え

保存名は元の名前と出力形式の拡張子だけです。例えば `写真.jpg` を AVIF に変換すると `写真.avif` になり、`_compressed` や連番は付けません。同名の既存出力は上書きします。

初期状態では元ファイルを上書きしません。入力と保存先が同じパスになる場合は、別の保存先を選ぶか、メイン画面の「元ファイルを上書き」を有効にしてください。チェックの有無で現在の設定を確認でき、切り替えた設定は次回の起動にも引き継がれます。

「元ファイルを上書き」を有効にすると、指定した保存先にかかわらず元画像と同じフォルダーへ保存します。出力の拡張子が元画像と同じ場合は元画像自身を上書きし、拡張子が変わる場合は新しい名前で保存した後に元画像を削除します。変換と保存結果の検証は一時ファイルで完了してから置き換えます。

元画像の削除に失敗した場合は、新規出力の取り消し、または同名の既存出力の復元を試み、元画像を残します。OS が復元も拒否した場合は、残っている出力や復元用コピーの場所をエラーに表示し、復元用コピーを保持します。

## 操作と設定の保存

詳細設定と結果一覧は別ウィンドウで表示するため、メイン画面の大きさは変わりません。`Ctrl+O` で画像選択、`Ctrl+D` で詳細設定、`Ctrl+R` で結果一覧を開きます。中止ボタンは処理中の 1 枚が終わってから残りの処理を停止します。

設定と、最後に閉じたメインウィンドウの位置は次の起動時にも引き継がれます。最小化中に閉じても直前の通常位置を保存します。モニター構成が変わった場合は、画面内へ位置を補正します。起動時は画面の内容・大きさ・保存位置の準備が終わるまで非表示にします。

「通知音」「圧縮後自動終了」「常に最前面」の設定も記憶します。通知音と自動終了を両方有効にした場合は、全画像が成功した後に通知して終了します。

設定ファイルは Windows では `%APPDATA%\ImageCompressor\settings.json`、Linux では `$XDG_CONFIG_HOME/image-compressor/settings.json`（未設定の場合は `~/.config/image-compressor/settings.json`）に保存します。

## 依存ライブラリーと同梱資料

Python の実行依存は [requirements.txt](../requirements.txt)、EXE 作成用の依存は [requirements-dev.txt](../requirements-dev.txt)に固定しています。

同梱ツールの構成、出典と再ビルド資料は [vendor/README.md](../vendor/README.md)にまとめています。

- Pillow 12.3.0：画像の読み込み・リサイズ・保存と AVIF 対応
- pillow-heif 1.8.0：libheif による HEIF・HEIC の読み込みと CPU 保存
- libavif 1.4.2 の avifenc：AVIF の可逆／非可逆保存。Windows・Linux 用のツールを [vendor/libavif](../vendor/libavif)に同梱
- FFmpeg 9.0.2：GPU エンコーダーの検出と非可逆保存。静止画像用の独自 LGPL 共有ライブラリービルド
- tkinterdnd2 0.6.3：ドラッグ＆ドロップ
- PyInstaller 6.22.3：EXE 作成時のみ

FFmpeg は元の同梱版と同じソースリビジョン `46d8f462eeb87ee1f704d8c44a0ee24fca471ad1` から、画像変換に必要な機能に絞って構築しています。Windows の AV1・HEVC 用 NVIDIA NVENC / Intel QSV / AMD AMF と JPEG 用 Intel QSV の計 7 エンコーダーを維持します。

構成とチェックサムは [vendor/ffmpeg/manifest.json](../vendor/ffmpeg/manifest.json)、再ビルド手順は [vendor/ffmpeg/rebuild-minimal.sh](../vendor/ffmpeg/rebuild-minimal.sh)、対応ソースは [vendor/ffmpeg/sources](../vendor/ffmpeg/sources)に保存します。配布ライセンスは同フォルダーの [NOTICE.txt](../vendor/ffmpeg/NOTICE.txt)・`COPYING.*`・`licenses` にあります。大きな対応ソースのアーカイブはリポジトリーに残し、アプリの配布物には含めません。

Windows 用 HEIF ライブラリーのライセンス本文、実際のバージョン、チェックサム、公式ソースの参照先は [vendor/heif](../vendor/heif)に保存し、EXE にも同梱します。依存ライブラリーのライセンスは各同梱資料を参照してください。

完成した配布物には専用アイコン、AVIF 用ライブラリー、可逆／非可逆保存用の `avifenc.exe`、HEIF 用 libheif、GPU 用 FFmpeg と DLL、ドラッグ＆ドロップ用の Tcl・ネイティブライブラリーを同梱します。ZIP 作成には Python 3 の標準ライブラリーを使い、追加の ZIP ツールは不要です。EXE はビルドした OS・CPU アーキテクチャ向けです。

アイコンは imagegen で生成し、[assets/app-icon.png](../assets/app-icon.png) と [assets/app-icon.ico](../assets/app-icon.ico)に保存しています。生成プロンプトは [assets/icon-prompt.txt](../assets/icon-prompt.txt)です。

### 公式資料

- [libavif の avifenc 仕様](https://github.com/AOMediaCodec/libavif/blob/v1.4.2/apps/avifenc.c)：AVIF の品質・速度・色空間指定
- [Pillow の AVIF 対応](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#avif)：画像の読み込み
- [pillow-heif の保存仕様](https://pillow-heif.readthedocs.io/en/latest/saving-images.html)：HEIF の可逆保存と色情報指定
- [FFmpeg のエンコーダー仕様](https://ffmpeg.org/ffmpeg-codecs.html)：GPU エンコードの指定
- [PyInstaller の使用方法](https://pyinstaller.org/en/stable/usage.html)：EXE 作成
