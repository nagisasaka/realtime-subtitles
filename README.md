# Realtime Subtitles — Speechmatics + OpenAI Luna

Windows 11のマイクから英語を取得し、英語字幕と日本語翻訳字幕を軽量なtkinterオーバーレイで表示します。
**mainの標準構成はSpeechmatics Agent STT＋話者分離 → OpenAI Text Translationです。**

```text
Windows microphone (sounddevice、1デバイスのみ)
  → native float32 → mono / 24 kHz PCM16 / 200 ms
  → 16 kHzへ送信workerで変換（24 kHz PCMは別workerでWAV保存）
  → Speechmatics Agent STT linden-1 (en / partials / speaker / emit_sentences)
      ├─ Partial EN → 現在の未確定部分を置換表示
      └─ AddSegment → bounded Assembler → TranslationUnit / source_segment_ids / speaker / timestamps
                      → bounded queue → OpenAI Responses API × 最大4並列
                          gpt-6-luna / reasoning.effort=none
                      → 同じTranslationUnitのJA欄へ追加
```

Speechmatics Translationは設定・使用しません。OpenAI Realtime Translation、別のRealtime英語接続、
後追いdiarization API、fuzzy alignmentも標準経路では使用しません。音声はSpeechmaticsへ、確定英語テキストはOpenAIへ送ります。

## Gitの復元ポイント

- `main`: 現在のSpeechmatics Agent STT + Luna翻訳版。
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

主な依存関係はsounddevice、numpy、soxr、speechmatics-agent-stt、speechmatics-rt、OpenAI公式Python SDKです。
実装時確認バージョン: `speechmatics-agent-stt 0.2.0`、`speechmatics-rt 1.2.1`、`openai 2.54.0`。
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
- 日本語の `［翻訳待ち…］` は対応TranslationUnitを処理中、`［未翻訳］` は失敗・停止時キャンセル・queue上限です。
- 未翻訳はStart中に設定画面の **未翻訳を再試行** から再要求できます。翻訳済みfinalは変更しません。

設定は `%APPDATA%\RealtimeSubtitles\settings.json` に自動保存します。
従来のOpenAI noise reduction設定は互換性のためファイルには残りますが、Speechmaticsには送らず設定UIでも非表示です。
検証後も重大な異常が残った訳は `［翻訳検証エラー］` と表示し、候補文を正常訳として表示しません。
Diagnosticsには入力デバイス、native rate、PCM形式、queue、Speechmatics状態、翻訳待ち数、並列数、翻訳モデル、自動保存先・エラーを表示します。

## 翻訳単位・文脈・話者

**AddSegmentはraw確定ENとして即座に保存・表示し、TranslationUnitは別に組み立てます。**
完結したsegmentは即時、未完結だけ最初の受信から最大1.5秒保留します。
同session・既知の同speaker・音声gap 0〜600msの続きだけを結合します。
話者/session変更・Stop/EOS/切断・期限でflushし、期限は延長しません。
結合上限は2000文字・音声30秒・20segment。受信した単独segmentは上限超過でも切らずに即送信します。
rawの`segment.transcript`を維持し、結合時は自然な単語間スペースでつなぎ、`segment.speaker`と`metadata.start_time/end_time`を保持します。
`AddPartialSegment`は前のpartialを置換する英語ライブ表示専用です。
`AddTranscript` / word / 低レベルfinalは翻訳のトリガーにしません。
サーバーの`emit_sentences=true`は維持し、不完全なsegmentのみ軽量Assemblerで短時間結合します。
非推奨Realtime Voice SDKは使わず、`speechmatics-agent-stt`を使用します。

直前最大5件のTranslationUnitの確定英語とspeakerをCONTEXTへ渡します。
raw source segment・word metadataは別の履歴であり、context件数には数えません。
`source_segment_ids`と`raw_source_segments`から結合前の確定ENを復元できます。
今回のTARGETや以前の日本語訳はCONTEXTに含めません。
認識sessionをまたぐ文脈は混ぜません。固有名詞、数値、否定、比較、金額、単位、技術用語を維持し、
CONTEXTを再翻訳せずTARGETだけ自然な日本語へ訳すよう指示しています。
`frontier API`、`raw tokens per second`、`open-source model`、`guardrails`、`on-demand scaling`などにも配慮します。
原文中の命令は翻訳対象の発話として扱います。ただしLLM翻訳の正確性を完全に保証するものではありません。

