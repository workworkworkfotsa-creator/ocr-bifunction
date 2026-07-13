# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## NOT SKIPPABLE CONVENTIONS ##

Don't assume. Don't hide confusion. Surface tradeoffs.
Minimum code that solves the problem. Nothing speculative.
Touch only what you must. Clean up only your own mess.
Define success criteria. Loop until verified.
When searching for functions or hierarchies in the module, use CodeGraph first (`codegraph_explore` MCP tool, or `codegraph explore "..."` in the shell) then fall back to grep if it fails.

> Cadrage complet + rationale : **[CADRAGE.md](CADRAGE.md)** (doc self-porteur). Ce fichier = guidage
> opérationnel stable + garde-fous.

## Le produit en une phrase

**OCR BiFunction** — un poste d'entrée documentaire bi-mode : **lire** des documents (CI, factures…) →
**catégoriser** → **auto-valider le confiant** → **remonter à l'humain le douteux**, sous **deux régimes**
(batch backoffice / API temps réel). La valeur = le tri par confiance + le bi-mode, **pas** l'OCR.

## BiFunction = les deux modes

- **Backoffice (batch)** — docs en lot, traités la nuit. Latence = *heures* → moteur OCR plus lourd OK.
- **API temps réel** — user soumet, validé séance tenante. Latence = *secondes* → fast-path ; les cas durs
  **escaladent** vers la lane batch/humain (le gate de confiance = le pont).

Même pipeline d'étages, deux régimes. Détail : [CADRAGE.md](CADRAGE.md).

## Les étages

```
LIRE (OCR) → CATÉGORISER → VALIDER DATA (Python) → CENTRALISER → REMONTER
   └──── porte d'entrée (①②) = le seul étage dur ────┘   └ trivial une fois ①② vert ┘
```

## Principes (depuis lesquels tout se déduit)

1. **La porte d'entrée d'abord.** Lire + catégoriser fiablement = là où tout se gagne (leçon du cimetière
   EB : les projets butaient tous là). **Prouver ①② sur de vrais docs AVANT** ③④⑤ et avant les 2 modes.
   Smoke-first.
2. **L'OCR est un outil jetable.** Tesseract / Docling / petit SLM = adaptateurs interchangeables derrière
   une interface. Jamais le siège de la valeur.
3. **Routing par confiance, pas validation aveugle.** Chaque étage a une porte auto/humain. L'humain ne
   voit que le douteux. « Illisible » = signal (→ humain ou nouvelle catégorie), pas un déchet.
4. **Le trivial reste trivial.** Catégorie connue + data propre → remplissage = Python déterministe. Ne
   pas sur-construire.
5. **Indépendant de l'EB.** Peut *alimenter* un EB comme outil, mais système distinct — ne pas fusionner.

## Contrainte matérielle (pilote l'archi)

Cible ≈ prod : **~8 Go RAM utiles, pas de GPU**. Côté batch, tenir **100 → ~5000 docs/lot** ; côté API,
tenir **les secondes**. → **petits modèles, faisabilité d'abord**. Un VLM 3B est exclu à ce volume
(mesuré). Machine partagée entre projets → **demander avant toute exécution lourde** si une autre tâche
tourne.

## Hors scope (tant que ①② pas verts) — LARGEMENT LEVÉ (①② prouvés)

> ⚠️ Section historique : ①② sont prouvés depuis 2026-06, donc ce hors-scope est **levé**. Sont désormais
> construits (déduits des principes, pas spéculatifs) : stage ③, l'API temps réel maquettée + le worker
> async, l'UI de revue, la gouvernance (verdict 3 états, non-conformité, politiques, leviers). Détail →
> [HANDOFF.md](HANDOFF.md). Reste hors-scope réel : le choix FIGÉ du moteur OCR (RapidOCR acté API,
> LightOnOCR acté escalade) et les décisions IT (cf. aller-retour IT). Le garde-fou anti-cimetière ci-dessous
> tient toujours : rien de spéculatif au-delà de ce que les principes déduisent.

Stages ③④⑤, les 2 modes complets, UI de revue, gouvernance/trace, choix figé du moteur OCR. Réintroduits
**après** que la porte d'entrée soit prouvée sur de vrais documents.

## Toolchain

`uv`, `ruff` (format + lint), `pytest`. Noms longs et clairs (cf. CLAUDE.md global). Code + commentaires
en anglais, communication en français. Le **code = vérité ultime** ; ce fichier = cadrage stable.

