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
| `chore`, `docs`, `ci`, `test`, `style`, `refactor`, `build` tranne `build(deps)` (anche `chore(deps)`, `ci(deps)`) | nessuna |

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
gate (ci.yml) ──► version ──┬──► image (amd64, arm64) ──► index ──┬──► publish
                            └──► chart ───────────────────────────┘
```

1. **gate**: `ci.yml` richiamato con `workflow_call`, cioè gli stessi
   controlli delle PR: test con ffmpeg, pre-commit, `check_docs.py`, Helm lint
   + helm-unittest + kubeconform, actionlint e zizmor, build della
   documentazione, build e smoke test dell'immagine.
2. **version**: calcola la versione, e una volta per tutti i job i tag, le
   label e le annotazioni OCI dell'immagine. Senza commit rilasciabili il
   workflow finisce qui: non si pubblicano immagini `edge` o di sviluppo,
   solo release. Si ferma senza pubblicare niente anche se il commit è già
   dentro una release più recente (un run vecchio rilanciato) o se un tag di
   release fuori dalla storia del commit è uguale o più alto della versione
   successiva (vedi [Problemi frequenti](#problemi-frequenti)).
3. **image**: una build per piattaforma, ciascuna sul suo runner nativo
   (`ubuntu-24.04` per `linux/amd64`, `ubuntu-24.04-arm` per `linux/arm64`,
   niente emulazione), **da zero**: nessuna cache di build, che altri run
   (anche di una PR) potrebbero aver scritto. BuildKit e il generatore di
   SBOM sono fissati per digest; SBOM e provenance (`mode=max`) di BuildKit.
   Push su GHCR **solo per digest**, poi smoke test dell'immagine scaricata,
   sulla sua architettura.
4. **index**: l'indice con le due piattaforme, caricato anch'esso solo per
   digest, con attestazione di provenienza GitHub e firma keyless cosign.
5. **chart** (in parallelo all'immagine): `helm package` con la versione e
   attestazione del `.tgz`.
6. **publish**, l'unico job che rende pubblico qualcosa, e solo quando tutti
   gli altri sono riusciti: i tag `X.Y.Z`, `X.Y`, `X` (da 1.0.0), `latest` e
   `sha-<commit>` sull'indice firmato (il workflow controlla che puntino
   proprio a quel digest); poi il chart su
   `oci://ghcr.io/lucaam/charts/social-media-reuploader`, con attestazione e
   firma (dopo l'immagine, così un chart pubblicato ha sempre la sua
   immagine); per ultimi il tag `vX.Y.Z` sul commit e la release GitHub con
   le note raggruppate per tipo (breaking, feature, fix, ...), gli artefatti
   con i loro digest e il `.tgz` del chart allegato.

Se qualcosa fallisce prima di *publish* restano solo digest senza tag (con
le loro firme e attestazioni): nessun tag dell'immagine, nessun chart,
nessun tag Git. Il tag viene creato per ultimo, quindi dopo un errore *Re-run
failed jobs* sullo stesso run pubblica di nuovo quella versione, oppure il
push successivo la rilascia insieme ai propri commit.

Ogni job che carica qualcosa (*image*, *index*, *publish*) comincia con lo
stesso controllo: rilegge i tag `v*` dall'API di GitHub e si ferma se la
lettura fallisce, se il tag pianificato esiste già o se esiste un tag di
release più alto (per una ripubblicazione: solo se sposterebbe `latest`);
`tests/test_release_workflow.py` esegue quel controllo e lo tiene uguale
nei tre job.
*Re-run failed jobs* riusa il piano del primo tentativo: così un run vecchio
rilanciato dopo una release uguale o più recente non pubblica niente e non
riporta `latest` indietro. *Re-run all jobs* ricalcola il piano, che si
ferma già in *version*. Il controllo si fida dei tag: chi cancella a mano il
tag di una release lo aggira, per questo serve il ruleset dei tag (punto 5
delle impostazioni).

Le release non vanno mai in parallelo (`concurrency: release`, senza
annullare quella in corso). Gli eventi creati con `GITHUB_TOKEN` non avviano
altri workflow: per questo immagine, chart e release stanno nello stesso
run. Ogni job ha solo i permessi che gli servono (`contents: write` solo per
creare tag e release in *publish*; `packages: write`, `id-token: write`,
`attestations: write` solo dove si carica, firma o attesta).