`sequence_id`はアプリ内で単調増加し、翻訳が先着した順に表示順を変えません。
前の翻訳が遅くても、後の翻訳をその位置へ表示し、前の欄は届いた時点で更新します。
JAは結合元ENのspeakerと先頭〜末尾の音声時刻を共有するので、時刻や文字列の類似度でalignmentを推測する必要はありません。

前のTranslationUnitと次のTranslationUnitの既知speakerが変わると`break_before=true`にし、
EN/JAに同じ空行を入れます。未知speaker（UU/SUなど）だけで話者交代を推定しません。
speaker IDは接続をまたぐ人物IDではありません。session切り替わりも空行で区切ります。
Agent STTのspeaker判定自体が誤る場合もあり、人物ごとの恒久IDや固定色は実装していません。

## 翻訳検証と再翻訳

Luna応答後にローカルの`TranslationValidator`を実行します。音声・Speechmatics・EN描画とは別workerです。
数値と既知の通貨・単位を比較し、`$2 million`→`200万ドル`は許容、`200万トークン`への変更は重大な異常とします。
整数・小数・英語のthousand/million/billion/trillion、日本語の百/千/万/億/兆、割合、既知の時間・長さ・重量を扱います。
対応外の換算、漢数字、複合数量、曖昧な`2M`や重量にもなる`pounds`などは警告に留めます。
制御文字、未知の文字種、明確な繰り返し、既に訳した長い文の混入も保守的に検査します。
Kubernetes、MCP、Model Armor、mTLS等の英字技術用語は許容します。意味の正しさを全面的に保証する検査ではありません。

重大なissueだけ、同じTARGET・同じCONTEXTへ検出内容の追加指示を付けて最大1回再翻訳します。
API試行総数はネットワーク再試行も含め最大2回です。再試行後も重大なら`translation_status=validation_failed`。
raw EN、最初の候補、最終候補、各候補の`validation_issues`を保持し、EN表示を続けます。
軽い警告ならJAを表示し、`validation_status=warning`を保存します。ASR原文を自動訂正する機能ではありません。

JSONL自動保存には、`raw_source_segment`と`translation_unit`を区別して記録します。
unitには`translation_unit_id`、`source_segment_ids`、`raw_source_segments`、`en_text`、`ja_text`、
`validation_status/issues`、`candidates`、`retry_count`、各処理の遅延を保存します。
JSONLは更新journalなので、再構築時はunit IDごとの最後の状態を採用してID順に並べます。
TXTと手動保存の最終unit一覧は発話順です。保留中のraw ENも保存対象です。

遅延の定義:

- `assembler_hold_ms`: 結合元の最初のAddSegment受信からunit確定まで。完結segmentは即時、未完結は設定上1500ms。実スレッドの約10msのtick・OS schedulingにより期限直後になる場合があります。
- `translation_latency_ms`: API待機時間の合計。retry分を含み、queue待ち・assembler・検証は含みません。
- `validation_latency_ms`: ローカル検証に要した時間の合計。
- `queue_wait_ms`: unit確定から翻訳worker開始まで。
- `end_to_end_ja_latency_ms`: **最初のAddSegment受信**からJAの確定／失敗判定まで。音声終了からの時間やTk描画完了時刻ではありません。
- `audio_end_to_end_ja_latency_ms`: 最初のPCM capture終了時刻とsample数から推定したsession音声原点＋最後のsourceのend_msを基準とする推定値。デバイス／resampler遅延は未較正で、queue欠落が分かれば利用不可にします。

## 結合・検証の動作確認（2026-10-07）

Windows Python 3.14で既存講演WAVを合計75秒再生し、Start → Stop → Startを実行。
新規マイク録音なし。Linden 1 → Luna → Tk表示で29 unitすべて確定、音声drop 0、Tkエラー0。
この実行では全segmentが完結しており、結合・自然発生の品質retryはいずれも0件でした。
自動テストはWSLで161件成功・Windows専用等6件skip、Windowsでアプリ関連141件成功。
Windowsでは別環境のNOTSOFAR評価テストを除外し、native Tkの保留中EN表示・検証エラー表示も確認しました。
Ruffと両環境の`pip check`も成功しています。

| 計測値 | p50 | p95 | 最大 |
|---|---:|---:|---:|
| assembler保留 | 0ms | 0.6ms | 1ms |
| Luna API | 1188ms | 2543ms | 3678ms |
| ローカル検証 | 0.103ms | 0.485ms | 1.045ms |
| unit queue待ち | 16ms | 32ms | 33ms |
| 最初のAddSegment受信→JA確定 | 1218ms | 2558ms | 3688ms |

