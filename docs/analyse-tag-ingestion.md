# Analyse — un TAG D'INGESTION (type de document) comme axe de décision

> Document d'aide à la décision, écrit 2026-07-21 pendant une pause de réflexion. **Rien n'est
> décidé ici** : il expose les options et leurs contreparties pour qu'on puisse trancher, et
> revenir sur ce choix plus tard sans re-dériver le raisonnement. Chiffres = **mesurés**, sources
> ancrées. Concepts → [dictionnaire-metier.md](dictionnaire-metier.md).

## L'idée

Plutôt qu'un choix GLOBAL par décision, **spécialiser selon le type d'ingestion** : photo, PDF
image (scanné), PDF natif (born-digital).

## Le recadrage que ça produit (le point important)

Plusieurs décisions que j'avais posées comme des arbitrages globaux **n'en sont pas** : chacune
n'affecte en réalité qu'une lane. Vues comme globales, elles s'opposent entre elles ; vues par
lane, elles deviennent locales et sûres.

## Le paysage mesuré (2026-07-21)

| | **Photo / image** | **PDF scanné** | **PDF natif** |
|---|---|---|---|
| Chemin de lecture | `_read_image` → moteur OCR | `get_pixmap(dpi=200)` → OCR | couche texte PyMuPDF |
| Grain de la géométrie | région de texte (≈ ligne) | idem | **bloc** (≈ paragraphe) |
| Hauteur de boîte médiane | **1,66 %** de la page | idem | **3,3 %** |
| Pire cas mesuré | 13,96 % | idem | **30 %** (⅓ de page) |
| Résolution du repère | **NON contrôlée** (caméra, image intégrée) | **contrôlée** (200 dpi → 1654×2339 px) | stable (points PDF) |
| Mode de défaillance « sens » | mauvaise reconnaissance OCR | idem | **mojibake CMap** |
| Confiance | score OCR ∈ [0,1] | idem | `None` (exact) |
| Complétude multi-pages | sans objet (1 page) | `conversion_guard` | `conversion_guard` |

Sources : [reader.py:239](../ocr_bifunction/reader.py:239) (routage par suffixe),
[reader.py:306](../ocr_bifunction/reader.py:306) (rendu 200 dpi),
[reader.py:293](../ocr_bifunction/reader.py:293) (blocs PyMuPDF),
[pipeline.extract_card_images](../ocr_bifunction/pipeline.py) (images CI intégrées, résolution native).

## Ce que chaque décision parkée devient, une fois scopée

| Décision parkée | Lane réellement concernée | Effet du scoping |
|---|---|---|
| Resserrer les spans au mot (`get_text("words")`) | **PDF natif SEULEMENT** | L'OCR est déjà au grain ligne (1,66 %). Le fix ne touche plus la lane CI **du tout** → risque de régression ≈ nul |
| Tolérances en pixels dépendantes de la résolution | **Photo SEULEMENT** | Le scan PDF est rendu par nous à 200 dpi (déterministe) ; le natif est en points. Seule la photo a une résolution libre — et c'est la lane CI |
| Garde d'intégrité-caractères (mojibake) | **PDF natif SEULEMENT** | Déjà constaté : la passe observateur a laissé 2 docs image non évalués. Le mojibake CMap n'existe pas sans couche texte |
| Chemin lourd / `conversion_guard` | **PDF (les deux)** | Sans objet sur une photo mono-page |

**Conclusion de cette section** : le tag ne « personnalise » pas du confort — il **découpe des
chantiers risqués en chantiers locaux**. C'est son argument le plus fort.

## ⚠️ Le grain honnête du tag est la PAGE, pas le document

Un document peut être **mixte**, et le code le gère déjà : le lecteur produit un `backend_name`
`pymupdf+<moteur>` quand un même PDF a **des pages à couche texte ET des pages image**
([reader.py](../ocr_bifunction/reader.py), calcul de `backend_name`). Un tag au niveau du DOCUMENT
serait donc un **mensonge** pour ces fichiers-là — exactement le genre de donnée fausse-en-silence
que le projet a déjà rencontré avec `document.pages` vs `confidence.pages`
([[docling-produced-signal-confidence-pages]]).

Trois grains possibles, du plus grossier au plus fidèle :
1. **document** — simple, faux sur les mixtes ;
2. **page** — fidèle, coût modéré ;
3. **ligne/span** — le plus fidèle : chaque `TextLine` sait déjà de quel backend il vient (il porte
   son repère de page). Le tag deviendrait alors une *lecture* de l'existant, pas une donnée
   nouvelle à maintenir.

L'option 3 mérite examen : elle n'ajoute **aucune source de vérité**, donc rien à désynchroniser.

## DÉCLARÉ ou DÉTECTÉ ? (le vrai fork)

- **Détecté** — le lecteur sait déjà (suffixe, présence d'une couche texte). Toujours vrai, zéro
  saisie, mais l'uploadeur ne peut rien exiger.
- **Déclaré** — l'uploadeur annonce le type. Le repo a **déjà ce patron** : `document_type` est
  déclaré à la porte et un écart devient une **non-conformité** (`intake._type_mismatch_outcome`).
  Un type d'ingestion déclaré-puis-démenti serait traitable pareil.

Ils ne s'excluent pas : **détecté = la vérité**, déclaré = une *attente* dont l'écart est un signal.
C'est exactement la doctrine `expected_holder_name` du projet.

## Pour / contre — introduire le tag MAINTENANT

**Pour**
- Rend locaux 3 chantiers aujourd'hui risqués ou bloqués (tableau ci-dessus).
- Rend la question « quel oracle valide ce changement ? » répondable par lane.
- Rend explicite un routage **déjà réel mais implicite** ([reader.py:239](../ocr_bifunction/reader.py:239)).
- Surface de config naturelle pour le métier (patron D4/D6), cohérent avec la fabrique.

**Contre**
- **Risque de taxonomie prématurée** : un tag qu'aucun code ne consomme = code inerte, ce que la
  discipline du projet interdit explicitement. Il faut un consommateur *le jour même*.
- Le grain document est faux (mixtes) → il faut choisir page ou ligne, donc ce n'est pas « juste
  un champ ».
- Une 4e catégorie arrive vite (`.docx`, et le `heavy_page_converter` quand il sera câblé) : une
  énumération figée trop tôt se paie.

## Recommandation (à valider, pas actée)

**Ne pas créer le tag comme donnée maintenant.** L'information existe déjà et le grain fidèle est
la ligne : `TextLine` porte son repère de page depuis 2026-07-21, et « a-t-il un repère » distingue
déjà les lanes. Utiliser l'axe comme **critère de SCOPING des chantiers** (« ce fix ne concerne que
le born-digital ») donne dès aujourd'hui tout le bénéfice **sans ajouter de donnée à maintenir**.

Le jour où un consommateur RÉEL le réclame — politique d'exécution par type, ou un rendu de page
qui doit savoir s'il rend un scan ou un natif — le poser alors, **au grain page**, détecté d'abord.

## Ce qui invaliderait cette recommandation

- Un besoin métier de **déclarer** le type à la porte (donc de le contrôler), pas seulement de le
  constater.
- Une politique d'exécution qui doit différer par type **avant** la lecture (choix de moteur, de
  lane, de rétention) — là, détecter trop tard ne suffit plus.
