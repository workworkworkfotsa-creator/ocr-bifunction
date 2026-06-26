# CADRAGE — OCR BiFunction

> Doc **self-porteur** : tout ce qu'il faut pour reprendre le projet à froid, sans contexte externe.
> Compagnon : `CLAUDE.md` (cadrage opérationnel pour Claude Code).

## Le produit en une phrase

Un **poste d'entrée documentaire bi-mode** : il **lit** les documents (cartes d'identité, factures, …),
les **catégorise**, **auto-valide** ceux dont on est sûr, **ne remonte à un humain que le douteux** — et
le fait sous **deux régimes** (batch backoffice / API temps réel) selon *comment* le document arrive.

## Le problème (terrain)

Les documents s'accumulent. La main-d'œuvre **valide tout sans regarder** → la validation ne vaut rien
(GIGO). On veut **inverser la charge** : la machine traite le volume connu/confiant, l'humain se
concentre sur les cas qui le méritent.

## BiFunction = les deux modes (l'identité du projet)

Le **même pipeline d'étages** tourne sous deux régimes — *comment* le doc arrive fixe le **budget de
latence**, qui décide quel moteur OCR est même candidat :

| | **① Backoffice (batch)** | **② API temps réel** |
|---|---|---|
| Déclencheur | docs reçus en lot, traités le soir/la nuit | un user soumet un doc, validé séance tenante |
| Budget latence | **heures** | **secondes** |
| Moteur OCR | plus lourd toléré (couverture max) | **fast-path obligatoire** (petit/rapide) |
| Cas durs | passe lourde (VLM) tolérée | **escaladent** → lane batch ou humain |

**Le pont** : le gate de confiance permet à l'API de **basculer un cas dur vers la lane batch/humain** au
lieu de faire attendre l'user. Un seul système, deux régimes.

## Le retournement de valeur

- Aujourd'hui : l'humain valide 100 % → attention diluée → garbage.
- Cible : **auto-validation gâtée par la confiance** → l'humain ne voit que la fraction incertaine → son
  attention vaut enfin quelque chose.

## Les étages (communs aux deux modes)

```
doc entrant (CI, facture, …)
   │
   ▼ ① LIRE          OCR (outil jetable : Tesseract / Docling / petit SLM)   ┐ PORTE D'ENTRÉE
   ▼ ② CATÉGORISER   « c'est quoi ? »  (CI France / facture / …)             ┘ = le seul étage dur
   │      ├ catégorie connue + assez d'exemples → AUTO
   │      └ inconnue / doute → HUMAIN (ou créer une nouvelle catégorie)
   │
   ▼ ③ VALIDER LA DATA   (Python déterministe, par catégorie)
   │      ├ champs cohérents → AUTO-REMPLIR
   │      └ échec → HUMAIN (lire ce que le robot ne lit pas)
   │
   ▼ ④ CENTRALISER + RANGER par catégorie
   ▼ ⑤ REMONTER à l'aval (contrat)
```

## La porte d'entrée = le crux (la leçon du cimetière)

Les projets précédents (famille EB) **butaient tous sur la porte d'entrée**. Ici aussi : **on gagne ou on
perd à ①②** (lire + catégoriser fiablement). Une fois la catégorie connue et la data propre, **③④⑤ sont
triviaux** (Python déterministe).

**Discipline (anti-cimetière)** : on **prouve ①② sur de vrais documents AVANT** de bâtir le reste. Aucun
stage aval, aucun 2e mode, tant que la porte d'entrée n'est pas verte.

## Contraintes (non négociables)

- **OCR = un outil jetable**, pas la valeur. Interchangeable derrière une interface. La valeur = les
  étages + le routing par confiance + le bi-mode.
- **Indépendant de l'EB.** Pourrait *alimenter* un EB comme outil, mais c'est un **autre système** — ne
  pas fusionner.
- **Hardware ≈ prod** : ~8 Go utiles, **pas de GPU**. Tenir le **volume réel (100 → ~5000 docs/lot)** côté
  batch, et **les secondes** côté API. → petits modèles, faisabilité d'abord. (Un VLM 3B est mort à ce
  volume — mesuré en bac à sable GutenOCR.)

## Non tranché (à décider au fil)

- Le **jeu de catégories** (le « dico ») et le seuil « assez d'exemples → auto ».
- Les **règles de validation data par catégorie** (③).
- Le **moteur d'entrée par mode** (Tesseract vs Docling vs petit SLM) → à **bencher** sur vrais docs
  (latence + RAM + fiabilité).
- La **cible de centralisation** (④) et le **contrat de remontée** (⑤).
- Le **seuil de confiance** qui déclenche l'escalade API → batch/humain.
- Gouvernance / trace (append-only) : *plus tard*, si le besoin se confirme.
