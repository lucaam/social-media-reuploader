Esempi Flux per il chart Helm di questo repository

Questa cartella contiene manifest Flux (v2) di esempio per installare il chart
`charts/social-media-reuploader` direttamente da questo repository.

Importante: NON salvare segreti nel repository. Usa SealedSecrets,
External Secrets, file cifrati con SOPS oppure Secret creati fuori da Git.

File:
- `gitrepository.yaml`: sorgente `GitRepository` (`source.toolkit.fluxcd.io/v1`)
  che segue l'ultima release (`semver`; per deploy riproducibili fissa un tag di
  release). Non seguire `main`: il suo chart può richiedere un'immagine non
  ancora pubblicata. Sostituisci `url` con il tuo repository.
- `helmrelease.yaml`: `HelmRelease` (`helm.toolkit.fluxcd.io/v2`) che usa il chart
  dal percorso del repository, con il token del bot nel Secret `smr-bot` e le
  credenziali della GUI nel Secret `smr-gui`.

Uso:
1. Crea il namespace `smr` e i Secret `smr-bot` (chiave `BOT_TOKEN`) e `smr-gui`
   (chiavi `SECRET_KEY` e `ADMIN_TOKEN`, oppure le impostazioni `OAUTH_*`).
2. Applica i manifest (o aggiungili a un repository gestito da Flux):
   `kubectl apply -f examples/flux/`.
3. Controlla con `flux get helmreleases -n smr`.

Il chart di ogni release usa l'immagine della stessa versione: non serve
impostare `image.tag` nella `HelmRelease`, a meno di voler installare un'altra
versione.
