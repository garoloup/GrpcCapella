# -*- coding: utf-8 -*-
"""
Export Capella -> .proto, via capellambse (lecture seule, pur Python 3,
sans Capella ni Java lances).

Usage :
    python3 export_capella_to_proto.py Model.aird InterfaceName sortie.proto [--layer=la]

--layer sert a lever l'ambiguite si plusieurs Interfaces portent le
meme nom dans des couches differentes (defaut : cherche dans tout le
modele, prend la premiere trouvee si une seule couche a un resultat ;
sinon precisez --layer).

LIMITE CONNUE (fidelite du round-trip) : la detection "repeated" se
base sur prop.max_card. Si vos Classes ont ete importees par notre
script d'import et que celui-ci n'a pas pose min_card/max_card
explicitement, tous les champs ressortiront ici comme non-repeated.
"""

import sys
import argparse
import html
import capellambse

from proto_capella_types import (
    CAPELLA_TO_PROTO_PRIMITIVE,
    PVMT_CLIENT_STREAMING_KEY,
    PVMT_SERVER_STREAMING_KEY,
)

LAYER_CHOICES = {"oa": "Operational Analysis", "sa": "System Analysis",
                  "la": "Logical Architecture", "pa": "Physical Architecture"}


def proto_type_name(capella_type):
    if capella_type is None:
        return "bytes"  # type inconnu : ne bloque pas la generation
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
        lines.append(f"    {multiplicity}{proto_type_name(prop.type)} {prop.name} = {counter};")
    lines.append("}")
    return "\n".join(lines)


def interface_to_proto_service(interface):
    lines = _as_comment_lines(interface.description)
    lines.append(f"service {interface.name} {{")
    for op in interface.owned_features:
        if type(op).__name__ != "Service":
            continue  # ignore les autres types de Feature eventuels

        in_param = next((p for p in op.parameters if str(p.direction) == "IN"), None)
        out_param = next((p for p in op.parameters if str(p.direction) == "OUT"), None)
        in_type = in_param.type.name if in_param and in_param.type else "google.protobuf.Empty"
        out_type = out_param.type.name if out_param and out_param.type else "google.protobuf.Empty"

        try:
            client_streaming = bool(op.pvmt[PVMT_CLIENT_STREAMING_KEY])
            server_streaming = bool(op.pvmt[PVMT_SERVER_STREAMING_KEY])
        except KeyError:
            client_streaming = server_streaming = False

        in_stream = "stream " if client_streaming else ""
        out_stream = "stream " if server_streaming else ""
        lines.extend(_as_comment_lines(op.description, indent="    "))
        lines.append(
            f"    rpc {op.name} ({in_stream}{in_type}) returns ({out_stream}{out_type});"
        )
    lines.append("}")
    return "\n".join(lines)


def export_interface_to_proto(interface, referenced_classes=None):
    """Genere le texte .proto complet pour une Interface : les messages
    de tous les types references par ses operations, puis le service."""
    if referenced_classes is None:
        referenced_classes = {}
        for op in interface.owned_features:
            if type(op).__name__ != "Service":
                continue
            for p in op.parameters:
                if p.type is not None and type(p.type).__name__ == "Class":
                    referenced_classes[p.type.name] = p.type

    lines = ['syntax = "proto3";', ""]
    for cls in referenced_classes.values():
        lines.append(class_to_proto_message(cls))
        lines.append("")
    lines.append(interface_to_proto_service(interface))
    return "\n".join(lines)


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
    args = parser.parse_args()

    model = capellambse.MelodyModel(args.model_path)
    interface = find_interface(model, args.interface_name, args.layer)

    text = export_interface_to_proto(interface)
    with open(args.output_path, "w") as f:
        f.write(text)

    print(f"Export termine ({interface.layer.name}) : {args.output_path}")