# Contrat BD — sketch de destination

> **SKETCH, pas un schéma figé.** Vue sur la cible pour garder la direction en tête — **PAS à
> construire maintenant**. À **co-geler conjointement avec l'IT et dater** le jour de la passation
> (leçon dure du contrat de fabrique : livrer ≠ geler). Cadrage POC↔IT → skill `handoff-it`.
> État : les **4 domaines sont proxifiés** (D1/D2/D3 depuis 2026-07-02 ; politiques d'exécution
> depuis 2026-07-08).

## Principe

**Le contrat qui traverse la frontière POC→prod = les tables.** Le reste — mécanisme de queue,
moteurs OCR, worker, UI — est un **adaptateur jetable**. Physiquement côté IT : **1 base relationnelle
interne, tables préfixées** (pas 3 bases). « 3 BD » = **3 domaines** = 3 lifecycles + 3 propriétaires distincts.
(Le produit + version exacts de la BD cible sont un détail d'intégration — hors de ce dépôt public,
fixés avec l'IT au gel du schéma.)

> **Mécanisme de connexion (proxy)** : depuis 2026-07-13 la connexion + le schéma + la migration
> du proxy SQLite vivent dans **UN** `ocr_bifunction/store.py` (`class Store`) ; les 7 repos le
> reçoivent (`Store | chemin`) au lieu d'ouvrir chacun leur connexion. C'est le **point de swap IT** :
> un adaptateur pour la BD cible interne remplace `Store` derrière les 7 mêmes interfaces de repo. `Store(":memory:")`
> = la même SQL en mémoire pour les tests (repos partageant une connexion). Chaque repo garde SON
> `CREATE TABLE` (locality) ; le Store ne fait que l'exécuter.

## Les 3 domaines (qui possède quoi)

| Domaine | Préfixe proposé | Surface (handoff-it) | Propriétaire | Lifecycle | Proxy actuel |
|---|---|---|---|---|---|
| **1 — Jobs + queue async** | `ocr_jobs_*` | Infra / exécution | **IT** | opérationnel / transient | **`ocr_bifunction/repository.py` (`SqliteRepository`, table `ocr_jobs`)** |
| **2 — Templates (+ critères validés)** | `ocr_templates_*` | Dictionnaire métier | **Expert métier / Backoffice** (PAS IT, PAS algo) | référence, lent | `ocr_bifunction/template_repository.py` (seed = `templates/*.json`) |
| **3 — Revue / curation** | `ocr_review_*` | Métier (revue) + staging | **User / reviewer** | curation, croissance organique | `ocr_bifunction/review_repository.py` |
| **4 — Politiques d'exécution** | `ocr_execution_policies` | Infra / exécution (contenu opéré par le Backoffice) | **Backoffice / opérateur** (l'IT possède le store, pas le contenu) | config vivante, très lent | `ocr_bifunction/execution_policy.py` |
| **5 — Registre des organismes** | `ocr_issuer_registry` | Dictionnaire métier (conformité) | **Expert métier / Backoffice** | référence curée, lent | `ocr_bifunction/issuer_registry.py` |
| **6 — Politiques de non-conformité** | `ocr_conformity_policies` | Dictionnaire métier (réaction) | **Expert métier / Backoffice** | config vivante, très lent | `ocr_bifunction/conformity_policy.py` |
| **7 — Clefs use_case (auth)** | `ocr_use_case_keys` | Infra / auth (premier auth de la maquette) | **Opérateur** | secrets, très lent | `ocr_bifunction/use_case_key.py` |
| **8 — Tables extraites (cellules)** | `ocr_document_table_cells` | Dictionnaire métier (la STRUCTURE, dans D2) + opérationnel (les VALEURS) | **Expert métier** (structure, une fois par layout) / **worker** (valeurs, par document) | référence lente + opérationnel | **aucun — sketch, rien de construit** |

## Domaine 1 — Jobs + queue (worker Python écrit)

Le store opérationnel = le `_jobs` de la maquette rendu réel. La **queue = les lignes `status` en
attente** (un worker les dépile ; mécanisme = adaptateur). Colonnes (esquisse) :

