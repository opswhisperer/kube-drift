# Changelog

Notable changes to kube-drift. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). Releases before 0.5.0 are in the
git tags and on the GitHub releases page.

## [Unreleased]

## [0.8.0] - 2026-10-31

### Added

- **Links in an expanded row**, where there was only text. The Renovate line links the repository and
  its package file on the forge; the Argo CD line links the Application in Argo CD's web UI, with the
  new `argocd.url`; "Version from" links the GitHub releases page or the ArtifactHub chart it reads.
  The file link needs the repository's default branch, which kube-drift now reads from the forge once
  per Renovate report (`renovate.forge` with a token); without one the repository is still linked.
- **Every row that is behind upstream now says what Renovate makes of it.** A row with no Upgrade
  button and no PR used to say nothing at all, which read as a gap. It now says it is tracked with
  nothing pending — linking the repository and the package file — or that Renovate is not tracking
  it. An image inside a Helm release names the chart that ships it and links the repository that
  chart's version is pinned in, since the image's own version is not in any Renovate file. Rows that
  are current stay quiet, as do the cluster and its nodes, which Renovate has no say over.
- **"Show N up to date"**, a filter that starts switched on, so the version table opens on the rows
  that need attention instead of the whole inventory. Picking a bar segment (including `current`)
  still shows exactly that segment, and the choice is remembered per browser.

### Fixed

- **An image pinned by digest is no longer reported stale for ever.** kube-drift compared the
  registry's digest for the tag against the digest the kubelet reports, and for a pinned image
  those are routinely different digests for the *same* content — containerd keeps more than one
  index digest per image and reports whichever its store resolves to. A correctly pinned row read
  "tag re-pushed, running image is stale" on every scan. A row whose manifest pins a digest is now
  judged on that pin, and says "pinned digest is behind this tag" when the pin itself has fallen
  behind, which is the thing Renovate can act on. Rows with no pin are unchanged.

## [0.7.2] - 2026-10-28

### Fixed

- **Renovate approve on Forgejo/Gitea, for real:** it always failed with "has no open issue titled …".
  Forgejo and Gitea send `"pull_request": null` on every plain issue, and kube-drift skipped any issue
  with a `pull_request` key (meant for GitHub, which lists PRs as issues). Now only issues whose
  `pull_request` has a value are skipped. 0.7.1 blamed Forgejo's issue search for this; the search was
  not the cause (its paging change stays).

## [0.7.1] - 2026-10-28

### Fixed

- **Renovate approve on Forgejo/Gitea:** finding the Dependency Dashboard no longer uses the issue search
  (`q=`), which Forgejo answers from its issue indexer and which can miss an open issue ("has no open issue
  titled …" although it is there). kube-drift now pages through the open issues and matches the title exactly,
  on GitHub too.

## [0.7.0] - 2026-10-28

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

[Unreleased]: https://github.com/opswhisperer/kube-drift/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/opswhisperer/kube-drift/compare/v0.7.2...v0.8.0
[0.7.2]: https://github.com/opswhisperer/kube-drift/compare/v0.7.1...v0.7.2
[0.7.1]: https://github.com/opswhisperer/kube-drift/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/opswhisperer/kube-drift/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/opswhisperer/kube-drift/compare/v0.5.2...v0.6.0
[0.5.2]: https://github.com/opswhisperer/kube-drift/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/opswhisperer/kube-drift/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/opswhisperer/kube-drift/compare/v0.4.0...v0.5.0
