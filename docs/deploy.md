# Deploy (Docker & Kubernetes)

Docker

Build the container locally:

```bash
docker build -t ghcr.io/lucaam/social-media-reuploader:latest .
```

Run locally:

```bash
docker run -e BOT_TOKEN="$BOT_TOKEN" -p 8080:8080 ghcr.io/lucaam/social-media-reuploader:latest
```

Kubernetes (Helm)

The Helm chart is under `charts/social-media-reuploader`.

Install locally (dry-run):

```bash
helm lint charts/social-media-reuploader
helm install --dry-run --debug my-release charts/social-media-reuploader
```

In production, supply values for `image.repository`, `image.tag`, and environment variables such as `BOT_TOKEN`.

Flux (example)

This repository includes an `examples/flux` folder with a sample `GitRepository`
and `HelmRelease` you can adapt to deploy the bundled Helm chart with Flux v2.
Replace the placeholders and ensure secrets are provided via an external secret
mechanism (SealedSecrets / ExternalSecrets / SOPS).

Gateway API / HTTPRoute

This chart supports optional Gateway API `HTTPRoute` resources. To enable HTTPRoute routing instead of (or in addition to) a classic Ingress, set the following values:

```yaml
gatewayApi:
	enabled: true
	gatewayName: your-gateway-name
	hosts:
		- example.com
	path: /
	servicePort: 8080
```

The chart will create an `HTTPRoute` resource that references the specified Gateway. Ensure your cluster has the Gateway API controller installed and a `Gateway` resource that accepts the `parentRef`.

OAuth & Admin GUI

See [OAuth setup](oauth.md) for instructions to register an OAuth client and configure the GUI. The chart can pass OAuth-related environment variables to the GUI deployment via `Values.env` or Kubernetes Secrets as needed.

Creating a Kubernetes Secret for GUI (recommended)

You can create a Kubernetes Secret with your OAuth/client data and instruct the chart to use it:

```bash
kubectl create secret generic social-media-reuploader-gui-secret \
	--from-literal=ADMIN_TOKEN="<your-admin-token>" \
	--from-literal=SECRET_KEY="<random-secret>" \
	--from-literal=OAUTH_CLIENT_ID="<client-id>" \
	--from-literal=OAUTH_CLIENT_SECRET="<client-secret>" \
	--from-literal=OAUTH_AUTHORIZE_URL="https://provider.example/authorize" \
	--from-literal=OAUTH_TOKEN_URL="https://provider.example/token" \
	--from-literal=OAUTH_USERINFO_URL="https://provider.example/userinfo"
```

Then install the chart with `gui.useSecret=true` so the GUI pod will load these values from the Secret:

```bash
helm upgrade --install td charts/social-media-reuploader \
	--set gui.useSecret=true
```

Alternatively, set `gui.secretName` to reference a pre-created secret with a custom name.


GHCR publishing

See [GHCR notes](ghcr.md) for details about publishing images to GitHub Container Registry and verifying package visibility.

Local testing

For step-by-step local testing instructions, see [Local testing & development](local-testing.md).

