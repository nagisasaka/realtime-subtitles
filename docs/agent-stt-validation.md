# Agent STT移行検証記録 — 2026-10-07

## 結論

mainの翻訳単位を、低レベル`AddTranscript`からAgent STTの`AddSegment`へ変更した。
`AddSegment` 1件につきTranslationUnitを1件作り、`segment.transcript`全体をLunaへ送る。
独自のsentence buffer、word単位翻訳、Speechmatics Translation、deprecated Voice SDKは使用しない。

同一の6分音源では翻訳対象になる確定イベント数が634件から129件へ減った（79.7%減）。
ただし、これは**翻訳要求の粒度改善であり、英語認識精度の改善を証明する結果ではない**。
Agent側も短いsegmentを返し、固有名詞・略語には両方式で出力差がある。

## 変更前のraw監査

Windows native sounddeviceから入力し、既存Enhanced接続の`AddTranscript`を31件保存した。
SDKがJSONをdecodeした直後のイベント辞書を、本文やresultsを変換せずJSONLへ保存した。
内部履歴は31件。すべて`FinalSegment.en_text == event.metadata.transcript`だった。
旧コードはresultsごとに翻訳を作っていない。resultsはword timestamp/speaker metadataだった。
多くのraw message自体が1〜2語であり、低レベルfinalを翻訳単位にした設計が細切れJAの原因だった。

ローカル証拠ファイル（Git対象外）:

```text
C:\workspace\realtime-subtitles\diagnostics\agent-migration\
  enhanced-raw.jsonl     # 変更前の31件
  enhanced-audit.json    # メッセージと内部履歴の照合
  comparison-6min.wav    # ユーザーの許可後に取得した共通音源
  ab-6min\
    enhanced-raw.jsonl
    agent-raw.jsonl
    enhanced.txt
    agent.txt
    report.json
    status.json
```

APIキー・Authorization・音声のbase64はログに含めない。音声はWAVに分離する。

## 実装

- `agent_stt.py`: 現行`speechmatics-agent-stt 0.2.0`のAgentSttAsyncClient。
  `linden-1 / en / enable_partials=true / diarization=speaker / emit_sentences=true`。
  Agent APIは16kHzを要求するため、既存24kHz PCMを送信workerで連続リサンプリングする。
- `translation_history.py`: TranslationUnitの本文・speaker・timestamp・sequence・翻訳状態。
  word metadataは別レコード。contextは直前最大5 TranslationUnit。接続をまたぐcontextは混ぜない。
- `live_client.py`: `AddPartialSegment`は置換表示、`AddSegment`だけが翻訳トリガー。
  `AddTranscript`はmetadata保存のみ。`EndOfTurn`も翻訳要求にはしない。
- speakerは`segment.speaker`、時刻は公式schemaの`metadata.start_time/end_time`から取得する。
  segment内の文字列は書き換えず、描画時の単位間separatorだけ追加する。
- 4並列・bounded queue・sequence順の日本語表示、EOS、再接続、既存Tk overlayを維持する。
- `audio_recording.py`: 同じPCMを別workerへfan-outし、24kHz mono PCM16 WAVとして自動保存。
  マイクを追加で開かず、30分単位にローテーションする。保存先・欠落sample数・エラーを診断へ表示。

既存OpenAI安定版tag `openai-stable-v1`（`1e95fe5`）と比較branch
`experiment/speechmatics`（`e20f0b3`）には変更を加えない。

## A/B条件と測定結果

- Windows Python、同じ録音360秒を200ms単位・実時間で2接続へ送信。
- 入力録音: Windows default Microphone Array on SoundWire D、44.1kHz mono float32から24kHz PCM16へ変換。
- 録音時のdevice overflow、変換queue/drop、PCM queue/dropはすべて0。
- 音源は講演終盤と終演後の音を含む。ユーザーの終了指示時点で録音を閉じ、その後の比較は録音のみを使用。
- Enhanced: `enhanced`, `max_delay=4`, 24kHz。
- Agent: `linden-1`, `emit_sentences=true`, 同じ24kHz音源を16kHzへ変換。
- 音源SHA256: `41dc07cdf3f40955944b9fed5a15cd94dbeaa43ee14121c777de317c9c6a93ff`。
- A/BそのものではOpenAI翻訳要求を出していない。

