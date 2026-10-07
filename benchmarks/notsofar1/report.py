"""Generate a Japanese report from measured results only; no API calls."""

import argparse
import csv
from pathlib import Path

from .common import load


def pct(x):
    return f"{x * 100:.1f}%" if x is not None else "計算不能"


def seconds(x):
    return f"{x / 1000:.2f}s" if x is not None else "未測定"


def report(output):
    manifest, metrics = load(output / "manifest.json"), load(output / "metrics.json")
    meetings = metrics["meetings"]
    lines = [
        "# NOTSOFAR-1 Linden 1 実測評価",
        "",
        f"実行開始（UTC）: {manifest['execution_date']}",
        "",
        f"dataset: `{manifest['dataset_name']}` / `{manifest['dataset_version']}`",
        f"revision: `{manifest['dataset_revision']}`",
        "",
        f"完了 {len(meetings)} 会議、音声合計 {sum(m['duration_seconds'] for m in meetings) / 60:.2f} 分。",
        f"Agent STT SDK {manifest['sdk_version']}、Linden 1、en、partial有効、speaker diarization、emit_sentences=true。",
        "16kHz mono PCM16、200ms chunk、実時間再生。音声強調・無音削除・ASR比較・OpenAI呼出しなし。",
        "",
        "## 評価条件・精度",
        "",
        "| Meeting | Category | Duration | 非重複bin WER* | tcpWER | tcORC-WER |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for m in meetings:
        lines.append(
            f"| {m['meeting_id']} | {', '.join(m['categories'])} | {m['duration_seconds']:.1f}s | "
            f"{pct(m['ordinary_WER_nonoverlap']['error_rate'])} | {pct(m['tcpWER']['error_rate'])} | {pct(m['tcORC_WER']['error_rate'])} |"
        )
    lines += [
        "",
        "*通常WERはGTで異話者重複がない10秒bin限定。分母が主指標と異なり、bin境界の推定誤差を含む補助値。",
        "主指標は公式MeetEval、collar=5秒、公式NOTSOFAR/CHiME-8正規化を両側へ適用。",
        "GTの発話重複を保持し、S1等と人名を直接一致させず最適割当てで採点。",
        "語の時刻は公式既定のpseudo word timing。GT word_timingは原本に保持。",
        "",
        "| Meeting | GT語数 | ASR語数 | tcp S / D / I | tcORC S / D / I | GT / ASR話者数 | GT重複秒 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for m in meetings:
        tcp, orc = m["tcpWER"], m["tcORC_WER"]
        lines.append(
            f"| {m['meeting_id']} | {m['reference_words']} | {m['hypothesis_words']} | "
            f"{tcp['substitutions']} / {tcp['deletions']} / {tcp['insertions']} | "
            f"{orc['substitutions']} / {orc['deletions']} / {orc['insertions']} | "
            f"{m['gt_speakers']} / {m['asr_speakers']} | {m['gt_overlap_seconds']:.1f} |"
        )
    lines += ["", "補助WERの対象量（主指標とは分母が異なる）:", ""]
    for m in meetings:
        ordinary = m["ordinary_WER_nonoverlap"]
        lines.append(
            f"- {m['meeting_id']}: {ordinary.get('length', 0)}語 / {ordinary['seconds']:.1f}秒。"
        )
    lines += ["", "話者対応の補助確認（tcORCで一致した語だけの対応数。DERではない）:", ""]
    for m in meetings:
        totals = {}
        for row in m.get("text_aligned_speaker_matches", []):
            totals[row["asr_speaker"]] = totals.get(row["asr_speaker"], 0) + row["matched_words"]
        if totals:
            label = max(totals, key=totals.get)
            matches = [
                f"{row['gt_speaker']}={row['matched_words']}語"
                for row in m["text_aligned_speaker_matches"]
                if row["asr_speaker"] == label
            ]
            lines.append(
                f"- {m['meeting_id']}: 最多のASRラベル `{label}` は "
                + ", ".join(matches)
                + " に対応。"
            )
    lines += ["", "各会議の録音・metadata:", ""]
    for m in meetings:
        source = next(x for x in manifest["meetings"] if x["meeting_id"] == m["meeting_id"])
        lines.append(
            f"- {m['meeting_id']}: `{m['device']}`、`{m['hashtags']}`、部屋 `{source['metadata']['Room']}`。"
        )
    lines += [
        "",
        "全件Logitech MeetUpの公開single-channel出力。追加処理なし。元WAVと入力WAVのSHA256・時間長はmanifest参照。",
        "",
        "## 環境別集計",
        "",
        "| Category | 会議数 | GT語数 | tcpWER | tcORC-WER |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, row in metrics["categories"].items():
        lines.append(
            f"| {name} | {row['meetings']} | {row['reference_words']} | {pct(row['tcpWER'])} | {pct(row['tcORC_WER'])} |"
        )
    lines += [
        "",
        "カテゴリは重複する。特に突発雑音の会議はホワイトボード条件も含む。部屋・話者・話題は統制されておらず因果効果を分離できない。",
        "",
        "## 遅延",
        "",
        "| Meeting | Final p50 | p95 | max | Partial末尾 p50 / p95 | 初回Partial p50 / p95* |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for m in meetings:
        f, p, first = (
            m["final_latency_ms"],
            m["partial_audio_end_latency_ms"],
            m["first_partial_from_segment_start_ms"],
        )
        lines.append(
            f"| {m['meeting_id']} | {seconds(f['p50'])} | {seconds(f['p95'])} | {seconds(f['max'])} | "
            f"{seconds(p['p50'])} / {seconds(p['p95'])} | {seconds(first['p50'])} / {seconds(first['p95'])} |"
        )
    lines += [
        "",
        "Final/Partial末尾遅延 = 受信monotonic − 実時間再生開始monotonic − API音声終了時刻。",
        "*初回PartialはAPI segment開始から最初のpartial受信まで。発話進行時間を含み、Tk描画や個別単語の遅延ではない。",
        "",
    ]
    for m in meetings:
        send = m["send_lateness_ms"]
        lines.append(
            f"- {m['meeting_id']}: chunk送信遅れ p95={send['p95']:.2f}ms、max={send['max']:.2f}ms。観測イベント `{m['observed_event_counts']}`。"
        )
    lines += [
        "",
        "## 代表的な差分（tcORC-WER alignment）",
        "",
        "正規化後の単語差分から各会議最大3例を機械的に抽出。時刻は元発話の開始。",
        "話者対応を無視するtcORCのalignmentから抽出。削除との対応ずれを含み得る。全例はerrors.csv。",
        "",
    ]
    errors_path = output / "errors.csv"
    if errors_path.exists():
        with errors_path.open(encoding="utf-8", newline="") as f:
            errors = list(csv.DictReader(f))
        for m in meetings:
            seen = set()
            count = 0
            for e in errors:
                if (
                    e["metric"] != "tcORC-WER"
                    or e["meeting_id"] != m["meeting_id"]
                    or e["type"] != "S"
                    or len(e["gt_word"]) < 5
                ):
                    continue
                key = (e["gt_utterance"], e["asr_utterance"])
                if key in seen:
                    continue
                seen.add(key)
                lines += [
                    f"- {m['meeting_id']} GT {e['gt_utterance_start']}s / ASR {e['asr_utterance_start']}s: `{e['gt_word']}` → `{e['asr_word']}`",
                    f"  - GT: `{e['gt_utterance']}`",
                    f"  - ASR: `{e['asr_utterance']}`",
                ]
                count += 1
                if count == 3:
                    break
    if meetings:
        worst = max(meetings, key=lambda m: m["tcORC_WER"]["error_rate"])
        orc = worst["tcORC_WER"]
        matched = 1 - (orc["substitutions"] + orc["deletions"]) / orc["length"]
        lines += [
            "",
            "## 読める英語の目安と限界",
            "",
            f"話者を無視するtcORC-WERで最も難しかったのは **{worst['meeting_id']} ({', '.join(worst['categories'])})**。",
            f"tcORC-WER={pct(orc['error_rate'])}、参照語のうち置換・削除されず対応した割合は **{pct(matched)}**。",
            f"これは挿入語{orc['insertions']}語を差し引いた『文章理解率』ではなく、正規化後の単語対応率。",
            "WERを単純に100%から引いて人間の理解率とは呼べない。長い発話の骨子が残っても、短い応答や重複発話が欠ければ会話全体の意味を失う。",
            "tcpWERとtcORC-WERの差は話者対応の不安定さを検討する手掛かりだが、DERを直接測定した数値ではない。",
            "タグは会議全体の条件であり、各誤認識時刻の騒音原因は音声の個別確認なしには断定できない。",
            "NOTSOFAR-1は会議室の複数話者会議。講演会場のPA音声やSF Tech WeekでのWERは保証しない。",
            "",
            "## 次に検討すること（今回未実装）",
            "",
            "- errors.csvの削除が多い時刻を録音と照合し、遠距離・小声・重複発話の寄与を確認する。",
            "- 同じ人物の話者IDが増える区間を点検し、字幕の話者交代表示へどれだけ影響するかを調べる。",
            "- この4会議とは独立した実会場音声と人手GTで再評価し、用途への外挿を検証する。",
            "",
            "## 再現",
            "",
            "コマンド・正規化規則・保存構成は `benchmarks/notsofar1/README.md`。",
            "再採点はevaluate.pyとreport.pyのみで可能。再送せずに分析できる。",
            "Microsoft NOTSOFAR (CC BY 4.0)。[公式データ](https://huggingface.co/datasets/microsoft/NOTSOFAR)、",
            "[公式採点](https://github.com/microsoft/NOTSOFAR1-Challenge/blob/6f58e08b008f7530ba4141f0aeb02447c70b6fd7/utils/scoring.py)。",
        ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    report(parser.parse_args().output)
