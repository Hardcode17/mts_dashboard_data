#!/usr/bin/env python3
"""Вернуть локальный менторский стенд MCC к сохранённому началу показа."""

from contextlib import ExitStack
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen
from uuid import uuid4


base = Path.home() / ".local/share/mcc-local"
mentor = base / "mentor"
examples = mentor / "examples"
seed = examples / "initial"
live = "mcc_mentor_live"
pg = [shutil.which("docker") or "/usr/local/bin/docker", "--context", "orbstack", "exec", "-i", "mcc-local-postgres"]
domain = f"gui/{os.getuid()}"
jobs = ["mentor-ai", "mentor-web", "mentor-itsm", "mentor-backend"]


def sql(db, query):
    return subprocess.check_output(
        pg + ["psql", "-XAt", "-v", "ON_ERROR_STOP=1", "-U", "dashboard", "-d", db, "-c", query],
        text=True,
    ).strip()


def rename_db(old, new):
    for _ in range(50):
        if sql("postgres", f"SELECT count(*) FROM pg_stat_activity WHERE datname='{old}'") == "0":
            sql("postgres", f"ALTER DATABASE {old} RENAME TO {new}")
            return
        time.sleep(0.2)
    raise RuntimeError(f"В базе {old} остались подключения. Закройте соединение и повторите.")


def verify(db):
    counts = sql(db, "SELECT source_id, count(*) FROM itsm_incident GROUP BY 1 ORDER BY 1")
    if counts.splitlines() != ["mentor-background|44", "mentor-login-examples|100", "mentor-session-4374127057|1"]:
        raise RuntimeError(f"Неверный состав начального снимка в {db}: {counts}")
    crits = sql(db, """SELECT count(*) FROM itsm_ingest_batch b CROSS JOIN LATERAL
        jsonb_array_elements(convert_from(b.payload,'UTF8')::jsonb) r WHERE r->>'priorityId'='4'""")
    if crits != "0":
        raise RuntimeError("Начальная копия содержит критический INC")


def stop(svc):
    name = f"{domain}/local.mcc.hackathon.{svc}"
    if subprocess.run(["launchctl", "print", name], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", name], check=True)
        for _ in range(100):
            if subprocess.run(["launchctl", "print", name], capture_output=True).returncode:
                return
            time.sleep(0.1)
        raise RuntimeError(f"Служба ещё работает: {svc}")


def start(svc):
    file = base / "run" / f"local.mcc.hackathon.{svc}.plist"
    for _ in range(30):
        if subprocess.run(["launchctl", "bootstrap", domain, str(file)], capture_output=True).returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError(f"Не удалось запустить {svc}")


def wait_for(url, field, expected):
    for _ in range(80):
        try:
            with urlopen(url, timeout=2) as reply:
                if json.load(reply).get(field) == expected:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Проверка после сброса не прошла: {url}")


def publish_background_snapshot():
    with urlopen("http://127.0.0.1:8093/api/mock/emit", data=b"", timeout=30) as reply:
        delivery = json.load(reply)
    version = delivery.get("deliveredVersion")
    if delivery.get("sourceId") != "mentor-background" or type(version) is not int or version < 1:
        raise RuntimeError("Фоновая ITSM не подтвердила отправку снимка.")
    for _ in range(60):
        status = sql(live, f"""SELECT status FROM itsm_ingest_batch
            WHERE source_id = 'mentor-background' AND source_version = {version}""")
        if status == "SUCCEEDED":
            return
        if status == "FAILED":
            break
        time.sleep(0.5)
    raise RuntimeError(f"Spring не применил фоновый снимок версии {version}. Статус: {status}")


def reset():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    archive = mentor / "archives" / stamp
    archive.mkdir(parents=True, mode=0o700)
    staging, previous = f"mcc_mentor_ready_{stamp}", f"mcc_mentor_before_{stamp}"
    sql("postgres", f"CREATE DATABASE {staging} OWNER dashboard")
    with (seed / "baseline.dump").open("rb") as dump:
        subprocess.run(pg + ["pg_restore", "-U", "dashboard", "-d", staging, "--exit-on-error"],
                       stdin=dump, check=True)
    verify(staging)
    # Prepare fresh files before stopping any services. Preserve whole SQLite directories with WAL.
    ready = archive / "ready"
    ready.mkdir()
    shutil.copy2(seed / "itsm-state.json", ready / "itsm-state.json")
    shutil.copy2(seed / "local.postman_environment.json", ready / "local.postman_environment.json")
    (ready / "ai-workspace").mkdir()
    shutil.copy2(seed / "ai.sqlite", ready / "ai-workspace/workspace.sqlite")
    (ready / "mentor-release-web").mkdir()
    paths = [mentor / "itsm-state.json", mentor / "local.postman_environment.json",
             examples / "ai-workspace", base / "data/mentor-release-web"]

    with ExitStack() as rollback:
        for svc in jobs:
            stop(svc)
            rollback.callback(start, svc)
        with ExitStack() as files:
            # Reverse a partial database switch if either a rename or a subsequent startup fails.
            rename_db(live, previous)
            files.callback(rename_db, previous, live)
            rename_db(staging, live)
            files.callback(rename_db, live, staging)
            for path in paths:
                if path.exists():
                    path.rename(archive / path.name)
                    files.callback((archive / path.name).rename, path)
                (ready / path.name).rename(path)
                files.callback(path.rename, ready / path.name)
            with ExitStack() as started:
                for svc in reversed(jobs):
                    start(svc)
                    started.callback(stop, svc)
                wait_for("http://localhost:3000/health", "status", "UP")
                wait_for("http://127.0.0.1:8093/api/mock/status", "sourceId", "mentor-background")
                wait_for("http://localhost:5187/auth/config", "realm", "dashboard")
                publish_background_snapshot()
                verify(live)
                started.pop_all()
            files.pop_all()
        rollback.pop_all()
    print(f"Готово. Откройте заново вкладки стенда. Предыдущий прогон: {previous} и {archive}")


if __name__ == "__main__":
    os.umask(0o077)
    if sys.argv[1:] not in ([], ["--check"]):
        sys.exit("Доступна проверка --check или сброс без аргументов.")
    with (mentor / "send.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in ("baseline.dump", "ai.sqlite", "itsm-state.json", "local.postman_environment.json"):
            if not (seed / name).is_file():
                sys.exit(f"Не найден файл начального состояния: {name}")
        with sqlite3.connect(f"file:{seed / 'ai.sqlite'}?mode=ro", uri=True) as db:
            meta = json.loads(db.execute("SELECT body FROM workspace WHERE mode='case'").fetchone()[0])
            reports = meta["work"]["INC000021940126"]["reports"]
            if not any("deadline_ms=1500" in json.dumps(r) for r in reports):
                sys.exit("В начальной копии отсутствует разбор ИИ.")
        if sys.argv[1:]:
            verify((seed / "seed-db.txt").read_text().strip())
            with (seed / "baseline.dump").open("rb") as dump:
                subprocess.run(pg + ["pg_restore", "-l"], stdin=dump, stdout=subprocess.DEVNULL, check=True)
            print("Копия проверена: доставка, 100 INC, 44 фоновых карточки и разбор ИИ. Состояние стенда сохранено.")
        else:
            reset()
