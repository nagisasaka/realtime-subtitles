# 履歴再翻訳の小規模評価

保存済みAgent STTイベントを本番のfinal結合・再構成window plannerへ通し、同じ英文/contextで再翻訳promptを比較する。マイクとSpeechmaticsは起動しない。
作業条件・採用基準は [goal仕様](../../docs/history-readability-goal.md) に固定する。

## 準備

既存のプロジェクト `.venv` と `pip install -e '.[dev]'` を使用する。追加依存なし。
Earnings22の取得済み結果directoryを指定する。取得元worktreeへ書き込まない。

```bash
.venv/bin/python -m benchmarks.history_readability.prepare \
  --source /workspace/jimaku-earnings-benchmark/benchmark_results/earnings22/20261008_arqit_completed \
  --output benchmark_results/history_readability/run1
```

最初はwindow一覧のみ表示。選定はモデル出力を見る前に固定する。
`anchors.json` は `[meeting_id, 音声開始秒の下限, dev/holdout, 選定理由]` の配列。
`--anchors <file>` を加えると、manifestが未作成の場合だけ入力・context・原文・出典hash・baseline promptを保存する。
重複する原文/contextがdev/holdoutに跨る選定は拒否する。正常EOSのないsessionも拒否する。
再生は全イベントの受信順から行い、partialの話者変化とEOSもassemblerへ反映する。
最終的な最大windowを採点し、途中の2単位版を同じ母数へ二重計上しない。
queue failureや再翻訳到着時間による実画面状態まではこのplanner再現に含まない。

## 実API評価

`OPENAI_API_KEY` を環境変数から読む。WSLで未設定ならWindows User環境変数をcaptureで取得する。キーやheaderは保存しない。

```bash
.venv/bin/python -m benchmarks.history_readability.run \
  --output benchmark_results/history_readability/run1 --name baseline
.venv/bin/python -m benchmarks.history_readability.run \
  --output benchmark_results/history_readability/run1 --name candidate1 \
  --prompt benchmark_results/history_readability/run1/candidate1.txt \
  --compare benchmark_results/history_readability/run1/baseline/dev.json
.venv/bin/python -m benchmarks.history_readability.report \
  benchmark_results/history_readability/run1/candidate1/dev.json \
  --baseline benchmark_results/history_readability/run1/baseline/dev.json \
  --manifest benchmark_results/history_readability/run1/manifest.json
```

- `--split holdout`: 採用候補確定後だけ使用。
- `--ids <id> ...`: 問題例・再現性確認の固定subset。
- `--nonce repeat1`: cacheを使わない再生成。API予算を消費する。
- `--reverse`: judgeのA/B表示順を反転する。
- `--seconds 240`: API作業deadline。上限480秒、各request45秒、同時2件まで。
- 既定の80 attempt上限は同じoutputの `ledger.json` で永続管理。別outputへ移して上限を回避しない。
- SDK retryなし。重大なローカル翻訳検証エラーだけ最大1回retry。schema失敗・通信失敗は保存して終了。
- request前に予算予約、各結果受信直後にatomic保存。prompt/input/schema/model/nonceのhashでcacheする。
- 再開は同じコマンド。cache済みAPI成功・失敗は再送しない。deadlineで未送信のものだけ続行する。
- 排他lockは異常終了時に残る。記載PIDが生きていないことを確認してから除去する。
- baselineとcandidateのmanifest hashを照合し、無関係な入力の比較を拒否する。

## 結果の解釈

8件程度では統計的な性能保証はしない。段落が短いだけで誤りとは数えない。
厳密な被覆検証・翻訳Validatorと、別requestの固定LLM採点を併用する。
同じLunaによる採点には自己評価・順序・表現の好みの偏りがある。Codexのレビューも人間評価ではない。

構造検証に失敗した出力は本番に適用されない。ただし英文範囲を復元できる場合は、
**棄却draftの診断**として英日部分を保存し、再分割の問題を比較する。これを実画面改善と混同しない。
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
  --manifest benchmark_results/history_readability/run1/manifest.json `
  --a benchmark_results/history_readability/run1/baseline/dev.json `
  --b benchmark_results/history_readability/run1/candidate3/dev.json `
  --output benchmark_results/history_readability/run1/windows-preview.json
```

同じ幅・DPI・フォントのText widgetで各例を8秒ずつ表示し、実Tkのdisplaylinesを保存する。
原音声の受信速度を再現するプレイヤーではない。棄却draftの比較用表示を含み、本番への適用はしない。
稼働アプリ・設定・マイク・APIには触れない。`preview.py` はWindows専用。

[2026-10-08の採否](../../docs/history-readability-results.md)：最大3候補を検証し、現行維持。
