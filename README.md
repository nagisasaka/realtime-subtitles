# Realtime Subtitles — 英語音声＋日本語字幕

Windows 11 **ネイティブのマイク入力**から英語原文と日本語訳を同時表示する Python / tkinter アプリです。
音声取得・24kHz PCM変換は1回だけ行い、同じフレームを日本語用Translationと英語用Transcriptionの2接続に配信します。
当初の同一session方式はrawイベントで英語の途切れを確認したため、指定された切り分け後に2接続へ変更しました。
比較用の同一sessionモードも残しています。
英語は上段・小さめ、日本語は下段・大きめ。半透明の黒背景、最前面表示、ドラッグ移動、
リサイズ、マイク選択、入力レベル、履歴保存に対応します。

PC再生音のloopback取得、翻訳音声の再生、Qt、Electronは使いません。
WSLg / PulseAudio / ALSA の入力にも依存しません。マイク取得とアプリ起動はWindows Pythonで行います。

## Speechmatics比較実験（`experiment/speechmatics`）

安定版の復元ポイントは `openai-stable-v1` → `1e95fe5b433c846a1534d25301060b17416e8930` です。
`main` とこのtagは変更しません。通常の `python -m realtime_subtitles` は従来のOpenAI版を起動します。
以下は別エントリーポイントから起動する実験機能で、既定backendの切り替えではありません。
既存の `audio.py` / `realtime_api.py` / `transcription.py` / `subtitle_buffer.py` / `ui.py` はbaselineと同一です。

### 構成とAPI仕様

- Speechmaticsは公式の現行Python SDK `speechmatics-rt` を使用します。検証バージョンは **1.2.1**。
  `TranslationConfig` と英日4種類のイベントが公開されているため、独自WebSocket実装は使いません。
- 接続先は `wss://global.rt.speechmatics.com/v2/`（近いregionへルーティング）。`--endpoint` で公式の地域別endpointも指定できます。
- `transcription_config`: `model="enhanced"`, `language="en"`, `diarization="speaker"`, `enable_partials=true`, `max_delay=4.0`。
  精度優先の初期設定です。遅延を比較する場合は `--max-delay 0.7` / `1` / `2` / `3` / `4` を指定します。
- `translation_config`: `target_languages=["ja"]`, `enable_partials=true`。
- マイクは既存のWindows sounddevice経路で1つだけ開き、native rateのfloat32から既存converterで24kHz mono PCM16へ変換します。
  Speechmaticsへは200ms・9,600bytesをbinary audioとして送ります。PyAudio・WSLg入力は使いません。
- `--compare` では同じ取得済みPCMを独立したbounded queueへ分岐し、OpenAIとSpeechmaticsへ送ります。
  OpenAIには既存のmicrophone factory注入箇所から取得済み音声を渡すだけで、既存のsession設定・EN/JA受信・再接続コードは変更しません。
  OpenAIの既存の英語用接続と遅延話者分離も維持するため、比較中は両社の利用料が発生します。
- Speechmaticsの英語は `AddPartialTranscript` / `AddTranscript`、日本語は `AddPartialTranslation` / `AddTranslation` を区別します。
  partialは未確定部分を**置換**し、finalは確定履歴へ追加します。final確定時は対応範囲のpartialを除き、既に届いた後続範囲のpartialは残します。
- `SubtitleSegment` にbackend・language・text・is_final・start_ms・end_ms・speaker・session_id・受信monotonic時刻を保持します。
  ENはword/punctuationごとのspeaker・時刻、JAは各translation resultのspeaker・時刻を取得します。
  SDKのtranscript文字列だけへ変換せずraw eventのresultsを解析します。ENの空白・句読点は`metadata.transcript`を維持します。
- 時刻はAPIの秒をmsに換算し、**セッション内の音声時刻**として扱います。再接続後は別session_idなので、前の時刻・話者IDと混同しません。
  `received_monotonic_ms`は受信時刻であり音声時刻ではありません。
  OpenAIの正規化イベントでは不明なstart_ms・end_ms・speakerは`null`。`elapsed_ms`から区間時刻を捏造しません。
  OpenAIの`is_final=true`は本アプリが保持するappend-only deltaを表し、Speechmaticsのfinalイベントと同等の発話確定通知という意味ではありません。
- Speechmatics側で後追いdiarization・fuzzy alignment・時刻推定は行いません。APIのspeakerだけを使い、確定字幕の話者交代に空行を表示します。
  人物ごとの固定色や話者見出しは表示しません。speakerが不明なら交代を推測しません。
- SDK 1.2.1ではAudioAdded受信時に内部送信カウンターが書き換わるため、終了時は公開`send_message()`で
  自前の実送信数を`EndOfStream.last_seq_no`へ指定し、`EndOfTranscript`を最大8秒待ちます。
  Speechmaticsだけの再接続は5秒から最大30秒のbackoff、最大8回。Stopは接続待ち・backoff中でも中断できます。
  認証/設定エラーを表示し、Speechmaticsの失敗でOpenAIの正常な接続は再起動しません。

### Windowsで導入・起動

既存の安定版と分けた実機確認先は **`C:\workspace\realtime-subtitles-speechmatics`** です。
WSL側のソースをコピーする場合（Windowsのvenvは別途Windows Pythonで作成）:

```bash
bash scripts/sync-to-windows.sh /mnt/c/workspace/realtime-subtitles-speechmatics
```

Windows PowerShell:

