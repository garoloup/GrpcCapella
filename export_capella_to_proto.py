# -*- coding: utf-8 -*-
"""
Export Capella -> .proto, via capellambse (lecture seule, pur Python 3,
sans Capella ni Java lances).

Usage :
    python3 export_capella_to_proto.py Model.aird InterfaceName sortie.proto [--layer=la] [--order=messages-first] [--package=mon.pkg]
    python3 export_capella_to_proto.py Model.aird InterfaceName --output-root=protos [--layer=la] [--order=messages-first]
    python3 export_capella_to_proto.py Model.aird --all --output-root=protos [--layer=la] [--order=messages-first]
        (mode arborescence : exporte TOUTES les Interfaces trouvees,
        symetrique du mode arborescence de l'import)

--layer sert a lever l'ambiguite si plusieurs Interfaces portent le
meme nom dans des couches differentes (mode simple), ou a restreindre
--all a une seule couche.

--output-root : regenere l'arborescence de dossiers (miroir des
packages Capella, cf. compute_output_path) sous ce dossier racine.
Incompatible avec un chemin de sortie explicite.

--package : le .proto d'origine a un "package" (ex: soba_template_api).
Si l'import a pu l'ecrire via PVMT (optionnel, cf. proto_capella_types.py
-- Grpc.Metadata.Package), l'export le relit automatiquement, sans rien
a faire ici. Sinon (PVMT absent au moment de l'import), passez-le
explicitement avec --package ; un --package explicite est de toute
facon toujours prioritaire sur le PVMT.

Cross-references entre fichiers d'un meme package Capella : si le PVMT
SourceFile (optionnel, cf. proto_capella_types.py) est configure, un
type reference mais defini dans un AUTRE fichier .proto est correctement
importe via un vrai "import", pas redefini en double -- important en
mode --all, ou plusieurs fichiers exportes ensemble doivent rester
compilables comme un tout cohérent, pas seulement chacun isolement.
Sans SourceFile, repli sur l'ancien comportement (inline systematique).

LIMITES CONNUES (fidelite du round-trip) :
- "repeated" se base sur prop.max_card. Si vos Classes ont ete
  importees par notre script d'import et que celui-ci n'a pas pose
  min_card/max_card explicitement, tous les champs ressortiront ici
  comme non-repeated.
- Les valeurs numeriques explicites des enums (ex: THRESHOLD = 1) ne
  sont pas stockees par Capella (pas de champ "value" sur
  EnumerationLiteral) -- elles sont regenerees sequentiellement a
  partir de 0, dans l'ordre des litteraux. Fidele pour un enum proto3
  standard (cas courant), pas pour un enum a numerotation custom/avec
  trous.
- Seuls les enums de premier niveau sont geres (pas les enums imbriques
  dans un message).
"""

import sys
import os
import posixpath
import argparse
import html
import capellambse
import proto_comments

from proto_capella_types import (
    pvmt_get,
    CAPELLA_TO_PROTO_PRIMITIVE,
    PVMT_STREAMING_MODE_KEY,
    STREAMING_MODE_TO_FLAGS,
    PVMT_PACKAGE_KEY,
    PVMT_SOURCE_FILE_KEY,
    PVMT_HEADER_KEY,
    PVMT_COMMENT_STYLE_KEY,
    PVMT_ONEOF_KEY,
    PVMT_FIELD_NUMBER_KEY,
)

LAYER_CHOICES = {"oa": "Operational Analysis", "sa": "System Analysis",
                  "la": "Logical Architecture", "pa": "Physical Architecture"}

# Types Google "well-known" geres avec un vrai "import", pas redefinis
# inline -- nom Capella (= nom proto court) -> nom de fichier .proto
# standard sous google/protobuf/. Uniquement reconnus s'ils vivent
# EFFECTIVEMENT dans un package Capella 'google/protobuf' (cf.
# get_capella_type_folder) -- un Empty personnel ailleurs ne serait pas
# traite comme le well-known type.
WELL_KNOWN_GOOGLE_FOLDER = "google/protobuf"
WELL_KNOWN_GOOGLE_FILES = {
    "Empty": "empty", "Timestamp": "timestamp", "Duration": "duration",
    "Struct": "struct", "Value": "struct", "ListValue": "struct",
    "Any": "any", "FieldMask": "field_mask",
    "BoolValue": "wrappers", "Int32Value": "wrappers", "Int64Value": "wrappers",
    "UInt32Value": "wrappers", "UInt64Value": "wrappers", "FloatValue": "wrappers",
    "DoubleValue": "wrappers", "StringValue": "wrappers", "BytesValue": "wrappers",
}


