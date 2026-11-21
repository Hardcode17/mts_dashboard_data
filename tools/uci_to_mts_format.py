#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KAN-81: адаптер UCI 498 (Incident Management Process, enriched event log)
в формат, близкий к 46-полевой выгрузке инцидентов МТС.

Что делает:
1) Схлопывает журнал событий (141 712 строк) до инцидентов (24 918) по полю `number`.
2) Восстанавливает псевдо-дедлайн по SLA-матрице приоритета: для priority 1..4
   подбирает часы из кандидатов [4, 8, 24, 48, 72] так, чтобы доля инцидентов
   с `resolved_at <= opened_at + SLA` максимально совпадала с фактической долей
   `made_sla = true`. Подбор — только на train-части (первые 70% периода по opened_at).
3) Выгружает образец в docs/data/uci_adapted/incidents_uci_sample.json
   и промежуточную таблицу инцидентов в .cache/uci498/processed/incidents.csv
   (сырой CSV в git не коммитим, он живёт в .cache/).

Исходный набор: UCI ML Repository #498 «Incident Management Process (enriched
event log)», лицензия CC BY 4.0 (источник указываем всегда). Адаптированные записи
помечены `source: "uci-servicenow-adapted"` и НЕ являются выгрузкой МТС.

Принятые допущения (фиксируем, чтобы обучение было воспроизводимым):
- `made_sla` на уровне инцидента = значение из последнего события
  (в логе он монотонен: один раз False — дальше False; проверяется при запуске).
- resolved_at/closed_at в исходном логе проставлены и в ранних строках,
  поэтому агрегируем как максимум по всем событиям инцидента.
- priority/impact/urgency/category/contact_type — первые непустые значения,
  assignment_group/assigned_to — последние непустые (финальная ответственность).
- Время в исходнике без таймзоны; трактуем как UTC и выгружаем с суффиксом Z.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

MISSING = "?"
SLA_CANDIDATES_H = [4, 8, 24, 48, 72]
TRAIN_FRACTION = 0.70
MTS_PRIORITY_LABELS = {1: "Низкий", 2: "Средний", 3: "Высокий", 4: "Наивысший"}

def parse_label(series: pd.Series) -> pd.Series:
    """'3 - Moderate' -> 'Moderate' (подпись приоритета/влияния из исходника)."""
    return series.astype("string").str.split(" - ").str[1]

# incident_state (UCI) -> (stateId, stateEnum, state) нашего справочника.
STATE_MAP = {
    "New": (0, "Assigned", "Назначен"),
    "Active": (2, "WorkInProgress", "Выполняется"),
    "Awaiting User Info": (20, "Research", "Исследование ситуации"),
    "Await User": (20, "Research", "Исследование ситуации"),
    "Awaiting Vendor": (20, "Research", "Исследование ситуации"),
    "Await Vendor": (20, "Research", "Исследование ситуации"),
    "Awaiting Problem": (20, "Research", "Исследование ситуации"),
    "Awaiting Evidence": (20, "Research", "Исследование ситуации"),
    "-100": (20, "Research", "Исследование ситуации"),  # мусорное значение в исходнике
    "Resolved": (7, "Resolved", "Завершен"),
    "Closed": (10, "Closed", "Закрыт"),
}
STATE_UNKNOWN = (20, "Research", "Исследование ситуации")


def parse_scale(series: pd.Series) -> pd.Series:
    """'3 - Moderate' -> 3."""
    return pd.to_numeric(series.astype("string").str.split(" - ").str[0], errors="coerce")


def iso(ts) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_events(csv_path: Path) -> pd.DataFrame:
    events = pd.read_csv(csv_path)
    obj_cols = events.select_dtypes(include="object").columns
    events[obj_cols] = events[obj_cols].replace(MISSING, np.nan)

    for col in ["opened_at", "sys_updated_at", "resolved_at", "closed_at"]:
        events[col] = pd.to_datetime(events[col], dayfirst=True, errors="coerce")

    events["priority_n"] = parse_scale(events["priority"])
    events["impact_n"] = parse_scale(events["impact"])
    events["urgency_n"] = parse_scale(events["urgency"])
    events["priority_label"] = parse_label(events["priority"])
    events["impact_label"] = parse_label(events["impact"])
    events["urgency_label"] = parse_label(events["urgency"])

    # Порядок событий: по времени обновления, при равенстве — по sys_mod_count.
    events = events.sort_values(
        ["number", "sys_updated_at", "sys_mod_count"], kind="stable"
    )
    return events


