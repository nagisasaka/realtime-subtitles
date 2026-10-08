# Earnings22による英文先行分割方式の評価

2026-10-08、評価対象 `be1e0ed`。**構造上は安定したが、読みやすさの改善は確認できず、細切れ化の悪化例が再現した。**

前回と同じ取得済みLinden 1英文・contextで比較8窓、追加確認4窓、合計244.92秒相当を評価した。
Speechmaticsへ音声を再送せず、現在のプロンプトを固定。ASR本文・GTの置換やプロンプトチューニングは行っていない。
source hashと入力一致を検証し、旧manifest/resultもそのまま保持した。

| 項目 | 旧方式 | 今回の方式 |
|---|---:|---:|
| 比較8窓の構造・ローカル検証成功 | 6/8 | 8/8 |
| 共通成功6窓の段落数 | 17 | 21 |
| 共通成功6窓の段落語数中央値 | 15 | 13 |
| 共通成功6窓の5語以下段落 | 1 | 2 |
| 比較8窓のAPI所要時間p50 | 4.13秒 | 3.98秒 |
| 比較8窓のAPI所要時間p95 | 8.72秒 | 5.47秒 |

旧所要時間は前回保存値であり、速度改善の証拠とはしない。今回の12窓はすべて成功・retry 0、p50=3.62秒、p95=5.20秒。
これはAPI待ちの合計で、音声認識・queue待ち・UI反映時間を含まない。
旧失敗2窓のdraftは診断用に比較したが、実画面に表示できた改善例としては数えていない。

主な観測：

- 話者紹介の主語と `will start the call` を分断し、日本語も「チュー氏が、」と後続述語に分かれた。
- `Ensuring our asset quality.` と `At a stable level.` を分断し、「安定した水準に保っています。」だけが別段落になった。
- この2例を再生成しても英文境界は同じだった。
- 追加4窓でも `launch its product.` と `Onto the global market with high impact.` などが分かれた。
- `34億元人民元` の重複、`public shareholders`→`個人株主` の意味範囲縮小、`And citizens.` へCONTEXTから「データを守る」を補う例が局所Validatorを通った。

自動judgeの読みやすさは共通成功6窓で新方式1勝5敗。A/Bの提示順を反転した問題例2件でも旧方式が優位だった。
ただし、judgeには「元」を重大通貨誤り扱いする矛盾、境界番号の取り違え、見落としもあった。本文レビューとraw採点は別々に保存した。
Codexのレビューも独立した人間評価ではなく、少数例から一般的な品質を保証しない。

Windows native Tkでも64.48秒表示し、callback error 0。同じ幅・フォントで共通成功6窓のvisual linesはEN30→31、JA18→21となり、段落間隔も増える。
これは保存結果の表示診断で、ライブ音声の再現ではない。

次に試すなら**英文分割プロンプトだけ**を調整し、主語＋述語、動詞＋目的語、修飾句の関係を保つ効果を検証する。
今回は評価のみで、本番の認識・翻訳・UI・プロンプトを変更していない。追加4窓も結果を開いたため、今後調整に利用する場合は未見holdoutとは呼ばない。

- API: 38 attempts、入力44,430 / 出力6,061 tokens。金額未算出。
- 生成バッチ: 比較8窓17.339秒、追加4窓7.400秒、2例再生成3.861秒。準備・採点・レビューは別。
- 検証: 関連unit tests **56 passed**、lint/format/diff check成功。
- [詳細レポート・全英日出力](../benchmark_results/history_readability/two_stage_20261008/report.md)
- [匿名比較表](../benchmark_results/history_readability/two_stage_20261008/comparison.pairs.md)
- [集計JSON](../benchmark_results/history_readability/two_stage_20261008/summary.json)
- [採点監査](../benchmark_results/history_readability/two_stage_20261008/review.json)
- [再現スクリプト](../benchmarks/history_readability/README.md)

元データ：Earnings22（Qudian 4474955 / Arqit 4475604）、[revdotcom/speech-datasets](https://github.com/revdotcom/speech-datasets/tree/c05ab6fd8b4b627d123c922a22a39e993dd37635/earnings22)、CC-BY-SA 4.0。
生成物・コーパス・API応答・鍵はGit管理対象外。
