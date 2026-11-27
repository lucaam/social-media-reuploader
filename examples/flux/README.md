Esempi Flux per il chart Helm di questo repository

Questa cartella contiene manifest Flux (v2) di esempio per installare il chart
pubblicato da ogni release su GHCR
(`oci://ghcr.io/lucaam/charts/social-media-reuploader`).

Importante: NON salvare segreti nel repository. Usa SealedSecrets,
External Secrets, file cifrati con SOPS oppure Secret creati fuori da Git.

File:
- `ocirepository.yaml`: sorgente `OCIRepository` (`source.toolkit.fluxcd.io/v1`)
  che segue le release (`semver`; per deploy riproducibili fissa una versione
  con `tag`) e accetta solo chart firmati dal workflow di release di questo
  repository (`spec.verify`, cosign keyless; serve Flux 2.8 o successivo).
- `helmrelease.yaml`: `HelmRelease` (`helm.toolkit.fluxcd.io/v2`) che usa il
  chart dell'`OCIRepository` (`chartRef`), con il token del bot nel Secret
  `smr-bot` e le credenziali della GUI nel Secret `smr-gui`.

Non usare come sorgente il repository Git: il chart in `charts/` ha la
versione segnaposto `0.0.0` (le versioni vere le assegna il workflow di
release) e un'immagine `0.0.0` che non esiste.

Uso:
1. Crea il namespace `smr` e i Secret `smr-bot` (chiave `BOT_TOKEN`) e `smr-gui`
   (chiavi `SECRET_KEY` e `ADMIN_TOKEN`, oppure le impostazioni `OAUTH_*`).
2. Applica i manifest (o aggiungili a un repository gestito da Flux):
   `kubectl apply -f examples/flux/`.
3. Controlla con `flux get sources oci -n smr` e `flux get helmreleases -n smr`.

Il chart di ogni release usa l'immagine della stessa versione: non serve
impostare `image.tag` nella `HelmRelease`, a meno di voler installare un'altra
versione.
