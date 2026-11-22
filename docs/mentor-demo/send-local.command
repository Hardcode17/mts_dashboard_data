#!/usr/bin/env python3
"""Отправить подготовленный снимок в локальный Spring через Postman CLI."""

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from tempfile import TemporaryDirectory


def main():
    baseline = sys.argv[1:] == ["--baseline"]
    if sys.argv[1:] and not baseline:
        raise SystemExit("Для подготовки используйте --baseline. Для показа запустите без аргументов.")
    base = Path.home() / ".local/share/mcc-local"
    folder = base / "mentor"
    environment = folder / "local.postman_environment.json"
    collection = Path(__file__).resolve().with_name("mcc.postman_collection.json")
    if not environment.is_file():
        raise SystemExit(f"Не найдено подготовленное окружение {environment}")

    with (folder / "send.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads(environment.read_text())
        fields = {field["key"]: field for field in config["values"]}
        session = json.loads(fields["session_state"]["value"])
        if not baseline and not session.get("baselineApplied"):
            raise SystemExit("Сначала запустите send-local.command --baseline.")

        def save_session():
            fields["session_state"]["value"] = json.dumps(session)
            updated = environment.with_suffix(".tmp")
            updated.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
            updated.replace(environment)

        if not baseline and not session.get("criticalAt"):
            session["criticalAt"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            save_session()
        request = "01 · Исходный снимок" if baseline else "02 · Новый критический INC"
        suffix = 1 if baseline else session.get("firstVersion", 1) + 1
        inc = f"INC9{session['tag']}{suffix}"
        print(f"Локальный стенд · {request} · {inc}", flush=True)
        app = Path("/Applications/Postman.app/Contents")
        cli = app / "Resources/data/postman-cli/bin/postman"
        with TemporaryDirectory(dir=folder) as temp:
            keys = json.loads((base / "keys/runtime.json").read_text())
            fields["itsm_client_secret"]["value"] = keys["ITSM_SENDER_CLIENT_SECRET"]
            private_env = Path(temp) / "request.postman_environment.json"
            private_env.write_text(json.dumps(config))
            fields["itsm_client_secret"]["value"] = ""
            result = subprocess.run(
                [str(cli), "collection", "run", str(collection), "-e", str(private_env),
                 "-i", request, "--no-report-events", "--bail"],
                env={**os.environ, "POSTMAN_CLI_ELECTRON": str(app / "MacOS/Postman")},
                capture_output=True, text=True,
            )
        print(result.stdout)
        if result.returncode or f"Pass  SUCCEEDED · {inc}" not in result.stdout:
            raise SystemExit("Применение не подтверждено. Проверьте доступность локального Spring и Keycloak.")
        if baseline:
            session["baselineApplied"] = True
            save_session()
        print(f"Применено. http://localhost:3000/#incident/{inc}")


if __name__ == "__main__":
    os.umask(0o077)
    main()
