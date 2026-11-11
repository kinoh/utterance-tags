#!/usr/bin/env python3
"""分析結果の Markdown から、会話の進行とともに成長する焦点グラフの HTML を作る

docs/analysis-policy.md の記録形式（スクリプト表と分析表）を読み、
発話ごとの参照先・焦点（節・足場・記述）・操作・構図のスタックを JSON にして HTML テンプレートへ埋め込む
節を円、足場を辺とし、記述は節に積まれる一覧として表示する
記録形式に合わない入力は、補わずにエラーとして止める

使い方:
    python3 tools/growth_graph.py analysis/1tSeRgc28E8.md viz/1tSeRgc28E8.html
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger("growth_graph")

TEMPLATE = Path(__file__).with_name("growth_graph.template.html")
PLACEHOLDER = "__GROWTH_GRAPH_DATA__"

SCRIPT_COLUMNS = ["発話番号", "開始", "話者", "本文"]
ANALYSIS_COLUMNS = [
    "発話番号",
    "参照する先行発話",
    "当該発話の焦点",
    "足場",
    "記述",
    "操作",
    "構図の操作",
    "共有コンテキストの更新",
    "判定上の不確実性",
]
OPERATIONS = {"paraphrase", "switch", "add", "topic_shift", "保留"}
CONTEXT_UPDATES = {"", "同意", "了解", "引受"}
PRECONTEXT = "前提"
PENDING = "保留"
BASE_FRAME = "語り合い"


class FormatError(Exception):
    """入力が記録形式に合わない"""


def read_table(lines, heading, columns):
    """見出し heading の節にある最初の表を、列名をキーにした行の一覧で返す"""
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        raise FormatError(f"見出し「{heading}」がない") from None

    rows = []
    header_seen = False
    for lineno in range(start + 1, len(lines)):
        line = lines[lineno].strip()
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            if header_seen:
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not header_seen:
            if cells != columns:
                raise FormatError(
                    f"{lineno + 1}行目: 「{heading}」の表の列が {columns} ではない: {cells}"
                )
            header_seen = True
            continue
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if len(cells) != len(columns):
            raise FormatError(
                f"{lineno + 1}行目: 列数が {len(columns)} ではない（{len(cells)} 列）"
            )
        rows.append((lineno + 1, dict(zip(columns, cells))))
    if not header_seen:
        raise FormatError(f"「{heading}」の節に表がない")
    return rows


def parse_frame_operation(cell, lineno):
    """構図の操作のセルを (操作, 構図名) にする。空欄は (None, None)"""
    value = cell.strip("`").strip()
    if value == "":
        return None, None
    if value == PENDING:
        return PENDING, None
    if value == "pop":
        return "pop", None
    m = re.fullmatch(r"(push|replace)\s+(\S+)", value)
    if not m:
        raise FormatError(f"{lineno}行目: 構図の操作を読めない: {cell!r}")
    return m.group(1), m.group(2)


def build(md_path):
    lines = md_path.read_text(encoding="utf-8").splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), md_path.stem)

    script = {}
    for lineno, row in read_table(lines, "## スクリプト", SCRIPT_COLUMNS):
        uid = row["発話番号"]
        if uid in script:
            raise FormatError(f"{lineno}行目: スクリプトの発話番号 {uid} が重複している")
        script[uid] = row
    analysis = read_table(lines, "## 分析表", ANALYSIS_COLUMNS)
    log.info("スクリプト %d 行、分析表 %d 行を読んだ", len(script), len(analysis))

    utterances = []
    seen = set()
    stack = []
    nodes = {}  # 節の名前 -> 立てた発話番号
    for lineno, row in analysis:
        uid = row["発話番号"]
        if uid not in script:
            raise FormatError(f"{lineno}行目: 発話番号 {uid} がスクリプトにない")
        if uid in seen:
            raise FormatError(f"{lineno}行目: 分析表の発話番号 {uid} が重複している")

        refs = [r.strip() for r in re.split(r"[・、,]", row["参照する先行発話"]) if r.strip()]
        if not refs:
            raise FormatError(f"{lineno}行目: 参照する先行発話が空欄")
        for ref in refs:
            if ref != PRECONTEXT and ref not in seen:
                raise FormatError(
                    f"{lineno}行目: 参照先 {ref} が、発話 {uid} より前の分析表にない"
                )

        operation = row["操作"].strip("`").strip()
        if operation not in OPERATIONS:
            raise FormatError(f"{lineno}行目: 操作を読めない: {row['操作']!r}")
        context = row["共有コンテキストの更新"]
        if context not in CONTEXT_UPDATES:
            raise FormatError(f"{lineno}行目: 共有コンテキストの更新を読めない: {context!r}")
        focus = row["当該発話の焦点"]
        if focus == "":
            raise FormatError(f"{lineno}行目: 当該発話の焦点が空欄")
        footing = [f.strip() for f in row["足場"].split("・") if f.strip()]
        description = row["記述"]
        if focus == PENDING:
            if footing or description:
                raise FormatError(f"{lineno}行目: 焦点が保留の発話 {uid} に足場か記述がある")
        elif footing:
            if focus in nodes:
                raise FormatError(
                    f"{lineno}行目: 発話 {uid} が立てる節「{focus}」は、発話 {nodes[focus]} で既に立っている"
                )
            for foot in footing:
                if foot != PRECONTEXT and foot not in nodes:
                    raise FormatError(
                        f"{lineno}行目: 発話 {uid} の足場「{foot}」が、それまでに立った節にない"
                    )
            nodes[focus] = uid
        elif focus not in nodes:
            raise FormatError(
                f"{lineno}行目: 発話 {uid} の焦点「{focus}」は立っていない節で、足場も空欄"
            )

        speaker = script[uid]["話者"]
        frame_op, frame = parse_frame_operation(row["構図の操作"], lineno)
        closed = None
        if frame_op in ("pop", "replace"):
            if not stack:
                raise FormatError(
                    f"{lineno}行目: 発話 {uid} の {frame_op} の時点で、積まれた構図がない"
                )
            closed = stack.pop()
        if frame_op in ("push", "replace"):
            stack.append({"frame": frame, "by": speaker, "at": uid})
        if frame_op == PENDING:
            log.warning("発話 %s: 構図の操作が保留のため、スタックは変えずに進めた", uid)

        seen.add(uid)
        utterances.append(
            {
                "id": uid,
                "time": script[uid]["開始"],
                "speaker": speaker,
                "text": script[uid]["本文"],
                "refs": refs,
                "focus": focus,
                "footing": footing,
                "description": description,
                "operation": operation,
                "frameOp": frame_op,
                "frame": frame,
                "closed": closed,
                "stack": list(stack),
                "context": context,
                "uncertainty": row["判定上の不確実性"],
            }
        )

    missing = [uid for uid in script if uid not in seen]
    if missing:
        raise FormatError(f"スクリプトにあって分析表にない発話番号: {', '.join(missing)}")
    if stack:
        log.info(
            "末尾の時点で閉じていない構図: %s",
            "、".join(f"{s['frame']}（{s['at']}）" for s in stack),
        )
    pushes = sum(1 for u in utterances if u["frameOp"] in ("push", "replace"))
    log.info(
        "発話 %d 件、節 %d 個、記述 %d 件、構図を積む操作 %d 件",
        len(utterances),
        len(nodes),
        sum(1 for u in utterances if u["description"]),
        pushes,
    )
    return {
        "title": title,
        "source": md_path.name,
        "baseFrame": BASE_FRAME,
        "precontext": PRECONTEXT,
        "pending": PENDING,
        "utterances": utterances,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("analysis", type=Path, help="分析結果の Markdown")
    parser.add_argument("output", type=Path, help="出力する HTML")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    log.info("入力: %s", args.analysis)
    try:
        data = build(args.analysis)
    except FormatError as e:
        log.error("%s: %s", args.analysis, e)
        return 1

    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        log.error("テンプレート %s に %s がちょうど1か所ない", TEMPLATE, PLACEHOLDER)
        return 1
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(template.replace(PLACEHOLDER, payload), encoding="utf-8")
    log.info("出力: %s（%d バイト）", args.output, args.output.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
