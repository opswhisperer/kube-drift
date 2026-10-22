# Changelog

Notable changes to kube-drift. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/). Releases before 0.5.0 are in the
git tags and on the GitHub releases page.

## [Unreleased]

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

[Unreleased]: https://github.com/opswhisperer/kube-drift/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/opswhisperer/kube-drift/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/opswhisperer/kube-drift/compare/v0.4.0...v0.5.0
