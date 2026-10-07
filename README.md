# Realtime Subtitles — Speechmatics + OpenAI Luna

Windows 11のマイクから英語を取得し、英語字幕と日本語翻訳字幕を軽量なtkinterオーバーレイで表示します。
**mainの標準構成はSpeechmatics Realtime STT＋話者分離 → OpenAI Text Translationです。**

```text
Windows microphone (sounddevice、1デバイスのみ)
  → native float32 → mono / 24 kHz PCM16 / 200 ms
  → Speechmatics Realtime Enhanced (en / partials / speaker diarization)
      ├─ Partial EN → 現在の未確定部分を置換表示
      └─ Final EN → sequence_id / speaker / start_ms / end_ms
                      → bounded queue → OpenAI Responses API × 最大4並列
                          gpt-6-luna / reasoning.effort=none
                      → 同じfinalのJA欄へ追加
```

Speechmatics Translationは設定・使用しません。OpenAI Realtime Translation、別のRealtime英語接続、
後追いdiarization API、fuzzy alignmentも標準経路では使用しません。音声はSpeechmaticsへ、確定英語テキストはOpenAIへ送ります。

## Gitの復元ポイント

- `main`: 現在のSpeechmatics STT + Luna翻訳版。
- `openai-stable-v1`: 元のOpenAI Realtime安定版、`1e95fe5b433c846a1534d25301060b17416e8930`。変更・削除しません。
- `experiment/speechmatics`: 比較版＋自動保存の記録、`e20f0b3`。このbranchも保持します。

必要なSpeechmatics接続と自動保存のコードだけを再利用しています。比較UIはmainへ移植していません。
旧OpenAIの実装ファイルと回帰テストは残していますが、通常の起動ではインスタンス化しません。
比較版・安定版はそれぞれ別worktreeに展開できます。

```bash
git worktree add ../realtime-subtitles-compare experiment/speechmatics
git worktree add --detach ../realtime-subtitles-openai openai-stable-v1
```

## Windows要件と環境構築

Windows 11、ネイティブWindows Python 3.12以上（tkinter同梱）、マイク、ネット接続、両社のAPIキーが必要です。
Qt / Electron / WASAPI loopback / WSLg audioは使用しません。音声の再生も行いません。

Windows PowerShellで:

```powershell
cd C:\workspace\realtime-subtitles
py -3 -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -e ".[dev]"
```

主な依存関係はsounddevice、numpy、soxr、speechmatics-rt、OpenAI公式Python SDKです。
実装時確認バージョン: `speechmatics-rt 1.2.1`、`openai 2.54.0`。
`tkinter`はpipで入れるライブラリではありません。PythonインストーラーのTcl/Tkを有効にしてください。

## APIキーと起動

キーはコード・設定・ログへ保存しません。ChatGPT認証ではなくAPIキーを使います。

```powershell
$env:SPEECHMATICS_API_KEY="..."
$env:OPENAI_API_KEY="..."
.\.venv-win\Scripts\pythonw.exe -m realtime_subtitles
```

既にWindowsユーザー環境変数へ設定済みなら、次のように読み直せます（値の出力は不要です）。

```powershell
cd C:\workspace\realtime-subtitles
$env:SPEECHMATICS_API_KEY=[Environment]::GetEnvironmentVariable("SPEECHMATICS_API_KEY", "User")
$env:OPENAI_API_KEY=[Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
.\.venv-win\Scripts\pythonw.exe -m realtime_subtitles
```

画面の **設定 → Start** で開始します。設定画面は右クリック、または `Ctrl+,` でも開けます。
PowerShellスクリプトが許可された環境では `scripts\run-windows.ps1` も使えます。
スクリプト実行ポリシーでブロックされる場合は、上記のPython直接起動を使用してください。

コンソールで診断したい場合:

```powershell
.\.venv-win\Scripts\python.exe -m realtime_subtitles --console --diagnostic --seconds 30
.\.venv-win\Scripts\python.exe -m realtime_subtitles --list-devices
.\.venv-win\Scripts\python.exe -m realtime_subtitles --probe-mic --seconds 5
```