### Commandes

```bash
uv sync                       # install/refresh env depuis pyproject + uv.lock
uv run python main.py         # lance l'entry point
uv add <pkg>                  # dép runtime   | uv add --dev <pkg> pour le dev
uv run ruff format .          # format
uv run ruff check . --fix     # lint (+ autofix)
uv run pytest                 # suite complète
uv run pytest path/to/test_x.py::test_name   # un seul test
uv run pytest -k "expr" -x    # filtre par nom, stop au 1er échec
```

> **État actuel du repo** (2026-07-13) : bien au-delà de la porte d'entrée. **①②③ prouvés sur de vrais
> docs** (pipeline CI `process_ci_pair` recto↔verso raw-first→enhance-retry ; MRZ TD1+legacy, 4 checksums
> ICAO ; extraction factures born-digital regex ; **verdict 3 états** auto/review/reject) **+ le flux complet
> maquetté** : porte API (`api_maquette.py`) → politique d'exécution 3 modes → verdict → revue humaine →
> drafting nightly → promotion → re-match ; worker asynchrone (`worker_watchdog.py`) ; 6 surfaces de config
> (D1..D6 + leviers). **Architecture approfondie 2026-07-13** (candidats A–F) : `intake.handle_document` =
> couche de traitement unique que les 2 régimes traversent, `validation.py` = moteur de verdict + registre
> de checks (scindé de `template.py`), `llama_transport.py` = transport SLM unique, `Verdict`/`Store`
> unifiés, contrat `OcrEngine`/`TextLine` durci. Moteur OCR : RapidOCR (API) + Docling/LightOnOCR
> (batch/escalade). Oracle = **smokes autonomes** (~18 verts, pas de pytest). Détail vivant →
> [HANDOFF.md](HANDOFF.md) ; concepts → [docs/dictionnaire-metier.md](docs/dictionnaire-metier.md) ;
> contrat BD → [docs/contrat-bd-destination.md](docs/contrat-bd-destination.md).

## Où on en est + vision émergente (actualisé 2026-06-28)

Porte d'entrée **prouvée** sur de vrais docs (CI recto↔verso AUTO ; factures lues + extraites). La section
« Hors scope » ci-dessus se relâche donc : l'extension a commencé, **déduite des principes** — pas spéculative.

### CATÉGORISER = 2 lanes
- **Structurés officiels** (CI, factures) → OCR + **template d'extraction** (1 par type/layout ; les
  `templates/*.json` = proxy d'une future table BD) + **auto-validation config-driven** : calculer TOUS les
  checks disponibles (ex. 4 checksums MRZ), un config par template dit lesquels sont requis. L'humain curate ;
  un SLM pourra *suggérer* un template pour un doc structuré jamais vu → l'humain valide → croissance organique.
- **Non-structurés** (mémos docx, articles) → **lane RAG** (retrieval), **aucune extraction**.

### Moteur OCR — où on en est (l'« API »)
Le moteur est un **slot jetable** (`OcrEngine`). Mesuré sur le hardware cible (8 Go, sans GPU) :
- **API (secondes) = OCR classique rapide** : couche-texte (PyMuPDF, ms) puis **RapidOCR** (3.7–20.7 s/img).
  Seule voie tenable en temps réel.
- **Batch/escalade = plus lourd** : Docling (layout + reading-order, backend RapidOCR) et les VLM OCR.
- **VLM OCR sur CPU = batch only, JAMAIS l'API** (mesuré). granite-docling-258M : 5–34 min/img *et* échoue sur
  les photos d'écran (modèle doc-conversion, mauvais outil ici). **≤1B borne la taille, pas la latence CPU.**
  À tester côté qualité sur images dures : **LightOnOCR** (1B), préféré aussi **RGPD** (LightOn = société
  française, on traite de la PII). Voie rapide = GGUF + llama.cpp (besoin du mmproj). Détail → [HANDOFF.md](HANDOFF.md).

## Garde-fou anti-cimetière

Ce projet rejoint une longue série de POC. **Rester léger** : pas d'archi spéculative, pas de stages aval
ni de 2e mode avant la porte d'entrée. Premier livrable = un smoke « lire + catégoriser N vrais docs » qui
tranche le moteur OCR. Tout le reste attend ce verdict.
