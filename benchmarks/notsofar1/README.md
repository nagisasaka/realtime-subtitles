# NOTSOFAR-1 / Linden 1 再現ベンチマーク

アプリと同じ `AgentSttClient._new_sdk()` を再利用し、録音済み音声だけをAgent STTへ実時間送信します。
マイク、GUI、翻訳worker、OpenAI APIは起動しません。Enhanced比較もしません。
アプリの録音・接続・UXを変更せず、評価用の独立したスクリプトを追加しています。

## セットアップ（WSL/Linux推奨）

リポジトリrootでPython 3.12以上の専用venvを使用します。MeetEvalはC++ extensionをビルドするため、
LinuxではC++コンパイラが必要です（Ubuntu: `sudo apt install build-essential`）。

```bash
python3 -m venv .venv-benchmark
.venv-benchmark/bin/python -m pip install -e '.[dev]'
.venv-benchmark/bin/python -m pip install -r benchmarks/notsofar1/requirements.txt
.venv-benchmark/bin/python -m benchmarks.notsofar1.download
.venv-benchmark/bin/python -m benchmarks.notsofar1.prepare
```

`SPEECHMATICS_API_KEY` を実行プロセスの環境変数へ設定します。キーをソース、設定ファイル、引数、ログへ入れないでください。
Windowsユーザー環境変数に設定済みでも、WSLの環境変数へ自動的には継承されません。
Windows Pythonからの録音ファイル送信も可能ですが、採点環境はWSLを推奨します。
アプリを起動したり、マイク入力を有効にしたりする必要はありません。

## データと選定

- データ: Microsoft NOTSOFAR、`dev_set/240825.1_dev1`。
- Hugging Face revision: `ba8fd0f034ce185fe4d24f47e53b4b8194795f07` に固定。
- `download.py` はHugging Face REST APIで36会議のディレクトリ・metadataを調査し、4会議のGTと1本ずつのWAVだけ取得。
- MTG_30884: `#TransientNoise=high` と `#TalkNearWhiteboard=Ernie+Carly`。
- MTG_30861: `#TalkNearWhiteboard=Ernie`。
- MTG_30862: `#DebateOverlaps`。
- MTG_30917: `#TurnsNoOverlap`。
- 各会議の `devices.json` にある `is_close_talk=false`、`is_mc=false`、`channels_num=1` の `sc_meetup_0/ch0.wav`。
- `meetup_0` はLogitech MeetUp（主催者のデバイス資料を参照）。マイクまでの正確な距離・SNRはmetadataからは分からないため推定しません。
- 合計1478.248秒、取得WAV約47.3MB。全WAVが元から16kHz/mono/PCM16で、今回のpreparedはバイト単位で同一です。
- 元音源は変更せず保存。別レートの場合のみsoxr HQで16kHz化。チャンネル混合、正規化、無音削除、音声強調なし。
- これはデバイスが出力したsingle-channel録音の評価です。収録デバイス内部の音声処理を取り消した「生マイク信号」とは主張しません。

全データ、GT、生成ログはGit除外対象です。再実行時は既存ファイルを使い、APIへ全splitを送信しません。
公式 `run_inference.py` とベースラインASRモデルは使いません。

## 実行：必ず最初に1会議

```bash
.venv-benchmark/bin/python -m benchmarks.notsofar1.run \
  --output benchmark_results/notsofar1/my_run --meetings MTG_30884
.venv-benchmark/bin/python -m benchmarks.notsofar1.evaluate \
  --output benchmark_results/notsofar1/my_run
```

正常終了・採点を確認した後、同じrunへ残りを追加します。

```bash
.venv-benchmark/bin/python -m benchmarks.notsofar1.run \
  --output benchmark_results/notsofar1/my_run --meetings MTG_30861 MTG_30862 MTG_30917
.venv-benchmark/bin/python -m benchmarks.notsofar1.evaluate \
  --output benchmark_results/notsofar1/my_run
.venv-benchmark/bin/python -m benchmarks.notsofar1.report \
  --output benchmark_results/notsofar1/my_run
```

同じrunへの同じ会議の再送は禁止。接続エラーの自動retry/reconnectもありません。
1 runは最大5会議・1800秒。失敗した会議も予約音声時間に算入します。
意図的な再評価は新しいoutputディレクトリで行います（新たにAPI利用料が発生）。
中断されたrunを黙って正常扱いせず、`manifest.json` のattempt statusがcompletedでEOS確認済みの会議だけ採点します。

## ストリーミングと遅延

- 3200 samples = 200msごとに送信。最初のchunkも再生開始から200ms後に送信。
- `monotonic` の絶対締切 `playback_epoch + audio_end` を使い、将来の音声を先送りしません。
- 遅れたchunkはcatch-upできますが、累積的に200msのsleepを足す方式にはしません。
- 送信遅れ・送信開始/完了・音声区間を `chunks.jsonl` に保存。データをdropしません。
- 最後に実際のbinary frame数で `EndOfStream` を送り、最大20秒 `EndOfTranscript` を待ちます。最後の確定segmentも保存します。
- Finalization latency = `received_monotonic - playback_epoch - AddSegment.audio_end`。
- Partial末尾遅延も同じ定義。最初のpartialは `(speaker, audio_start)` ごとの最初の受信時刻からsegment開始を引きます。
  これはsegment内で発話が続く時間を含み、単語の初回認識速度や実際のTk描画遅延ではありません。
