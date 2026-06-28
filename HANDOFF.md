# HANDOFF — OCR BiFunction

> Mémoire de passation entre sessions. **Lire en ouverture de session froide** (`/resume`) avant tout
> travail. Cadrage stable → [CLAUDE.md](CLAUDE.md) / [CADRAGE.md](CADRAGE.md). Décision moteurs →
> [docs/lecture-moteurs-paysage.md](docs/lecture-moteurs-paysage.md). **Ne pas dupliquer** ces docs ici :
> seulement l'état vivant + le prochain pas. Dates absolues.

> ⚠️ **Données sensibles** : `inputs/` (CI réelles, factures, photos terrain) et `outputs/` (extractions
> avec PII) sont **gitignorés** et n'ont **jamais** été versionnés. Aucune PII / donnée entreprise dans le
> repo ni l'historique (audité 2026-06-26). **Ce repo part sur GitHub** → ne jamais `git add -f` un doc,
> ne jamais coller de valeur réelle (nom, n°doc, adresse) dans le code, les docs ou un message de commit.

## État au 2026-06-28

**Porte d'entrée CI prouvée bout-en-bout + pipeline câblé + extraction factures multi-layout + lecture
couverte (RapidOCR + Docling fallback) ; LightOnOCR-2 validé en moteur d'escalade ; maquette API écrite
(contrat structurel prouvé, smoke vraies images EN ATTENTE).** POC solo sur `master`, pas de remote.
Dernier commit = `feat: API maquette` (cette session). **Pas de tests pytest** — oracle = runs réels sur
vrais docs + smokes structurels + KAT (composite MRZ), conforme à la discipline smoke-first.

> ▶ **NEXT (reprise) — Smoke vraies images de la maquette API (DoD non clos).** Le contrat structurel est
> prouvé (202/job/404/400/idempotence via `TestClient`), mais les 2 cas verdict sur de vraies CI ne sont
> **pas** encore lancés (utilisateur absent du PC au commit). Lancer : `uv run uvicorn api_maquette:app
> --reload` → `/docs` → paire concordante = `validated`, recto A + verso B = `needs_review`. Tant que ce
> smoke n'est pas vert, la maquette est « écrite », pas « prouvée ».

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

## Prochain pas
1. **CLORE le DoD de la maquette** : lancer le smoke `/docs` sur de vraies CI (validated + needs_review).
   Tant qu'il n'est pas vert, la maquette reste « écrite », pas « prouvée ». Cf. NEXT en tête.
2. **Routing escalade** : RapidOCR (échec/low-conf) → LightOnOCR-2 (batch/async).
3. **Lane suggestion-template** (SLM/GBNF) — spec → `docs/briefs/BRIEF-suggestion-template.md` (global, plus tard).
4. **Lane RAG** (docx + articles) ; **Validation facture** (HT+TVA=TTC, template de validation config-driven).
- Dettes mineures : décimales virgule/point ; date textuelle `facture_entrante_03` ; mmproj F32 (qualité max ; Q8 déjà OK).

## Suivis ouverts
- **CLAUDE.md « État actuel du repo »** = **périmé** (dit « archi pas implémentée » alors que ①②③ + MRZ
  tournent). À actualiser + ajouter la carte des modules. Cf. mémoire `claudemd-module-map-pending`.
- **Dette template recto** : les anchors flous sont OK, mais sur la vraie mise en page 2 champs
  *périphériques* sont mal extraits (`lieu_naissance` attrape la date, `nationalite=None`). Hors clefs de
  reconcile → zéro impact verdict, mais à tuner pour un record CI complet.
- **Détection de coins du rectifier** = no-op sur photo plein cadre → à durcir si on veut le warp auto.
- **Pre-commit hook** posé (`3c3d055`). Tout nouveau clone : `sh scripts/setup-hooks.sh`.
