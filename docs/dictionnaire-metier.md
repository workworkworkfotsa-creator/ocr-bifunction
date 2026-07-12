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

Source de vérité : `ocr_bifunction/template.py` (`class CheckFailure`, `class ValidationOutcome`,
`evaluate_validation`) pour la lane structurée ; `ocr_bifunction/reconcile.py` (verdict 3-états) pour
CI/MRZ ; `STATUS_REJECTED` (`repository.py`), mapping `router`/`orchestrator`/`api_maquette`/`batch_check`.
Confirmation utilisateur 2026-07-03. Prouvé : `verdict_check.py` (11/11), `verdict_flow_check.py` (7/7,
bout-en-bout), `reconcile_verdict_check.py` (5/5). **Câblé de bout en bout** (structuré + CI/MRZ).

## politique d'exécution (sync / async immédiat / async nuit)

Le « QUAND traiter » d'un document, découplé du « QUOI » (demande utilisateur 2026-07-08 : les infra
et les besoins changent → le mapping catégorie→régime doit être une **config opérée**, pas du code).
Trois modes :

- **sync** — dans la requête HTTP (secondes ; moteurs classe RapidOCR).
- **async_immediate** — spool + ligne D1 `received` en lane `deferred`, drainée par le watchdog
  qui tourne en continu (minutes).
- **async_nightly** — même mécanique en lane `nightly`, drainée UNIQUEMENT par la passe de nuit
  (`worker_watchdog.py --once --nightly` = le cron IT).

Résolution à la porte : la ligne de la catégorie gagne, sinon la ligne `*` (défaut, non supprimable).
Le client de l'API peut envoyer un `processing_mode` optionnel — honoré **seulement** si la politique
de la catégorie dit `override_allowed` (cohabitation : `carte_identite` verrouillée sync — son doute
escalade par son propre chemin — pendant qu'une facture peut être poussée en nuit). Hint ignoré ou
mode async → tracé dans `reasons`. Défauts dans le code, seed qui n'écrase jamais une édition
opérateur, édition via la page `/policies` — effet à l'upload suivant, zéro redéploy.

Source de vérité : `ocr_bifunction/execution_policy.py` (`EXECUTION_MODES`, `resolve_execution`,
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

Source de vérité : `ocr_bifunction/issuer_registry.py` (table `ocr_issuer_registry`) ; check
`issuer_registry` → `ocr_bifunction/template.py` (`_check_issuer_registry`) ; contexte câblé →
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

Source de vérité : `ocr_bifunction/drafting_flow.py` (`run_draft_pass`),
`ocr_bifunction/drafting.py` (`seed_candidate_checks`), `worker_watchdog.py` (`--nightly`,
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
(`ocr_bifunction/repository.py`) ; check → `ocr_bifunction/template.py`
(`_check_reconcile_ci`) ; normalisation stricte → `ocr_bifunction/reconcile.py`.
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

Source de vérité : `ocr_bifunction/context_assembly.py` (`ATTESTATION_REFERENCE_ROLES_KEY`,
`collect_validated_attestations`) ; colonne D2 `reference_roles_json`
(`ocr_bifunction/template_repository.py`) ; assignation → `api_maquette.py`
(`ValidateSuggestionRequest.reference_roles`, gardes 400) + `ui/review.html` ; check →
`ocr_bifunction/template.py` (`_check_corroborated_by`). Prouvé : `corroboration_smoke.py` 7/7
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

Source de vérité : `ocr_bifunction/conformity_policy.py` (table `ocr_conformity_policies`,
`resolve_conformity_action`) ; application → `api_maquette.py` (`_nonconformity_response`,
`_holder_block_reason`, `_detected_type_mismatch`) et `worker_watchdog.py` (lanes async, sweep
« clore » = purge de la preuve). Prouvé : `conformity_smoke.py` 12/12 (2026-07-12).
Voir [[verdict de routing (auto / humain / invalide)]].
