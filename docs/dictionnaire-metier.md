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
  terminal, PAS de revue humaine**. Checks concernés (`REJECTING_CHECKS`) : `date_order`, `date_span`
  (dates incohérentes/rallongées au stylo), `vocabulary` (code inventé), `reconcile_ci` (MRZ recto≠verso,
  ou nom titulaire ≠ record CI — la fraude fratrie Ahmed≠Hamed).

Priorité : **reject > review > auto** (un doc prouvé invalide n'est pas adouci en « à revoir » parce
qu'il porte aussi un check en attente).
Source de vérité : `ocr_bifunction/template.py` (`REJECTING_CHECKS`, `class ValidationOutcome`,
`evaluate_validation`) ; confirmation utilisateur 2026-07-03. Prouvé : `verdict_check.py` (8/8).
**Reste à câbler** dans le flux/pipeline (statut terminal `rejected` en D1, mapping API/batch) — le
classifieur existe, le routage aval pas encore.