```powershell
cd C:\workspace\realtime-subtitles-speechmatics
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -e ".[speechmatics,dev]"

# 設定済みのWindowsユーザー環境変数を、このPowerShellにも反映。値は表示しません。
$env:SPEECHMATICS_API_KEY=[Environment]::GetEnvironmentVariable("SPEECHMATICS_API_KEY","User")
$env:OPENAI_API_KEY=[Environment]::GetEnvironmentVariable("OPENAI_API_KEY","User")

# Phase 1: Speechmaticsだけでコンソール確認（OpenAI keyは不要）
.\.venv-win\Scripts\python.exe -m realtime_subtitles.comparison --seconds 30

# 同じマイク入力を両社へ送り、コンソール比較
.\.venv-win\Scripts\python.exe -m realtime_subtitles.comparison --compare --seconds 60

# 比較ウィンドウ（起動後にStart。右: Speechmatics、左: OpenAI）
.\.venv-win\Scripts\python.exe -m realtime_subtitles.comparison --compare --gui

# Speechmaticsだけのウィンドウ
.\.venv-win\Scripts\python.exe -m realtime_subtitles.comparison --gui
```

`SPEECHMATICS_API_KEY` はSpeechmatics portalで取得し、Windowsユーザー環境変数へ設定してください。
既存の`OPENAI_API_KEY`は変更不要です。実験キーをファイルへ保存したり、チャットへ貼ったりする必要はありません。
`scripts/run-speechmatics.ps1` も用意していますが、PowerShellのscript実行が禁止されている環境では上記のPython直接起動を使えます。

`--list-devices` でWindows入力一覧、`--device 番号` でマイクを指定できます。GUIではドロップダウンから選択します。
比較時は旧OpenAIアプリの録音をStopにし、比較プロセス1つだけをStartしてください。旧ウィンドウを閉じる必要はありません。
実験ウィンドウは既存overlayとは別のUIで、EN/JAサイズ・最前面・手動スクロール・最新追従に対応します。
薄い文字が未確定partialです。実験UIの位置・フォント設定は現段階では保存せず、安定版のsettings.jsonも書き換えません。

### 比較ログと保存

通常は字幕をRAMに保持し、音声ファイルは作りません。Speechmatics側に自動ファイル保存はありません。
`Save finals` は `%USERPROFILE%\RealtimeSubtitles\Comparisons` へ日時付きの新規ファイルを作り、
Speechmaticsのfinal segmentと、比較時はOpenAIの従来形式の履歴をそれぞれ保存します。partialは保存対象に含めません。
OpenAI側の既存diarization-debug.logは比較中も従来通り生成されます。

partialの変化も含めて後から比べたい場合は、明示的にイベントログを指定します:

```powershell
New-Item -ItemType Directory -Force diagnostics | Out-Null
.\.venv-win\Scripts\python.exe -m realtime_subtitles.comparison --compare --seconds 60 --event-log diagnostics\comparison.jsonl --save diagnostics\speechmatics-finals.jsonl
```

- `--event-log`: 英日partial/finalのraw JSONと正規化segment、OpenAIのdelta、受信UTC/monotonic時刻、開始終了時の診断情報をJSONLで保存。
  字幕文章を含みます。APIキー・Authorization header・音声bytesは記録しません。
- `--save`: 終了時にSpeechmaticsの**finalだけ**をJSONL保存。話者・時刻を保持します。
- 同名ファイルは上書きしません。別のファイル名を指定してください。ログ書込は独立workerとbounded queueで行います。
- `diagnostics/`と`*.jsonl`はGit対象外です。ファイルサイズのローテーションは未実装なので、比較ごとに終了して別ファイルにしてください。
- canonical timestampは音声区間時刻、ログの受信時刻は到着時刻です。両者の差をそのまま厳密なend-to-end latencyとみなさないでください。
  接続開始・キューdrop・APIのセッション時刻基準を考慮した遅延統計や正解文章に対するWER集計は未実装です。

### 実測・テスト状況（2026-10-06）

Windows: **62 passed**。WSL: **56 passed / 6 skipped**（Windows専用テスト）。Ruff lint/format・両環境の`pip check`も通過。

- Windowsネイティブの実マイク、44100Hz mono float32 → 24000Hz PCM16でSpeechmaticsの実APIに接続。
  最初の30秒検証で EN partial 75 / final 13、JA partial 1 / final 3 を受信。
  JAのraw resultにもspeakerとstart_time/end_timeがあることを確認しました。
- 比較ウィンドウで同じPCMを両社へ送信。約1分＋Stop/Start後の約30秒で、Speechmatics累計
  EN partial 229 / final 45、JA partial 1 / final 5、OpenAI EN/JA delta合計379を受信しました。
  2回ともSpeechmatics送信queue dropは0、終了時はSTOPPED、接続エラーなしでした。
  時間指定は接続待ちも含むため、送信済み音声の長さは壁時計より短くなります。
- 英語/日本語partial置換、final確定と色変更、wordの話者保持、翻訳の時刻保持、重複final抑制、再接続、
  EOS実送信数、Start/Stop/Start、認証エラーの秘匿、1つのマイクから同一bytesのfan-out、
  Speechmatics障害時のOpenAI継続、Windows Tkのスクロール維持をテストします。
- 実API受信・Windows表示・同時比較が可能であることの確認です。会話内容の正解ラベルを用いた精度評価ではなく、
  現時点でどちらが高精度かは断定しません。JA partialは常時出るとは限らず、finalのみの区間もあります。

```powershell
.\.venv-win\Scripts\python.exe -m pytest -q
.\.venv-win\Scripts\python.exe -m ruff check .
```

### 安定版の復元

実験branchの変更をコミットした上で `git switch main`、または実験を残したまま別作業フォルダに安定版を取り出せます:

```bash
git worktree add ../jimaku-openai-stable openai-stable-v1
```

そのフォルダから従来の環境構築・起動手順を使います。`openai-stable-v1` tagを付け替える必要はありません。
実機確認用の旧 `C:\workspace\realtime-subtitles` は引き続き安定版のソースです。

