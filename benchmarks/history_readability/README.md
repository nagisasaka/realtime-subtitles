# 履歴再翻訳の小規模評価

## 5案以上の自然さ比較（2026-10-09）

新しい[比較計画](../../docs/history-naturalness-plan.md)では、固定入力・Luna生成・
Astraによる匿名判定を使う。最初の5案と、結果を踏まえた追加案を区別して保存する。
[7案の結果・採否・残課題](../../docs/history-naturalness-results.md)を参照。

```bash
.venv/bin/python -m benchmarks.history_readability.naturalness \
  --output benchmark_results/history_readability/naturalness_20261008 \
  --source /workspace/jimaku-earnings-benchmark/benchmark_results/earnings22/20261008_arqit_completed \
  --anchors benchmarks/history_readability/naturalness_anchors.json --prepare
.venv/bin/python -m benchmarks.history_readability.naturalness \
  --output benchmark_results/history_readability/naturalness_20261008 --variant baseline
```

続けて `--variant discourse / japanese_syntax / concise / referents / terminology` を
1プロセスずつ実行する。原文・context・prompt・判定基準はmanifestに固定され、
元データの変更はSHA-256照合で拒否する。既定300秒、最大480秒、同時要求2、全試行240回まで。

追加案は `--variant-file <JSON>` で指定する。
JSONには `id`、`parent_manifest_hash`、`config: {title, split, translation}` と選定理由を記録し、
元の5案とmanifestを上書きしない。`--split holdout` は開発評価後に使用する。
`--reverse` で判定の左右を逆にする。`--nonce repeat1 --ids ...` で独立再生成し、
両案を再生成した場合は `--baseline-run baseline.dev.repeat1.json` を指定して比較する。

本番の末尾持ち越しも含めた評価:

```bash
.venv/bin/python -m benchmarks.history_readability.naturalness_tail \
  --output benchmark_results/history_readability/naturalness_20261008 \
  --variant reverse_cohesion \
  --variant-file benchmark_results/history_readability/naturalness_20261008/reverse_cohesion.json \
  --ids 4474955-r6 4474955-r17
```

同一の確定履歴を起点に、両案それぞれ3 unitを順次追加する。
元の英文と再翻訳対象外の段落が維持されることを検査し、最後の履歴を匿名採点する。
JSONL/TXT出力も保存する。Speechmatics、マイク、音声再生は使用しない。
一時的な通信失敗後に再生成する場合は `--nonce recovery2` を付ける。
これは有料API要求を増やす操作であり、残高不足時に自動で繰り返さない。
失敗の機械判読用codeだけを保存し、例外の本文やHTTP headerは保存しない。

Windowsでは `native_preview` が本番の `SubtitleApp` を使って保存済み出力を描画する。
独立した一時設定を使い、`start` 自体を無効化するため、音声/API接続は開始しない。
`--width` は96 DPI相当の幅。実際の幅・DPI・行数・最新順・英日表示順を記録する。
既存の `preview` は2列比較用であり、実際の履歴順の検証には `native_preview` を使う。

```powershell
python -m benchmarks.history_readability.native_preview `
  --manifest benchmark_results/naturalness/manifest.json `
  --run benchmark_results/naturalness/reverse_cohesion.dev.json `
  --events benchmark_results/naturalness/events.jsonl `
  --id 4474955-r9 --width 700 `
  --output benchmark_results/naturalness/native.json `
  --screenshot benchmark_results/naturalness/native.png
```

入力ファイルはWSLの無視対象directoryからWindowsの検証用directoryへコピーする。
`events.jsonl` はmanifestが指す読み取り専用のsourceから取得する。常用アプリへは同期しない。
API試行時間と、音声受信から字幕までの時間は別物。LLM判定は人間評価の代替指標であり、
人間の被験者実験や統計的な性能保証とは扱わない。

以下は以前の3案比較用ツールの説明。

保存済みAgent STTイベントを本番のfinal結合・再構成window plannerへ通し、同じ英文/contextで再翻訳promptを比較する。マイクとSpeechmaticsは起動しない。
現在は本番と同じ英文分割→ID付き一括翻訳の2段階。`--prompt` は英文分割側だけを変更し、翻訳プロンプトはmanifestに固定する。
旧 `run1` は旧方式の評価記録のため再利用せず、新しいmanifestを準備する（旧形式はrunnerが拒否する）。
作業条件・採用基準は [goal仕様](../../docs/history-readability-goal.md) に固定する。

## 準備

既存のプロジェクト `.venv` と `pip install -e '.[dev]'` を使用する。追加依存なし。
Earnings22の取得済み結果directoryを指定する。取得元worktreeへ書き込まない。

```bash
.venv/bin/python -m benchmarks.history_readability.prepare \
  --source /workspace/jimaku-earnings-benchmark/benchmark_results/earnings22/20261008_arqit_completed \
  --output benchmark_results/history_readability/two-stage
```

