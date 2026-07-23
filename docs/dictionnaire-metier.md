# Dictionnaire métier — OCR BiFunction

> Glossaire **curé à la main** (convention globale « doc vivante »). Une entrée n'est ajoutée que si le
> sens a été **vérifié à la source ET confirmé par l'utilisateur**. Ancres : `fichier:ligne` du repo, ou
> constat daté sur le corpus local (`inputs/`, gitignoré — jamais de nom de personne ni de partie ici :
> repo public).

## attestation de formation (organisme)

Document émis par un **[[organisme-de-formation]] TIERS** à l'issue d'une formation (ex. préparation aux
habilitations électriques NF C 18-510). C'est **la preuve de confiance forte** de la chaîne certification :
l'émetteur est un tiers identifiable et vérifiable (raison sociale + SIRET/RCS présents **en texte natif**
dans le pied de page, constat corpus 2026-07-03 — couche texte PyMuPDF, sans OCR).
Régime de validation visé : check `issuer_registry` (l'émetteur lu ∈ registre curé des organismes
reconnus). Voir [[titre-d-habilitation]] pour le document qu'elle corrobore.
Source : corpus `inputs/` sondé 2026-07-03 (paire même layout, émetteur en texte) ;
`docs/briefs/BRIEF-template-drafting.md` (gitignoré) § régimes d'émetteur.

## titre-d-habilitation

Document **établi et signé par l'EMPLOYEUR** (mention normative lue dans les docs du corpus : NF C 18-510
chap. 5.5, délivré pour 3 ans) attestant qu'un salarié est habilité (codes [[codes-habilitation]]).
**Auto-déclaré par construction** → confiance faible seul : n'importe quel employeur peut en imprimer un
(« ma mère peut me faire une certif » — formulation utilisateur, 2026-07-03). L'émetteur (l'employeur)
**varie d'un doc à l'autre** → c'est un CHAMP à extraire/réconcilier, jamais une ancre de template ; sur
une partie du corpus il n'existe QUE dans l'image d'en-tête (invisible sans OCR).
Régime de validation visé : check `corroborated_by` — AUTO seulement si une
[[attestation de formation (organisme)]] **validée** existe pour le MÊME titulaire (réconciliation nom
**stricte**, cf. règle Ahmed≠Hamed) avec des dates cohérentes. Sinon → humain.
Source : corpus `inputs/` sondé 2026-07-03 (2 titres, 2 employeurs différents, mention « établi et signé
par l'employeur » en texte) ; confirmation utilisateur même date.

## organisme-de-formation

Le tiers émetteur d'une [[attestation de formation (organisme)]]. Identité forte = **SIRET/RCS** (un
fraudeur copie facilement un nom ou un logo, pas un SIRET valide inscrit au registre). Le **registre des
organismes reconnus** est une surface de config Backoffice à curer (même famille que les templates D2) —
pas encore implémenté (2026-07-03).

## codes-habilitation

Codes normalisés NF C 18-510 portés par les titres/attestations (H0B0, B0, H0, H0V, B1V, B2V, BR, BC…).
Liste fermée → check `vocabulary` : un code inventé ne passe pas.
Source : `docs/briefs/BRIEF-template-drafting.md` § kit de checks anti-fraude.

## verdict de routing (auto / humain / invalide)

L'issue d'un document a **TROIS états**, pas deux (affinage confirmé utilisateur 2026-07-03). C'est le
principe de routing par confiance précisé — la nuance clé étant **« je ne sais pas » ≠ « je sais que
c'est faux »** :