def get_capella_type_folder(container_pkg):
    """Reconstruit le chemin de dossier proto ('google/protobuf',
    'service_base_api'...) a partir du package Capella conteneur d'un
    type, en remontant les DataPkg/InterfacePkg imbriques -- symetrique
    de get_or_create_package_path() cote import. Le DataPkg/InterfacePkg
    RACINE (celui de la couche) est exclu du chemin, detecte par le
    fait que SON parent n'est plus un DataPkg/InterfacePkg (c'est la
    Layer elle-meme)."""
    names = []
    pkg = container_pkg
    while type(pkg).__name__ in ("DataPkg", "InterfacePkg") and \
          type(pkg.parent).__name__ in ("DataPkg", "InterfacePkg"):
        names.insert(0, pkg.name)
        pkg = pkg.parent
    return "/".join(names)


def is_well_known_google_type(capella_type):
    return (capella_type is not None
            and capella_type.name in WELL_KNOWN_GOOGLE_FILES
            and get_capella_type_folder(capella_type.parent) == WELL_KNOWN_GOOGLE_FOLDER)


def proto_type_name(capella_type, needed_imports=None):
    """needed_imports : set mutable, alimente avec le chemin
    'google/protobuf/xxx.proto' si capella_type est un well-known type
    Google -- l'appelant s'en sert pour ecrire les 'import' necessaires."""
    if capella_type is None:
        return None  # type reellement inconnu
    if is_well_known_google_type(capella_type):
        if needed_imports is not None:
            file_stem = WELL_KNOWN_GOOGLE_FILES[capella_type.name]
            needed_imports.add(f"google/protobuf/{file_stem}.proto")
        return f"google.protobuf.{capella_type.name}"
    return CAPELLA_TO_PROTO_PRIMITIVE.get(capella_type.name, capella_type.name)


def is_repeated(prop):
    return prop.max_card is not None and prop.max_card.value not in ("0", "1")


def get_comment_style(element):
    """Style de commentaire d'origine (PVMT CommentStyle, cf.
    proto_comments.py), "//" par defaut si absent/non configure."""
    try:
        style = pvmt_get(element, PVMT_COMMENT_STYLE_KEY)
    except KeyError:
        return proto_comments.DEFAULT_STYLE
    return style if style in proto_comments.KNOWN_STYLES else proto_comments.DEFAULT_STYLE


def _description_text(element):
    text = str(element.description).strip() if element is not None else ""
    return html.unescape(text) if text else ""


def _as_comment_lines(element, indent=""):
    """Commentaire place AU-DESSUS d'un element (message, enum, service,
    rpc), dans son style d'origine."""
    return proto_comments.render_leading(_description_text(element),
                                         get_comment_style(element), indent)


def _emit_aligned_block(entries):
    """entries : liste de (code_line_deja_indentee, element_capella,
    extra_leading) -- extra_leading est une liste optionnelle de lignes
    de commentaire toujours affichees en bloc au-dessus (ex : un
    avertissement "type non resolu"), independamment de la description.
    Convention observee dans les .proto ecrits a la main (ex:
    counter.proto) : un commentaire tenant sur UNE ligne est mis en fin
    de ligne de code, aligne en colonne avec les autres lignes du meme
    bloc (message/enum) ; un commentaire MULTI-lignes reste en bloc
    au-dessus. Dans les deux cas, le style d'origine (//, ///, /* */...)
    est respecte (cf. proto_comments)."""
    processed = []
    for code_line, element, extra_leading in entries:
        text = _description_text(element)
        style = get_comment_style(element) if element is not None else proto_comments.DEFAULT_STYLE
        indent = code_line[:len(code_line) - len(code_line.lstrip())]
        leading = list(extra_leading or [])
        if not text:
            processed.append((code_line, None, leading))
        elif "\n" not in text and not leading:
            processed.append((code_line, proto_comments.render_trailing(text, style), leading))
        else:
            # multi-lignes, ou groupe avec un avertissement : bloc au-dessus
            leading.extend(proto_comments.render_leading(text, style, indent))
            processed.append((code_line, None, leading))

    trailing_lens = [len(code) for code, trailing, _ in processed if trailing is not None]
    align_col = max(trailing_lens) + 3 if trailing_lens else 0  # +3 : au moins 2 espaces avant le commentaire

    lines = []
    for code_line, trailing, leading in processed:
        lines.extend(leading)
        if trailing is not None:
            pad = " " * (align_col - len(code_line))
            lines.append(f"{code_line}{pad}{trailing}")
        else:
            lines.append(code_line)
    return lines


def class_to_proto_message(cls, needed_imports):
    lines = _as_comment_lines(cls)
    lines.append(f"message {cls.name} {{")
    entries = []
    props = list(cls.owned_properties)
    numbers = field_numbers(props, 1)
    for prop in props:
        counter = numbers[prop.uuid]
        multiplicity = "repeated " if is_repeated(prop) else ""
        type_name = proto_type_name(prop.type, needed_imports)
        extra_leading = []
        if type_name is None:
            extra_leading = ["    // ATTENTION : type non resolu pour ce champ -- verifiez le modele"]
            type_name = "bytes"  # place-holder syntaxiquement valide, signale ci-dessus
        code_line = f"    {multiplicity}{type_name} {prop.name} = {counter};"
        entries.append((code_line, prop, extra_leading))
    lines.extend(_emit_aligned_block(entries))
    lines.append("}")
    return "\n".join(lines)