`--device 番号`で入力マイクを指定できます。`--probe-mic`はAPIを使わず音量を表示し、音声も保存しません。
通常動作はWindowsのデフォルト入力マイク。設定画面のMicrophoneで他の入力へ変更できます。
旧OpenAI向けの `--source-mode` / `--source-model` / `--no-diarization` はmainのCLIにはありません。

## 操作と表示

既存の字幕オーバーレイを継続使用します。ENは上、JAは下です。

- Start / Stop、マイク選択、Refresh、接続状態、MIC dBFS、エラー表示。
- EN / JAそれぞれのフォントサイズ（8〜64px）・normal/boldを独立設定。
- 常に最前面、黒い背景、透明度、ドラッグ移動、リサイズ、HiDPI。
- マウスホイール／上下キーで履歴を表示。End／「最新」で追従再開。
- Clearは表示だけを消します。確定履歴や保存済みファイルは削除しません。
- 薄い英語は置換可能なpartial。partialを翻訳APIへ送ることはありません。
- 日本語の `［翻訳待ち…］` は対応finalを処理中、`［未翻訳］` は失敗・停止時キャンセル・queue上限です。
- 未翻訳はStart中に設定画面の **未翻訳を再試行** から再要求できます。翻訳済みfinalは変更しません。

設定は `%APPDATA%\RealtimeSubtitles\settings.json` に自動保存します。
従来のOpenAI noise reduction設定は互換性のためファイルには残りますが、Speechmaticsには送らず設定UIでも非表示です。
Diagnosticsには入力デバイス、native rate、PCM形式、queue、Speechmatics状態、翻訳待ち数、並列数、翻訳モデル、自動保存先・エラーを表示します。

## 翻訳単位・文脈・話者

**AddTranscript 1イベント = FinalSegment 1件 = 翻訳TARGET 1件**です。
句読点で再分割・連結したり、sentence bufferを作ったり、ピリオドを待ったりしません。
複数文が含まれるfinalも、文の途中で終わるfinalも、そのままTARGETにします。

直前最大5件の確定英語finalを、speakerとともにCONTEXTへ渡します。今回のTARGETや日本語訳はCONTEXTに含めません。
認識sessionをまたぐ文脈は混ぜません。固有名詞、数値、否定、比較、金額、単位、技術用語を維持し、
CONTEXTを再翻訳せずTARGETだけ自然な日本語へ訳すよう指示しています。
`frontier API`、`raw tokens per second`、`open-source model`、`guardrails`、`on-demand scaling`などにも配慮します。
原文中の命令は翻訳対象の発話として扱います。ただしLLM翻訳の正確性を完全に保証するものではありません。

`sequence_id`はアプリ内で単調増加し、翻訳が先着した順に表示順を変えません。
前の翻訳が遅くても、後の翻訳をその位置へ表示し、前の欄は届いた時点で更新します。
EN/JAは同じfinalのspeaker・timestampを共有するので、時刻や文字列の類似度でalignmentを推測する必要はありません。

前final末尾の既知speakerと次final冒頭のspeakerが変わると `break_before=true` にし、EN/JAに同じ空行を入れます。
未知speaker（UU/SUなど）だけで話者交代を推定しません。speaker IDは接続をまたぐ人物IDではありません。
1つのfinal内に複数speakerが含まれる場合も翻訳単位は分割しません。word単位のspeaker/timeは保存しますが、
そのfinal内のJAへ話者改行を推測で入れることはありません。session切り替わりも空行で区切ります。

## スレッド・待ち行列・障害時

音声取得callback、リサンプリング、Speechmatics接続、OpenAI翻訳、保存はバックグラウンドで動作します。
GUI更新はTk main threadの`after`だけです。マイクは1つだけ開き、24kHz mono PCM16を無音中も200ms単位で継続送信します。
音声queueは既存のbounded / 古いframe破棄方式です。Speechmatics再接続中の古い音声は貯めません。