- **auto** — tout concorde, valide → auto-validé.
- **review (humain)** — « je ne CONNAIS pas / en attente » : template/layout non reconnu (comme une
  certification jamais vue → curation, peut naître un template), émetteur hors registre (peut être un
  nouvel [[organisme-de-formation]] légitime → l'humain l'ajoute), [[titre-d-habilitation]] non encore
  adossé (en attente d'une [[attestation de formation (organisme)]]). Checks concernés : `present`,
  `issuer_registry`, `corroborated_by`, plus le no-match de template.
- **reject (invalide)** — « je SAIS que c'est faux » : preuve POSITIVE de falsification → **rejet AUTO
  terminal, PAS de revue humaine**. Déclencheurs : `date_order`, `date_span` (dates incohérentes/rallongées
  au stylo), `vocabulary` (code inventé), `reconcile_ci` (nom titulaire ≠ record CI — fraude fratrie
  Ahmed≠Hamed), et **CI recto/verso** : une clef partagée qui DIVERGE entre recto et MRZ (« recto de A +
  verso de B »).

Nuance **input vs preuve** (fix 2026-07-03) : la classe dépend du POURQUOI de l'échec, pas du type de
check. Un input **manquant/illisible** (date non lue, contexte non câblé) ou un **read non fiable**
(checksum MRZ KO — un digit mal lu par l'OCR) → **review**, JAMAIS reject : on ne rejette une vraie carte
sur du bruit OCR. Seule une valeur **prouvée fausse** (2 sources qui se contredisent, maths cassées, code
hors liste) → reject.

Priorité : **reject > review > auto** (un doc prouvé invalide n'est pas adouci en « à revoir » parce
qu'il porte aussi un check en attente).

**Terminologie (confirmé utilisateur 2026-07-12) : dire « document NON CONFORME », pas « fraude ».**
La machine prouve une non-conformité (checksum cassé, dates incohérentes, émetteur hors registre,
type déclaré ≠ type reconnu) ; la FRAUDE est un jugement d'intention qui appartient au département
compliance — le mot est souvent employé de façon exagérée. Le statut technique reste `rejected` ;
les surfaces disent « document non validé car Y » et la PREUVE (template, checks calculés, document
retenu) part à la revue / compliance. La réaction est configurable →
[[politique de non-conformité (block / block_holder / flag_and_continue)]].

Vocabulaire canonique : **`auto` / `review` / `reject`** (le glossaire, `verdict.value`, la colonne
D1 `verdict` et le champ wire disent tous les trois mêmes littéraux — l'ancien `human` est retiré).

Source de vérité (unifiée 2026-07-12) : **`ocr_bifunction/validation/verdict.py` (`class Verdict`)** = LE domicile
unique du verdict 3-états. `Verdict.from_reasons(reject_reasons, review_reasons)` porte l'UNIQUE
précédence `reject > review > auto` ; `Verdict.d1_status` / `Verdict.wire_status` sont les SEULES
sérialisations vers un statut D1 (`ocr_bifunction/validation/status.py`, `STATUS_*`) ou HTTP. Les deux lanes
alimentent ce domicile : la lane structurée via `template.ValidationOutcome.verdict` (`evaluate_validation`,
`class CheckFailure`), la lane CI/MRZ via `reconcile.py` (buckets reject/review). Les ~6 tables de remap
(`router`/`orchestrator`/`api_maquette`/`batch_check`/`worker_watchdog`) sont SUPPRIMÉES — chaque sink lit
les propriétés de l'enum. Confirmation utilisateur 2026-07-03 (3 états) + 2026-07-12 (vocabulaire `review`,
domicile unique). Prouvé : `verdict_check.py` (11/11), `verdict_flow_check.py` (7/7, bout-en-bout),
`reconcile_verdict_check.py` (5/5), `escalation_reject_smoke.py` (5/5 — le trou reject→needs_review du
bridge d'escalade fermé). **Câblé de bout en bout** (structuré + CI/MRZ).

## politique d'exécution (sync / async immédiat / async nuit)

Le « QUAND traiter » d'un document, découplé du « QUOI » (demande utilisateur 2026-07-08 : les infra
et les besoins changent → le mapping catégorie→régime doit être une **config opérée**, pas du code).
Trois modes :

- **sync** — dans la requête HTTP (secondes ; moteurs classe RapidOCR).
- **async_immediate** — spool + ligne D1 `received` en lane `deferred`, drainée par le watchdog
  qui tourne en continu (minutes).
- **async_nightly** — même mécanique en lane `nightly`, drainée UNIQUEMENT par la passe de nuit
  (`worker_watchdog.py --once --nightly` = l'ordonnanceur de nuit interne).

Résolution à la porte : la ligne de la catégorie gagne, sinon la ligne `*` (défaut, non supprimable).
Le client de l'API peut envoyer un `processing_mode` optionnel — honoré **seulement** si la politique
de la catégorie dit `override_allowed` (cohabitation : `carte_identite` verrouillée sync — son doute
escalade par son propre chemin — pendant qu'une facture peut être poussée en nuit). Hint ignoré ou
mode async → tracé dans `reasons`. Défauts dans le code, seed qui n'écrase jamais une édition
opérateur, édition via la page `/policies` — effet à l'upload suivant, zéro redéploy.

Source de vérité : `ocr_bifunction/governance/execution_policy.py` (`EXECUTION_MODES`, `resolve_execution`,
`DEFAULT_EXECUTION_POLICIES`, table `ocr_execution_policies`) ; porte `api_maquette.py`
(`validate_document`) ; lanes `deferred`/`nightly` → `worker_watchdog.py`
(`CONTINUOUS_EXECUTION_LANES`, `--nightly`). Prouvé : `policy_smoke.py` 20/20 (2026-07-08).
Voir aussi [[verdict de routing (auto / humain / invalide)]] — le verdict dit « vers qui »,
la politique dit « quand ».

## registre des organismes (de formation)

La liste CURÉE des organismes de formation reconnus — la preuve forte du régime
[[attestation de formation (organisme)]] : l'émetteur lu sur le document (SIRET de préférence, un nom
se copie) doit appartenir à cette liste. C'est l'expert métier qui la possède et l'édite (page
`/registry`), pas l'IT ni l'algo. **Registre vide ou émetteur absent → revue humaine, JAMAIS un pass
silencieux ni un rejet auto** (un organisme inconnu peut être un nouvel organisme légitime que
l'humain ajoute). Concept confirmé utilisateur 2026-07-03 (2 régimes d'émetteur), surface livrée
2026-07-08.

Source de vérité : `ocr_bifunction/governance/issuer_registry.py` (table `ocr_issuer_registry`) ; check
`issuer_registry` → `ocr_bifunction/extraction/template.py` (`_check_issuer_registry`) ; contexte câblé →
`api_maquette._build_validation_context`, `worker_watchdog` (construction par passe).
Voir [[verdict de routing (auto / humain / invalide)]].

## passe DRAFT (drafting câblé au flux)

L'étape nocturne qui transforme les inconnus ACCUMULÉS en brouillons de templates, sans CLI : les
rows D1 `needs_review` sans template gardent leurs bytes (rétention du spool) → cluster par layout
(D-a) → draft par invariance (D-b) → checks candidats dérivés des extractions du cluster (D-c
partie 2 déterministe : dates → `date_order`/`date_span`, codes récurrents → `vocabulary` ; **garde
PII = récurrence** : un token n'entre en liste fermée que s'il revient dans ≥2 documents — un nom de
titulaire n'y entre jamais) → nommage SLM opt-in (fallback placeholders) → suggestion D3 `pending`
que l'humain COCHE et valide (promotion D2, re-match). Idempotente nuit après nuit.

Source de vérité : `ocr_bifunction/knowledge/drafting_flow.py` (`run_draft_pass`),
`ocr_bifunction/knowledge/drafting.py` (`seed_candidate_checks`), `worker_watchdog.py` (`--nightly`,
`--draft-ocr`, `--slm-naming`). Prouvé : `flow_smoke.py` 14/14 (2026-07-08).

## titulaire attendu (liaison document-titulaire)

Le nom du titulaire tel que le DOSSIER le déclare — la référence contre laquelle
`reconcile_ci` compare le nom lu sur le document (strict, accents pliés seulement :
la fraude réelle = frères aux noms proches, Ahmed≠Hamed). **v1 (confirmé utilisateur
2026-07-08) : saisi À LA MAIN** à l'upload (champ optionnel `expected_holder_name`) ;
absent → le check part en revue (fail-loud), jamais un pass ni un reject silencieux ;
divergence prouvée → **rejet auto terminal**. **Upgrade actée pour plus tard** : le lire
automatiquement depuis le record CI validé du même dossier (D1) au lieu de la saisie.

Source de vérité : `api_maquette.py` (`ValidateRequest.expected_holder_name`,
`_build_validation_context`) ; colonne `ocr_jobs.expected_holder_name`
(`ocr_bifunction/storage/repository.py`) ; check → `ocr_bifunction/extraction/template.py`
(`_check_reconcile_ci`) ; normalisation stricte → `ocr_bifunction/extraction/reconcile.py`.
Prouvé : `holder_reference_smoke.py` 5/5 (2026-07-08).
Voir [[verdict de routing (auto / humain / invalide)]].

## rôles d'attestation (mapping métier pour la corroboration)

Le mapping « quels champs du record d'une attestation validée jouent les rôles titulaire /
délivrance / expiration » quand ses documents servent à CORROBORER un [[titre-d-habilitation]]
(check `corroborated_by`). **Confirmé utilisateur 2026-07-08 : c'est de la CONFIG MÉTIER, pas du
code** — le bloc `attestation_reference_roles` voyage AVEC le template (même doctrine que les
checks : compute-all/config-requires) et c'est le reviewer qui l'assigne à la promotion (3 selects
sur la carte draft, parmi les champs du draft — les 3 rôles ou aucun). Un template sans bloc ne
corrobore rien ; aucun code à écrire par type de document. La projection record→référence est
mécanique (`context_assembly.py`) : jobs D1 clos (`done`) des templates à rôles →
`AttestationReference` (titulaire strict, fenêtre de validité ISO).

Source de vérité : `ocr_bifunction/knowledge/context_assembly.py` (`ATTESTATION_REFERENCE_ROLES_KEY`,
`collect_validated_attestations`) ; colonne D2 `reference_roles_json`
(`ocr_bifunction/storage/template_repository.py`) ; assignation → `api_maquette.py`
(`ValidateSuggestionRequest.reference_roles`, gardes 400) + `ui/review.html` ; check →
`ocr_bifunction/extraction/template.py` (`_check_corroborated_by`). Prouvé : `corroboration_smoke.py` 7/7
(2026-07-08). Voir [[attestation de formation (organisme)]], [[titre-d-habilitation]],
[[titulaire attendu (liaison document-titulaire)]].

## politique de non-conformité (block / block_holder / flag_and_continue)

Ce qu'un document PROUVÉ non conforme DÉCLENCHE — une config métier par catégorie (demande
utilisateur 2026-07-12 : « dans ces cas-là on bloque les uploads suivants, ou pas, ou on flag mais
le process continue ») :

- **block** (défaut) — CET upload est refusé, terminal : « document non validé car Y » à la page.
- **block_holder** — idem, ET les uploads SUIVANTS déclarant le même [[titulaire attendu (liaison
  document-titulaire)]] sont refusés tant que la non-conformité est OUVERTE (pas de décision de
  revue) ; « clore » à la revue débloque le dossier. Une row-trace d'upload refusé (sans document
  retenu) ne re-bloque jamais elle-même.
- **flag_and_continue** — rien n'est bloqué : la non-conformité est flaggée dans les raisons et le
  document part en revue humaine ; le process continue.

Dans TOUS les cas la preuve est RETENUE (bytes au spool, row `rejected` listée dans la section
« Documents non conformes » de la revue) jusqu'à la clôture, où le watchdog purge. La résolution se
fait sur le type DÉCLARÉ d'abord (un passeport envoyé comme CI = un incident « carte_identite »).
Ligne `*` = défaut, non supprimable ; édition page `/policies`, effet à l'upload suivant.

Cas automatisé inclus : **type déclaré ≠ type reconnu** — un doc qui ne matche aucun template de sa
catégorie déclarée mais matche un template d'une AUTRE catégorie est non conforme (« attendu
carte_identite, reconnu passeport ») — il suffit qu'un template de l'autre type existe (croissance
organique).

Source de vérité : `ocr_bifunction/governance/conformity_policy.py` (table `ocr_conformity_policies`,
`resolve_conformity_action`) ; application → `api_maquette.py` (`_nonconformity_response`,
`_holder_block_reason`, `_detected_type_mismatch`) et `worker_watchdog.py` (lanes async, sweep
« clore » = purge de la preuve). Prouvé : `conformity_smoke.py` 12/12 (2026-07-12).
Voir [[verdict de routing (auto / humain / invalide)]].

## sévérité par check (durcir / adoucir un contrôle)

Le bouton métier qui règle CE QUE VAUT un échec DÉTERMINÉ d'un check donné : une règle du bloc
`validation.required` peut porter `"severity": "reject"` (échec → document non conforme) ou
`"severity": "review"` (échec → revue humaine), au lieu du défaut du check. Cas nommé par
l'utilisateur (2026-07-12) : une fois le [[registre des organismes (de formation)]] de confiance,
durcir `issuer_registry` — « émetteur ≠ Y → non valide » — sans redéploy, la règle voyage avec le
template (assignable au cochage de la promotion, select « défaut / non conforme / revue »).

**Garde-fou NON négociable (doctrine input-vs-preuve)** : la sévérité ne s'applique qu'aux échecs
DÉTERMINÉS — le check a tourné avec tous ses inputs et la réponse est « non ». Un « je ne peux pas
savoir » (registre absent, date illisible, contexte non câblé) part TOUJOURS en revue, quelle que
soit la config : on ne durcit pas l'ignorance. Une valeur de sévérité inconnue (typo config) fait
elle-même surface en raison de revue — jamais un pass silencieux.

Source de vérité : `ocr_bifunction/extraction/template.py` (`CheckFailure.determined`, override dans
`evaluate_validation`) ; promotion → `api_maquette.py` (cochage tolère `severity`, garde 400) +
`ui/review.html` (selects). Prouvé : `severity_smoke.py` 8/8 (2026-07-12 — durcissement émetteur,
registre vide invincible, adoucissement vocabulary, typo fail-loud, promotion).
Voir [[politique de non-conformité (block / block_holder / flag_and_continue)]] (la RÉACTION par
catégorie ; la sévérité règle la CLASSE par check — les deux se composent).

## capacité de la porte (admission / débordement)

Le plafond de traitements SYNCHRONES simultanés et ce que fait la porte au-delà — la réponse au
« et si j'ai 1000 appels en même temps ? » (analyse 2026-07-12 : sans plafond, ~40 OCR concurrents
sur 4 cœurs = thrashing + OOM à 8 Go ; les clients partent, le serveur brûle du CPU pour rien).
Doctrine : **la porte ne fond jamais, elle dégrade** — le bi-mode est la soupape de pression.