Come verificare firme e attestazioni: [docs/ghcr.md](docs/ghcr.md).

## Avvio manuale

*Actions → Release → Run workflow*, sempre sul branch `main`:

- `bump`: livello minimo (`patch`, `minor`, `major`), anche senza commit
  rilasciabili; `major` su `0.y.z` rilascia `1.0.0`.
- `tag`: pubblica di nuovo immagine e chart di un tag esistente (es.
  `v0.9.0`), senza nuovo tag né release; `latest`, `X.Y` e `X` si spostano
  solo se è la release più alta di `main`. Il gate viene saltato (quel
  commit lo aveva già passato), quindi il tag deve essere nella storia di
  `main`: un tag di un altro branch ferma il run, come un valore che non è un
  tag di release.

```bash
gh workflow run release.yml --ref main -f bump=patch
gh workflow run release.yml --ref main -f tag=v0.9.0
```

Limiti della ripubblicazione:

- Immagine e chart si ricostruiscono dal commit del tag, con i suoi script.
  I tag precedenti a questo workflow (fino a `v0.8.1`) non hanno
  `scripts/smoke_test_image.sh` né la versione iniettata in fase di build:
  non si possono ripubblicare (il run si ferma nel job *image*, senza
  pubblicare niente).
- La build non è riproducibile bit per bit: immagine e chart ripubblicati
  hanno digest nuovi (di nuovo firmati e attestati) e i tag `X.Y.Z` passano a
  quelli. La sezione *Artifacts* delle note della release e il `.tgz`
  allegato restano quelli della prima pubblicazione: le note si possono
  correggere a mano (anche con le release immutabili), il `.tgz` allegato
  resta attestato ma non è più identico al chart OCI.

Un run in coda dietro a quello in corso viene sostituito dal successivo: se
un `tag` sostituisce un push in attesa, i commit di quel push escono con il
push seguente (o con un avvio manuale con `bump=auto`). Meglio ripubblicare
quando non ci sono release in corso.

## Dipendenze (Renovate)

- `requirements.txt` e immagine base del `Dockerfile` (fissata per digest,
  aggiornata ogni settimana): PR `fix(deps): ...`, il merge rilascia una
  patch con la nuova immagine.
- yt-dlp: unione automatica (squash) dopo 6 ore dalla sua pubblicazione e
  con i check verdi, quindi una patch release senza intervento. Vale anche
  per la prima versione di ogni anno: le versioni di yt-dlp sono date
  (`2026.11.20`), e per Renovate il cambio d'anno è un aggiornamento *major*.
- Strumenti di sviluppo e CI: `chore(deps)` / `ci(deps)`, nessuna release;
  raggruppati in PR settimanali. Ne fanno parte anche BuildKit e il
  generatore di SBOM usati dalle build (fissati per digest nell'`env` di
  `ci.yml` e `release.yml`, con lo stesso valore). Le azioni e gli strumenti
  dei job di pubblicazione si provano prima del merge con la *Release dry
  run* di `ci.yml`; solo una release esercita `actions/attest`, la firma
  cosign e il passaggio di artefatti fra i job (`upload-artifact` /
  `download-artifact`).

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
   Aggiungi agli obbligatori **`Release dry run`**: riassume la prova della
   release (piano, chart e immagine `linux/amd64` e `linux/arm64` come li
   costruisce la release) e passa anche quando la prova viene saltata (PR che
   non toccano immagine o release), quindi non resta mai in attesa. Così anche
   l'automerge di Renovate per yt-dlp aspetta che entrambe le immagini si
   costruiscano. Non rendere obbligatori i singoli `Release dry run (...)`:
   un job a matrice saltato non riporta i nomi per piattaforma e il check
   resterebbe in attesa.
3. *Packages* (dopo la prima release): rendi pubblico il package
   `charts/social-media-reuploader` (*Package settings → Change visibility*) e
   controlla che anche `social-media-reuploader` sia pubblico; entrambi devono
   avere il repository in *Manage Actions access* con ruolo *Write* (succede
   da solo per i package creati dal workflow).
4. *Settings → Actions → General*: nessuna modifica necessaria (i workflow
   dichiarano i permessi job per job). Non servono più i secret
   `RELEASE_TOKEN` e `GHCR_PAT`: eliminali.