def collapse_to_incidents(events: pd.DataFrame) -> pd.DataFrame:
    g = events.groupby("number", sort=False)
    inc = pd.DataFrame(
        {
            "opened_at": g["opened_at"].first(),
            "resolved_at": g["resolved_at"].max(),
            "closed_at": g["closed_at"].max(),
            "priority_n": g["priority_n"].first(),
            "priority_label": g["priority_label"].first(),
            "impact_n": g["impact_n"].first(),
            "impact_label": g["impact_label"].first(),
            "urgency_n": g["urgency_n"].first(),
            "urgency_label": g["urgency_label"].first(),
            "category": g["category"].first(),
            "subcategory": g["subcategory"].first(),
            "u_symptom": g["u_symptom"].first(),
            "contact_type": g["contact_type"].first(),
            "assignment_group": g["assignment_group"].last(),
            "assigned_to": g["assigned_to"].last(),
            "resolved_by": g["resolved_by"].first(),
            "incident_state": g["incident_state"].last(),
            "active": g["active"].last(),
            "made_sla": g["made_sla"].last(),
            "reassignment_count": g["reassignment_count"].max(),
            "reopen_count": g["reopen_count"].max(),
            "sys_mod_count": g["sys_mod_count"].max(),
        }
    ).reset_index()

    # Проверка монотонности made_sla: "хоть раз False" == "в конце False".
    any_false = events.groupby("number")["made_sla"].apply(lambda s: bool((~s).any()))
    last_false = ~inc.set_index("number")["made_sla"]
    mismatch = int((any_false != last_false).sum())
    if mismatch:
        raise AssertionError(
            f"made_sla немонотонен у {mismatch} инцидентов — проверьте логику агрегации"
        )
    return inc


def time_split(inc: pd.DataFrame) -> pd.Series:
    """train = инциденты из первых 70% периода по opened_at, test = последние 30%.

    Точка разбиения — 70-й перцентиль opened_at (а не 70% диапазона min..max):
    в исходнике ~1% инцидентов растянуты до 2017-02 при массе до 2016-08,
    сплит по диапазону почти всё кладёт в train."""
    split_ts = inc["opened_at"].quantile(TRAIN_FRACTION)
    return np.where(inc["opened_at"] <= split_ts, "train", "test")


def fit_sla_hours(inc: pd.DataFrame) -> tuple[dict, dict]:
    """Подбор часов SLA по приоритетам: доля (resolved <= opened + SLA)
    должна максимально совпадать с фактической долей made_sla=true.
    Только на train-части, только по инцидентам с известным resolved_at."""
    tr = inc[(inc["split"] == "train") & inc["resolved_at"].notna()]
    sla_hours, report = {}, {}
    for p in [1, 2, 3, 4]:
        sub = tr[tr["priority_n"] == p]
        actual = float(sub["made_sla"].mean())
        rows = []
        best_h, best_d = None, np.inf
        for h in SLA_CANDIDATES_H:
            frac = float((sub["resolved_at"] <= sub["opened_at"] + pd.Timedelta(hours=h)).mean())
            d = abs(frac - actual)
            rows.append({"hours": h, "pseudoSlaShare": round(frac, 4), "absDiff": round(d, 4)})
            if d < best_d:
                best_h, best_d = h, d
        sla_hours[str(p)] = best_h
        report[str(p)] = {
            "incidents": int(len(sub)),
            "actualMadeSlaShare": round(actual, 4),
            "chosenHours": best_h,
            "candidates": rows,
        }
    return sla_hours, report


