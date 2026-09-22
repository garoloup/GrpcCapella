# -*- coding: utf-8 -*-
"""
Cree les DataTypes primitifs (String, Boolean, Integer, Float) dans le
DataPkg d'une couche, s'ils n'existent pas deja -- a executer UNE FOIS
par projet/couche avant le premier import, si list_datatypes.py montre
qu'ils sont absents.

Usage :
    python3 setup_primitive_types.py Model.aird [--layer=la]

Idempotent : relancer ce script ne duplique pas les types deja
presents (verification par nom avant creation).
"""

import sys
import argparse
import capellambse

from proto_capella_types import PROTO_TO_CAPELLA_PRIMITIVE, NUMERIC_TYPE_RANGES

LAYER_CHOICES = {"oa": "Operational Analysis", "sa": "System Analysis",
                  "la": "Logical Architecture", "pa": "Physical Architecture"}

# Genere la liste des types a creer directement depuis
# PROTO_TO_CAPELLA_PRIMITIVE (proto_capella_types.py), pour rester
# automatiquement synchronise si vous ajoutez/modifiez des types la-bas.
#
# NB : les bornes min/max reelles (NUMERIC_TYPE_RANGES) ne sont PAS
# posees ici -- capellambse exige un objet LiteralNumericValue construit
# explicitement pour min_value/max_value (un entier brut est rejete :
# "Cannot create object from a single attribute"), ce qui alourdirait
# ce script pour un gain secondaire. Ce qui est corrige et essentiel,
# c'est la distinction de NOM (Int32 vs UInt64 vs...), qui elle est
# fiable pour le round-trip. Les bornes restent une amelioration
# possible plus tard si besoin.
def _build_types_to_create():
    types = []
    for capella_name in sorted(set(PROTO_TO_CAPELLA_PRIMITIVE.values())):
        if capella_name == "String":
            types.append((capella_name, "StringType", {}))
        elif capella_name == "Bytes":
            types.append((capella_name, "StringType", {}))  # pas de type "bytes" dedie en Capella
        elif capella_name == "Boolean":
            types.append((capella_name, "BooleanType", {}))
        elif capella_name in NUMERIC_TYPE_RANGES:
            kind, _min_v, _max_v = NUMERIC_TYPE_RANGES[capella_name]
            types.append((capella_name, "NumericType", {"kind": kind}))
        else:
            raise ValueError(f"Type Capella '{capella_name}' non reconnu -- "
                              f"ajoutez-le a NUMERIC_TYPE_RANGES ou au cas "
                              f"particulier dans _build_types_to_create().")
    return types


PRIMITIVE_TYPES_TO_CREATE = _build_types_to_create()


def ensure_primitive_types(data_pkg):
    created, already_present = [], []
    for name, metaclass, kwargs in PRIMITIVE_TYPES_TO_CREATE:
        try:
            data_pkg.data_types.by_name(name)
            already_present.append(name)
        except KeyError:
            data_pkg.data_types.create(metaclass, name=name, **kwargs)
            created.append(name)
    return created, already_present


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Cree les DataTypes primitifs manquants (String/Boolean/Integer/Float)")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("--layer", default="la", choices=list(LAYER_CHOICES),
                         help="Couche cible (defaut: la = Logical Architecture)")
    args = parser.parse_args()

    model = capellambse.MelodyModel(args.model_path)
    layer = getattr(model, args.layer)

    created, already_present = ensure_primitive_types(layer.data_pkg)
    if created:
        model.save()

    print(f"Couche : {LAYER_CHOICES[args.layer]}")
    if created:
        print(f"Crees        : {', '.join(created)}")
    if already_present:
        print(f"Deja presents : {', '.join(already_present)}")
    if not created:
        print("Rien a faire, tous les types primitifs existent deja.")
