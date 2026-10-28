# Changelog

Notable changes to kube-drift. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). Releases before 0.5.0 are in the
git tags and on the GitHub releases page.

## [Unreleased]

### Added

- **Renovate** (opt-in, `renovate:` in the config): POST Renovate's report (`reportType: file`) to
  `/api/renovate/report` after each run, and every image or chart row says what Renovate has for it:
  an update waiting for approval, an open PR (linked), planned, tracked, or not tracked at all. With a
  forge token (`renovate-forge-token` in `kube-drift-credentials`), an **Upgrade** button approves the
  update: `POST /api/renovate/approve` ticks its box in the repository's Dependency Dashboard on
  Forgejo, Gitea or GitHub. Nothing in the cluster changes. The scan's `argocd` part now lists, per
  namespace, the git repositories its Applications sync from (`namespace_repos`), which tells whose
  update a row is when several repositories pin the same image.

## [0.6.0] - 2026-10-25

### Added

- `GET /api/drift` and `/api/drift.csv` take filters for `components`: `status` (repeatable or
  comma-separated), `ignored=true|false` and `top=true|false` (leave out, or keep only, the images
  inside Helm releases). `?status=outdated&ignored=false&top=true` lists exactly what
  `summary.outdated` counts, for dashboards such as Homepage that can show a list but not filter
  it. Without parameters the response is unchanged; an unknown parameter or value is a 400.
- Opt-in Argo CD support (`argocd.enabled`, plus the RBAC component `deploy/components/argocd`).
  Every Helm chart source of an Argo CD Application becomes a Helm release row with the version
  Argo CD deployed, looked up upstream like any release, and the workloads it rendered are listed
  under it instead of as manifests. An Argo CD release replaces a stale Helm release Secret of the
  same name and namespace, left behind by a move from `helm install`. Rows carry
  `install: argocd` and an `argocd` object in `GET /api/drift`; off, nothing changes.

## [0.5.2] - 2026-10-22

### Changed

- Fewer Helm rules needed: when several ArtifactHub repositories carry a chart of the same name,
  kube-drift picks the one the installed chart's own home and source links name, then the most
  starred. Before, any "verified publisher" won, and that's often a one-star mirror. A repository
  older than the installed chart is ranked last.
- "AH guess: <repo>" only shows when nothing clearly decided the pick, not on every chart name
  that more than one repository uses.
- Release-notes links need less config. A Helm release links to the releases page of the GitHub
  repo it takes versions from, else of its chart's GitHub or GitLab source repo, before its home
  page. An image set to take versions from GitHub links to that repo's releases. An image whose
  labels don't name its project links to a repo its Helm chart lists under the image's name, else
  to the GitHub repo named like it (`ghcr.io/org/app` → `github.com/org/app`; also for Docker Hub,
  Quay and registry.k8s.io) when that repo has releases or tags, before the registry page. It
  asks the repo's public releases feed, so it doesn't use up GitHub's 60 anonymous API calls an
  hour.

## [0.5.1] - 2026-10-22

### Added

- A **Config** tab in the dashboard that shows the config kube-drift is running with, secrets
  hidden. Each `images:` and `helm:` rule lists the rows it matched in the last scan.
- Image and Helm rules can be added and overridden in the dashboard, from the Config tab or with
  **Configure…** on a version row: track, where newer versions come from, the release-notes link,
  and for images the display name, note and `loose_suffix`. They're kept on kube-drift's volume,
  replace the config file's entry for the same key, start a rescan, and can be copied out as YAML
  for the config file. In the API: `GET /api/config`, `POST /api/config/rules` and
  `POST /api/config/rules/delete`, plus `config_key` on each component.

### Fixed

- A workload on a floating tag showed **unknown** when its node already held the same image under
  another name. The container runtime then reports that name (for example `x:2.4.1` for a pod that
  asked for `x:latest`, or an old registry's name), so kube-drift found no running digest. Running
  digests are now matched by the image the pod spec asked for as well.

## [0.5.0] - 2026-10-21

### Added

- Registry credentials from a standard pull secret, with no config change. Create a
  `kubernetes.io/dockerconfigjson` Secret named `kube-drift-registry-auth` in kube-drift's
  namespace, for example with `kubectl create secret docker-registry`, or copy a pull secret you
  already have. `deploy/base` mounts it if it exists (`optional: true`), and one Secret can hold
  credentials for any number of registries. Hosts match the way the kubelet matches them:
  `docker.io` under any of its names, and the longest path prefix wins. The Secret is re-read when
  it changes. `REGISTRY_AUTH_FILE` points kube-drift at another file, or `""` turns this off.
  See [Registry credentials](README.md#registry-credentials).

### Changed

- A non-public registry that kube-drift has credentials for counts as your own registry, the same
  as one listed under `registries:`. Its images get `loose_suffix` by default.
- `registries:` entries are now needed only for `insecure: true`. `auth_env` still works and
  still wins for its host, so existing configs keep working unchanged.

[Unreleased]: https://github.com/opswhisperer/kube-drift/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/opswhisperer/kube-drift/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/opswhisperer/kube-drift/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/opswhisperer/kube-drift/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/opswhisperer/kube-drift/compare/v0.4.0...v0.5.0
