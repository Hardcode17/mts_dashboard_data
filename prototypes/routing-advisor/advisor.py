"""Data-only KAN-74 prototype. No network calls to ITSM or assignment writes."""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = "intfloat/multilingual-e5-small"
MODEL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
DIMENSIONS = 384
DEFAULT_THRESHOLD = 0.86
SIMILARITY_WINDOW = 0.02
CANDIDATE_LIMIT = 10
RESULT_LIMIT = 3
SITE_BONUS = 0.015
TERMINAL_STATES = {"Resolved", "Closed"}
STOP_WORDS = set("при после через когда перед их для был были нет это там уже что еще как все без".split())
SENSITIVE = re.compile(r"https?://[^\s<>]+|[\w.+-]+@[\w.-]+\.\w+|\b(?:\d{1,3}\.){3}\d{1,3}\b", re.I)
TOKENS = re.compile(r"[a-zа-яё0-9]+(?:[-‑][a-zа-яё0-9]+)*", re.I)

# Materializing the scoped rows makes the top ten exact rather than approximate.
SEARCH_SQL = """
WITH scoped AS MATERIALIZED (
    SELECT incident_id, service, infrastructure, site, title, description, outcome, resolved_by,
           1 - (embedding <=> %(embedding)s::vector) AS similarity
    FROM routing_advisor.history
    WHERE status IN ('Resolved', 'Closed')
      AND incident_id <> %(id)s
      AND (service = %(service)s OR infrastructure && %(infrastructure)s::text[])
)
SELECT * FROM scoped
ORDER BY similarity DESC, incident_id
LIMIT 10
"""


def infrastructure(record):
    value = record.get("infrastructure")
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return sorted({item.strip() for item in (value or []) if item.strip()})


def symptom(record):
    text = " ".join(f"{record['title']} {record['description']}".split())
    return SENSITIVE.sub(" ", text)


def keywords(record):
    return {word for word in TOKENS.findall(symptom(record).casefold()) if len(word) > 2} - STOP_WORDS


def vector_literal(values):
    if len(values) != DIMENSIONS:
        raise ValueError(f"expected {DIMENSIONS} embedding dimensions")
    return "[" + ",".join(map(str, values)) + "]"


def fixture_id(fixture):
    corpus = json.dumps([fixture["history"], fixture["owners"]], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(corpus.encode("utf-8")).hexdigest()


def embedding_model_id(model_path):
    if model_path == MODEL:
        return f"{MODEL}@{MODEL_REVISION}"
    folder = Path(model_path).resolve()
    files = ("config.json", "model.safetensors", "tokenizer.json", "tokenizer_config.json")
    digest = hashlib.sha256()
    for name in files:
        with (folder / name).open("rb") as stream:
            digest.update(hashlib.file_digest(stream, "sha256").digest())
    return f"local:{digest.hexdigest()}"


class E5Embedder:
    def __init__(self, model_path):
        import torch
        from transformers import AutoModel, AutoTokenizer

        revision = {"revision": MODEL_REVISION} if model_path == MODEL else {}
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, **revision)
        self.model = AutoModel.from_pretrained(model_path, use_safetensors=True, **revision).eval()

    def encode(self, records, prefix):
        vectors = []
        with self.torch.inference_mode():
            for start in range(0, len(records), 16):
                inputs = self.tokenizer(
                    [f"{prefix}: {symptom(record)}" for record in records[start:start + 16]],
                    return_tensors="pt", padding=True, truncation=True, max_length=512,
                )
                hidden = self.model(**inputs).last_hidden_state
                mask = inputs["attention_mask"].unsqueeze(-1)
                pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1)
                normalized = self.torch.nn.functional.normalize(pooled, dim=1)
                vectors.extend(vector_literal(row) for row in normalized.tolist())
        return vectors


def analogue(query, candidate):
    signals, differences = [], []
    if candidate["service"] == query["service"]:
        signals.append(f"Сервис: {query['service']}")
    else:
        differences.append(f"Сервис аналога: {candidate['service']}")
    shared = sorted(set(infrastructure(query)) & set(candidate["infrastructure"]))
    if shared:
        signals.append("Общая инфраструктура: " + ", ".join(shared))
    overlap = sorted(keywords(query) & keywords(candidate))[:5]
    if overlap:
        signals.append("Совпавшие слова: " + ", ".join(overlap))
    if candidate["site"] == query["site"]:
        signals.append(f"Площадка: {query['site']}")
    else:
        differences.append(f"Площадка аналога: {candidate['site']}")
    return {
        "incident_id": candidate["incident_id"],
        "source_ref": f"itsm:{candidate['incident_id']}",
        "matched_signals": signals,
        "differences": differences,
        "outcome": candidate["outcome"],
        "resolved_by": candidate.get("resolved_by"),
        "similarity": round(candidate["similarity"], 4),
    }


