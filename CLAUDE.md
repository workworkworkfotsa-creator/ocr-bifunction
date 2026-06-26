# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Hors scope (tant que ①② pas verts)

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

> **État actuel du repo** : squelette `uv init` (Python ≥ 3.12, `main.py` = hello-world, 0 dépendance,
> pas encore de tests). Premier livrable attendu = le smoke « lire + catégoriser N vrais docs » qui tranche
> le moteur OCR (cf. *Garde-fou anti-cimetière*). Toute l'archi des étages ci-dessus est **cible**, pas
> encore implémentée.

## Garde-fou anti-cimetière

Ce projet rejoint une longue série de POC. **Rester léger** : pas d'archi spéculative, pas de stages aval
ni de 2e mode avant la porte d'entrée. Premier livrable = un smoke « lire + catégoriser N vrais docs » qui
tranche le moteur OCR. Tout le reste attend ce verdict.