def field_numbers(elements, start):
    """{uuid: numero} : numero d'origine (PVMT FieldNumber) quand il est
    connu ; sinon, pour les elements importes avant ce mecanisme,
    numerotation sequentielle a partir de start (1 pour un champ, 0 pour
    une valeur d'enum), apres le plus grand numero deja pris -- pour ne
    jamais creer de doublon."""
    stored = {}
    for e in elements:
        try:
            stored[e.uuid] = int(pvmt_get(e, PVMT_FIELD_NUMBER_KEY))
        except (TypeError, ValueError):
            pass
    nxt = max(list(stored.values()) + [start - 1]) + 1
    numbers = {}
    for e in elements:
        if e.uuid in stored:
            numbers[e.uuid] = stored[e.uuid]
        else:
            numbers[e.uuid] = nxt
            nxt += 1
    return numbers


def enum_to_proto(enum):
    """Regenere sequentiellement 0..N-1 (cf. limite documentee en tete
    de fichier : Capella ne stocke pas la valeur numerique proto).
    Commentaires courts alignes en fin de ligne (cf. _emit_aligned_block)."""
    lines = _as_comment_lines(enum)
    lines.append(f"enum {enum.name} {{")
    literals = list(enum.owned_literals)
    numbers = field_numbers(literals, 0)
    entries = [(f"    {lit.name} = {numbers[lit.uuid]};", lit, None) for lit in literals]
    lines.extend(_emit_aligned_block(entries))
    lines.append("}")
    return "\n".join(lines)


def interface_to_proto_service(interface, needed_imports, type_namer=None):
    """needed_imports : set mutable, alimente si un well-known type
    Google est rencontre (pour que l'appelant sache quels 'import'
    ecrire). type_namer : fonction capella_type -> nom proto (export par
    fichier : qualification pkg.Type et imports inter-fichiers) ; par
    defaut proto_type_name (ancien mode par Interface)."""
    namer = type_namer or (lambda t: proto_type_name(t, needed_imports))
    lines = _as_comment_lines(interface)
    lines.append(f"service {interface.name} {{")
    first_method = True
    for op in interface.owned_features:
        if type(op).__name__ != "Service":
            continue  # ignore les autres types de Feature eventuels

        if not first_method:
            lines.append("")  # ligne vide entre chaque methode, comme dans un .proto ecrit a la main
        first_method = False

        in_param = next((p for p in op.parameters if str(p.direction) == "IN"), None)
        out_param = next((p for p in op.parameters if str(p.direction) == "OUT"), None)

        in_type = namer(in_param.type) if in_param else None
        if in_type is None:
            lines.append(f"    // ATTENTION : type d'entree non resolu pour '{op.name}'")
            in_type = "bytes"

        out_type = namer(out_param.type) if out_param else None
        if out_type is None:
            lines.append(f"    // ATTENTION : type de sortie non resolu pour '{op.name}'")
            out_type = "bytes"

        mode_literal = pvmt_get(op, PVMT_STREAMING_MODE_KEY)
        mode_name = getattr(mode_literal, "name", None)  # None si non renseigne
        client_streaming, server_streaming = STREAMING_MODE_TO_FLAGS.get(
            mode_name, (False, False))

        in_stream = "stream " if client_streaming else ""
        out_stream = "stream " if server_streaming else ""
        lines.extend(_as_comment_lines(op, indent="    "))
        lines.append(
            f"    rpc {op.name}({in_stream}{in_type}) returns ({out_stream}{out_type});"
        )
    lines.append("}")
    return "\n".join(lines)


def get_type_source_file(element):
    """Lit PVMT_SOURCE_FILE_KEY sur n'importe quel element (Class/
    Enumeration/Interface) ; None si absent (PVMT non configure ou
    element importe avant l'ajout de ce mecanisme)."""
    try:
        return pvmt_get(element, PVMT_SOURCE_FILE_KEY) or None
    except KeyError:
        return None


