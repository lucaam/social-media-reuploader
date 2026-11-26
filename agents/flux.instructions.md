# Flux deployment agent

Usa questo prompt per generare o adattare i manifest Flux di questo repository.

Prompt suggeriti:

- "Genera una `HelmRelease` (`helm.toolkit.fluxcd.io/v2`) per il chart in `charts/social-media-reuploader` con il
  token del bot nel Secret `smr-bot` (`secret.name`) e le credenziali della GUI nel Secret `smr-gui`
  (`gui.existingSecret`)."
- "Crea una `Kustomization` che applica `examples/flux/` nel namespace `smr`."
- "Spiega come creare con sealed-secrets o SOPS i Secret `smr-bot` (BOT_TOKEN) e `smr-gui` (SECRET_KEY, ADMIN_TOKEN o
  OAUTH_*) e come referenziarli dalla HelmRelease."

Note:

- Non includere mai segreti nei manifest: solo nomi di Secret (`secret.name`, `gui.existingSecret`,
  `ytdlpCookies.existingSecret`).
- Sorgente `GitRepository` (`source.toolkit.fluxcd.io/v1`) su un tag di release (`ref.tag` o `ref.semver`, mai
  `branch: main`: il chart di `main` può richiedere un'immagine non ancora pubblicata) e `HelmRelease` che punta a
  `./charts/social-media-reuploader`; fissa `image.tag` a una versione invece di `latest`.
- Il chart ammette una sola replica del bot (`replicaCount: 1`) e mette la GUI sullo stesso nodo del bot perché
  condividono un PVC `ReadWriteOnce`.