def summarize(query, candidates, owners, threshold):
    ranked = sorted(candidates[:CANDIDATE_LIMIT], key=lambda row: (
        -(row["similarity"] + SITE_BONUS * (row["site"] == query["site"])), row["incident_id"],
    ))
    strongest = max((row["similarity"] for row in ranked if (row.get("outcome") or "").strip()),
                    default=threshold)
    floor = max(threshold, strongest - SIMILARITY_WINDOW)
    audit, analogues = [], []
    for candidate in ranked:
        if not (candidate.get("outcome") or "").strip():
            reason = "unknown_outcome"
        elif candidate["similarity"] < floor:
            reason = "low_similarity"
        elif len(analogues) == RESULT_LIMIT:
            reason = "outside_top_3"
        else:
            reason = "selected"
            analogues.append(analogue(query, candidate))
        audit.append({"incident_id": candidate["incident_id"], "decision": reason,
                      "similarity": round(candidate["similarity"], 4)})

    group, recommendation = None, None
    if query.get("site") and len(owners) == 1:
        [group] = owners
    if analogues and group and group != (query.get("current_group") or "").strip():
        recommendation = {
            "group": group,
            "incident_ids": [row["incident_id"] for row in analogues],
            "reasons": [
                f"Актуальный владелец сервиса «{query['service']}» на площадке «{query['site']}».",
                f"Найдено аналогов с известным исходом: {len(analogues)}.",
            ],
        }
    return {"incident_id": query["id"], "analogues": analogues, "recommendation": recommendation}, audit


def search(conn, query, embedding, threshold):
    args = dict(query, embedding=embedding, infrastructure=infrastructure(query))
    candidates = conn.execute(SEARCH_SQL, args).fetchall()
    groups = conn.execute(
        """SELECT owner_group FROM routing_advisor.owners
           WHERE service = %s AND site = %s ORDER BY owner_group""",
        (query["service"], query["site"]),
    ).fetchall()
    return summarize(query, candidates, [row["owner_group"] for row in groups], threshold)


def baseline(query, history):
    tokens = keywords(query)
    candidates = [
        (len(tokens & keywords(row)), row["id"])
        for row in history
        if row["status"] in TERMINAL_STATES and row["service"] == query["service"]
        and row["id"] != query["id"] and (row.get("outcome") or "").strip()
    ]
    return [incident_id for count, incident_id in sorted(
        candidates, key=lambda item: (-item[0], item[1])
    ) if count > 0][:RESULT_LIMIT]


def seed(conn, fixture, embedder, model_id):
    history = fixture["history"]
    embeddings = embedder.encode(history, "passage")
    with conn.transaction(), conn.cursor() as cur:
        # Serialize schema creation as well as corpus replacement on first use.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('kan-74 routing advisor seed'))")
        cur.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
        cur.execute("DELETE FROM routing_advisor.history")
        cur.execute("DELETE FROM routing_advisor.owners")
        cur.executemany(
            """INSERT INTO routing_advisor.history
               (incident_id, status, service, infrastructure, site, title, description,
                outcome, resolved_by, first_assigned_group, embedding)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)""",
            [(row["id"], row["status"], row["service"], infrastructure(row), row["site"],
              row["title"], row["description"], row.get("outcome"), row.get("resolved_by"),
              row.get("first_assigned_group"), embedding)
             for row, embedding in zip(history, embeddings, strict=True)],
        )
        cur.executemany(
            """INSERT INTO routing_advisor.owners VALUES (%s,%s,%s)
               ON CONFLICT DO NOTHING""",
            [(owner["service"].strip(), owner["site"].strip(), owner["group"].strip())
             for owner in fixture["owners"]],
        )
        cur.execute(
            """INSERT INTO routing_advisor.seed_state VALUES (true, %s, %s)
               ON CONFLICT (singleton) DO UPDATE
               SET model_id = EXCLUDED.model_id, fixture_id = EXCLUDED.fixture_id""",
            (model_id, fixture_id(fixture)),
        )
    return {"history_count": len(history), "owner_rows": len(fixture["owners"])}