5. *Settings → Rules → Rulesets → New ruleset → New tag ruleset*
   (consigliato): nome `release tags`, *Enforcement status* `Active`, target
   *Include by pattern* `v*` (i tag `refs/tags/v*`), regole *Restrict
   updates*, *Restrict deletions* e *Block force pushes*. Nessuno può più
   spostare o cancellare il tag di una release, su cui si basa il controllo
   dei job di pubblicazione; al workflow non serve alcun permesso in più,
   perché crea solo tag nuovi. Per far creare i tag `v*` **solo** al workflow
   attiva anche *Restrict creations* e metti nella *Bypass list* soltanto
   l'app *GitHub Actions* (cioè `GITHUB_TOKEN`; via API
   `{"actor_type": "Integration", "actor_id": 15368, "bypass_mode": "always"}`).
   Se la *Bypass list* non la propone lascia libere le creazioni: senza
   bypass il job *publish* non potrebbe creare il tag. Per cancellare un tag
   creato per errore (mai quello di una release pubblicata) disattiva il
   ruleset per il tempo necessario.
6. Facoltativo: *Settings → General → Releases → Enable release
   immutability*. Tag e asset di una release pubblicata non si possono più
   spostare, cambiare o cancellare e GitHub aggiunge un'attestazione della
   release; titolo e note restano modificabili. È compatibile con il
   workflow, che carica il `.tgz` nella bozza e solo dopo la pubblica. Il
   nome del tag di una release immutabile non si può più riusare, neanche
   cancellando la release.

## Problemi frequenti

- *Nessuna release dopo il merge*: il titolo della PR non era rilasciabile
  (`chore`, `docs`, ...: vedi il riepilogo del job *Version*) oppure il gate
  è fallito. Per forzarla: avvio manuale con `bump`.
- *È fallito Image, Image index o Chart*: non è stato pubblicato niente, ci
  sono solo digest senza tag. *Re-run failed jobs* riprova con la stessa
  versione; altrimenti il prossimo push la ricalcola.
- *Publish è fallito a metà*: i tag dell'immagine (ed eventualmente il chart)
  possono essere già pubblici, il tag Git no. *Re-run failed jobs* rifà
  *publish* dall'inizio: ogni passo si può ripetere.
- *`vX.Y.Z exists already (released by another run)`*, *`vA.B.C is a higher
  release than vX.Y.Z`* (controllo dei job di pubblicazione) oppure *`... is
  already in the release vA.B.C, tagged on a later commit`* (job *Version*):
  hai rilanciato un run più vecchio di una release già uscita, che contiene
  i suoi commit. Non c'è niente da fare, né da cancellare.
- *`... already exists as a tag outside the history of ...`* o *`... is
  higher than ... but outside the history of ...`*: un tag `v*` uguale o più
  alto della versione da pubblicare non è nella storia del commit (per una
  ripubblicazione: della punta di `main`). In locale, su un branch rimasto indietro, fai rebase su `main`. Su
  `main` vuol dire che il tag è stato creato fuori dal workflow, ad esempio a
  mano su un altro branch: controllalo (`git branch -r --contains vX.Y.Z`,
  pagina delle release). Se non ha una release pubblicata cancellalo
  (`git push --delete origin vX.Y.Z`, disattivando il ruleset dei tag) e
  rilancia il workflow. **Non cancellare il tag di una release pubblicata**
  finché esiste un'alternativa: se il tag fuori da `main` ha la stessa
  versione major dell'ultima release di `main`, rilascia sopra di esso con
  un avvio manuale e un `bump` sufficiente. Se ha una major più alta (es.
  `v2.0.0` fuori da `main` con `main` a `v1.3.0`) nessun `bump` lo supera:
  l'unica uscita è cancellare quel tag (disattivando il ruleset dei tag),
  ricreare a mano la sua release se serve, e rilanciare il workflow. Il
  ruleset dei tag del punto 5 serve proprio a evitarlo.
- *`vX.Y.Z (...) is not in the history of ...: only a release of main can be
  published again`*: si ripubblicano solo i tag di `main` (il gate viene
  saltato).
- *`cannot read the tags of ...`*: l'API di GitHub non ha risposto e il
  controllo, per sicurezza, ferma il job. *Re-run failed jobs* più tardi.
- *Package GHCR privato*: vedi il punto 3 delle impostazioni.
