"""Loopback-only HTTP adapter for the KAN-74 routing advisor.

The adapter is deliberately small: it reuses ``advisor.py`` for the embedding
model, the corpus check, the pgvector query and the analogue/hand-off policy.
It never writes to PostgreSQL and never talks to ITSM.
"""
import argparse
import json
import os
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import advisor

HOST = "127.0.0.1"
DEFAULT_PORT = 8747
MAX_BODY = 8 * 1024
MAX_SYMPTOM = 1000
MAX_VALUE = 200
MAX_INFRASTRUCTURE = 8
MAX_CURRENT_GROUP = 200
JSON_TYPE = "application/json"

# Query-only statements. The connection itself is also opened read-only.
CATALOGUE_SQL = {
    "service": """
        SELECT value FROM (
            SELECT service AS value FROM routing_advisor.history
            UNION
            SELECT service AS value FROM routing_advisor.owners
        ) AS values
        WHERE btrim(value) <> ''
        ORDER BY value
    """,
    "site": """
        SELECT value FROM (
            SELECT site AS value FROM routing_advisor.history
            UNION
            SELECT site AS value FROM routing_advisor.owners
        ) AS values
        WHERE btrim(value) <> ''
        ORDER BY value
    """,
    "infrastructure": """
        SELECT DISTINCT btrim(value) AS value
        FROM routing_advisor.history, unnest(infrastructure) AS value
        WHERE btrim(value) <> ''
        ORDER BY value
    """,
}


