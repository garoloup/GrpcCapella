# Import / Export .proto <-> Capella

*[English version](README.md)*

Outils en ligne de commande pour importer des services gRPC (fichiers
`.proto`, y compris multi-fichiers avec imports croisés) dans un modèle
Capella (Interfaces, Classes, Enumerations, Services, Parameters), et
pour exporter des Interfaces Capella vers des fichiers `.proto`.

Fonctionne entièrement en dehors de Capella (pas de plug-in, pas de
Python4Capella) : les scripts ouvrent et modifient directement le
fichier `.aird`/XMI du modèle via la bibliothèque `capellambse`.


## 1. Installation

Environnement virtuel dédié recommandé (`uv` ou `venv` classique) :

```bash
uv venv capella-grpc-env
source capella-grpc-env/bin/activate      # macOS/Linux
# capella-grpc-env\Scripts\activate       # Windows

uv pip install grpcio-tools capellambse
```

Ou avec `pip` :

```bash
python3 -m venv capella-grpc-env
source capella-grpc-env/bin/activate
pip install grpcio-tools capellambse
```

Pour figer les versions (reproductibilité pour l'équipe) :

```bash
uv pip freeze > requirements.txt     # puis : uv pip install -r requirements.txt
```


## 2. Fichiers du projet

| Fichier | Rôle |
|---|---|
| `proto_capella_types.py` | Mapping partagé types proto ↔ DataType Capella, et toutes les clés PVMT (streaming, package, fichier d'origine). **À adapter une seule fois** aux noms réels de votre projet (voir §6). |
| `import_proto_to_capella.py` | Importe un `.proto` (et ses imports transitifs) vers un modèle Capella. |
| `export_capella_to_proto.py` | Exporte une Interface Capella vers un `.proto`. |
| `verify_import.py` | Relit un modèle après import et affiche ce qui a été créé, pour vérification rapide. |
| `list_datatypes.py` | Liste les `DataType` existants dans le `DataPkg` d'une couche, pour renseigner `proto_capella_types.py`. |
| `setup_primitive_types.py` | Crée les 15 `DataType` primitifs manquants (`Int32`/`UInt64`/`String`/...) dans une couche, si absents. Idempotent. Utilisable seul, ou appelé automatiquement par l'import (§6). |

Gardez tous les fichiers `.py` dans le même dossier : les scripts
s'importent mutuellement (`from proto_capella_types import ...`).


## 3. Usage — Import : `.proto` → Capella

```bash
python import_proto_to_capella.py mon_service.proto Mon_Modele.aird \
    [--layer=la] [--proto-root=DOSSIER] [--strict-types]
```

- `--layer` : couche cible, `oa` / `sa` / `la` (défaut) / `pa` —
  respectivement Operational Analysis, System Analysis, Logical
  Architecture, Physical Architecture. Pour des interfaces logicielles
  gRPC, `la` ou `pa` sont les choix pertinents (pas `oa`, qui sert au
  besoin métier).
- `--proto-root` : dossier racine par rapport auquel vos directives
  `import` **relatives et imbriquées** se résolvent (ex : si vos
  fichiers font `import "service_base_api/ServiceB.proto";`,
  `--proto-root` doit être le dossier qui **contient**
  `service_base_api/`, pas ce dossier lui-même). Indispensable dès que
  votre `.proto` importe un autre fichier situé dans un dossier
  différent du sien — voir §5 pour le détail.
- `--strict-types` : n'auto-crée pas les `DataType` primitifs
  manquants ; arrête l'import si l'un d'eux est absent (voir §6 —
  par défaut, auto-création silencieuse).

Ce que le script traite, dans **tous** les fichiers rencontrés (le
fichier cible et tous ses imports transitifs, y compris les
"well-known types" Google comme `google/protobuf/empty.proto`) :

- Un `message` → une `Class`, avec ses champs en `Property`.
- Un `enum` de premier niveau → une `Enumeration` Capella, avec ses
  valeurs en `EnumerationLiteral` (pas les enums imbriqués dans un
  message — limite connue, §9).
- Un `service` → une `Interface`, avec un `Service` (opération) par
  `rpc` et ses `Parameter` d'entrée/sortie typés.
- Les commentaires `.proto` (au-dessus de chaque
  message/champ/enum/service/rpc) → la `description` Capella
  correspondante.
- Le mode de streaming (`stream` sur la requête et/ou la réponse) →
  PVMT (voir §7).
- L'organisation en dossiers de vos fichiers `.proto` → une hiérarchie
  de sous-packages Capella miroir (voir §5).

**Idempotent** : relancer l'import sur le même `.proto`, une version
modifiée, ou même un fichier différent qui importe le même module,
ne duplique jamais les éléments déjà présents — voir §8.

**Pré-requis dans le modèle**, dans la couche visée : un `DataPkg` et
un `InterfacePkg` existants (créés par défaut par Capella), ainsi que
le PVMT du streaming (§7, toujours manuel — pas d'auto-création
possible pour PVMT, contrairement aux types primitifs, §6).

Pour vérifier ce qui a été créé sans repasser par l'interface Capella :

```bash
python verify_import.py Mon_Modele.aird NomDuService
```


## 4. Usage — Export : Capella → `.proto`

```bash
python export_capella_to_proto.py Mon_Modele.aird NomDuService sortie.proto \
    [--layer=la] [--order=messages-first] [--package=mon.pkg]
```

ou, pour régénérer automatiquement l'arborescence de dossiers (voir §5) :

```bash
python export_capella_to_proto.py Mon_Modele.aird NomDuService \
    --output-root=protos [--layer=la] [--order=messages-first] [--package=mon.pkg]
```

`sortie.proto` (chemin exact) et `--output-root` (dossier racine,
arborescence régénérée automatiquement dessous) sont **mutuellement
exclusifs** — donnez l'un ou l'autre, jamais les deux, jamais aucun
des deux.

- `--layer` : optionnel, sert à lever l'ambiguïté si une Interface du
  même nom existe dans plusieurs couches (le script s'arrête avec une
  erreur explicite dans ce cas si `--layer` n'est pas précisé).
- `--order` : `messages-first` (défaut, convention gRPC officielle —
  les types détaillés d'abord, le service en dernier) ou
  `service-first` (le service en tête du fichier, convention utilisée
  par certaines équipes). Les deux ordres produisent un `.proto`
  valide et recompilable.
- `--package` : force le `package` proto écrit en tête de fichier.
  Sans cet argument, l'export tente de le lire automatiquement depuis
  le PVMT de l'Interface (§7) ; si absent des deux côtés, aucune ligne
  `package` n'est écrite. Un `--package` explicite est toujours
  prioritaire sur le PVMT.

Régénère : `syntax = "proto3";`, les `import` nécessaires (well-known
types Google détectés automatiquement, voir §5), le `package`
optionnel, les `enum`/`message` référencés (transitivement, y compris
à travers les champs des messages eux-mêmes), puis le `service` avec
ses `rpc` (streaming recalculé depuis PVMT), et les commentaires
depuis les `description` Capella.


## 5. Organisation en packages Capella (miroir des dossiers proto)

Un package Capella (`DataPkg`/`InterfacePkg` imbriqué) correspond à un
**dossier** de vos fichiers `.proto` — pas à un fichier individuel, ni
à la déclaration `package X.Y;` à l'intérieur du fichier (ce sont deux
informations distinctes en Protocol Buffers, qui coïncident par
convention dans la plupart des projets bien organisés, mais que le
script ne confond jamais : c'est le **chemin de fichier réel** qui
décide). Deux fichiers `.proto` dans le même dossier partagent le même
package Capella ; leurs types se résolvent entre eux même si vous
n'importez directement qu'un seul des deux fichiers (résolution
globale, tous fichiers transitivement importés confondus).

**`--proto-root` est indispensable** dès que vos imports sont en
chemin relatif imbriqué. Exemple concret :

```
protos/
  service_base_api/
    ServiceA.proto   # import "service_base_api/ServiceB.proto";
    ServiceB.proto
```

```bash
python import_proto_to_capella.py protos/service_base_api/ServiceA.proto Model.aird \
    --proto-root=protos
```

`--proto-root=protos` doit être le dossier qui **contient**
`service_base_api/` — pas `service_base_api/` lui-même. Sans cet
argument, `protoc` ne peut résoudre les imports que par rapport au
dossier direct du fichier cible, ce qui casse la résolution dès que la
structure a plus d'un niveau (et fait atterrir tous les éléments à la
racine du `DataPkg`/`InterfacePkg`, sans sous-package).

**Well-known types Google** (`google/protobuf/empty.proto`,
`timestamp.proto`, `duration.proto`, `struct.proto`, `any.proto`,
`field_mask.proto`, `wrappers.proto`) sont traités comme n'importe
quel autre type importé : ils sont créés comme de vraies `Class`
Capella dans un package `google/protobuf`, avec leurs vrais champs.
`google.protobuf.Empty` n'est **pas** un cas spécial ("pas de
paramètre") : c'est juste une `Class` avec zéro champ, comme les
autres. À l'export, ces types précis sont reconnus automatiquement (en
détectant leur package Capella `google/protobuf`) et référencés via un
vrai `import "google/protobuf/xxx.proto";`, jamais redéfinis en double.