- `job_id` (PK), `request_id` (idempotence), `expected_holder_name` (titulaire DÉCLARÉ à la porte —
  saisie manuelle v1, nourrit `reconcile_ci` ; upgrade futur : lu depuis le record CI validé),
  `document_ref` (pointeur spool). **Lifecycle du spool** :
  une row `needs_review` GARDE ses bytes (la revue montre le doc, la passe DRAFT clusterise les
  unknowns) et une row `rejected` AUSSI (la preuve de non-conformité part à la revue / compliance) ;
  purge aux autres états terminaux et au sweep de clôture (« clore » une non-conformité purge sa
  preuve) — PII hygiène, un seul purgeur par phase
- `execution_lane` : `fast` | `escalation` (CI douteuse, VLM) | `deferred` (politique
  `async_immediate`, drainée en continu) | `nightly` (politique `async_nightly`, drainée
  par la passe de nuit `--nightly` — l'ordonnanceur de nuit interne)
- `status` : `received` | `processing` | `needs_review` | `done` | `rejected` | `failed`
- `verdict` : `auto` | `review` | `reject` | null (= `Verdict.value` ; l'ancien `human` retiré
  2026-07-12, vocabulaire unifié — **à re-signaler au gel IT**) ; `reasons` (texte/JSON)
- `verso_read_path` : `raw` | `enhance` | `escalation` | `none`
- **le RECORD extrait** (champs consolidés) = **ici, source de vérité unique**. Colonne
  `record_fields`, JSON, **une seule forme pour TOUTES les lanes** (structurée et CI — une colonne
  qui changerait de forme selon l'écrivain serait illisible pour l'IT) :
  `{"<nom_champ>": {"value": str|null, "origin": "anchor"|"pattern"|"mrz"|null,
  "spans": [{"page_index": int (0-based), "bbox": [x0,y0,x1,y1]}]}}`.
  **`bbox` est NORMALISÉE : 4 fractions dans `[0,1]`** de la largeur / hauteur de page (origine
  top-left). Volontairement PAS les unités natives du lecteur : elles diffèrent selon le backend
  (points PDF à 72 dpi pour une couche texte, pixels d'un rendu 200 dpi pour l'OCR), donc un tuple
  brut est **impossible à placer** sans savoir lequel. Normalisée, un consommateur dessine la zone
  sans unité, sans dpi, sans dimensions à transporter (`left: x0*100%`), et la valeur survit à un
  changement de résolution de rendu ou de moteur OCR.
  **`spans` porte la PROVENANCE** (décidé 2026-07-21) : exigence produit « nœud → page → on montre
  la zone » — un reviewer ne peut valider ou corriger une valeur que s'il voit la région d'où elle
  sort. C'est une **liste** (un regex peut matcher à cheval sur plusieurs lignes, donc plusieurs
  pages), et elle est **vide quand la provenance n'existe pas** (backfill MRZ : la zone lue est
  décodée, pas localisée sur la carte ; champ non trouvé). **La provenance absente reste absente,
  jamais fabriquée.** Deux causes d'absence, toutes deux réelles : un champ backfillé depuis la
  MRZ (décodée, pas localisée) et une lecture **sans repère de page** — le lecteur VLM émet des
  boîtes synthétiques qui encodent l'ORDRE de lecture, pas une position, donc il ne déclare aucune
  dimension et aucun span n'en est frappé.
  **PRÉCISION** (mesurée 2026-07-21 sur facture réelle) : la bbox cerne **les mots de la valeur**,
  pas le paragraphe qui la contient. Le born-digital est lu par BLOCS PyMuPDF (jusqu'à 30 % d'une
  page) ; le span est resserré aux mots que la valeur occupe, sélectionnés **par position dans le
  texte** — jamais par orthographe, un même mot revenant plusieurs fois sur une page. Gain mesuré
  sur les 3 champs : aire **3,0× à 7,7×** plus petite, hauteurs ramenées à 1,46–1,65 % de la page
  (une ligne). Les lanes OCR n'ont pas de grain mot et n'en ont pas besoin : leurs boîtes sont déjà
  au grain ligne (médiane 1,66 %) — elles retombent sur la ligne entière.
  **Limite résiduelle, mineure** : un mot que le lecteur ne rend pas verbatim (ligature, césure)
  est ignoré, donc la boîte peut être un peu plus étroite que la valeur ; si aucun mot ne
  correspond, repli sur le bloc entier. Jamais de mot placé approximativement. `origin` nomme le chemin d'obtention — ce n'est pas un score de qualité.
  Producteur/lecteur = `template.field_payload` / `template.payload_value` (une seule
  connaissance de la forme). **Pas de `superseded_by` ici, contrairement à D8** : D1 porte
  l'extraction MACHINE ; la correction humaine appartient à D3 (qui référence le job sans
  dupliquer le record). Un champ = un extracteur, donc rien à arbitrer — au contraire d'une
  cellule de table, où deux extracteurs coexistent parce qu'aucun ne suffit.
