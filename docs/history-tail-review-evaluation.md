# 履歴末尾の引き継ぎとEarnings22再評価

2026-10-08、変更前 `2205f51`。改善案1「前回の最後の段落を次の確定英文と再検討」だけを実装した。
英語分割・日本語翻訳のプロンプト、モデル、Validator、ライブ字幕の表示条件は変更していない。

## 実装

- 新しいTranslationUnitが届くと、直前の表示段落＋新unitを専用workerへ予約する。時間による保留なし。
- queue待ち中に前の処理が完了した場合にも対応するため、対象はworkerの実行開始時に確定する。
- 引き継ぐのは履歴再構成の最後の1段落のみ。元の最大3 unitsまで＋新1 unit、合計1,800文字まで。
  上限・話者交代・未知話者・session変更・Clearを越えず、skip/失敗時は元の表示を保持する。
- 再構成結果がない場合は直前の元unitを使用する。失敗したrevisionを次の入力には使わない。
- 原文のunit ID＋文字位置で範囲を管理し、既存段落を丸ごと置換する。日本語を文字数比率で切り取らない。
  末尾より前の英日ペアは再翻訳・再描画しない。同じunitに属する前半と末尾も別管理できる。
- UIとTXTは同じ非重複段落を表示・保存し、JSONLには元unitと全revisionを残す。
  `first_unit_offset` / `parent_revision_id` / `applied` とrawモデル判断・実適用境界を保存する。
- 古い遅延応答は新しい範囲を上書きせず、最新partialは従来どおり即時表示する。

初回評価では再検討により、既にまとまっていた英文が再び分かれる例があった。
そのため、**引き継いだ再構成済み段落の内部へ新境界を作らない**制限を追加した。
段落と後続を結合する判断、新しい英文内部の分割はLLMに任せる。
raw分割結果の完全被覆を検証してから制限を適用するため、不正なAPI応答を修復して成功扱いにはしない。
初回結果も `comparison.json` に保存し、採用結果は `stable_tail.json` と分離した。

## 再評価

Earnings22の前回使用した6範囲に、それぞれ次の3 TranslationUnitsを加えた。
元の範囲を含め220.92秒、追加部分102.08秒。分断4ケース＋独立した話題2ケースを出力生成前に固定した。
両案の初期表示は前回の検証済み結果と同一で、元ASR・CONTEXTの取得元・両プロンプトを固定。
Speechmaticsへ音声を再送せず、履歴再構成だけを実APIで実行した。

| 対象 | 比較前→後の段落数 | 境界の結果 |
|---|---:|---|
| Qudian r5：話者紹介→注意事項 | 8→8 | 独立した話題として分離を維持 |
| Qudian r13：戦略→取引量 | 2→2 | 独立した話題として分離を維持 |
| Qudian r17：融資残高→減少率 | 3→2 | 主語と述語・数値がつながった |
| Qudian r21：With our→Key business | 5→3 | 単独の断片が後続につながった |
| Arqit r1：dial in→To the call | 7→5 | 電話参加の案内がつながった |
| Arqit r5：does not undertake→To update | 6→5 | 否定する義務の内容がつながった |

対象の分断**4/4**が結合され、独立した話題**2/2**は境界を維持した。
合計**31→25段落**、5語以下**3→0**。短さだけを採否条件にはしていない。
新方式の18処理はすべて構造・ローカル翻訳検証を通過し、retry 0。
原文tokenの欠落・重複なし、保持対象の前半EN/JA不変を全ケースで検査した。

例：

```text
以前：The company does not undertake. / To publicly update or revise ...
今回：The company does not undertake. To publicly update or revise ...
JA：当社は、このウェブキャスト終了後に、いかなる将来予想に関する記述についても
    公に更新または修正する義務を負いません。
```

Qudian r21・Arqit r5をcacheなしで再生成し、最終的な英文境界は同じだった。
日本語表現には揺れがある。別の実データ3箇所ではspeaker変更を越えずskipすることも確認した。

## 遅延と負荷

新方式の2段階API待ち合計は p50 **2.88秒**、p95 **4.06秒**、最大 **4.43秒**。
同じrequestの保存済み実測値をcacheから再利用したものを含む。queue待ち・ASR・window成立・UI反映は含まず、
音声入力からのライブ遅延ではない。最初の比較処理61.09秒、保護追加後4.80秒（ほぼcache）、独立再生成25.86秒。

新unitごとに見直すため、同じ話者が続いて成功する場合、**履歴側のAPI呼び出し頻度は約1.5倍**になる
（従来3 unitsにつき最大2見直し→新方式3見直し、各2 API呼び出し）。初回翻訳は変更していない。
入力文字量も変わるため料金が1.5倍になるとは限らない。1並列・待ちqueue最大2件の制限は維持した。

本試験の比較前は旧windowの最終3-unit版だけを生成し、途中の2-unit版は生成していない。
試験のrequest件数比を本番料金比に使わない。今回は計64 API attempts（上限80）、入力48,785 / 出力2,975 tokens。

## 検証と限界

- WSL：255 passed / 17 skipped。Ruff lint/format・diff check成功。
- Windows：244 passed / 2 skipped、56.46秒、警告なし。2件は複数モニターが必要なテスト。
  native UIでは同一widgetの維持、読み位置、
  遅延した元訳の上書き防止、保持前半と末尾の非連続な画面範囲の置換も検査。
- 本番Tk UIへ実API結果を24回反映。30.35秒、168 DPI、callback error 0。
  ウィンドウ高さ910px・最新英文欄の位置97pxは一定で、表示される原文範囲が内部historyと全フレーム一致した。
  これは保存済み結果の再生で、マイクや音声再生を伴うテストではない。
- 並行するライブASR・初回翻訳下でのqueue飽和率は未測定。細かい発話が続く場合はskipが増える可能性がある。
- 引き継ぐ段落より前の誤分割や、元ASRの誤認識・speaker判定は修正しない。
- まだ先の続きがない `We had seven.` は未完結のまま。訳語の揺れや日本語の反復も残る。
- 小標本の境界比較であり、独立した人手評価や一般的な翻訳品質保証ではない。

## 成果物

- [全英日比較・逐次TARGET/CONTEXT](../benchmark_results/history_readability/tail_review_20261008/report.md)
- [集計JSON](../benchmark_results/history_readability/tail_review_20261008/summary.json)
- [再現方法](../benchmarks/history_readability/README.md)
- 実装：`history_reconstruction.py`、`translation_history.py`、`ui.py`。
- データ・raw API応答・JSONL/TXT・Windows表示記録は `benchmark_results/history_readability/tail_review_20261008/`。

旧結果は保持し、コーパス・生成物・秘密鍵はcommitしない。
元データ：[Earnings22 / revdotcom](https://github.com/revdotcom/speech-datasets/tree/c05ab6fd8b4b627d123c922a22a39e993dd37635/earnings22)、CC-BY-SA 4.0。