**Avertissement de divergence** : si le dossier réel d'un fichier ne
correspond pas à sa déclaration `package X.Y;` interne (dossier
attendu = le nom du package avec les points remplacés par des `/`),
l'import affiche un `ATTENTION` explicite — informatif, jamais
bloquant. C'est souvent le signe d'un fichier importé "à plat" (sans
son dossier d'origine) par erreur.

**`--output-root` à l'export** régénère l'arborescence de dossiers en
sens inverse, sous ce dossier racine :

```bash
python export_capella_to_proto.py Model.aird ServiceA --output-root=protos --layer=pa
# -> protos/service_base_api/ServiceA.proto
```

Le nom de fichier utilise, par priorité : le chemin exact d'origine
s'il a été mémorisé via PVMT (`SourceFile`, §7), sinon
`<NomInterface>.proto` dans le dossier miroir du package Capella
(approximatif si le nom du fichier `.proto` d'origine différait du nom
du service qu'il contient).

**Limite connue** : une référence croisée vers un type **custom** (pas
Google) d'un *autre* package Capella que celui de l'Interface exportée
est, pour l'instant, toujours **redéfinie en ligne** dans le fichier
de sortie plutôt que référencée via un `import` précis vers son
fichier d'origine exact — même si ce fichier d'origine est
potentiellement connu via `SourceFile` (PVMT), ce mécanisme n'est pas
encore exploité pour régénérer un vrai `import` inter-fichiers
personnalisé. Le fichier généré reste valide et autonome (il se suffit
à lui-même), simplement pas éclaté en plusieurs fichiers comme
l'original pourrait l'être.


## 6. Réglage des types primitifs

`proto_capella_types.py` contient le mapping **bijectif** (1 type
proto ↔ 1 type Capella) entre types primitifs `.proto` (`string`,
`int32`, `uint64`, `sint32`...) et noms de `DataType` Capella
(`String`, `Int32`, `UInt64`, `SInt32`...). Chaque largeur/signe a son
propre type Capella — pas de fusion en un seul `Integer` générique,
pour ne pas perdre d'information au round-trip.

**Par défaut, les `DataType` primitifs manquants sont créés
automatiquement à l'import**, dans le `DataPkg` **racine** de la
couche ciblée (jamais dans les sous-packages par dossier — ce sont des
types partagés, pas spécifiques à un module proto). Idempotent : ne
duplique jamais un type déjà présent. Le script affiche ce qui a été
créé :

```
INFO : DataTypes primitifs crees automatiquement : Int32, String
```

Pour trouver les types créés dans Capella : Project Explorer →
`[votre couche]` → `Data` → vous les verrez directement là, au fur et
à mesure qu'ils sont utilisés.

Pour désactiver l'auto-création et forcer un contrôle strict (le
script s'arrête si un type nécessaire manque, sans rien créer) :

```bash
python import_proto_to_capella.py mon_service.proto Mon_Modele.aird --strict-types
```

Vous pouvez aussi préparer un modèle à l'avance, sans lancer d'import,
avec le script autonome :

```bash
python list_datatypes.py Mon_Modele.aird             # voir ce qui existe deja
python setup_primitive_types.py Mon_Modele.aird --layer=la   # creer les 15 types
```

Si votre projet utilise déjà des types primitifs sous d'autres noms,
inutile d'auto-créer : ajustez plutôt les valeurs dans
`PROTO_TO_CAPELLA_PRIMITIVE` (dans `proto_capella_types.py`) pour
qu'elles correspondent aux noms existants. Ce fichier est partagé par
l'import et l'export : une seule modification suffit pour les deux
sens.


## 7. Procédure PVMT — à faire une fois par projet

Trois informations optionnelles-mais-utiles passent par l'extension
PVMT de Capella : le mode de streaming gRPC (§7.1, la seule des trois
qui bloque l'import si absente), le `package` proto d'origine (§7.2),
et le chemin de fichier d'origine exact (§7.3). Aucune ne peut être
créée par script : ni Python4Capella ni `capellambse` ne le permettent
(limite volontaire des deux outils) — c'est une configuration manuelle
à faire une fois dans Capella, via le **PV Definition Editor**
(sélectionnez un élément du modèle, ouvrez la vue **Property Values**
— `Window > Show View > Other... > Property Values` — puis le PV
Definition Editor depuis cette vue).

### 7.1 Streaming (obligatoire pour un import complet)

Une **seule propriété d'énumération**, pas deux booléens séparés —
modélise directement les 4 modes gRPC possibles :

| Niveau | Nom | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Enumeration type (dans le Domain) | `grpc_streaming_mode` | 4 littéraux : `UNARY`, `CLIENT_STREAMING`, `SERVER_STREAMING`, `BIDIR_STREAMING` |
| Group (dans le Domain) | `GrpcMethod` | — |
| Property (dans le Group) | `streaming_mode` | type `grpc_streaming_mode` |

Scope du Group sur la ou les couches où vivent vos interfaces
(Logical/Physical — pas "Operational", qui désigne la couche
Operational Analysis, sans rapport avec les opérations d'interface).

**Sans cette structure**, l'import s'arrête **avant toute
modification** avec un message explicite listant exactement ce qui
manque — contrôle préalable automatique, pas d'import partiel silencieux.

Les noms `Grpc`, `GrpcMethod`, `streaming_mode`, `grpc_streaming_mode`
et les 4 littéraux sont ceux utilisés par défaut dans
`proto_capella_types.py` (`PVMT_STREAMING_*`, `STREAMING_FLAGS_TO_MODE`).
Si vous utilisez d'autres noms, adaptez ces constantes en conséquence.

### 7.2 Package d'origine (optionnel)

| Niveau | Nom | Type |
|---|---|---|
| Domain | `Grpc` (le même que ci-dessus) | — |
| Group (nouveau, dans le Domain) | `Metadata` | — |
| Property (dans Metadata) | `Package` | String |

Si présente, le `package` proto d'origine (ex : `soba_template_api`)
est stocké automatiquement sur chaque Interface à l'import, et relu
automatiquement à l'export (repli sur `--package` explicite si absent
d'un côté ou de l'autre). **Absente, elle n'empêche pas l'import** —
contrairement au streaming, c'est une commodité, pas un pré-requis.

### 7.3 Chemin de fichier d'origine (optionnel)

| Niveau | Nom | Type |
|---|---|---|
| Domain | `Grpc` | — |
| Group | `Metadata` (le même que ci-dessus) | — |
| Property (dans Metadata) | `SourceFile` | String |

Si présente, mémorise le chemin exact du fichier `.proto` d'origine
(ex : `service_base_api/ServiceB.proto`) sur chaque `Class`,
`Enumeration` et `Interface` créée. Utilisée par `--output-root` à
l'export (§4, §5) pour régénérer l'arborescence avec les noms de
fichiers exacts plutôt qu'un nom approximatif basé sur le nom de
l'élément Capella. Absente, `--output-root` retombe sur
`<dossier>/<NomInterface>.proto`.


## 8. Ré-import : mise à jour vs duplication

L'import est **idempotent**, par nom, dans chaque package Capella
concerné : relancer sur le même `.proto` (identique, modifié, ou même
un autre fichier qui l'importe transitivement) ne crée jamais de
doublon. Pour chaque Class, Property, Enumeration, Interface, Service
et Parameter, le script cherche d'abord un élément existant du même
nom au même endroit ; s'il le trouve, il le met à jour (description,
type) ; sinon il le crée.

| Situation | Comportement |
|---|---|
| Ré-import du même fichier | Aucune duplication, tout est simplement mis à jour. |
| Version modifiée avec ajout (nouveau champ/méthode/message/enum) | Le nouvel élément est ajouté aux existants. |
| Version modifiée avec suppression | L'élément Capella devenu orphelin (absent du nouveau `.proto`) est **signalé** (`INFO : ... non supprimes`) mais **jamais supprimé automatiquement** — une suppression auto pourrait casser une référence ailleurs dans le modèle (un diagramme, par exemple). À vous de le retirer manuellement si besoin. |
| Import d'un fichier B qui était déjà importé transitivement (via un fichier A) | Retrouve les éléments déjà créés dans le bon package, ne duplique rien — peu importe par quel fichier vous « entrez » dans un module proto, le résultat final converge. |

Le script signale aussi, à titre indicatif, les Classes d'un package
qui ne sont liées à aucun des fichiers traités dans cet import
(contenu préexistant sans rapport) — normal si votre `DataPkg` (ou un
sous-package) contient d'autres éléments.


## 9. Limites connues

- **`repeated`** : l'export détermine `repeated` à partir de la
  cardinalité (`max_card`) de la `Property` Capella. L'import ne pose
  pas cette cardinalité explicitement — un champ `repeated` importé
  puis ré-exporté ressortira donc comme simple.
- **Enums imbriqués** : seuls les `enum` déclarés au niveau du fichier
  sont gérés, pas ceux imbriqués dans un `message`.
- **Valeurs numériques des enums** : Capella ne stocke pas de valeur
  explicite par littéral (pas de champ `value` sur
  `EnumerationLiteral`) — régénérées séquentiellement depuis 0 à
  l'export. Fidèle pour un enum proto3 standard (cas courant), pas
  pour une numérotation personnalisée ou avec des trous.
- **Imports cross-package personnalisés** : voir la limite détaillée
  en §5 (types custom redéfinis en ligne plutôt que via un `import`
  précis vers leur fichier d'origine).
- **`oneof`, `map<>`** : non gérés par les scripts actuels.