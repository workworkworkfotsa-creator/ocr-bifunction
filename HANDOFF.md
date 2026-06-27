# HANDOFF — OCR BiFunction

> Mémoire de passation entre sessions. **Lire en ouverture de session froide** (`/resume`) avant tout
> travail. Cadrage stable → [CLAUDE.md](CLAUDE.md) / [CADRAGE.md](CADRAGE.md). Décision moteurs →
> [docs/lecture-moteurs-paysage.md](docs/lecture-moteurs-paysage.md). **Ne pas dupliquer** ces docs ici :
> seulement l'état vivant + le prochain pas. Dates absolues.

> ⚠️ **Données sensibles** : `inputs/` (CI réelles, factures, photos terrain) et `outputs/` (extractions
> avec PII) sont **gitignorés** et n'ont **jamais** été versionnés. Aucune PII / donnée entreprise dans le
> repo ni l'historique (audité 2026-06-26). **Ce repo part sur GitHub** → ne jamais `git add -f` un doc,
> ne jamais coller de valeur réelle (nom, n°doc, adresse) dans le code, les docs ou un message de commit.

## État au 2026-06-27

**Porte d'entrée CI prouvée bout-en-bout + pipeline câblé + extraction factures multi-layout, sur de vrais
docs ; + lecture couverte (RapidOCR + Docling fallback).** POC solo sur `master`, pas de remote. Dernier
commit `6a74fe6`. **Pas de tests pytest** — oracle = runs réels sur vrais docs + smokes structurels + KAT
(composite MRZ), conforme à la discipline smoke-first.

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

## Prochain pas (la roadmap lecture est couverte — décisions à trancher)
**Lecture couverte** : text-layer (PyMuPDF) + RapidOCR (fast-path) + Docling (fallback batch) + docx (python-docx).
1. **standard-Docling vs VLM granite-docling** : pour mieux *lire* les images dures, le levier = granite (autre
   modèle), pas Docling standard (qui retombe sur RapidOCR). À mesurer sur une vraie image dure.
2. **Routing fallback** : quand escalader RapidOCR → Docling (analogue au raw-first → enhance, au niveau moteur).
3. **Lane RAG** (docx + articles PDF) : monter le sous-système retrieval, ou rester une étiquette pour l'instant.
4. **Validation facture** (cohérence HT+TVA=TTC) = futur « template de validation » config-driven
   (mémoire `template-validation-architecture-direction`).
- Dettes mineures : décimales virgule/point ; date textuelle `facture_entrante_03` (« AOUT 2022 ») non ISO-isable ;
  gate cascade verso config-driven (composite-based).

## Suivis ouverts
- **CLAUDE.md « État actuel du repo »** = **périmé** (dit « archi pas implémentée » alors que ①②③ + MRZ
  tournent). À actualiser + ajouter la carte des modules. Cf. mémoire `claudemd-module-map-pending`.
- **Dette template recto** : les anchors flous sont OK, mais sur la vraie mise en page 2 champs
  *périphériques* sont mal extraits (`lieu_naissance` attrape la date, `nationalite=None`). Hors clefs de
  reconcile → zéro impact verdict, mais à tuner pour un record CI complet.
- **Détection de coins du rectifier** = no-op sur photo plein cadre → à durcir si on veut le warp auto.
- **Pre-commit hook** posé (`3c3d055`). Tout nouveau clone : `sh scripts/setup-hooks.sh`.