最初はwindow一覧のみ表示。選定はモデル出力を見る前に固定する。
`anchors.json` は `[meeting_id, 音声開始秒の下限, dev/holdout, 選定理由]` の配列。
`--anchors <file>` を加えると、manifestが未作成の場合だけ入力・context・原文・出典hash・baseline promptを保存する。
重複する原文/contextがdev/holdoutに跨る選定は拒否する。正常EOSのないsessionも拒否する。
再生は全イベントの受信順から行い、partialの話者変化とEOSもassemblerへ反映する。
`prepare` はAPI応答を注入しないため、現在は直前の元unit＋新unitという初期候補を列挙する。
前回の検証済み段落を引き継ぐ連続処理は、下記 `tail_review` で評価する。
以前の3-unit windowのmanifestは保存済みの実験入力としてのみ使用し、現在のplannerが再生成したものとは扱わない。
queue failureや再翻訳到着時間による実画面状態まではこのplanner再現に含まない。

## 実API評価

`OPENAI_API_KEY` を環境変数から読む。WSLで未設定ならWindows User環境変数をcaptureで取得する。キーやheaderは保存しない。

```bash
.venv/bin/python -m benchmarks.history_readability.run \
  --output benchmark_results/history_readability/two-stage --name baseline
.venv/bin/python -m benchmarks.history_readability.run \
  --output benchmark_results/history_readability/two-stage --name candidate1 \
  --prompt benchmark_results/history_readability/two-stage/candidate1.txt \
  --compare benchmark_results/history_readability/two-stage/baseline/dev.json
.venv/bin/python -m benchmarks.history_readability.report \
  benchmark_results/history_readability/two-stage/candidate1/dev.json \
  --baseline benchmark_results/history_readability/two-stage/baseline/dev.json \
  --manifest benchmark_results/history_readability/two-stage/manifest.json
```

- `--split holdout`: 採用候補確定後だけ使用。
- `--ids <id> ...`: 問題例・再現性確認の固定subset。
- `--nonce repeat1`: cacheを使わない再生成。API予算を消費する。
- `--reverse`: judgeのA/B表示順を反転する。
- `--seconds 240`: API作業deadline。上限480秒、各request45秒、同時2件まで。
- 既定の80 attempt上限は同じoutputの `ledger.json` で永続管理。別outputへ移して上限を回避しない。
- 通常は2 request/範囲。SDK retryなし。重大なローカル翻訳検証エラーだけ、固定境界で一括翻訳を最大1回retry。schema失敗・通信失敗は保存して終了。
- request前に予算予約、各結果受信直後にatomic保存。prompt/input/schema/model/nonceのhashでcacheする。
- 再開は同じコマンド。cache済みAPI成功・失敗は再送しない。deadlineで未送信のものだけ続行する。
- 排他lockは異常終了時に残る。記載PIDが生きていないことを確認してから除去する。
- baselineとcandidateのmanifest hashを照合し、無関係な入力の比較を拒否する。

## 結果の解釈

8件程度では統計的な性能保証はしない。段落が短いだけで誤りとは数えない。
厳密な被覆検証・翻訳Validatorと、別requestの固定LLM採点を併用する。
同じLunaによる採点には自己評価・順序・表現の好みの偏りがある。Codexのレビューも人間評価ではない。

構造検証に失敗した出力は本番に適用されない。正常にID対応できた検証棄却訳だけをdraftとして記録し、実画面改善と混同しない。
旧方式の全文訳と部分訳の連結一致を修復してdraftを生成する処理は削除した。
自動指標の段落数は棄却draftを含むため、必ずvalid/structural_failure件数と一緒に読む。
採点が全境界を覆わない場合は `judge_invalid`。欠測を0件の誤りとして扱わない。
ASR-only入力のため以前のJAは存在せず、既存validatorのprevious_translationsは全案で空。
context再翻訳はjudge/Codexで別途確認する。

## テスト・データ

```bash
.venv/bin/python -m pytest tests/test_history_readability.py tests/test_history_reconstruction.py -q
.venv/bin/ruff check benchmarks/history_readability tests/test_history_readability.py
```

