# HANDOFF — OCR BiFunction

> Mémoire de passation entre sessions. **Lire en ouverture de session froide** (`/resume`) avant tout
> travail. Cadrage stable → [CLAUDE.md](CLAUDE.md) / [CADRAGE.md](CADRAGE.md). Décision moteurs →
> [docs/lecture-moteurs-paysage.md](docs/lecture-moteurs-paysage.md). **Ne pas dupliquer** ces docs ici :
> seulement l'état vivant + le prochain pas. Dates absolues.

> ⚠️ **Données sensibles** : `inputs/` (CI réelles, factures, photos terrain) et `outputs/` (extractions
> avec PII) sont **gitignorés** et n'ont **jamais** été versionnés. Aucune PII / donnée entreprise dans le
> repo ni l'historique (audité 2026-06-26). **Ce repo part sur GitHub** → ne jamais `git add -f` un doc,
> ne jamais coller de valeur réelle (nom, n°doc, adresse) dans le code, les docs ou un message de commit.

## État au 2026-06-26

**Porte d'entrée ①②③ prouvée + MRZ recto↔verso amorcé, sur de vrais docs.** POC solo sur `master`,
pas de remote. Dernier commit `3680c87`. **Pas de tests pytest** — oracle = run réel
(`smoke.py` / `extract.py` / `mrz_check.py` / `preprocess_ab.py`), conforme à la discipline smoke-first.

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
- Hors-scope tant que la valeur n'est pas étendue : stages ④⑤, UI de revue, gouvernance/trace.

## Prochain pas (à trancher par l'utilisateur)
**Le payoff = reconcile recto↔verso bout-en-bout sur la vraie paire.** Deux parties :
1. **Anchors recto robustes à l'OCR** — sur la vraie carte un sous-label est mal lu (`rn`→`m`) → le
   template recto ne matche pas. Fix : matching flou (distance d'édition) ou anchors de repli.
2. **`reconcile(recto_fields, mrz_fields)`** sur les clefs partagées (n°doc, nom, prénom, naissance,
   expiration) → **auto** si tout concorde + checksums verts, **humain** si une clef diverge (= « recto de
   A + verso de B » détecté).

Autres pistes en réserve : template facture (PDF couche-texte) ; améliorer la détection de coins du
rectifier ; tuner `enhance` ; preprocessing verso (mémoire `verso-ci-preprocessing`).

## Suivis ouverts
- **CLAUDE.md « État actuel du repo »** = **périmé** (dit « archi pas implémentée » alors que ①②③ + MRZ
  tournent). À actualiser + ajouter la carte des modules. Cf. mémoire `claudemd-module-map-pending`.
- **Anchors de template OCR-fragiles** (sous-labels mal lus sur vraies cartes) → cf. partie 1 ci-dessus.
- **Détection de coins du rectifier** = no-op sur photo plein cadre → à durcir si on veut le warp auto.
- **Pre-commit hook** posé (`3c3d055`). Tout nouveau clone : `sh scripts/setup-hooks.sh`.