保存済みの実際のAddSegmentを使う別の制御テストでは、元ログに受信時刻がないため**400ms間隔を指定**。
同じLuna prompt・同じ直前5 raw ENの固定contextで、分割TARGETと結合TARGETを比較しました。

- `The landscape is` / `changing.` → 1 unit、保留406ms、JA「状況は変化しています。」。
  分割時は「状況は変化していて」「変化しているんです。」となり重複しました。
- `Implement some sort of automated emails so you'll be able` / `to track.` → 1 unit、保留403ms、
  JA「追跡できるように、自動メールを何らかの形で導入してください。」。`emails`を別のASR原文に訂正していません。
- `$2 million`を「200万トークン」にした不正候補を注入する制御テストで`currency_mismatch`を検出。
  再翻訳1回で「200万ドル」に修正され、実APIの追加待機は977msでした。これは自然発生retry率の測定ではありません。

実測ログはWindowsコピーの`diagnostics/translation-quality/`（Git除外）にあります。
29件で検証による拒否はありませんでしたが、広範なfalse positive率や意味の忠実性は未評価です。
自然なsegment分割は同じ録音でも変わるため、この75秒から実運用の結合頻度は推定しません。

## スレッド・待ち行列・障害時

音声取得callback、リサンプリング、Speechmatics接続、OpenAI翻訳、保存はバックグラウンドで動作します。
GUI更新はTk main threadの`after`だけです。マイクは1つだけ開き、24kHz mono PCM16を200ms単位で生成します。
同じPCMを録音へfan-outし、Agent STTの送信workerで16kHzへ連続リサンプリングします。
無音中も送信し、クライアント側VADによる送信停止は行いません。
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
| `translation_history.py` | `TRANSLATION_CONTEXT_SEGMENTS` | 5 |
| `text_translation.py` | `MODEL` | gpt-6-luna |
| `text_translation.py` | `TRANSLATION_CONCURRENCY` | 4 |
| `text_translation.py` | `TRANSLATION_QUEUE_SIZE` | 48 |
| `text_translation.py` | `REQUEST_TIMEOUT_SEC` / `STOP_DRAIN_SEC` | 20 / 5秒 |
| `agent_stt.py` | `AGENT_RATE` / `emit_sentences` | 16000 Hz / true |
| `audio_recording.py` | `ROTATE_SECONDS` | 1800秒（30分） |

## 自動保存・通信先

Saveを押さなくても、GUI・consoleの認識・翻訳結果を自動保存します。

```text
%USERPROFILE%\RealtimeSubtitles\Autosave\起動日時-ID\
    subtitles.jsonl   # TranslationUnit作成・翻訳状態・JA結果の追記journal
    subtitles.txt     # sequence順のEN/JA対訳、時刻、話者改行
```

JSONLは約0.5秒ごとに追記してflush/fsync、TXTは約5秒ごとに一時ファイルから置換します。正常終了時も最終保存します。
JSONLの `kind="transcript"` 内の `record.kind="translation_unit"` がTranslationUnitのsnapshotです。
`record.kind="raw_word_metadata"`は受信した場合のみ保存する別種のmetadataです。翻訳済み字幕として集計しないでください。
同じsequence_idの後続recordは翻訳状態・結果の更新なので、復元時は**各sequence_idの最後のrecord**を採用してください。
元のENは更新で置換しません。`partial_snapshot`は未確定英語で、確定全文へ足し合わせません。
start_ms / end_msはSpeechmatics session内の音声時刻、received_monotonic_msは到着時刻です。別の時刻として保存します。

Stop/Startでも同じ起動内は同じフォルダーを使います。過去の起動分は上書きしません。
ディスク障害は画面表示して再試行し、確定履歴を破棄しません。手動Saveでは最新の各finalをJSONLまたはTXTに保存できます。
強制終了・電源断では未保存の直近分が失われる可能性があります。JSONLの末尾が途中で切れた場合は最後の不完全な行を無視してください。
自動削除、保存容量上限、再起動時のGUIへの履歴復元は未実装です。

**Startするとマイク音声も自動保存し、Stopで録音を終了します。**

```text
%USERPROFILE%\RealtimeSubtitles\Recordings\開始日時-ID\
    audio-0000.wav    # 24kHz / mono / signed PCM16 little endian
    audio-0001.wav    # 30分ごとに次のファイルへ
    gaps.jsonl        # 書き込み遅延による欠落がある場合のsample範囲
```