`benchmark_results/` はGit対象外。固定manifest、raw応答、使用量、全候補prompt、採点、匿名比較表を保持する。
元音源/GTの配布元は [revdotcom/speech-datasets](https://github.com/revdotcom/speech-datasets/tree/c05ab6fd8b4b627d123c922a22a39e993dd37635/earnings22)、CC-BY-SA 4.0。
source manifestに記録された版・取得元・ライセンスを維持し、録音やコーパスの本文をGitへ追加しない。

## Windowsで保存結果だけを確認

```powershell
python -m benchmarks.history_readability.preview `
  --manifest benchmark_results/history_readability/two-stage/manifest.json `
  --a benchmark_results/history_readability/two-stage/baseline/dev.json `
  --b benchmark_results/history_readability/two-stage/candidate3/dev.json `
  --output benchmark_results/history_readability/two-stage/windows-preview.json
```

同じ幅・DPI・フォントのText widgetで各例を8秒ずつ表示し、実Tkのdisplaylinesを保存する。
原音声の受信速度を再現するプレイヤーではない。棄却draftの比較用表示を含み、本番への適用はしない。
稼働アプリ・設定・マイク・APIには触れない。`preview.py` はWindows専用。

[2026-10-08の採否](../../docs/history-readability-results.md)：最大3候補を検証し、現行維持。

## 異なる実装の保存結果を比較する

`compare_saved` は旧新それぞれのmanifestとrunのhashを検査し、本文/context/speaker/音声時刻/source hashが一致する場合だけ匿名採点する。
local replay時に変化する `raw_sources.received_at` だけを比較から除外する。旧runのmanifest hashを新runへ付け替えない。
旧方式が棄却したdraftを比較する場合は `draft_only` を残し、双方が成功した窓とは別集計する。
生成をやり直さず、同じ評価directoryのledger/cache・排他lockを使用する。

```bash
.venv/bin/python -m benchmarks.history_readability.compare_saved \
  --old-manifest benchmark_results/history_readability/run1/manifest.json \
  --old benchmark_results/history_readability/run1/baseline/dev.json \
  --manifest benchmark_results/history_readability/two_stage_20261008/manifest.json \
  --current benchmark_results/history_readability/two_stage_20261008/current/dev.json \
  --directory benchmark_results/history_readability/two_stage_20261008 \
  --output benchmark_results/history_readability/two_stage_20261008/comparison.json
```

上の例の新manifestは、同じsourceと旧 `run1/anchors.json` を `prepare --anchors` に指定して作成した。
新出力は `run --name current` と `run --name current --split holdout` で取得できる。
生成の再現確認は `run --name repeat --ids 4474955-r5 4474955-r13 --nonce repeat1`、
採点の提示順確認は `compare_saved --ids 4474955-r5 4474955-r13 --reverse` を別outputへ保存する。

[今回の英文先行分割方式の評価](../../docs/history-two-stage-evaluation.md)：構造成功率は向上したが、分断の悪化例が再現。プロンプト変更なし。

## 文法依存を保つ分割プロンプトの評価

[追加の改善評価](../../docs/history-dependency-prompt-evaluation.md) では、直前の2段階版を比較対象に英文分割側だけを変更した。
`dependency_prompt_20261008/manifest.json` は `two_stage_20261008/manifest.json` の同一コピー、
`before/{dev,holdout}.json` は同directoryの `current` 出力のコピーである。
候補は明示的な `--prompt` で指定し、baselineの入力や識別hashは付け替えていない。

```bash
.venv/bin/python -m benchmarks.history_readability.run \
  --output benchmark_results/history_readability/dependency_prompt_20261008 \
  --name candidate3 \
  --prompt benchmark_results/history_readability/dependency_prompt_20261008/candidate3.txt \
  --compare benchmark_results/history_readability/dependency_prompt_20261008/before/dev.json
```

同じコマンドはcacheを使う。追加実測の `--nonce` は同じledgerの残予算を消費する。
既知4窓は未見holdoutではない。調整後の未使用2窓は別の `fresh_manifest.json` に固定し、
同じ `Runner` / ledgerで両promptを評価した。追加request上限を別directoryで回避していない。
全候補と棄却理由・採点の矛盾も保存し、最良出力だけを残す扱いはしていない。


## 次の範囲へ履歴末尾を引き継ぐ評価

```bash
.venv/bin/python -m benchmarks.history_readability.tail_review \
  --output benchmark_results/history_readability/tail_review_20261008 \
  --baseline benchmark_results/history_readability/dependency_prompt_20261008 \
  --name stable_tail
```

前回の検証済み表示を同一の初期状態とし、元ASRから続きの3 TranslationUnitsを取り出す。
比較前はその3 unitsを独立した旧windowとして分割・翻訳する。比較後は本番のplannerと文字位置の置換処理を使い、
新しいunitごとに末尾段落だけを再検討する。英文分割・翻訳promptは両案で同じ。
過去の原文・CONTEXT・両段階のraw response・source位置・保持された前半を保存し、全文tokenの欠落・重複を検査する。
引き継ぐ段落を内部で再分割しない制限も本番と同じ `split_english` を使う。

対象は選定済み6ケースのみ。`--ids` で絞れ、`--nonce repeat1 --name repeat` はcacheなしで再生成する。
全requestは既存Runnerの同一ledger（最大80）、deadline240秒、SDK retryなしで管理する。
元ASRファイルのSHA-256を再実行時にも照合する。前回の評価directoryへ書き込まない。
前半のJAは変更せず、JSONL/TXTへ最終表示と原文・revisionをそれぞれ保存する。

これは順次処理による境界品質の評価であり、マイク・音声再生・Speechmatics・初回ライブ翻訳は実行しない。
比較前は最終3-unit windowだけを生成し、途中の2-unit版は生成しないため、request総数を本番の料金比率と解釈しない。
queue詰まり・ライブ遅延は別の検証が必要。調整中の最初の試行 `comparison.json` も残し、
最終採用した内部境界維持版は `stable_tail.json` として別保存している。
