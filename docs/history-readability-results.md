# 履歴再翻訳の短時間評価結果

2026-10-08、開始commit `15fdc9c`。**3候補を評価したが、採用条件を満たさず現行promptを維持した。**
本番アプリの認識・翻訳・GUIは変更していない。

Earnings22の既存raw ASRを本番の結合/window plannerへ通し、12窓（244.92秒）を固定。
調整用8窓を評価し、確認用4窓は合格候補がなかったため未使用。
生成・judgeともgpt-6-luna/none。ASR・音声の再送なし。

| 案 | 構造検証通過 | 段落数※ | 生成p50 | 読みやすさ 勝/負/同等/欠測 |
|---|---:|---:|---:|---:|
| 現行 | 6/8 | 24 | 4.13秒 | — |
| 第1案 | 7/8 | 27 | 3.29秒 | 2/3/3/0 |
| 第2案 | 8/8 | 17 | 3.75秒 | 3/4/1/0 |
| 第3案 | 8/8 | 18 | 2.92秒 | 3/4/0/1 |

※棄却draftを含む診断値。実画面への適用率・改善率ではない。

第2・第3案は依存句の分断を減らしたが、過剰結合や未完結部分の訳抜けが残った。
自動judgeにも通貨表記の誤判定、境界の見落とし、提示順による勝敗逆転があった。
人間評価は未実施。少数例の自動採点だけで改善が証明されたとはしない。

全628確定イベント中452件が不明話者UUで、本番の結合・再構成対象外だった。
これはpromptだけでは解決しない別要因。今回の既知話者windowを全音声へ一般化しない。

各候補の生成＋採点は約31〜34秒、レビュー込みの記録区間は約3〜4分。
初期準備は約15分で10分制約を超過。その後のrunnerはdeadline・timeout・永続予算を実装した。
72 request attempts（上限80）、入力121,584 / 出力19,840 tokens。API金額は未算出。

Windows nativeで保存結果を64.77秒表示し、同一幅/DPI/フォントの行数を計測、callback error 0。
元の受信間隔や音声end-to-endは再現していない。稼働アプリは操作していない。

- [仕様](history-readability-goal.md)
- [再現コマンド](../benchmarks/history_readability/README.md)
- [詳細レポート（ローカル生成物）](../benchmark_results/history_readability/run1/report.md)
- [匿名比較表（ローカル生成物）](../benchmark_results/history_readability/run1/candidate3/dev.pairs.md)
- [採否と採点監査（ローカル生成物）](../benchmark_results/history_readability/run1/decision.json)

rawコーパス・API結果はGit管理対象外。将来は人が確認した少数例でjudgeを校正し、
英文と全文JAを固定した分割専用実験を行うと、翻訳の変動と分割の改善を切り分けられる。

検証：Linux全体219 passed / 16 skipped、Windows追加テスト13 passed、lint/format成功。
