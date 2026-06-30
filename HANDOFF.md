# HANDOFF — OCR BiFunction

> Mémoire de passation entre sessions. **Lire en ouverture de session froide** (`/resume`) avant tout
> travail. Cadrage stable → [CLAUDE.md](CLAUDE.md) / [CADRAGE.md](CADRAGE.md). Décision moteurs →
> [docs/lecture-moteurs-paysage.md](docs/lecture-moteurs-paysage.md). **Ne pas dupliquer** ces docs ici :
> seulement l'état vivant + le prochain pas. Dates absolues.

> ⚠️ **Données sensibles** : `inputs/` (CI réelles, factures, photos terrain) et `outputs/` (extractions
> avec PII) sont **gitignorés** et n'ont **jamais** été versionnés. Aucune PII / donnée entreprise dans le
> repo ni l'historique (audité 2026-06-26). **Ce repo part sur GitHub** → ne jamais `git add -f` un doc,
> ne jamais coller de valeur réelle (nom, n°doc, adresse) dans le code, les docs ou un message de commit.

## État au 2026-06-30

**Porte d'entrée CI prouvée bout-en-bout + pipeline câblé + extraction factures multi-layout + lecture
couverte (RapidOCR + Docling fallback) ; LightOnOCR-2 validé en moteur d'escalade ; maquette API avec
escalade ASYNC câblée + prouvée sur vraies images (validated / pending→done) ; validation facture
config-driven (value-check HT+TVA=TTC).** POC solo sur `master`, pas de remote. **Pas de tests pytest**
— oracle = runs réels sur vrais docs + smokes structurels/logiques + KAT (composite MRZ), conforme à la
discipline smoke-first.

> ▶ **NEXT (reprise) — au choix.** L'API dispatche maintenant par `document_type` vers le bon flux (cf.
> Fait 2026-06-30). Pistes : (a) **tier génératif RAG** (résumé/Q&A LLM via `granite-chat` llama.cpp, comme
> Personal Assistant — surfacer le résumé dans la réponse `needs_review` non-structurée) ; (b) **lane
> suggestion-template** (SLM/GBNF, global). La limite « recto scoping » est **résolue** par le dispatch.

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

## Fait (2026-06-30)
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