def check_seed(conn, model_id, expected_fixture_id=None):
    state = conn.execute(
        "SELECT model_id, fixture_id FROM routing_advisor.seed_state"
    ).fetchone()
    if state is None or state["model_id"] != model_id:
        raise ValueError("corpus uses a different embedding model or is empty; run seed first")
    if expected_fixture_id is not None and state["fixture_id"] != expected_fixture_id:
        raise ValueError("evaluation fixture differs from the seeded corpus; seed the selected fixture first")


def evaluate(conn, fixture, embedder, threshold):
    queries = fixture["queries"]
    vectors = embedder.encode(queries, "query")
    positives = vector_hits = keyword_hits = correct_groups = false_suggestions = 0
    retrieved = relevant_retrieved = 0
    details = []
    history = conn.execute(
        """SELECT incident_id AS id, status, service, title, description, outcome
           FROM routing_advisor.history"""
    ).fetchall()
    for query, vector in zip(queries, vectors, strict=True):
        result, audit = search(conn, query, vector, threshold)
        relevant = set(query["relevant_ids"])
        vector_ids = [analogue["incident_id"] for analogue in result["analogues"]]
        baseline_ids = baseline(query, history)
        retrieved += len(vector_ids)
        relevant_retrieved += len(relevant.intersection(vector_ids))
        group = result["recommendation"]["group"] if result["recommendation"] else None
        if relevant:
            positives += 1
            vector_hits += bool(relevant.intersection(vector_ids))
            keyword_hits += bool(relevant.intersection(baseline_ids))
        correct_groups += group == query["expected_group"]
        false_suggestions += group is not None and group != query["expected_group"]
        details.append({"id": query["id"], "vector_top_3": vector_ids, "keyword_top_3": baseline_ids,
                        "group": group, "expected_group": query["expected_group"], "candidates": audit})
    return {"positive_queries": positives, "negative_queries": len(queries) - positives,
            "hit_at_3_vector": vector_hits / positives if positives else None,
            "hit_at_3_keyword": keyword_hits / positives if positives else None,
            "vector_hits": vector_hits, "keyword_hits": keyword_hits,
            "correct_groups": correct_groups, "false_suggestions": false_suggestions,
            "relevant_analogues_returned": relevant_retrieved, "analogues_returned": retrieved,
            "threshold": threshold, "details": details}


def load_fixture(path):
    fixture = json.loads(path.read_text(encoding="utf-8"))
    history = {row["id"]: row for row in fixture["history"]}
    if len(history) != len(fixture["history"]):
        raise ValueError("history incident IDs must be unique")
    if len({row["id"] for row in fixture["queries"]}) != len(fixture["queries"]):
        raise ValueError("query IDs must be unique")
    for query in fixture["queries"]:
        for incident_id in query["relevant_ids"]:
            row = history[incident_id]
            if (incident_id == query["id"] or row["status"] not in TERMINAL_STATES
                    or not (row.get("outcome") or "").strip()):
                raise ValueError(f"invalid positive label {incident_id} for {query['id']}")
    return fixture


def main(argv=None):
    import psycopg
    from psycopg.rows import dict_row

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed", "evaluate", "show"])
    parser.add_argument("--fixture", type=Path, default=ROOT / "demo.json")
    parser.add_argument("--id", help="fixture query ID for show")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)
    if not 0 <= args.threshold <= 1:
        parser.error("--threshold must be between 0 and 1")
    dsn = os.environ.get("ROUTING_DSN")
    if not dsn:
        parser.error("set ROUTING_DSN to a dedicated prototype database")
    model_path = os.environ.get("ROUTING_MODEL", MODEL)
    try:
        model_id = embedding_model_id(model_path)
        fixture = load_fixture(args.fixture)
        query = next((row for row in fixture["queries"] if row["id"] == args.id), None)
        if args.command == "show" and not query:
            parser.error("show requires --id matching a fixture query")
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            if args.command != "seed":
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                expected = fixture_id(fixture) if args.command == "evaluate" else None
                check_seed(conn, model_id, expected)
            embedder = E5Embedder(model_path)
            if args.command == "seed":
                result = seed(conn, fixture, embedder, model_id)
            elif args.command == "evaluate":
                result = evaluate(conn, fixture, embedder, args.threshold)
            else:
                [vector] = embedder.encode([query], "query")
                result, _ = search(conn, query, vector, args.threshold)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, psycopg.Error) as exc:
        print(f"Routing advisor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
