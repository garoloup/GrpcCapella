# -*- coding: utf-8 -*-
"""
Export Capella -> .proto, via capellambse (lecture seule, pur Python 3,
sans Capella ni Java lances).

Usage :
    python3 export_capella_to_proto.py Model.aird InterfaceName sortie.proto [--layer=la] [--order=messages-first] [--package=mon.pkg]

--layer sert a lever l'ambiguite si plusieurs Interfaces portent le
meme nom dans des couches differentes.

--package : le .proto d'origine a un "package" (ex: soba_template_api).
Si l'import a pu l'ecrire via PVMT (optionnel, cf. proto_capella_types.py
-- Grpc.Metadata.Package), l'export le relit automatiquement, sans rien
a faire ici. Sinon (PVMT absent au moment de l'import), passez-le
explicitement avec --package ; un --package explicite est de toute
facon toujours prioritaire sur le PVMT.

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
import argparse
import html
import capellambse

from proto_capella_types import (
    CAPELLA_TO_PROTO_PRIMITIVE,
    PVMT_CLIENT_STREAMING_KEY,
    PVMT_SERVER_STREAMING_KEY,
    PVMT_PACKAGE_KEY,
)

LAYER_CHOICES = {"oa": "Operational Analysis", "sa": "System Analysis",
                  "la": "Logical Architecture", "pa": "Physical Architecture"}

EMPTY_TYPE_PROTO_NAME = "google.protobuf.Empty"
EMPTY_TYPE_IMPORT = 'import "google/protobuf/empty.proto";'


def proto_type_name(capella_type):
    if capella_type is None:
        return None  # type reellement inconnu -- distinct de "pas de type" (Empty)
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


def class_to_proto_message(cls):
    lines = _as_comment_lines(cls.description)
    lines.append(f"message {cls.name} {{")
    for counter, prop in enumerate(cls.owned_properties, start=1):
        lines.extend(_as_comment_lines(prop.description, indent="    "))
        multiplicity = "repeated " if is_repeated(prop) else ""
        type_name = proto_type_name(prop.type)
        if type_name is None:
            lines.append(f"    // ATTENTION : type non resolu pour ce champ -- verifiez le modele")
            type_name = "bytes"  # place-holder syntaxiquement valide, signale ci-dessus
        lines.append(f"    {multiplicity}{type_name} {prop.name} = {counter};")
    lines.append("}")
    return "\n".join(lines)


def enum_to_proto(enum):
    """Regenere sequentiellement 0..N-1 (cf. limite documentee en tete
    de fichier : Capella ne stocke pas la valeur numerique proto)."""
    lines = _as_comment_lines(enum.description)
    lines.append(f"enum {enum.name} {{")
    for number, lit in enumerate(enum.owned_literals):
        lines.extend(_as_comment_lines(lit.description, indent="    "))
        lines.append(f"    {lit.name} = {number};")
    lines.append("}")
    return "\n".join(lines)


def interface_to_proto_service(interface, uses_empty):
    """uses_empty : set mutable, alimente ici si Empty est rencontre
    (pour que l'appelant sache s'il doit ecrire l'import correspondant)."""
    lines = _as_comment_lines(interface.description)
    lines.append(f"service {interface.name} {{")
    for op in interface.owned_features:
        if type(op).__name__ != "Service":
            continue  # ignore les autres types de Feature eventuels

        in_param = next((p for p in op.parameters if str(p.direction) == "IN"), None)
        out_param = next((p for p in op.parameters if str(p.direction) == "OUT"), None)

        # Absence de Parameter == google.protobuf.Empty (c'est ainsi que
        # l'import encode ce cas special -- cf. import_proto_to_capella.py)
        if in_param is None:
            in_type = EMPTY_TYPE_PROTO_NAME
            uses_empty.add(True)
        else:
            in_type = proto_type_name(in_param.type)
            if in_type is None:
                lines.append(f"    // ATTENTION : type d'entree non resolu pour '{op.name}'")
                in_type = EMPTY_TYPE_PROTO_NAME
                uses_empty.add(True)

        if out_param is None:
            out_type = EMPTY_TYPE_PROTO_NAME
            uses_empty.add(True)
        else:
            out_type = proto_type_name(out_param.type)
            if out_type is None:
                lines.append(f"    // ATTENTION : type de sortie non resolu pour '{op.name}'")
                out_type = EMPTY_TYPE_PROTO_NAME
                uses_empty.add(True)

        try:
            client_streaming = bool(op.pvmt[PVMT_CLIENT_STREAMING_KEY])
            server_streaming = bool(op.pvmt[PVMT_SERVER_STREAMING_KEY])
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


def _collect_referenced_types(interface):
    """Parcourt transitivement les types references par les operations
    de l'Interface (parametres, puis champs des Classes trouvees, en
    profondeur) pour recuperer toutes les Class et Enumeration a
    exporter -- pas seulement celles directement en parametre de rpc.

    Le RESULTAT est ensuite reordonne selon l'ordre naturel du DataPkg
    (data_pkg.classes / data_pkg.enumerations), qui correspond a l'ordre
    de declaration d'origine dans le .proto importe (cf. import :
    les Class/Enumeration sont creees dans l'ordre du fichier source).
    Le parcours lui-meme (recherche en profondeur, ordre de pile) sert
    uniquement a trouver l'ensemble complet des types ; ce n'est pas
    l'ordre de sortie final."""
    classes, enums = {}, {}
    to_visit = []

    for op in interface.owned_features:
        if type(op).__name__ != "Service":
            continue
        for p in op.parameters:
            if p.type is not None:
                to_visit.append(p.type)

    while to_visit:
        t = to_visit.pop()
        kind = type(t).__name__
        if kind == "Class" and t.name not in classes:
            classes[t.name] = t
            for prop in t.owned_properties:
                if prop.type is not None:
                    to_visit.append(prop.type)
        elif kind == "Enumeration" and t.name not in enums:
            enums[t.name] = t

    # Reordonner selon l'ordre naturel du DataPkg (= ordre de declaration
    # d'origine). data_pkg est accessible via le parent de n'importe quel
    # type deja collecte (toutes les Class/Enumeration d'une meme
    # Interface vivent dans le meme DataPkg dans notre modele d'import).
    if classes or enums:
        data_pkg = next(iter(classes.values())).parent if classes else next(iter(enums.values())).parent
        class_order = {c.name: i for i, c in enumerate(data_pkg.classes)}
        enum_order = {e.name: i for i, e in enumerate(data_pkg.enumerations)}
        classes = dict(sorted(classes.items(), key=lambda kv: class_order.get(kv[0], 0)))
        enums = dict(sorted(enums.items(), key=lambda kv: enum_order.get(kv[0], 0)))

    return classes, enums


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

    if referenced_classes is None or referenced_enums is None:
        auto_classes, auto_enums = _collect_referenced_types(interface)
        referenced_classes = referenced_classes if referenced_classes is not None else auto_classes
        referenced_enums = referenced_enums if referenced_enums is not None else auto_enums

    uses_empty = set()
    enum_blocks = []
    for en in referenced_enums.values():
        enum_blocks.append(enum_to_proto(en))
        enum_blocks.append("")
    message_blocks = []
    for cls in referenced_classes.values():
        message_blocks.append(class_to_proto_message(cls))
        message_blocks.append("")
    service_block = interface_to_proto_service(interface, uses_empty)

    header = ['syntax = "proto3";']
    if uses_empty:
        header.append(EMPTY_TYPE_IMPORT)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export Capella Interface -> .proto")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("interface_name", help="Nom de l'Interface Capella a exporter")
    parser.add_argument("output_path", help="Fichier .proto de sortie")
    parser.add_argument("--layer", default=None, choices=list(LAYER_CHOICES),
                         help="Restreint la recherche a une couche si le nom est ambigu. "
                              "Choix : " + ", ".join(f"{k}={v}" for k, v in LAYER_CHOICES.items()))
    parser.add_argument("--order", default="messages-first",
                         choices=["messages-first", "service-first"],
                         help="Ordre de generation : messages-first (defaut, convention gRPC "
                              "officielle -- types detailles d'abord) ou service-first "
                              "(le service en tete du fichier).")
    parser.add_argument("--package", default=None,
                         help="Force le package proto (prioritaire sur le PVMT de "
                              "l'Interface s'il existe). Sans cet argument, tente de "
                              "lire Grpc.Metadata.Package via PVMT ; sinon omis.")
    args = parser.parse_args()

    model = capellambse.MelodyModel(args.model_path)
    interface = find_interface(model, args.interface_name, args.layer)

    text = export_interface_to_proto(interface, order=args.order, package=args.package)
    with open(args.output_path, "w") as f:
        f.write(text)

    print(f"Export termine ({interface.layer.name}) : {args.output_path}")