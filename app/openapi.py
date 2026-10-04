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
            "(ignores, ignore rules, rescans) or return text: `/api/orphans/commands` returns a kubectl "
            "script for a human to review and run. Responses never contain Secret data.\n\n"
            "**Security: kube-drift has no authentication of its own.** It must only be reachable "
            "through a gateway that authenticates every request (SSO for people, API keys or JWTs for "
            "agents); any credentials you use belong to that gateway. If you can call this API without "
            "having authenticated, the deployment is exposed — tell its operator.\n\n"
            "Requests with a body must use `Content-Type: application/json`.\n\n"
            "Scans take time: check `scanning` and `scanned_at`, and after `POST /api/rescan` poll the "
            "GET endpoint until `scanning` is false and `scanned_at` has changed."
        ),
        "license": {"name": "Apache-2.0", "identifier": "Apache-2.0"},
    },
    "tags": [
        {"name": "versions", "description": "Installed vs available versions."},
        {"name": "orphans", "description": "Unused resources, ignores and cleanup scripts."},
        {"name": "service", "description": "Scans, health and this document."},
    ],
    "paths": {
        "/api/drift": {"get": {
            "tags": ["versions"], "operationId": "getVersionDrift",
            "summary": "Version scan: every component with installed and available versions",
            "description": "Components are sorted with `outdated` first. `summary` counts components by status.",
            "responses": {"200": _json("VersionScan")},
        }},
        "/api/drift.csv": {"get": {
            "tags": ["versions"], "operationId": "getVersionDriftCsv",
            "summary": "Version scan as CSV",
            "responses": {"200": {"description": "CSV with a header row", "content": {"text/csv": {"schema": {"type": "string"}}}}},
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
        "/healthz": {"get": {
            "tags": ["service"], "operationId": "healthz", "summary": "Liveness",
            "responses": {"200": _json("Ok")},
        }},
        "/readyz": {"get": {
            "tags": ["service"], "operationId": "readyz", "summary": "Readiness: 503 until the first version scan has data",
            "responses": {"200": _json("Ok"), "503": _json("Ok", "Not ready")},
        }},
        "/openapi.json": {"get": {
            "tags": ["service"], "operationId": "openapi", "summary": "This document",
            "responses": {"200": {"description": "OpenAPI 3.1", "content": {"application/json": {"schema": {"type": "object"}}}}},
        }},
    },
    "components": {"schemas": {
        "Error": {"type": "object", "properties": {"error": {"type": "string"}}},
        "Ok": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        "ScanState": {"type": "object", "properties": {
            "cluster": {"type": "string", "description": "Cluster name from the config."},
            "scanning": {"type": "boolean", "description": "A scan is running now."},
            "scanned_at": {"type": ["string", "null"], "format": "date-time", "description": "When the data was produced; null before the first scan."},
            "duration_s": {"type": "number"},
            "next_scan_at": {"type": ["string", "null"], "format": "date-time"},
            "last_error": {"type": ["string", "null"], "description": "Why the last scan failed; the previous data is still served."},
            "generated_at": {"type": "string", "format": "date-time"},
        }},
        "VersionScan": {"allOf": [{"$ref": "#/components/schemas/ScanState"}, {"type": "object", "properties": {
            "summary": {"type": "object", "description": "`total`, plus counts per status.", "additionalProperties": {"type": "integer"}},
            "components": {"type": "array", "items": {"$ref": "#/components/schemas/Component"}},
        }}]},
        "Component": {"type": "object", "description": "One installed thing and its newest upstream version.", "properties": {
            "id": {"type": "string"},
            "category": {"enum": ["cluster", "node", "helm", "manifest", "helm-workload"]},
            "status": {"enum": ["outdated", "current", "unknown", "error"]},
            "name": {"type": "string"}, "namespace": {"type": "string", "description": "`-` for cluster-wide components."},
            "kind": {"type": "string", "description": "Workload kind (Deployment, StatefulSet, DaemonSet, StaticPod)."},
            "install": {"type": "string", "description": "How it was installed: helm, manifest, kubeadm, static pod…"},
            "installed": {"type": "string", "description": "Installed chart version, image tag, or component version."},
            "installed_app": {"type": "string", "description": "Helm: the chart's appVersion."},
            "running_version": {"type": "string", "description": "Version reported by the app itself (probes), for floating tags."},
            "latest": {"type": ["string", "null"], "description": "Newest version within the configured track."},
            "latest_any": {"type": ["string", "null"], "description": "Newest version overall, when it differs."},
            "latest_app": {"type": ["string", "null"], "description": "Helm: appVersion of the latest chart."},
            "track": {"enum": ["any", "major", "minor"]},
            "image": {"type": "string"}, "image_host": {"type": "string"}, "image_repo": {"type": "string"}, "tag": {"type": "string"},
            "floating": {"type": "boolean", "description": "The tag carries no version (latest, main…)."},
            "pinned_digest": {"type": ["string", "null"]}, "running_digests": {"type": "array", "items": {"type": "string"}},
            "remote_digest": {"type": ["string", "null"]},
            "digest_current": {"type": "boolean", "description": "The running image is the tag's current image."},
            "ref": {"type": "string", "description": "Where to read release notes."},
            "ref_ah": {"type": "string", "description": "ArtifactHub page, when ref is elsewhere."},
            "note": {"type": "string"}, "error": {"type": "string"}, "probe_error": {"type": "string"}, "digest_error": {"type": "string"},
            "source": {"type": "object", "description": "Where the available version came from (see config)."},
            "probe_source": {"type": ["object", "null"]}, "first_party": {"type": "boolean"}, "loose_suffix": {"type": "boolean"},
            "ref_overridden": {"type": "boolean"}, "updated": {"type": "string"}, "checked_at": {"type": "string", "format": "date-time"},
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
