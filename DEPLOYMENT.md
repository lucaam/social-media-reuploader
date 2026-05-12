Flux/Kubernetes deployment notes

- This repository ships a Helm chart in `charts/social-media-reuploader`.
- For Flux-based deployment, create a GitRepository + HelmRelease or a Kustomization that references the chart.
- Secrets (BOT_TOKEN, ADMIN_TOKEN, SECRET_KEY, OAUTH_CLIENT_SECRET) must NOT be committed. Use Kubernetes Secrets managed by SealedSecrets, ExternalSecrets, or SOPS.
- Recommended steps:
  1. Create a Git repository (e.g. GitHub) and push the `main` branch.
  2. Configure Flux to watch the repository and apply `charts/social-media-reuploader` as a HelmRelease.
  3. Store secrets in the cluster via sealed-secrets or ExternalSecrets and reference them from the Helm values (`values.yaml`).
  4. Provide image repository and tag in `values.yaml` during deployment.
  5. Expose the GUI and bot using standard Kubernetes Services/Ingress; the chart contains templates for GUI and bot deployments.

## Resource Profiles

The Helm chart supports three predefined resource profiles to handle different video workloads:

| Profile | CPU Req | Memory Req | CPU Limit | Memory Limit | Grace Period | Use Case |
|---------|---------|------------|-----------|--------------|--------------|----------|
| **small** | 100m | 256Mi | 300m | 512Mi | 60s | Videos ≤50MB |
| **medium** | 200m | 512Mi | 1000m | 1Gi | 120s | Videos ≤100MB (default) |
| **large** | 500m | 1Gi | 2000m | 2Gi | 300s | Videos ≤500MB |

### Usage

Set the profile in `values.yaml`:
```yaml
resourceProfile: medium  # or: small, large
```

To override with custom values:
```yaml
resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    cpu: 1500m
    memory: 1.5Gi
terminationGracePeriodSeconds: 180
```

The `terminationGracePeriodSeconds` ensures ffmpeg subprocess processes complete gracefully before pod termination (critical during video recompression).

Checklist before push:
- Ensure `.gitignore` excludes `data/` and local artifacts.
- Remove any local credentials from the repo (none detected in tracked files).
- Decide remote policy: we will force-push the rewritten `main` branch when you're ready.

If you want, I can generate a sample Flux `HelmRelease` manifest referencing the chart and a sample `Kustomization` for your GitRepository.
