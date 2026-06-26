# HANDOFF — OCR BiFunction

> Mémoire de passation entre sessions. **Lire en ouverture de session froide** (`/resume`) avant tout
> travail. Cadrage stable → [CLAUDE.md](CLAUDE.md) / [CADRAGE.md](CADRAGE.md). Décision moteurs →
> [docs/lecture-moteurs-paysage.md](docs/lecture-moteurs-paysage.md). **Ne pas dupliquer** ces docs ici :
> seulement l'état vivant + le prochain pas. Dates absolues.

## État au 2026-06-26

**Porte d'entrée ①②③ prouvée sur de vrais docs.** Commit baseline `3fcc7a8` sur `master`
(1er commit du repo ; POC solo, pas de remote). Working tree : propre au commit (HANDOFF.md ajouté
ce tour, non committé). **Pas de tests pytest** encore — oracle = run réel (`smoke.py` / `extract.py`),
conforme à la discipline smoke-first.

### Ce qui tourne
- **① LIRE** — `ocr_bifunction/reader.py` : routeur par type + présence de couche texte. PyMuPDF
  (PDF born-digital), RapidOCR (images + pages image-only), python-docx. Chaque backend rend des
  `TextLine{texte, bbox, score, page}` (la **géométrie** porte les liens).
- **Slot OCR jetable** — `OcrEngine` Protocol ; 1 impl = `RapidOcrEngine` (`rapidocr_engine.py`).
- **②③ CI** — `template.py` + `templates/ci_fr_electronique_2021_recto.json` : `match_template`
  (= mini-stage ② via anchors de signature ; le registre de templates **fait office de dictionnaire de
  catégories**) + extraction d'ancres (label → valeur dessous, même colonne X) + normalisation dates ISO.
- Preuves : `uv run python smoke.py` (①, dump `.txt`+`.json` dans `outputs/`),
  `uv run python extract.py <doc>` (②③).

### KPI / findings (run 2026-06-26) — pas de verdict auto, l'utilisateur tranche
- Text-layer-first : 8 PDF + 2 docx lus **sans OCR**, en ms.
- RapidOCR images : conf 0.91–1.00, mais **confiance ≠ couverture** — verso CI = 54 car. inexploitables
  à conf 0.93.
- Latence OCR 3.7–20.7 s/img CPU → **batch OK** ; **API** = seuls les petits passent, le reste **cascade**
  (le pont du modèle dual, chiffré).
- CI recto → `{nom, prénoms, nationalité, naissance(ISO), lieu, n°doc, expiration(ISO)}`.

## Décisions actées
- **OCR = RapidOCR** (pip-only) — binaire Tesseract **absent** de la machine (2026-06-26).
- **Pas de SLM** pour l'instant. `granite-docling-258M.gguf` (local, ~178 Mo + mmproj requis) réservé =
  lane **batch soir / escalade**, jamais défaut batch (~3 s/img). Cf. docs note.
- **Templates JSON éditables hors-code** = le contrat ②③ que le Backoffice valide. **Plusieurs modèles
  par catégorie** (Kaizen : on enrichit petit à petit ; l'engagement du filtreur fait vivre le système).
- Hors-scope tant que la valeur n'est pas étendue : stages ④⑤, UI de revue, gouvernance/trace.

## Prochain pas (à trancher par l'utilisateur)
Reco : **1**.
1. **2e template CI (vieille CI 1988)** — valide le multi-template/catégorie (`match_template` choisit le
   bon des deux) + la direction `right` (`Nom: BERTHIER` en ligne, layout différent du 2021).
2. **Template facture** (PDF couche-texte, blocs PyMuPDF) — les docs à valeur volume.
3. **Preprocessing verso** — cf. mémoire `verso-ci-preprocessing` (blur + désat + threshold en OpenCV,
   mesuré A/B ; sinon passe regex MRZ dédiée).

## Suivis ouverts
- **CLAUDE.md « État actuel du repo »** dit encore « archi cible, pas implémentée » → **périmé** : ① + ②③
  CI sont codés. À actualiser (ajouter la carte des modules). Cf. mémoire `claudemd-module-map-pending`.
- **Pre-commit hook** (`.githooks/` versionné + `ruff`) : proposé, en attente de décision (opt-in).