- `created_at`, `updated_at` (**timestamps écrits explicitement** — la BD cible peut ne pas avoir
  `DEFAULT CURRENT_TIMESTAMP` selon sa version ; le proxy les écrit déjà, donc portable)

## Domaine 2 — Templates (Backoffice curate)

Ce que `templates/*.json` proxysent. Une ligne = un template **et ses critères de validation**
(les critères **voyagent avec** le template — déjà le cas : bloc `validation` du JSON ; pas de table
critères séparée). Colonnes : `template_id` (PK), `category`, `match` (anchors), `fields` (extraction),
`validation` (checks requis ; une règle peut porter **`severity: reject|review`** — le métier durcit
ou adoucit la classe d'un échec DÉTERMINÉ de ce check, jamais un « je ne peux pas savoir »),
`reference_roles_json` (**config métier** : quels champs du record = titulaire / délivrance /
expiration quand les docs de ce template CORROBORENT des titres — `corroborated_by` ; assigné par le
reviewer à la promotion), `active`, `version`.

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

## Domaine 4 — Politiques d'exécution (l'opérateur écrit, la porte lit)

Le « quand » : par catégorie de document, le régime d'exécution — `sync` (dans la requête),
`async_immediate` (file continue du watchdog), `async_nightly` (passe de nuit). Les infra et les
besoins changent → **table éditée via UI (`/policies`), effet immédiat, zéro redéploy**. Défauts
**dans le code** (`DEFAULT_EXECUTION_POLICIES`), seed idempotent qui **n'écrase jamais** une édition
opérateur (patron « leviers » de la fabrique). Colonnes :

- `category` (PK ; `*` = la ligne de défaut, non supprimable)
- `execution_mode` : `sync` | `async_immediate` | `async_nightly`
- `override_allowed` : le client de l'API peut-il imposer son `processing_mode` optionnel
  (cohabitation : `carte_identite` verrouillée sync, une facture peut être poussée en nuit)
- `created_at`, `updated_at` (`NOW()` explicite)

Résolution à la porte : ligne de la catégorie, sinon `*` ; hint client honoré **seulement** si
`override_allowed` ; toute décision non triviale tracée dans `reasons`.

## Domaine 6 — Politiques de non-conformité (le métier écrit, la porte + le worker lisent)

Le « QUE FAIRE » quand un document est PROUVÉ non conforme (terminologie actée 2026-07-12 : la
machine prouve la non-conformité, la fraude est le jugement de compliance). Par catégorie
(résolution sur le type DÉCLARÉ d'abord, `*` en défaut) :

- `action` : `block` (cet upload refusé — défaut) | `block_holder` (+ les uploads suivants du même
  titulaire déclaré tant que la non-conformité est ouverte) | `flag_and_continue` (rien de bloqué,
  flag + revue humaine, le process continue)
- `category` (PK ; `*` non supprimable), `created_at`, `updated_at` (`NOW()` explicite)

La preuve (row `rejected` + bytes retenus) est visible dans la section « Documents non conformes »
de la revue ; « clore » = vu / transmis compliance → le worker purge la preuve au sweep.

## Domaine 7 — Clefs use_case (le premier auth de la maquette)

Décision 2026-07-20 (cf. mémoire `enveloppe-profondeur-variable`, CADRAGE-META du méta-repo) :
un second consommateur réel de la lecture démarre (`sop_contract`, réconciliation
contrat↔SOP↔instruction), aux côtés du consommateur existant (`ci_pii`, validation
CI/habilitation). Une clef API à la porte résout QUEL profil consommateur traite la
requête — jamais la FORME de sortie, qui reste un schéma unique (seule sa profondeur de
remplissage variera, une fois le lecteur SOP construit — **non fait à ce stade**, cf.
`use_case_key.py` : ce domaine ne fait QUE l'auth + la traçabilité, volontairement, pour
ne pas construire de code inerte en avance d'un lecteur qui n'existe pas).

- `key_id` (PK), `key_hash` (SHA-256, le secret brut n'est **jamais** stocké — affiché une
  seule fois à la création), `label`, `use_case` (`ci_pii` | `sop_contract`), `created_at`,
  `updated_at`.
- **Défaut silencieux** : requête sans clef → `use_case="ci_pii"`, comportement inchangé
  pour tout appelant antérieur à ce header (zéro régression, patron `resolve_execution`).
  Clef présentée mais inconnue/révoquée → **401** (une vraie garantie d'auth, jamais un
  repli silencieux).
- **Snapshot, pas FK vivante** : `ocr_jobs.use_case` capture le profil résolu AU MOMENT de
  l'intake ; révoquer une clef plus tard ne réécrit jamais l'historique des rows passées.
- Prouvé `use_case_key_smoke.py` **13/13** (défaut silencieux ; clef inconnue → 401 ; clef
  émise → job porte le bon use_case ; liste n'expose jamais le secret/hash ; révocation →
  401 identique à inconnue ; double révocation → 404 sans crash ; use_case inconnu à la
  création → 422). Régressions vertes : `flow_smoke` 14/14, `policy_smoke` 20/20,
  `conformity_smoke` 12/12, `holder_reference_smoke` 5/5.

## Domaine 8 — Tables extraites (cellules + provenance) — SKETCH, rien de construit

**Pourquoi un domaine à part** : pour des centaines de types de fichiers, la **table est la principale
source d'information** — pas le texte autour. Or l'information d'une table n'est pas ses mots, c'est
**quel mot est en relation avec quel autre** (ligne ↔ colonne ↔ portée). L'aplatir en texte détruit
exactement ce qui la rend interrogeable — c'est toute la raison d'avoir des **nœuds chaînés** plutôt
que du RAG pur. Le record plat de D1 (`fields`) ne peut pas porter ça.

**LE PIÈGE, mesuré sur un document réel (2026-07-21) — une portée n'est pas une ligne.** Sur un titre
d'habilitation, le libellé `Exécutant` apparaît **deux fois** : sous la section *TRAVAUX D'ORDRE NON
ÉLECTRIQUE* (valeur `B0/HOV`) et sous *TRAVAUX D'ORDRE ÉLECTRIQUE* (valeur `B1V`). Stocké à plat en
`(ligne, colonne, valeur)`, ça produit **deux triplets contradictoires pour la même clé** → donnée
fausse, et fausse **silencieusement**. Le libellé de section est une **PORTÉE**, pas une ligne de
données. Aucun des deux extracteurs testés ne le représente comme tel (l'un le réplique dans chaque
colonne, l'autre l'isole en cellule) → **c'est la curation humaine qui le déclare**. La colonne
`section_path` existe uniquement pour ça et **n'est pas optionnelle**.

**Deux couches, deux propriétaires, deux lifecycles :**

1. **La STRUCTURE** — quelles colonnes, laquelle est la clé de ligne, quelles lignes sont des portées.
   Curée **une fois par layout**, jamais par document → **appartient à D2** (bloc `table_schema` qui
   voyage avec le template, même doctrine que `validation` et `reference_roles_json`). L'humain n'y
   choisit pas un extracteur : il **déclare le sens**.
2. **Les VALEURS** — par document, en forme **tidy/joignable** (une ligne = une cellule). C'est cette
   forme qui rend les croisements possibles ; la grille n'est qu'un rendu.

**Colonnes (esquisse) — `ocr_document_table_cells`** :
- `cell_id` (PK), `job_id` (FK → D1), `table_id` (n° de table dans le document), `template_id`
  (FK → D2, null si extraction non curée) ;
- `section_path` — la **PORTÉE** (ex. `TRAVAUX D'ORDRE ELECTRIQUE`), null si la table n'en a pas.
  **Sans elle la clé de ligne n'est pas unique** (cf. piège ci-dessus) ;
- `row_key` (libellé de ligne), `column_header`, `value` ;
- **PROVENANCE — obligatoire, pas un confort** : `page_number` + `bbox`. Exigence produit posée
  2026-07-21 : « nœud 14, page 12 → on montre la section du document original ». Un humain doit
  pouvoir **voir la zone** d'où sort une valeur, sinon il ne peut ni valider ni corriger. Cette
  provenance est **impossible à reconstruire après coup** → elle est portée par la cellule dès
  l'extraction ;
- `extractor` (`neural` | `geometric` | `human`) + `superseded_by` (null = valeur retenue).

**On RETIENT les deux extractions, on n'en CHOISIT pas une** (mesuré 2026-07-21) : un choix exclusif
**perd de la donnée**. Sur le document témoin, le lecteur **neural** portait les bonnes relations
ligne↔valeur mais avait **perdu deux dates** ; le lecteur **géométrique** avait ces dates mais avait
**détruit** les relations (5 libellés fusionnés dans une seule cellule). Donc :
- le **neural fournit le squelette** (les relations, ce que l'autre détruit) ;
- le **géométrique est une source de valeurs** là où le premier a droppé ;
- **l'humain arbitre**, et les deux extractions brutes restent **retenues comme preuve** (même
  doctrine que la rétention du spool en D1).

Un curateur qui cliquerait « prends A » en bloc perdrait deux dates en silence — d'où `extractor` +
`superseded_by` plutôt qu'une valeur unique écrasée.

**Le levier de gouvernance — « ai-je besoin de cette donnée croisable ? »** Produire la forme
relationnelle **coûte une curation humaine** (déclarer la structure une fois par layout). Beaucoup de
types de documents n'en ont pas besoin : le texte suffit (lane RAG). Le choix **par type de document**
appartient donc à **l'entité compétente côté métier** — jamais un défaut de code, jamais une décision
de l'algo. Config, pas constante : patron D4/D6 (défaut en code, override opéré, effet à l'upload
suivant).

**Statut : la table D8 n'est pas construite** — mais la brique la plus fragile, la **provenance**,
a cessé d'être jetée en amont (2026-07-21). Le chemin de lecture lourd conserve désormais la
géométrie (`TextSpan(text, bbox)` porté par la réconciliation → `TextLine` reconstruits →
`ReadResult.lines`), donc « page + bbox » **existe au moment de l'extraction** au lieu d'être perdu.
La lane structurée a suivi le **2026-07-21** : `extract_fields` renvoie désormais
`dict[str, ExtractedField]` (valeur + `spans` + `origin`) au lieu de `dict[str, str | None]`, sur
**les deux chemins** (ancre géométrique ET regex — pour ce dernier, la géométrie est reconstruite
via la plage de caractères du match sur le texte joint). Donc « page + bbox » ne meurt plus **ni à
la lecture ni à l'extraction**, et D1 les stocke (cf. domaine 1). Reste NON fait : la provenance
**par cellule de table**, faute de consommateur (ce serait du code inerte). Les deux extracteurs de
tables sont mesurés (cf.
[outils-evalues.md](outils-evalues.md)) et la fenêtre d'adjudication humaine existe
(`table_adjudication_build.py` → HTML local **gitignoré**, contenu réel). Le schéma ci-dessus est un
sketch, à co-geler avec l'IT comme les autres.

## Leviers infra — `ocr_capacity_settings` (l'admission de la porte)

Worst-case assumé (2026-07-12 : serveurs modestes, pas de rack GPU) : le sync est **plafonné** et
le plafond est un **levier vivant** (patron VRP : défaut en code, seed clé/valeur, override lu à
chaque requête — s'adapte au hardware du jour J sans redéploy). Table générique clé/valeur
(`setting_key` PK, `setting_value`) pour accueillir les futurs leviers sans changement de schéma :

- `SYNC_CONCURRENCY_LIMIT` (défaut 2) — traitements sync simultanés max.
- `SYNC_OVERFLOW_ACTION` (défaut `defer`) — porte saturée : `defer` = bascule async (202, lane
  `deferred` — le bi-mode EST la soupape de pression) | `reject_503` = refus + `Retry-After`.

**Notes worst-case pour l'IT (limites assumées du proxy, à traiter à l'intégration)** :
- pas de kill mi-OCR d'un thread Python → le timeout dur par requête = **gateway/reverse-proxy IT** ;
- le verrou global + la connexion SQLite unique sont des artefacts du proxy → la BD cible interne
  apporte la vraie concurrence ; les scans par requête (`attestations validées`, `rejected` par titulaire)
  deviennent des **requêtes indexées** (index `status`, `expected_holder_name`, `template_id`) ;
- multi-process : la porte est stateless SAUF le cache d'idempotence en mémoire (borné LRU) — le
  `request_id` étant en D1, une idempotence cross-process peut se re-dériver de la table ;
- le watchdog reste volontairement mono-job (8 Go) ; sa concurrence = levier futur si le hardware suit.

## Contrat de colonnes — QUI écrit QUOI (non négociable)

| Écrivain | Écrit | Lit seulement |
|---|---|---|
| **Worker async (Python)** | D1 (status, verdict, record) | D2 (templates actifs) |
| **UI de revue (humain, UI interne)** | D3 (comment, decision, suggestions) | D1 (`needs_review`) |
| **Promotion (transaction)** | D2 (template activé) | D3 (suggestion validée) |
| **UI politiques (opérateur + métier, UI interne)** | D4 (`ocr_execution_policies`), D6 (`ocr_conformity_policies`), leviers (`ocr_capacity_settings`) | — |
| **UI registre (expert métier, UI interne)** | D5 (`ocr_issuer_registry`) | — |
| **Porte API (Python)** | D1 (insertion des jobs) | D4 (résolution sync/async), D2, D5 (contexte de conformité), D6 (réaction), D7 (résolution use_case) |
| **UI clefs use_case (opérateur, UI interne)** | D7 (`ocr_use_case_keys`) | — |
| **Passe DRAFT nightly (Python)** | D3 (suggestions stagées) | D1 (`needs_review` + spool), D2 (ids libres) |
| **Extraction de tables (Python)** *(D8, à construire)* | D8 (cellules des 2 extracteurs + provenance page/bbox) | D2 (`table_schema` du template) |
| **UI d'adjudication de tables (humain, UI interne)** *(D8, à construire)* | D8 (valeur retenue via `superseded_by`, cellules `human`), D2 (`table_schema` curé) | D1 (job + spool pour afficher la zone d'origine), D8 (les 2 extractions) |

