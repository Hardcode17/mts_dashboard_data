#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""KAN-81: обучение модели риска нарушения дедлайна на UCI 498 (ServiceNow).

Конвейер (всё офлайн, seed зафиксирован):
1) Для каждого инцидента с известным resolved_at выбираем случайный «момент
   оценки» t равномерно между opened_at и resolved_at (seed = --seed).
2) Признаки строго на момент t — из строки события с наибольшим
   sys_updated_at <= t (без подглядывания в будущее):
     x1 = медианное время решения (category x priority x assignment_group),
          посчитанное ТОЛЬКО на train-части, делённое на (псевдо-дедлайн - t) в часах
          (остаток зажимаем снизу 0.25 ч, если t уже за дедлайном);
     x2 = sys_mod_count на момент t (proxy «дедлайн продлевался/пересоздавался»);
     x3 = reassignment_count на момент t;
     x4 = reopen_count на момент t;
     x5 = часы от последней смены incident_state до t;
     x6 = число открытых инцидентов у assigned_to на момент t (point-in-time);
     x7 = priority (1..4).
   Таргет: made_sla (инцидент в итоге нарушил SLA).
3) Разбиение строго по времени: train = первые 70% периода, test = последние 30%
   (та же граница, что в tools/uci_to_mts_format.py).
4) Логистическая регрессия sklearn БЕЗ стандартизации — коэффициенты
   нестандартизированные, Java-скоринг воспроизводит их как
   score = w0 + w1*x1 + ... + w7*x7 (клампы признаков — в weights.json).
5) Пороги подбираем на train под бюджет ~20% ложных срабатываний на
   карточку-не-просрочку; на test считаем recall/precision/F1/lead time.
6) Бейзлайн A: «до дедлайна < N часов» (N на train). Бейзлайн B: медиана
   (category x priority x group) > остатка до дедлайна (плюс вариант с
   множителем k, подобранным на train под тот же бюджет ~20% FPR).