def _collect_referenced_types(interface):
    """Parcourt transitivement les types references par les operations
    de l'Interface (parametres, puis champs des Classes trouvees, en
    profondeur) pour recuperer toutes les Class et Enumeration a
    exporter -- pas seulement celles directement en parametre de rpc.

    Les well-known types Google (Empty, Timestamp...) sont EXCLUS du
    resultat : ils sont geres a part, via un vrai "import", jamais
    redefinis inline (cf. proto_type_name).

    Les types CUSTOM dont le SourceFile (PVMT) differe de celui de
    l'Interface exportee sont EUX AUSSI exclus de l'inline -- ils sont
    references via un vrai "import" vers leur fichier d'origine exact
    (cf. foreign_imports retourne), pas redefinis en double. On ne
    descend pas non plus dans leurs propres champs (inutile : ce
    fichier importe les definit deja). Ceci resout le cas concret ou
    deux fichiers d'un meme dossier proto se referencent l'un l'autre
    (ex: ServiceA.proto utilise Y de ServiceB.proto) : exportes
    ENSEMBLE (--all), ils ne doivent PAS chacun redefinir Y.

    SI SourceFile n'est pas disponible (PVMT non configure, sur
    l'Interface ou sur le type reference), on retombe sur l'ancien
    comportement : inline systematique (fichier de sortie toujours
    valide et autonome, mais pas organise en plusieurs fichiers).

    Le RESULTAT est ensuite reordonne selon l'ordre naturel du DataPkg
    (data_pkg.classes / data_pkg.enumerations) DE CHAQUE PACKAGE
    D'ORIGINE, qui correspond a l'ordre de declaration d'origine dans
    le .proto importe."""
    own_source_file = get_type_source_file(interface)
    classes, enums = {}, {}
    foreign_imports = set()
    seen = set()
    to_visit = []

    for op in interface.owned_features:
        if type(op).__name__ != "Service":
            continue
        for p in op.parameters:
            if p.type is not None:
                to_visit.append(p.type)

    while to_visit:
        t = to_visit.pop()
        if is_well_known_google_type(t):
            continue  # gere a part (import), jamais inline
        kind = type(t).__name__
        if kind not in ("Class", "Enumeration") or t.name in seen:
            continue
        seen.add(t.name)

        t_source = get_type_source_file(t)
        if own_source_file and t_source and t_source != own_source_file:
            foreign_imports.add(t_source)
            continue  # import precis, pas d'inline, pas de descente dans ses champs

        if kind == "Class":
            classes[t.name] = t
            for prop in t.owned_properties:
                if prop.type is not None:
                    to_visit.append(prop.type)
        else:
            enums[t.name] = t

    # Reordonner selon l'ordre naturel du DataPkg d'origine de CHAQUE
    # type (plusieurs packages possibles desormais, contrairement a la
    # version a plat -- on trie par (chemin du package, position dans
    # ce package) pour un resultat stable et groupe par module).
    def sort_key(item, is_enum):
        name, obj = item
        pkg = obj.parent
        folder = get_capella_type_folder(pkg)
        collection = pkg.enumerations if is_enum else pkg.classes
        try:
            position = list(collection).index(obj)
        except ValueError:
            position = 0
        return (folder, position)

    classes = dict(sorted(classes.items(), key=lambda kv: sort_key(kv, False)))
    enums = dict(sorted(enums.items(), key=lambda kv: sort_key(kv, True)))

    return classes, enums, foreign_imports


def export_interface_to_proto(interface, referenced_classes=None, referenced_enums=None,
                                order="messages-first", package=None):
    """Genere le texte .proto complet pour une Interface : les enums et
    messages references (transitivement) par ses operations, et le
    service.

    order : "messages-first" (defaut, convention gRPC officielle -- les
            types detailles d'abord, le service en dernier) ou
            "service-first" (le service en tete, les messages ensuite --
            convention utilisee par certaines equipes).
    package : nom de package proto a forcer explicitement (ex:
            "soba_template_api"). Si None (defaut), on essaie de le lire
            depuis le PVMT de l'Interface (Grpc.Metadata.Package, cf.
            proto_capella_types.py) ; si le PVMT n'a rien non plus,
            aucune ligne "package" n'est ecrite. Un --package explicite
            a toujours priorite sur le PVMT (utile pour surcharger ou
            tester sans avoir configure le PVMT)."""
    if order not in ("messages-first", "service-first"):
        raise ValueError('order doit etre "messages-first" ou "service-first"')

    if package is None:
        try:
            package = pvmt_get(interface, PVMT_PACKAGE_KEY) or None
        except KeyError:
            package = None

    file_header = None
    try:
        file_header = pvmt_get(interface, PVMT_HEADER_KEY) or None
    except KeyError:
        pass

    if referenced_classes is None or referenced_enums is None:
        auto_classes, auto_enums, foreign_imports = _collect_referenced_types(interface)
        referenced_classes = referenced_classes if referenced_classes is not None else auto_classes
        referenced_enums = referenced_enums if referenced_enums is not None else auto_enums
    else:
        foreign_imports = set()  # classes/enums fournies explicitement : pas de detection

    needed_imports = set(foreign_imports)  # + chemins 'google/protobuf/xxx.proto' ajoutes ci-dessous
    enum_blocks = []
    for en in referenced_enums.values():
        enum_blocks.append(enum_to_proto(en))
        enum_blocks.append("")
    message_blocks = []
    for cls in referenced_classes.values():
        message_blocks.append(class_to_proto_message(cls, needed_imports))
        message_blocks.append("")
    service_block = interface_to_proto_service(interface, needed_imports)

    header = []
    if file_header:
        # cartouche (licence/copyright) stocke BRUT a l'import (marqueurs,
        # bordures et ligne vide eventuelle compris) -> restitue a
        # l'identique ; ancien format (texte nettoye) gere aussi.
        header.extend(proto_comments.render_header(file_header))
    header.append('syntax = "proto3";')
    for import_path in sorted(needed_imports):
        header.append(f'import "{import_path}";')
    if package:
        header.append(f"package {package};")
    header.append("")  # une seule ligne vide separant le header du corps

    type_blocks = enum_blocks + message_blocks
    if order == "messages-first":
        body = type_blocks + [service_block]
    else:  # service-first
        body = [service_block, ""] + type_blocks[:-1]  # pas de ligne vide finale en trop

    return "\n".join(header + body)