確認した公式資料:
[Realtime quickstart](https://docs.speechmatics.com/speech-to-text/realtime/quickstart)、
[Realtime API schema](https://docs.speechmatics.com/api-ref/realtime-transcription-websocket)、
[Translation](https://docs.speechmatics.com/speech-to-text/features/translation)、
[Realtime diarization](https://docs.speechmatics.com/speech-to-text/realtime/realtime-diarization)、
[推奨SDK](https://docs.speechmatics.com/integrations-and-sdks/sdks)。

## 現在の動作・保存・通信・料金（2026-10-06確認）

現行の標準構成は **英語用WebSocket + 日本語用WebSocket + 遅延する話者分離HTTP API** です。
同じWindowsマイクの音声を1回だけ取得し、24kHz mono PCM16へ変換して3つの処理へ渡します。
英語と日本語はそれぞれ逐次表示します。話者交代は数十秒遅れて推定し、対応が確かな位置へ空行だけを追加します。
話者ラベル・人物ごとの色・見出しは表示せず、Realtime字幕本文も書き換えません。

### 音声や結果は保存されるか

| データ | 保存方法・保存先 |
| --- | --- |
| マイク音声 | **通常動作では音声ファイルを保存しません。** 音声キュー・30秒のring buffer・処理待ち/処理中windowはRAM上に保持します。APIへ送るWAVも`BytesIO`で作り、ディスクの一時WAVは作りません |
| 英語・日本語の字幕全文と話者境界 | アプリのRAMに保持。**定期自動保存・終了時自動保存・再起動時の復元は未実装**です。設定画面の **Save → 保存** で明示的に書き出します |
| 手動保存ファイル | 既定は `%USERPROFILE%\RealtimeSubtitles\Transcripts\subtitles-日時.jsonl`。保存画面でパスや拡張子を変更できます。`.txt`は話者改行付き本文、`.jsonl`は元delta・時刻・表示用本文・境界metadataを保存します。既存ファイルは上書きしません |
| ウィンドウ・フォント・マイク等の設定 | **自動保存**。変更から約500ms後および終了時に `%APPDATA%\RealtimeSubtitles\settings.json` へ保存します |
| 話者分離の診断ログ | 話者分離有効時に**自動保存**。`%APPDATA%\RealtimeSubtitles\diarization-debug.log`。segmentの認識文章、話者ラベル、時刻、境界推定、エラーを含みます。約2MB×最大3ファイルのローテーション |
| Realtimeのrawイベントログ | 標準起動では無効。`--event-log パス` 指定等で有効化した場合に**自動追記**。EN/JAの認識文章を含みますが、音声payload・APIキー・Authorization headerは記録しません。現在の実装にはファイルのサイズ上限・ローテーションがありません |

StopやClearは保存操作ではありません。Stop/Startを繰り返しても同じアプリ内の字幕履歴は残り、
Clearは表示を消すだけなので過去分もSaveの対象です。保存後に届いた字幕・話者境界は既存の保存ファイルへ自動追記しません。
アプリ終了・異常終了後に字幕全文を確実に残すには、事前にSaveが必要です。
診断ログは字幕の一部を残しますが、全文保存や再開用データの代わりにはなりません。

**開発中の現在のアプリではrawログが有効**になっています（2026-10-06確認）。
保存先は `C:\workspace\realtime-subtitles\diagnostics\speaker-timing-check.jsonl` です。
これは調査用に有効化した実行中設定で、通常起動時の既定値ではありません。
診断ログを含め「文章は何も自動保存されない」という状態ではない点に注意してください。

上記はアプリ自身のローカル保存仕様です。StopはRAM上の全音声を即時消去する機能ではなく、
直近のbuffer等は次の録音による置換やプロセス終了まで残る場合があります。会議全体を録音ファイルとして蓄積する実装ではありません。
音声は処理のためOpenAIへ送信します。OpenAI側のデータ保持はアカウント・組織設定に依存する別事項で、
このアプリから保持設定を変更していません。

### API接続先とタイミング

全てOpenAIの `api.openai.com:443` にTLS通信し、認証は環境変数 `OPENAI_API_KEY` を使用します。

| 用途 | モデル | 接続先 | 接続・送信タイミング |
| --- | --- | --- | --- |
| 日本語翻訳 | `gpt-realtime-translate` | `wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate` | Start時に接続し、Stopまで維持。200ms・9,600bytesのPCMを毎秒約5回送信 |
| 英語文字起こし | `gpt-live-transcribe` | `wss://api.openai.com/v1/realtime?intent=transcription` | Start時に別WebSocketを接続し、Stopまで維持。同じ200ms PCMを毎秒約5回送信。モデル名はsession設定で指定 |
| 話者交代の検出 | `gpt-4o-transcribe-diarize` | `POST https://api.openai.com/v1/audio/transcriptions` | 最初の30秒が溜まってから1回。その後は約25秒ごとに30秒分を送信。5秒ずつ重複する独立HTTPリクエスト |

- **200msごとに再接続するわけではありません。** 2本のWebSocket上で小さな音声メッセージを連続送信します。
- 無音も送信します。音量判定は改行や英語側の区間確定に利用しますが、音声送信を止めるVADには使いません。
- 英語側は発話後の約1秒の静けさ、または20秒分の音声ごとにcommitします。commitは区間確定要求であり再接続ではなく、deltaは途中から表示します。
- WebSocketの生存確認pingは20秒間隔。接続断時は0.5秒、1秒、2秒、4秒、8秒…（上限8秒、最大8回）の待機で再接続します。
  英語側だけの接続障害は英語側で再接続し、英語字幕の停止だけを理由に日本語の正常な接続を再起動するwatchdogはありません。
- 話者分離は独立workerです。応答に時間がかかれば25秒間隔を維持できない場合があり、古い待機jobを捨てます。
  HTTPのtimeout設定は40秒。不正response・一時エラー時は次の新しいwindowで再試行し、Realtime接続を再起動しません。
- Stopでマイク取得と新しい音声の連続送信を止め、残りの字幕を受信してWebSocketを閉じます。
  話者分離の送信済みリクエストや停止時に残っていた待機jobは完了する場合があります。30秒未満の末尾を特別に送る処理はありません。
- 標準のGUIは起動後Startを押すまで接続しません。開発用の `C:\workspace\jimaku-tools\run-live-subtitles.py` は起動後に自動Startする別ランチャーです。
- 翻訳音声はAPIから受信しますが破棄し、再生・録音しません。字幕だけを表示する構成です。

### 1時間あたりのAPI料金の目安

2026-10-06に確認した公式USD単価に基づく、**60分連続稼働・標準の2接続・話者分離有効**の概算です。
実際の請求額やアカウント残高を取得した結果ではありません。

| 処理 | 公式単価 | 1時間の概算 |
| --- | --- | --- |
| 日本語翻訳 | $0.034 / 音声1分 | $2.04 |
| 英語文字起こし | $0.017 / 音声1分 | $1.02 |
| 話者分離 | 推定 $0.006 / 音声1分 | 約$0.43（重複window分を含む） |
| **合計** | | **約$3.49 / 時間** |
| 話者分離を無効にした場合 | `--no-diarization` | **約$3.06 / 時間** |

話者分離は30秒分を25秒ごとに送るため、長時間平均では `30 / 25 = 1.2倍` の音声を処理します。
`60分 × 1.2 × $0.006 = $0.432`。厳密には開始から最初の1時間で完全なwindowは143個、71.5分相当なので約$0.429です。
話者分離の分単価は公式の推定値であり、固定の分課金を保証するものではありません。
処理欠落・追加検証・複数アプリの同時起動・実際のusage・税・為替で支払額は変わります。
仮に1ドル150円なら合計約524円/時間ですが、現在の為替を示す数字ではありません。

Realtimeの2モデルは音声時間ベースです。アプリは無音も送り続けるため、見積もりでは発話時間だけでなく
**StartからStopまでの送信時間全体**を数えてください。最小化・透明化・設定画面を閉じる操作では停止しません。
翻訳音声を再生しないことによる割引は計算に入れていません。
アプリ内の実請求集計・費用上限による自動停止は未実装です。

料金出典：
[GPT-Realtime-Translate](https://developers.openai.com/api/docs/models/gpt-realtime-translate)、
[GPT-Live-Transcribe](https://developers.openai.com/api/docs/models/gpt-live-transcribe)、
[公式料金表（全モデルを含むMarkdown版）](https://developers.openai.com/api/docs/pricing.md)、
[Realtimeの料金計算](https://developers.openai.com/api/docs/guides/voice-latency-cost)。

## 要件

- Windows 11、64 bit Python **3.12以上**。python.org版の Tcl/Tk と pip を含むインストール。
- Windowsのマイクと、その入力を許可するプライバシー設定。
- `gpt-realtime-translate` と `gpt-live-transcribe` を利用できるOpenAI APIプロジェクトと `OPENAI_API_KEY`。
- `api.openai.com:443` にWSS接続できるネットワーク。
- 通常の実行依存は `numpy`, `sounddevice`, `soxr`, `websockets`。
  Windows版sounddevice wheelにはPortAudioが同梱されます。OpenAI Python SDKは不要です。

## Windowsで環境構築

PowerShellでプロジェクトのディレクトリを開きます。

```powershell
cd C:\workspace\realtime-subtitles
python --version
python -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -e "."
```

`python` がMicrosoft Storeの案内を表示する場合はPython本体が見つかっていません。
Pythonをインストールし、その `python.exe` のフルパスを使用してください。
仮想環境をactivateする必要はありません。

## APIキーと最初の動作確認

同じPowerShellプロセスで設定します。キーはファイルへ保存しません。

```powershell
$env:OPENAI_API_KEY="..."

# Windowsの入力デバイスを確認（★ が既定の入力）
.\.venv-win\Scripts\python.exe -m realtime_subtitles --list-devices

# API接続なしで10秒間マイクを確認。RMS/dBFSが変化するか確認してください。
.\.venv-win\Scripts\python.exe -m realtime_subtitles --probe-mic --seconds 10

# Phase 1: マイク→変換→API→英語/日本語をコンソールで確認
.\.venv-win\Scripts\python.exe -m realtime_subtitles --console --diagnostic

# GUI起動
.\.venv-win\Scripts\python.exe -m realtime_subtitles
```

コンソール版は Ctrl+C で終了。`--seconds 30` で自動停止、`--save session.jsonl` で保存できます。
マイク指定は `--device 9` のように一覧のインデックスを使います。インデックスはPCごとに異なります。
内蔵マイクは `--noise-reduction far_field`（既定）、ヘッドセットは `near_field` を指定します。

起動直後は **STOPPED**。字幕上で右クリック（またはCtrl+,）→設定画面のStartを押すまで録音・API接続は始まりません。
Start後はマイク音声をOpenAIへ送信し、Stopで送信を終了します。

## GUI操作

字幕ウィンドウには接続状態・Micメーター・小さな設定ボタンと字幕を表示します。
**Micバー右端の「設定」、字幕上の右クリック、またはCtrl+,で設定画面**を開きます。以下の操作ボタンは設定画面にあります。

| 操作 | 動作 |
| --- | --- |
| Start / Stop | 接続・字幕開始 / マイク停止とセッション終了。Escでも停止 |
| Microphone | 初回はWindowsの既定入力。停止中に別のマイクへ変更 |
| Refresh | デバイス一覧を再取得。新しいUSB機器が表示されない場合はアプリも再起動 |
| Noise | 内蔵マイク向け `far_field` / 口元マイク向け `near_field` |
| 英語 px / 日本語 px | どちらも8〜64で完全に独立して変更。Enterまたはフォーカス移動で即時反映・自動保存。サイズの大小関係は強制しません |
| normal / bold | 英語・日本語それぞれの太さを独立設定。文字サイズとは独立して即時反映・保存 |
| 透明度 | 0〜70%。右へ動かすほど背景・文字を含むウィンドウ全体が透けます。即時反映・自動保存 |
| Always on Top | 最前面表示の切り替え |
| Clear | 表示字幕だけをクリア。内部の完全な履歴は保持 |
| Save | 保存画面で保存先を指定し、英語・日本語の全delta履歴をUTF-8 JSONLで保存。字幕更新は継続 |
| Diagnostics | デバイス名、入力/出力形式、RMS/dBFS、キュー数、破棄数、接続状態 |

状態・Micバーまたはタイトルバーをドラッグして移動、ウィンドウ端でリサイズできます。
英語・日本語の各字幕上でホイール、スクロールバー、上下キー、PageUp/Downを使うと過去ログを読めます。
過去を読んでいる間は新着でスクロール位置を戻しません。表示される **「↓ 最新」ボタン** で英語・日本語とも追従を再開します。各字幕の **Endキー、または最下部までスクロール** でも再開できます。Start時には両方とも最新へ戻ります。
最新時は表示領域に収まる分だけ表示し、全履歴はスクロールで辿れます。短い無音で字幕は消しません。

**無音後の改行:** 入力PCMのRMSが-45 dBFS未満で1秒以上続き、その後発話が再開したとき、
次に届く英語・日本語の字幕にそれぞれ改行を1つ入れます。無音自体もAPIへ送り続けます。
200msフレーム単位の簡易判定なので、騒音の多い会場では無音を検出しづらい場合があります。
字幕到着時点で改行するため、APIの遅れによって厳密な発話境界とはずれる場合があります。
raw deltaは変更せず、保存履歴では追加改行を `kind: "paragraph"` として区別します。

設定は `%APPDATA%\RealtimeSubtitles\settings.json` に自動保存します。
保存対象はマイク名＋ホストAPI、ウィンドウ位置・サイズ、英日フォントのサイズ・太さ、透明度、最前面、noise reductionです。
存在しなくなった保存マイクはWindows既定入力へ戻ります。
履歴はSaveで明示的に保存してください。既定の保存先は `%USERPROFILE%\RealtimeSubtitles\Transcripts` です。
保存画面のパスを編集して別の場所も指定できます。同名ファイルは上書きせず、エラーを表示します。
Windows標準ファイルダイアログ内で応答停止した実機事例があるため、保存画面は字幕更新を止めないTkの画面を使用します。
アプリ終了後は未保存の字幕全文を復元できません。ただし診断用の文章ログは別途残る場合があります（冒頭の保存仕様参照）。

## API仕様

実装時に確認した公式資料（2026-10-06）:

- [Realtime translation guide](https://developers.openai.com/api/docs/guides/realtime-translation)
- [Translation client events](https://developers.openai.com/api/reference/resources/realtime/translation-client-events)
- [Translation server events](https://developers.openai.com/api/reference/resources/realtime/translation-server-events)

接続先:

```text
wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
```

既定の2接続モードではTranslationの `audio.input.transcription` は `null`、出力言語は `ja`。
英語は `wss://api.openai.com/v1/realtime?intent=transcription` に別接続し、以下を設定します。

```json
{"type":"session.update","session":{"type":"transcription","audio":{"input":{"format":{"type":"audio/pcm","rate":24000},"transcription":{"model":"gpt-live-transcribe","languages":["en"],"delay":"low"},"noise_reduction":{"type":"far_field"},"turn_detection":null}}}}
```

[Realtime transcription公式ガイド](https://developers.openai.com/api/docs/guides/realtime-transcription)に従い、
`input_audio_buffer.append`、`conversation.item.input_audio_transcription.delta` / `.completed` を扱います。
英語側は1秒の無音、または連続20秒ごとに `input_audio_buffer.commit` して認識単位を区切ります。
日本語側のTranslation sessionは維持します。英語用接続の分、API利用料金が追加になります。

24 kHz / mono / signed PCM16 little endianの **4,800 samples = 9,600 bytes = 200 ms** を
両接続へ送信します。SoXRの状態を保持した変換、stereo downmix、clippingは共通で1回だけ行います。
各接続のキューは独立し、片方が遅れても他方の送信を待たせません。
`session.output_audio.delta` は受信して破棄し、再生しません。rawログにも音声payloadは保存しません。

比較用に元の1接続構成を選べます。

```powershell
.\.venv-win\Scripts\python.exe -m realtime_subtitles --source-mode sidecar --source-model gpt-live-transcribe
# 元のモデルと比較する場合のみ
.\.venv-win\Scripts\python.exe -m realtime_subtitles --source-mode sidecar --source-model gpt-realtime-whisper
```

sidecarのclient schemaには `transcription.model` のみを指定し、未定義の `language` は送りません。
このモードでは `session.input_transcript.delta` / `session.output_transcript.delta` を同じsessionから取得します。

停止時はマイクと変換workerを停止し、最後の不完全フレームを無音で200 msに補い、
`session.close` を送信します。最長10秒、残りの字幕と `session.closed` を待ってからWSを閉じます。
停止操作でGUIはブロックしません。応答がなければ末尾未確定の注意を表示します。

## スレッド・遅延・再接続

```text
Windows PortAudio callback
    → bounded raw queue (8 × 50 ms)
    → conversion thread (SoXR → PCM16 → 200 ms)
    → fan-out: independent bounded queues (各3 × 200 ms、同じPCM bytes)
    → Translation WebSocket → JA
    → Transcription WebSocket → EN（接続・再試行も独立）
    → thread-safe transcript history
    → Tk main thread: root.after(50 ms)で表示更新
```

キューが満杯なら古いデータを捨て、さらに古いフレームは送信前にも破棄します。
2秒以上詰まった送信は接続を再作成します。音声の長い遅延蓄積を避けるため、
再接続時はマイク・resampler・音声キューを作り直し、切断中の会話は再送しません。
診断画面に破棄数・入力overflow数が出ます。API推論・ネットワークの遅延自体は保証できません。

状態はSTOPPED / CONNECTING / RUNNING / RECONNECTING / STOPPING / ERROR。
切断時は0.5、1、2、4、8秒（以降8秒）の間隔で最大8回再試行します。
30秒以上安定した接続では連続失敗回数をリセットします。Stopはbackoff待機も中断します。
翻訳・マイクの致命的エラーはERRORで停止します。英語側だけのエラーでは日本語を継続します。
**英語字幕の無更新だけを理由にTranslation sessionを再接続するwatchdogはありません。**
設定画面とDiagnosticsで英語接続状態・エラーも確認できます。

## 履歴フォーマット

1 deltaまたは追加改行 = 1 JSONL行です。時刻はUTC、`elapsed_ms` はAPIが返した値を保持します。
複数deltaが同じ `elapsed_ms` を持っていても削除しません。再接続による時間リセットは
`session_id` で区別し、アプリ内連番 `sequence` は全体で一意です。

```json
{"sequence":0,"time":"2026-10-06T12:34:56+00:00","session_id":"sess_example","event_id":"event_example","elapsed_ms":200,"language":"en","delta":"Hello"}
{"sequence":1,"time":"2026-10-06T12:34:57+00:00","session_id":"sess_example","event_id":"event_example2","elapsed_ms":400,"language":"ja","delta":"こんにちは。"}
```

`language` ごとに `delta` を順番に連結すると完全な字幕になります。
英日deltaは1対1対応とは限りません。文単位の自動alignmentは実装していません。
英語の別接続でAPIが `elapsed_ms` を返さない場合は `null` のまま保存し、時刻を捏造しません。
model変更でENのelapsed_msだけリセットされる場合もあるため、rawログではモデルと接続を区別します。

## WSL2での開発とWindowsでの実行

WSLの `.venv` とWindowsの `.venv-win` を共有しないでください。どちらもgitignore対象です。
WSLでは編集・lint・API/実マイク不要のテストを実施できます。マイク・GUI・WinAPIはWindows側です。

```bash
# WSL側
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .

# ext4上のソースをWindows実行用にコピー（.env、venv、履歴等はコピーしない）
bash scripts/sync-to-windows.sh /mnt/c/workspace/realtime-subtitles

# Windows Pythonを探す
which python.exe
python.exe --version
# 必要な場合だけ探索
find /mnt/c/Users -path '*Python*' -name python.exe 2>/dev/null | head

# 見つかったWindows Python本体を使う
python.exe -m venv 'C:\workspace\realtime-subtitles\.venv-win'
cd /mnt/c/workspace/realtime-subtitles
.venv-win/Scripts/python.exe -m pip install -e '.[dev,packaging]'
.venv-win/Scripts/python.exe -m pytest -q
.venv-win/Scripts/python.exe -m realtime_subtitles --list-devices
.venv-win/Scripts/python.exe scripts/windows-smoke.py
```

`scripts/windows-smoke.py` は既定マイクを3秒ずつ2回開き、PCMフレーム・レベル・解放を検証します。
API接続や録音ファイル作成はしません。Windowsユーザー環境変数に設定済みのキーをWSLから使う場合:

```bash
powershell.exe -NoProfile -ExecutionPolicy Bypass -File \
  'C:\workspace\realtime-subtitles\scripts\run-windows.ps1' --console --diagnostic
```

このスクリプトはプロセスにキーがなければWindowsユーザー環境変数をメモリ上に読み込みます。
キーの表示・ファイル書き出しはしません。通常のPowerShellでは `$env:OPENAI_API_KEY` で十分です。

## テスト

```powershell
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv-win\Scripts\python.exe -m pytest -q
.\.venv-win\Scripts\python.exe -m ruff check .
.\.venv-win\Scripts\python.exe -m ruff format --check .
```

音声変換、200 ms分割、無音、リサンプリング、clipping、字幕連結・履歴・rolling、設定、
状態遷移、再接続・上限、停止中断、設定エラー、秘密情報redaction、Windows Tkを検証します。
ローカルのWebSocket模擬サーバーが実際のクライアントコードを通し、Start→Stop→Startと
`session.close` 後の最終字幕を検証します。テストはAPIキー・実API・実マイク不要です。
Windows TkテストはLinuxではskipします。実マイクは上記smoke scriptで別途確認します。

## Windows exeの生成

**Windows Pythonで**ビルドしてください。Linuxで生成したバイナリはWindows exeになりません。
まずコンソール版で自分のAPIキー・マイクを使った字幕動作を確認してください。

```powershell
.\.venv-win\Scripts\python.exe -m pip install -e ".[packaging]"
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

出力は `dist\RealtimeSubtitles\RealtimeSubtitles.exe`。
同じフォルダーの `_internal` も必要なので、**RealtimeSubtitlesフォルダーごと**配布します。
依存を絞った `onedir` が既定です。単一ファイルが必要な場合は `-OneFile` を指定できます。
`-Console` を追加すると診断用の `RealtimeSubtitlesConsole` を別名で生成します。
APIキーをexeに埋め込みません。exeにも起動元プロセスの `OPENAI_API_KEY` が必要です。

## トラブルシューティング

| 症状 | 確認事項 |
| --- | --- |
| `OPENAI_API_KEY` エラー | 起動するWindowsプロセスにキーを設定。WSLとWindowsの環境変数は別 |
| マイクがない / input deviceエラー | Windows「設定 → プライバシーとセキュリティ → マイク」でアクセスとデスクトップアプリのアクセスを許可。Windows「システム → サウンド」で入力機器を確認 |
| レベルがずっと −120 dBFS | ミュート、デバイス選択、ハードウェアスイッチを確認。`--probe-mic` でAPIと切り離して診断 |
| 同名のマイクが複数ある | MME / DirectSound / WASAPIなどの別ホストAPI。まず既定入力を試し、必要に応じWASAPIの同じ実マイクを選択 |
| PortAudioエラー | 停止後に別ホストAPIを選ぶ。他アプリの排他使用やサンプルレート設定を確認 |
| 接続エラー / HTTP 401・403 | APIキーとモデルへのアクセスを確認。詳細はError欄 |
| HTTP 429 | APIプロジェクトの利用上限・レート制限を確認 |
| RECONNECTINGが続く | ネットワーク・Firewall・WSS 443を確認。v1は直接接続で、HTTPプロキシ自動検出は無効 |
| 音声が途切れる | Diagnosticsでoverflow/drop数を確認。マイクの変更、CPU負荷、ネットワーク状態を確認 |
| Tkがimportできない | Windows PythonのTcl/Tkを有効にしてインストール。`python -m tkinter` で確認 |
| ウィンドウが見えない | アプリ終了後に設定JSONのgeometryを削除。取り外したモニター配置も確認 |
| exeでPortAudio / DLLエラー | `_internal` を含む配布フォルダー全体を確認し、Windowsで再ビルド |
| `credit_balance_exhausted` | APIプロジェクトの利用可能クレジットを確認。ChatGPTの契約とは別のAPI利用枠です |
| exeをアプリケーション制御がブロック | 配布exeは未署名です。組織の許可・署名済み配布が必要な場合があります。Windows Pythonからの起動は別途確認できます |

## 既知の制約と検証状況

- **Windowsの実マイクから実APIへの接続、英日字幕の受信を確認済みです。**
  認識・翻訳の精度や出力タイミングは音声・ネットワーク・API側の状態に依存します。
  発話開始から字幕までの厳密な遅延計測や長時間の耐久試験は実施していません。
- 出力音声は再生しませんがAPIからは届くため、その通信帯域は使います。
- 切断・混雑時は低遅延を優先して音声を破棄するため、その区間の字幕が欠けます。
- 英語と日本語は生成タイミングが異なり、語・文の厳密な対応付けはできません。話者交代は独立した遅延処理で推定します（下記参照）。
- 完全なテキスト履歴はアプリが開いている間メモリに保持します。音声は処理用bufferとして一時的にメモリへ保持しますが、録音ファイルは保存しません。
- 半透明はウィンドウ全体に適用します。click-throughは未実装で、黒いWindows標準タイトルバー・リサイズ枠があります。
- Auto noise reductionは未実装。near/farを明示選択します。
- DPI awarenessを起動時に設定しフォントを拡大します。異なる倍率のモニター間移動では再起動が必要な場合があります。
- 音声ドライバー自体が応答しない場合、そのネイティブ呼び出しをPythonから強制中断はできません。

2026-10-06の実施済み確認:

- WSL Python 3.12.15: **46 passed / 4 skipped**（Windows Tkのみskip）、ruff check / format pass。
- Windows 11 Python 3.14.8: **50 passed**、ruff check pass。ネイティブTkで字幕・設定・エラー表示を検証。
- Windows入力一覧の取得、既定Realtekマイクの44.1 kHz / mono / float32入力。
- 別の入力選択としてWASAPI側のRealtekマイク（48 kHz）も取得・変換を確認。
- 24 kHz PCM16への変換、200 msフレームを3秒間に15個取得。停止・再開を2回確認。
- RMSが変化し、ピークは約 −13 dBFS。probe中のoverflow/dropは0。
- 実APIで `session.updated` が原文文字起こしモデル・far_field・日本語出力を受理することを確認。
- クレジット補充後の実マイク試験で、同一sessionから英語18 delta・日本語88 deltaを受信。
  音声drop/overflowは0。APIエラーはなく `session.closed` を受信して正常終了。
- 実APIの終了処理に約5.9秒かかったため、待機上限を3秒から10秒へ修正。
  マイクはStop直後に停止し、残りの字幕を受け取る間もTkのイベント処理を続けます。
- 修正後、実マイク＋実API＋Windows TkでStart→Stop→Start→Stopを実行。
  2回とも英日字幕を実画面へ表示し、`session.closed` を受信、エラーなしでSTOPPEDに復帰。
  1回目は英語7 / 日本語68 delta、2回目は英語6 / 日本語60 delta。
  GUI callback error・音声drop・overflowはすべて0。
- 2接続版でもWindows実マイク＋実APIでStart→Stop→Start→Stopを確認。
  両接続ともRUNNING→STOPPEDに復帰し、APIエラーなし、終了時キュー0、worker終了。
- PyInstallerによるWindows exe生成成功（フォルダー全体で約59 MiB）。
  このPCではアプリケーション制御ポリシーがexe起動をブロックし、exeの起動確認は未完了。
  Windows Pythonからのアプリ起動は成功しています。

生成exeの実行許可後の確認と、長時間利用・厳密な翻訳遅延の評価が残っています。

### rawイベントによる切り分け（2026-10-06）

UIへ渡す前の `WebSocket.recv` でEN/JAイベントのJSONそのものを保存して確認しました。
時刻はUTCです。同じtranslation session IDを維持したままモデルを変更しています。

| 試験 | raw受信結果 |
| --- | --- |
| Whisper / 20:13:38.817〜20:14:31.549 | EN 0件 / JA 222件、JA elapsed_ms 101600→152400 |
| 同じsessionでLive Transcribeへ変更 | session.updatedで受理。切り替え後約5分でEN449件 / JA811件 |
| Live Transcribeの継続試験 | 20:20:34.211〜20:20:56.201の約21.99秒、ENが空く間にJA26件。JA elapsed_ms 519400→538800 |
| 2接続へ移行 | 日本語session IDを維持し、別の英語sessionからdelta・completedの受信を確認 |

したがって描画処理より前のsource transcription経路で受信が途切れていました。
サーバー内部の具体的な故障原因を証明するものではありません。
2接続移行後は昼休みの遠い雑談が中心となったため、明瞭な英語による長時間の改善度はまだ未検証です。

再現ログを取るには（字幕本文を含むローカルファイルです）:

```powershell
.\.venv-win\Scripts\python.exe -m realtime_subtitles --raw-events diagnostics\events.jsonl
.\.venv-win\Scripts\python.exe scripts\analyze-events.py diagnostics\events.jsonl
```

ログにはUTC受信時刻、monotonic timestamp、stream、session_id、event_id、elapsed_ms、
EN/JAのraw JSONを保存します。認証header・APIキー・音声payloadは保存しません。
書き込みは専用workerに渡し、ログキューのdrop数もDiagnosticsへ表示します。
実測ファイルはWindowsコピーの `diagnostics\sidecar-comparison.jsonl` と `diagnostics\fanout-live.jsonl` にあります。
英語 `.completed` はrawログへ保存します。deltaの末尾への追記は反映し、前の文字列を書き換える最終修正は
`english_final_mismatches` で検知します（前後の空白差は除外）。文字列全体の遡及修正は未実装です。

## 遅延する話者交代の段落表示

既存のEN/JA接続を維持したまま、取得済み24kHz mono PCM16を独立した話者分離sidecarへ分岐します。
マイクは1つだけ開きます。標準の30秒window・5秒overlap（25秒間隔）をWAVにして、
`POST /v1/audio/transcriptions` の `gpt-4o-transcribe-diarize` / `diarized_json` / `chunking_strategy=auto`
へ送ります。追加のRealtime WebSocketは作りません。話者分離のAPI利用分は追加課金です。

- **音声時刻**：PCM累積sample数 / 24000。録音epochごとに0から開始し、Stop/Startや再接続の境界は別IDで管理します。
- **受信時刻**：`received_monotonic_ms` とUTCの `received_at_ms`。発話時刻に変換しません。
- **文字位置**：Realtime本文の `char_start` / `char_end`、確定境界の `en_char_offset`。
- **英語**：`elapsed_ms` の有無に依存しません。diarization segment冒頭最大12語と直前の文脈をtoken sequence similarityで照合します。
  Unicode・大小文字・句読点・apostrophe・um/uh等を正規化し、語数差を許容します。
  同一録音epochの最近の受信範囲に限定し、前後の確定境界の文字位置を越えない単調な対応を保ちます。
  同じフレーズが複数箇所に現れて区別できなければ保留します。
- **日本語**：`elapsed_ms` があれば、その接続へ実際に送信したPCMとの対応表で自前sample clockへ写像します。
  音声キューでdropした分を時間から消してしまわないためです。近くの文頭だけに段落を置き、語の途中には入れません。
  `elapsed_ms` がない場合は、EN/JAの文境界受信順と受信遅延の一貫性が十分ある場合だけ推定し、信頼度不足なら省略します。
- **本文の保護**：Realtimeの `delta` は不変です。`SpeakerBoundary` metadataとTkの独立した段落マーカーを重ねます。
  後続windowで推定が改善すればマーカーだけ移動します。ローカルなA/Bラベルは恒久的な人物IDとして使いません。

最新の字幕は即時表示し、話者段落は後から付きます。API待ち、timeout、429、認証失敗、不正な応答は
sidecar内で処理し、Translationの再接続を起動しません。受信キュー20frame、待機job1件を上限とし、
古いjobは破棄します。音声欠落を含むwindowはリセットし、欠落部分の前後をつなげて誤った時刻を作りません。
認証などの非一時エラーは当該録音epochで停止し、Startで再試行します。一時エラーや不正responseは次の新しいwindowで再試行します。
停止前に30秒に満たない末尾音声はv1では話者分離しません。

交代位置には空行だけを追加します。話者ラベル・番号・見出し・人物ごとの色分けは表示しません。
APIから得る各segmentの話者ラベル・開始/終了時刻・認識文章は境界推定にだけ利用します。
A/Bは30秒window内の仮ラベルで、次のwindowのAが同一人物とは限りません。
信頼度不足の位置には空行を追加しません。英語のみ反映され、日本語側は保留されることもあります。
元のRealtime本文は変更せず、保存時も段落と境界metadataに反映します。

有効/無効・windowサイズ等は `diarization.py` の定数、類似度・confidence・merge閾値は
`speaker_timeline.py` の定数で変更できます。API実装は `Diarizer` interfaceで分離しています。
機能を無効にして起動するには `python -m realtime_subtitles --no-diarization` を使用します。

Diagnosticsにはwindow範囲、処理状態、遅れ、検出/適用境界数、confidence、キューサイズとエラーを表示します。
詳細なsegment文章と境界ログは `%APPDATA%\RealtimeSubtitles\diarization-debug.log` に記録します
（2MB×最大3ファイル）。音声データ・APIキー・Authorization headerは記録しません。

保存は、`.txt` ではEN/JAそれぞれの本文に話者段落を反映します。`.jsonl` では元の `delta` を保持し、
`rendered_delta` / `speaker_breaks` と `kind: "speaker_boundary"` のmetadata行を追加します。
Clearや画面スクロールで非表示になった部分も保存対象です。

保守的なv1のため、短い相づち（5語未満）、重なった発話、繰り返しの多い文章、不鮮明な音声では改行を省略します。
日本語の文対応は英語ほど確実ではなく、confidenceは実測確率ではなくheuristicです。

公式仕様：
[録音音声の話者分離](https://developers.openai.com/api/docs/guides/speech-to-text#speaker-diarization)、
[Transcriptions API](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)。
2026-10-06時点で使用可能なモデルですが、公式の
[廃止予定](https://developers.openai.com/api/docs/deprecations)に `gpt-4o-transcribe-diarize` の2027-02-26提供終了予定があるため、
将来の移行では話者ラベルを返すproviderを改めて確認してください。

話者分離追加時の実機確認では、同じPCMの30秒windowからspeaker segmentを受信し、
英語の段落マーカー追加とraw本文不変・metadata付き保存を確認しました。
英語rawイベントにはelapsed_msがなく、日本語には存在することも確認済みです。
日本語の実発話との段落対応精度は継続評価が必要で、低confidenceの候補は適用しません。
