# Flux deployment agent

Usa questo prompt per generare o adattare i manifest Flux di questo repository.

Prompt suggeriti:

- "Genera una `HelmRelease` (`helm.toolkit.fluxcd.io/v2`) per il chart OCI
  `oci://ghcr.io/lucaam/charts/social-media-reuploader` (un `OCIRepository` con firma verificata) con il token del bot
  nel Secret `smr-bot` (`secret.name`) e le credenziali della GUI nel Secret `smr-gui` (`gui.existingSecret`)."
- "Crea una `Kustomization` che applica `examples/flux/` nel namespace `smr`."
- "Spiega come creare con sealed-secrets o SOPS i Secret `smr-bot` (BOT_TOKEN) e `smr-gui` (SECRET_KEY, ADMIN_TOKEN o
  OAUTH_*) e come referenziarli dalla HelmRelease."

Note:

- Non includere mai segreti nei manifest: solo nomi di Secret (`secret.name`, `gui.existingSecret`,
  `ytdlpCookies.existingSecret`).
- Sorgente `OCIRepository` (`source.toolkit.fluxcd.io/v1`) sul chart delle release
  `oci://ghcr.io/lucaam/charts/social-media-reuploader` (`ref.tag` per una versione fissa o `ref.semver`), con
  `layerSelector` sul layer del chart e `verify` cosign keyless (identità: il workflow `release.yml` del repository su
  `refs/heads/main`; serve Flux 2.8 o successivo), e `HelmRelease` con `chartRef` verso quell'`OCIRepository`. Mai un
  `GitRepository`: il chart in `charts/` ha la versione segnaposto `0.0.0`. `image.tag` resta vuoto: il chart di una
  release installa l'immagine della stessa versione.
- Il chart ammette una sola replica del bot (`replicaCount: 1`) e mette la GUI sullo stesso nodo del bot perché
  condividono un PVC `ReadWriteOnce`.