# ======================================================================
# Export PAR FICHIER (necessite le PVMT SourceFile)
# ----------------------------------------------------------------------
# Regroupe Classes, Enumerations et Interfaces par fichier .proto
# d'origine et regenere chaque fichier EN ENTIER. Seule facon correcte de
# traiter : les fichiers sans service (types seuls), les fichiers a
# plusieurs services (sinon chaque Interface ecraserait l'autre, meme
# SourceFile), et les references entre fichiers (import + nommage).
# ======================================================================

def _pvmt_str(element, key):
    try:
        value = pvmt_get(element, key)
    except KeyError:
        return None
    return value or None


def _top_owner(capella_type):
    """Class/Enumeration de niveau fichier qui contient ce type (lui-meme
    s'il n'est pas imbrique) -- c'est elle qui porte SourceFile/Package."""
    t = capella_type
    while type(t.parent).__name__ == "Class":
        t = t.parent
    return t


def _dotted_name(capella_type):
    """Nom relatif au package proto : 'Outer.Inner' pour un message imbrique."""
    names = [capella_type.name]
    t = capella_type
    while type(t.parent).__name__ == "Class":
        t = t.parent
        names.insert(0, t.name)
    return ".".join(names)


def _type_package(capella_type):
    """Package proto d'un type : PVMT Package (pose a l'import sur les
    Class/Enumeration), sinon deduit du dossier (convention dossier ==
    package, '/' -> '.')."""
    owner = _top_owner(capella_type)
    pkg = _pvmt_str(owner, PVMT_PACKAGE_KEY)
    if pkg:
        return pkg
    folder = get_capella_type_folder(owner.parent)
    return folder.replace("/", ".") or None


def _import_path(target_file, from_file):
    """Directive import vers target_file depuis from_file : fichier du
    MEME dossier -> nom de fichier seul (convention de vos .proto :
    import "serviceA.proto";), sinon chemin depuis la racine proto."""
    if posixpath.dirname(target_file) == posixpath.dirname(from_file):
        return posixpath.basename(target_file)
    return target_file


def make_type_namer(file_path, file_package, imports):
    """Fonction capella_type -> nom proto, pour un fichier donne :
      - primitif           -> mot-cle proto (int32, string...)
      - well-known Google  -> google.protobuf.X + import standard
      - meme package       -> nom court (TypeA, ou Outer.Inner)
      - autre package      -> nom qualifie (autre_pkg.TypeA)
      - defini dans un autre fichier -> + import de ce fichier
    imports : set alimente au fil de l'eau."""
    def namer(capella_type, scope=None):
        if capella_type is None:
            return None
        if is_well_known_google_type(capella_type):
            return proto_type_name(capella_type, imports)
        if type(capella_type).__name__ not in ("Class", "Enumeration"):
            return CAPELLA_TO_PROTO_PRIMITIVE.get(capella_type.name, capella_type.name)
        owner_file = get_type_source_file(_top_owner(capella_type))
        if owner_file and owner_file != file_path:
            imports.add(_import_path(owner_file, file_path))
        dotted = _dotted_name(capella_type)
        pkg = _type_package(capella_type)
        if pkg and file_package and pkg != file_package:
            return f"{pkg}.{dotted}"
        return _relative_to_scope(capella_type, scope, pkg)
    return namer


def _class_chain(capella_type):
    """[englobante de niveau fichier, ..., capella_type]"""
    chain = [capella_type]
    while type(chain[0].parent).__name__ == "Class":
        chain.insert(0, chain[0].parent)
    return chain


def _relative_to_scope(capella_type, scope, pkg):
    """Nom le plus court valide depuis le message 'scope' (Class Capella
    dans laquelle le champ est ecrit), comme dans un .proto ecrit a la
    main : 'Inner' plutot que 'Features.Inner' depuis Features.
    Masquage : si un message imbrique d'un scope intermediaire porte le
    meme nom que le debut du nom court, protoc le trouverait en premier
    -> nom pleinement qualifie '.pkg.Type' (toujours sans ambiguite)."""
    target = _class_chain(capella_type)
    if scope is None:
        return ".".join(t.name for t in target)
    scope_chain = _class_chain(scope)
    k = 0
    while (k < len(target) - 1 and k < len(scope_chain)
           and target[k].uuid == scope_chain[k].uuid):
        k += 1
    candidate = [t.name for t in target[k:]]
    for enclosing in scope_chain[k:]:
        for nested in enclosing.nested_classes:
            if nested.name == candidate[0] and nested.uuid != target[k].uuid:
                full = ".".join(t.name for t in target)
                return f".{pkg}.{full}" if pkg else f".{full}"
    return ".".join(candidate)


