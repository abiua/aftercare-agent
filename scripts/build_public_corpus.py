#!/usr/bin/env python3
"""Build or byte-verify the staged public expression corpus from fixed source files."""

import argparse
import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter
from itertools import combinations
from pathlib import Path

CORPUS = Path(__file__).resolve().parents[1] / "data" / "corpus"
INTENTS = {"damaged_delivery": "damaged", "wrong_item": "wrong_item", "refund_status": "refund_status"}
SPLIT_COUNTS = {"dev": 20, "validation": 10, "holdout": 10}
NEAR_THRESHOLD = 0.68
FAMILIES = {
    "damage-received-product": "damaged",
    "damage-parcel": "damaged",
    "damage-in-transit": "damaged",
    "wrong-delivery-mismatch": "wrong_item",
    "wrong-direct-report": "wrong_item",
    "wrong-attribute-mismatch": "wrong_item",
    "refund-check": "refund_status",
    "refund-information": "refund_status",
    "refund-track": "refund_status",
}


FAMILY_CHANGE = {
    "damage-received-product": "保留商品受损与件数线索，将报备方式改写为客服用语。",
    "damage-parcel": "保留受损对象为包裹或快件，未补写包裹内商品是否损坏。",
    "damage-in-transit": "保留原文明确提到的运输途中受损，保留压坏等具体描述。",
    "wrong-delivery-mismatch": "保留收到商品不符及涉及件数，未补写规格、责任或补救方案。",
    "wrong-direct-report": "保留直接报备错发商品的请求，未补写订单或商品资料。",
    "wrong-attribute-mismatch": "保留颜色、尺码或型号不符，未补写正确规格。",
    "refund-check": "保留查询已有退款状态的意图，未改写为发起退款。",
    "refund-information": "保留询问已有退款详情或查找入口的表述，未补写退款状态。",
    "refund-track": "保留跟进已有退款的表述，未补写到账时限。",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def read_sources(source_dir, manifest):
    archives, checks = {}, []
    for source in manifest["sources"]:
        path = source["path"]
        raw = (source_dir / path).read_bytes()
        actual = sha(raw)
        require(actual == source["sha256"] and len(raw) == source["size_bytes"], f"Source mismatch: {path}")
        if "archived_as" in source:
            archives[source["archived_as"]] = raw
        checks.append({"path": path, "expected_sha256": source["sha256"], "actual_sha256": actual,
                       "size_bytes": len(raw), "use": source["use"]})
    return archives, checks


def expressions(source_dir, manifest):
    source = next(entry for entry in manifest["sources"] if entry["use"] == "expression_text")
    families = manifest["split_by_family"]
    require(families.keys() == FAMILIES.keys(), "Unexpected family mapping")
    with (CORPUS / "curation.tsv").open(encoding="utf-8", newline="") as handle:
        selections = list(csv.DictReader(handle, delimiter="\t"))
    selected = {int(row["source_record"]): row for row in selections}
    require(len(selected) == len(selections), "Source record selected twice")
    counts = Counter()
    output = []
    with (source_dir / source["path"]).open(encoding="utf-8", newline="") as handle:
        for number, row in enumerate(csv.DictReader(handle), 1):
            raw_intent = row["intent"]
            if raw_intent in INTENTS:
                counts[raw_intent] += 1
            if number not in selected:
                continue
            entry = selected[number]
            family = entry["family"]
            intent = INTENTS.get(raw_intent)
            chinese = entry["chinese"].strip()
            require(intent == FAMILIES.get(family) and family in families, f"Family/intent mismatch: {number}")
            require(bool(re.search(r"[\u3400-\u9fff]", chinese)), f"Missing Chinese: {number}")
            output.append({
                "id": f"PE01-{number:05d}",
                "split": families[family],
                "family_id": family,
                "candidate_intent": intent,
                "source": {
                    "dataset": manifest["source_attribution"], "kind": manifest["input_kind"],
                    "path": source["path"], "url": source["source_url"], "revision": source["revision"],
                    "file_sha256": source["sha256"], "csv_record": number, "field": "instruction",
                    "category": row["category"], "intent": raw_intent, "tags": row["tags"],
                    "license": source["license"], "license_archive": "licenses/CDLA-Sharing-1.0.html",
                },
                "source_expression_en": row["instruction"],
                "expression_zh": chinese,
                "transformation": {
                    "origin": "AI", "kind": "TRANSLATION_AND_PARAPHRASE",
                    "notes": FAMILY_CHANGE[family],
                },
                "human_review_status": "PENDING", "gold_status": "PENDING",
            })
    require(len(output) == len(selected), "Selected source records missing")
    observed = Counter((row["candidate_intent"], row["split"]) for row in output)
    expected = {(intent, split): n for intent in INTENTS.values() for split, n in SPLIT_COUNTS.items()}
    require(observed == expected, f"Unexpected counts: {observed}")
    return output, dict(counts)


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\{\{[^{}]+\}\}", " placeholder ", value)
    return "".join(character for character in value if character.isalnum())


def ngrams(value, n):
    return {value[i:i + n] for i in range(len(value) - n + 1)}


def review_and_verify(rows):
    distances = []
    seen = {key: {} for key in ("source_expression_en", "expression_zh")}
    for row in rows:
        for key, entries in seen.items():
            value = normalized(row[key])
            require(value and value not in entries, f"Normalized duplicate {key}: {row['id']}, {entries.get(value)}")
            entries[value] = row["id"]
    family_splits = {}
    for row in rows:
        family, split = row["family_id"], row["split"]
        require(family_splits.setdefault(family, split) == split, f"Family leak: {family}")
    for key, n in (("source_expression_en", 4), ("expression_zh", 3)):
        grams = {row["id"]: ngrams(normalized(row[key]), n) for row in rows}
        for left, right in combinations(rows, 2):
            if left["split"] == right["split"]:
                continue
            a, b = grams[left["id"]], grams[right["id"]]
            score = len(a & b) / len(a | b) if a or b else 0
            distances.append((score, left["id"], right["id"], key))
    distances.sort(key=lambda item: (-item[0], *item[1:]))
    rankings = [{"left": left, "right": right, "field": key, "jaccard": round(score, 4)}
                for score, left, right, key in distances]
    pairs = rankings[:sum(score >= NEAR_THRESHOLD for score, *_ in distances)]
    flagged = Counter(record for pair in pairs for record in (pair["left"], pair["right"]))
    output = io.StringIO(newline="")
    fieldnames = [
        "id", "split", "family_id", "candidate_intent", "source_record", "source_expression_en", "expression_zh",
        "cross_split_lexical_alerts", "human_review_status", "gold_status", "reviewer",
        "intent_correct", "meaning_preserved", "natural_chinese", "family_and_split_approved", "review_notes",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        record = {name: row[name] for name in fieldnames if name in row}
        record["source_record"] = row["source"]["csv_record"]
        record["cross_split_lexical_alerts"] = flagged[row["id"]]
        writer.writerow(record)
    verification = {
        "exact_normalized_duplicates_en": 0, "exact_normalized_duplicates_zh": 0, "families_in_multiple_splits": 0,
        "near_duplicate_check": {
            "method": "Jaccard of NFKC case-folded alphanumeric character grams, EN n=4, ZH n=3",
            "threshold": NEAR_THRESHOLD, "cross_split_field_pairs_checked": len(distances),
            "highest_similarity": rankings[0]["jaccard"] if rankings else 0,
            "flagged_field_pairs": len(pairs), "alerts": pairs, "top_cross_split_field_pairs": rankings[:10],
            "human_semantic_review": "PENDING",
        },
    }
    return output.getvalue().encode(), verification


def build(source_dir):
    manifest = json.loads((CORPUS / "manifest.json").read_text())
    output, source_checks = read_sources(source_dir, manifest)
    rows, counts = expressions(source_dir, manifest)
    review, verification = review_and_verify(rows)
    output["expressions.jsonl"] = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()
    output["human-review.csv"] = review
    tasks = json.loads((source_dir / "raw/tau2_retail/tasks.json").read_text())
    splits = json.loads((source_dir / "raw/tau2_retail/split_tasks.json").read_text())
    ids = {task["id"] for task in tasks}
    require(len(ids) == len(tasks) == 114 and set(splits["base"]) == ids, "Unexpected tau2 task IDs")
    train, test = set(splits["train"]), set(splits["test"])
    require(len(train) == len(splits["train"]) == 74 and len(test) == len(splits["test"]) == 40
            and not train & test and train | test == ids, "Unexpected tau2 split coverage")
    verification.update({
        "verified_sources": source_checks, "raw_bitext_intent_counts": counts,
        "selected_total": len(rows), "selected_per_intent_by_split": {
            intent: SPLIT_COUNTS for intent in INTENTS.values()
        },
        "selected_by_split": dict(Counter(row["split"] for row in rows)),
        "selected_families": dict(Counter(row["family_id"] for row in rows)),
        "source_record_id_collisions": 0, "source_responses_copied": 0, "external_policies_copied": 0,
        "tau2_reference": {"task_count": len(tasks), "train": len(splits["train"]), "test": len(splits["test"]),
                           "expressions_or_answers_copied": 0},
        "human_review_pending": len(rows), "gold_labels_pending": len(rows),
        "artifact_sha256": {name: sha(raw) for name, raw in output.items()},
    })
    output["verification.json"] = encode_json(verification)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True, help="Directory containing raw/ and licenses/")
    parser.add_argument("--check", action="store_true", help="Compare generated bytes without overwriting")
    args = parser.parse_args()
    for name, content in build(args.source_dir).items():
        path = CORPUS / name
        if args.check:
            require(path.read_bytes() == content, f"Generated artifact differs: {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    print("Public corpus artifacts verified." if args.check else "Public corpus artifacts generated.")


if __name__ == "__main__":
    main()