def incident_to_mts_json(row: pd.Series) -> dict:
    priority_id = 5 - int(row["priority_n"])
    state = STATE_MAP.get(row["incident_state"], STATE_UNKNOWN)
    assignee = row["assigned_to"]
    if pd.isna(assignee):
        assignee = row["resolved_by"]  # fallback: кто закрыл
    return {
        "id": row["number"],
        "header": None if pd.isna(row["u_symptom"]) else row["u_symptom"],
        "priorityId": priority_id,
        "priority": MTS_PRIORITY_LABELS[priority_id],
        "impactId": int(row["impact_n"]),
        "impact": row["impact_label"],
        "urgencyId": int(row["urgency_n"]),
        "urgency": row["urgency_label"],
        "submitDate": iso(row["opened_at"]),
        "completionDate": iso(row["resolved_at"]),
        "deadline": iso(row["deadline"]),
        "closedDate": iso(row["closed_at"]),
        "assignedGroupName": None if pd.isna(row["assignment_group"]) else row["assignment_group"],
        "assignedEmployee": None if pd.isna(assignee) else assignee,
        "receivingChannel": None if pd.isna(row["contact_type"]) else row["contact_type"],
        "service": None if pd.isna(row["category"]) else row["category"],
        "productName": None if pd.isna(row["subcategory"]) else row["subcategory"],
        "stateId": state[0],
        "stateEnum": state[1],
        "state": state[2],
        "active": bool(row["active"]),
        "madeSla": bool(row["made_sla"]),
        "slaHours": int(row["sla_hours"]),
        "source": "uci-servicenow-adapted",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path, default=Path(".cache/uci498/extracted/incident_event_log.csv"))
    ap.add_argument("--workdir", type=Path, default=Path(".cache/uci498/processed"))
    ap.add_argument("--sample-out", type=Path, default=Path("docs/data/uci_adapted/incidents_uci_sample.json"))
    ap.add_argument("--sample-size", type=int, default=200)
    args = ap.parse_args()

    print(f"Читаю события: {args.csv}")
    events = load_events(args.csv)
    print(f"Событий: {len(events)}, инцидентов: {events['number'].nunique()}")

    inc = collapse_to_incidents(events)
    inc["split"] = time_split(inc)

    sla_hours, sla_report = fit_sla_hours(inc)
    inc["sla_hours"] = inc["priority_n"].map({int(k): v for k, v in sla_hours.items()})
    inc["deadline"] = inc["opened_at"] + pd.to_timedelta(inc["sla_hours"], unit="h")

    print("\nSLA-часы по приоритетам (подбор на train, совмещение с долей made_sla=true):")
    for p in ["1", "2", "3", "4"]:
        r = sla_report[p]
        print(
            f"  priority {p}: выбрано {r['chosenHours']} ч "
            f"(инцидентов {r['incidents']}, фактическая доля made_sla={r['actualMadeSlaShare']})"
        )

    args.workdir.mkdir(parents=True, exist_ok=True)
    inc_path = args.workdir / "incidents.csv"
    inc.to_csv(inc_path, index=False)
    (args.workdir / "sla_hours.json").write_text(
        json.dumps(
            {
                "slaHoursByPriority": sla_hours,
                "candidatesHours": SLA_CANDIDATES_H,
                "fittedOn": "train split (первые 70% периода по opened_at, инциденты с известным resolved_at)",
                "fitReport": sla_report,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nТаблица инцидентов: {inc_path} ({len(inc)} строк)")
    print(f"train/test: {(inc['split'] == 'train').sum()} / {(inc['split'] == 'test').sum()}")

    sample = (
        inc.sort_values(["opened_at", "number"], kind="stable")
        .head(args.sample_size)
        .apply(incident_to_mts_json, axis=1)
        .tolist()
    )
    args.sample_out.parent.mkdir(parents=True, exist_ok=True)
    args.sample_out.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Образец в нашем формате: {args.sample_out} ({len(sample)} инцидентов)")

    n_no_resolved = int(inc["resolved_at"].isna().sum())
    print(f"Инцидентов без resolved_at (в обучении не участвуют): {n_no_resolved}")


if __name__ == "__main__":
    main()