def _map_entry_name(field_name):
    """Nom du message genere par protoc pour 'map<K,V> field_name' :
    CamelCase + 'Entry' (ex: event_list -> EventListEntry)."""
    parts = field_name.split("_")
    return "".join(p[:1].upper() + p[1:] for p in parts if p) + "Entry"


def _as_map_entry(prop, owner_cls):
    """Si prop est un champ map, retourne sa Class d'entree (imbriquee
    dans owner_cls, nommee <Champ>Entry, proprietes key/value), sinon None."""
    t = prop.type
    if (t is None or type(t).__name__ != "Class" or t.parent is None
            or getattr(t.parent, "uuid", None) != owner_cls.uuid
            or t.name != _map_entry_name(prop.name) or not is_repeated(prop)):
        return None
    names = [p.name for p in t.owned_properties]
    return t if names == ["key", "value"] else None


def _is_optional(prop):
    return (prop.min_card is not None and prop.max_card is not None
            and prop.min_card.value == "0" and prop.max_card.value == "1")


def class_to_proto_message_v2(cls, namer, indent=""):
    """Message complet : messages imbriques (hors entrees de map), puis
    champs -- map<K, V>, optional, repeated, et groupes oneof (PVMT
    Oneof). Commentaires dans leur style d'origine, fin de ligne alignee."""
    inner = indent + "    "
    lines = _as_comment_lines(cls, indent)
    lines.append(f"{indent}message {cls.name} {{")

    map_entries = {}
    for prop in cls.owned_properties:
        entry = _as_map_entry(prop, cls)
        if entry is not None:
            map_entries[entry.uuid] = entry
    for nested in cls.nested_classes:
        if nested.uuid in map_entries:
            continue  # regenere sous forme map<K, V>, pas comme message
        lines.append(class_to_proto_message_v2(nested, namer, inner))
        lines.append("")

    def field_line(prop, number, ind):
        entry = _as_map_entry(prop, cls)
        extra = []
        if entry is not None:
            k, v = entry.owned_properties
            kt, vt = namer(k.type, cls) or "string", namer(v.type, cls) or "bytes"
            return f"{ind}map<{kt}, {vt}> {prop.name} = {number};", extra
        type_name = namer(prop.type, cls)
        if type_name is None:
            extra = [f"{ind}// ATTENTION : type non resolu pour ce champ -- verifiez le modele"]
            type_name = "bytes"
        label = "repeated " if is_repeated(prop) else ("optional " if _is_optional(prop) else "")
        return f"{ind}{label}{type_name} {prop.name} = {number};", extra

    pending, emitted_oneofs = [], set()
    props = list(cls.owned_properties)
    numbers = field_numbers(props, 1)
    for prop in props:
        oneof = _pvmt_str(prop, PVMT_ONEOF_KEY)
        if not oneof:
            code, extra = field_line(prop, numbers[prop.uuid], inner)
            pending.append((code, prop, extra))
            continue
        if oneof in emitted_oneofs:
            continue
        emitted_oneofs.add(oneof)
        lines.extend(_emit_aligned_block(pending)); pending = []
        members = [p for p in props if _pvmt_str(p, PVMT_ONEOF_KEY) == oneof]
        lines.append(f"{inner}oneof {oneof} {{")
        block = []
        for m in members:
            code, extra = field_line(m, numbers[m.uuid], inner + "    ")
            block.append((code, m, extra))
        lines.extend(_emit_aligned_block(block))
        lines.append(f"{inner}}}")
    lines.extend(_emit_aligned_block(pending))
    lines.append(f"{indent}}}")
    return "\n".join(lines)


def _in_layer(element, layer_uuid):
    return layer_uuid is None or element.layer.uuid == layer_uuid


def collect_files(model, layer_code=None):
    """{SourceFile: {"classes": [...], "enums": [...], "interfaces": [...]}}
    pour tous les elements de niveau fichier portant un SourceFile (hors
    well-known types Google, jamais regeneres). Ordre = ordre du modele
    (= ordre de declaration d'origine, les elements etant crees dans
    l'ordre du fichier a l'import)."""
    layer_uuid = getattr(model, layer_code).uuid if layer_code else None
    files = {}

    def add(kind, element):
        if not _in_layer(element, layer_uuid):
            return
        src = get_type_source_file(element)
        if not src or src.startswith(WELL_KNOWN_GOOGLE_FOLDER + "/"):
            return
        files.setdefault(src, {"classes": [], "enums": [], "interfaces": []})[kind].append(element)

    for c in model.search("Class"):
        if type(c.parent).__name__ != "Class":  # les imbriquees suivent leur englobante
            add("classes", c)
    for e in model.search("Enumeration"):
        add("enums", e)
    for i in model.search("Interface"):
        add("interfaces", i)
    return files


