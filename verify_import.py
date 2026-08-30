# -*- coding: utf-8 -*-
"""
Verifie ce qui a ete cree par import_proto_to_capella_v2.py, en
relisant le modele depuis le disque (donc si quelque chose s'est mal
passe a la sauvegarde, ca se verra ici).

Usage :
    python3 verify_import.py Model.aird ServiceName
"""

import sys
import capellambse


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 verify_import.py <Model.aird> <NomDuService>")
        sys.exit(1)

    model_path, service_name = sys.argv[1], sys.argv[2]
    model = capellambse.MelodyModel(model_path)

    matches = model.search("Interface")
    matches = [i for i in matches if i.name == service_name]
    if not matches:
        print(f"AUCUNE Interface nommee '{service_name}' trouvee dans le modele.")
        sys.exit(1)

    interface = matches[0]
    print(f"Interface : {interface.name}  (uuid={interface.uuid})")
    print(f"  parent (package) : {interface.parent.name}")
    print(f"  operations ({len(interface.owned_features)}) :")

    for op in interface.owned_features:
        params = []
        for p in op.parameters:
            type_name = p.type.name if p.type is not None else "!! SANS TYPE !!"
            params.append(f"{p.name}:{p.direction}->{type_name}")
        print(f"    - {op.name} ({type(op).__name__})  [{', '.join(params)}]")

        try:
            cs = op.pvmt["Grpc.Streaming.ClientStreaming"]
            ss = op.pvmt["Grpc.Streaming.ServerStreaming"]
            print(f"        streaming: client={cs} server={ss}")
        except KeyError:
            print("        streaming: PVMT non renseigne (domaine/groupe absent)")

    print()
    print("Classes (messages) referencees, avec leurs champs :")
    seen = set()
    for op in interface.owned_features:
        for p in op.parameters:
            if p.type is not None and p.type.name not in seen:
                seen.add(p.type.name)
                fields = [f"{f.name}:{f.type.name if f.type else '!! SANS TYPE !!'}"
                          for f in p.type.owned_properties]
                print(f"  - {p.type.name}  [{', '.join(fields)}]")