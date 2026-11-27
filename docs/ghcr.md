# Immagini, chart e release

Ogni release `vX.Y.Z` pubblica su GitHub Container Registry, con la stessa
versione:

```text
ghcr.io/lucaam/social-media-reuploader:X.Y.Z               # immagine (linux/amd64, linux/arm64)
ghcr.io/lucaam/social-media-reuploader:latest              # ultima release
oci://ghcr.io/lucaam/charts/social-media-reuploader X.Y.Z  # chart Helm
```

- Tag dell'immagine: `X.Y.Z`, `X.Y`, `X` (solo da 1.0.0 in poi), `latest` e
  `sha-<commit>`. `X.Y`, `X` e `latest` si spostano solo sulla release più
  alta.
- Label e annotazioni OCI (versione, commit, data di build, sorgente), SBOM e
  provenance SLSA (`mode=max`) di BuildKit dentro l'immagine.
- Immagine e chart sono firmati con cosign (keyless, Sigstore) e hanno
  un'attestazione di provenienza GitHub; il `.tgz` del chart è allegato alla
  release GitHub.
- La versione è scritta nell'immagine in fase di build (`--build-arg
  VERSION=X.Y.Z` → `SMR_VERSION`): compare nei log di avvio, nello stato del
  worker nella GUI e in `/health` con `HEALTH_DEBUG=true`.

## Flusso di release

Automatico, a ogni push su `main` (workflow *Release*,
`.github/workflows/release.yml`; dettagli in
[RELEASE.md](https://github.com/lucaam/social-media-reuploader/blob/main/RELEASE.md)):

1. **gate**: gli stessi controlli delle pull request (`ci.yml`).
2. **version**: la versione successiva dai Conventional Commits dall'ultimo
   tag (`feat` → minor; `fix`, `perf`, `revert`, `fix(deps)` → patch;
   breaking → major, minor finché la versione è `0.y.z`; gli altri tipi non
   rilasciano). Senza commit rilasciabili il workflow si ferma: non esistono
   immagini `edge` o di sviluppo.
3. **image**: build e push per digest, smoke test dell'immagine pubblicata
   su entrambe le piattaforme, attestazione e firma; solo allora i tag, che
   puntano al digest firmato.
4. **chart** (dopo l'immagine): pacchetto con la versione della release,
   push OCI, attestazioni e firma.
5. **release**: tag `vX.Y.Z` e release GitHub con le note raggruppate per tipo.

Nel repository le versioni restano segnaposto (`Chart.yaml` `0.0.0`, codice
`0.0.0-dev`): il tag è l'unica fonte della versione, nessun commit di bump.

## Verificare firme e provenienza

Firme (cosign 3 o successivo):

```bash
IDENTITY=https://github.com/lucaam/social-media-reuploader/.github/workflows/release.yml@refs/heads/main
ISSUER=https://token.actions.githubusercontent.com

cosign verify ghcr.io/lucaam/social-media-reuploader:X.Y.Z \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER"
cosign verify ghcr.io/lucaam/charts/social-media-reuploader:X.Y.Z \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER"
```

Attestazioni di provenienza (GitHub CLI):

```bash
gh attestation verify oci://ghcr.io/lucaam/social-media-reuploader:X.Y.Z \
  --repo lucaam/social-media-reuploader \
  --signer-workflow lucaam/social-media-reuploader/.github/workflows/release.yml
gh attestation verify oci://ghcr.io/lucaam/charts/social-media-reuploader:X.Y.Z \
  --repo lucaam/social-media-reuploader
gh release download vX.Y.Z --repo lucaam/social-media-reuploader --pattern '*.tgz'
gh attestation verify social-media-reuploader-X.Y.Z.tgz --repo lucaam/social-media-reuploader
```

SBOM e provenance di BuildKit:

```bash
docker buildx imagetools inspect ghcr.io/lucaam/social-media-reuploader:X.Y.Z \
  --format '{{ json .SBOM }}'
docker buildx imagetools inspect ghcr.io/lucaam/social-media-reuploader:X.Y.Z \
  --format '{{ json .Provenance }}'
```

Con Flux la firma del chart si verifica nell'`OCIRepository` (`spec.verify`):
vedi `examples/flux/` e [Deploy](deploy.md#flux).

## Workflow

| Workflow | Quando | Cosa |
|---|---|---|
| `ci.yml` | pull request; richiamato da `release.yml` | test, lint, chart, documentazione, build e smoke test dell'immagine |
| `pr-title.yml` | pull request | titolo della PR in formato Conventional Commits e release che produrrà |
| `release.yml` | push su `main`; avvio manuale | versione, immagine, chart, tag e release |
| `docs-deploy.yml` | push su `main` che tocca la documentazione | sito su GitHub Pages |

Tutti i workflow usano solo `GITHUB_TOKEN`, con permessi minimi per job, e
azioni fissate per commit SHA (aggiornate da Renovate).

## Badge

```markdown
[![GHCR Image Version](https://img.shields.io/docker/v/ghcr.io/lucaam/social-media-reuploader?label=ghcr.io&sort=semver)](https://github.com/lucaam/social-media-reuploader/pkgs/container/social-media-reuploader)
```

## Build locale

```bash
docker build -t smr:dev --build-arg VERSION=0.0.0-dev .
scripts/smoke_test_image.sh smr:dev 0.0.0-dev   # gli stessi controlli della CI
docker run --rm --stop-timeout 75 -e BOT_TOKEN="$BOT_TOKEN" -p 127.0.0.1:8080:8080 smr:dev
```

Senza `--build-arg VERSION` l'immagine riporta la versione `0.0.0-dev`.
