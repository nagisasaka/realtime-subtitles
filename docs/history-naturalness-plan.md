# 履歴の自然さを改善する比較計画（2026-10-09）

目的は、保存済みEarnings22英語認識結果から生成する英日履歴を、意味を変えずに読みやすくすること。現在の英語先分割→IDごとの一括翻訳、末尾段落の持ち越し、最新が上の画面順を維持する。ASR、音声、原文、自動保存は変更しない。

## 参考にする先行研究・実装

- [Gerber-Morón et al., 2018](https://pmc.ncbi.nlm.nih.gov/articles/PMC7901653/): 文法構造に沿わない字幕分割と読解負荷の関係を調査。英文字幕内の研究であり、日本語履歴への効果は今回の仮説として検証する。
- [Evaluating Subtitle Segmentation, 2022](https://aclanthology.org/2022.lrec-1.328/): 翻訳品質と分割品質を分けて測定する考え方を採用。正解字幕境界がないため、今回の独自評点を論文のSigma指標とは呼ばない。
- [Between Flexibility and Consistency, 2021](https://aclanthology.org/2021.iwslt-1.26/): 英語と訳文の対応と、訳文言語に適した表現を両立する着想。英日を同じ行数に強制しない。
- [When a Good Translation is Wrong in Context, 2019](https://aclanthology.org/P19-1116/): 指示表現・省略・語彙的一貫性を個別に検討する根拠。論文の言語対・モデルでの成果が本アプリにそのまま成立するとは仮定しない。
- [Re-translation versus Streaming, 2020](https://arxiv.org/abs/2004.03643): 再翻訳の品質と更新量のトレードオフ。現在の限定された末尾再翻訳を維持し、履歴全体を書き直さない。
- [Googleのlive caption text stability実装, 2023](https://research.google/blog/modeling-and-improving-text-stability-in-live-captions/): 表示の変動も読解を阻害する。今回、正しい更新を捨てる処理は導入せず、段落内の読順・最新順・原文の範囲を検査する。
- [Netflix日本語字幕ガイド](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215767517-Japanese-Timed-Text-Style-Guide): 簡潔な表現と数値表記の参考。映像字幕の文字数上限や句読点規則を本アプリへ一律適用しない。
- [OpenAIの評価ガイド](https://developers.openai.com/api/docs/guides/evaluation-best-practices): 明示的な基準で比較し、順序・長さバイアスを点検する。モデル評価を人間による実験と混同しない。

## 事前に決める5案

| ID | 変更箇所 | 仮説 |
|---|---|---|
| discourse | 英語分割指示 | 主張と直後の説明など、同じ発話意図をまとめると断片感が減る |
| japanese_syntax | 日本語翻訳指示 | ASRの句点を模倣せず、日本語の修飾・主題・述語を組み直すと理解しやすい |
| concise | 日本語翻訳指示 | 意味を保った短い動詞表現で認知負荷が減る |
| referents | 日本語翻訳指示 | 文脈上明確な指示対象を短い名詞で示すと単独段落でも読める |
| terminology | 日本語翻訳指示 | 財務・技術用語と金額表記をそろえると理解しやすい |

1案ずつ現行指示への追加として試す。数字・意味の補正、要約による情報削除は禁止。短いというだけで改悪判定したり、長いというだけで高評価にしたりしない。

## データ・評価手順

`benchmarks.history_readability.naturalness --prepare`で、原文・文脈・生イベントのSHA-256、候補指示、judgeの基準をmanifestへ固定する。

- Qudian / Arqitの保存済みAgent STT結果から、開発8窓と保留評価4窓を選ぶ。各群間で原文・文脈のsource segmentを共有しない。
- 選定は出力を見る前に行う。金融数値、注意書き、主語・述語の断片、末尾未完結を含める。保留群は今回の5案選定に未使用であり、過去の全実験から未使用とは主張しない。
- 通常の初期2-unit依頼を固定して比較する。採用候補は別途、実際の末尾持ち越しを含む逐次処理でも確認する。
- 生成は現在の`gpt-6-luna` / reasoning `none`。judgeは別モデル`gpt-6-astra` / `low`。匿名A/Bを窓ごとに入れ替え、採用候補で提示順反転・再生成を追加する。
- 自然さ、理解しやすさ（各1〜5）、比較選好、不要境界、過剰結合、重大な意味誤り、英日対応を別々に記録する。judgeが挙げた原文と出力の根拠を確認する。
- 新たな重大誤り・対応誤りが確認された案は採用しない。件数がbaseline以下というだけでは安全とはみなさない。根拠が不確かな判定を確定事実として報告しない。
- 採用には開発群の改善に加え、保留群と提示順の確認が必要。小標本の観測であり、統計的な優越性や人間評価済みを主張しない。

## 実行範囲・負荷

1イテレーションは原則300秒、最大480秒。同時API要求2、SDK自動retryなし、翻訳validation retryは最大1回。manifest単位の全API試行上限240（失敗も計上）。過去の`history-readability-goal.md`にある3案・80試行は以前のタスクの制約であり、今回はユーザーの「最低5案」に合わせた別試行枠とする。

保存済み文字起こしだけを使い、Speechmatics・マイクは起動しない。計測する時間は分割＋履歴翻訳APIの待ち時間であり、音声から字幕までの実時間遅延ではない。

API出力、judge、生データは無視対象の`benchmark_results/history_readability/naturalness_20261008/`に保存する。スクリプト・テスト・計画と結果報告をGit管理する。稼働中のアプリは停止しない。表示確認はWindows側の独立した検証環境を使う。

## 追加の音声分野による確認（ユーザー指定、2026-10-09）

決算説明会だけへの過適合を確認するため、採用候補`reverse_cohesion`と旧baselineの指示・judge基準を固定したまま、次も比較する。

- 既存の6分技術講演WAVと対応するLinden 1のraw log: 製品名、構成要素、細切れの原因説明の3窓。
- NOTSOFAR-1 `dev_set/240825.1_dev1`の遠距離マイク: `MTG_30884`（雑音high・whiteboard）、`MTG_30861`（whiteboard）、`MTG_30862`（overlaps）の各1窓。タグは会議全体のmetadataであり、各抜粋で雑音を測定したという意味ではない。
- 音源SHA-256を元のASR実行manifest/reportと照合する。既存raw ASRを再利用し、原文は補正しない。今回は再翻訳の確認であり、WERやASRの再評価ではない。
- 技術講演の旧ログは受信時刻とEOSを記録していない。完了reportとfinal件数を照合してローカルでflushし、記録のないreceive timeは`null`とする。音声時刻を受信時刻として代用しない。
- 6窓・対象原文70.08秒を出力取得前に固定。技術講演の3番目は初期選定案から224秒以降へ変更したが、API実行前であり、初期案も`pre_api_selection_draft.json`に残す。
- 追加結果を見てpromptを調整しない。追加枠は最大48試行、先行177試行との合計上限225で、元の240試行枠内。生成・評価・順序反転をこの枠で行う。

再現用は`benchmarks.history_readability.cross_audio`。結果は`benchmark_results/history_readability/cross_audio_20261009/`へ独立保存する。
