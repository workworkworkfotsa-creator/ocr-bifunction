# Contrat BD — sketch de destination

> **SKETCH, pas un schéma figé.** Vue sur la cible pour garder la direction en tête — **PAS à
> construire maintenant**. À **co-geler conjointement avec l'IT et dater** le jour de la passation
> (leçon dure du contrat de fabrique : livrer ≠ geler). Cadrage POC↔IT → skill `handoff-it`.
> Aujourd'hui seul le **domaine 1** est touché (câblage async ; le `_jobs` de la maquette → table).

## Principe

**Le contrat qui traverse la frontière POC→prod = les tables.** Le reste — mécanisme de queue,
moteurs OCR, worker, UI — est un **adaptateur jetable**. Physiquement côté IT : **1 MariaDB `tools`,
tables préfixées** (pas 3 bases). « 3 BD » = **3 domaines** = 3 lifecycles + 3 propriétaires distincts.

## Les 3 domaines (qui possède quoi)

| Domaine | Préfixe proposé | Surface (handoff-it) | Propriétaire | Lifecycle | Proxy actuel |
|---|---|---|---|---|---|
| **1 — Jobs + queue async** | `ocr_jobs_*` | Infra / exécution | **IT** | opérationnel / transient | `_jobs` dict (`api_maquette.py`) |
| **2 — Templates (+ critères validés)** | `ocr_templates_*` | Dictionnaire métier | **Expert métier / Backoffice** (PAS IT, PAS algo) | référence, lent | `templates/*.json` |
| **3 — Revue / curation** | `ocr_review_*` | Métier (revue) + staging | **User / reviewer** | curation, croissance organique | — (à venir) |

## Domaine 1 — Jobs + queue (worker Python écrit)

Le store opérationnel = le `_jobs` de la maquette rendu réel. La **queue = les lignes `status` en
attente** (un worker les dépile ; mécanisme = adaptateur). Colonnes (esquisse) :

- `job_id` (PK), `request_id` (idempotence), `document_ref` (recto/verso ou pointeur stockage)
- `lane` : `fast` | `escalation`
- `status` : `received` | `processing` | `needs_review` | `done` | `failed`
- `verdict` : `auto` | `human` | null ; `reasons` (texte/JSON)
- `verso_read_path` : `raw` | `enhance` | `escalation` | `none`
- **le RECORD extrait** (champs consolidés) = **ici, source de vérité unique**
- `created_at`, `updated_at` (**`NOW()` explicite** — MariaDB 5.5 n'a pas `DEFAULT CURRENT_TIMESTAMP`)

## Domaine 2 — Templates (Backoffice curate)

Ce que `templates/*.json` proxysent. Une ligne = un template **et ses critères de validation**
(les critères **voyagent avec** le template — déjà le cas : bloc `validation` du JSON ; pas de table
critères séparée). Colonnes : `template_id` (PK), `category`, `match` (anchors), `fields` (extraction),
`validation` (checks requis), `active`, `version`.

## Domaine 3 — Revue / curation (reviewer humain écrit)

La couche humaine + le **staging des suggestions**. **Référence le job (D1), ne duplique pas le record.**

- `review_id` (PK), `job_id` (FK → D1)
- `resume` / `analyse` = **projection** pour l'humain (vue, pas une 2e source de vérité)
- `comment` (humain), `decision` : `accept` | `reject`
- **suggestions** : template candidat + critères proposés (SLM), `status` : `pending` | `validated`
- **promotion D3 → D2** : valider une suggestion **insère/active** le template en D2 (transaction)

→ C'est la **boucle de croissance organique** (cf. mémoire `template-validation-architecture-direction`) :
un template suggéré → validé par l'humain → devient actif en D2. (Bonus futur, non construit :
l'historique des `decision` en D3 = données pour améliorer les suggestions SLM.)

## Contrat de colonnes — QUI écrit QUOI (non négociable : 2 écrivains)

| Écrivain | Écrit | Lit seulement |
|---|---|---|
| **Worker async (Python)** | D1 (status, verdict, record) | D2 (templates actifs) |
| **UI de revue (humain / PHP)** | D3 (comment, decision, suggestions) | D1 (`needs_review`) |
| **Promotion (transaction)** | D2 (template activé) | D3 (suggestion validée) |

L'UI **lit** le `status` D1, ne le réécrit jamais → pas de course Python↔PHP sur la même ligne.

## La 4e surface — leviers algo (PAS une BD)

Propriétaire = **toi (data science)**. Patron VRP : **constante Python + seed override** (tuning sans
redéploy), pas une table métier. Leviers connus à ce stade :

- seuil de confiance OCR / gate ; **tolérance fuzzy sur le nom** (décision sécurité — assouplir
  affaiblit la détection « recto A + verso B ») ; **quels checks requis par défaut** par catégorie ;
  concurrence de la file d'escalade (1-2).

## À co-geler avec l'IT (le jour J)

- Geler le schéma **conjointement + daté** ; négocier toute évolution de forme (leçon `partner_sources`).
- **Valider contre la vraie cible** (MariaDB 5.5 / Antelope) : `NOW()` explicite, index ≤ 767 o utf8mb4,
  pas de SQL arbitraire ni de creds en UI, PK composite portée par une clé partenaire si multi-partenaire.
- Fournir un `CLAUDE.md` par sous-livrable + une section « Pour le Claude de l'IT ».