L'UI **lit** le `status` D1, ne le réécrit jamais → pas de course Python↔UI interne sur la même ligne.

## La 4e surface — leviers algo (PAS une BD)

Propriétaire = **toi (data science)**. Patron VRP : **constante Python + seed override** (tuning sans
redéploy), pas une table métier. Leviers connus à ce stade :

- seuil de confiance OCR / gate ; **tolérance fuzzy sur le nom** (décision sécurité — assouplir
  affaiblit la détection « recto A + verso B ») ; **quels checks requis par défaut** par catégorie ;
  concurrence de la file d'escalade (1-2).

## Serving SLM (décision actée 2026-07-12)

**llama.cpp supervisé par llama-swap, sur Linux** — Ollama/LocalAI/vLLM écartés. Le code ne dépend
que d'un endpoint HTTP compatible OpenAI (`LLAMA_SWAP_URL`) : le serving est un adaptateur.
Checklist « changer X et Y sur Linux » → **[deploiement-linux-serving-slm.md](deploiement-linux-serving-slm.md)**.

## À co-geler avec l'IT (le jour J)

- Geler le schéma **conjointement + daté** ; négocier toute évolution de forme (leçon `partner_sources`).
- **Valider contre la vraie cible** (le produit/version + format de ligne de la BD interne, à
  confirmer avec l'IT) : timestamps écrits explicitement, **attention aux limites de taille d'index**
  selon l'encodage et le format de ligne de la cible, pas de SQL arbitraire ni de creds en UI, PK
  composite portée par une clé partenaire si multi-partenaire.
- Fournir un `CLAUDE.md` par sous-livrable + une section « Pour le Claude de l'IT ».
