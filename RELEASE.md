# Guida alle release

Le release sono **automatiche**: ogni push su `main` (cioè ogni pull request
unita con *squash*) passa dal workflow *Release*
(`.github/workflows/release.yml`), che decide dai Conventional Commits se e
quale versione pubblicare. Non ci sono PR di bump, commit di versione né
token personali: tutto usa `GITHUB_TOKEN`.

## Come si calcola la versione

Il tag `vX.Y.Z` è l'unica registrazione di una release. Nel repository le
versioni sono segnaposto (`Chart.yaml`: `0.0.0`; il codice: `0.0.0-dev`) e il
workflow inietta quella vera in fase di build: nell'immagine con
`--build-arg VERSION=X.Y.Z` (`SMR_VERSION`, letta da `src/__init__.py`), nel
chart con `helm package --version X.Y.Z --app-version X.Y.Z`.

La versione successiva viene dai commit arrivati dopo l'ultimo tag `v*`
raggiungibile da `main` (`scripts/release/version.py`, con i suoi test in
`tests/test_release_version.py`):

| Commit su `main` (= titolo della PR) | Release |
|---|---|
| `feat: ...` | minor |
| `fix: ...`, `perf: ...`, `revert: ...` / `Revert "..."`, `fix(deps): ...`, `build(deps): ...` | patch |
| `tipo!: ...` oppure il footer `BREAKING CHANGE: ...` | major; **minor** finché la versione è `0.y.z` |
| `chore`, `docs`, `ci`, `test`, `style`, `refactor`, `build` (anche `chore(deps)`, `ci(deps)`) | nessuna |

Vince il livello più alto fra i commit dall'ultimo tag. Le PR si uniscono
con squash, quindi conta il **titolo della PR**: il check `commitlint` lo
controlla e il riepilogo del job dice che release produrrà il merge. Un
breaking change va segnato nel titolo (`feat(chart)!: ...`).

Anteprima in locale (servono i tag: `git fetch --tags`):

```bash
python scripts/release/version.py plan                  # prossima release e note
python scripts/release/version.py check-title "fix(worker): ..."
```

## Il workflow

```text
gate (ci.yml) ──► version ──► image ──► chart ──► release (tag + note)
```

1. **gate**: `ci.yml` richiamato con `workflow_call`, cioè gli stessi
   controlli delle PR: test con ffmpeg, pre-commit, `check_docs.py`, Helm lint
   + helm-unittest + kubeconform, actionlint e zizmor, build della
   documentazione, build e smoke test dell'immagine.
2. **version**: calcola la versione. Senza commit rilasciabili il workflow
   finisce qui: non si pubblicano immagini `edge` o di sviluppo, solo
   release.
3. **image**: build `linux/amd64` + `linux/arm64` con SBOM e provenance
   (`mode=max`) di BuildKit, push su GHCR **solo per digest**; smoke test
   dell'immagine pubblicata su entrambe le piattaforme, attestazione di
   provenienza GitHub, firma keyless con cosign; solo allora i tag `X.Y.Z`,
   `X.Y`, `X` (da 1.0.0), `latest` e `sha-<commit>`, che puntano proprio al
   digest firmato (il workflow lo controlla).
4. **chart** (dopo l'immagine, così un chart pubblicato ha sempre la sua
   immagine): `helm package` con la versione, push su
   `oci://ghcr.io/lucaam/charts/social-media-reuploader`, attestazioni
   (artefatto OCI e `.tgz`) e firma cosign.
5. **release**: tag `vX.Y.Z` sul commit e release GitHub con le note
   raggruppate per tipo (breaking, feature, fix, ...), gli artefatti con i
   loro digest e il `.tgz` del chart allegato.

Immagine e chart vengono pubblicati **prima** del tag: se qualcosa fallisce
non resta un tag senza artefatti, e il push successivo (o *Re-run failed
jobs* sullo stesso run) pubblica di nuovo la stessa versione. Image e Chart
si fermano se intanto un altro run ha già creato quel tag, così il re-run di
un run vecchio non sovrascrive una release più recente. Le release non vanno
mai in parallelo (`concurrency: release`, senza annullare quella in corso).
Gli eventi creati con `GITHUB_TOKEN` non avviano altri workflow: per questo
immagine, chart e release stanno nello stesso run. Ogni job ha solo i
permessi che gli servono (`contents: write` solo per creare tag e release,
`packages: write` / `id-token: write` / `attestations: write` solo per
immagine e chart).