def export_file_to_proto(file_path, content, order="messages-first", package=None):
    """Regenere un fichier .proto complet : cartouche, syntax, imports,
    package, enums, messages, puis services (0, 1 ou plusieurs)."""
    if order not in ("messages-first", "service-first"):
        raise ValueError('order doit etre "messages-first" ou "service-first"')
    elements = content["enums"] + content["classes"] + content["interfaces"]
    if package is None:
        package = next((p for p in (_pvmt_str(e, PVMT_PACKAGE_KEY) for e in elements) if p), None)
    file_header = next((h for h in (_pvmt_str(e, PVMT_HEADER_KEY) for e in elements) if h), None)

    imports = set()
    namer = make_type_namer(file_path, package, imports)
    type_blocks = []
    for en in content["enums"]:
        type_blocks += [enum_to_proto(en), ""]
    for cls in content["classes"]:
        type_blocks += [class_to_proto_message_v2(cls, namer), ""]
    service_blocks = []
    for iface in content["interfaces"]:
        service_blocks += [interface_to_proto_service(iface, imports, namer), ""]

    header = []
    if file_header:
        header.extend(proto_comments.render_header(file_header))
    header.append('syntax = "proto3";')
    for imp_path in sorted(imports, key=lambda x: (not x.startswith("google/"), x)):
        header.append(f'import "{imp_path}";')
    if package:
        header.append(f"package {package};")
    header.append("")

    if order == "messages-first":
        body = type_blocks + service_blocks
    else:
        body = service_blocks + type_blocks
    while body and body[-1] == "":
        body.pop()
    return "\n".join(header + body) + "\n"



def find_interface(model, interface_name, layer_code=None):
    candidates = [i for i in model.search("Interface") if i.name == interface_name]
    if not candidates:
        raise KeyError(f"Aucune Interface nommee '{interface_name}' trouvee.")
    if layer_code is not None:
        target_uuid = getattr(model, layer_code).uuid
        candidates = [i for i in candidates if i.layer.uuid == target_uuid]
        if not candidates:
            raise KeyError(f"Aucune Interface '{interface_name}' dans la couche "
                            f"{LAYER_CHOICES[layer_code]}.")
    if len(candidates) > 1:
        layers_found = ", ".join(sorted({i.layer.name for i in candidates}))
        raise KeyError(f"Plusieurs Interfaces '{interface_name}' trouvees, dans : "
                        f"{layers_found}. Precisez --layer.")
    return candidates[0]


def compute_output_path(interface, output_root):
    """Reconstruit le chemin de sortie sous output_root :
    - si PVMT_SOURCE_FILE_KEY est renseigne (cf. import), utilise le
      chemin EXACT d'origine (ex: 'service_base_api/ServiceB.proto') --
      fidele au nom de fichier reel, pas juste au nom de l'Interface.
    - sinon, repli sur <dossier miroir du package Capella>/
      <InterfaceName>.proto (approximatif : le nom de fichier n'est pas
      garanti correspondre a l'original, cf. limite documentee)."""
    try:
        source_file = pvmt_get(interface, PVMT_SOURCE_FILE_KEY)
        if source_file:
            return os.path.join(output_root, source_file)
    except KeyError:
        pass
    folder = get_capella_type_folder(interface.parent)
    filename = f"{interface.name}.proto"
    return os.path.join(output_root, folder, filename) if folder else os.path.join(output_root, filename)