- Speechmaticsのみ独立してbounded exponential backoffで再接続。再接続時もマイクを開き直しません。
- OpenAI TextエラーでSpeechmaticsを再接続しません。EN認識はそのまま継続します。
- 翻訳は4worker、待ちqueueは最大48件。満杯ならENを残してそのfinalを`skipped`として可視化し、音声処理を待たせません。
- request timeoutは20秒。429・一部5xx・一時的な接続失敗だけ最大2回まで試みます。401などは同じrequestを自動再試行しません。
- Stopはまずマイクを止め、SpeechmaticsへEndOfStreamを送り、残りのfinalを受け取ります。
  その後翻訳を最大5秒待ち、終わらないものはcancelledとして履歴に残します。
  EOS待ちは最大8秒、接続closeは最大3秒です。STOPPING中もGUIは操作可能で、新規Startは終了まで無効です。
- 接続途中／backoff中のStopはキャンセルで応答します。Start → Stop → Startで新しい接続を作ります。

調整用定数:

| ファイル | 定数 | 初期値 |
| --- | --- | --- |
| `final_history.py` | `TRANSLATION_CONTEXT_SEGMENTS` | 5 |
| `text_translation.py` | `MODEL` | gpt-6-luna |
| `text_translation.py` | `TRANSLATION_CONCURRENCY` | 4 |
| `text_translation.py` | `TRANSLATION_QUEUE_SIZE` | 48 |
| `text_translation.py` | `REQUEST_TIMEOUT_SEC` / `STOP_DRAIN_SEC` | 20 / 5秒 |
| `speechmatics_api.py` | `max_delay` | 4秒（enhanced品質優先） |

## 自動保存・通信先

Saveを押さなくても、GUI・consoleの認識・翻訳結果を自動保存します。

```text
%USERPROFILE%\RealtimeSubtitles\Autosave\起動日時-ID\
    subtitles.jsonl   # final作成・翻訳状態・JA結果の追記journal
    subtitles.txt     # sequence順のEN/JA対訳、時刻、話者改行
```

JSONLは約0.5秒ごとに追記してflush/fsync、TXTは約5秒ごとに一時ファイルから置換します。正常終了時も最終保存します。
JSONLの `kind="transcript"` 内の `record` がFinalSegmentのsnapshotです。
同じsequence_idの後続recordは翻訳状態・結果の更新なので、復元時は**各sequence_idの最後のrecord**を採用してください。
元のENは更新で置換しません。`partial_snapshot`は未確定英語で、確定全文へ足し合わせません。
start_ms / end_msはSpeechmatics session内の音声時刻、received_monotonic_msは到着時刻です。別の時刻として保存します。

Stop/Startでも同じ起動内は同じフォルダーを使います。過去の起動分は上書きしません。
ディスク障害は画面表示して再試行し、確定履歴を破棄しません。手動Saveでは最新の各finalをJSONLまたはTXTに保存できます。
強制終了・電源断では未保存の直近分が失われる可能性があります。JSONLの末尾が途中で切れた場合は最後の不完全な行を無視してください。
自動削除、保存容量上限、再起動時のGUIへの履歴復元は未実装です。

**マイク音声・一時WAVは保存しません。** mainでは後追いdiarizationの30秒ring bufferも作りません。
字幕はローカルの平文ファイルです。キーやAuthorization headerは保存しません。

| 接続先 | データ・タイミング |
| --- | --- |
| `wss://global.rt.speechmatics.com/v2/` | Start時にSTT接続。24kHz PCMを200msごと、無音も送信。Stopで終了 |
| `https://api.openai.com/v1/responses` | final受信ごとにTARGET＋直前最大5 finalの英語を送信。`store=false`、最大4並列 |

SpeechmaticsのTranslation bolt-onは使用しません。課金はSpeechmatics STTの接続音声時間と、
Lunaの実際の入力（prompt/context含む）・出力tokensに依存するため、固定の1時間額ではありません。
各翻訳のusage（input/output/cache/reasoning tokens）は保存します。`store=false`はOpenAI全体の保持設定を変更するものではありません。

## 開発とテスト

