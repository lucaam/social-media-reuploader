Flux examples for deploying the Helm chart in this repository

This folder contains example Flux manifests you can adapt to deploy the bundled
Helm chart `charts/social-media-reuploader` using Flux (v2).

Important: do NOT store real secrets in the repository. Use SealedSecrets,
ExternalSecrets, SOPS-encrypted files or Kubernetes Secret objects managed
outside of this repo.

Files:
- `gitrepository.yaml` - example `GitRepository` source (replace `url`).
- `helmrelease.yaml` - example `HelmRelease` referencing the chart path in this repo.

Usage:
1. Copy these manifests to the cluster or add them to a Flux-controlled repo.
2. Replace placeholders (repo URL, namespace, secret names) with your real values.