- タイムスタンプ未提供ならnull。受信時刻を音声時刻に代入しません。
- APIから実際に届いたイベントだけ記録。イベント種類によっては一度も届かない可能性があります。

## 評価方法

主指標はMeetEval **tcpWER**（最適な話者割当てを含む）と **tcORC-WER**（話者対応に依存しない補助指標）。
NOTSOFAR公式scoringと同じcollar=5秒、utterance単位のGT text、重複を保持したSegLSTを使用。
MeetEval既定のpseudo word timing（GT: character-based、hyp: character-based points）を使用します。
GTの実際の `word_timing` は保存しますが、公式utterance評価と合わせるため主スコアの時刻へ混在させません。
字幕本文はAddSegment全文。低レベルwordやpartialを混ぜません。話者IDなしの場合は明示的な `__unknown__` streamとして評価します。

正規化は公式NOTSOFAR/CHiME-8コードをrevision固定・MITライセンス付きで `vendor/chime8` に保持。
GT/hypとも**同一**の関数を適用します。大文字小文字、句読点、略語、英米綴り、数値の英単語化、フィラー除外を含みます。
`<ST/> <FILL/> <UNKNOWN/> <PName/> <BA/> <PAUSE/> <ISSUE/>` などはタグそのものを取り除き、
残った発話本文は採点します。タグだけの発話は参照語0。UNKNOWN位置を正答扱いするwildcardにはしません。
括弧注記・角括弧注記も公式関数の規則に従います。数値変換の未対応ケースを独自に補正しません。

通常WERは補助として、GTに異話者の時間重複がない10秒binだけで計算。
正規化済みutteranceの語を均等時間の中心点でbinへ配置するため、bin境界誤差があり、主指標との直接比較には使えません。
全話者を単に時刻順に並べた全会議WERは主指標にしません。

`metrics.csv` のS/D/Iは**tcpWERの内訳**、`WER` は上記非重複binのみ。分母が違うので混同しないでください。
JSONには各スコアの分母・S/D/I・話者割当て・通常WERの対象binを保存します。
`errors.csv` はtcpWERとtcORC-WERの最適割当て・時間制約付きalignmentによる実例（metric列で区別）。
語の時刻はpseudo timingで、元のGT/hyp utterance開始時刻と全文も併記します。
tcpWER側には話者取り違えによるものも含まれます。綴りだけのASR誤り頻度とは同一視しません。
レポート例とsubstitutions.jsonはtcORC-WER側を使用します。

カテゴリ集計は参照語数で重み付けします。1会議が複数カテゴリに入るためカテゴリ間の件数は排他的ではありません。
機種は揃えましたが、話者・部屋・話題は統制されていません。タグだけから騒音の因果効果を断定しません。
NOTSOFAR会議室での4会議の結果から、SF Tech WeekのPA音声でのWERは保証できません。

## 出力

- `testdata/external/notsofar1/`: recordings / prepared / references / metadata / catalog / selection。
- `manifest.json`: dataset commit、選定根拠、デバイス、SHA256、WAV仕様、ASR設定、SDK版、日時、実行状態。
- `events.jsonl`: raw server eventsと受信時刻、session/speaker/audio metadata。
- `transcript.jsonl`:確定AddSegmentのみ。
- `chunks.jsonl`:送信タイミング。
- `*-reference-normalized.json`, `*-hypothesis-normalized.json`:原文を添えた採点入力。
- `metrics.json`, `metrics.csv`, `errors.csv`, `substitutions.json`, `report.md`。

```bash
.venv-benchmark/bin/python -m pytest tests/test_notsofar_benchmark.py -q
```

テストは手書きの合成fixtureだけで動き、API・データ取得・音声デバイスは不要です。

## 公式資料

- [データとコード](https://github.com/microsoft/NOTSOFAR1-Challenge)
- [Hugging Face dataset](https://huggingface.co/datasets/microsoft/NOTSOFAR)
- [主催者のデバイス・環境資料](https://www.chimechallenge.org/workshops/chime2024/papers/NOTSOFAR_alon_slides.pdf)
- [公式採点実装](https://github.com/microsoft/NOTSOFAR1-Challenge/blob/6f58e08b008f7530ba4141f0aeb02447c70b6fd7/utils/scoring.py)
- [MeetEval](https://github.com/fgnt/meeteval)
- [Speechmatics Agent STT](https://docs.speechmatics.com/speech-to-text/agent-stt/quickstart)

Dataset: Microsoft NOTSOFAR, CC BY 4.0。配布時は原データのライセンス・帰属を維持してください。