- `SYNC_CONCURRENCY_LIMIT` (défaut 2 sur la machine de référence 4 cœurs/8 Go) — levier VIVANT,
  à monter sur le hardware du jour J, sans redéploy (édition `/policies`, effet à l'upload suivant).
- `SYNC_OVERFLOW_ACTION` : `defer` (défaut) = l'upload excédentaire bascule en asynchrone
  (202 pending, lane `deferred`, drainée par le watchdog — rien n'est perdu) ; `reject_503` =
  refus avec `Retry-After` (rien n'est mis en file chez nous).

Se compose avec la [[politique d'exécution (sync / async immédiat / async nuit)]] : elle dit le
régime VOULU par catégorie ; la capacité dit ce que le hardware PEUT — la saturation dégrade vers
l'async quelle que soit la politique (y compris `carte_identite` verrouillée sync : sous
saturation, il n'y a de temps réel pour personne).

Source de vérité : `ocr_bifunction/governance/capacity_settings.py` (table `ocr_capacity_settings`, patron
leviers) ; admission → `api_maquette.py` (`_try_acquire_sync_slot`, branche débordement de
`validate_document`) ; cache d'idempotence borné (LRU). Prouvé : `load_smoke.py` 10/10
(2026-07-12 — 12 uploads concurrents, pic mesuré ≤ plafond, zéro 5xx, débordement drainé).

## couche intake (traitement d'un document — point d'entrée unique)

« Un document + son contexte → un résultat prêt à persister » — la couche que **les DEUX régimes
traversent** (porte API temps réel ET worker batch), pour ne plus la ré-écrire deux fois. Elle
s'assoit AU-DESSUS du cœur pur `orchestrator.process_document` (qui reste `document → DocumentRecord`,
sans persistance) et ajoute ce qui était dupliqué porte↔worker : le check de type déclaré ≠ type
reconnu ([[politique de non-conformité (block / block_holder / flag_and_continue)]]), la réaction de
non-conformité, et l'unique mapping record→`Job`.

`handle_document` est **PUR** — il ne touche AUCUN store ; il renvoie un `DocumentOutcome` (record,
status, [[verdict de routing (auto / humain / invalide)|verdict]], reasons, retain_bytes,
nonconformity). Ce sont les **adaptateurs** qui persistent : la porte `save` une nouvelle row, le
worker `update_status` la row réclamée — les checkpoints durables (et la reprise sur crash) restent
aux adaptateurs, et le handler est ré-exécutable + testable sur un Store en mémoire. Deux edges
restent DANS les adaptateurs (policy réelle par point d'entrée, pas de la duplication) : la porte
escalade un CI douteux (fast-lane) et done-trace un CI incomplet/inconnu (l'uploader resoumet) ; le
worker, sans uploader à qui renvoyer, route ces cas en `needs_review`.

Source de vérité : `ocr_bifunction/flow/intake.py:160` (`handle_document`), `:222` (`job_from_outcome`) ;
appelée par `api_maquette.py` (`_handle_validated_document`) et `worker_watchdog.py`
(`_process_claimed_job`). Prouvé : `handler_check.py` 6/6 (isolé, Store `:memory:`). Refactor
candidat B, 2026-07-13.

## arête « sens » : structure vs intégrité-caractères (l'encodage born-digital)

L'arête SENS de la validation de conversion (la 3e, après complétude et forme) se **scinde en deux
sous-arêtes de nature différente** (confirmé utilisateur 2026-07-20) :

- **structure / ordre de lecture** — titres, paragraphes, cellules de table, linéarisation. Un
  extracteur peut mal linéariser un multi-colonnes ou une table → tokens dans le mauvais ordre → sens
  corrompu SANS qu'un caractère soit faux. **Corroborable en principe** par un 2e lecteur indépendant.
  **Sur les TABLES, la corroboration AUTOMATIQUE a été tentée puis ABANDONNÉE (2026-07-21)** : deux
  reconstructions réellement indépendantes existent bien (`pdfplumber` **géométrique** vs TableFormer
  **neural** de Docling), mais comparer leur **FORME** (lignes×colonnes) diverge sur **100 %** des
  documents réels — les deux lecteurs ne désaccordent pas sur la QUALITÉ, ils appliquent des
  conventions de **segmentation** différentes (« qu'est-ce qu'UNE table »). Un détecteur qui se
  déclenche toujours ne détecte rien. **La voie retenue est l'ARBITRAGE HUMAIN** : la vérité n'est
  pas dérivable de deux extracteurs qui se contredisent, donc on présente à l'humain l'image de la
  page à côté des deux reconstructions et **c'est lui qui tranche** ; les deux extractions restent
  retenues comme preuve (doctrine `extractor`/`superseded_by`, cf. domaine 8 du contrat BD).
  ⚠️ **Ce qui n'est PAS couvert, assumé et nommé** : la **linéarisation** (écartée, ROI nul — si on y
  revient, la métrique devra être **sensible à l'ordre** : un TF-IDF score ~1.0 par construction,
  les deux lecteurs tirant les mêmes mots de la même couche texte) et la **hiérarchie**, qui n'a
  **aucun second avis possible** (markitdown ne produit aucun titre : 0 sur 24 PDF réels).
  Source : `table_adjudication_build.py` (la fenêtre d'arbitrage : image de page + les deux
  reconstructions, HTML local **gitignoré car PII**). Le module de corroboration automatique
  (`table_corroboration.py`) a été **supprimé** après invalidation — voir HANDOFF pour la leçon
  (un smoke vert 6/6 qui figeait l'hypothèse de conception).
- **intégrité-caractères** — les caractères eux-mêmes sont-ils les bons ? En born-digital, le texte
  vient de la couche programmatique du PDF via sa table `ToUnicode` (CMap). **Table absente/cassée
  (police sous-ensemble) → mojibake** (`Ã©`, `â€™`…) alors que le doc est parfaitement natif. **NON
  corroborable** : Docling, markitdown, PyMuPDF font TOUS confiance à la MÊME CMap → même faux →
  **faux accord** (« corroboré » à tort). C'est une propriété de la SOURCE, héritée à l'identique par
  tout extracteur du même type — changer de modèle n'y change rien.

Donc l'intégrité-caractères exige un **garde de plausibilité model-agnostique** : un test intrinsèque
sur le texte extrait (quel qu'en soit le producteur), placé AU-DESSUS du slot lecteur. Signaux
(vérifiés Context7 2026-07-20) : compte de `U+FFFD` (perte déjà consommée → flag dur, irréparable) ;
`ftfy.badness.is_bad` / `badness()` (heuristique mojibake, sans réparer, false-positive-safe sur du
FR propre) ; `ftfy.fix_and_explain()` → `(fixed, explanation)` (répare + explique ce qu'il a changé →
réparation en SUGGESTION, l'humain valide, doctrine [[passe DRAFT (drafting câblé au flux)]] «
propose/dispose ») ; ratio « script attendu » (Latin + ponctuation FR).

**LIVRÉ + CÂBLÉ (logique 2026-07-20, câblage 2026-07-21)** — actif de la lecture au verdict.
Deux classes séparées, mesurées sur `ftfy` 6.3.1 : **perte irréversible** (`U+FFFD`
présent → flag dur, aucun repair) et **mojibake réparable** (`is_bad` + `fix_and_explain` renverse et
explique les octets → `repaired_text` en suggestion, humain valide). Finding verrouillé :
`ftfy.badness.is_bad` renvoie **False** sur du `U+FFFD` → le check U+FFFD n'est PAS redondant, il
attrape une classe que l'heuristique mojibake laisse passer.

**Règle porteuse du câblage** : un texte non-clean **escalade AUTO → REVIEW** (un document peut matcher
son template et passer tous ses checks alors que les CARACTÈRES dont les champs sont extraits sont du
mojibake — l'arête est ORTHOGONALE aux checks) ; un **REJECT n'est jamais adouci** ; le `repaired_text`
est offert en **SUGGESTION**, jamais appliqué (après détection, c'est l'humain qui tranche).

Source : `ocr_bifunction/reading/text_integrity_guard.py` (`assess_text_integrity`, `TextIntegrityAssessment`,
dispositions `clean`/`repairable_mojibake`/`irreversible_loss`/`suspect_encoding`) ; câblage
`ocr_bifunction/reading/reader.py` (`ReadResult.text_integrity`, calcul à un seam unique dans `read_document` —
tous backends confondus) et `ocr_bifunction/flow/router.py` (`apply_text_integrity_signal`, appliqué aux 2
lanes). Prouvé `text_integrity_guard_smoke.py` 5/5 (logique, chaînes fabriquées
`encode("utf-8").decode("latin-1")`) + `text_integrity_wiring_smoke.py` 8/8 (bout-en-bout via un PDF
synthétique dont la couche texte PORTE le mojibake — la cause de la corruption est indifférente au
garde, donc aucun PDF à CMap cassée n'est nécessaire pour prouver le chemin). Complétude/forme
déjà ancrées `ocr_bifunction/reading/conversion_guard.py` (`assess_page_coverage`, `low_layout_pages`) et
`ocr_bifunction/reading/docling_page_range_converter.py` (`confidence.pages` = produced ; `layout_score`).
Confirmation utilisateur 2026-07-20 ; `ftfy` vérifié Context7 + install 2026-07-20.
Voir [[verdict de routing (auto / humain / invalide)]].

## lane de lecture (born-digital / officiel scanné / scanné « tout le reste »)

Le **discriminant d'escalade**, acté le 2026-07-23. Ce n'est PAS un signal de qualité de lecture —
c'est une propriété du SUPPORT, décidée mécaniquement **page par page**, avant toute estimation :

- **born-digital** — la page porte sa couche texte, la lecture est exacte, rien à escalader. Test
  déjà en place : `TEXT_LAYER_MINIMUM_CHARACTERS` (10) plus la garde de dominance image
  (`IMAGE_DOMINANT_COVERAGE_PERCENT` 80 %, `IMAGE_DOMINANT_MAXIMUM_CHARACTERS` 600) qui rattrape la
  page « photo pleine page + légende ».
- **document officiel scanné** — CI, titre d'habilitation, passeport : ces types **ont un template**
  et, avec lui, un **oracle mécanique** (les 4 checksums ICAO de la MRZ). Ils suivent la cascade
  existante `raw -> enhance -> escalation`, qui garde la lecture passant le PLUS de check digits et
  reste sur l'étage le moins cher à égalité.
- **scanné, tout le reste** — SOP, contrats, formulaires : **pas de template**, donc rien à déclarer
  et aucun oracle disponible. Les **deux moteurs tournent d'office** (rapide + VLM), en asynchrone,
  puis l'humain tranche.

**Pourquoi la 3e branche ne cherche plus de déclencheur** : trois candidats ont été réfutés par la
mesure le 2026-07-23 (confiance OCR moyenne — elle classe le scan dur AU-DESSUS d'un CI valide ;
densité de texte ; encre orpheline et géométrie des boîtes). Le moteur rapide **détecte** le
manuscrit et le **transcrit faux**, ce qui rend aveugle tout signal fondé sur la détection. Poser le
routage sur `born-digital ?` supprime le besoin d'un déclencheur au lieu de le résoudre.

**Le coût est un levier, pas une constante** : mesuré sur le corpus dur (7 documents, 165 pages),
153 pages sont born-digital et 12 scannées — les contrats et SOP n'atteignent jamais le VLM, et la
règle coûte ~1,5 h de nuit. Un fonds d'archives scannées inverserait ce ratio : la politique est donc
**configurable en table** (défaut en constante Python + override runtime), patron des surfaces
existantes ([[politique d'exécution (sync / async immédiat / async nuit)]],
[[capacité de la porte (admission / débordement)]]).

Source de vérité : `ocr_bifunction/reading/reader.py:42` / `:54` / `:385` (les portes born-digital),
`ocr_bifunction/flow/pipeline.py:90` (`read_verso_mrz`, la cascade officielle),
`ocr_bifunction/flow/router.py:75` (`route_document`, le partage des lanes). Décision utilisateur
2026-07-23 ; mesures dans HANDOFF (section du 2026-07-23). La forme de la surface de config et la
reconstruction du record à partir de DEUX lectures sont des chantiers SÉPARÉS, non engagés.
Voir [[verdict de routing (auto / humain / invalide)]], [[couche intake (traitement d'un document — point d'entrée unique)]].
