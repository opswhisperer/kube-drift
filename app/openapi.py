"""OpenAPI 3.1 description of the HTTP API, served at /openapi.json.

Kept by hand next to the handlers in main.py; tests/test_openapi.py checks that every route
is documented and that live responses only use documented fields.
"""
from __future__ import annotations

_ERROR = {"description": "Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}


def _json(schema_ref: str, description: str = "OK") -> dict:
    return {"description": description, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema_ref}"}}}}


def _body(schema: dict) -> dict:
    return {"required": True, "content": {"application/json": {"schema": schema}}}


_IDS = {
    "type": "array", "minItems": 1, "maxItems": 1000, "items": {"type": "string"},
    "description": "Orphan ids, `Kind/namespace/name` as in OrphanItem.id; cluster-scoped objects have "
                   "an empty namespace (`ClusterRoleBinding//old-binding`).",
}

# Narrow `components` only; every other field, `summary` included, stays as without them.
_DRIFT_FILTERS = [
    {"name": "status", "in": "query", "required": False, "style": "form", "explode": True,
     "description": "Only components with this status. Repeat it or comma-separate values for any of several.",
     "schema": {"type": "array", "items": {"enum": ["outdated", "current", "unknown", "error"]}}},
    {"name": "ignored", "in": "query", "required": False, "schema": {"type": "boolean"},
     "description": "`false`: leave out components whose update is ignored (`ignored` set, also through their "
                    "release). `true`: only those."},
    {"name": "top", "in": "query", "required": False, "schema": {"type": "boolean"},
     "description": "`true`: leave out Helm-managed workloads (components with `release`), which `summary` counts "
                    "through their release. `false`: only those."},
]

SPEC: dict = {
    "openapi": "3.1.0",
    "info": {
        "title": "kube-drift",
        "version": "1",
        "summary": "Version drift and orphaned resources in a Kubernetes cluster.",
        "description": (
            "kube-drift scans one Kubernetes cluster on a timer and reports two things:\n\n"
            "- **Version drift** (`GET /api/drift`): every installed component — control plane, nodes, "
            "Helm releases, workloads — with the newest upstream version and a status.\n"
            "- **Orphaned resources** (`GET /api/orphans`): objects nothing uses, found by kor and "
            "cross-checked, with ignore state applied.\n\n"
            "The API never changes the cluster. POST endpoints only change kube-drift's own state "
            "(ignores, ignore rules, image and Helm rules, rescans, API keys) or return text: `/api/orphans/commands` returns a kubectl "
            "script for a human to review and run. Responses never contain Secret data.\n\n"
            "**Authentication** depends on how kube-drift is deployed:\n\n"
            "- **Built-in sign-in** (`auth.oidc` in its config; `GET /auth/me` says `\"auth\": \"oidc\"`): send an "
            "API key as `Authorization: Bearer kd_…`. People make and revoke keys in the dashboard (API keys) "
            "after signing in; the dashboard itself uses a session cookie. Without either, `/api/…` answers 401.\n"
            "- **No built-in sign-in** (`\"auth\": \"none\"`): kube-drift must only be reachable through a gateway "
            "that authenticates every request, and any credentials you use belong to that gateway. If you "
            "can call this API without having authenticated anywhere, the deployment is exposed — tell its "
            "operator.\n\n"
            "Requests with a body must use `Content-Type: application/json`.\n\n"
            "Scans take time: check `scanning` and `scanned_at`, and after `POST /api/rescan` poll the "
            "GET endpoint until `scanning` is false and `scanned_at` has changed."
        ),
        "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
    },
    "tags": [
        {"name": "versions", "description": "Installed vs available versions."},
        {"name": "orphans", "description": "Unused resources, ignores and cleanup scripts."},
        {"name": "config", "description": "The config in effect, and image and Helm rules kept by kube-drift."},
        {"name": "service", "description": "Scans, health and this document."},
        {"name": "auth", "description": "Sign-in and API keys; only when built-in sign-in (`auth.oidc`) is on."},
        {"name": "renovate", "description": "Renovate's report in, update approvals out (to the forge); only with "
                                            "`renovate.enabled` in the config. Off: 404."},
    ],
    # Either one; open endpoints say `security: []`. With built-in sign-in off, the gateway decides.
    "security": [{"apiKey": []}, {"session": []}],
    "paths": {
        "/api/drift": {"get": {
            "tags": ["versions"], "operationId": "getVersionDrift",
            "summary": "Version scan: every component with installed and available versions",
            "description": "Components are sorted with `outdated` first. `summary` counts components by status.\n\n"
                           "The query parameters narrow `components` and nothing else; they combine with AND. "
                           "`?status=outdated&ignored=false&top=true` returns exactly the components `summary.outdated` "
                           "counts, in the same order. An unknown parameter or value is a 400, not ignored.",
            "parameters": _DRIFT_FILTERS,
            "responses": {"200": _json("VersionScan"), "400": _ERROR},
        }},
        "/api/drift.csv": {"get": {
            "tags": ["versions"], "operationId": "getVersionDriftCsv",
            "summary": "Version scan as CSV",
            "description": "Takes the same filters as `GET /api/drift`.",
            "parameters": _DRIFT_FILTERS,
            "responses": {"200": {"description": "CSV with a header row", "content": {"text/csv": {"schema": {"type": "string"}}}},
                          "400": _ERROR},
        }},
        "/api/drift/ignore": {"post": {
            "tags": ["versions"], "operationId": "ignoreUpdates",
            "summary": "Ignore components' updates for a while",
            "description": (
                "Hides these components' updates (and moves them out of `summary`'s status counts) until the "
                "first of the conditions given is met: `days` pass, the cluster's Kubernetes version changes, or "
                "a version newer than the one on offer now comes out (`until_newer`; for a floating tag, its image "
                "changes again). At least one is required. Ignoring a component again replaces its conditions."),
            "requestBody": _body({"type": "object", "required": ["ids"], "properties": {
                "ids": {"type": "array", "minItems": 1, "maxItems": 1000, "items": {"type": "string"},
                        "description": "Component ids, as in Component.id."},
                "days": {"type": "integer", "minimum": 1, "maximum": 3650},
                "until_kube_change": {"type": "boolean", "description": "End when the control plane's version changes."},
                "until_newer": {"type": "boolean", "description": "End when a newer version than the one on offer now is available."},
                "note": {"type": "string", "maxLength": 500}}}),
            "responses": {
                "200": {"description": "Ignored", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "ignored": {"type": "integer"},
                    "skipped": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}, "why": {"type": "string"}}},
                                "description": "Components `until_newer` can't track (no version or image digest)."},
                    "missing": {"type": "array", "items": {"type": "string"}, "description": "Ids not in the current scan."}}}}}},
                "400": _ERROR, "415": _ERROR, "503": _ERROR},
        }},
        "/api/drift/unignore": {"post": {
            "tags": ["versions"], "operationId": "unignoreUpdates",
            "summary": "Show components' updates again",
            "requestBody": _body({"type": "object", "required": ["ids"], "properties": {
                "ids": {"type": "array", "minItems": 1, "maxItems": 1000, "items": {"type": "string"}}}}),
            "responses": {
                "200": {"description": "Unignored", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "unignored": {"type": "integer"}}}}}},
                "400": _ERROR, "415": _ERROR, "503": _ERROR},
        }},
        "/api/renovate/report": {"post": {
            "tags": ["renovate"], "operationId": "postRenovateReport",
            "summary": "Take Renovate's report",
            "description": "The report file Renovate writes with `reportType: file` (`reportPath`), as it is. kube-drift "
                           "keeps each repository's dependencies and branches; a report for some repositories replaces "
                           "just those. Rows of `GET /api/drift` then carry `renovate`. Up to 64 MiB.",
            "requestBody": _body({"type": "object", "required": ["repositories"], "properties": {
                "repositories": {"type": "object", "description": "Renovate's report: repository → packageFiles, branches.",
                                 "additionalProperties": {"type": "object"}}}}),
            "responses": {
                "200": {"description": "Taken", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "repositories": {"type": "integer"}, "dependencies": {"type": "integer"},
                    "awaiting_approval": {"type": "integer", "description": "Updates waiting in a Dependency Dashboard."},
                    "received_at": {"type": "string", "format": "date-time"}}}}}},
                "400": _ERROR, "404": _ERROR, "415": _ERROR},
        }},
        "/api/renovate/approve": {"post": {
            "tags": ["renovate"], "operationId": "approveRenovateUpdate",
            "summary": "Approve an update Renovate is waiting on",
            "description": "Ticks the branch's box in the repository's open Dependency Dashboard issue on the forge "
                           "(`renovate.forge`), as a person would; Renovate opens the PR on its next run. Use "
                           "`renovate.repo` and `renovate.branch` of a row with `renovate.state: approve`. Nothing in "
                           "the cluster changes.",
            "requestBody": _body({"type": "object", "required": ["repo", "branch"], "properties": {
                "repo": {"type": "string", "description": "`owner/name`"}, "branch": {"type": "string"}}}),
            "responses": {
                "200": {"description": "Approved (or it already was)", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "status": {"enum": ["approved", "already approved"]},
                    "issue": {"type": "string", "description": "The dashboard issue's URL."}}}}}},
                "400": _ERROR, "404": _ERROR, "409": {**_ERROR, "description": "That branch isn't waiting for approval."},
                "415": _ERROR, "502": _ERROR,
                "503": {**_ERROR, "description": "No forge URL or token configured."}},
        }},
        "/api/orphans": {"get": {
            "tags": ["orphans"], "operationId": "getOrphans",
            "summary": "Orphan scan with ignores and ignore rules applied",
            "description": (
                "Every finding is returned, ignored ones included (they carry `ignored`). `summary.total` "
                "counts the findings that are not ignored. Act on `status`: `orphan` is a real candidate for "
                "cleanup; `in-use` means kor missed a reference and the object is needed; `managed` means an "
                "owner would recreate it; `protected` never gets a delete command."
            ),
            "responses": {"200": _json("OrphanScan")},
        }},
        "/api/orphans/ignore": {"post": {
            "tags": ["orphans"], "operationId": "ignoreOrphans",
            "summary": "Ignore specific findings",
            "description": "Hides these findings from the counts across rescans. To ignore everything like them, add a rule instead.",
            "requestBody": _body({"type": "object", "required": ["ids"], "properties": {
                "ids": _IDS, "note": {"type": "string", "maxLength": 500, "description": "Why this is fine."}}}),
            "responses": {
                "200": {"description": "Ignored", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "ignored": {"type": "integer"},
                    "missing": {"type": "array", "items": {"type": "string"}, "description": "Ids not in the current scan."}}}}}},
                "400": _ERROR, "415": _ERROR, "503": _ERROR},
        }},
        "/api/orphans/unignore": {"post": {
            "tags": ["orphans"], "operationId": "unignoreOrphans",
            "summary": "Undo ignoring specific findings",
            "description": "Only undoes per-finding ignores; findings hidden by a rule stay hidden until the rule is removed.",
            "requestBody": _body({"type": "object", "required": ["ids"], "properties": {"ids": _IDS}}),
            "responses": {
                "200": {"description": "Unignored", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "unignored": {"type": "integer"}}}}}},
                "400": _ERROR, "415": _ERROR, "503": _ERROR},
        }},
        "/api/orphans/rules": {"post": {
            "tags": ["orphans"], "operationId": "addIgnoreRule",
            "summary": "Add an ignore rule",
            "description": "Ignores every current and future finding the rule matches. Rules are listed in `GET /api/orphans` → `rules`, with match counts.",
            "requestBody": _body({"type": "object", "required": ["rule"], "properties": {
                "rule": {"$ref": "#/components/schemas/RuleMatch"},
                "note": {"type": "string", "maxLength": 500}}}),
            "responses": {
                "200": {"description": "Added", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "rule": {"$ref": "#/components/schemas/Rule"}}}}}},
                "400": _ERROR, "415": _ERROR, "503": _ERROR},
        }},
        "/api/orphans/rules/delete": {"post": {
            "tags": ["orphans"], "operationId": "removeIgnoreRule",
            "summary": "Remove an ignore rule added through the API or dashboard",
            "description": "Config-file rules (`from: config`) can't be removed here.",
            "requestBody": _body({"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}),
            "responses": {
                "200": {"description": "Removed", "content": {"application/json": {"schema": {"type": "object", "properties": {"removed": {"const": True}}}}}},
                "404": {"description": "No dashboard rule with that id", "content": {"application/json": {"schema": {"type": "object", "properties": {"removed": {"const": False}}}}}},
                "415": _ERROR, "503": _ERROR},
        }},
        "/api/orphans/commands": {"post": {
            "tags": ["orphans"], "operationId": "getCleanupCommands",
            "summary": "kubectl script to export, or back up and delete, findings",
            "description": (
                "Returns text only; nothing is changed. The script saves each object to a local JSON file "
                "(server-owned fields stripped so `kubectl apply -f` restores it) and, for `delete`, deletes "
                "it only after its backup succeeded. Needs kubectl and jq. Protected findings never get a "
                "delete line; ids not in the current scan are skipped. A human should review and run it."
            ),
            "requestBody": _body({"type": "object", "required": ["ids", "action"], "properties": {
                "ids": _IDS, "action": {"enum": ["export", "delete"]}}}),
            "responses": {
                "200": {"description": "Script", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "script": {"type": "string", "description": "Paste-able shell script; empty when nothing applies."},
                    "skipped": {"type": "array", "items": {"type": "object", "properties": {
                        "id": {"type": "string"}, "why": {"type": "string"}}}}}}}}},
                "400": _ERROR, "415": _ERROR},
        }},
        "/api/config": {"get": {
            "tags": ["config"], "operationId": "getConfig",
            "summary": "The config in effect: image and Helm rules, and everything else as read",
            "description": (
                "`images` and `helm` list the rules the version scan uses: the config file's, and the ones "
                "added through the dashboard or API (`from: dashboard`), which replace the file's entry for the "
                "same key. `components` are the ids that used each rule in the last scan. `settings` is the rest "
                "of the config file, read-only here. Values that look secret, probe headers and credentials in "
                "URLs are replaced with `(hidden)`; env var names are shown."),
            "responses": {"200": _json("Config")},
        }},
        "/api/config/rules": {"post": {
            "tags": ["config"], "operationId": "setConfigRule",
            "summary": "Add or replace an image or Helm rule",
            "description": (
                "Kept on kube-drift's volume and laid over the config file: for a key the file also has, it "
                "replaces that entry whole. Starts a version rescan (after the one running, if any). "
                "`dashboard_yaml` in `GET /api/config` has these rules as YAML to move into the config file."),
            "requestBody": _body({"type": "object", "required": ["section", "key", "rule"], "properties": {
                "section": {"enum": ["images", "helm"]},
                "key": {"type": "string", "maxLength": 300, "description": (
                    "images: the image without its tag (`ghcr.io/org/app`; `mongo` or `grafana/grafana` for Docker Hub). "
                    "helm: the release name, `release@namespace`, or the chart name. Either: `re:<regex>` to cover many.")},
                "rule": {"$ref": "#/components/schemas/ConfigRuleBody"}}}),
            "responses": {
                "200": {"description": "Saved", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "section": {"enum": ["images", "helm"]}, "key": {"type": "string"},
                    "rule": {"$ref": "#/components/schemas/ConfigRuleBody"},
                    "at": {"type": "string", "format": "date-time"}, "by": {"type": "string"}}}}}},
                "400": _ERROR, "415": _ERROR, "503": _ERROR},
        }},
        "/api/config/rules/delete": {"post": {
            "tags": ["config"], "operationId": "removeConfigRule",
            "summary": "Remove an image or Helm rule added through the dashboard or API",
            "description": "The config file's entry for that key, if any, applies again. Config-file rules can't be removed here. Starts a version rescan.",
            "requestBody": _body({"type": "object", "required": ["section", "key"], "properties": {
                "section": {"enum": ["images", "helm"]}, "key": {"type": "string"}}}),
            "responses": {
                "200": {"description": "Removed", "content": {"application/json": {"schema": {"type": "object", "properties": {"removed": {"const": True}}}}}},
                "404": {"description": "No dashboard rule with that key", "content": {"application/json": {"schema": {"type": "object", "properties": {"removed": {"const": False}}}}}},
                "415": _ERROR, "503": _ERROR},
        }},
        "/api/rescan": {"post": {
            "tags": ["service"], "operationId": "rescan",
            "summary": "Start a scan now",
            "description": "Returns at once; the scan runs in the background (versions: under a minute, orphans: a few minutes).",
            "parameters": [{"name": "scope", "in": "query", "required": False,
                            "schema": {"enum": ["all", "versions", "orphans"], "default": "all"}}],
            "responses": {"202": {"description": "Requested", "content": {"application/json": {"schema": {"type": "object", "properties": {
                "status": {"type": "string", "description": "`rescan requested`, or `already scanning: …`."},
                "scope": {"enum": ["all", "versions", "orphans"]}}}}}}},
        }},
        "/api/keys": {
            "get": {
                "tags": ["auth"], "operationId": "listApiKeys", "summary": "List API keys",
                "description": "Signed-in people only (not with an API key). Keys themselves are never returned, only their first characters.",
                "security": [{"session": []}],
                "responses": {"200": {"description": "Keys, newest first", "content": {"application/json": {"schema": {
                    "type": "object", "properties": {"keys": {"type": "array", "items": {"$ref": "#/components/schemas/ApiKey"}}}}}}},
                    "403": _ERROR, "404": {**_ERROR, "description": "Built-in sign-in is off"}},
            },
            "post": {
                "tags": ["auth"], "operationId": "createApiKey", "summary": "Create an API key",
                "description": "Signed-in people only. The key is in the response once; kube-drift stores only its SHA-256.",
                "security": [{"session": []}],
                "requestBody": _body({"type": "object", "required": ["name"], "properties": {
                    "name": {"type": "string", "maxLength": 100, "description": "What it's for."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 3650, "description": "Expires after this many days; leave out for never."}}}),
                "responses": {
                    "201": {"description": "Created", "content": {"application/json": {"schema": {"type": "object", "properties": {
                        "token": {"type": "string", "description": "The key, `kd_…`. Shown only here."},
                        "key": {"$ref": "#/components/schemas/ApiKey"}}}}}},
                    "400": _ERROR, "403": _ERROR, "404": {**_ERROR, "description": "Built-in sign-in is off"}, "415": _ERROR},
            },
        },
        "/api/keys/delete": {"post": {
            "tags": ["auth"], "operationId": "revokeApiKey", "summary": "Revoke an API key",
            "description": "Signed-in people only. Takes effect at once.",
            "security": [{"session": []}],
            "requestBody": _body({"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}),
            "responses": {
                "200": {"description": "Revoked", "content": {"application/json": {"schema": {"type": "object", "properties": {"revoked": {"const": True}}}}}},
                "404": {"description": "No such key, or built-in sign-in is off", "content": {"application/json": {"schema": {"type": "object", "properties": {
                    "revoked": {"const": False}, "error": {"type": "string"}}}}}},
                "403": _ERROR, "415": _ERROR},
        }},
        "/auth/me": {"get": {
            "tags": ["auth"], "operationId": "whoami", "summary": "Whether sign-in is on, and who is calling",
            "security": [],
            "responses": {"200": _json("Me")},
        }},
        "/auth/login": {"get": {
            "tags": ["auth"], "operationId": "login", "summary": "Start signing in (browser): redirects to the identity provider",
            "security": [],
            "parameters": [{"name": "next", "in": "query", "required": False, "schema": {"type": "string", "default": "/"},
                            "description": "Local path to return to afterwards."}],
            "responses": {"302": {"description": "To the identity provider"}, "404": _ERROR,
                          "502": {"description": "The identity provider can't be reached (HTML page)"}},
        }},
        "/auth/callback": {"get": {
            "tags": ["auth"], "operationId": "loginCallback", "summary": "Where the identity provider sends the browser back",
            "security": [],
            "responses": {"302": {"description": "Signed in; back to `next`"},
                          "400": {"description": "Sign-in failed (HTML page)"},
                          "403": {"description": "Not in an allowed group or email list (HTML page)"}, "404": _ERROR},
        }},
        "/auth/logout": {"post": {
            "tags": ["auth"], "operationId": "logout", "summary": "Sign out of kube-drift",
            "security": [],
            "responses": {"200": {"description": "Session cookie cleared", "content": {"application/json": {"schema": {"type": "object", "properties": {
                "redirect": {"type": "string", "description": "Where to send the browser: the identity provider's end-session page, or `/`."}}}}}},
                "404": _ERROR},
        }},
        "/healthz": {"get": {
            "tags": ["service"], "operationId": "healthz", "summary": "Liveness", "security": [],
            "responses": {"200": _json("Ok")},
        }},
        "/readyz": {"get": {
            "tags": ["service"], "operationId": "readyz", "summary": "Readiness: 503 until the first version scan has data",
            "security": [],
            "responses": {"200": _json("Ok"), "503": _json("Ok", "Not ready")},
        }},
        "/openapi.json": {"get": {
            "tags": ["service"], "operationId": "openapi", "summary": "This document", "security": [],
            "responses": {"200": {"description": "OpenAPI 3.1", "content": {"application/json": {"schema": {"type": "object"}}}}},
        }},
    },
    "components": {
    "securitySchemes": {
        "apiKey": {"type": "http", "scheme": "bearer", "bearerFormat": "kd_…",
                   "description": "An API key made in the dashboard (API keys), when built-in sign-in is on."},
        "session": {"type": "apiKey", "in": "cookie", "name": "kd_session",
                    "description": "Set by signing in at /auth/login in a browser."},
    },
    "schemas": {
        "Error": {"type": "object", "properties": {"error": {"type": "string"}}},
        "ApiKey": {"type": "object", "properties": {
            "id": {"type": "string"}, "name": {"type": "string"},
            "prefix": {"type": "string", "description": "The key's first characters, to recognise it."},
            "created_by": {"type": "string", "description": "Email (or name) of who made it."},
            "created_at": {"type": "string", "format": "date-time"},
            "expires_at": {"type": ["string", "null"], "format": "date-time"},
            "last_used_at": {"type": ["string", "null"], "format": "date-time", "description": "Saved every few minutes, so it may lag."},
        }},
        "Me": {"type": "object", "properties": {
            "auth": {"enum": ["oidc", "none"], "description": "`none`: no built-in sign-in; a gateway must authenticate."},
            "via": {"enum": ["session", "api-key", None], "description": "How this request authenticated; null if it didn't."},
            "user": {"type": ["object", "null"], "description": "Signed-in person.", "properties": {
                "sub": {"type": ["string", "null"]}, "name": {"type": ["string", "null"]}, "email": {"type": ["string", "null"]}}},
            "key": {"type": ["object", "null"], "description": "The API key used.", "properties": {
                "id": {"type": "string"}, "name": {"type": "string"}}},
        }},
        "Ok": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        "ScanState": {"type": "object", "properties": {
            "cluster": {"type": "string", "description": "Cluster name from the config."},
            "version": {"type": "string", "description": "kube-drift's own version: the release tag or `git describe` "
                        "it was built from; `dev` when run from source."},
            "scanning": {"type": "boolean", "description": "A scan is running now."},
            "scanned_at": {"type": ["string", "null"], "format": "date-time", "description": "When the data was produced; null before the first scan."},
            "duration_s": {"type": "number"},
            "next_scan_at": {"type": ["string", "null"], "format": "date-time"},
            "last_error": {"type": ["string", "null"], "description": "Why the last scan failed; the previous data is still served."},
            "generated_at": {"type": "string", "format": "date-time"},
        }},
        "VersionScan": {"allOf": [{"$ref": "#/components/schemas/ScanState"}, {"type": "object", "properties": {
            "summary": {"type": "object", "description": "`total`, plus counts per status. Helm-managed workloads "
                        "(components with `release`) are counted through their release, not on their own. Components "
                        "with ignored updates count under `ignored` instead of their status, and not in `total`.",
                        "additionalProperties": {"type": "integer"}},
            "components": {"type": "array", "items": {"$ref": "#/components/schemas/Component"}},
            "ignore_enabled": {"type": "boolean", "description": "False when kube-drift can't save ignores."},
            "renovate": {"type": "object", "description": "Present only with `renovate.enabled`.", "properties": {
                "received_at": {"type": ["string", "null"], "format": "date-time", "description": "The last report; null before the first."},
                "repositories": {"type": "integer"},
                "can_approve": {"type": "boolean", "description": "A forge URL and token are configured."}}},
            "argocd": {"type": "object", "description": "Present only when `argocd.enabled` is set in the config: "
                       "the Argo CD Applications read for Helm releases.", "properties": {
                "namespaces": {"type": "array", "items": {"type": "string"}, "description": "Where Applications were listed."},
                "namespace_repos": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}},
                                    "description": "Per destination namespace, the git repositories its Applications sync "
                                                   "from (chart sources left out). Renovate uses it to tell whose update a row is."},
                "applications": {"type": "integer", "description": "Applications found that deploy to this cluster."},
                "errors": {"type": "array", "items": {"type": "string"}, "description": "Namespaces whose Applications "
                           "couldn't be listed (no Argo CD CRD, or kube-drift lacks get/list on applications.argoproj.io). "
                           "Absent when there were none."},
            }},
        }}]},
        "Component": {"type": "object", "description": "One installed thing and its newest upstream version.", "properties": {
            "id": {"type": "string"},
            "category": {"enum": ["cluster", "node", "helm", "manifest", "helm-workload"]},
            "status": {"enum": ["outdated", "current", "unknown", "error"]},
            "name": {"type": "string"}, "namespace": {"type": "string", "description": "`-` for cluster-wide components."},
            "kind": {"type": "string", "description": "Workload kind (Deployment, StatefulSet, DaemonSet, StaticPod)."},
            "install": {"type": "string", "description": "How it was installed: helm, argocd (a Helm chart an Argo CD "
                        "Application deploys), manifest, kubeadm, static pod…; for a Helm-managed workload, "
                        "`helm (<release>)` or `argocd (<release>)`."},
            "installed": {"type": "string", "description": "Installed chart version, image tag, or component version."},
            "installed_app": {"type": "string", "description": "Helm: the chart's appVersion. For an Argo CD release, "
                              "from ArtifactHub or the OCI chart's metadata; empty when neither says."},
            "argocd": {"type": "object", "description": "Helm release deployed by Argo CD (`install: argocd`, only with "
                       "`argocd.enabled`). Its id is `helm/<namespace>/<release>`, as for a release Secret, which it "
                       "replaces when both exist.", "properties": {
                "application": {"type": "string", "description": "The Application, as `<namespace>/<name>`."},
                "url": {"type": "string", "description": "The Application in Argo CD's web UI; only with `argocd.url`."},
                "source_index": {"type": "integer", "description": "Multi-source Application: which `spec.sources` entry."},
                "repo": {"type": "string", "description": "The source's repoURL: a Helm repository or an OCI registry path."},
                "target_revision": {"type": "string", "description": "The source's targetRevision (may be a range)."},
                "sync_status": {"type": "string", "description": "Argo CD's sync status: Synced, OutOfSync, Unknown."},
                "deployed": {"type": "boolean", "description": "`false` when Argo CD has never deployed it: `installed` "
                             "is then the version it would sync to, not one that is running."},
                "replaced_secret_version": {"type": "string", "description": "Chart version in the stale Helm release "
                                            "Secret this row replaces; absent when there is none."},
                "namespace_assumed": {"type": "boolean", "description": "`true` when the Application sets neither "
                                      "helm.namespace nor a destination namespace: `namespace` is then where a release "
                                      "Secret of that name is (if only one), else `default`. Absent otherwise."},
            }},
            "release": {"type": "string", "description": "Helm-managed workload: `id` of the Helm release component "
                        "that installs it (a release Secret, or an Argo CD Helm source when `argocd.enabled`), directly or through an operator the release ships (a workload whose "
                        "`…/managed-by` label names one of the release's workloads); it is upgraded as part of that "
                        "release. Absent when the release isn't deployed."},
            "images": {"type": "integer", "description": "Helm release: how many workload images it installs."},
            "images_outdated": {"type": "integer", "description": "Helm release: how many of those images are outdated."},
            "running_version": {"type": "string", "description": "Version reported by the app itself (probes), for floating tags."},
            "latest": {"type": ["string", "null"], "description": "Newest version within the configured track."},
            "latest_any": {"type": ["string", "null"], "description": "Newest version overall, when it differs."},
            "latest_app": {"type": ["string", "null"], "description": "Helm: appVersion of the latest chart."},
            "ignored": {"$ref": "#/components/schemas/UpdateIgnored"},
            "renovate": {"$ref": "#/components/schemas/RenovateState"},
            "incompatible": {"type": "string", "description": "Why `latest` can't be installed on this cluster, e.g. the "
                             "chart's `kubeVersion` range excludes the control plane's version. Absent when it can, or "
                             "when kube-drift can't tell."},
            "track": {"enum": ["any", "major", "minor"]},
            "image": {"type": "string"}, "image_host": {"type": "string"}, "image_repo": {"type": "string"}, "tag": {"type": "string"},
            "floating": {"type": "boolean", "description": "The tag carries no version (latest, main…)."},
            "pinned_digest": {"type": ["string", "null"]}, "running_digests": {"type": "array", "items": {"type": "string"}},
            "remote_digest": {"type": ["string", "null"]},
            "digest_current": {"type": "boolean", "description": "The deployed image is the tag's current image: the pinned digest when the manifest pins one, else what the pods report."},
            "ref": {"type": "string", "description": "Where to read release notes: the config's `ref`, else for images "
                    "the source repo's releases page (or docs/home page) from the image's OCI labels, else its registry page. "
                    "Empty when none is known."},
            "ref_ah": {"type": "string", "description": "ArtifactHub page, when ref is elsewhere."},
            "note": {"type": "string"}, "error": {"type": "string"}, "probe_error": {"type": "string"}, "digest_error": {"type": "string"},
            "source": {"type": "object", "description": "Where the available version came from (see config)."},
            "probe_source": {"type": ["object", "null"]}, "first_party": {"type": "boolean"}, "loose_suffix": {"type": "boolean"},
            "ref_overridden": {"type": "boolean", "description": "ref was set in config or by a probe, not discovered."},
            "config_key": {"type": "string", "description": "The `images` or `helm` rule it matched (`GET /api/config`); absent when none did."}, "updated": {"type": "string"}, "checked_at": {"type": "string", "format": "date-time"},
        }},
        "RenovateState": {"type": "object", "description": "Images and charts only, once a Renovate report came in "
                          "(`renovate.enabled`).", "properties": {
            "state": {"enum": ["approve", "pr", "pending", "tracked", "untracked"],
                      "description": "`approve`: an update waits in the Dependency Dashboard (`POST /api/renovate/approve`). "
                                     "`pr`: Renovate opened its PR. `pending`: Renovate has the branch planned (`result` "
                                     "says why it waits). `tracked`: Renovate watches it, nothing to do. `untracked`: "
                                     "no Renovate dependency is this image or chart."},
            "repo": {"type": "string"}, "dependency": {"type": "string"}, "file": {"type": "string"},
            "repo_url": {"type": "string", "description": "The repository on the forge (`renovate.forge.url`); absent without one."},
            "file_url": {"type": "string", "description": "`file` on the forge, on the repository's default branch. Absent "
                         "until a report has come in while a forge token was set, since the branch is read from the forge."},
            "branch": {"type": "string"}, "title": {"type": "string", "description": "The PR title, as in the dashboard."},
            "new": {"type": "string", "description": "The version the update moves to."},
            "update_type": {"type": "string"}, "result": {"type": "string", "description": "Renovate's branch result."},
            "pr": {"type": "integer"}, "pr_url": {"type": "string"},
            "skipped": {"type": "string", "description": "Renovate's skipReason for the dependency."},
            "also_in": {"type": "array", "items": {"type": "string"}, "description": "Other repositories with the same image and tag."},
        }},
        "UpdateIgnored": {"type": "object", "description": "Present only while the component's update is ignored. "
                          "It ends at the first condition met; ended ignores are removed.", "properties": {
            "latest": {"type": ["string", "null"], "description": "The version on offer when it was ignored."},
            "latest_any": {"type": ["string", "null"], "description": "latest_any when it was ignored."},
            "digest": {"type": ["string", "null"], "description": "Floating tag: the registry digest when it was ignored."},
            "until": {"type": ["string", "null"], "format": "date-time"},
            "kube_version": {"type": ["string", "null"], "description": "Ends when the cluster no longer runs this version."},
            "until_newer": {"type": "boolean", "description": "Ends when something newer than `latest` is available."},
            "via": {"type": "string", "description": "Helm-managed workload: ignored because its release (this id) is."},
            "note": {"type": "string"}, "at": {"type": "string", "format": "date-time"},
        }},
        "ConfigRuleBody": {"type": "object", "description": "An `images` or `helm` entry, as in the config file. Fields left out take the default.", "properties": {
            "track": {"enum": ["any", "major", "minor"], "description": "Only offer upgrades within the installed major (or minor) version."},
            "source": {"type": "object", "description": (
                "Where newer versions come from; leave out for the default (images: the registry's tags; helm: an "
                "ArtifactHub search by chart name). images: `{type: none}` or `{type: github, repo, version_regex?, prefer_tags?}`. "
                "helm: also `{type: artifacthub, repo, chart?}` and `{type: oci, ref}`."),
                "properties": {"type": {"enum": ["none", "github", "artifacthub", "oci"]}, "repo": {"type": "string"},
                               "chart": {"type": "string"}, "ref": {"type": "string"}, "version_regex": {"type": "string"},
                               "prefer_tags": {"type": "boolean"}}},
            "ref": {"type": "string", "format": "uri", "description": "http(s) link for the Release notes column."},
            "name": {"type": "string", "description": "images: shown instead of the workload's name."},
            "note": {"type": "string", "description": "images: shown under the name."},
            "loose_suffix": {"type": "boolean", "description": "images: compare tags by their version part only (1.4.2-a1b2c3-r1)."},
        }},
        "ConfigRule": {"type": "object", "properties": {
            "key": {"type": "string"},
            "from": {"enum": ["config", "dashboard"]},
            "rule": {"type": "object", "description": "The entry in effect (ConfigRuleBody; a config-file entry may carry more)."},
            "replaces": {"type": "object", "description": "Dashboard rule: the config file's entry for the same key, which it replaces."},
            "components": {"type": "array", "items": {"type": "string"}, "description": "Component ids that used it in the last scan."},
            "at": {"type": ["string", "null"], "format": "date-time"},
            "by": {"type": ["string", "null"], "description": "Who saved it, when sign-in is on."},
        }},
        "Config": {"type": "object", "properties": {
            "images": {"type": "array", "items": {"$ref": "#/components/schemas/ConfigRule"}},
            "helm": {"type": "array", "items": {"$ref": "#/components/schemas/ConfigRule"}},
            "settings": {"type": "object", "description": "The rest of the config file (auth, probes, registries, ignore, orphans…), redacted."},
            "settings_yaml": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Each of `settings` as YAML."},
            "dashboard_yaml": {"type": "string", "description": "The dashboard rules as `images:`/`helm:` YAML for the config file; empty when there are none."},
            "file": {"type": "string", "description": "Where kube-drift reads its config file."},
            "env": {"type": "object", "additionalProperties": {"type": "string"}, "description": "Env vars set that override the file (CLUSTER_NAME, SCAN_INTERVAL_HOURS)."},
            "editable": {"type": "boolean", "description": "False when kube-drift can't save rules."},
            "cluster": {"type": "string"}, "version": {"type": "string"},
        }},
        "OrphanScan": {"allOf": [{"$ref": "#/components/schemas/ScanState"}, {"type": "object", "properties": {
            "format": {"type": "integer", "description": "Shape version of the scan result."},
            "kor_version": {"type": "string"},
            "kor_args": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "object", "properties": {
                "orphan": {"type": "integer"}, "in-use": {"type": "integer"}, "managed": {"type": "integer"},
                "protected": {"type": "integer"}, "ignored": {"type": "integer"},
                "total": {"type": "integer", "description": "Findings that are not ignored."}}},
            "items": {"type": "array", "items": {"$ref": "#/components/schemas/OrphanItem"}},
            "rules": {"type": "array", "items": {"$ref": "#/components/schemas/Rule"}},
            "kubectl_context": {"type": ["string", "null"], "description": "The --context used in generated commands."},
            "ignore_enabled": {"type": "boolean", "description": "False when kube-drift can't save ignores."},
            "stale_ignores": {"type": "array", "items": {"type": "string"}, "description": "Ignored ids no longer found."},
            "warnings": {"type": "array", "items": {"type": "string"}},
        }}]},
        "OrphanItem": {"type": "object", "properties": {
            "id": {"type": "string", "description": "`Kind/namespace/name`; empty namespace for cluster-scoped objects."},
            "kind": {"type": "string"}, "namespace": {"type": "string"}, "name": {"type": "string"},
            "status": {"enum": ["orphan", "in-use", "managed", "protected"]},
            "reason": {"type": "string", "description": "kor's reason, e.g. `ConfigMap is not used in any pod or container`."},
            "hints": {"type": "array", "description": "kube-drift's evidence.", "items": {"$ref": "#/components/schemas/Hint"}},
            "protected": {"type": "string", "description": "Why it's protected (only when status is protected)."},
            "ignored": {"$ref": "#/components/schemas/Ignored"},
            "created": {"type": ["string", "null"], "format": "date-time"},
            "known_kind": {"type": "boolean", "description": "kube-drift can write commands for this kind."},
            "namespaced": {"type": "boolean"},
        }},
        "Hint": {"type": "object", "properties": {
            "type": {"enum": ["in-use", "recreated", "leftover", "gone"]},
            "source": {"type": "string", "description": "Match it with Rule.source: cert-manager, gateway, cert-manager-issuer, helm, helm-leftover, argocd, managed-by:<name>, gone."},
            "text": {"type": "string"},
        }},
        "Ignored": {"type": "object", "description": "Present only on ignored findings.", "properties": {
            "by": {"enum": ["dashboard", "rule", "config"], "description": "Ignored individually, by a dashboard/API rule, or by a config rule."},
            "rule": {"type": "string", "description": "Id of the matching rule."},
            "note": {"type": "string"}, "at": {"type": ["string", "null"], "format": "date-time"},
            "kind": {"type": "string"}, "namespace": {"type": "string"}, "name": {"type": "string"},
        }},
        "RuleMatch": {"type": "object", "minProperties": 1,
                      "description": "Every field set must match; fields left out match anything.", "properties": {
            "kind": {"type": "string", "examples": ["Secret"]},
            "namespace": {"type": "string"},
            "name": {"type": "string", "description": "Exact name (config rules only)."},
            "name_regex": {"type": "string", "description": "Python regex, searched anywhere in the name."},
            "source": {"type": "string", "description": "Matches a finding's Hint.source.", "examples": ["cert-manager"]},
        }},
        "Rule": {"allOf": [{"$ref": "#/components/schemas/RuleMatch"}, {"type": "object", "properties": {
            "id": {"type": "string"},
            "from": {"enum": ["dashboard", "config"]},
            "note": {"type": "string"}, "why": {"type": "string"},
            "at": {"type": ["string", "null"], "format": "date-time"},
            "matches": {"type": "integer", "description": "Findings it matches in the current scan."},
        }}]},
    }},
}
