# HANDOFF — OCR BiFunction

> Mémoire de passation entre sessions. **Lire en ouverture de session froide** (`/resume`) avant tout
> travail. Cadrage stable → [CLAUDE.md](CLAUDE.md) / [CADRAGE.md](CADRAGE.md). Décision moteurs →
> [docs/lecture-moteurs-paysage.md](docs/lecture-moteurs-paysage.md). **Ne pas dupliquer** ces docs ici :
> seulement l'état vivant + le prochain pas. Dates absolues.

> ⚠️ **Données sensibles** : `inputs/` (CI réelles, factures, photos terrain) et `outputs/` (extractions
> avec PII) sont **gitignorés** et n'ont **jamais** été versionnés. Aucune PII / donnée entreprise dans le
> repo ni l'historique (audité 2026-06-26). **Le repo EST sur GitHub (privé — le passage en public = décision utilisateur)** → ne jamais `git add -f` un doc,
> ne jamais coller de valeur réelle (nom, n°doc, adresse) dans le code, les docs ou un message de commit.

## État au 2026-07-13 — REFACTOR ARCHITECTURE en cours (A + D faits, B à finir)

> Issu de `/improve-codebase-architecture` (rapport HTML généré en temp, non versionné — les 6
> candidats A–F sont résumés ici). Chaîne de deepenings via `/grilling`. **A + D commités (`8392afb`),
> B step 1 commité (`1ebd164`)** ; steps 2-3 de B restent (working tree propre à part `CLAUDE.md`).
> Oracle = smokes autonomes (pas de pytest). Avant de reprendre : `uv run ruff check .` + relancer les
> smokes listés.
>
> **Self-healing review de `HEAD~9..HEAD` passée (2026-07-13, commit `931c4cd`).** 5 agents (sécu /
> fuites DB / code mort / magic-values / régression). Verdict : refactor **propre** — 0 code mort, 0
> fuite de connexion en prod (les 7 connexions API sont des singletons process-lifetime délibérés),
> `Verdict.d1_status`/`.wire_status`/`.from_reasons` **byte-équivalents** aux dicts de mapping
> supprimés, migration `human`→`review` complète. **1 seul durcissement appliqué** : `store_check.py`
> ferme le repo in-memory `wrapped` (cohérence close-everything du fichier, test-only). Advisory
> **laissés car mi-refactor** (décision utilisateur) : endpoints leviers `/v1/capacity-settings` sans
> auth (aucun endpoint de la maquette n'a d'auth) ; magic-values = vocabulaire cross-file (`"deferred"`,
> clés `SYNC_*`) ou surface config `capacity_settings.py`.

### Les 6 candidats (rapport d'archi 2026-07-13)
- **A — Verdict value object** — FAIT (commité `8392afb`).
- **B — un module de traitement unique** — EN COURS : step 1 fait (commité `1ebd164`), **steps 2-3 à faire** (le crux ci-dessous).
- **C — transport llama-swap unique** — À FAIRE (risque le plus faible).
- **D — Store + adaptateur in-memory** — FAIT (commité `8392afb`).
- **E — scinder `template.py`** au seam ~L202 (extraction vs moteur de verdict) + registre de checks — À FAIRE.
- **F — durcir le contrat `OcrEngine`/`TextLine`** (confidence/geometry) — À FAIRE.

### A — Verdict value object (FAIT)
- **`ocr_bifunction/verdict.py`** : `Verdict(AUTO/REVIEW/REJECT)`, `from_reasons(reject, review)` =
  l'UNIQUE précédence `reject>review>auto`, `.d1_status`/`.wire_status` = les seules sérialisations.
- **`ocr_bifunction/status.py`** : leaf des `STATUS_*` (`repository.py` les ré-exporte via `X as X`).
- Vocabulaire canonique **auto/review/reject** (`human` retiré partout, colonne D1 + wire compris).
- **Bug fermé** : `worker_watchdog._terminal_from_record` ne collapse plus `reject`→needs_review.
- Oracle vert : `verdict_check` 11/11, `reconcile_verdict_check` 5/5, `verdict_flow_check` 7/7,
  `escalation_reject_smoke` 5/5, `context_checks_check` 14/14, `checks_check` 12/12 + smokes FastAPI.

### D — Store + adaptateur in-memory (FAIT)
- **`ocr_bifunction/store.py`** : `Store(database=":memory:"|chemin, *, clock, check_same_thread)` =
  une connexion + `clock()` + `ensure_schema(ddl, *, table, migrations)` (le connect/executescript/
  PRAGMA-migrate/commit, une seule fois). `Store(":memory:")` = la même SQL en mémoire (repos
  partageant UNE connexion — des `:memory:` séparés = bases vides distinctes).
- Les **7 repos** : `__init__(store: Store | chemin)` → aliase `self._connection`/`self._clock`
  (corps de méthodes inchangés), appelle `store.ensure_schema(...)`. `clock`/`check_same_thread`
  remontés sur le Store.
- **`api_maquette._new_store()`** aux 7 sites (iso-concurrence : 7 connexions, comme avant).
- Oracle vert : `store_check.py` 7/7 (connexion partagée, round-trip, isolation, migration, path-accept).

### B — un module de traitement unique (EN COURS — step 1 FAIT, steps 2-3 À FAIRE)

**Step 1 FAIT** : **`ocr_bifunction/intake.py`** — `handle_document(item, templates_directory, engine, *,
escalation_engine=None, templates=None, context=None, today=None, conformity_policies=None) ->
DocumentOutcome` (PUR, ne touche AUCUN store) : compose `orchestrator.process_document` (pur, inchangé)
+ type-mismatch + réaction non-conformité. Plus `job_from_outcome(outcome, *, source, request_id,
document_ref, expected_holder_name, execution_lane)` = l'UNIQUE mapping record→Job. `DocumentOutcome`
= (record, status, verdict, reasons, retain_bytes, nonconformity). Prouvé : **`handler_check.py` 6/6**
sur `Store(":memory:")`.

**Décisions de conception (grilling — ne PAS re-litiger sans raison forte)** :
1. Handler **PUR → DocumentOutcome** ; les adaptateurs persistent (porte `save`, worker `update_status`).
   Crash-safety **inchangée** : les checkpoints durables restent aux adaptateurs (worker : row `processing`
   + `recover_stale` ; porte : `save` unique ou retry idempotent). Le handler est ré-exécutable.
