# 履歴の英文先行分割・一括翻訳への変更

2026-10-08。現在の変更は履歴再構成だけに適用する。ライブのLinden 1認識、2-final Assembler、初回翻訳、画面構成には変更を加えていない。

## 実装

`src/realtime_subtitles/history_reconstruction.py` の処理：

1. `ReconstructionHistory.plan`：既存の同一話者/session、最大3 TranslationUnits・1,800文字制約で対象英文を結合する。
2. `ReconstructionTranslator.translate`：最初に `EnglishSplit` を要求する。LLMが返せるのは末尾token indexだけ。
3. `split_english`：tokenの完全被覆を検査し、元の英文の文字範囲から `EnglishChunk` を作る。アプリがrevision内の連番IDを付け、`set_chunks` で固定する。
4. 次の1回のAPI呼び出しに全 `TARGETS[{id,text}]` と過去の確定英文CONTEXTを渡す。`BatchTranslation` の `{id,ja}` を受け取る。チャンク別のAPI呼び出しや待ちqueueは作らない。
5. `pair_translations`：IDの過不足・重複・空欄を拒否し、応答順に関係なく英日を対応付ける。
6. `ReconstructionWorker._validate`：各チャンクに既存TranslationValidatorを適用する。合計の数字が同じでも、異なるチャンク間で金額・単位が入れ替われば検出対象となる。
7. 全件成功したrevisionだけ既存のUI・TXT出力へ反映する。最新枠はそのまま。元のEN/JAは保持する。

両段階とも `gpt-6-luna / reasoning.effort=none`、Responses APIの `responses.parse(text_format=...)` を使用する。
[公式Structured Outputs説明](https://developers.openai.com/api/docs/guides/structured-outputs) に従い、形式の制約とは別にアプリで被覆・対応・内容を検証する。

翻訳検証に失敗した場合は、同じID・英文・CONTEXTで一括翻訳のみ最大1回再試行する。英文を再分割しない。
既存のworkerの並列数・queue・タイムアウト・終了待ち・通信retryを再利用した。通常の初回翻訳側は、検証処理を `_validate` メソッドへ移しただけで動作を変更していない。
構造不正・検証失敗・キャンセル時は原表示または直前の成功revisionを残す。Stop時の5秒drainを超えた処理も、未完成のまま履歴を差し替えない。

JSONLの `history_revision` に固定した `chunks` と段階別 `decisions` を追加した。各decisionにstage、応答状態、構造化出力、使用量、所要時間、秘匿化したエラー種別を保持する。
最終 `translation.usage` は両段階・retryを含む累計。課金分析では各 `decisions[].usage` を一度ずつ合計する（累計を含む候補snapshotを重ねて合計しない）。
`translation.ja_text` は部分訳を保存用にローカル連結した値で、別途生成した全文訳ではない。

## 削除・整理

- 旧 `ReconstructionDecision` / `ParagraphDecision`、`align_paragraphs`、全文翻訳→英日分割プロンプトを削除。
- 独立した全文日本語 `japanese_translation` と部分訳の連結一致検査を削除。
- 評価runnerの旧生成処理と、連結不一致を修復してdraftを作る処理を削除。
- 評価runnerは新方式へ移行。旧manifestの使用は明示的に拒否し、過去の評価結果を書き換えない。
- モデル生成による英文の訂正・置換や、失敗を隠す疑似的な分割fallbackは追加していない。

## Windowsネイティブ実API・GUI確認

既存アプリとは独立した検証用コピーで、本番のTranslator/Worker/History/Tk UIを実行した。
マイク・Speechmaticsは起動せず、2つの短文例と、取得済みEarnings22の開発用英文2範囲を使用した。ホールドアウトは未使用。

| 入力 | EN/JAペア | API呼び出し | retry | 2段階＋検証の所要時間 |
| --- | ---: | ---: | ---: | ---: |
| `The landscape is` / `changing. Reliability matters.` | 2 | 2 | 0 | 5,690 ms |
| `$2 million` と `25 percent` の2文 | 2 | 2 | 0 | 4,646 ms |
| 取得済み開発英文 `4474955-r5`（462文字） | 4 | 2 | 0 | 4,320 ms |
| 取得済み開発英文 `4474955-r7`（458文字） | 4 | 2 | 0 | 3,414 ms |

4/4 completed、中央値4,483ms。各値はworkerが処理を開始してから終了するまでで、queue待ち・音声認識時間・初回翻訳時間を含まない。旧方式と同時条件の比較ではない。

「The landscape is changing.」は「状況は変化しています。」、金額は「200万ドル」、割合は「25％」と出力された。
取得済み英文のASR由来の断片は、同じ思考の続きとして結合された例がある。一方、「Reliability matters.」のような独立した短文は残った。
入力範囲の最後が `the company's 20.` で終わる例も補完せず残った。窓の端を超えた文の完成はこの方式の保証対象ではない。

全12ペアが日本語→対応英文の順で実Tk履歴に表示されたことを確認した。英語の原本は全ケース不変、JSONL/TXT自動保存も保持。
Tk callback errorは0件。初期配置後のウィンドウ高さ910px・最新ENのY位置97pxは一定。
50ms周期で観測したUI callback間隔は中央値53.7ms、最大167.5ms。これは描画応答の観測であり、音声から字幕までの遅延ではない。
主観的な読みやすさの人間評価や、新たなマイク／実音声ストリーミング評価は行っていない。

実測詳細（Git対象外）：

- WSL：`/workspace/jimaku-test/diagnostics/two-stage-history/result.json`
- Windows：`C:\workspace\realtime-subtitles-ux\diagnostics\two-stage-history\result.json`
- 同階層の `history.jsonl` / `history.txt`、Windows側 `autosave/` に原本・revision・自動保存結果。

## テストと限界

- WSL：237 passed / 16 skipped（主にWindows専用GUIテスト）。
- Windows native：227 passed / 1 skipped（WSLに導入済みの評価用依存関係の差により対象件数が異なる）。
- `ruff check`、変更Pythonファイルのformat check、`compileall`、`git diff --check` 成功。
- Windows全体テストに履歴の差し替え、古い応答の排除、閲覧位置保持、DPI・設定画面を含むGUIテストを含む。

テスト対象は、英文の完全被覆、境界固定後の一括翻訳、ID順序復元、欠落・重複拒否、チャンクごとの数値検査、最大1回retry、通信retryで分割を繰り返さないこと、キャンセル、原文保持、旧revisionへのfallback、自動保存、既存ライブ字幕からの障害分離。

少数例で、翻訳品質の全面的な改善や平均遅延は保証しない。ID対応・数値検証が通っても、意味上の訳抜けや文脈の過剰補完は残り得る。
2段階の直列API待ちが増えるため、混雑時の履歴再構成skipは引き続き発生し得る。次の評価では固定英文で分割の適切さと各英日ペアの忠実性を別々に調べる。