def _write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export Capella -> .proto")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("interface_name", nargs="?", default=None,
                         help="Nom de l'Interface Capella a exporter (exporte TOUT le fichier "
                              ".proto qui la contient si SourceFile est connu). Omis avec --all/--file.")
    parser.add_argument("output_path", nargs="?", default=None,
                         help="Fichier .proto de sortie (chemin exact). Incompatible avec "
                              "--output-root.")
    parser.add_argument("--output-root", default=None,
                         help="Dossier racine : chaque fichier est ecrit sous "
                              "<output-root>/<chemin d'origine> (SourceFile), ou "
                              "<output-root>/<dossier>/<InterfaceName>.proto a defaut. "
                              "Obligatoire avec --all.")
    parser.add_argument("--all", action="store_true",
                         help="Exporte TOUS les fichiers .proto du modele (y compris ceux sans "
                              "service ou a plusieurs services), en regenerant l'arborescence "
                              "sous --output-root.")
    parser.add_argument("--file", default=None,
                         help="Exporte un fichier .proto precis, designe par son chemin "
                              "d'origine (SourceFile), ex: soba_function_api/types.proto -- "
                              "utile pour un fichier sans service.")
    parser.add_argument("--layer", default=None, choices=list(LAYER_CHOICES),
                         help="Restreint la recherche a une couche.")
    parser.add_argument("--include-legacy", action="store_true",
                         help="Avec --all : exporte AUSSI les Interfaces sans SourceFile, dans "
                              "l'ancien mode par Interface (modeles importes avant ce "
                              "mecanisme). Par defaut non : sinon TOUTES les Interfaces du "
                              "modele, y compris celles sans rapport avec gRPC, seraient "
                              "exportees.")
    parser.add_argument("--order", default="messages-first",
                         choices=["messages-first", "service-first"],
                         help="messages-first (defaut : enums, messages, puis services) ou "
                              "service-first.")
    parser.add_argument("--package", default=None,
                         help="Force le package proto (prioritaire sur le PVMT). En mode --all, "
                              "a laisser vide pour que chaque fichier garde le sien.")
    args = parser.parse_args()

    modes = sum(bool(x) for x in (args.all, args.file, args.interface_name))
    if modes != 1:
        print("ERREUR : choisissez UN mode : un nom d'Interface, --file ou --all.")
        sys.exit(1)
    if args.all and (args.output_path or not args.output_root):
        print("ERREUR : --all s'utilise avec --output-root uniquement.")
        sys.exit(1)
    if not args.all and bool(args.output_path) == bool(args.output_root):
        print("ERREUR : donnez soit un chemin de sortie explicite, soit --output-root.")
        sys.exit(1)

    model = capellambse.MelodyModel(args.model_path)
    files = collect_files(model, args.layer)

    if args.all:
        # Interfaces sans SourceFile (importees avant ce mecanisme, ou PVMT
        # absent) : repli sur l'ancien export par Interface.
        layer_uuid = getattr(model, args.layer).uuid if args.layer else None
        orphans = [i for i in model.search("Interface")
                   if _in_layer(i, layer_uuid) and not get_type_source_file(i)]
        if orphans and not args.include_legacy:
            print(f"INFO : {len(orphans)} Interface(s) sans SourceFile ignoree(s) (non issues "
                  f"d'un import .proto, ou importees avant ce mecanisme) -- ajoutez "
                  f"--include-legacy pour les exporter dans l'ancien mode.")
            orphans = []
        if not files and not orphans:
            print("ERREUR : rien a exporter.")
            sys.exit(1)
        print(f"INFO : {len(files)} fichier(s) .proto a regenerer (SourceFile)"
              + (f" + {len(orphans)} Interface(s) sans SourceFile (ancien mode)" if orphans else "")
              + f" sous '{args.output_root}' :")
        exported, failed = 0, []
        for src in sorted(files):
            c = files[src]
            try:
                out = os.path.join(args.output_root, src)
                _write(out, export_file_to_proto(src, c, order=args.order, package=args.package))
                print(f"  OK  {src}  ({len(c['enums'])} enum(s), {len(c['classes'])} message(s), "
                      f"{len(c['interfaces'])} service(s))")
                exported += 1
            except Exception as e:
                print(f"  ECHEC  {src} : {e}")
                failed.append(src)
        for iface in orphans:
            try:
                out = compute_output_path(iface, args.output_root)
                _write(out, export_interface_to_proto(iface, order=args.order, package=args.package))
                print(f"  OK  {iface.name} -> {out}  (sans SourceFile)")
                exported += 1
            except Exception as e:
                print(f"  ECHEC  {iface.name} : {e}")
                failed.append(iface.name)
        total = len(files) + len(orphans)
        print(f"\nExport termine : {exported}/{total} exporte(s)"
              + (f", {len(failed)} en echec : {', '.join(failed)}" if failed else "."))
        sys.exit(1 if failed else 0)

    if args.file:
        src = args.file.replace("\\", "/")
        if src not in files:
            print(f"ERREUR : aucun element avec SourceFile '{src}'. Fichiers connus :")
            for k in sorted(files):
                print(f"    {k}")
            sys.exit(1)
        out = args.output_path or os.path.join(args.output_root, src)
        _write(out, export_file_to_proto(src, files[src], order=args.order, package=args.package))
        print(f"Export termine : {out}")
        sys.exit(0)

    interface = find_interface(model, args.interface_name, args.layer)
    src = get_type_source_file(interface)
    if src and src in files:
        out = args.output_path or os.path.join(args.output_root, src)
        _write(out, export_file_to_proto(src, files[src], order=args.order, package=args.package))
        print(f"Export termine ({interface.layer.name}) : fichier complet '{src}' -> {out}")
    else:
        out = args.output_path or compute_output_path(interface, args.output_root)
        _write(out, export_interface_to_proto(interface, order=args.order, package=args.package))
        print(f"Export termine ({interface.layer.name}) : {out}  (sans SourceFile, ancien mode)")