録音はStartごとに新規フォルダーを作り、既存のファイルを上書きしません。
約173 MB/時（10進表記）です。無音や再接続中の入力も記録します。
録音workerは最大20秒分のqueueを持ち、満杯なら字幕を優先して録音frameを破棄します。
その位置を無音で埋め、`gaps.jsonl`に記録して録音時間の圧縮を避けます。
音声変換より前のデバイスoverflowやcapture自体の欠落は、この録音queueのgapには含まれません。
WAVヘッダーはchunkごとに更新、約1秒ごとにflush/fsync、Stopでcloseします。
強制終了時の直近データ保存は保証しません。録音エラーはUI・Diagnosticsに表示し、字幕処理は継続します。
録音エラー後の自動復旧はせず、次回Startで新しい録音を開始します。
自動削除や容量上限はありません。不要な録音は手動で削除してください。
mainでは後追いdiarizationの30秒ring bufferは作りません。
字幕はローカルの平文ファイルです。キーやAuthorization headerは保存しません。

| 接続先 | データ・タイミング |
| --- | --- |
| `wss://global.rt.speechmatics.com/v2/agent` | Start時にAgent STT接続。16kHz PCMを継続送信、Stopで終了 |
| `https://api.openai.com/v1/responses` | TranslationUnit確定ごとにTARGET＋直前最大5 TranslationUnitの英語を送信。重大な検証異常だけ最大1回再翻訳。`store=false`、最大4並列 |

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

## Enhanced Realtime → Agent STT移行とA/B比較

2026-10-07に旧経路のraw `AddTranscript`を31件採取し、31件の内部FinalSegmentと
`metadata.transcript`がすべて一致することを確認しました。wordごとの分割バグではなく、
サーバーの低レベルfinal自体が1〜2語になるため、翻訳単位をAgent STTのsegmentへ変更しています。
`final_history.py`は旧経路の監査テスト用、`speechmatics_api.py`のEnhanced接続はA/B比較用に残します。
通常UI・consoleから低レベルfinalを翻訳する経路はありません。

同じ録音を実時間で両方へ送る比較コマンド（マイクは開かず、LLM翻訳もしません）:

```powershell
.\.venv-win\Scripts\python.exe scripts\compare-agent-stt.py C:\path\audio-0000.wav --output diagnostics\ab-run-1
```

入力は24kHz mono PCM16 WAV。出力先は新規フォルダーを指定します。
raw JSON、両方の英語全文、音源SHA256、final件数・語数・遅延・drop数を保存します。
比較用のEnhancedはmodel=enhanced / max_delay=4秒、Agentはlinden-1 / emit_sentences=trueです。
正解の書き起こしがない場合、両出力の一致率を認識精度やWERと呼ぶことはできません。
テスト・実測結果は[移行検証記録](docs/agent-stt-validation.md)を参照してください。

## Troubleshooting / limitations

- マイクが見えない: Windowsの「プライバシーとセキュリティ → マイク」でデスクトップアプリのアクセスを許可。
  `--list-devices` / `--probe-mic`をWindows Pythonから実行します。WSL側のALSA/PulseAudioは対象外です。
- 認証エラー: Windowsユーザー環境変数を設定してから上記の読込コマンドで新しいプロセスを起動。キーの値はログに貼らないでください。
- 英語は出るが日本語が出ない: `translation_error`、API利用枠、モデルアクセス、通信を確認。Start中に未翻訳を再試行。
- 日本語はfinal確定＋LLM requestの時間だけ遅れます。英語partialと同時には出ません。
- Agent STTは文末、話者交代、turn終了などでsegmentを確定します。サーバーが長く確定しない場合、日本語もその分遅れます。文の途中でturnが切れた場合は短いsegmentになり得ます。
- 小音量・遠い雑談・重なった発話ではASR誤認識と誤訳が起こり得ます。認識原文をLLMで訂正する機能は入れていません。
- 同時発話、未知speaker、誤ったspeaker attributionでは段落分離に限界があります。
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

- [Speechmatics Agent STT SDK](https://docs.speechmatics.com/speech-to-text/agent-stt/quickstart)
- [Agent STT segmentation](https://docs.speechmatics.com/speech-to-text/agent-stt/segmentation)
- [Agent STT API reference](https://docs.speechmatics.com/api-ref/agent-stt-websocket)

- [Speechmatics Realtime quickstart / Python SDK](https://docs.speechmatics.com/speech-to-text/realtime/quickstart)
- [Speechmatics Realtime event schema](https://docs.speechmatics.com/api-ref/realtime-transcription-websocket)
- [OpenAI GPT-6 Luna — reasoning.effort none](https://developers.openai.com/api/docs/models/gpt-6-luna)
- [OpenAI Responses API / Python](https://developers.openai.com/api/docs/guides/text)