2. **Nouvelle couche intake AU-DESSUS d'`orchestrator`** (qui reste pur `document→DocumentRecord`).
3. **Doubtful-CI → escalade = PORTE seulement** (adaptateur décide escalate-vs-finalize sur l'outcome).
4. **detected-type-mismatch DANS le handler** (unifie porte+worker). **Changement de comportement
   délibéré côté async** : un type-mismatch poussé async devient non-conforme (avant : RAG/needs_review)
   → couvrir par un nouveau smoke. Iso pour la porte sync.
5. **Rollout incrémental** (handler → porte → worker), un GATE vert à chaque étape.

**Deux edges restent dans les ADAPTATEURS (policy réelle par point d'entrée, PAS de la duplication)** :
- **Doubtful-CI escalate** : porte seulement.
- **Incomplete/unrecognized CI status** : la PORTE done-trace (l'uploader resoumet, aucun reviewer ne
  corrige un côté manquant) ; le WORKER/batch → needs_review (pas d'uploader à qui renvoyer).

#### STEP 2 — cutover PORTE (`api_maquette.py`) — À FAIRE
`validate_document` : **ADMISSION inchangée** (idempotence l.860, holder-block l.870, exec-policy l.893,
spool+enqueue async l.905, sync-slot/overflow l.923). Remplacer les DEUX appels
`_handle_ci_submission`/`_handle_single_document` (l.~958-971) par **UN seul chemin** :
- `item = BatchItem(paths=<temp files>, document_type=request.document_type)` (la dispatch CI-vs-routed
  passe DANS le handler) ; `ctx = _build_validation_context(request.expected_holder_name)` ;
  `policies = {p.category: p for p in _ensure_conformity_policy_repository().all_policies()}` ;
  `active = _ensure_template_repository().active_templates()`.
- `o = intake.handle_document(item, TEMPLATES_DIRECTORY, _get_engine(), escalation_engine=None,
  templates=active, context=ctx, today=date.today(), conformity_policies=policies)`.
- **EDGE (a)** `o.record.lane=="ci" and o.verdict=="review"` → `_spool_and_enqueue(...)` en lane
  `escalation`, wire `pending` (l'actuel comportement doubtful-CI, ancien l.573).
- **EDGE (b)** `o.record.detail in ("incomplete","unrecognized")` → `_save_job` d'un trace `done`
  (verdict None, PAS de spool) + wire `status` = `"incomplete"|"unrecognized"` (ancien l.580-598).
- **SINON** : `job = intake.job_from_outcome(o, source=<orig filenames>, request_id=...,
  document_ref=_spool_files(files) if o.retain_bytes else None, expected_holder_name=...)` ;
  `_save_job(job)` ; wire = `ValidateResponse(status=Verdict(o.verdict).wire_status, verdict=o.verdict,
  reasons=o.reasons, job_id=...)`.
- **PIÈGE source** : `item.paths` sont des fichiers TEMP (`file{suffix}`) → **passer `source=", ".join(
  filename for filename,_ in files)`** à `job_from_outcome`, sinon la row porte le nom temp.
- **SUPPRIMER après cutover** : `_handle_ci_submission`, `_handle_single_document`,
  `_nonconformity_response`, `_detected_type_mismatch`, `_run_fast_submission`, `_map_complete_auto`,
  `_map_incomplete_or_unrecognized`. `_run_route_document` : ne survit que si un autre caller l'utilise
  (le type-mismatch qui l'appelait part dans le handler) → sinon supprimer. **GARDER** (admission) :
  `_build_validation_context`, `_spool_files`, `_spool_and_enqueue`, `_holder_block_reason`,
  `_conformity_action_for`, `_new_store`, tous les `_ensure_*`.
- **GATE step 2** : `uv run python` de `verdict_flow_check`, `severity_smoke`, `holder_reference_smoke`,
  `conformity_smoke`, `flow_smoke`, `load_smoke` → tous verts (iso) ; + `ruff check .`.

#### STEP 3 — cutover WORKER (`worker_watchdog.py`) — À FAIRE
`_process_claimed_job` : remplacer la branche `ci`/`routed` (`_process_ci_job`/`_process_routed_job`)
par **UN seul chemin** :
- `item = BatchItem(paths=_spooled_files(job), document_type=("carte_identite" if job.category_lane==
  "ci" else job.category))` (⚠ pour CI il FAUT `document_type="carte_identite"` pour que le handler
  dispatche la soumission CI).
- `job_context = replace(validation_context, ci_reference_name=job.expected_holder_name)` ;
  `escalation = escalation_engine if job.category_lane=="ci" else None`.
- `o = intake.handle_document(item, TEMPLATES_DIRECTORY, fast_engine, escalation_engine=escalation,
  templates=active_templates, context=job_context, today=date.today(), conformity_policies=conformity_policies)`.
- `repository.update_status(job.job_id, o.status, verdict=o.verdict, record_fields=o.record.fields,
  reasons=[*job.reasons, *o.reasons], category_lane=o.record.lane, category=o.record.category,
  template_id=o.record.template_id)`.
- **SUPPRIMER** : `_process_ci_job`, `_process_routed_job`, `_terminal_from_record`. **GARDER** :
  `_one_pass` (recover/claim/loop), le sweep de clôture, la passe DRAFT nightly, `_spooled_files`.
- **NOUVEAU smoke** (le seul changement de comportement, décision B-4) : un doc déclaré type A poussé
  en `async_immediate`, qui matche un template de type B → le worker le classe **non-conforme**
  (avant : RAG/needs_review). Patron = `conformity_smoke` (TestClient + subprocess watchdog).
- **GATE step 3** : `conformity_smoke`, `policy_smoke`, `holder_reference_smoke`, `load_smoke`
  (subprocess watchdog) + le nouveau smoke + `verdict_flow_check` + `handler_check` + `store_check` ;
  + `ruff check .`.

#### Après B
- Doc : note `intake.handle_document` comme point d'entrée unique de traitement (dictionnaire ou
  contrat-bd) ; mettre à jour CE HANDOFF (B fait).

### Oracle global (pas de pytest — 2026-07-13)
- **Autonomes légers** : `checks_check`, `verdict_check`, `reconcile_verdict_check`,
  `context_checks_check`, `verdict_flow_check`, `escalation_reject_smoke`, `store_check`, `handler_check`.
- **FastAPI / subprocess** : `severity_smoke`, `holder_reference_smoke`, `conformity_smoke`,
  `policy_smoke`, `flow_smoke`, `load_smoke`, `corroboration_smoke`.
- **Exigent des args (docs `inputs/`, ne tournent pas seuls)** : `ui_smoke`, `promotion_check`,
  `suggestion_check`, `consolidation_check`, `batch_check`, `draft_check` — NON régressions si absents.

---

## État au 2026-07-12 (soir) — LIVRAISON PRÉPARÉE, l'échange IT commence

**Le flux COMPLET est prouvé depuis les surfaces** (upload → politique d'exécution → verdict 3 états →
revue humaine avec doc visible → drafting nightly → cochage/promotion → re-match) **+ les 7 surfaces
de config** (D1..D6 + leviers de capacité, chacune avec sa page) **+ la doctrine non-conformité**
(terminologie, réaction configurable block/block_holder/flag, sévérité par check, preuve retenue)
**+ la porte sous charge** (admission plafonnée, dégradation vers l'async, `load_smoke` 10/10).
Oracle = smokes autonomes PII-free (~100 checks verts) + runs réels. **Pas de tests pytest** —
discipline smoke-first.

**Livraison** : README architecture ; handoff IT → `0_Aller_retour_IT/…/LISEZMOI_HANDOFF.md`
(gitignoré) ; **remote GitHub PRIVÉ** `workworkworkfotsa-creator/ocr-bifunction` (master poussé) ;
zip 5,4 Go (repo + 4 GGUF + binaires dev) prêt à transmettre. Serving acté : llama.cpp + llama-swap
sur Linux (checklist → `docs/deploiement-linux-serving-slm.md`).

> ⚠️ **inputs/ nettoyé entre le 08 et le 12/07** : photos CI (IMG_8391/8392) et courriers disparus.
> Régressions re-pointées : `ui_smoke` = facture 14a + docx ; CI = `recto_verso.pdf --expect
> validated` (verte). Le cas live « recto A + verso B » n'est plus re-jouable (chemin couvert par
> `reconcile_verdict_check` 5/5 + `conformity_smoke`).

> ▶ **NEXT — l'ALLER-RETOUR IT.** L'ordre du jour de la première réunion = les 8 questions du
> `LISEZMOI_HANDOFF.md` ; les 3 qui débloquent tout : **(1) porte option A (serveur Python temps
> réel) vs B (PHP tout-async)**, **(2) version MariaDB réelle** (DDL à co-geler + dater),
> **(3) qui héberge llama-swap et avec quelle RAM** (→ leviers). Après la réunion : figer les
> décisions ICI + ouvrir le `plan_integration.md` dans `0_Aller_retour_IT/`.
>
> **EN ATTENTE (ne bloquent pas l'échange IT) :**
> 1. **Preuve fraude réelle → `rejected`** : DIFFÉRÉE — l'utilisateur soupçonne une attestation,
>    **attente de confirmation métier** (ne pas inventer de fraude de test à sa place).
> 2. **D-c partie 2, part SLM** : `normalize`/`pattern` pour les zones hors-famille (dates « Le 12
>    janvier 2024 », non-colon, tables) — le SLM propose, le déterministe dispose ; patron =
>    `suggestion.py`. (Nommage + checks candidats déterministes : FAITS et branchés au flux.)
> 3. **Upgrade liaison titulaire** (actée « plus tard ») : lire `ci_reference_name` depuis le
>    record CI validé en D1 au lieu de la saisie manuelle.
> 4. **`api_smoke_async` à re-pointer** : exige une paire CI genuinement douteuse, introuvable
>    dans le corpus actuel (préexistant, vérifié par A/B sur master).
> 5. **#RAG contrat — placement** (flux batch vs lane « store de contrats ») : indépendant.
> 6. **GO explicite requis** avant tout `draft_check --ocr` sur le scan H0B0 (machine partagée VRP).
>
> Décisions/concepts stables → `docs/dictionnaire-metier.md` (2 régimes d'émetteur, verdict 3 états,
> non-conformité, politiques, capacité) ; contrat → `docs/contrat-bd-destination.md`. **Piège soldé
> (2026-07-08)** : rétention du spool (une row `needs_review`/`rejected` garde ses bytes jusqu'à la
> clôture). **Finding D-a toujours vrai** : la similarité TF-IDF dépend du POOL — `--threshold` = le
> bouton.

Historique : `3fcc7a8` baseline ①②③ · `3c3d055` HANDOFF+hook · `19e8041` slot Preprocessor ·
`395e9e3` MRZ parse · `3680c87` rectifier + TD1.

### Ce qui tourne
- **① LIRE** — `reader.py` : routeur par type + couche texte. PyMuPDF (PDF born-digital), RapidOCR
  (images + pages image-only), python-docx. Chaque backend rend des `TextLine{texte, bbox, score, page}`.
- **Slot OCR jetable** — `OcrEngine` Protocol ; impl = `RapidOcrEngine`.
- **②③ CI** — `template.py` + `templates/*.json` : `match_template` (mini-② via anchors de signature) +
  extraction d'ancres (label → valeur dessous, même colonne X) + normalisation dates ISO.
- **Slot Preprocessor jetable** — `preprocess.py` : `NoPreprocessor` (défaut no-op), `EnhancePreprocessor`
  (gris + median blur + adaptive threshold), `PerspectiveRectifier` (4 coins → warp, fallback no-op).
- **MRZ** — `mrz.py` : `extract_mrz_lines` + `parse_mrz` dispatch par longueur → `parse_french_2line`
  (legacy 2×36) **ou** `parse_td1` (ICAO 3×30, lenient). `icao_check_digit` 7-3-1.

### KPI / findings (2026-06-26) — pas de verdict auto, l'utilisateur tranche
- Text-layer-first : 8 PDF + 2 docx lus **sans OCR**, en ms.
- **Confiance ≠ couverture** : un verso a scoré 0.93 en étant illisible → la confiance OCR ne dit pas si
  la lecture est *correcte*. Le **checksum MRZ** est le vrai signal « → humain ».
- Latence OCR 3.7–20.7 s/img CPU → **batch OK** ; **API** = seuls les petits passent, le reste **cascade**.
- **2 schémas MRZ** coexistent (legacy 2×36 + TD1 3×30) → dispatch par longueur. Validé sur réel : checks
  qui passent sur les deux formats.
- Sur une **vraie carte photographiée de travers**, `EnhancePreprocessor` a **récupéré la ligne MRZ dense**
  tuée par l'angle → parse TD1 complet, **3/3 checksums** (n°doc, naissance, expiration), champs
  concordants recto↔verso. `PerspectiveRectifier` n'a **pas** trouvé de quadrilatère (carte plein cadre)
  → no-op : la détection de coins auto demande mieux (ou coins manuels).

## Décisions actées
- **OCR = RapidOCR** (pip-only) — binaire Tesseract **absent** (2026-06-26).
- **Pas de SLM** pour l'instant. `granite-docling-258M.gguf` (local) réservé = lane **batch soir / escalade**.
- **Templates JSON éditables hors-code** = contrat ②③ validé par le Backoffice. **Plusieurs schémas par
  catégorie** (prouvé : 2 formats MRZ). Kaizen : on enrichit petit à petit ; l'engagement du filtreur fait
  vivre le système. Cible métier réelle : ~200 postes à ~90% d'erreur → auto-valider le concordant.
- **MRZ legacy non-ICAO** hand-rollé (la lib `mrz` PyPI ne couvre que TD1/TD2/TD3 ICAO).
- **Routing CATÉGORISER = 2 lanes** (acté 2026-06-27) : *structurés officiels* (CI, factures) → extraction
  template + auto-validation ; *non-structurés* (docx mémos, articles PDF) → **lane RAG** (retrieval, pas
  d'extraction). Les docx sont **volontairement** non-structurés ; **aucun document officiel en .docx**.
- Hors-scope tant que la valeur n'est pas étendue : stages ④⑤, UI de revue, gouvernance/trace.

## Fait (2026-06-26) — la thèse prouvée bout-en-bout
**Reconcile recto↔verso opérationnel sur la vraie paire** (`8d6bf4a`) : anchors flous (difflib, tolère
`rn→m`) + `reconcile()` sur les clefs partagées (n°doc, nom, prénom, naissance, expiration) + checksums.
Démo réelle : paire concordante → **AUTO** (5/5 clefs, 3/3 checksums) ; recto × MRZ d'une autre personne →
**HUMAIN** avec raisons (le « recto de A + verso de B » détecté). `reconcile_check.py` = le runner.

## Fait (2026-06-27)
- **Pipeline câblé (option 1 done)** : `main.py` = point d'entrée unique « paire CI → record + verdict » ;
  `pipeline.py` (`process_ci_pair` → `CiRecord`), verso **raw-first → enhance-retry**. AUTO prouvé sur la
  vraie paire (le read raw a suffi → 1 seul checksum ; cf. gate ci-dessous). Commit `43f229f`.
- **Composite TD1** ajouté à `parse_td1` (4e check ICAO ; lie doc/naissance/expiration → attrape un bloc
  MRZ incohérent). Validé par KAT contre le spécimen ICAO 9303. Commit `43f229f`.
- **Factures born-digital** : mode champ `pattern` (regex) dans `template.py` (PyMuPDF colle label+valeur
  dans un bloc → géométrie inapplicable). **4 templates** : `facture_sortante_01` (interne, TELIMA émet) +
  `facture_entrante_01/02/03` (reçues fournisseurs). Cross-match propre sur 5 docs ; les 2 courriers
  mise-en-demeure → aucun match. Lecture = **résolu** pour ce corpus (text-layer PyMuPDF, ms).
  Commits `67a8d5f`, `0651b74`. Ancres **structurelles** (jamais un nom de partie — repo public).
- **docx → lane RAG** (acté) : non-structurés volontaires, aucun doc officiel en .docx → pas d'extraction.
- **Docling fallback** : `DoclingOcrEngine` (`docling_engine.py`) derrière le slot `OcrEngine` ; image bytes →
  layout+OCR → `TextLine`. Smoke screenshot OK (géométrie top-left correcte) mais **lent** (~57 s init + ~38 s/img)
  → batch/escalade. **Backend OCR de Docling = RapidOCR** → valeur ajoutée = layout/reading-order (RAG), PAS une
  meilleure reconnaissance brute que RapidOCR seul. Commit `6a74fe6`.

## Fait (2026-06-28)
- **Benchmark VLM-OCR amorcé** sur images dures (photos d'écran `inputs/HP_preuve de testes`, gitignorées ;
  Tesseract n'en sortait que du bruit). **granite-docling-258M via Docling (transformers, CPU) = ÉCHEC** :
  2051 s et 307 s/img, sortie poubelle (`0 0 0…` en boucle / « Screenshot »). Modèle doc-conversion, inadapté
  aux photos d'écran. **Finding clé : ≤1B borne la taille, PAS la latence CPU** → VLM OCR = batch only, jamais
  l'API. (Throwaway, non commité.)
- **Ressources provisionnées (gitignorées)** : `models/` (GGUF locaux : granite-docling, PaddleOCR-VL,
  LightOnOCR ×2 + embeddings), `docs/Brainstoms/` (2 posts ref VLM-OCR + Surya×Docling). `.gitignore` durci.
- **Setup VLM local** : llama-swap (`~/Tools`) = texte only, **aucun mmproj** → voie GGUF/llama.cpp bloquée
  tant qu'on n'a pas les projecteurs vision. Sur Windows, tout download HF exige `HF_HUB_DISABLE_SYMLINKS=1`.
- **LightOnOCR-2-1B VALIDÉ (verdict utilisateur) comme moteur OCR d'escalade/batch** : tourne sur `b9542`
  (l'issue llama.cpp #18943 « not planned » était trompeuse — prouvé en le lançant). **Qualité parfaite** sur les
  photos d'écran HP là où Tesseract=bruit et granite=poubelle (markdown structuré, serials lus). RAM ~1.8 Go.
  **Latence ~171 s/img CPU → batch/escalade, jamais l'API.** Acté : **API = RapidOCR ; escalade cas durs =
  LightOnOCR-2.** mmproj Q8 dans `models/` (gitignored). Cf. mémoire `lighton-ocr-french-rgpd-preference`.
- **3 briefs ajoutés** (`docs/briefs/`, gitignored — internes) : maquette API (phase suivante), lane
  suggestion-template (GBNF), CADRAGE-META (machine commune des 3 repos).
- **Maquette API écrite** (`api_maquette.py`) : FastAPI **fin** au-dessus de `process_ci_pair` (pipeline/moteurs
  **non touchés**). Contrat Pydantic `ValidateRequest`/`ValidateResponse`/`JobResponse` ; `POST
  /v1/documents:validate` (décode base64 → fichiers temp système nettoyés → mapping `auto→validated` /
  `human→needs_review`) ; stub `pending`/202 sur flag debug `force_pending` ; `GET /v1/jobs/{id}` en mémoire
  (reste `pending`, pas de worker) ; idempotence jouet par `request_id` ; moteur RapidOCR lazy (1 seule
  construction). `fastapi 0.138.1` + `uvicorn 0.49.0` en `--dev` (uv.lock à jour). **Smoke structurel vert**
  (202/job/404/400/idempotence via `TestClient`). **Smoke vraies images (validated/needs_review) PAS encore
  fait** → DoD non clos (cf. NEXT). Friction shell notée : `uv`/`git` hors PATH Git Bash → route `cmd.exe`
  chemin absolu (`MSYS_NO_PATHCONV=1`) ; PowerShell bloqué par règle `deny` (pas dans `settings.json` global).

## Fait (2026-07-12)
- **LIVRAISON PRÉPARÉE — le premier échange IT peut commencer.** (1) **README.md** écrit
  (architecture complète porte/router/watchdog + D1..D6 + leviers, 3 modes, verdicts/non-conformité,
  smokes — public-safe, zéro détail de stack interne). (2) **Handoff IT spécifique** →
  `0_Aller_retour_IT/ocr_bifunction_handoff_2026-07-12/LISEZMOI_HANDOFF.md` (GITIGNORÉ — détails de
  stack interne) : décision n°1 à trancher (porte Python option A vs PHP tout-async option B),
  garder/réécrire/jeter, checklist Windows→Linux, PII/rétention, **8 questions ouvertes pour la
  première réunion**, brief « Pour le Claude de l'IT ». (3) **GitHub** : repo **PRIVÉ**
  `workworkworkfotsa-creator/ocr-bifunction` créé via API (gh absent ; credentials GCM) + master
  poussé — passer en public = décision utilisateur, réversible dans un seul sens. (4) **Zip de
  livraison** `0_Aller_retour_IT/ocr_bifunction_livraison_2026-07-12.zip` (5,73 Go, 156 entrées) :
  repo tracké + 4 GGUF + binaires dev Windows + LISEZMOI à la racine ; **garde PII vérifiée dans
  l'archive : aucune trace d'inputs/outputs/store/spool/briefs**. (5) **Skill handoff-it → v3**
  (leçons : N surfaces, leviers jusqu'à l'infra, serving=endpoint, checklist OS, porte qui dégrade,
  vocabulaire livrable).
- **Serving SLM sur Linux — décision ACTÉE + note de livraison écrite.** llama.cpp (`llama-server`)
  supervisé par **llama-swap** retenu (« le plus rapide et contrôlable ») ; **Ollama écarté** (« no
  way, trop peu de contrôle » + re-validation GBNF/multimodal à payer), **LocalAI écarté**, vLLM/TGI
  hors sujet (GPU). Le code ne dépend que de l'endpoint compatible OpenAI (`LLAMA_SWAP_URL`) — le
  serving est un adaptateur. **Checklist « sur Linux changer X et Y » →
  `docs/deploiement-linux-serving-slm.md`** (binaires linux-x64 pin b9542 ou re-valider, 4
  occurrences `.exe` du config.yaml, `-t` = cœurs physiques cible, models/ à provisionner avec le
  mmproj, systemd + bind 127.0.0.1 SEULEMENT — llama-server sans auth, validation à froid des 3
  slots dont le test GBNF « BANANE », pièges Windows-only à ne pas porter). Pointeur ajouté au
  contrat de destination.
- **Porte sous charge — worst case assumé : admission PLAFONNÉE + débordement configurable ;
  prouvé `load_smoke.py` 10/10.** Question utilisateur : « 1000 appels simultanés, la porte sync
  tient ? » Analyse honnête (persistée au dictionnaire « capacité de la porte ») : NON — threadpool
  FastAPI ~40 threads × OCR CPU-bound sur 4 cœurs = thrashing + OOM 8 Go, zéro admission control,
  zéro timeout, cache d'idempotence non borné. Décision utilisateur : serveurs modestes (pas de
  rack GPU) → prévoir le worst case ET rendre le plafond CONFIGURABLE (même gouvernance que
  sync/async/SLM) pour s'adapter au hardware du jour J. Livré : **leviers infra**
  `ocr_capacity_settings` (`capacity_settings.py`, table clé/valeur générique, patron VRP —
  `SYNC_CONCURRENCY_LIMIT` défaut 2, `SYNC_OVERFLOW_ACTION` défaut `defer`) ; **soupape
  d'admission** dans `validate_document` (compteur+lock à limite VIVANTE — pas un Semaphore figé) :
  saturée → `defer` = bascule async (202 pending, lane `deferred`, trace « capacity saturated »)
  ou `reject_503` (+ `Retry-After`) — **la porte ne fond jamais, elle dégrade vers le bi-mode** ;
  **cache d'idempotence borné** (LRU 1024, éviction du plus ancien) ; endpoints GET/PUT
  `/v1/capacity-settings` (gardes 422) + section « Capacité » sur `/policies`. **Prouvé 10/10**
  (12 uploads concurrents via threads + sonde de pic : pic mesuré = 2 ≤ plafond ; zéro 5xx ;
  validated+deferred = total ; rows `received/deferred` spoolées puis TOUTES drainées done/auto
  par le watchdog ; mode 503 avec Retry-After ; cap<1 → 422 ; flood de request_id → cache ≤ cap).
  Régressions vertes : flow 14/14, policy 20/20, conformity 12/12, holder 5/5, corroboration 7/7,
  severity 8/8, ui_smoke. **Limites assumées, notées au contrat (section « Leviers infra ») pour
  l'IT** : timeout dur mi-OCR = gateway IT (on ne tue pas un thread Python proprement) ; verrou
  global + connexion SQLite unique = artefacts du proxy (MariaDB + index les remplacent) ;
  idempotence cross-process re-dérivable de D1 ; concurrence watchdog = levier futur.
- **Sévérité PAR CHECK — le bouton métier « durcir / adoucir » construit + prouvé `severity_smoke.py`
  8/8 (dette du bullet précédent SOLDÉE, demande utilisateur « à construire »).** Une règle du bloc
  `validation.required` peut porter **`"severity": "reject" | "review"`** — la classe d'un échec
  DÉTERMINÉ devient config métier, voyageant avec le template (cas nommé : registre de confiance →
  `issuer_registry` durci, « émetteur ≠ Y → non valide »). **Le garde-fou input-vs-preuve SURVIT à la
  config** : `CheckFailure` gagne `determined` (7 branches négatives marquées : sum, date_order ×2,
  date_span, vocabulary, reconcile_ci, issuer_registry hors-registre, corroborated_by non-adossé) et
  l'override ne s'applique QU'À elles — un « je ne peux pas savoir » (registre vide, input illisible)
  part TOUJOURS en revue ; une valeur de sévérité inconnue fait surface en raison de revue (typo
  fail-loud, même quand tout passe). Promotion : le cochage tolère une `severity` attachée à un
  candidat (comparé hors-severity ; valeur inconnue → 400) + select « défaut / non conforme / revue »
  par candidat sur la carte draft. **Prouvé 8/8** : durcissement émetteur → rejected ; registre vide
  → needs_review MALGRÉ severity=reject ; émetteur reconnu → auto ; contrôle vocabulary → rejected
  puis ADOUCI → needs_review ; typo → raison explicite ; promotion écrit la sévérité en D2 + garde
  400. Régressions vertes : verdict_check 11/11, checks 12/12, context_checks 14/14, conformity
  12/12, flow 14/14, holder 5/5, corroboration 7/7, verdict_flow 7/7, ui_smoke. **Se compose avec la
  [politique de non-conformité]** : la sévérité règle la CLASSE (non conforme vs revue) par check ;
  la politique D6 règle la RÉACTION (block/block_holder/flag) par catégorie.
- **« Document NON CONFORME » — terminologie actée + politique de réaction configurable métier ;
  prouvé `conformity_smoke.py` 12/12.** Décisions utilisateur : la machine prouve une
  NON-CONFORMITÉ, la FRAUDE est le jugement de compliance (mot souvent exagéré) ; la preuve est
  RETENUE et « passe par la revue humaine » ; et il manquait LA config de réaction : « on bloque
  les uploads suivants, ou pas, ou on flag mais le process continue ». Livré : **D6**
  `ocr_conformity_policies` (`conformity_policy.py`, patron leviers, `*`=block par défaut) avec
  3 actions — `block` (cet upload refusé), `block_holder` (+ uploads suivants du même titulaire
  déclaré refusés tant que la non-conformité est OUVERTE ; « clore » débloque ; une row-trace de
  blocage — sans document retenu — ne re-bloque jamais elle-même, bug attrapé par le smoke),
  `flag_and_continue` (rien de bloqué : flag + needs_review). Résolution sur le type DÉCLARÉ
  d'abord (un passeport envoyé comme CI = incident carte_identite). **Rétention de la preuve** :
  les rows `rejected` gardent leurs bytes (porte + watchdog) ; section « Documents non conformes »
  sur la page revue (doc + « non validé car Y » + note compliance) ; « Clore » = décision D3 → le
  sweep purge la preuve, status reste `rejected`. **Check « type déclaré ≠ type reconnu »** : un
  doc sans match dans sa catégorie déclarée mais qui matche un template d'une AUTRE catégorie →
  non conforme (« type mismatch: declared X, recognized Y ») — branché single-doc ET flux CI
  (unrecognized → re-route ; coût = 2e lecture, simplification maquette) ; le reject CI
  recto↔verso passe aussi par la politique (+ paire retenue). Watchdog : mêmes règles sur les
  lanes async (flag → needs_review). UI : 2e table « non-conformité » sur `/policies`, libellé
  upload « Document non conforme — ne peut pas aller plus loin ». **Prouvé 12/12** (rétention ;
  file non-conformes ; clore→purge ; flag ; blocage titulaire + déblocage ; 2 type-mismatch ;
  async flaggé ; gardes 422/400 ; page). Régressions vertes : flow 14/14, holder 5/5,
  corroboration 7/7, policy 20/20, verdict_flow 7/7, review, ui_smoke (docs re-pointés),
  `api_smoke_real recto_verso.pdf --expect validated`. La dette « sévérité PAR CHECK » notée ici a
  été **construite le jour même** (cf. bullet ci-dessus, `severity_smoke` 8/8).

## Fait (2026-07-08)
- **Rôles d'attestation configurables par le MÉTIER — `corroborated_by` tire de bout en bout à
  travers la porte ; les 2 régimes d'émetteur sont opérationnels dans le flux.** Décision
  utilisateur : le mapping « quels champs du record = titulaire / délivrance / expiration » doit
  être configurable par le métier → bloc **`attestation_reference_roles`** qui voyage AVEC le
  template (comme les checks) : nouvelle colonne D2 `reference_roles_json` (+ migration auto,
  `template_repository.py` — piège attrapé : l'upsert D2 aurait silencieusement PERDU le bloc) ;
  **assignation par le reviewer à la promotion** (3 selects « titulaire / délivrance / expiration »
  sur la carte draft de `review.html`, parmi les champs du draft — les 3 ou aucun ; gardes 400 :
  mapping incomplet, champ inexistant) ; `ocr_bifunction/context_assembly.py` =
  `collect_validated_attestations` (jobs D1 `done` des templates à rôles → `AttestationReference`,
  projection mécanique, zéro code par type de doc). Contexte branché porte (par requête) + watchdog
  (par passe — une attestation fraîchement close corrobore dès la passe suivante). **Prouvé** :
  `corroboration_smoke.py` **7/7** (round-trip D2 du bloc ; attestation validée on-file ; titre
  même titulaire dans la fenêtre → **validated/auto corroboré** ; titre sans attestation couvrante
  → review (jamais reject — « ma mère peut me faire une certif » = en attente, pas prouvé faux) ;
  titre hors fenêtre → review ; gardes 400 ; la promotion écrit le bloc en D2). Régressions
  vertes : flow 14/14, holder 5/5, policy 20/20, review, promotion, ui_smoke.
- **Titulaire déclaré (liaison doc↔titulaire MANUELLE) — décision utilisateur implémentée + prouvée.**
  Réponses D-e : pas de fraude confirmée (soupçon attestations, attente métier) ; le titulaire est
  saisi À LA MAIN pour l'instant (l'auto-liaison depuis le record CI D1 = upgrade plus tard).
  Livré : champ optionnel **`expected_holder_name`** sur `ValidateRequest` + input « Titulaire
  attendu » sur l'upload ; **colonne D1** (`ocr_jobs.expected_holder_name`, migration auto) → le
  déclaré VOYAGE avec les jobs async ; porte : `_build_validation_context(expected_holder_name)` →
  `ci_reference_name` ; watchdog : contexte PAR JOB (`dataclasses.replace` sur le contexte de passe).
  **Prouvé** : `holder_reference_smoke.py` **5/5** (match→auto ; mismatch→**rejected** terminal —
  la fraude fratrie ; absent→review fail-loud ; async : la row porte le déclaré et le watchdog
  rejette le mismatch). Régressions : flow_smoke 14/14, policy_smoke 20/20, review_check, ui_smoke.
- **FLUX COMPLET fermé depuis les surfaces — upload → décision → revue (doc visible) → drafting
  automatique → cochage → promotion → re-match ; prouvé `flow_smoke.py` 14/14 + navigateur réel.**
  Décisions utilisateur : (1) garder le spool ; (2) lier le drafting au flux ; (3) doc ET extraction
  côte à côte en revue (« cela nous permet aussi d'évaluer l'extraction ») ; plugger D-c et D-e.
  Livré : **(A) rétention du spool** — toute row `needs_review` garde ses bytes (`document_ref`),
  y compris la lane sync (`_spool_files`) ; le watchdog ne purge PLUS sur needs_review, le **sweep
  purge à la clôture** ; endpoint `GET /v1/jobs/{id}/document` (+`?index=`) ; la file de revue expose
  `documents[]` et `review.html` rend le doc (img/embed PDF) à côté des champs/raisons (prouvé
  Playwright, screenshot envoyé). **(B) passe DRAFT dans le flux** — `ocr_bifunction/drafting_flow.py`
  (`run_draft_pass`) branchée sur `--nightly` : unknowns needs_review avec bytes → cluster D-a →
  draft D-b → **D-c partie 2 déterministe** (`drafting.seed_candidate_checks` : champs 100 % dates →
  `normalize date_ddmmyyyy` + candidats `date_order`/`date_span` à écart d'années constant ;
  **vocabulary avec garde PII par RÉCURRENCE** — un token n'entre dans `allowed` que s'il revient
  dans ≥2 docs : un nom de titulaire n'y entre jamais, un code réglementaire oui) → re-test gate D-b
  inchangée (candidats infaisables droppés avec raison) → **D-c partie 1 opt-in `--slm-naming`**
  (granite nomme ; serveur mort → placeholders + raison, jamais bloquant) → stage D3 sur le 1er job
  du cluster. **Idempotent** (suggestion déjà stagée → skip) ; images sans OCR → skip sauf
  `--draft-ocr` (frein machine partagée) ; catégorie = le type déclaré par l'appelant (fix : la lane
  rag persiste maintenant `document_type` au lieu de None). **(C) plomberie D-e** —
  `ocr_bifunction/issuer_registry.py` (table `ocr_issuer_registry`, curation métier) + endpoints
  GET/PUT/DELETE `/v1/issuer-registry` + page `/registry` ; **contexte câblé** : porte API et
  watchdog passent `ValidationContext(issuer_registry=…)` + `today` à `route_document` (registre
  vide → None → review fail-loud). `reconcile_ci`/`corroborated_by` restent fail-loud → **questions
  D-e posées à l'utilisateur** (les 2 docs fraude ; liaison doc↔titulaire ; mapping attestation).
  **Prouvé** : `flow_smoke.py` **14/14** (bytes servis = originaux ; draft stagé par la passe avec
  4 types de candidats ; garde PII (l'ancre organisme « SPECIMEN SAS » ≠ PII titulaire, faux positif
  de smoke corrigé) ; idempotence ; subset coché promu ; 4e doc re-matche `validated/auto` avec
  **dates ISO au record** (normalize prouvé par extraction) ; sweep purge le spool ; registre CRUD).
  Régressions vertes : policy_smoke 20/20, ui_smoke 15/15, draft_smoke 12/12, checks 12/12,
  context_checks 14/14, verdict_flow 7/7, review_check.
- **Politiques d'exécution — la surface de config « QUAND traiter » livrée + prouvée (demande
  utilisateur : le mapping catégorie→régime doit être une config opérée, les infra/besoins changent ;
  cohabitation avec la variable optionnelle de l'API).** Nouveau domaine **D4** `ocr_execution_policies`
  (`ocr_bifunction/execution_policy.py` : `ExecutionPolicyRepository` ABC + proxy SQLite, patron
  « leviers » handoff-it — défauts DANS le code `DEFAULT_EXECUTION_POLICIES`, seed idempotent qui
  n'écrase JAMAIS une édition opérateur). 3 modes : `sync` (dans la requête) / `async_immediate`
  (lane D1 `deferred`, watchdog continu) / `async_nightly` (lane `nightly`, drainée SEULEMENT par
  `worker_watchdog.py --nightly` = le seam cron IT). **Résolution pure** (`resolve_execution`) : ligne
  catégorie sinon `*` ; hint client `processing_mode` honoré SEULEMENT si `override_allowed` (défauts
  seedés : `*`=sync+override, `carte_identite`=sync VERROUILLÉE — son doute escalade par son propre
  chemin) ; tout tracé dans `reasons`. Câblage : la porte `validate_document` résout AVANT de
  dispatcher (async → spool + row `received`, non-CI = `category_lane='unrouted'`) ; le watchdog
  draine lane par lane et gagne `_process_routed_job` (route_document depuis le spool, templates D2,
  row FINALISÉE — `update_status` étendu `category_lane`/`category`/`template_id`) ; UI : page
  `/policies` (liste/édite/revert, zéro logique métier) + select `processing_mode` sur l'upload ;
  endpoints GET/PUT/DELETE `/v1/execution-policies`. **Prouvé** : `policy_smoke.py` **20/20**
  (autonome, corpus synthétique draft_smoke, zéro OCR/SLM : défauts seedés ; `*`→sync ; nightly →
  202 + row nightly/unrouted + la passe plaine NE la claim PAS + `--nightly` la route → done/auto +
  spool purgé ; hint ignoré/honoré ; deferred drainé par la passe par défaut ; delete → retombe sur
  `*` ; gardes 400/422/404). Régressions vertes : `ui_smoke` 15/15, `verdict_flow_check` 7/7,
  `review_check`, `promotion_check`, + micro-run réel lane escalation CI (claim → done/auto, spool
  purgé) post-refactor `_process_claimed_job`. Docs : D4 + contrat de colonnes dans
  `contrat-bd-destination.md`, entrée dictionnaire « politique d'exécution ».
- **⚠️ Finding — `api_smoke_async.py` FAIL, PRÉEXISTANT (vérifié par A/B sur master sans mes
  changements : même sortie).** La vraie paire (IMG_8391/8392) lit désormais **validated** en
  fast-path (plus de doute → pas d'escalade), et depuis `bab3ab7` un recto A + verso B sort
  **rejected sync** (plus `pending`). Le smoke attend `202 pending` → il lui faut une paire
  GENUINEMENT douteuse (complete + human), introuvable dans le corpus actuel. À re-pointer ou faire
  évoluer — le chemin escalade lui-même reste prouvé (micro-run réel ci-dessus).

## Fait (2026-07-03)
- **CI/MRZ recto≠verso → `reject` — dernière pièce du câblage verdict. Décision utilisateur : « tout
  mismatch → reject ». `bab3ab7`.** `reconcile()` renvoie désormais un verdict 3-états : une clef PARTAGÉE
  qui DIVERGE entre recto et MRZ (2 reads indépendants nommant 2 identités — « recto de A + verso de B »)
  → **reject** (prouvé invalide, terminal). Un **checksum MRZ KO** ou rien à comparer → **human** (read
  NON FIABLE, pas une fraude prouvée : un seul digit mal lu par l'OCR casse un checksum → jamais un
  auto-reject sur bruit OCR). Toutes clefs concordent + checks OK → auto. Câblé aval : `CiRecord.verdict`
  le porte ; `orchestrator._record_from_ci` mappe via `_OUTCOME_FROM_VERDICT` → `STATUS_REJECTED` ; l'API
  CI gagne une branche complete+reject → status `rejected` **sans escalade** (un OCR plus lourd ne sauve
  pas 2 faces nommant 2 personnes). Prouvé : `reconcile_verdict_check.py` **5/5** (match→auto ; divergence
  nom→reject ; checksum KO→human ; recto vide→human) ; `verdict_flow_check` 7/7 intact. **→ Le verdict
  `reject` est maintenant CÂBLÉ DE BOUT EN BOUT (structuré + CI/MRZ).** Concept persisté → dictionnaire.
- **Verdict `reject` CÂBLÉ à travers le flux (lane structurée) — prouvé bout-en-bout. `ab397d6`.**
  Suite du classifieur : `reject` voyage maintenant de bout en bout. **`STATUS_REJECTED`** rejoint D1
  comme état TERMINAL (distinct de `failed` = crash de traitement, pas un verdict de validité). Mapping
  par couche : (1) `router._structured_result` appelle `evaluate_validation` → `RoutedDocument.verdict` ∈
  {auto,human,reject} ; `route_document` gagne `context`/`today` (nourrissent les checks anti-fraude ;
  contexte absent → review, jamais un faux reject). (2) `orchestrator` : `DocumentRecord.outcome` ∈
  {auto,review,reject} + `BatchResult.rejected` + threading `context`/`today`. (3) sink `batch_check` :
  reject → `STATUS_REJECTED`/verdict `reject`. (4) API : `ValidateResponse`/`JobResponse` + `rejected`,
  single-doc structuré mappe via `_{D1,WIRE}_STATUS_FOR_VERDICT`, `GET /v1/jobs` traite rejected comme
  terminal. **Prouvé** : `verdict_flow_check.py` **7/7** (clean→auto/done ; validité rallongée + code
  inventé → reject via `route_document` ET `process_batch` → `BatchResult.rejected` → bridge sink
  `STATUS_REJECTED`) ; `review_check` vert. **RESTE (à trancher)** : **CI/MRZ → reject** (recto≠verso).
  `reconcile.py` renvoie encore auto/human ; un mismatch de clef doit → reject MAIS le MRZ vient d'OCR
  (un caractère mal lu = faux mismatch → risque de rejeter une vraie carte). Règle proposée : reject
  SEULEMENT si les checksums MRZ PASSENT (read fiable) ET une clef recto/mrz diverge ; checksum KO →
  review (read non fiable). À confirmer.
- **Verdict à 3 ÉTATS (`auto`/`review`/`reject`) — le classifieur posé + PROUVÉ ; concept métier
  confirmé + persisté. `b50ae05`.** Affinage utilisateur : un document PROUVÉ invalide n'est pas « à
  revoir », il est **REJETÉ** (rejet AUTO terminal, pas d'humain — décision Q1). La nuance = **« je ne
  connais pas » (→ humain) ≠ « je sais que c'est faux » (→ rejet)**. `template.py:evaluate_validation`
  classe chaque échec : `REJECTING_CHECKS` = {`date_order`,`date_span`,`vocabulary`,`reconcile_ci`} →
  **reject** ; tout le reste (`present`, `issuer_registry`, `corroborated_by`, no-match template) →
  **review** (décision Q2 : émetteur inconnu = peut-être légitime, titre non adossé = en attente).
  Priorité **reject > review > auto**. `ValidationOutcome` (reject_reasons/review_reasons/`verdict`) ;
  `validate_fields` devient un **wrapper rétrocompatible** (les gates de re-test drafting/naming/suggestion
  intacts — ils ne veulent que « vert/pas vert »). Prouvé : `verdict_check.py` **8/8** ; régressions
  12/12 + 14/14 + 12/12. Concept persisté → `docs/dictionnaire-metier.md` (`## verdict de routing`).
  **Reste à câbler aval** : statut terminal `rejected` en D1 (`repository.py`), mapping API/batch, et
  passer `context`/`today` réels jusqu'à l'appel — le classifieur existe, le routage pipeline pas encore.
- **Kit de checks anti-fraude — 3 checks CONTEXTUELS (`reconcile_ci`/`issuer_registry`/`corroborated_by`)
  codés + PROUVÉS ; les 2 régimes d'émetteur tiennent bout-en-bout. `97075e2`.** Complète le kit (6 checks
  au total) avec ceux qui exigent un état EXTERNE, via un `ValidationContext` (dataclass, param keyword-only
  sur `validate_fields`, rétrocompatible). **Garde-fou fail-loud** : un check contextuel déclaré SANS son
  état → échec explicite (`needs_review`), jamais un pass silencieux (un registre absent ne peut PAS prouver
  un émetteur légitime). (1) `reconcile_ci` : le champ titulaire concorde STRICTEMENT avec le record CI —
  **réutilise `reconcile._normalize`** (fold accents seul, Ahmed≠Hamed, mémoire reconcile-name-match-strict).
  (2) `issuer_registry` : l'émetteur lu (SIRET préféré) ∈ registre curé d'organismes — la preuve forte du
  régime `attestation_formation` ; un émetteur maison échoue. (3) `corroborated_by` : un `titre_habilitation`
  auto-déclaré n'est AUTO que si une `attestation_formation` validée en D1 couvre le MÊME titulaire (strict)
  avec le titre émis DANS la fenêtre de validité de la formation — « ma mère peut me faire une certif »
  encodé. `AttestationReference` porte une attestation validée (titulaire + fenêtre). **Prouvé** :
  `context_checks_check.py` **14/14** (chaque check passe le propre, tire sa fraude — nom du frère, émetteur
  maison, titre non corroboré — et fail-loud sans contexte ; 2 régimes bout-en-bout). Régressions vertes :
  `checks_check` 12/12, `draft_smoke` 12/12. **Reste** : câbler le contexte réel dans le flux (D-e) +
  proposer ces checks en candidats au drafting (D-c partie 2). Oracle = run réel, pas de pytest.
- **Kit de checks anti-fraude — 3 checks PURS (`date_order`/`date_span`/`vocabulary`) codés + PROUVÉS
  (déterministe, sans machine). `7a67297`.** Cousins du `sum` dans `template.py:validate_fields`,
  config-driven, voyageant avec le template (compute-all/config-requires) : `date_order` (délivrance <
  expiration + `require_future` opt-in = pas expiré), `date_span` (expiration == délivrance + N années
  calendaires, tolérance jours — une validité rallongée au stylo casse l'équation ; Feb 29 → Feb 28 géré),
  `vocabulary` (chaque token d'un champ ∈ liste fermée `allowed`, case-insensitive — un code inventé
  échoue). `validate_fields` gagne un `today` **keyword-only** (fraîcheur reproductible en test ;
  rétrocompatible — appelants positionnels intacts). **Prouvé** : `checks_check.py` **12/12** (chaque check
  passe le propre ET tire sa fraude : fenêtre inversée/expirée, 3→5 ans, code B9Z ; leap-day) ;
  `draft_smoke` **12/12** (chemin `present` intact). **RESTENT les 3 CONTEXTUELS** (`reconcile_ci`,
  `issuer_registry`, `corroborated_by`) : pas des fonctions pures de `(fields, rule)` → évaluateur porteur
  de contexte (record CI / registre organismes / D1 attestations validées), tranche suivante = les 2
  régimes d'émetteur + l'oracle D-e. Oracle = run réel, pas de pytest.
- **D-c PARTIE 1 — le SLM contraint NOMME les champs placeholder du draft ; PROUVÉ LIVE (granite via
  llama-swap, corpus synthétique PII-free, zéro OCR). `f578445`.** Suite déterministe de D-b : le draft
  sort avec des noms placeholder (slugs de label) ; `ocr_bifunction/field_naming.py` réveille granite
  (`/completion` + `json_schema` : `placeholder` = enum des champs DU draft → impossible de nommer un champ
  inexistant ; `name` = string libre) pour proposer un nom sémantique par champ. **Le SLM propose, le
  déterministe dispose** : `_sanitize_name` (ASCII-fold + slug → identifiant sûr), unicité garantie,
  **fallback au placeholder** sur vide/collision → le mapping est TOUJOURS total et sans collision ; puis
  **re-test inchangé** (`_draft_retests_green` = match + extract + validate sur TOUT le cluster) — un
  renommage qui casse l'extraction (impossible, c'est un pur relabel) → rejet, draft original gardé.
  **PII-free** : le prompt n'envoie QUE les placeholders (déjà des slugs structurels) + la méthode
  d'extraction (below/right/pattern), **jamais les valeurs** (la PII du titulaire) — même discipline que
  `suggestion.py`. **Prouvé** : `field_naming_check.py` **10/10** (réutilise le corpus de `draft_smoke`) —
  granite a renommé les 5 champs attestation (`nom_du_titulaire`→`name_of_holder`, `delivree_le`→
  `date_of_issue`, `codes_obtenus`→`codes_obtained`…), re-test vert sur le cluster 3-docs, **invariance des
  valeurs** vérifiée (le relabel change les noms, pas les valeurs), règles `validation.required` suivent le
  renommage (zéro champ pendant). Warmup granite ~129 s (load), appels ensuite = secondes ; machine libérée
  (mon task llama-swap seul arrêté, 0 orphelin). **Additif — aucun module existant touché** (zéro régression).
  **PARTIE 2 restante** (normalize/pattern hors-famille + CHECKS candidats) **bloquée sur le kit de checks
  non codé** : sans validateur, une proposition de check n'a rien à re-tester. Oracle = run réel, pas de pytest.
- **D-b v2 — famille prefix-pattern + gates durcis ; RE-RUN RÉEL VERT (nom/prénom extraits). Une fuite
  PII dans l'invariance trouvée par repro et SOLDÉE.** Corrections nées du 1er run réel : (1) **fuite
  PII** : l'invariance réutilisait le prédicat FUZZY du match (fait pour les slips OCR) → une ligne
  « label : VALEUR » quasi-identique cross-docs (long préfixe commun, ratio 0.88 > 0.75) passait en
  ANCRE avec sa valeur (repro synthétique : la référence de dossier) → invariance = **égalité normalisée
  EXACTE** (exact ⊂ fuzzy : l'ancre reste matchable) + `draft_smoke` durci (TOUTE valeur per-doc interdite
  en ancre, plus seulement noms/dates). (2) **Famille colon-prefix** (`_seed_pattern_field_candidates`) :
  « label : valeur » collés dans UN block PyMuPDF → champ **`pattern`** (même chemin d'extraction que les
  factures) ; label invariant EXACT cross-docs (filtre PII), mot ≥3 lettres (« Nom » est un vrai label —
  la garde ≥4 des ancres était trop stricte, vécu au re-run). (3) **Garde anti-dump** : valeur extraite
  >120 chars ou >1 saut de ligne = table/block, droppée avec raison (« mécaniquement stable » ≠ champ).
  (4) **`_value_below/right` filtrent `page_index`** (`template.py` — iso prouvable : tous les templates
  géométriques actuels sont mono-page ; vécu : un block p1 « sous » un label p0). **Re-run réel (pool
  4 certificats)** : anchors inchangés propres, champs = **nom + prénom du titulaire** (valeurs réelles
  vérifiées en console, non reproduites ici — repo public), 8 candidats droppés avec raisons explicites.
  `draft_smoke` **12/12** (+ cas « ligne collée → pattern »), `ui_smoke` **15/15** (5 candidats cochables).
  **2 findings notés** : (a) la similarité TF-IDF dépend du POOL (IDF) — la paire seule score 0.49<0.5,
  dans un pool de 4 → 0.59 ; cas réel = file d'unknowns mélangée, `--threshold` = bouton si besoin ;
  (b) les dates de formation (non-colon, enfouies dans les tables) = frontière D-c.
- **D-d — le draft VOYAGE jusqu'à D2 par le geste humain : schéma D3 + revue v2 (COCHAGE des checks) +
  promotion + re-match, prouvé en NAVIGATEUR RÉEL (zéro llama, zéro OCR — corpus synthétique).**
  (1) **D3** : colonne **`suggested_template_json`** (+ migration auto des .sqlite existants, même patron
  que D1) — le DRAFT COMPLET voyage avec la suggestion (`Suggestion.template`) ; les suggestions
  liste-fermée (lane SLM) gardent `template=None`, zéro régression. (2) **API** : `/v1/suggestions/pending`
  expose `draft_template` + ses checks candidats ; `POST …/validate` accepte le **cochage** (subset des
  candidats — l'humain choisit, n'écrit pas de règles ; 400 si règle hors-candidats ; sans body = tout
  reste requis, rétrocompatible) → `validation.required` promu = EXACTEMENT les checks cochés
  (compute-all/config-requires). (3) **Trou d'intégration soudé** : le routage single-doc de l'API lisait
  les templates DEPUIS LES FICHIERS → un draft promu ne re-matchait jamais via la porte. `_run_route_document`
  lit désormais **D2 `active_templates()`** (seedée des `templates/*.json` au premier usage — les fichiers
  restent le SEED anonymisé) ; `/v1/document-types` dérive de D2 → **une catégorie organique apparaît
  toute seule dans la select box**. Flux CI inchangé (hors boucle, documenté). (4) **Revue v2**
  (`ui/review.html`) : carte draft = anchors + champs + checkboxes cochées par défaut ; Valider envoie le
  subset. (5) **`draft_check.py --store`** : stage le draft accepté en D3 `pending` sur le job
  `needs_review` du cluster (match par `source` = nom de fichier ; review ouverte si absente).
  **Prouvé** : `ui_smoke.py` étendu (scénario 6) **15/15 PASS** — 2 inconnus → needs_review → le VRAI CLI
  stage → pending porte le draft + 4 candidats → cochage 3/4 → D2 `required` = les 3 cochés → la 3e
  attestation **re-matche à l'upload** (`done/auto`, template `draft_attestation_01`) → « attestation »
  dans la select ; **navigateur réel** (uvicorn :8123 + Playwright) : upload ×2 → « needs_review » → CLI
  → /review affiche le draft + 4 checkboxes → décoche `codes_obtenus` → Valider → « Aucune suggestion en
  attente » → re-upload → **« Résultat : validated »** ; état en table vérifié (D2 active
  required=3 sans codes_obtenus, job 3 done/auto, D3 validated + draft à bord). Régressions re-passées
  vertes : `review_check`, `promotion_check`, `draft_smoke` 11/11. **Comportement noté (à trancher plus
  tard)** : après promotion, les jobs D1 du cluster restent `needs_review` (clôture = re-run batch/worker
  ou acceptation humaine — le re-match ferme les SUIVANTS, pas rétroactivement les membres du cluster).
  `.gitignore` : + `.playwright-mcp/` (artefacts navigateur).
- **Lane DRAFTING, moitié déterministe (D-a + D-b) LIVRÉE + prouvée (synthétique born-digital, zéro OCR,
  zéro SLM — contrainte VRP respectée).** `ocr_bifunction/drafting.py` : **D-a `cluster_unknown_documents`**
  (cosine TF-IDF plein-doc — RÉUTILISE `TfidfRetriever` tel quel, 1 doc = 1 chunk ; single-link glouton
  déterministe, seuil défaut 0.5) + **D-b `draft_from_cluster`** = invariance cross-docs (lignes du 1er doc
  retrouvées dans TOUS les autres via le MÊME prédicat fuzzy que le match — imports assumés des privates de
  `template.py`, dérive de sémantique interdite par construction) → anchors structurels (filtre PII mécanique
  + garde anti-ancre-numérique : une date partagée n'est pas de la PII mais une ancre fragile) ; zones
  variables sous/à-droite d'un label invariant → champs candidats (noms = placeholders déterministes
  slugifiés, le SLM nommera en D-c) ; **gate de re-test généralisé** : match sur CHAQUE doc du cluster +
  extraction non-vide PARTOUT + valeurs NON constantes (une constante = structure, pas un champ) sinon champ
  droppé / draft rejeté ; validation du draft = presence par champ (candidats à COCHER, doctrine
  compute-all/config-requires). Runner CLI **`draft_check.py <docs…>`** (docs en CLI = le piège « D1 ne
  retient ni chemin ni texte » contourné) avec **gate OCR opt-in** : une image sans `--ocr` → refus fort
  exit 2 (« re-run --ocr APRÈS GO explicite ») — le frein VRP encodé mécaniquement. **Prouvé** :
  `draft_smoke.py` versionné (corpus synthétique PyMuPDF PII-free en tempdir : 3 attestations même layout +
  2 certificats + 1 courrier) → **11/11 PASS** : clustering exact (intra-layout 0.69-0.80, cross ≤0.07 →
  seuil 0.5 large), anchors = vocabulaire structurel seul (aucun nom/date dedans), 4 champs extraits à
  valeurs variantes, contrôle négatif (draft attestation ≠ doc certificat), granularité blocks vérifiée
  (label/valeur séparés de 28 pt, 11 lignes/doc) ; CLI vert sur le même corpus ; gate OCR prouvé (PNG sans
  `--ocr` → exit 2, zéro OCR lancé). **Limite v1 notée** (docstring `drafting.py`) : invariance à granularité
  TextLine — un label collé à sa valeur dans UNE ligne (« Delivree le : 12/03/2024 ») ne donne ni anchor ni
  champ ; famille prefix-pattern à ajouter SI un cluster réel l'exige. Aucun module existant touché (zéro
  risque de régression). Oracle = runs réels, pas de pytest.

## Fait (2026-07-02)
- **MIX local, étapes B+C — les 2 pages HTML (upload + revue) livrées + prouvées dans un VRAI navigateur
  (Playwright), zéro llama.** Adaptateurs jetables `ui/upload.html` + `ui/review.html` (0 dép front, servies
  par FastAPI `GET /` et `GET /review`) — peaux sur le contrat prouvé, AUCUNE logique métier côté client.
  **Upload** : select box `document_type` **dérivée du serveur** (`GET /v1/document-types` = les categories des
  templates, jamais codée en dur) + files multiples → base64 → `POST /v1/documents:validate` (contrat inchangé)
  → rendu status/verdict/reasons/missing + **poll auto du job** si `pending`. **Revue** : `GET /v1/reviews/queue`
  (rows D1 `needs_review` + état de décision D3), Accepter/Rejeter + commentaire → `POST
  /v1/reviews/{job}/decision` (**écrit D3 seulement** — la clôture D1 = le sweep du watchdog, contrat
  d'écrivains respecté à l'écran : « l'UI ne touche jamais D1.status ») ; suggestions D3 `pending` +
  **critères d'auto-validation du template affichés READ-ONLY** (Q3 v1) → Valider = `POST
  /v1/suggestions/{review}/validate` (promotion D2 + D3 validated, 409 au replay) / Rejeter. **Prouvé** :
  (1) `ui_smoke.py` versionné (TestClient + vrai process watchdog) **9/9 PASS** (pages servies, select dérivée,
  facture→validated+row, courrier→queue, accept→sweep→file vidée, suggestion+critères→promotion D2, replay 409) ;
  (2) **navigateur réel** (uvicorn :8123 + Playwright) : facture uploadée via la page → « Résultat : validated » ;
  courrier → needs_review → visible dans /review (raisons + keywords) → clic Accepter → « Décision enregistrée » →
  watchdog `--once` → reload → **« File vide »**. Le fonctionnement mix est TESTABLE localement de bout en bout :
  `uvicorn api_maquette:app` + `python worker_watchdog.py` + un navigateur.
- **MIX local, étape A — worker WATCHDOG (process séparé) remplace le worker in-process ; la table EST la
  file, durcie (prouvé réel, 5 preuves, zéro llama).** Cadrage → `docs/briefs/BRIEF-fonctionnement-mix.md`
  (gitignoré) ; décisions user : Q1 watchdog-table (avec durcissement), Q2 persist-all, Q3 critères v1
  affichage seul. ⚠️ Contrainte session : **zéro SLM (stress test VRP)** — preuves fake-engine + born-digital.
  (1) **D1 durci** (`repository.py`) : colonnes `document_ref` (pointeur spool — était déjà dans le sketch
  contrat) + `attempts` (+ migration auto des .sqlite existants) ; **`claim_next`** = claim ATOMIQUE portable
  (SELECT candidat → `UPDATE … WHERE status='received'`, rowcount 0 = pris — MariaDB-compatible) ;
  **`recover_stale`** = lease timeout (crash mi-job → `processing` périmé re-devient `received` ; cap
  `attempts` → `failed` = anti poison-pill). (2) **API = pure porte** (`api_maquette.py`) : worker
  thread/queue.Queue SUPPRIMÉS (perdaient les jobs au restart) ; le douteux est **spoolé sur disque**
  (`spool/<sub>/` gitignoré, purgé au terminal — les bytes traversent les process par le disque, pas la
  mémoire) + row `received` ; **persist-all** : CHAQUE issue laisse une row D1 (`done`/auto avec record,
  `needs_review` sync, `received`) → `job_id` sur TOUTES les réponses. (3) **`worker_watchdog.py`** : recover
  → drain (1 job à la fois, 8 Go) → **sweep décisions D3** (accept→`done`, reject→`failed` — l'UI écrit D3,
  le WORKER écrit D1, idempotent par construction) ; `--once` (parité cron IT + smokes), `--fake-escalation`
  (seam smoke sans VLM), **PID-lockfile** (2e instance refuse). D3 gagne `decided()`. **Prouvé** : micro-smoke
  claim/lease/poison-pill/migration ; cycle réel door→**watchdog process séparé**→done (`api_smoke_async.py`
  réécrit : lance le VRAI process, assert « claimed job » dans sa sortie) ; PID-lock refuse (exit 2) ; sweep
  vérifié en table ; régression `api_smoke_real --expect validated` verte + row `done/auto` 7 champs.
  **Reste (étapes B/C du brief)** : page upload `GET /` (select box type), page revue `GET /review`
  (accept/reject → D3, valider suggestion → promotion D2, critères read-only). Étape D (SLM sur les nouvelles
  attestations/habilitations d'`inputs/`) = machine libre.
- **Lane SLM câblée EN LIVE dans le flux batch — la suggestion ne vit plus dans son runner (prouvé réel,
  3 legs).** Le hook se pose **DANS `route_document`** (seul endroit où text/lines sont en scope → zéro
  double-read/OCR) : param **`suggester`** opt-in (défaut `None` = le fast-path API ne réveille JAMAIS le SLM —
  même patron qu'`escalation_engine`), tiré **uniquement** sur no-match avec texte lisible. Type
  `SuggesterHook = (text, lines, category) -> SuggestionOutcome` (`router.py`). L'orchestrateur **reste
  storage-agnostic** : l'outcome voyage sur `DocumentRecord.suggestion` + une raison lisible dans `reasons`
  (`_suggestion_reason`) ; c'est le **sink** (`batch_check._persist`) qui stage les suggestions **vérifiées** en
  D3 (`open_review` + suggestion `pending`) à côté du persist D1 — même frontière que le bridge D1. Cohérence
  liste-fermée : `suggest_template` a gagné le param **`templates`** (miroir du seam D2 du router, trou noté à
  l'étape 2 maintenant exercé) et `batch_check` charge les templates **UNE fois** pour le match ET le SLM
  (`--suggest`). **Prouvé live (llama-swap, granite, runs réels)** : (1) HP `Image.jpeg` scopé `preuve_test` →
  no-match → SLM → gates 1+2 OK → **D1 job `needs_review` + D3 review `pending` stagés PAR LE FLUX** ; (2)
  facture 14a → match déterministe → **SLM endormi** (AUTO) ; (3) courrier → SLM non-vérifié → review avec
  raison explicative, **PAS de ligne D3** (état final vérifié en table : 3 jobs, 1 seule review pending).
  Régression sans `--suggest` : iso. **Note croissance réelle observée** : pour Image.jpeg le bon geste de
  promotion est une **variante** `grow_template_from_base` (le template de base existe ; c'est sa SIGNATURE de
  match qui rate cette photo) — le mécanisme existe déjà (`promotion.py`). Machine rendue llama-free (0 orphelin).
- **CONSOLIDATION end-to-end — la chaîne complète D1→D3→D2→re-match en UNE démo (prouvée réel) ; 2 trous
  d'intégration révélés ET soudés.** Runner `consolidation_check.py` (déterministe, sans llama, un seul store) :
  PHASE A intake = **le vrai `process_batch`** avec **templates lus DEPUIS D2** (seedée moins `facture_entrante_03`)
  → 14a **miss** (rag/review), 20a **hit auto via D2** (`facture_entrante_01` — le read-path D2 prouvé sur un hit
  aussi), courrier → rag/review ; tout persisté D1. PHASE B file `needs_review` → reviews D3. PHASE C curation →
  suggestion `pending` **sur la review existante**. PHASE D promote → D2 active, file vide. PHASE E re-match
  depuis D2 → `structured/auto` (`facture_entrante_03`) → **le worker ferme le job D1** (`done/auto`, record
  réécrit — la boucle se voit dans la table jobs elle-même). **Les 2 trous soudés** (la raison d'être de la
  consolidation) : (1) le contrat de colonnes dit « le worker LIT D2 » mais `route_document`/`process_batch`
  lisaient les fichiers → **param `templates` optionnel** (rétrocompatible, `None` = fichiers), exercé par le
  run ; le flux CI garde le répertoire (ses templates hors boucle de suggestion, documenté) ; (2) D3 ne savait
  pas stager une suggestion sur une review EXISTANTE (la lane la posait à la création ; le vrai flux = review à
  l'intake, candidat PLUS TARD) → **`stage_suggestion(review_id, suggestion)`** ajouté au contrat
  `ReviewRepository`. Régressions : `review_check` + `promotion_check` re-passés VERTS. Oracle = runs réels.
- **Étape 3 — D2 ÉMERGE (`ocr_templates`) + seam de promotion D3→D2 ; boucle de croissance organique prouvée.**
  Dernier des 3 domaines : **D2 rendu store** (`ocr_bifunction/template_repository.py` : `TemplateRepository`
  ABC + `SqliteTemplateRepository`, table `ocr_templates` — `template_id` PK, `category`, match/fields/validation
  en **colonnes JSON**, `active`, `version`, timestamps). Les critères **voyagent avec** le template (bloc
  `validation`, pas de table séparée). `seed_from_directory` importe les `templates/*.json` **anonymisés** (le
  SEED) ; `active_templates(category)` rend des dicts **shape identique aux JSON** → `match_template`/
  `extract_fields` (`template.py`) le consomment **INCHANGÉS** (on back le read path, on ne le touche pas). D2
  émerge MAINTENANT car la promotion en a besoin d'écrire (avant : fichiers OK en lecture, YAGNI). **Seam de
  promotion** (`ocr_bifunction/promotion.py`, l'écrivain « Promotion » du contrat) : `promote_suggestion` upsert
  un template **actif** en D2 + flip `suggestion_status`→`validated` (transaction unique côté MariaDB ; en proxy,
  2 stores séparés en séquence) ; `grow_template_from_base` (pur) mint une variante réutilisant fields/validation
  d'une base quand le SLM pointe un id connu mais que `match_template` a raté le layout. **Prouvé déterministe
  (sans llama, PII-safe, `promotion_check.py`)** : D2 seedée moins `facture_entrante_03` → doc **miss**
  (`match=None`) → D1 `needs_review` + D3 review `pending` (contenu curé = le JSON committé anonymisé) → **promote**
  → D2 actif + D3 `validated` → doc **matche** `facture_entrante_03` → extract+validate → **auto**. Chaîne
  D1→D3→D2 dans **un seul store**, aucun fichier PII écrit (D2 = `.sqlite` gitignoré), aucune valeur imprimée.
  Oracle = run réel. **Bilan : le sketch `docs/contrat-bd-destination.md` (3 domaines) est intégralement proxifié.**
- **Étape 2 (D3) — lane SLM de suggestion LIVRÉE + prouvée end-to-end (GBNF actif, deterministic-first).**
  Suite du store D3 (ci-dessous), la lane qui écrit les suggestions `pending`. **D'abord le harnais diagnostic
  GBNF** (`gbnf_diag.py`, brief délivrable 1) : test « banane » (grammaire `root ::= "BANANE"` + prompt qui
  réclame du code) → **GBNF ACTIVE sur chat ET /completion** (contrôle sans grammaire = granite écrit du
  FastAPI ; avec = `BANANE`). Le filet mécanique tient ; la docilité ne joue plus que sur le *fond*. **Puis la
  lane** (`ocr_bifunction/suggestion.py` + runner `suggestion_check.py`, brief délivrable 2), **deterministic-
  first** : `match_template` gratuit d'abord (majorité → SLM PAS réveillé) ; sur un miss → SLM propose un
  `template_id` de la **liste fermée dérivée des `templates/*.json`** (enum via `json_schema`→GBNF, `/completion`
  granite) + les anchors vus → **2 gates déterministes** : (1) anti-hallucination = les anchors proposés sont
  réellement dans l'OCR ; (2) **fit** = TENTER le template (`extract_fields`+`validate_fields`). `verified` SEULEMENT
  si id connu ∧ anchor confirmé ∧ validation OK, sinon → humain. Le SLM **propose**, le déterministe **dispose** ;
  le SLM ne crée jamais un template (curation = humain). **Prouvé (runs réels, llama-swap)** : (case 1) facture →
  match déterministe, SLM endormi ; (case 2) facture `--force-slm` → SLM propose une facture, 1 anchor halluciné
  **rejeté par gate 1**, mais sous-template faux → **gate 2 FAIL** (`total_ht` non lu) → humain ; (case 3) courrier
  (mise-en-demeure, non-structuré) → gate 2 FAIL → humain (pas faussement classé facture) ; (case 4) HP image,
  catégorie `preuve_test` (template unique) `--force-slm` → `hp_preuve_test_01`, **gate 2 PASS** → `verified` →
  **suggestion `pending` stagée en D3** (boucle lane→store fermée). **Découverte smoke-first** : ma 1re barre
  « ≥1 anchor confirmé » laissait passer le courrier (un anchor copié verbatim se re-vérifie trivialement) → **corrigé
  en ajoutant le gate 2 fit** (le brief l'exigeait : « anchors confirmés → TENTER extract+validate »). Machine
  partagée rendue propre (llama-swap arrêté, 0 orphelin). Oracle = runs réels, pas de pytest.
- **Étape 2 (D3) — store `ocr_reviews` bâti + boucle de croissance organique prouvée (stub, sans SLM).**
  Domaine 3 (revue/curation) rendu réel, **séparé de D1** (autre propriétaire : l'UI de revue écrit D3, le
  worker écrit D1 ; D3 **référence** le job par `job_id`, **ne duplique pas** le record — source unique en D1).
  `ocr_bifunction/review_repository.py` : **`ReviewRepository` ABC** (seam DI → l'IT swappe un
  `MariaDbReviewRepository`) + **`SqliteReviewRepository`**, table **`ocr_reviews`** (review_id PK, job_id FK,
  `projection` = **vue** pour l'humain PAS 2e vérité, `comment`/`decision` accept|reject, suggestion =
  `suggested_template_id`/`category`/`anchors`/`suggestion_status`). **La comm = la colonne `suggestion_status`**
  (comme `status` en D1) : suggestion en attente = `pending`, l'humain flippe → `validated` (→ promotion D2,
  étape 3) | `rejected`. **1 écrivain par phase** (reviewer possède la décision ; promotion possède l'écriture
  D2). Runner `review_check.py` (jobs D1 synthétiques PII-free, **un seul store** — `ocr_jobs`=D1 +
  `ocr_reviews`=D3). **Prouvé** : 2 jobs `needs_review` → 2 revues (structured avec suggestion `pending`, rag
  sans) → file `pending_suggestions()`=1 → `validated` → 0 (loop fermée) + décisions accept/reject
  enregistrées. Oracle = run réel, pas de pytest. **Suite** : harnais diagnostic GBNF, puis lane SLM qui écrit
  les suggestions `pending` (deterministic-first, GBNF liste-fermée, re-vérif anchors), puis seam promotion D3→D2.
- **Étape 1 du plan acté — API migrée sur `repository` : D1 UNIFIÉ, un seul store, 2 régimes (prouvé réel).**
  Le `_jobs` dict en mémoire de `api_maquette.py` est **remplacé par le `SqliteRepository`** (même table
  `ocr_jobs` que le batch). Le lifecycle escalade (received→processing→done|**needs_review**|failed) vit
  désormais en D1. **3 frictions de contrat résolues** en exerçant les colonnes depuis 2 producteurs : (a)
  `job_id` **str→int** (autoincrement D1 ; path `GET /v1/jobs/{id}` typé `int`) ; (b) terminal douteux
  **unifié sur `needs_review`** (l'API faisait `done`+human) — le client mappe tout terminal→`done`, verdict
  dit auto/human ; (c) **`verso_read_path` n'a PAS de colonne D1** (le batch ne le porte pas) → **plié dans
  `reasons`** (`"verso read via: raw"`), champ retiré de `JobResponse`. **Thread-safety** : `SqliteRepository`
  gagne un kwarg `check_same_thread=False` (défaut inchangé pour le batch mono-thread) + l'API sérialise TOUT
  accès sous `_repository_lock` (1 écrivain par phase ; le VLM ~171 s tourne HORS lock). `request_id` porté sur
  la ligne (colonne exercée). Store path env-overridable `OCR_STORE_PATH` (défaut `ocr_store.sqlite`, gitignoré).
  **Prouvé (oracle = runs réels, smokes versionnés)** : (1) fast auto `recto_verso.pdf`→`validated`, **0 write
  D1** (le fast-path ne persiste pas) ; (2) `api_smoke_async` recto A+verso B→202 `job_id=1`→worker→`done`,
  ligne D1 re-lue (status `needs_review`, exec `escalation`, verdict human, `record_fields` réécrit,
  `created_at`≠`updated_at`) ; (3) `api_smoke_real --expect pending` vert ; (4) **unification** : `batch_check
  --store <même fichier>` → `ocr_jobs` porte 3 lignes de 2 producteurs (API `ci/escalation`, batch
  `structured/fast` + `rag/fast`), file ⑤ = 1 requête SQL cross-régime. Limite notée : `--expect-escalation`
  de `api_smoke_async` **inatteignable via le contrat submission** (un verso à MRZ VLM-only est déclaré
  `incomplete` en fast-path AVANT enfilement — propriété du flux, pas de la migration) ; `template_id` pas
  réécrit au terminal (`update_status` ne le couvre pas — sans régression, l'API ne le suivait pas). Oracle
  = run réel, pas de pytest.
- **#2 sink ④⑤ — D1 proxy (store jobs+records) bâti + comm async prouvée.** Décision utilisateur : **table, PAS
  JSON** (« JSON = temporaire ; table = organisation du travail ; le JSON vit en COLONNE »). `ocr_bifunction/
  repository.py` : **`Repository` ABC** (seam DI → l'IT swappe un `MariaDbRepository`, doctrine fabrique) +
  **`SqliteRepository`** (proxy jetable), table **`ocr_jobs`** = record consolidé (**source unique**) + `status`
  (`received`/`processing`/`needs_review`/`done`/`failed`) + `execution_lane` (`fast`/`escalation`) + `verdict`
  + `record_fields`/`reasons` en **colonnes JSON** ; timestamps **explicites** (MariaDB 5.5 sans DEFAULT
  CURRENT_TIMESTAMP). **La comm inter-tables = la colonne `status`** (pas de bus) : « record en attente d'async »
  = `pending('received','escalation')`, le worker dépile → `processing` → `done` + record réécrit. La
  **suggestion-template** suivra le MÊME loop (autre type ; D3 réf D1 par `job_id`). Garde-fou course = **1 seul
  écrivain par phase** (worker écrit `D1.status` ; l'UI lit seulement). Batch câblé (`batch_check.py --store` ;
  bridge `DocumentRecord→Job` **dans le runner** → `orchestrator`/`repository` restent indépendants). **Prouvé** :
  batch 3 docs → 2 auto/`done` + 1 rag/`needs_review` **re-lu depuis la table** ; démo async received→done, file
  vidée + record récupérable. Le `.sqlite` (PII) **gitignoré**. Contrat = les COLONNES, à co-geler IT (sketch
  `docs/contrat-bd-destination.md` màj). Oracle = run réel.
- **Backbone BATCH monté + prouvé (end-to-end, colonne vertébrale du régime batch).** `ocr_bifunction/
  orchestrator.py` : `process_batch(items, …) -> BatchResult`. Chaque `BatchItem` (1 doc, ou une submission CI
  multi-fichiers via `document_type=carte_identite`) est dispatché sur le **même cœur que l'API** —
  `process_ci_submission` (CI) ou `route_document` (2-lane) — puis mappé en `DocumentRecord` uniforme (source,
  lane, `outcome` ∈ {auto, review}, detail, fields, reasons, summary). `BatchResult` expose le **split ④/⑤**
  (`.auto` = centralise-ready, `.review` = file de revue humaine). Escalade LightOCR câblée pour le verso CI
  seulement (opt-in `--escalate`). **Persistance VOLONTAIREMENT hors-scope** : `process_batch` RETOURNE les
  records, le SINK (SQLite/JSON/MariaDB) se branche sur `BatchResult` — c'est le contrat ④/⑤ à co-geler avec
  l'IT (#2, non figé). Runner `batch_check.py` (lazy RapidOCR ; `--ci` groupe une submission ; `--escalate`).
  **Prouvé** (mix réel, sans llama) : `14a FACTURE…`→STRUCTURED/**auto** (facture_entrante_03, total_ht
  5909.74) ; courrier mise-en-demeure→RAG/review ; screenshot log-API→RAG/review (RapidOCR a tiré, aucun
  template) → **AUTO 1 / REVIEW 2**. Zéro edit d'appelant (réutilise les cœurs). Oracle = run réel.
- **Convergence llama-swap TERMINÉE — embedding + LightOCR passés clients (prouvés). Sujet llama SOLDÉ.**
  Suite du générateur (`187ddaf`), les 2 derniers slots SLM convergent sur le llama-swap partagé, chacun
  prouvé sur du réel. (1) **`GgufEmbeddingRetriever`** (`rag.py`) ne spawn plus de `llama-server --embedding`
  → **client `/v1/embeddings`** (clé `granite-embedding-r2` ; env `LLAMA_SWAP_URL` / `RAG_EMBEDDING_MODEL_KEY`) ;
  `close()` no-op ; batching token-budget conservé. **Prouvé** : 3 chunks + requête « comment mettre fin au
  contrat » → top-1 = clause de résiliation (0.884), dim 768, 7,3 s (lazy-load inclus). (2) **`LightOnOcrEngine`**
  (`lightonocr_engine.py`) ne shelle plus `llama-mtmd-cli` → **client multimodal HTTP** (`/v1/chat/completions`,
  image en base64 data-URL, clé `lightonocr-2-1b`). **Doute levé : le chemin multimodal serveur b9542 marche** —
  verso CI 2021 → llama-swap charge modèle + mmproj → **3 lignes MRZ TD1 récupérées** + champs carte. Les 3 slots
  gardent des constructeurs **sans-args** → **zéro edit d'appelant** (`api_maquette`, `ci_submission_check`,
  `contrat_check`, `rag_check`, `contrat_graph_check`). **Latence LightOCR ~482 s** (max_tokens 2048 → 436 lignes
  dont micro-texte décoratif) : batch/escalade OK, `max_tokens` = bouton de réglage vitesse. Machine rendue
  llama-free (TaskStop, 0 orphelin). Oracle = runs réels.

## Fait (2026-07-01)
- **PROD-PREP : générateur → client llama-swap + projet self-contained (prouvé).** Convergence infra pour la
  mise en prod. (1) `LlamaCppGenerator` (spawn+close d'un llama-server) **remplacé** par `LlamaSwapGenerator` =
  **client HTTP pur** du llama-swap partagé (`LLAMA_SWAP_URL` défaut `127.0.0.1:8080`, clé
  `granite-4.0-h-tiny-Q4_K_M`) : **zéro process**, `close()` no-op, TTL décharge → plus de spawn/kill à oublier
  (vraie douleur = l'oubli, pas la RAM ; l'utilisateur sérialise déjà ; cf. mémoire
  `shared-machine-3-slm-projects`). (2) **Self-containment** : binaire llama.cpp b9542 (50 M) copié dans
  `tools/llamacpp/`, granite-4.0 (4 G) copié dans `models/` (embedding + LightOnOCR + mmproj y étaient déjà) ;
  `tools/llama-swap/config.yaml` **réécrit en chemins RELATIFS** (3 clés : `granite-4.0-h-tiny-Q4_K_M` gén.,
  `granite-embedding-r2` RAG, `lightonocr-2-1b` OCR), lancé depuis la racine
  (`tools/llama-swap/llama-swap.exe --config tools/llama-swap/config.yaml --listen 127.0.0.1:8080`). **Frontière
  git** : binaires (`models/`, `tools/**/*.exe`, `tools/llamacpp/`) **gitignorés** (repo public, multi-Go) ;
  `config.yaml` **tracké** = contrat déployable (0 PII, 0 chemin perso). **Prouvé** : appel Article 2 via
  `LlamaSwapGenerator` → llama-swap lazy-load granite → **2 `REMPLACE` verbatim identiques au direct-server**
  (101 s dont ~100 s load). Runner : `--threads/--binary/--model` retirés, `--llama-swap-url/--model-key` ajoutés.
  **Reste** : convergence embedding + LightOCR (mêmes clés déjà dans le yaml ; LightOCR HTTP non prouvé).
- **RAG contrat — Étape 2 DÉ-BRUITÉE (filtre structurel `is_document_reference`) — prouvé sur run réel.**
  Le modèle sur-extrayait sur la prose (arêtes dont `ancien` = un délai, une date, une valeur, un statut
  produit, « les Parties »…). Fix = garde-fou **déterministe** dans `build_reference_graph` : garder une arête
  seulement si son `ancien` **nomme un élément documentaire** (mène par `Article|Annexe|Avenant|Contrat` après
  déterminant optionnel, **ancré au HEAD** pour éviter le faux-positif substring « …du Contrat Cadre »). PAS
  une ré-extraction rule-based fragile (la définition de « référence » est stable) ; **prompt validé intact**.
  **Prouvé** (même corpus, run réel) : **305 → 58 arêtes gardées** (247 jetées, **−81 %**), pendants **536 →
  80** ; **oracle intact** (Art.2 → 2 `REMPLACE` verbatim ; `Article 1 —MODIFIE→ Article 2` résolu ; Art.XVII
  passe de 6 arêtes bruitées à 2 vraies `ABROGE/REMPLACE` de l'avenant du *précédent* contrat n°DA21-M-290).
  Rejets vérifiés = vraie prose. **Limite acceptée (utilisateur)** : « Bon de commande » hors liste d'éléments
  → jeté (pas pertinent dans ce cadre ; bouton de réglage si besoin). Runner rapporte gardé/jeté + échantillon.
  **GBNF** reste l'escalade si le filtre structurel ne suffit plus un jour. Oracle = run réel, pas de pytest.
- **RAG contrat — Étape 2 (graphe de références) LIVRÉE + PROUVÉE (run réel vert, 3 contrats).** Build 1→2→3
  du brief : slot `Generator` jetable `ocr_bifunction/generation.py` (`LlamaCppGenerator`, **patron
  `GgufEmbeddingRetriever` = llama-server DIRECT**, pas llama-swap → `close()` fiable sans orphelin ; prompt
  validé copié verbatim + `parse_references` **tolérant** : salvage d'un array tronqué, garde-fou longueur,
  fail-loud si aucun array) ; `ocr_bifunction/reference_graph.py` (`build_reference_graph` = 1 appel LLM /
  article → arêtes résolues vers nœuds OU **pendantes** = signal de complétude ; `outgoing` = traversée
  1-hop) ; runner `contrat_graph_check.py` (read→segment→graphe→retrieve tfidf→traversée). **Prouvé** (oracle
  « que modifie l'avenant 7 », 47 articles, **305 arêtes**) : top-1 = `Avenant Article 2` → 2 `REMPLACE`
  verbatim + provenance p.2 (Annexe 4/5 du Contrat → Annexe n°1/2 de l'Avenant n°7, **DANGLING** car les
  annexes ne sont pas encore des nœuds) ; **résolution intra-doc prouvée** `Article 1 —MODIFIE→ Article 2`.
  **Pièges perf soldés** (leçon : le timeout CPU = **taille de prompt, PAS threads**) : `segment_articles`
  mesure des tokens-CONTENU qui sous-comptent les tokens-MODÈLE (Article V table = 13 734 chars / 1201
  content-tok) → **cap 6000 chars** + `max_tokens` **800** + timeout **420** + **`-t 4`** (= cœurs physiques,
  4/8 logiques ; cf. mémoire `llama-cpp-threads-physical-cores`, profils user jour=3/nuit=4). **Dette QUALITÉ
  ouverte** : sur-extraction (305 arêtes / **536 pendants**, beaucoup de spurious sur la prose ; garde-fou
  160-chars insuffisant) → **GBNF / prompt plus strict** (escalade prévue au brief). Oracle = run réel, pas de
  pytest. Serveur fermé sans orphelin.
- **RAG contrat — Étape 2 (graphe de références) : CONCEPT LLM PROUVÉ (build livré, cf. bullet ci-dessus).** Décision
  utilisateur : extraction d'arêtes **au LLM**, PAS rule-based (« le rule-based est une maintenance
  constante », vécu classification pièces SAV). **granite-4.0-h-tiny** (via **llama-swap** —
  `tools/llama-swap/` copié + gitignoré, port 8080, clé `granite-4.0-h-tiny-Q4_K_M`, TTL 300) extrait sur
  du réel (Avenant Article 2) les **2 arêtes `REMPLACE`** avec **direction juste + valeurs verbatim**
  (`ancien=Contrat Annexe 4/5` → `nouveau=Avenant Annexe 1/2`). **Leçon** : un schéma `cible/par` est
  **instable** (label ET direction sautaient entre runs : RENVOIE↔REMPLACE, direction inversée) →
  **stabilisé** par champs **directionnels** `ancien`/`nouveau` + enum **exact** + **one-shot**. Prompt
  validé + plan build 1→2→3 (slot `Generator` → extraction→graphe → traversée 1-hop) →
  [brief](docs/briefs/BRIEF-rag-ingestion-strategy.md) section « Étape 2 ». **Smoke throwaway, rien commité
  côté code.** Serveurs fermés (0 orphelin). ⚠️ Ne PAS lancer granite (4.2 Go) si d'autres tâches lourdes
  tournent (ex. VRP en série) — demander/attendre.
- **RAG contrat — Étape 1a+1b livrée + prouvée sur 3 vrais contrats cross-référencés (born-digital,
  RAG PUR zéro OCR).** Besoin cadré (utilisateur) : un **STORE de contrats** (3000+ partenaires FR,
  groupe Europe → millions de chunks) **très fréquemment consulté**, **batch nuit OK**, et **toujours
  lier la lecture au doc source**. Décision DB : **pas de vector DB selon la PORTÉE de requête** —
  par-partenaire (cas réaliste) = working-set petit → **force brute**, partitionné `partner_id` →
  MariaDB suffit même ancien ; global cross-partenaires = **ANN** (MariaDB 11.8+ HNSW natif / vector DB
  dédié). Store = millions de lignes persistées → **décision destination/IT à co-geler**, pas un build
  POC. Code (`rag.py`) : `Chunk` porte `ProvenanceSpan` (page+bbox = lien au source) + `heading` ;
  `chunk_textlines` (packing avec provenance depuis `reader` TextLines) ; `segment_articles` (découpe
  par article **romain ET arabe**, **TOC dédupliqué** par corps-max, **éclatement de blocs** pour les
  titres enfouis, sous-chunk <512 pour l'embedder, **fallback plat** si pas d'articles). Runner
  `contrat_check.py` : indexe N contrats en **UN corpus**, retrouve **verbatim + provenance** (tfidf
  défaut / `--engine embedding`). **Prouvé** (`Contrat MPE 111p` ENGIE romain + `AV7 39p` Solutions30
  arabe + `Annexe FREE 12p`) : `« que modifie l'avenant 7 »` → **top-1 = `Article 2 Modifications
  introduites par l'Avenant`** (verbatim : remplace Annexe 4/5 du Contrat par Annexe 1/2 de l'Avenant)
  + provenance p.2 ; en **TF-IDF pur, zéro LLM**. **Limite observée** : le retrieval plat *trouve* la
  clause mais ne *résout* pas l'arête (`Avenant Art.2 —REMPLACE→ Contrat Annexe 4/5`) → motive
  **Étape 2 (graphe de références)**, désormais spécifiée par la donnée réelle. Oracle = runs réels.
  Commits `eac73a1` (1a) + ci-dessous (1b). Brief (gitignoré) : `docs/briefs/BRIEF-rag-ingestion-strategy.md`.
- **RAG contrat — moteur embedding rendu robuste au texte légal dense + A/B (verdict = utilisateur,
  NON tranché).** Le `GgufEmbeddingRetriever` (jadis prouvé sur un article aéré) **crashait sur le
  contrat** : chunks denses = 520-700 tokens MODÈLE (mesuré : 1109 chars = 633 tokens, ratio
  ~1.75 ch/tok ≪ l'estimation `content_tokens` ~4×) > fenêtre native 512 de granite-embedding → le
  serveur **refuse** (500 « physical batch too small » → 400 « input too large » ; il ne tronque PAS un
  embedding). Fixes (`rag.py`) : char-cap/input **1600→800** (≤512 tok même dense) ; `-b 2048 -ub 2048`
  (un batch de plusieurs inputs ≤512 tient le physical batch) ; `_embed_many` (batching — un corpus
  entier en 1 requête overrun) ; capture du corps d'erreur HTTP. **A/B `« que modifie l'avenant 7 »`**
  (oracle = run réel) : **TF-IDF** → top-1 = `Article 2 Modifications` (liste des remplacements =
  précision sur la clause exacte) ; **embedding** → top-3 = **les 3 articles de l'avenant** (1 Objet, 3
  Divers, 2 Modifications, 0.88-0.93 = rappel du cluster, dont « le reste inchangé » raté par le lexical)
  MAIS classe le pointeur (Art.1) AVANT le contenu (Art.2). KPIs en tension (précision-clause vs
  rappel-section vs coût/chauffe vs robustesse) → **pas d'auto-verdict**. Granularité forcée + fine
  (336 vs 86 chunks). Dette : chunking **tokenizer-aware** = cure propre (vs troncature au char-cap).
  Serveur fermé proprement (pas d'orphelin).
- **CORRECTION — granite-embedding-r2 = 32K tokens de contexte, PAS 512 (erreur propagée, soldée).**
  Le « 512 natif » (code + note ci-dessus) était **faux** : vérifié — IBM ([blog HF](https://huggingface.co/blog/ibm-granite/granite-embedding-multilingual-r2),
  [arXiv 2508.21085](https://arxiv.org/pdf/2508.21085)) → **32768** (RoPE θ=160k), multilingue 200+ langues,
  768-dim, Apache 2.0. On l'avait bridé nous-mêmes via `-c 512`. Fix (`rag.py`) :
  `_EMBEDDING_CONTEXT_SIZE` 512→**8192** ; `-b/-ub` **alignés au contexte** (une séquence d'embedding
  doit tenir en UN physical batch) ; char-cap 800→14000 (simple garde-fou). **Prouvé** : inputs 16000
  chars OK (dim 768, `n_ctx=8192` au log) ; smoke **article-level** (`--target-tokens 1200`) → **47
  chunks** (vs 336) = ~1 article = 1 vecteur, **zéro troncature**, top-3 embedding inchangé. → la
  troncature char-cap + les micro-chunks de la note précédente sont **OBSOLÈTES** (faux problème).
  Dette segmentation : le dernier `Article N` **absorbe les `ANNEXE` suivantes** (avenant Art.3 s'étend
  p.2-9) — les annexes intra-doc ne sont pas segmentées à part. **Modèle embedding = à garder** (pas de
  swap : 32K + FR + Apache = idéal). llama-swap + son yaml copiés (gitignorés) dans `tools/llama-swap/`.
- **API : dispatch par `document_type` vers le flux du type déclaré — prouvé sur réel.** Le champ optionnel
  n'est plus un simple scope de matching : c'est la **clé de routage** (« ce doc est censé être un X » → l'API
  lance le bon flux, au lieu de toujours supposer une CI). `validate_document` dispatche : `carte_identite`
  → `process_ci_submission` (flux paire, 4 issues) ; **tout autre type OU absent** → `_handle_single_document`
  → `route_document` (un seul doc, structuré-ou-RAG, **sans escalade VLM**). Nouveau statut **`needs_review`**
  (douteux non-CI synchrone : structuré-human, ou non-structuré RAG → revue humaine). `route_document` a gagné
  un paramètre `category` (scope au type déclaré ; un doc déclaré `facture` mais sans match facture → repli RAG).
  **Prouvé** (`api_smoke_real.py`, +choix `needs_review`) : facture→validated/auto ; HP `preuve_test`→validated ;
  courrier déclaré `facture`→needs_review (repli RAG + keywords) ; CI combined→validated (regression) ; docx
  sans hint→needs_review non-structuré. **Limite « recto scoping » RÉSOLUE** par le dispatch (une facture part
  au flux facture, plus jamais prise pour un « recto » CI).
- **API maquette migrée au contrat de SUBMISSION (liste de fichiers + 4 issues) — prouvée sur réel.**
  Décision UI (tranchée par l'utilisateur) : requête = **`files: [{filename, content_base64}]`** (au lieu des
  2 champs recto/verso) → gère 1 fichier / 2 photos / PDF combiné / côté manquant uniformément. Réponse =
  enveloppe étendue `status ∈ {validated, pending, incomplete, unrecognized}` + **`missing: ['recto'|'verso']`**.
  `validate_document` appelle `process_ci_submission` (fast-path) : complete+auto→`200 validated` ; complete+
  human→**`202 pending`** (escalade async, le worker re-run `process_ci_submission` AVEC le VLM) ; incomplete→
  `200` + `missing` (l'UI redemande le côté) ; unrecognized→`200`. Infra worker/queue/seam **inchangée**.
  **Prouvé** (`api_smoke_real.py` màj, files-list) : combined PDF→validated ; recto seul→incomplete missing
  verso ; image non-CI→unrecognized ; recto A+verso B→pending ; **cycle async** (`api_smoke_async.py`)
  pending→done. **Limite connue** : recto détecté via tous templates si `category=None` (une facture pourrait
  passer pour « recto ») → l'UI CI passe `document_type=carte_identite` (cf. NEXT c).
- **Complétude de submission CI — « tout reçu » vs « il manque un côté » (pilote l'API/UI upload).**
  `process_ci_submission(source_paths, …) -> CiSubmissionResult` dans `pipeline.py` : une submission = N
  fichiers (images et/ou PDF combiné recto+verso) → `extract_card_images` aplatit (PDF → images embarquées ;
  image → elle-même) → on cherche le **recto** (1er côté qui matche un template CI) **puis** le **verso** (MRZ).
  **Garde-fou** : le recto est trouvé d'abord pour que le **VLM ne tourne JAMAIS sur le recto** ; le verso est
  cherché dans les images NON-recto (escalade permise là), avec repli sur l'image recto pour une **photo
  combinée** (lecture cheap, sans VLM). 3 issues : `complete` (recto+verso → record réconcilié + verdict),
  `incomplete` (1 seul côté → `missing=['recto'|'verso']` → l'UI redemande), `unrecognized` (ni recto ni MRZ).
  Refactor : `_reconciled_record` extrait de `process_ci_pair` (iso-sortie **confirmée** : concordant →
  validated/auto). Runner `ci_submission_check.py` (remplace `reconcile_pdf_check.py`). **Prouvé sur réel** :
  `recto_verso.pdf` → COMPLETE/AUTO (AIT-ALLA, 3/3 clés) ; recto seul → INCOMPLETE missing verso ; verso seul
  (IMG_8392) → INCOMPLETE missing recto. **À FAIRE ensuite** : exposer ces 3 issues dans le **contrat JSON de
  l'API** (forme requête liste-de-fichiers + statut `incomplete`/`missing` = décision UI, à confirmer).
- **Dette reconcile (a) accents SOLDÉE + adaptateur PDF combiné recto+verso.** (1) `reconcile._normalize`
  **plie** désormais les accents (`unicodedata` NFD + drop combining) au lieu de les **jeter** : `GAÊLLE`
  devenait `GALLE` (Ê supprimé) ≠ MRZ `GAELLE` ; maintenant `GAÊLLE`→`GAELLE` = MRZ (la MRZ est accent-free
  par translittération ICAO, donc plier rend le recto comparable). **Crucial** : le fix n'introduit **AUCUNE
  tolérance floue** — un vrai écart d'1 char (`Gaëlle`≠`GAELE`, le type de slip VLM) reste un **MISMATCH** →
  la détection « recto A + verso B » est intacte. Oracle : smoke synthétique `reconcile_normalize_smoke.py`
  (7 cas, positifs accents + négatifs réels) + run réel ci-dessous. (2) **Adaptateur PDF combiné**
  `reconcile_pdf_check.py` : un PDF 1 page / 2 images (recto+verso scannés ensemble, image-only) → extrait
  les 2 images (`pymupdf.extract_image`) → **auto-détecte** recto vs verso (le recto est le côté qui matche
  un template CI ; les 2 ordres sont essayés) → `process_ci_pair`. **Prouvé** sur `inputs/recto_verso.pdf`
  (gitignoré) : carte AIT-ALLA, recto=image_0, MRZ lue en **raw** (pas de VLM), 3/3 clés concordent →
  **AUTO**. Adaptateur jetable (runner ; à promouvoir dans `pipeline` si l'usage se confirme). **Dette (b)
  tolérance floue nom = décision sécurité, EN ATTENTE de l'utilisateur** (cf. Suivis ouverts).
- **Routeur 2-lanes câblé — un seul point d'entrée structuré-vs-RAG, prouvé sur mix réel.**
  `ocr_bifunction/router.py` : `route_document(path, templates_dir, engine)` → `RoutedDocument`. UNE
  question — le doc matche-t-il **un** template structuré (toutes catégories) ? → STRUCTURÉ (extract +
  `validate_fields` → auto/human) ; sinon → RAG (résumé extractif + nb de chunks indexables). Unifie ce que
  `hp_check`/`facture_check`/`rag_check` faisaient séparément. **2 garde-fous d'honnêteté** : (1) un template
  **sans bloc `validation`** ne peut PAS être auto-validé single-doc → verdict **human** (« flux paire
  `process_ci_pair` ») — un **CI recto seul n'est jamais faussement AUTO** ; (2) les courriers mise-en-demeure,
  jadis « intrus facture » (runner scopé), deviennent correctement **lane RAG** (prose à résumer, pas un
  déchet). Moteur RapidOCR **lazy** (born-digital ne charge pas l'ONNX). Runner `route_check.py`. **Prouvé**
  (mix 5 docs) : facture→STRUCTURED/AUTO ; courrier→RAG ; docx→RAG ; image HP→STRUCTURED/AUTO (OCR lazy a
  tiré) ; CI recto→STRUCTURED/HUMAN (règle paire). **STRUCTURED 3 | RAG 2.** CI **paires** gardent leur entrée
  dédiée (hors routeur single-doc).
- **Lane RAG — retriever sémantique (embedding GGUF) livré + prouvé en A/B.** 2e impl `GgufEmbeddingRetriever`
  derrière le **même slot `Retriever`** : modèle `granite-embedding-311M-multilingual-r2-Q8_0.gguf` (FR/EU,
  RGPD), servi par **`llama-server --embedding`** (la build b9542 n'a **pas** de binaire `llama-embedding` ;
  endpoint OpenAI `/v1/embeddings`). **Config vérifiée à la source** contre le projet sibling *Personal
  Assistant* (`C:\…\Personal Assistant\clients.py` + `llama-swap\assistant.config.yaml`, 2026-06-29) :
  flags `-c 512` (**limite native** granite-embedding → mes chunks ~120 tokens passent), `--pooling` omis
  (défaut GGUF), `--embd-normalize` défaut 2 (L2) → **cosine = produit scalaire**. Client **`urllib` stdlib
  (pas de dép httpx)**. Cycle serveur géré : start lazy sur port libre, poll `/health`, `close()` context-
  manager — **prouvé sans orphelin** (seul le `granite-chat` de PA, port 5800, restait — pas touché). Modèle
  copié dans `models/` (gitignoré ; env `RAG_EMBEDDING_MODEL`/`RAG_EMBEDDING_BINARY` pour pointer ailleurs).
  Runner : `rag_check.py --engine embedding`. **A/B prouvé** sur l'article (33 chunks, requête « agent loop
  call tools ») : les 2 moteurs s'accordent sur le top-1 (chunk 20 = boucle `tool_calls`) ; l'embedding
  remonte en #2 le chunk 8 (gestion sortie d'outil, sémantique) là où le lexical prend l'intro dense en
  mots-clés → l'apport sémantique est visible. Garde-fou char-budget avant embed (sur-longueur → fail-loud).
- **Lane RAG — baseline lexicale prouvée sur les 3 vrais non-structurés.** L'autre branche du routing
  2-lanes : un doc qui matche **aucun template structuré** → pas d'extraction, mais on donne à l'humain une
  prise → **résumé de contenu** (extractif : mots-clés + phrases saillantes) **+ index interrogeable**
  (cosine top-k). `ocr_bifunction/rag.py` : slot **`Retriever` jetable** (patron `OcrEngine`) ; 1re impl
  `TfidfRetriever` **maison, zéro download/dép lourde** (TF-IDF lissé + cosine, vecteurs L2). Le **même cœur
  TF-IDF** sert le résumé (`summarize_extractive`) ET le retrieval. `chunk_document` (paragraphes packés
  ~120 tokens). Runner `rag_check.py <doc> [--query --top-k]` via `read_document` (docx natif / PDF text
  layer, **pas d'OCR**). **Prouvé** : 2 mémos docx → résumés utiles (commandes/sécurité/dépôt ; pto/prises/
  free) ; article PDF (33 chunks) → résumé correct + requête « agent loop call tools » → top chunk = la
  boucle tool_calls. Décidé (délégué par l'utilisateur) : **lexical-first behind a slot**, embedding
  sémantique = swap suivant (cf. NEXT, modèle granite fourni). Mojibake console = cosmétique (codepage), la
  donnée Python est unicode-correcte.
- **Validation facture config-driven (value-check HT + TVA = TTC) — prouvée sur vrai corpus.** Validateur
  générique `validate_fields(fields, validation)` dans `template.py` (2 types de check, **les critères
  voyagent avec le template** = sketch D2) : `present` (présence, value-agnostic — déjà HP) **+ `sum`**
  (value-check : `terms` somment à `equals` à `tolerance` près). 4 templates facture dotés d'un bloc
  `validation` **par layout** : full-VAT (`facture_sortante_01`, `facture_entrante_02`) → sum ht+tva=ttc ;
  autoliquidation/293 B (`facture_entrante_01/03`) → présence ht seule (**l'absence de sum est la déclaration
  honnête du template, pas un trou**). Runner `facture_check.py` (miroir `hp_check.py`, born-digital →
  text-layer PyMuPDF, **pas de moteur OCR**). `hp_check.py` **refactoré** sur le validateur partagé
  (iso-sortie confirmée : AUTO 5/9 inchangé). **Prouvé** : 5 vraies factures → AUTO, 2 courriers
  mise-en-demeure → HUMAN (intrus, no match) ; sum-check tire sur le full-VAT réel (ex. 9966,00 + 0,00 =
  9966,00). **Bug attrapé par le smoke** : corpus réel 100 % TVA=0,00 → le sum ne discriminait jamais ;
  smoke synthétique `facture_validation_smoke.py` (TVA non nulle correcte/fausse/tolérance/manquant) a
  exposé une **erreur flottante au centime près** → corrigé en **comparaison centimes entiers** (exact).
  Décimales virgule ET point gérées (`_parse_amount`). Levier algo : `tolerance` par template (4e surface).
- **Escalade ASYNC câblée côté API + prouvée sur vraies images.** `api_maquette.py` : le douteux
  (`human`) n'est plus rendu `200 needs_review` synchrone → il **enfile une escalade hors chemin requête**
  et renvoie **`202 pending` + `job_id`** ; un **worker daemon sérialisé** (`ESCALATION_WORKER_COUNT=1`,
  `queue.Queue`, démarrage lazy) draine la file, re-run `process_ci_pair` AVEC `escalation_engine`, flippe
  le job `pending`→`done`. Job store aligné sketch **D1** (`received`→`processing`→`done`/`failed`, `lane`,
  `verdict`, `reasons`, `verso_read_path`) ; `GET /v1/jobs/{id}` mappe → `pending|done` client. Seam
  `set_escalation_engine_factory` (smoke injecte un faux moteur rapide, **zéro VLM 171 s**). Le fast-path
  `auto`→`200 validated` est **inchangé**. `force_pending` **supprimé** (stub « pas de worker » obsolète).
  **Prouvé** (oracle = runs réels, smokes `--expect`) : (a) concordant IMG_8391/8392 → `200 validated`/`auto` ;
  (b) 2021 (MRZ ratée) → `202 pending`→worker→`done`, **escalade tire dans le worker** (`engine.called=True`),
  hors requête ; (c) recto A (2021) + verso B (IMG_8392) → `202 pending` + 3 raisons mismatch réelles
  (numero_document, nom, prenoms) ; (d) structurel 400/404. Nouveau smoke versionné `api_smoke_async.py`
  (cycle pending→poll→done) ; `api_smoke_real.py` màj contrat (`needs_review`→`pending`). Pièges réglés :
  `tempfile.TemporaryDirectory(ignore_cleanup_errors=True)` sur le temp-dir du worker (race de nettoyage
  Windows WinError 145 quand le worker daemon survit à la requête — bénin, n'affecte aucune logique).
  **Simplification maquette assumée** : le worker re-run le pipeline complet (double RapidOCR) plutôt que
  reprendre l'état partiel du fast-path (couplage minimal ; à optimiser côté IT).
- **DoD maquette API CLOS — smoke vraies images VERT.** Piloté via `TestClient` (même `validate_document` +
  vrai `process_ci_pair`, sans serveur/port → contourne la friction uvicorn/PowerShell). 2 cas nommés sur de
  vraies CI : **paire concordante → 200 `validated`/`auto`** (0 reason) ; **recto A + verso B → 200
  `needs_review`/`human`** avec 3 reasons de mismatch réel (`numero_document`, `nom`, `prenoms`) — c'est la
  détection « recto A + verso B » qui tire, PAS le fallback « no MRZ ». La maquette est désormais **prouvée**,
  plus « écrite ». Smoke versionné `api_smoke_real.py` (argv `<recto> <verso>` + `--expect`, zéro PII).
- **API : argument optionnel `document_type` (hint de catégorie).** Un champ d'upload qui connaît déjà le type
  (« carte d'identité ») le passe → le matching de template est scopé à cette `category` seule (un template
  facture ne peut plus matcher par accident ; matching moins cher). Câblé `ValidateRequest.document_type` →
  `process_ci_pair(category=…)` → `read_recto_fields` → `load_templates(directory, category)`. Défaut `None` =
  tous les templates (comportement **inchangé**). Prouvé sur vraies images : hint `carte_identite` → `validated` ;
  mauvais hint `facture` sur une CI → `needs_review` + raison « recto: no 'facture' template matched ». Le smoke
  versionné a un flag `--document-type`.
- **1er doc NON-CI + validation config-driven (check de PRÉSENCE).** Template `hp_preuve_test_01.json` (category
  `preuve_test`) + runner `hp_check.py` : match signature de test (« Test de composants » + « SUCCÈS ») →
  extraction `id_acces`/`numero_serie` (regex) → **validation lue dans le bloc `validation` du template** (check
  `present`, rien de hardcodé). Prouvé sur les 9 images HP (RapidOCR brut, **zéro VLM**) : **AUTO 5/9** ; HUMAIN
  4/9 = 1 intrus BIOS correctement rejeté (no match) + 1 crop sans signature + 2 vraies pages où le label ID
  d'accès n'est pas capté. **Principe présence-vs-valeur prouvé** : un ID tronqué (« 7HS8S-MA ») valide quand
  même (présence ≠ valeur). Décision actée : **HP = RapidOCR suffit, PAS d'escalade VLM** → libère le budget SLM
  pour les value-checks durs (versos CI). Mémoire `template-validation-architecture-direction` étendue (présence-vs-valeur).
- **Moteur d'escalade LightOnOCR-2 livré + escalade PROUVÉE.** `LightOnOcrEngine` (slot `OcrEngine`,
  `ocr_bifunction/lightonocr_engine.py`) : shell vers `llama-mtmd-cli` (build b9542) + GGUF + mmproj, chemins
  configurables (arg > env `LIGHTONOCR_BINARY|MODEL|MMPROJ` > défaut). GGUF principal copié dans `models/`
  (gitignoré). **Valeur prouvée sur le cas le plus dur** : un verso CI dont RapidOCR ne parsait PAS la MRZ
  (`read_path=none`) → le VLM récupère la TD1 → **4/4 checksums ICAO**, clés concordantes avec le recto (le
  verdict final dépend de `reconcile` — cf. bullet « Escalade branchée » ci-dessous). Validé **via la classe**. Note IT : `docs/moteur-escalade-lightonocr.md`.
  Pièges réglés : `--image` casse sur virgule/accents Windows → temp ASCII interne. Cibles d'escalade réelles
  dans le corpus : **3/4 versos** (2021 MRZ illisible + French_1988 & changement-paul composite=False). **Sortie
  VLM = texte sans géométrie** (bbox synthétique) → extraction par contenu (MRZ) seulement, pas ancres recto.
- **Escalade branchée dans le pipeline (palier verso n°3).** `read_verso_mrz` = raw → enhance →
  `escalation_engine` injecté ; le VLM tourne UNIQUEMENT si raw+enhance ne donnent pas de MRZ de confiance.
  `process_ci_pair(escalation_engine=…)` ; défaut `None` = fast-path API n'escalade jamais (rétrocompatible :
  `main.py` / `api_maquette` inchangés). **Prouvé** (A/B sur 2021 recto+verso) : sans escalade → HUMAIN « no MRZ
  parsed » ; avec → `verso_read_path=escalation`, MRZ td1 récupérée, **4/4 checksums, 4/5 clés**. ⚠️ Le verdict
  reste HUMAIN sur cette paire : slip VLM d'1 caractère sur le prénom (`GAELE`≠`GAELLE`), ligne nom TD1 sans
  check digit → écart réel, HUMAIN défendable. **Le fallback = assurance, PAS garantie d'AUTO.** Dettes
  `reconcile._normalize` DIFFÉRÉES (micro-corpus, ne pas sur-tuner) : (a) accents jetés au lieu d'être pliés
  (`Ê`→∅) ; (b) pas de tolérance floue sur le nom = **décision métier sécurité** (assouplir affaiblit la
  détection « recto A + verso B »).

## Prochain pas
1. **Au choix** (cf. NEXT) : tier génératif (résumé/Q&A LLM via `granite-chat`), OU dettes `reconcile`,
   OU lane suggestion-template (SLM/GBNF, global).
2. **Dette `reconcile` (b) — TRANCHÉE par l'utilisateur : rester STRICT (pas de flou nom).** Raison
   métier (révélée 2026-06-29) : la **fraude réelle = frères aux noms proches** (Ahmed/Hamed/Ammed, 1-2
   char) → un match flou ferait passer le cœur de la fraude en AUTO. L'accent et la fraude sont
   **orthogonaux** : le folding (a) ne retire QUE les diacritiques (translittération ICAO, comme la MRZ)
   → `André`=`ANDRE` mais `Ahmed`≠`Hamed`. Accents traités spécifiquement, noms proches restent détectés.
   **Aucun code « flou » à ajouter — c'est une non-action actée.** Cf. mémoire `reconcile-name-match-strict`.
3. **Lane suggestion-template** (SLM/GBNF) — spec → `docs/briefs/BRIEF-suggestion-template.md` (global, plus tard).
4. **Validation facture — extensions** (si corpus s'élargit) : TVA non nulle réelle ; décimales mixtes ; multi-taux.
- **Async côté IT (différé, leur territoire)** : `_jobs` dict → table `ocr_jobs_*` (D1), worker Python →
  cron/queue réelle, idempotence/job store persistés. Cf. `docs/contrat-bd-destination.md` (co-geler jour J).
- Dettes mineures : décimales virgule/point ; date textuelle `facture_entrante_03` ; mmproj F32 (qualité max ; Q8 déjà OK).

## Suivis ouverts
- **Contrat BD destination (sketch, NON figé)** → `docs/contrat-bd-destination.md` : 3 domaines (jobs+queue
  / templates / revue-curation), 1 MariaDB préfixée, record source-unique en D1, critères avec le template,
  leviers algo hors-BD, contrat de colonnes. Vue sur la cible — à **co-geler avec l'IT** le jour J.
- **CLAUDE.md « État actuel du repo »** = **périmé** (dit « archi pas implémentée » alors que ①②③ + MRZ
  tournent). À actualiser + ajouter la carte des modules. Cf. mémoire `claudemd-module-map-pending`.
- **Dette template recto** : les anchors flous sont OK, mais sur la vraie mise en page 2 champs
  *périphériques* sont mal extraits (`lieu_naissance` attrape la date, `nationalite=None`). Hors clefs de
  reconcile → zéro impact verdict, mais à tuner pour un record CI complet.
- **Détection de coins du rectifier** = no-op sur photo plein cadre → à durcir si on veut le warp auto.
- **Pre-commit hook** posé (`3c3d055`). Tout nouveau clone : `sh scripts/setup-hooks.sh`.