class RequestError(Exception):
    """A client-visible 400/413/415/411 error."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def text_value(payload, name, *, required=False):
    value = payload.get(name)
    if value is None:
        if required:
            raise RequestError(HTTPStatus.BAD_REQUEST, f"{name} is required")
        return None
    if not isinstance(value, str):
        raise RequestError(HTTPStatus.BAD_REQUEST, f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"{name} must not be empty")
    if len(value) > (MAX_SYMPTOM if name == "symptom" else MAX_CURRENT_GROUP if name == "current_group" else MAX_VALUE):
        raise RequestError(HTTPStatus.BAD_REQUEST, f"{name} is too long")
    return value or None


def infrastructure_value(payload):
    value = payload.get("infrastructure")
    if value is None or value == "":
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise RequestError(HTTPStatus.BAD_REQUEST, "infrastructure must be a string or an array of strings")
    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise RequestError(HTTPStatus.BAD_REQUEST, "infrastructure must be a string or an array of strings")
        item = item.strip()
        if not item:
            continue
        if len(item) > MAX_VALUE:
            raise RequestError(HTTPStatus.BAD_REQUEST, "infrastructure value is too long")
        if item not in normalized:
            normalized.append(item)
    if len(normalized) > MAX_INFRASTRUCTURE:
        raise RequestError(HTTPStatus.BAD_REQUEST, f"at most {MAX_INFRASTRUCTURE} infrastructure values are allowed")
    return normalized


def search_payload(payload):
    """Validate a POST /search object and turn it into an advisor query."""
    if not isinstance(payload, dict):
        raise RequestError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
    allowed = {"incident_id", "symptom", "service", "site", "infrastructure", "current_group"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise RequestError(HTTPStatus.BAD_REQUEST, "unknown fields: " + ", ".join(unknown))

    incident_id = text_value(payload, "incident_id", required=True)
    symptom = text_value(payload, "symptom", required=True)
    service = text_value(payload, "service")
    site = text_value(payload, "site")
    current_group = text_value(payload, "current_group")
    infrastructure = infrastructure_value(payload)
    if not service and not infrastructure:
        raise RequestError(HTTPStatus.BAD_REQUEST, "select a service or an infrastructure type")

    return {
        "incident_id": incident_id,
        "service": service or "",
        "site": site or "",
        "infrastructure": infrastructure,
        "current_group": current_group,
        "symptom": symptom,
    }


def read_catalogue(conn):
    return {
        name: [row["value"] for row in conn.execute(sql).fetchall()]
        for name, sql in CATALOGUE_SQL.items()
    }


def validate_catalogue(query, catalogue):
    checks = (
        ("service", query["service"], "service"),
        ("site", query["site"], "site"),
        ("infrastructure", query["infrastructure"], "infrastructure"),
    )
    for field, value, allowed_name in checks:
        values = value if isinstance(value, list) else [value]
        for item in values:
            if item and item not in catalogue[allowed_name]:
                raise RequestError(HTTPStatus.BAD_REQUEST, f"{field} is not in the catalogue: {item}")


def read_only_connection(dsn):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(
        dsn,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on",
    )


def begin_read(conn):
    # SET TRANSACTION must be the first statement of the transaction.
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")


def search(conn, query, embedder, threshold):
    [embedding] = embedder.encode(
        [{"title": query["symptom"], "description": ""}],
        "query",
    )
    advisor_query = {
        "id": query["incident_id"],
        "service": query["service"],
        "site": query["site"],
        "title": query["symptom"],
        "description": "",
        "infrastructure": query["infrastructure"],
        "current_group": query["current_group"],
    }
    result, audit = advisor.search(conn, advisor_query, embedding, threshold)
    result["candidate_count"] = len(audit)
    return result


def error_payload(message):
    return {"error": message}


class RoutingAdvisorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, *, dsn, embedder, model_id, threshold):
        super().__init__(address, handler)
        self.dsn = dsn
        self.embedder = embedder
        self.model_id = model_id
        self.threshold = threshold
        self.search_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "KAN74RoutingAdvisor/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Default access logging is useful; request bodies are never logged.
        super().log_message(fmt, *args)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_TYPE + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != JSON_TYPE:
            raise RequestError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise RequestError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from exc
        if length < 1:
            raise RequestError(HTTPStatus.BAD_REQUEST, "request body is empty")
        if length > MAX_BODY:
            raise RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"request body exceeds {MAX_BODY} bytes")
        body = self.rfile.read(length)
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError(HTTPStatus.BAD_REQUEST, "request body must be valid UTF-8 JSON") from exc

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json(HTTPStatus.OK, {"status": "ok"})
            return
        if path == "/search":
            self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, error_payload("метод не поддерживается"))
            return
        if path != "/catalogue":
            self.send_json(HTTPStatus.NOT_FOUND, error_payload("не найдено"))
            return
        try:
            with read_only_connection(self.server.dsn) as conn:
                begin_read(conn)
                catalogue = read_catalogue(conn)
            self.send_json(HTTPStatus.OK, catalogue)
        except Exception as exc:  # pragma: no cover - exercised with a real database
            self.log_error("catalogue failed: %s", type(exc).__name__)
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, error_payload("база данных временно недоступна"))

    def unsupported(self):
        self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, error_payload("метод не поддерживается"))

    do_PUT = unsupported
    do_PATCH = unsupported
    do_DELETE = unsupported
    do_OPTIONS = unsupported

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/catalogue":
            self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, error_payload("метод не поддерживается"))
            return
        if path != "/search":
            self.send_json(HTTPStatus.NOT_FOUND, error_payload("не найдено"))
            return
        try:
            payload = search_payload(self.read_json())
            with read_only_connection(self.server.dsn) as conn:
                begin_read(conn)
                catalogue = read_catalogue(conn)
                validate_catalogue(payload, catalogue)
                with self.server.search_lock:
                    result = search(conn, payload, self.server.embedder, self.server.threshold)
            self.send_json(HTTPStatus.OK, result)
        except RequestError as exc:
            self.send_json(exc.status, error_payload(exc.message))
        except Exception as exc:  # pragma: no cover - exercised with a real database/model
            self.log_error("search failed: %s", type(exc).__name__)
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, error_payload("локальный поиск временно недоступен"))


def warm_up(embedder):
    embedder.encode([{"title": "проверка локального маршрутизатора", "description": ""}], "query")


def check_corpus(dsn, model_id):
    with read_only_connection(dsn) as conn:
        begin_read(conn)
        advisor.check_seed(conn, model_id)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("ROUTING_PORT", DEFAULT_PORT)))
    parser.add_argument("--threshold", type=float, default=advisor.DEFAULT_THRESHOLD)
    args = parser.parse_args(argv)
    if not 0 <= args.threshold <= 1:
        parser.error("--threshold must be between 0 and 1")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main(argv=None):
    args = parse_args(argv)
    dsn = os.environ.get("ROUTING_DSN")
    if not dsn:
        print("Routing advisor API: set ROUTING_DSN to a dedicated prototype database", file=sys.stderr)
        return 2
    model_path = os.environ.get("ROUTING_MODEL", advisor.MODEL)
    try:
        model_id = advisor.embedding_model_id(model_path)
        embedder = advisor.E5Embedder(model_path)
        warm_up(embedder)
        check_corpus(dsn, model_id)
    except (ValueError, OSError, KeyError) as exc:
        print(f"Routing advisor API: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # psycopg / model failures during startup
        print(f"Routing advisor API: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    server = RoutingAdvisorServer(
        (HOST, args.port),
        Handler,
        dsn=dsn,
        embedder=embedder,
        model_id=model_id,
        threshold=args.threshold,
    )
    print(f"Routing advisor API: http://{HOST}:{args.port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