Come verificare firme e attestazioni: [docs/ghcr.md](docs/ghcr.md).

## Avvio manuale

*Actions → Release → Run workflow*, sempre sul branch `main`:

- `bump`: livello minimo (`patch`, `minor`, `major`), anche senza commit
  rilasciabili; `major` su `0.y.z` rilascia `1.0.0`.
- `tag`: pubblica di nuovo immagine e chart di un tag esistente (es.
  `v0.9.0`), senza nuovo tag né release; `latest`, `X.Y` e `X` si spostano
  solo se è la release più alta. Il gate viene saltato: quel commit lo aveva
  già passato. Un valore che non è un tag di release ferma il run.

```bash
gh workflow run release.yml --ref main -f bump=patch
gh workflow run release.yml --ref main -f tag=v0.9.0
```

Un run in coda dietro a quello in corso viene sostituito dal successivo: se
un `tag` sostituisce un push in attesa, i commit di quel push escono con il
push seguente (o con un avvio manuale con `bump=auto`). Meglio ripubblicare
quando non ci sono release in corso.

## Dipendenze (Renovate)

- `requirements.txt` e immagine base del `Dockerfile` (fissata per digest,
  aggiornata ogni settimana): PR `fix(deps): ...`, il merge rilascia una
  patch con la nuova immagine.
- yt-dlp (minor e patch): unione automatica (squash) dopo 6 ore dalla sua
  pubblicazione e con i check verdi, quindi una patch release senza
  intervento.
- Strumenti di sviluppo e CI: `chore(deps)` / `ci(deps)`, nessuna release;
  raggruppati in PR settimanali.

## Impostazioni una tantum su GitHub

1. *Settings → General → Pull Requests*: consenti solo lo **squash merge** e
   come messaggio predefinito *Pull request title* (ora è "commit or PR
   title": con un solo commit verrebbe usato il messaggio del commit, che
   nessun check controlla). Abilita anche *Allow auto-merge* (Renovate).
2. *Settings → Rules → Rulesets → main*: i check obbligatori esistenti
   (`build`, `Python linters`, `Helm chart lint`, `GitHub Actions linter
   (actionlint)`, `Build Docker image (test)`, `Determine changed areas`,
   `commitlint`) hanno ancora gli stessi nomi; facoltativo `Docs build
   (mkdocs --strict)`. Nei metodi di merge consentiti lascia solo `squash`.
3. *Packages* (dopo la prima release): rendi pubblico il package
   `charts/social-media-reuploader` (*Package settings → Change visibility*) e
   controlla che anche `social-media-reuploader` sia pubblico; entrambi devono
   avere il repository in *Manage Actions access* con ruolo *Write* (succede
   da solo per i package creati dal workflow).
4. *Settings → Actions → General*: nessuna modifica necessaria (i workflow
   dichiarano i permessi job per job). Non servono più i secret
   `RELEASE_TOKEN` e `GHCR_PAT`: eliminali.
5. Non creare regole sui tag `v*` che impediscano a `github-actions[bot]` di
   crearli. Facoltativo: *Settings → General → Releases → Enable release
   immutability* (compatibile: gli asset vengono caricati prima della
   pubblicazione).

## Problemi frequenti

- *Nessuna release dopo il merge*: il titolo della PR non era rilasciabile
  (`chore`, `docs`, ...: vedi il riepilogo del job *Version*) oppure il gate
  è fallito. Per forzarla: avvio manuale con `bump`.
- *Il job Image o Chart è fallito*: il tag non è stato creato e nessun tag
  dell'immagine è stato spostato (l'immagine viene taggata solo dopo i test e
  la firma). *Re-run failed jobs* riprova con la stessa versione; altrimenti
  il prossimo push la ricalcola.
- *`vX.Y.Z is released already (by a newer run)`*: hai rilanciato un run
  vecchio dopo che un run più recente aveva già pubblicato quella versione;
  non c'è niente da fare.
- *`... already exists as a tag that is not an ancestor`*: esiste un tag `v*`
  con quella versione fuori da `main`; crea la release a mano o elimina il
  tag sbagliato.
- *Package GHCR privato*: vedi il punto 3 delle impostazioni.
