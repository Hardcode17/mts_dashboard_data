"""Build the reviewable MCP v1 schema bundle and synthetic examples (stdlib only)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA = "https://json-schema.org/draft/2020-12/schema"


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def arr(items, maximum=100):
    return {"type": "array", "items": items, "maxItems": maximum}


def ref(name):
    return {"$ref": "#/$defs/" + name}


def nullable(schema):
    return {"anyOf": [schema, {"type": "null"}]}


def enum(*values):
    return {"enum": list(values)}


TEXT = {"type": "string", "minLength": 1, "maxLength": 500}
ID = {"type": "string", "minLength": 1, "maxLength": 200}
INC = {"type": "string", "pattern": "^INC[0-9]{6,18}$"}
SEQ = {"type": "string", "pattern": "^(0|[1-9][0-9]{0,18})$"}
UTC = {"type": "string", "format": "date-time", "pattern": "Z$"}
TOKEN = {"type": "string", "minLength": 1, "maxLength": 2048}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
BOOL = {"type": "boolean"}
STATE = enum("Assigned", "WorkInProgress", "Research", "Resolved", "Closed")

defs = {
    "source": obj({"sourceId": ID, "asOf": nullable(UTC),
                   "status": enum("fresh", "stale", "unavailable")}),
    "meta": obj({"generatedAt": UTC, "sources": {**arr(ref("source"), 10), "minItems": 1},
                 "completeness": enum("complete", "partial"), "warnings": arr(TEXT, 10)}),
    "incident": obj({"incidentId": INC, "title": TEXT, "state": STATE,
                     "priorityId": {"type": "integer", "minimum": 1, "maximum": 4},
                     "serviceId": nullable(ID), "siteId": nullable(ID),
                     "assignedGroupId": nullable(ID), "assignedPersonId": nullable(ID),
                     "deadline": nullable(UTC), "updatedAt": nullable(UTC)}),
    "evidence": obj({"kind": enum("history", "directory"), "sourceRef": ID,
                     "observedAt": UTC, "summary": TEXT}),
    "error": obj({"code": enum("INVALID_ARGUMENT", "NOT_FOUND", "ACCESS_DENIED",
                               "SOURCE_UNAVAILABLE", "CURSOR_EXPIRED", "INVALID_CURSOR",
                               "RATE_LIMITED", "UNSUPPORTED_FILTER"),
                  "message": TEXT, "retryable": BOOL,
                  "recovery": enum("none", "retry", "resync_snapshot", "reauthenticate")}),
}
defs["card"] = obj({**defs["incident"]["properties"],
                    "description": nullable({"type": "string", "maxLength": 4000}),
                    "solution": nullable({"type": "string", "maxLength": 2000})})
defs["filters"] = obj({"query": {"type": "string", "minLength": 1, "maxLength": 200},
                       "states": {**arr(STATE, 5), "minItems": 1, "uniqueItems": True},
                       "priorityIds": {**arr({"type": "integer", "minimum": 1, "maximum": 4}, 4),
                                       "minItems": 1, "uniqueItems": True},
                       "serviceId": ID, "siteId": ID, "assignedGroupId": ID,
                       "unassigned": BOOL}, [])
defs["event"] = obj({"eventId": {"type": "string", "pattern": "^inc-change:[0-9a-f-]{36}:[1-9][0-9]{0,18}$"},
                     "eventSeq": {"type": "string", "pattern": "^[1-9][0-9]{0,18}$"}, "sourceId": ID, "incidentId": INC,
                     "sourceVersion": SEQ, "changeRevision": SEQ,
                     "kind": enum("ADDED", "UPDATED", "MISSING", "RETURNED"),
                     "observedAt": UTC, "sourceTime": nullable(UTC),
                     "changedFields": {**arr(enum("stateEnum", "assignedGroupName", "assignedEmployee",
                                                  "priorityId", "deadline", "solution"), 6),
                                       "uniqueItems": True}})
defs["analogue"] = obj({"incident": ref("incident"), "resolutionSummary": TEXT,
                        "matchedSignals": arr(TEXT, 10), "differences": arr(TEXT, 10),
                        "historicalResolverGroupId": nullable(ID), "evidence": ref("evidence")})
defs["group"] = obj({"groupId": ID, "name": TEXT,
                     "responsibility": enum("confirmed", "historical", "unknown"),
                     "evidence": arr(ref("evidence"), 10)})
defs["group"]["allOf"] = [{"if": {"properties": {"responsibility": {"const": "confirmed"}}},
                            "then": {"properties": {"evidence": {"contains": {
                                "properties": {"kind": {"const": "directory"}}, "required": ["kind"]}}}}}]
defs["assignee"] = obj({"personId": ID, "displayName": TEXT, "groupId": ID,
                        "competence": enum("confirmed", "unknown"),
                        "onDuty": enum("yes", "no", "unknown"), "evidence": arr(ref("evidence"), 10)})
defs["assignee"]["allOf"] = [{"if": {"anyOf": [
    {"properties": {"competence": {"const": "confirmed"}}},
    {"properties": {"onDuty": {"enum": ["yes", "no"]}}}]},
    "then": {"properties": {"evidence": {"contains": {
        "properties": {"kind": {"const": "directory"}}, "required": ["kind"]}}}}}]


def output(data):
    return {"type": "object", "oneOf": [
        obj({"contractVersion": {"const": "1.0.0"}, "ok": {"const": True},
             "data": data, "meta": ref("meta")}),
        obj({"contractVersion": {"const": "1.0.0"}, "ok": {"const": False},
             "error": ref("error")})]}


def page(items):
    return obj({"items": arr(items), "nextCursor": nullable(TOKEN)})


inputs = {
    "list_incidents": obj({"filters": ref("filters"), "limit": LIMIT, "cursor": TOKEN}, []),
    "get_incident": obj({"incidentId": INC}),
    "list_incident_changes": obj({"cursor": TOKEN, "limit": LIMIT}, ["cursor"]),
    "find_similar_incidents": obj({"incidentId": INC, "filters": ref("filters"),
                                   "limit": LIMIT, "cursor": TOKEN}, ["incidentId"]),
    "find_resolver_groups": obj({"incidentId": INC, "limit": LIMIT, "cursor": TOKEN}, ["incidentId"]),
    "find_assignees": obj({"groupId": ID, "query": {"type": "string", "minLength": 1, "maxLength": 200},
                           "limit": LIMIT, "cursor": TOKEN}, ["groupId"]),
}
outputs = {
    "list_incidents": output(obj({**page(ref("incident"))["properties"],
                                  "snapshotId": TOKEN, "checkpoint": TOKEN})),
    "get_incident": output(ref("card")),
    "list_incident_changes": output(obj({"events": arr(ref("event")), "nextCursor": TOKEN, "hasMore": BOOL})),
    "find_similar_incidents": output(page(ref("analogue"))),
    "find_resolver_groups": output(obj({**page(ref("group"))["properties"],
                                        "recommendedGroupId": nullable(ID),
                                        "reason": enum("confirmed_owner", "no_owner", "ambiguous_owner",
                                                       "directory_stale", "directory_unavailable", "already_assigned")})),
    "find_assignees": output(page(ref("assignee"))),
}
group_data = outputs["find_resolver_groups"]["oneOf"][0]["properties"]["data"]
group_data["allOf"] = [{
    "if": {"properties": {"reason": {"const": "confirmed_owner"}}},
    "then": {"properties": {"recommendedGroupId": ID}},
    "else": {"properties": {"recommendedGroupId": {"type": "null"}}},
}]
bundle = {"$schema": SCHEMA, "$id": "urn:mts-dashboard:mcp:v1", "$defs": defs}
for name in inputs:
    defs[name + "Input"] = inputs[name]
    defs[name + "Output"] = outputs[name]

now = "2026-11-22T09:00:00Z"
incident = {"incidentId": "INC000001", "title": "Синтетический сбой оплаты", "state": "Assigned",
            "priorityId": 4, "serviceId": "service-demo", "siteId": "site-demo",
            "assignedGroupId": None, "assignedPersonId": None, "deadline": None, "updatedAt": now}
meta = {"generatedAt": now, "sources": [{"sourceId": "itsm-demo", "asOf": now, "status": "fresh"}],
        "completeness": "complete", "warnings": []}


def success(data):
    return {"contractVersion": "1.0.0", "ok": True, "data": data, "meta": meta}


examples = [
    {"tool": "list_incidents", "input": {"filters": {"unassigned": True}, "limit": 1},
     "output": success({"items": [incident], "nextCursor": None,
                        "snapshotId": "demo-snapshot", "checkpoint": "demo-checkpoint-100"})},
    {"tool": "get_incident", "input": {"incidentId": "INC000001"},
     "output": success({**incident, "description": "Только тестовые данные", "solution": None})},
    {"tool": "list_incident_changes", "input": {"cursor": "demo-checkpoint-100", "limit": 1},
     "output": success({"events": [{"eventId": "inc-change:00000000-0000-4000-8000-000000000001:101",
                                    "eventSeq": "101", "sourceId": "itsm-demo", "incidentId": "INC000001",
                                    "sourceVersion": "7", "changeRevision": "3", "kind": "ADDED",
                                    "observedAt": now, "sourceTime": None, "changedFields": ["priorityId"]}],
                        "nextCursor": "demo-checkpoint-101", "hasMore": True})},
    {"tool": "find_similar_incidents", "input": {"incidentId": "INC000001"},
     "output": success({"items": [{"incident": {**incident, "incidentId": "INC000002", "state": "Closed"},
                                   "resolutionSummary": "Восстановлен тестовый сервис", "matchedSignals": ["Оплата"],
                                   "differences": ["Другая площадка"], "historicalResolverGroupId": "group-old",
                                   "evidence": {"kind": "history", "sourceRef": "itsm:INC000002",
                                                "observedAt": now, "summary": "Решение завершённого случая"}}],
                        "nextCursor": None})},
    {"tool": "find_resolver_groups", "input": {"incidentId": "INC000001"},
     "output": success({"items": [{"groupId": "group-demo", "name": "Тестовая группа",
                                   "responsibility": "confirmed", "evidence": [{"kind": "directory",
                                   "sourceRef": "directory:demo:1", "observedAt": now, "summary": "Сервис × площадка"}]}],
                        "nextCursor": None, "recommendedGroupId": "group-demo", "reason": "confirmed_owner"})},
    {"tool": "find_assignees", "input": {"groupId": "group-demo"},
     "output": success({"items": [{"personId": "person-demo", "displayName": "Тестовый исполнитель",
                                   "groupId": "group-demo", "competence": "unknown", "onDuty": "unknown",
                                   "evidence": []}], "nextCursor": None})},
]


def write(name, value):
    (ROOT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write("contract.schema.json", bundle)
    write("examples.json", examples)
