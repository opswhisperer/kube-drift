# Notes for coding agents (and humans)

The README explains what kube-drift is and how to run it. These are the rules that aren't
obvious from the code.

**Test:** `pip install -r requirements.txt && python3 -m unittest discover -s tests -p 'test_*.py'`.
Unit tests never touch the network or a cluster; `python3 -m tests.offline` is the networked
check against the fixture cluster.

- **kube-drift never writes to the cluster.** RBAC is get/list only (`deploy/base/rbac.yaml`) and
  the README promises it. Cleanup is a kubectl script the user runs (`orphans.commands`) —
  don't add API calls that create, patch or delete cluster objects. POST endpoints may only
  change kube-drift's own state (ignores, rules, rescans).
- **Never return Secret data** from the API or dashboard — names only.
- **Authentication is optional and off by default** (`app/auth.py`). With `auth.oidc` in the
  config, people sign in through OIDC and scripts use API keys; without it, kube-drift must sit
  behind an authenticating gateway, and the README/OpenAPI warnings say so. Keep both modes
  working. Every route except the open ones (`OPEN` in `app/main.py`, `/auth/*`) goes through
  `Handler._gate`. Store only API key hashes, and never let an API key create or revoke keys.
  Stay standard library only: no JWT library. The ID token is trusted because it comes straight
  from the token endpoint over TLS.
- **API changes update `app/openapi.py`** (served at `/openapi.json`, rendered at `/docs`).
  `tests/test_openapi.py` fails if a route or a response field isn't documented.
- **A new API read needs RBAC**: add the resource to `deploy/base/rbac.yaml` (get/list).
- **Changing the shape of an orphan scan result** (`OrphanScanner.run` in `app/orphans.py`)
  means bumping `FORMAT` there; otherwise a deployed pod crashes on the scan it saved before
  the upgrade.
- **Ignore-rule matching exists twice**: `rule_match` in `app/orphans.py` and `matchRule` in
  `app/static/index.html` (live match counts). Change both.
- **User-visible changes** get a line in `CHANGELOG.md` under `[Unreleased]` (Added, Changed,
  Fixed, Removed). A release renames that section to the version and date.
- **New or changed config keys** go in `config/example.yaml`, explained by the dashboard
  symptom they fix. Config is YAML; keys left empty load as null and are dropped (`load_config`).
- **Dependencies stay minimal**: standard library plus PyYAML. The UI is one HTML file with
  inline CSS/JS and no build step.
- **The test fixture is invented** (`tests/fake_k8s.py`, `tests/fake_orphans.py`). Never paste
  in names, addresses or data from a real cluster.
- `deploy/*` other than `deploy/base` is git-ignored on purpose: it's where site-specific
  overlays live.
