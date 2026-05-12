# Flux deployment agent

Use this agent prompt to help generate or adapt Flux manifests for this repo.

Suggested prompts:

- "Generate a Flux `HelmRelease` for the chart in `charts/social-media-reuploader`, with values pulled from a Kubernetes Secret named `td-secrets`."
- "Create a `Kustomization` that applies `examples/flux/helmrelease.yaml` in namespace `default`."
- "Explain how to create sealed-secrets for BOT_TOKEN and ADMIN_TOKEN and reference them from HelmRelease values."

Notes:
- Do not include raw secrets in generated manifests. Always reference secret names.
- Use `GitRepository` as source and `HelmRelease` pointing to `./charts/social-media-reuploader`.