Артефакты: tools/deadline_risk_weights.json, tools/deadline_risk_medians.json.
"""

import argparse
import json
from bisect import bisect_right
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

H = np.timedelta64(1, "h")
MIN_REMAINING_H = 0.25  # знаменатель x1, если t уже за дедлайном
FPR_BUDGET = 0.20       # бюджет ложных срабатываний на карточку-не-просрочку
TS_MAX = np.datetime64("2262-04-01", "ns")  # «бесконечность» для открытых карточек

# Клампы признаков: чтобы нестандартизированные коэффициенты были устойчивы,
# признак зажимается в диапазон перед обучением (и тем же образом в Java-скоринге).
CLAMPS = {
    "x1": (0.0, 100.0),
    "x2": (0.0, 200.0),
    "x3": (0.0, 50.0),
    "x4": (0.0, 20.0),
    "x5": (0.0, 2160.0),
    "x6": (0.0, 100.0),
    "x7": (1.0, 4.0),
}
FEATURE_ORDER = ["x1", "x2", "x3", "x4", "x5", "x6", "x7"]


def load_incidents(path: Path) -> pd.DataFrame:
    inc = pd.read_csv(path, parse_dates=["opened_at", "resolved_at", "closed_at", "deadline"])
    for col in ["category", "assignment_group"]:
        inc[col] = inc[col].fillna("unknown")
    return inc


def build_resolution_medians(train_inc: pd.DataFrame) -> tuple[dict, dict, dict]:
    """Медианы времени решения (часы) только на train-части.
    Возврат: (по (cat,pri,group), по (cat,pri), по priority)."""
    sub = train_inc[train_inc["resolved_at"].notna() & (train_inc["resolved_at"] > train_inc["opened_at"])].copy()
    sub["res_hours"] = (sub["resolved_at"] - sub["opened_at"]) / pd.Timedelta(hours=1)

    med_cpg = (
        sub.groupby(["category", "priority_n", "assignment_group"], observed=True)["res_hours"]
        .median()
        .to_dict()
    )
    med_cp = sub.groupby(["category", "priority_n"], observed=True)["res_hours"].median().to_dict()
    med_p = sub.groupby("priority_n")["res_hours"].median().to_dict()
    return med_cpg, med_cp, med_p


def median_for(cat, pri, grp, med_cpg, med_cp, med_p):
    v = med_cpg.get((cat, pri, grp))
    if v is not None and not pd.isna(v):
        return float(v), "cat_pri_grp"
    v = med_cp.get((cat, pri))
    if v is not None and not pd.isna(v):
        return float(v), "cat_pri"
    return float(med_p.get(pri, 0.0) or 0.0), "priority"


def build_event_index(events_path: Path) -> dict:
    """По одному проходу по логу собираем на инцидент:
    - times: массивы sys_updated_at (отсортированы);
    - счётчики на момент t (sys_mod_count, reassignment_count, reopen_count, assigned_to);
    - state_change_times: моменты смены incident_state (для x5);
    - assignee_intervals: (assignee, start, end) — кто и когда держал карточку (для x6)."""
    ev = pd.read_csv(
        events_path,
        usecols=[
            "number", "sys_updated_at", "sys_mod_count", "incident_state",
            "reassignment_count", "reopen_count", "assigned_to",
        ],
    )
    ev["sys_updated_at"] = pd.to_datetime(ev["sys_updated_at"], dayfirst=True, errors="coerce")
    ev = ev.sort_values(["number", "sys_updated_at", "sys_mod_count"], kind="stable")

    per_inc = ev.groupby("number", sort=False)
    index = {}
    for number, grp in per_inc:
        times = grp["sys_updated_at"].to_numpy()
        states = grp["incident_state"].to_numpy()
        cur_assignees = grp["assigned_to"].to_numpy()

        state_changes = [times[0]]
        prev_state = states[0]
        for ts, st in zip(times, states):
            if st != prev_state:
                state_changes.append(ts)
                prev_state = st

        intervals = []
        prev_a, start = None, times[0]
        for ts, a in zip(times, cur_assignees):
            if a != prev_a:
                if prev_a is not None:
                    intervals.append((prev_a, start, ts))
                prev_a, start = a, ts
        if prev_a is not None:
            intervals.append((prev_a, start, TS_MAX))

        index[number] = {
            "times": times,
            "sys_mod": grp["sys_mod_count"].to_numpy(dtype=float),
            "reassign": grp["reassignment_count"].to_numpy(dtype=float),
            "reopen": grp["reopen_count"].to_numpy(dtype=float),
            "assignees": cur_assignees,
            "state_changes": np.asarray(state_changes, dtype="datetime64[ns]"),
            "intervals": intervals,
        }
    return index


def build_assignee_timelines(event_index: dict, resolved_by_incident: dict) -> dict:
    """Для x6: по каждому исполнителю — интервалы [start, end), когда он держал
    открытую карточку. Конец интервала = min(следующая передача, resolved_at)."""
    starts, ends = {}, {}
    for number, data in event_index.items():
        resolved = resolved_by_incident.get(number)
        for a, s, e in data["intervals"]:
            end = e
            if resolved is not None and np.datetime64(resolved, "ns") < end:
                end = np.datetime64(resolved, "ns")
            if end <= s:
                continue
            starts.setdefault(a, []).append(s)
            ends.setdefault(a, []).append(end)
    return {
        a: (np.asarray(starts[a], dtype="datetime64[ns]"), np.asarray(ends[a], dtype="datetime64[ns]"))
        for a in starts
    }


def open_count(assignee, t, timelines, own_intervals) -> float:
    if assignee is None or (isinstance(assignee, float) and np.isnan(assignee)):
        return 0.0
    tl = timelines.get(assignee)
    if tl is None:
        return 0.0
    starts, ends = tl
    n = bisect_right(starts, t) - bisect_right(ends, t)
    # собственная карточка не должна попадать в «загруженность»
    own = any(a == assignee and s <= t < e for a, s, e in own_intervals)
    return float(max(n - (1 if own else 0), 0))


def clamp(name: str, value: float) -> float:
    lo, hi = CLAMPS[name]
    return float(min(max(value, lo), hi))


def make_samples(inc: pd.DataFrame, event_index: dict, medians, timelines, rng) -> dict:
    """Один случайный момент оценки t на инцидент; признаки строго на момент t."""
    med_cpg, med_cp, med_p = medians
    rows = []
    fallback_stat = {"cat_pri_grp": 0, "cat_pri": 0, "priority": 0}
    for row in inc.itertuples(index=False):
        data = event_index.get(row.number)
        if data is None:
            continue
        opened = np.datetime64(row.opened_at, "ns")
        resolved = np.datetime64(row.resolved_at, "ns")
        deadline = np.datetime64(row.deadline, "ns")
        if resolved <= opened:
            continue  # вырожденные/битые записи
        t = opened + rng.random() * (resolved - opened)

        idx = int(np.searchsorted(data["times"], t, side="right") - 1)
        if idx < 0:
            idx = 0

        med_h, level = median_for(row.category, row.priority_n, row.assignment_group, med_cpg, med_cp, med_p)
        fallback_stat[level] += 1
        remaining_h = (deadline - t) / H
        x1 = med_h / max(remaining_h, MIN_REMAINING_H) if med_h > 0 else 0.0

        ch_idx = int(np.searchsorted(data["state_changes"], t, side="right") - 1)
        x5 = max((t - data["state_changes"][ch_idx]) / H, 0.0) if ch_idx >= 0 else 0.0

        assignee = data["assignees"][idx]
        x6 = open_count(assignee, t, timelines, data["intervals"])

        rows.append(
            {
                "number": row.number,
                "split": row.split,
                "y": 0 if row.made_sla else 1,
                "x1": clamp("x1", x1),
                "x2": clamp("x2", data["sys_mod"][idx]),
                "x3": clamp("x3", data["reassign"][idx]),
                "x4": clamp("x4", data["reopen"][idx]),
                "x5": clamp("x5", x5),
                "x6": clamp("x6", x6),
                "x7": clamp("x7", float(row.priority_n)),
                "median_h": med_h,
                "remaining_h": remaining_h,
                "deadline_minus_t_h": remaining_h,
            }
        )
    df = pd.DataFrame(rows)
    return {"df": df, "fallback": fallback_stat}


def evaluate(y, pred, deadline_minus_t_h) -> dict:
    y = np.asarray(y)
    pred = np.asarray(pred, dtype=bool)
    tp = int(((y == 1) & pred).sum())
    fp = int(((y == 0) & pred).sum())
    n_pos, n_neg = int((y == 1).sum()), int((y == 0).sum())
    recall = tp / n_pos if n_pos else float("nan")
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    fpr = fp / n_neg if n_neg else float("nan")

    lead = np.asarray(deadline_minus_t_h)[(y == 1) & pred]
    lead_pos = lead[lead > 0]
    return {
        "flagged": int(pred.sum()),
        "recallViolations": round(recall, 4),
        "precision": round(precision, 4),
        "f1": round(f1, 4),
        "falsePositiveRate": round(fpr, 4),
        "leadTimeMedianH": round(float(np.median(lead)), 2) if len(lead) else None,
        "leadTimeMedianBeforeDeadlineH": round(float(np.median(lead_pos)), 2) if len(lead_pos) else None,
        "shareFlaggedAfterDeadline": round(float(np.mean(lead <= 0)), 4) if len(lead) else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events-csv", type=Path, default=Path(".cache/uci498/extracted/incident_event_log.csv"))
    ap.add_argument("--incidents-csv", type=Path, default=Path(".cache/uci498/processed/incidents.csv"))
    ap.add_argument("--sla-hours-json", type=Path, default=Path(".cache/uci498/processed/sla_hours.json"))
    ap.add_argument("--weights-out", type=Path, default=Path("tools/deadline_risk_weights.json"))
    ap.add_argument("--medians-out", type=Path, default=Path("tools/deadline_risk_medians.json"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    inc = load_incidents(args.incidents_csv)
    model_inc = inc[inc["resolved_at"].notna()].copy()
    n_dropped = int((model_inc["resolved_at"] <= model_inc["opened_at"]).sum())
    model_inc = model_inc[model_inc["resolved_at"] > model_inc["opened_at"]]
    print(
        f"Инцидентов для моделирования: {len(model_inc)} "
        f"(без resolved_at: {int(inc['resolved_at'].isna().sum())}, "
        f"битых (resolved <= opened): {n_dropped})"
    )

    train_inc = model_inc[model_inc["split"] == "train"]
    test_inc = model_inc[model_inc["split"] == "test"]
    print(f"train/test: {len(train_inc)} / {len(test_inc)}")
    print(f"доля нарушений SLA train/test: "
          f"{1 - train_inc['made_sla'].mean():.4f} / {1 - test_inc['made_sla'].mean():.4f}")

    med_cpg, med_cp, med_p = build_resolution_medians(train_inc)
    print(f"медианных бакетов (cat x pri x grp): {len(med_cpg)}")

    print("Индексирую события (момент оценки t)...")
    event_index = build_event_index(args.events_csv)
    resolved_map = dict(zip(model_inc["number"], model_inc["resolved_at"]))
    timelines = build_assignee_timelines(event_index, resolved_map)

    rng = np.random.default_rng(args.seed)
    res = make_samples(model_inc, event_index, (med_cpg, med_cp, med_p), timelines, rng)
    samples = res["df"]
    print(f"Проб «момент оценки»: {len(samples)}; fallback медиан: {res['fallback']}")

    tr = samples[samples["split"] == "train"]
    te = samples[samples["split"] == "test"]
    feats = FEATURE_ORDER
    X_tr, y_tr = tr[feats].to_numpy(), tr["y"].to_numpy()
    X_te, y_te = te[feats].to_numpy(), te["y"].to_numpy()

    lr = LogisticRegression(penalty=None, solver="lbfgs", max_iter=5000, tol=1e-8)
    lr.fit(X_tr, y_tr)
    w0 = float(lr.intercept_[0])
    w = [float(c) for c in lr.coef_[0]]
    print(f"\nВеса (нестандартизированные): w0={w0:.6f}, " + ", ".join(f"w{i+1}={v:.6f}" for i, v in enumerate(w)))

    # Пороги на train под бюджет ~20% FPR: high = 80-й перцентиль score у не-просрочек,
    # medium = 60-й (FPR ~40%).
    score_tr = lr.decision_function(X_tr)
    score_te = lr.decision_function(X_te)
    th_high = float(np.quantile(score_tr[y_tr == 0], 1 - FPR_BUDGET))
    th_medium = float(np.quantile(score_tr[y_tr == 0], 0.60))
    fpr_tr_high = float((score_tr[y_tr == 0] >= th_high).mean())
    print(f"Пороги: medium={th_medium:.4f}, high={th_high:.4f} (train FPR при high: {fpr_tr_high:.4f})")

    lead_te = te["deadline_minus_t_h"].to_numpy()
    rem_te = lead_te  # остаток до дедлайна на момент оценки t, часов
    metrics = evaluate(y_te, score_te >= th_high, lead_te)
    print(f"Регрессия на test (порог high): {metrics}")

    # Продуктовая метрика: предупреждение имеет смысл ДО дедлайна.
    # Протокол «t равномерно в (opened, resolved)» смещает оценку на поздние
    # моменты у долгих карточек, поэтому отдельно считаем метрики на подвыборке
    # «момент оценки ещё до дедлайна» (remaining > 0).
    before = rem_te > 0
    metrics_before = evaluate(y_te[before], (score_te >= th_high)[before], lead_te[before])
    print(f"Регрессия на test, t < дедлайна: {metrics_before}")

    before = rem_te > 0

    # Бейзлайн A: «до дедлайна < N часов», N подобран на train под тот же бюджет.
    rem_tr = tr["deadline_minus_t_h"].to_numpy()
    n_hours = float(np.quantile(rem_tr[y_tr == 0], FPR_BUDGET))
    bl_a = evaluate(y_te, rem_te < n_hours, lead_te)
    print(f"Бейзлайн A (N={n_hours:.2f} ч): {bl_a}")
    bl_a_before = evaluate(y_te[before], (rem_te < n_hours)[before], lead_te[before])
    print(f"Бейзлайн A, t < дедлайна: {bl_a_before}")

    # Бейзлайн B: медиана (cat x pri x grp) > остатка до дедлайна.
    med_tr, med_te = tr["median_h"].to_numpy(), te["median_h"].to_numpy()
    bl_b = evaluate(y_te, med_te > rem_te, lead_te)
    print(f"Бейзлайн B (medians > остатка): {bl_b}")
    bl_b_before = evaluate(y_te[before], (med_te > rem_te)[before], lead_te[before])
    print(f"Бейзлайн B, t < дедлайна: {bl_b_before}")

    # Бейзлайн B с множителем k, подобранным на train под бюджет ~20% FPR.
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio_tr = np.where(med_tr > 0, rem_tr / med_tr, -np.inf)
    k = float(np.quantile(ratio_tr[y_tr == 0], FPR_BUDGET))
    bl_b_k = evaluate(y_te, med_te * k > rem_te, lead_te)
    print(f"Бейзлайн B tuned (k={k:.4f}): {bl_b_k}")
    bl_b_k_before = evaluate(y_te[before], (med_te * k > rem_te)[before], lead_te[before])
    print(f"Бейзлайн B tuned, t < дедлайна: {bl_b_k_before}")

    # Вердикт: регрессия «заметно бьёт» B, если при том же бюджете FPR
    # recall выше минимум на 5 п.п. и F1 выше минимум на 0.03.
    beats = (
        metrics["recallViolations"] - bl_b_k["recallViolations"] >= 0.05
        and metrics["f1"] - bl_b_k["f1"] >= 0.03
    )
    verdict = (
        "regressionWins"
        if beats
        else "regression does not clearly beat the median baseline — в продукт идёт бейзлайн B (см. verdictDetails)"
    )
    print(f"\nВердикт: {verdict}")

    weights = {
        "model": "logistic regression (sklearn, penalty=None, без стандартизации)",
        "scoring": "score = w0 + w1*x1 + ... + w7*x7; risk = high, если score >= thresholds.high; medium, если >= thresholds.medium; иначе low",
        "w0": round(w0, 6),
        **{f"w{i + 1}": round(v, 6) for i, v in enumerate(w)},
        "features": {
            "x1": "medianResolutionHours(category x priority x assignment_group, train split) / max(deadline - t, 0.25h)",
            "x2": "sys_mod_count на момент t (proxy переносов дедлайна/пересоздания)",
            "x3": "reassignment_count на момент t",
            "x4": "reopen_count на момент t",
            "x5": "часы от последней смены incident_state до t",
            "x6": "число открытых инцидентов у assigned_to на момент t",
            "x7": "priority (1..4)",
        },
        "featureClamps": {k: list(v) for k, v in CLAMPS.items()},
        "minRemainingHoursForX1": MIN_REMAINING_H,
        "thresholds": {"medium": round(th_medium, 6), "high": round(th_high, 6)},
        "slaHoursByPriority": json.loads(args.sla_hours_json.read_text(encoding="utf-8"))["slaHoursByPriority"],
        "mediansSource": "train split",
        "mediansFile": "tools/deadline_risk_medians.json",
        "trainedOn": "UCI ML Repository #498 Incident Management Process (enriched event log), CC BY 4.0; адаптация source: uci-servicenow-adapted",
        "seed": args.seed,
        "fprBudget": FPR_BUDGET,
        "metrics": {
            "trainFprAtHigh": round(fpr_tr_high, 4),
            "test": metrics,
            "testBeforeDeadline": metrics_before,
            "baselineA_deadlineRemainingLtN": {
                **bl_a, "NHours": round(n_hours, 2), "beforeDeadline": bl_a_before,
            },
            "baselineB_medianGtRemaining": {**bl_b, "beforeDeadline": bl_b_before},
            "baselineB_tunedMultiplier": {
                **bl_b_k, "k": round(k, 4), "beforeDeadline": bl_b_k_before,
            },
            "note": "testBeforeDeadline — метрики на подвыборке проб, где момент оценки t ещё до дедлайна; это продуктовая метрика «успели предупредить заранее»",
        },
        "verdict": verdict,
        "verdictRule": "regression Wins, если при том же бюджете FPR ~20% recall выше бейзлайна B tuned минимум на 0.05 и F1 выше минимум на 0.03; иначе в продукт идёт бейзлайн B",
    }
    args.weights_out.write_text(json.dumps(weights, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Веса: {args.weights_out}")

    medians_out = {
        "source": "train split",
        "unit": "hours (resolved_at - opened_at), медиана",
        "bucketsCatPriGroup": {
            f"{cat}|{int(pri)}|{grp}": round(float(v), 4)
            for (cat, pri, grp), v in med_cpg.items()
        },
        "fallbackCatPri": {f"{cat}|{int(pri)}": round(float(v), 4) for (cat, pri), v in med_cp.items()},
        "fallbackPriority": {str(int(p)): round(float(v), 4) for p, v in med_p.items()},
        "chain": "cat_pri_grp -> cat_pri -> priority",
    }
    args.medians_out.write_text(json.dumps(medians_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Медианы: {args.medians_out}")


if __name__ == "__main__":
    main()