| 測定 | Enhanced Realtime | Agent STT |
| --- | ---: | ---: |
| 確定イベント | 634 | 129 |
| 確定テキストの語数（空白split） | 860 | 852 |
| 1確定あたりの語数中央値 | 1 | 5 |
| 1〜2語の確定イベント | 594 | 42 |
| partialイベント | 1,000 | 619 |
| 確定受信時刻−そのイベントend_timeの中央値 | 3.97秒 | 0.67秒 |
| 送信/ACK | 1800 / 1800 | 1801 / 1801 |
| 送信drop / 接続エラー | 0 / 0 | 0 / 0 |

Agentの追加1frameはリサンプラーの末尾flush。両方の入力は同じ360秒である。
遅延はパケット送出を基準にした近似値（200ms粒度）であり、発話開始からの待ち時間や翻訳時間ではない。
両モデルでは確定単位とend_timeも異なるため、この中央値だけで日本語字幕全体の遅延差は結論できない。

## 英語の比較と限界

同じ音源の両英語全文を照合した。次の出力差があった（左/右どちらが正解かは音源の人手照合が必要）:

| Enhanced | Agent |
| --- | --- |
| `fully accessible` | `only accessible` |
| `ops agent` | `Ops engines` |
| `it's a C` | `ADHD` |
| `multi-cloud posture` | `multi-cloud cluster` |
| `Wiz shines` | `it shines` |
| `regular emails` | `regular eval` |

Agentの`ADHD`などは講演の技術的文脈では不自然な候補であり、無条件の精度向上は主張しない。
逆に`regular eval`のようにAgent側が文脈に合う候補もある。
両方式に`$2 million input window`があり、数字・単位が同じでも正確とは限らない。
正解transcriptがないためWERを計算していない。両出力の一致率を精度の代用にもしていない。
原文の誤認識をLunaが推測で訂正する仕様にはしていない。

`emit_sentences=true`でもturn終了や話者交代で短いsegmentになる。
この音源ではAgent 129件中42件が1〜2語。細切れは大幅に減ったが、ゼロになったわけではない。

## 自動テスト

- WSL Python: 67 passed / 5 skipped（Windows GUIのみskip）。
- Windows native Python: 72 passed。
- Ruff lint / format確認。
- 実SDK＋ローカルWebSocketでAgent設定、16kHz送信、EOS、再接続、Stop/Start、翻訳失敗分離を検証。
- 2,000件の低レベルword finalを受けてもTranslationUnit・翻訳要求が0件であることを検証。
- partial置換、AddSegmentの1:1翻訳、context5単位、speaker、timestamp、重複final抑制、本文不変を検証。
- 録音のPCM一致、WAVローテーション、bounded queue欠落の明示、ディスク障害分離を検証。

## Windows GUIと実APIの確認

保存済み音源を入力として、Windows native Tk上でAgent STT→Lunaの実API接続を2回実行した。
実マイクは再開せず、録音ファイルのPCMを既存captureと同じqueueへ供給している。
各回AddSegment 9件、計18 TranslationUnitすべてが日本語翻訳completedになった。
Start→Stop→Startで別session IDを確認。Stop後のqueue・in-flightはいずれも0、接続・翻訳・Tkエラーも0。
英語1,129文字・日本語593文字をGUIへ描画し、JSONL/TXT自動保存を確認した。
証拠は上記diagnostics内の`agent-luna-ui.json`と`agent-luna-ui-units.jsonl`。
翻訳request処理時間の中央値は約1.63秒（ASR確定待ち・queue待ちは含まない）。
この確認は品質を統計評価するものではない。

Windows PyInstaller onedirビルドも成功し、生成exeの`--list-devices`は終了コード0。
通常のWindowsアプリは停止状態で開き、マイク録音・API送信は再開していない。

## 参照仕様

- [Agent STT quickstart / SDK](https://docs.speechmatics.com/speech-to-text/agent-stt/quickstart)
- [Agent STT segmentation](https://docs.speechmatics.com/speech-to-text/agent-stt/segmentation)
- [Agent STT wire schema](https://docs.speechmatics.com/api-ref/agent-stt-websocket)
