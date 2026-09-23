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
import argparse
import html
import capellambse

from proto_capella_types import (
    CAPELLA_TO_PROTO_PRIMITIVE,
    PVMT_STREAMING_MODE_KEY,
    STREAMING_MODE_TO_FLAGS,
    PVMT_PACKAGE_KEY,
    PVMT_SOURCE_FILE_KEY,
    PVMT_HEADER_KEY,
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


def _as_comment_lines(description, indent=""):
    """Convertit une description Capella (texte, eventuellement multi-
    lignes/multi-paragraphes) en lignes de commentaire .proto."""
    text = str(description).strip()
    if not text:
        return []
    text = html.unescape(text)
    lines = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        lines.append(f"{indent}// {raw_line}" if raw_line else f"{indent}//")
    return lines


def _emit_aligned_block(entries):
    """entries : liste de (code_line_deja_indentee, description_capella,
    extra_leading) -- extra_leading est une liste optionnelle de lignes
    de commentaire toujours affichees en bloc au-dessus (ex : un
    avertissement "type non resolu"), independamment de la description.
    Convention observee dans les .proto ecrits a la main (ex:
    counter.proto) : un commentaire tenant sur UNE ligne est mis en fin
    de ligne de code, aligne en colonne avec les autres lignes du meme
    bloc (message/enum) ; un commentaire MULTI-lignes reste en bloc
    au-dessus, comme avant (un commentaire multi-lignes ne peut pas
    raisonnablement tenir en fin de ligne)."""
    processed = []
    for code_line, description, extra_leading in entries:
        text = str(description).strip()
        text = html.unescape(text) if text else ""
        leading = list(extra_leading or [])
        if not text:
            processed.append((code_line, None, leading))
        elif "\n" not in text:
            if leading:
                # une extra_leading force le mode "bloc au-dessus" meme
                # pour un commentaire d'une ligne, pour rester groupe
                # avec l'avertissement juste au-dessus.
                indent = code_line[:len(code_line) - len(code_line.lstrip())]
                leading.append(f"{indent}// {text}")
                processed.append((code_line, None, leading))
            else:
                processed.append((code_line, text, leading))
        else:
            indent = code_line[:len(code_line) - len(code_line.lstrip())]
            leading.extend(f"{indent}// {l.strip()}" if l.strip() else f"{indent}//"
                            for l in text.splitlines())
            processed.append((code_line, None, leading))

    trailing_lens = [len(code) for code, trailing, _ in processed if trailing is not None]
    align_col = max(trailing_lens) + 3 if trailing_lens else 0  # +3 : au moins 2 espaces avant "//"

    lines = []
    for code_line, trailing, leading in processed:
        lines.extend(leading)
        if trailing is not None:
            pad = " " * (align_col - len(code_line))
            lines.append(f"{code_line}{pad}// {trailing}")
        else:
            lines.append(code_line)
    return lines


def class_to_proto_message(cls, needed_imports):
    lines = _as_comment_lines(cls.description)
    lines.append(f"message {cls.name} {{")
    entries = []
    for counter, prop in enumerate(cls.owned_properties, start=1):
        multiplicity = "repeated " if is_repeated(prop) else ""
        type_name = proto_type_name(prop.type, needed_imports)
        extra_leading = []
        if type_name is None:
            extra_leading = ["    // ATTENTION : type non resolu pour ce champ -- verifiez le modele"]
            type_name = "bytes"  # place-holder syntaxiquement valide, signale ci-dessus
        code_line = f"    {multiplicity}{type_name} {prop.name} = {counter};"
        entries.append((code_line, prop.description, extra_leading))
    lines.extend(_emit_aligned_block(entries))
    lines.append("}")
    return "\n".join(lines)


def enum_to_proto(enum):
    """Regenere sequentiellement 0..N-1 (cf. limite documentee en tete
    de fichier : Capella ne stocke pas la valeur numerique proto).
    Commentaires courts alignes en fin de ligne (cf. _emit_aligned_block)."""
    lines = _as_comment_lines(enum.description)
    lines.append(f"enum {enum.name} {{")
    entries = [(f"    {lit.name} = {number};", lit.description, None)
               for number, lit in enumerate(enum.owned_literals)]
    lines.extend(_emit_aligned_block(entries))
    lines.append("}")
    return "\n".join(lines)


def interface_to_proto_service(interface, needed_imports):
    """needed_imports : set mutable, alimente si un well-known type
    Google est rencontre (pour que l'appelant sache quels 'import'
    ecrire)."""
    lines = _as_comment_lines(interface.description)
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

        in_type = proto_type_name(in_param.type, needed_imports) if in_param else None
        if in_type is None:
            lines.append(f"    // ATTENTION : type d'entree non resolu pour '{op.name}'")
            in_type = "bytes"

        out_type = proto_type_name(out_param.type, needed_imports) if out_param else None
        if out_type is None:
            lines.append(f"    // ATTENTION : type de sortie non resolu pour '{op.name}'")
            out_type = "bytes"

        try:
            mode_literal = op.pvmt[PVMT_STREAMING_MODE_KEY]
            client_streaming, server_streaming = STREAMING_MODE_TO_FLAGS.get(
                mode_literal.name, (False, False))
        except KeyError:
            client_streaming = server_streaming = False

        in_stream = "stream " if client_streaming else ""
        out_stream = "stream " if server_streaming else ""
        lines.extend(_as_comment_lines(op.description, indent="    "))
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
        return element.pvmt[PVMT_SOURCE_FILE_KEY] or None
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
            package = interface.pvmt[PVMT_PACKAGE_KEY] or None
        except KeyError:
            package = None

    file_header = None
    try:
        file_header = interface.pvmt[PVMT_HEADER_KEY] or None
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
        # cartouche (licence/copyright), separe du reste par une ligne
        # vide -- meme convention que le fichier d'origine (cf.
        # leading_detached_comments cote import).
        header.extend(f"// {l}" if l else "//" for l in file_header.splitlines())
        header.append("")
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
        source_file = interface.pvmt[PVMT_SOURCE_FILE_KEY]
        if source_file:
            return os.path.join(output_root, source_file)
    except KeyError:
        pass
    folder = get_capella_type_folder(interface.parent)
    filename = f"{interface.name}.proto"
    return os.path.join(output_root, folder, filename) if folder else os.path.join(output_root, filename)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export Capella Interface(s) -> .proto")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("interface_name", nargs="?", default=None,
                         help="Nom de l'Interface Capella a exporter. Omis si --all.")
    parser.add_argument("output_path", nargs="?", default=None,
                         help="Fichier .proto de sortie (chemin exact). Incompatible avec "
                              "--output-root et --all -- utilisez l'un ou l'autre.")
    parser.add_argument("--output-root", default=None,
                         help="Dossier racine : le fichier est ecrit automatiquement dans "
                              "<output-root>/<dossier miroir du package Capella>/"
                              "<InterfaceName>.proto (les dossiers manquants sont crees). "
                              "Incompatible avec output_path. Obligatoire avec --all.")
    parser.add_argument("--all", action="store_true",
                         help="Exporte TOUTES les Interfaces trouvees (dans la couche "
                              "--layer si precisee, sinon dans tout le modele), en "
                              "regenerant l'arborescence complete sous --output-root "
                              "(obligatoire dans ce mode). Symetrique du mode arborescence "
                              "de l'import.")
    parser.add_argument("--layer", default=None, choices=list(LAYER_CHOICES),
                         help="Restreint la recherche a une couche si le nom est ambigu "
                              "(mode simple), ou limite --all a cette couche.")
    parser.add_argument("--order", default="messages-first",
                         choices=["messages-first", "service-first"],
                         help="Ordre de generation : messages-first (defaut, convention gRPC "
                              "officielle -- types detailles d'abord) ou service-first "
                              "(le service en tete du fichier).")
    parser.add_argument("--package", default=None,
                         help="Force le package proto (prioritaire sur le PVMT de "
                              "l'Interface s'il existe). Sans cet argument, tente de "
                              "lire Grpc.Metadata.Package via PVMT ; sinon omis. En mode "
                              "--all, applique a TOUTES les Interfaces si donne -- "
                              "generalement a laisser vide pour que chacune garde son "
                              "propre package via PVMT.")
    args = parser.parse_args()

    if args.all:
        if args.interface_name or args.output_path or not args.output_root:
            print("ERREUR : --all s'utilise avec --output-root uniquement, sans nom "
                  "d'Interface ni chemin de sortie explicite.")
            sys.exit(1)
    elif bool(args.output_path) == bool(args.output_root):
        print("ERREUR : donnez soit un chemin de sortie explicite, soit --output-root "
              "(pas les deux, pas aucun des deux). Ou --all pour tout exporter.")
        sys.exit(1)
    elif not args.interface_name:
        print("ERREUR : donnez le nom de l'Interface a exporter (ou --all pour tout exporter).")
        sys.exit(1)

    model = capellambse.MelodyModel(args.model_path)

    if args.all:
        interfaces = list(model.search("Interface"))
        if args.layer:
            target_uuid = getattr(model, args.layer).uuid
            interfaces = [i for i in interfaces if i.layer.uuid == target_uuid]
        if not interfaces:
            print("ERREUR : aucune Interface trouvee" +
                  (f" dans {LAYER_CHOICES[args.layer]}." if args.layer else " dans le modele."))
            sys.exit(1)
        print(f"INFO : {len(interfaces)} Interface(s) a exporter sous '{args.output_root}' :")
        for i in interfaces:
            print(f"    {i.name} ({i.layer.name})")

        exported, failed = 0, []
        for interface in interfaces:
            try:
                output_path = compute_output_path(interface, args.output_root)
                os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
                text = export_interface_to_proto(interface, order=args.order, package=args.package)
                with open(output_path, "w") as f:
                    f.write(text)
                print(f"  OK  {interface.name} -> {output_path}")
                exported += 1
            except Exception as e:
                print(f"  ECHEC  {interface.name} : {e}")
                failed.append(interface.name)

        print(f"\nExport termine : {exported}/{len(interfaces)} Interface(s) exportee(s)"
              + (f", {len(failed)} en echec : {', '.join(failed)}" if failed else "."))
        sys.exit(1 if failed else 0)

    interface = find_interface(model, args.interface_name, args.layer)

    output_path = args.output_path or compute_output_path(interface, args.output_root)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    text = export_interface_to_proto(interface, order=args.order, package=args.package)
    with open(output_path, "w") as f:
        f.write(text)

    print(f"Export termine ({interface.layer.name}) : {output_path}")