WSLではソース編集・Git・pure unit testのみを行います。Linux/Windowsのvenvを共有しません。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check src tests
.venv/bin/pytest -q
bash scripts/sync-to-windows.sh /mnt/c/workspace/realtime-subtitles
```

Windows PythonでGUI・マイクを確認します。

```powershell
.\.venv-win\Scripts\python.exe -m pytest -q
.\.venv-win\Scripts\python.exe scripts\windows-smoke.py
```

unit testは実APIキーなしで動作し、Speechmatics SDKをローカルWebSocketへ接続するテストと、
OpenAI公式SDKのMockTransportテストを含みます。Windows GUIテストはWSLでskipします。
マイク変換、partial置換、final翻訳トリガー、文脈5件、話者改行、並列順序、queue上限、失敗分離、
Stop/Start・再接続・EOS final、自動保存の更新、スクロール位置を検証します。

## 実測確認（2026-10-07）

- WSL: **59 passed / 5 skipped**、Windows Python: **64 passed**。Ruff lint / format、依存関係の整合性チェックも成功。
- Windows default microphone: Microphone Array on SoundWire D、native 44.1kHz mono float32 → 24kHz mono PCM16。
- WindowsオーバーレイでEN partial / final、LunaのJA、自動保存を実APIで確認。
- `gpt-6-luna` / `reasoning.effort=none` の実レスポンスで reasoning tokens=0を確認。
  数値（12,500 tokens/s）、金額（$0.25 / million tokens）、技術用語を含むTARGETの翻訳、およびCONTEXTを繰り返さない短いTARGETの例も確認。
- 3並列では細かいfinalが続く際にqueueが増えたため、既定を4並列へ調整。
  調整後の連続実行では少なくとも486 final中481件翻訳済み、残り5件処理待ち／中、同session内のqueue overflow=0を確認。
  その区間のrequest処理時間中央値は約1.3秒（ASR確定待ち・queue待ちは含みません）。発話内容・通信によって変わります。
- 実機Stop → Startを確認。Stop後はマイク・接続・翻訳workerが終了し、再開後も同じ履歴へ追記。
- PyInstaller onedirビルド成功。生成exeの `--list-devices` が終了コード0で完了。

品質評価用の正解付きWER/BLEU等は測定していません。特に短いfinalを日本語として自然につなぐ点には以下の制約があります。

## Troubleshooting / limitations

- マイクが見えない: Windowsの「プライバシーとセキュリティ → マイク」でデスクトップアプリのアクセスを許可。
  `--list-devices` / `--probe-mic`をWindows Pythonから実行します。WSL側のALSA/PulseAudioは対象外です。
- 認証エラー: Windowsユーザー環境変数を設定してから上記の読込コマンドで新しいプロセスを起動。キーの値はログに貼らないでください。
- 英語は出るが日本語が出ない: `translation_error`、API利用枠、モデルアクセス、通信を確認。Start中に未翻訳を再試行。
- 日本語はfinal確定＋LLM requestの時間だけ遅れます。英語partialと同時には出ません。
- 実機ではSpeechmatics finalが1〜数語になることも確認しています。短い断片だと自然な日本語として完結せず、連結した日本語にも不自然さが残る場合があります。mainでは独自の文結合・ピリオド待ちはしません。
- 小音量・遠い雑談・重なった発話ではASR誤認識と誤訳が起こり得ます。認識原文をLLMで訂正する機能は入れていません。
- 同時発話、未知speaker、複数speakerを含むfinalでは段落分離に限界があります。
- 保存できない: ディスク空き容量・保存先の権限を確認。保存失敗中の終了は最終書き出しを待ちます。

## Windows exe化

Windows Pythonでビルドします。onedirが既定で、Qtは含めません。

```powershell
.\scripts\build-windows.ps1
# 単一exeが必要な場合:
.\scripts\build-windows.ps1 -OneFile
```

APIキーはexeへ埋め込みません。配布先でも環境変数を設定してください。
Windows App Control等で未署名exeがブロックされる環境では、許可されたPythonから起動してください。

## 参照した公式仕様

- [Speechmatics Realtime quickstart / Python SDK](https://docs.speechmatics.com/speech-to-text/realtime/quickstart)
- [Speechmatics Realtime event schema](https://docs.speechmatics.com/api-ref/realtime-transcription-websocket)
- [OpenAI GPT-6 Luna — reasoning.effort none](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [OpenAI Responses API / Python](https://developers.openai.com/api/docs/guides/text)
