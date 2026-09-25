# -*- coding: utf-8 -*-
"""
Import .proto -> Capella, via capellambse (pur Python 3, sans Capella
ni Java lances). Traite le fichier cible ET tous ses imports transitifs
(y compris les well-known types Google), en organisant les Classes/
Enumerations/Interfaces dans des sous-packages Capella qui reproduisent
l'arborescence de DOSSIERS de vos fichiers .proto (pas le nom de
fichier individuel : deux .proto dans le meme dossier partagent le
meme package Capella).

Usage :
    python3 import_proto_to_capella.py mon_service.proto /chemin/vers/Model.aird [--layer=la] [--proto-root=DOSSIER] [--strict-types]
    python3 import_proto_to_capella.py mon_dossier_protos/ /chemin/vers/Model.aird [--layer=la] [--strict-types]
        (mode arborescence : tous les .proto sous ce dossier, recursivement --
        --proto-root vaut ce dossier par defaut si non precise)

--layer : oa (Operational Analysis) / sa (System Analysis) /
          la (Logical Architecture, defaut) / pa (Physical Architecture)
--proto-root : dossier racine pour resoudre vos "import" relatifs
          imbriques (ex: import "service_base_api/ServiceB.proto").
          Sans lui, seul le dossier direct du fichier cible est
          utilisable -- OK pour un fichier isole, insuffisant sinon.
--strict-types : n'auto-cree PAS les DataTypes primitifs manquants ;
          arrete l'import si un type primitif necessaire n'existe pas.

Pre-requis modele Capella :
  - Un DataPkg et un InterfacePkg dans la couche choisie (racine sous
    laquelle les sous-packages par dossier sont crees automatiquement)
  - Domaine/groupe/type d'enumeration PVMT deja defini pour le
    streaming (cf. check_pvmt_ready)
  - Par defaut, les DataTypes primitifs manquants sont crees
    automatiquement au besoin -- desactivable avec --strict-types.
"""

import sys
import argparse
import capellambse
from grpc_tools import protoc
from google.protobuf import descriptor_pb2
import tempfile
import posixpath
import json
import os

from proto_capella_types import (
    pvmt_get,
    PVMT_WRITE_ERRORS,
    PROTO_TO_CAPELLA_PRIMITIVE,
    PVMT_STREAMING_MODE_KEY,
    PVMT_STREAMING_DOMAIN,
    PVMT_STREAMING_GROUP,
    PVMT_STREAMING_PROPERTY,
    PVMT_STREAMING_ENUM_TYPE,
    STREAMING_FLAGS_TO_MODE,
    PVMT_PACKAGE_KEY,
    PVMT_SOURCE_FILE_KEY,
    PVMT_HEADER_KEY,
    PVMT_COMMENT_STYLE_KEY,
    PVMT_ONEOF_KEY,
    PVMT_FIELD_NUMBER_KEY,
    PVMT_LAYOUT_KEY,
)
from setup_primitive_types import ensure_primitive_types


# --------------------------------------------------------------------
# 1. Parsing du/des .proto, multi-fichiers (le fichier cible + tous ses
#    imports transitifs, y compris les "well-known types" Google), avec
#    extraction des commentaires (SourceCodeInfo)
# --------------------------------------------------------------------

def _field_type_ref(field):
    """Reference de type d'un champ : nom qualifie complet pour un
    message/enum (ex: '.service_base_api.Y', utilise comme cle globale
    de resolution inter-fichiers), ou mot-cle primitif proto (ex:
    'int32') sinon."""
    if field.type in (
        descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        descriptor_pb2.FieldDescriptorProto.TYPE_ENUM,
    ):
        return field.type_name  # deja qualifie complet, ex: ".pkg.Type"
    return descriptor_pb2.FieldDescriptorProto.Type.Name(field.type).replace("TYPE_", "").lower()


def _clean_protoc_comment(text):
    """Repli (source brut illisible) : texte de protoc, debarrasse des
    artefacts connus -- '/' en tete de ligne pour '///', ligne '*' seule
    pour '/**'."""
    lines = [l[1:] if l.startswith("/") else l for l in text.splitlines()]
    lines = [l for l in lines if l.strip() != "*"]
    return "\n".join(l.strip() for l in lines).strip()


def _build_comment_index(file_proto, source_lines):
    """
    Construit deux dicts {tuple(path): texte} et {tuple(path): style} :
    protoc (SourceCodeInfo, loc.span) sert UNIQUEMENT a localiser la
    ligne de chaque element ; le commentaire est relu dans le source
    brut (source_lines) pour en conserver le style (cf. proto_comments).
    Commentaire au-dessus prioritaire sur le commentaire de fin de ligne.
    Encodage des chemins (cf. descriptor.proto) :
      [4, i] / [4, i, 2, j]  -> message / champ
      [5, i] / [5, i, 2, j]  -> enum / valeur
      [6, i] / [6, i, 2, j]  -> service / methode (rpc)
    Repli si source_lines est None : texte protoc nettoye, style "//".
    """
    texts, styles, layouts = {}, {}, {}
    for loc in file_proto.source_code_info.location:
        path = tuple(loc.path)
        # longueur paire = un element (message, champ, enum, valeur, service,
        # rpc), y compris imbrique : (4,i,3,k) message imbrique, (4,i,3,k,2,j) son champ.
        if not path or path[0] not in (4, 5, 6) or len(path) % 2:
            continue
        if source_lines is not None and loc.span:
            text, style, layout = _comment_and_layout(source_lines, loc.span)
            if path[0] == 6 and len(path) == 4:  # rpc : fin telle qu'ecrite (';' ou ' {}')
                end = loc.span[2] if len(loc.span) == 4 else loc.span[0]
                code = source_lines[end]
                cut = proto_comments._find_comment_start(code)
                code = (code[:cut] if cut >= 0 else code).rstrip()
                close = code.rfind(")")
                if close >= 0 and code[close + 1:].strip() not in ("", ";"):
                    layout["rpc_end"] = code[close + 1:]
            layouts[path] = layout
        else:
            raw = (loc.leading_comments or loc.trailing_comments or "").strip()
            text, style = _clean_protoc_comment(raw), proto_comments.DEFAULT_STYLE
        if text:
            texts[path] = text
            styles[path] = style or proto_comments.DEFAULT_STYLE
    return texts, styles, layouts


def _file_level_layout(file_proto, lines, layouts):
    """Mise en forme de niveau FICHIER (stockee sur chaque declaration de
    niveau fichier) :
      preamble  texte brut de 'syntax' jusqu'a la 1re declaration (imports,
                package, lignes 'option', commentaires, lignes vides) --
                reutilise tel quel a l'export si imports et package n'ont
                pas change, ce qui conserve aussi les 'option'
      indent    unite d'indentation du fichier (2 espaces, 4, tabulation...)"""
    if lines is None:
        return {}
    out = {}
    tops = [lay for p, lay in layouts.items() if len(p) == 2 and "top" in lay]
    syntax_line = next((loc.span[0] for loc in file_proto.source_code_info.location
                        if list(loc.path) == [12] and loc.span), None)
    if syntax_line is not None and tops:
        end = min(lay["top"] for lay in tops)
        region = [l.rstrip() for l in lines[syntax_line:end]]
        while region and not region[-1].strip():
            region.pop()
        out["preamble"] = "\n".join(region)
    for lay in sorted(tops, key=lambda l: l["order"]):
        for j in range(lay["order"] + 1, len(lines)):
            l = lines[j]
            if l.strip() and l.strip() != "}":
                ind = l[:len(l) - len(l.lstrip())]
                if ind:
                    out["indent"] = ind
                break
        if "indent" in out:
            break
    return out


def _comment_and_layout(lines, span):
    """Commentaire d'un element (texte propre + style) et sa MISE EN FORME
    d'origine, stockee dans le PVMT Layout pour une regeneration a
    l'identique :
      order    ligne de declaration (ordre des declarations dans le fichier
               et dans un message)
      pos      'leading' (au-dessus) ou 'trailing' (fin de ligne)
      raw      texte BRUT du commentaire, seulement si le rendu standard du
               style detecte ne le reproduit pas exactement (styles
               atypiques : marqueurs seuls sur leur ligne, pas d'espace...)
      col      colonne d'un commentaire de fin de ligne
      detached bloc de commentaire separe par une ligne vide (ex : texte de
               presentation du fichier, apres les imports) ; dgap = lignes
               vides entre ce bloc et l'element
      blank    lignes vides avant l'element (commentaires compris)"""
    start = span[0]
    end = span[2] if len(span) == 4 else span[0]
    layout = {"order": start}
    elem_indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]

    # Element precede de code sur sa ligne (ex: champ de
    # 'message A { int32 a = 1; }') : ni commentaire au-dessus (c'est celui
    # du message), ni commentaire de fin de ligne (idem), ni bloc detache.
    if lines[start][:span[1]].strip():
        return "", None, {"order": start}
    # declaration compacte sur une seule ligne : 'message A { int32 a = 1; }'
    if len(span) == 3 and "{" in lines[start]:
        layout["oneline"] = True

    first_line, raw_lead = proto_comments.leading_block(lines, start)
    text, style = "", None
    if raw_lead:
        text, style = proto_comments.clean_leading(raw_lead)
        layout["pos"] = "leading"
        if proto_comments.render_leading(text, style, elem_indent) != [l.rstrip() for l in raw_lead]:
            layout["raw"] = "\n".join(proto_comments.dedent(raw_lead))
    else:
        col, raw_t = proto_comments.trailing_raw(lines, end)
        if raw_t:
            text, style = proto_comments.text_of_raw_trailing(raw_t), None
            style = proto_comments._clean_trailing(raw_t)[1]
            layout["pos"], layout["col"] = "trailing", col
            if proto_comments.render_trailing(text, style) != raw_t:
                layout["raw"] = raw_t

    detached, dgap = proto_comments.detached_block(lines, first_line)
    top = first_line
    if detached:
        layout["detached"] = "\n".join(proto_comments.dedent(detached))
        layout["dgap"] = dgap
        top = first_line - dgap - len(detached)
    blank, k = 0, top - 1
    while k >= 0 and not lines[k].strip():
        blank += 1
        k -= 1
    layout["blank"] = blank
    layout["top"] = top - blank  # 1re ligne du bloc (lignes vides comprises) -- sert a delimiter l'en-tete
    return text, style, layout


def _extract_header_comment(file_proto, source_lines):
    """
    Cartouche d'en-tete : tout ce qui precede 'syntax = ...;', relu BRUT
    dans le source (marqueurs, bordures /****/ et ligne vide finale
    compris) pour une restitution a l'identique a l'export. La ligne de
    'syntax' est localisee via protoc (champ syntax, path [12]).
    Repli si le source est illisible : texte nettoye par protoc (ancien
    comportement -- l'export le reconnait et le prefixe de '// ').
    """
    syntax_line = None
    for loc in file_proto.source_code_info.location:
        if list(loc.path) == [12] and loc.span:
            syntax_line = loc.span[0]
            if source_lines is None:
                if loc.leading_comments:
                    return loc.leading_comments.strip()
                if loc.leading_detached_comments:
                    return "\n\n".join(c.strip() for c in loc.leading_detached_comments).strip()
                return ""
    if source_lines is None:
        return ""
    return proto_comments.extract_header(source_lines, syntax_line)


def _resolve_source_path(proto_name, include_dirs):
    """Chemin reel sur disque d'un file_proto (nom relatif a un -I), dans
    l'ordre de recherche de protoc. None si introuvable."""
    for d in include_dirs:
        candidate = os.path.join(d, proto_name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _read_source_lines(real_path):
    """Lignes du fichier source, ou None si illisible (-> repli sur le
    texte de protoc)."""
    if not real_path:
        return None
    try:
        with open(real_path, encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except OSError:
        return None


def _relative_source_path(file_proto_name, real_path, base_dir):
    """Chemin du fichier RELATIF A LA RACINE PROTO, en '/' -- determine
    le package Capella (dossier) et le SourceFile.
    Pourquoi pas file_proto.name : un import ecrit sans dossier entre
    fichiers voisins (import "serviceA.proto";) est resolu par protoc via
    le -I du dossier courant, et nomme 'serviceA.proto' (dossier vide).
    Le meme fichier importe directement s'appelle, lui,
    'soba_function_api/serviceA.proto' : sans correction, ses types
    seraient crees en double (racine + sous-package). On part donc du
    chemin REEL sur disque. Repli sur file_proto.name hors de la racine
    (ex: well-known types Google, dans le dossier de grpcio-tools)."""
    if real_path:
        rel = os.path.relpath(real_path, base_dir)
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    return file_proto_name


def _parse_message(msg, path, qual_prefix, comments, style, nested_enums, layout):
    """Message -> dict, RECURSIF pour les messages imbriques (dont les
    entrees de map, que protoc represente comme un message imbrique
    'NomEntry' avec l'option map_entry). Chemins SourceCodeInfo :
    champ = path+(2,j), message imbrique = path+(3,k)."""
    qual = f"{qual_prefix}.{msg.name}"
    oneof_names = [o.name for o in msg.oneof_decl]
    fields = []
    for j, f in enumerate(msg.field):
        # proto3 'optional' : protoc cree un oneof SYNTHETIQUE '_nom' --
        # ce n'est pas un vrai oneof, on le traduit en 'optional'.
        is_optional = bool(f.proto3_optional)
        oneof = None
        if f.HasField("oneof_index") and not is_optional:
            oneof = oneof_names[f.oneof_index]
        fields.append({
            "name": f.name, "number": f.number, "type": _field_type_ref(f),
            "repeated": f.label == descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
            "optional": is_optional,
            "oneof": oneof,
            "comment": comments.get(path + (2, j), ""), "comment_style": style(path + (2, j)), "layout": layout(path + (2, j)),
        })
    for k in range(len(msg.enum_type)):
        nested_enums.append(f"{qual}.{msg.enum_type[k].name}")
    return {
        "name": msg.name,
        "qualified_name": qual,
        "map_entry": bool(msg.options.map_entry),
        "fields": fields,
        "nested": [_parse_message(n, path + (3, k), qual, comments, style, nested_enums, layout)
                   for k, n in enumerate(msg.nested_type)],
        "comment": comments.get(path, ""), "comment_style": style(path), "layout": layout(path),
    }


import grpc_tools
import proto_comments


def parse_proto_file(proto_path, extra_include_dirs=None, proto_root=None):
    """
    Parse le fichier .proto cible ET tous ses imports transitifs (y
    compris les "well-known types" Google : google/protobuf/empty.proto,
    timestamp.proto, etc. -- inclus automatiquement, sans rien a fournir).

    proto_root : le repertoire racine par rapport auquel vos "import"
    relatifs se resolvent (ex: si votre .proto fait
    `import "service_base_api/ServiceB.proto";`, proto_root doit etre
    le dossier qui CONTIENT "service_base_api/"). Sans lui, seul le
    dossier direct du fichier cible est utilise -- suffisant pour un
    fichier isole, mais PAS pour des imports en chemin relatif imbrique.

    extra_include_dirs : repertoires -I supplementaires additionnels.

    Retourne {"files": [ {path, folder, package, messages, enums,
    services}, ... ]}, un element par fichier reellement compile (cible
    + imports transitifs), dans l'ordre topologique de protoc
    (dependances avant leurs dependants).
    """
    proto_path = os.path.abspath(proto_path)
    base_dir = os.path.abspath(proto_root) if proto_root else os.path.dirname(proto_path)
    well_known_types_dir = os.path.join(os.path.dirname(grpc_tools.__file__), "_proto")

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "descriptor.pb")
        include_dirs = []
        if proto_root:
            include_dirs.append(os.path.abspath(proto_root))
        include_dirs.append(os.path.dirname(proto_path))  # repli pour fichier isole
        include_dirs.append(well_known_types_dir)
        for extra_dir in (extra_include_dirs or []):
            include_dirs.append(os.path.abspath(extra_dir))
        args = ["protoc"] + ["-I" + d for d in include_dirs]
        args += ["--include_imports", "--include_source_info",
                  "--descriptor_set_out=" + out_path, proto_path]
        if protoc.main(args) != 0:
            raise RuntimeError("protoc a echoue sur : %s" % proto_path)

        fds = descriptor_pb2.FileDescriptorSet()
        with open(out_path, "rb") as f:
            fds.ParseFromString(f.read())

    files = []
    for file_proto in fds.file:
        real_path = _resolve_source_path(file_proto.name, include_dirs)
        source_lines = _read_source_lines(real_path)
        comments, comment_styles, layouts = _build_comment_index(file_proto, source_lines)
        style = lambda path: comment_styles.get(path, proto_comments.DEFAULT_STYLE)
        imports_as_written = list(file_proto.dependency)

        file_level = _file_level_layout(file_proto, source_lines, layouts)

        def layout(path):
            lay = dict(layouts.get(path, {}))
            if len(path) == 2 and lay:  # declaration de niveau fichier : + donnees du fichier
                lay["imports"] = imports_as_written
                lay.update(file_level)
            return lay
        rel_path = _relative_source_path(file_proto.name, real_path, base_dir)
        folder = posixpath.dirname(rel_path)  # ex: "service_base_api", "google/protobuf", ou "" (racine)
        qual_prefix = f".{file_proto.package}" if file_proto.package else ""
        nested_enums = []

        messages = [_parse_message(msg, (4, i), qual_prefix, comments, style, nested_enums, layout)
                    for i, msg in enumerate(file_proto.message_type)]

        # Enums de premier niveau seulement (pas les enums imbriques
        # dans un message -- limite connue, cf. README).
        enums = []
        for i, en in enumerate(file_proto.enum_type):
            values = []
            for j, v in enumerate(en.value):
                values.append({
                    "name": v.name, "number": v.number,
                    "comment": comments.get((5, i, 2, j), ""), "comment_style": style((5, i, 2, j)), "layout": layout((5, i, 2, j)),
                })
            enums.append({
                "name": en.name,
                "qualified_name": f".{file_proto.package}.{en.name}" if file_proto.package else f".{en.name}",
                "values": values,
                "comment": comments.get((5, i), ""), "comment_style": style((5, i)), "layout": layout((5, i)),
            })

        services = []
        for i, svc in enumerate(file_proto.service):
            methods = []
            for j, m in enumerate(svc.method):
                methods.append({
                    "name": m.name,
                    "input_type": m.input_type,   # deja qualifie complet
                    "output_type": m.output_type,  # idem
                    "client_streaming": bool(m.client_streaming),
                    "server_streaming": bool(m.server_streaming),
                    "comment": comments.get((6, i, 2, j), ""), "comment_style": style((6, i, 2, j)), "layout": layout((6, i, 2, j)),
                })
            services.append({
                "name": svc.name, "methods": methods,
                "comment": comments.get((6, i), ""), "comment_style": style((6, i)), "layout": layout((6, i)),
            })

        files.append({
            "path": rel_path,
            "folder": folder,
            "nested_enums": nested_enums,
            "package": file_proto.package or None,
            "header": _extract_header_comment(file_proto, source_lines),
            "messages": messages,
            "enums": enums,
            "services": services,
        })

    return {"files": files}


# --------------------------------------------------------------------
# 2. Resolution des types : primitifs proto -> DataType Capella, et
#    types message/enum -> Class/Enumeration deja creees (n'importe
#    quel fichier, cf. registre global "created_types" indexe par nom
#    qualifie complet)
# --------------------------------------------------------------------

def resolve_type(type_ref, root_data_pkg, created_types):
    """type_ref : soit un mot-cle primitif ('int32', 'string'...), soit
    un nom de type qualifie complet ('.pkg.Message') -- distingue les
    deux par la presence d'un '.' initial (convention protoc).
    root_data_pkg : le DataPkg RACINE de la couche (pas le sous-package
    par dossier) -- les DataTypes primitifs (String, Int32...) sont
    toujours crees/cherches a la racine, partages entre tous les
    modules proto, jamais dupliques par sous-package."""
    if type_ref.startswith("."):
        capella_type = created_types.get(type_ref)
        if capella_type is not None:
            return capella_type
        print("ATTENTION : type '%s' introuvable dans le registre global "
              "(message/enum non cree ?) : le champ/parametre restera "
              "sans type." % type_ref)
        return None
    capella_name = PROTO_TO_CAPELLA_PRIMITIVE.get(type_ref, type_ref)
    try:
        return root_data_pkg.data_types.by_name(capella_name)
    except KeyError:
        print("ATTENTION : type primitif '%s' (-> '%s') introuvable dans "
              "le DataPkg racine, le champ/parametre restera sans type." %
              (type_ref, capella_name))
        return None


def _set_literal_numeric(element, attr_name, value):
    """Pose min_card/max_card (ou min_value/max_value, meme mecanisme)
    sur un MultiplicityElement (ex: Property) -- necessite un vrai
    objet LiteralNumericValue, pas une simple chaine/entier (verifie
    par test direct : assigner une valeur brute leve
    "TypeError: Cannot create object from a single attribute").
    Idempotent : si deja pose, met a jour sa valeur plutot que d'en
    creer un second (le wrapper Single ne garantit l'unicite qu'a la
    LECTURE, pas a l'ecriture -- creer sans verifier dupliquerait)."""
    current = getattr(element, attr_name)
    if current is not None:
        current.value = str(value)
        return
    descriptor = getattr(type(element), attr_name)
    raw_list = descriptor.wrapped.__get__(element, type(element))
    raw_list.create("LiteralNumericValue", value=str(value))


def set_field_cardinality(prop, field):
    """Cardinalite d'une Property selon le champ proto :
      repeated (dont map)  -> 0..*
      optional (proto3)    -> 0..1
      champ simple         -> 1..1, UNIQUEMENT s'il portait deja une
                              cardinalite (ex: champ devenu simple entre
                              deux versions du .proto) -- sinon on ne cree
                              rien (cardinalite implicite, modele allege).
    L'export relit ces valeurs : max '*' -> repeated, 0..1 -> optional."""
    if field["repeated"]:
        _set_literal_numeric(prop, "min_card", 0)
        _set_literal_numeric(prop, "max_card", "*")
    elif field["optional"]:
        _set_literal_numeric(prop, "min_card", 0)
        _set_literal_numeric(prop, "max_card", 1)
    elif prop.min_card is not None or prop.max_card is not None:
        _set_literal_numeric(prop, "min_card", 1)
        _set_literal_numeric(prop, "max_card", 1)


def get_streaming_mode_literal(model, mode_name):
    """Recupere l'objet EnumerationPropertyLiteral pour un nom de mode
    (ex: 'BIDIR_STREAMING') -- une propriete PVMT d'enumeration exige
    l'OBJET litteral reel a l'affectation, pas une simple chaine (verifie
    par test direct : assigner une str leve InvalidModificationError)."""
    domain = model.pvmt.domains.by_name(PVMT_STREAMING_DOMAIN)
    enum_type = domain.enumeration_property_types.by_name(PVMT_STREAMING_ENUM_TYPE)
    return enum_type.literals.by_name(mode_name)


# --------------------------------------------------------------------
# 3. Creation des elements Capella
# --------------------------------------------------------------------

def _get_or_create(collection, name, typehint=None, **create_kwargs):
    """Cherche un element par nom dans la collection ; le cree s'il
    n'existe pas encore. Rend l'import IDEMPOTENT : relancer le script
    sur le meme .proto (ou une version modifiee) met a jour les
    elements existants au lieu de les dupliquer."""
    try:
        return collection.by_name(name), False  # (element, cree_maintenant)
    except KeyError:
        if typehint is not None:
            return collection.create(typehint, name=name, **create_kwargs), True
        return collection.create(name=name, **create_kwargs), True


def get_or_create_package_path(root_pkg, folder, cache):
    """Cree/retrouve (find-or-create, idempotent) la hierarchie de
    sous-packages Capella correspondant a un chemin de dossier proto
    (ex: 'service_base_api', ou 'google/protobuf' -> 2 niveaux imbriques).
    folder == "" (fichier a la racine, sans dossier) -> renvoie root_pkg
    tel quel, sans sous-package. Fonctionne identiquement pour un
    DataPkg ou un InterfacePkg (meme API .packages.by_name/.create).
    cache : dict partage entre appels (cle = chemin cumule), pour eviter
    de re-parcourir toute la hierarchie a chaque fichier."""
    if not folder:
        return root_pkg
    if folder in cache:
        return cache[folder]
    current = root_pkg
    accumulated = ""
    for part in folder.split("/"):
        accumulated = f"{accumulated}/{part}" if accumulated else part
        if accumulated in cache:
            current = cache[accumulated]
            continue
        try:
            current = current.packages.by_name(part)
        except KeyError:
            current = current.packages.create(name=part)
        cache[accumulated] = current
    return current


def import_proto_model(proto_model, data_pkg, interface_pkg, model):
    """
    data_pkg / interface_pkg : packages RACINE (ceux de la couche
    choisie). Les messages/enums/interfaces de chaque fichier sont
    places dans un sous-package miroir de son dossier (get_or_create_
    package_path), sous cette racine -- un fichier a la racine (sans
    dossier dans son chemin d'import) va directement dans data_pkg/
    interface_pkg tels quels, sans sous-package supplementaire.
    """
    created_types = {}   # nom qualifie complet -> Class ou Enumeration Capella (TOUS fichiers)
    stats = {"created": 0, "updated": 0}
    data_pkg_cache, interface_pkg_cache = {}, {}
    package_stored = False
    package_pvmt_missing = False
    source_file_stored = False
    source_file_pvmt_missing = False
    header_stored = False
    header_pvmt_missing = False
    comment_style_stored = False
    comment_style_pvmt_missing = False
    oneof_stored = False
    oneof_pvmt_missing = False
    number_stored = False
    number_pvmt_missing = False
    redefinitions = []
    layout_stored = False
    layout_pvmt_missing = False

    def set_source_file(element, path):
        """Pose PVMT_SOURCE_FILE_KEY sur un element (Class/Enumeration/
        Interface), optionnel -- met a jour les compteurs de synthese
        plutot que d'imprimer un avertissement par element."""
        nonlocal source_file_stored, source_file_pvmt_missing
        previous = pvmt_get(element, PVMT_SOURCE_FILE_KEY)
        if previous and previous != path:
            # meme nom, meme package, mais defini dans un AUTRE fichier :
            # redefinition (protoc refuserait les deux fichiers ensemble)
            redefinitions.append((element.name, previous, path))
        try:
            element.pvmt[PVMT_SOURCE_FILE_KEY] = path
            source_file_stored = True
        except PVMT_WRITE_ERRORS:
            source_file_pvmt_missing = True

    def set_comment_style(element, style):
        """Pose PVMT_COMMENT_STYLE_KEY (optionnel) -- appele seulement
        quand l'element a un commentaire."""
        nonlocal comment_style_stored, comment_style_pvmt_missing
        try:
            element.pvmt[PVMT_COMMENT_STYLE_KEY] = style
            comment_style_stored = True
        except PVMT_WRITE_ERRORS:
            comment_style_pvmt_missing = True

    def set_oneof(prop, oneof):
        """Pose PVMT_ONEOF_KEY sur un champ de oneof ; efface une valeur
        perimee si le champ n'est plus dans un oneof."""
        nonlocal oneof_stored, oneof_pvmt_missing
        if oneof:
            try:
                prop.pvmt[PVMT_ONEOF_KEY] = oneof
                oneof_stored = True
            except PVMT_WRITE_ERRORS:
                oneof_pvmt_missing = True
        else:
            # lecture SANS effet de bord : n'applique pas le groupe aux
            # champs qui n'en ont jamais eu besoin
            if pvmt_get(prop, PVMT_ONEOF_KEY):
                try:
                    prop.pvmt[PVMT_ONEOF_KEY] = ""
                except PVMT_WRITE_ERRORS:
                    pass

    def set_package(element, package):
        """PVMT Package aussi sur Class/Enumeration : l'export en a besoin
        pour qualifier un type d'un AUTRE package proto (pkg.Type)."""
        nonlocal package_stored, package_pvmt_missing
        if not package:
            return
        try:
            element.pvmt[PVMT_PACKAGE_KEY] = package
            package_stored = True
        except PVMT_WRITE_ERRORS:
            package_pvmt_missing = True

    def set_field_number(element, number):
        """Pose PVMT_FIELD_NUMBER_KEY (numero proto d'origine) sur un
        champ ou une valeur d'enum."""
        nonlocal number_stored, number_pvmt_missing
        try:
            element.pvmt[PVMT_FIELD_NUMBER_KEY] = str(number)
            number_stored = True
        except PVMT_WRITE_ERRORS:
            number_pvmt_missing = True

    def set_layout(element, layout):
        """Pose PVMT_LAYOUT_KEY (mise en forme d'origine, JSON)."""
        nonlocal layout_stored, layout_pvmt_missing
        if not layout:
            return
        try:
            element.pvmt[PVMT_LAYOUT_KEY] = json.dumps(layout, ensure_ascii=False)
            layout_stored = True
        except PVMT_WRITE_ERRORS:
            layout_pvmt_missing = True

    def set_header(element, header):
        """Pose PVMT_HEADER_KEY (cartouche licence/copyright), optionnel,
        seulement si le fichier en a effectivement un."""
        nonlocal header_stored, header_pvmt_missing
        if not header:
            return
        try:
            element.pvmt[PVMT_HEADER_KEY] = header
            header_stored = True
        except PVMT_WRITE_ERRORS:
            header_pvmt_missing = True

    # --- Controle informatif : le chemin de DOSSIER (qui determine le
    #     package Capella, cf. --proto-root) et la declaration "package"
    #     interne au .proto peuvent diverger -- ce n'est jamais bloquant
    #     (le dossier reste seul decisif pour la hierarchie Capella),
    #     mais c'est souvent le signe d'une organisation non
    #     conventionnelle ou d'un fichier importe "a plat" sans son
    #     dossier d'origine (cf. discussion : counter.proto importe tel
    #     quel vs range dans soba_template_api/).
    for file_info in proto_model["files"]:
        if file_info["package"] is None:
            continue
        expected_folder = file_info["package"].replace(".", "/")
        if file_info["folder"] != expected_folder:
            print(f"ATTENTION : '{file_info['path']}' declare "
                  f"'package {file_info['package']};' (attendu comme dossier : "
                  f"'{expected_folder}') mais son chemin de fichier reel est "
                  f"'{file_info['folder'] or '(racine, aucun dossier)'}'  -- le package "
                  f"Capella suivra le CHEMIN DE FICHIER, pas la declaration "
                  f"'package'. Rangez le fichier dans '{expected_folder}/' si "
                  f"vous voulez que les deux coincident.")

    # --- Passe 1 : Enumerations, sur TOUS les fichiers (cible + imports
    #     transitifs, y compris les well-known types Google) ---------
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)
        for en in file_info["enums"]:
            capella_enum, is_new = _get_or_create(target_data_pkg.enumerations, en["name"])
            set_layout(capella_enum, en["layout"])
            stats["created" if is_new else "updated"] += 1
            if en["comment"]:
                capella_enum.description = en["comment"]
                set_comment_style(capella_enum, en["comment_style"])
            set_source_file(capella_enum, file_info["path"])
            set_header(capella_enum, file_info["header"])
            set_package(capella_enum, file_info["package"])
            for value in en["values"]:
                lit, _ = _get_or_create(capella_enum.owned_literals, value["name"])
                set_layout(lit, value["layout"])
                set_field_number(lit, value["number"])
                if value["comment"]:
                    lit.description = value["comment"]
                    set_comment_style(lit, value["comment_style"])
            created_types[en["qualified_name"]] = capella_enum

    # --- Passe 2 : Classes (juste creees, sans les champs -- pour que
    #     TOUTES les classes de TOUS les fichiers soient disponibles
    #     avant de resoudre le moindre champ, y compris les references
    #     circulaires entre fichiers) -----------------------------------
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)
        def create_classes(messages, container):
            """container : .classes du package (niveau fichier) ou
            .nested_classes de la Class englobante (message imbrique,
            dont les entrees de map 'NomEntry')."""
            for msg in messages:
                capella_class, is_new = _get_or_create(container, msg["name"])
                set_layout(capella_class, msg["layout"])
                stats["created" if is_new else "updated"] += 1
                if msg["comment"]:
                    capella_class.description = msg["comment"]
                    set_comment_style(capella_class, msg["comment_style"])
                set_source_file(capella_class, file_info["path"])
                set_header(capella_class, file_info["header"])
                set_package(capella_class, file_info["package"])
                created_types[msg["qualified_name"]] = capella_class
                create_classes(msg["nested"], capella_class.nested_classes)

        create_classes(file_info["messages"], target_data_pkg.classes)
        for qn in file_info["nested_enums"]:
            print(f"ATTENTION : enum imbrique '{qn}' ({file_info['path']}) non gere "
                  f"(Capella ne permet pas une Enumeration dans une Class) -- les "
                  f"champs de ce type resteront sans type.")

    # --- Passe 3 : les champs de chaque Classe, maintenant que le
    #     registre global created_types est complet -----------------
    # known_message_names_by_pkg : uuid du package -> (objet package,
    # set des noms de Classe QU'ON VIENT DE TRAITER pour ce package,
    # union sur tous les fichiers qui y contribuent -- plusieurs
    # fichiers peuvent partager le meme dossier/package).
    known_message_names_by_pkg = {}
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)

        def create_fields(messages):
            for msg in messages:
                capella_class = created_types[msg["qualified_name"]]
                proto_field_names = {f["name"] for f in msg["fields"]}

                for field in msg["fields"]:
                    prop, _ = _get_or_create(capella_class.owned_properties, field["name"])
                    set_layout(prop, field["layout"])
                    if field["comment"]:
                        prop.description = field["comment"]
                        set_comment_style(prop, field["comment_style"])
                    field_type = resolve_type(field["type"], data_pkg, created_types)
                    if field_type is not None:
                        prop.type = field_type
                    set_field_cardinality(prop, field)
                    set_field_number(prop, field["number"])
                    set_oneof(prop, field["oneof"])

                orphan_fields = [p.name for p in capella_class.owned_properties
                                  if p.name not in proto_field_names]
                if orphan_fields:
                    print(f"INFO : Class '{msg['name']}' ({file_info['path']}) contient des "
                          f"champs absents de ce .proto (non supprimes) : {', '.join(orphan_fields)}")
                create_fields(msg["nested"])

        create_fields(file_info["messages"])

        if file_info["messages"]:
            pkg_obj, names = known_message_names_by_pkg.get(target_data_pkg.uuid, (target_data_pkg, set()))
            names.update(m["name"] for m in file_info["messages"])
            known_message_names_by_pkg[target_data_pkg.uuid] = (pkg_obj, names)

    for target_data_pkg, known_names in known_message_names_by_pkg.values():
        orphan_classes = [c.name for c in target_data_pkg.classes if c.name not in known_names]
        if orphan_classes:
            print(f"INFO : le package '{target_data_pkg.name}' contient d'autres Classes "
                  f"non references par cet import : {', '.join(orphan_classes)}")

    # --- Passe 4 : Interfaces/Services, sur TOUS les fichiers (pas
    #     seulement le fichier cible -- un fichier importe qui declare
    #     lui-meme un service voit aussi son Interface creee ; idempotent,
    #     donc reimporter ce fichier plus tard directement ne duplique rien) --
    for file_info in proto_model["files"]:
        target_data_pkg = get_or_create_package_path(data_pkg, file_info["folder"], data_pkg_cache)
        target_interface_pkg = get_or_create_package_path(interface_pkg, file_info["folder"], interface_pkg_cache)

        for svc in file_info["services"]:
            capella_interface, is_new = _get_or_create(target_interface_pkg.interfaces, svc["name"])
            set_layout(capella_interface, svc["layout"])
            stats["created" if is_new else "updated"] += 1
            if svc["comment"]:
                capella_interface.description = svc["comment"]
                set_comment_style(capella_interface, svc["comment_style"])
            set_source_file(capella_interface, file_info["path"])
            set_header(capella_interface, file_info["header"])

            if file_info["package"]:
                try:
                    capella_interface.pvmt[PVMT_PACKAGE_KEY] = file_info["package"]
                    package_stored = True
                except PVMT_WRITE_ERRORS:
                    package_pvmt_missing = True

            proto_method_names = {m["name"] for m in svc["methods"]}

            for method in svc["methods"]:
                operation, op_is_new = _get_or_create(
                    capella_interface.owned_features, method["name"], typehint="Service")
                set_layout(operation, method["layout"])
                if method["comment"]:
                    operation.description = method["comment"]
                    set_comment_style(operation, method["comment_style"])

                # google.protobuf.Empty (et tout autre type, custom ou
                # Google) est desormais un VRAI type resolu via le
                # registre global -- plus de cas special "pas de
                # Parameter" : Empty est juste un Class avec 0 champ,
                # cree comme n'importe quel autre message importe.
                in_type = resolve_type(method["input_type"], data_pkg, created_types)
                in_param, _ = _get_or_create(operation.parameters, "request", direction="IN")
                if in_type is not None:
                    in_param.type = in_type

                out_type = resolve_type(method["output_type"], data_pkg, created_types)
                out_param, _ = _get_or_create(operation.parameters, "response", direction="OUT")
                if out_type is not None:
                    out_param.type = out_type

                # Streaming -> PVMT, une seule propriete d'enumeration
                mode_name = STREAMING_FLAGS_TO_MODE[(method["client_streaming"], method["server_streaming"])]
                try:
                    literal = get_streaming_mode_literal(model, mode_name)
                    operation.pvmt[PVMT_STREAMING_MODE_KEY] = literal
                except PVMT_WRITE_ERRORS:
                    print("ATTENTION : domaine/groupe/enumeration PVMT '%s' introuvable "
                          "(ou litteral '%s' absent), streaming non renseigne pour '%s'." %
                          (PVMT_STREAMING_MODE_KEY, mode_name, method["name"]))

            orphan_methods = [f.name for f in capella_interface.owned_features
                               if type(f).__name__ == "Service" and f.name not in proto_method_names]
            if orphan_methods:
                print(f"INFO : Interface '{svc['name']}' contient des operations absentes "
                      f"de ce .proto (non supprimees) : {', '.join(orphan_methods)}")

    if layout_pvmt_missing:
        domain_name, group_name = PVMT_LAYOUT_KEY.split(".")[0:2]
        print(f"INFO : mise en forme d'origine NON stockee -- le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'Layout' (optionnel) : "
              f"l'export ne pourra pas reproduire l'ordre des declarations, les "
              f"commentaires atypiques ni le commentaire de presentation du fichier.")
    if redefinitions:
        pairs = sorted({(old, new) for _, old, new in redefinitions})
        print(f"ATTENTION : {len(redefinitions)} element(s) deja definis dans un AUTRE fichier "
              f"du meme package ont ete REPRIS par ce fichier (meme nom, meme dossier) :")
        for old, new in pairs:
            names = [n for n, o, nw in redefinitions if (o, nw) == (old, new)]
            print(f"    '{old}' -> '{new}' : {', '.join(names)}")
        print("    Ces fichiers ne peuvent pas coexister dans un meme build proto (symboles en "
              "double). A l'export, les elements repris ne ressortiront que dans le dernier "
              "fichier importe. Supprimez l'un des deux fichiers, ou placez-les dans des "
              "dossiers/packages distincts.")
    if number_pvmt_missing:
        domain_name, group_name = PVMT_FIELD_NUMBER_KEY.split(".")[0:2]
        print(f"ATTENTION : numeros de champ NON stockes -- le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'FieldNumber' : "
              f"l'export renumerotera les champs 1, 2, 3... (compatibilite binaire "
              f"cassee si l'original a des trous ou un autre ordre).")
    if oneof_stored:
        print(f"INFO : appartenance aux oneof stockee via PVMT ({PVMT_ONEOF_KEY}).")
    if oneof_pvmt_missing:
        domain_name, group_name = PVMT_ONEOF_KEY.split(".")[0:2]
        print(f"ATTENTION : des champs appartiennent a un 'oneof' mais le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'Oneof' : ces champs "
              f"seront exportes comme des champs simples (exclusivite perdue).")
    if comment_style_stored:
        print(f"INFO : style(s) de commentaire stocke(s) via PVMT ({PVMT_COMMENT_STYLE_KEY}).")
    if comment_style_pvmt_missing:
        domain_name, group_name = PVMT_COMMENT_STYLE_KEY.split(".")[0:2]
        print(f"INFO : style(s) de commentaire NON stocke(s) -- le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'CommentStyle' "
              f"(optionnel) : l'export utilisera '//' partout.")
    if header_stored:
        print(f"INFO : cartouche(s) d'en-tete stocke(s) via PVMT ({PVMT_HEADER_KEY}).")
    if header_pvmt_missing:
        domain_name, group_name = PVMT_HEADER_KEY.split(".")[0:2]
        print(f"INFO : cartouche(s) d'en-tete NON stocke(s) -- le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'FileHeader' dans ce "
              f"modele (optionnel, cf. proto_capella_types.py).")
    if source_file_stored:
        print(f"INFO : chemin de fichier d'origine stocke via PVMT ({PVMT_SOURCE_FILE_KEY}) "
              f"sur les Classes/Enumerations/Interfaces -- utilise par --output-root a "
              f"l'export pour regenerer l'emplacement exact.")
    if source_file_pvmt_missing:
        domain_name, group_name = PVMT_SOURCE_FILE_KEY.split(".")[0:2]
        print(f"INFO : chemin de fichier d'origine NON stocke -- le groupe PVMT "
              f"'{domain_name}.{group_name}' n'a pas de propriete 'SourceFile' dans ce "
              f"modele (optionnel, cf. proto_capella_types.py). --output-root utilisera "
              f"alors le nom de l'element Capella comme nom de fichier, pas le nom exact "
              f"d'origine.")

    packages_used = sorted({fi["folder"] for fi in proto_model["files"] if fi["folder"]})
    if packages_used:
        print(f"INFO : packages Capella crees/reutilises (miroir des dossiers proto) : "
              f"{', '.join(packages_used)}")
    any_package_stmt = any(fi["package"] for fi in proto_model["files"])
    if any_package_stmt:
        if package_stored:
            print(f"INFO : package(s) proto stocke(s) via PVMT ({PVMT_PACKAGE_KEY}) sur "
                  f"les Interfaces concernees.")
        if package_pvmt_missing:
            domain_name, group_name = PVMT_PACKAGE_KEY.split(".")[0:2]
            print(f"INFO : package(s) proto NON stocke(s) -- le groupe PVMT "
                  f"'{domain_name}.{group_name}' n'existe pas dans ce modele (optionnel, "
                  f"cf. proto_capella_types.py). Repassez-le a l'export avec --package si besoin.")

    print(f"Bilan : {stats['created']} element(s) cree(s), "
          f"{stats['updated']} element(s) deja existant(s) mis a jour.")
    return created_types


# --------------------------------------------------------------------
# 4. Point d'entree
# --------------------------------------------------------------------


def check_pvmt_ready(model):
    """
    Controle prealable : si le domaine/groupe/type d'enumeration PVMT
    n'existe pas, on arrete tout de suite avec des instructions claires.
    capellambse ne permet pas de le creer par script (limite volontaire
    de la lib).
    """
    try:
        domain = model.pvmt.domains.by_name(PVMT_STREAMING_DOMAIN)
        domain.groups.by_name(PVMT_STREAMING_GROUP)
        domain.enumeration_property_types.by_name(PVMT_STREAMING_ENUM_TYPE)
        return True
    except KeyError:
        print(f"""
ARRET : la structure PVMT '{PVMT_STREAMING_MODE_KEY}' n'existe pas encore
dans ce modele. Le mode de streaming gRPC ne pourra pas etre enregistre
tant qu'elle n'est pas creee.

A faire UNE FOIS dans Capella (PV Definition Editor) :
  1. Domain                : {PVMT_STREAMING_DOMAIN}
  2. Enumeration type       : {PVMT_STREAMING_ENUM_TYPE}, avec 4 litteraux :
                              UNARY, CLIENT_STREAMING, SERVER_STREAMING,
                              BIDIR_STREAMING
  3. Group                  : {PVMT_STREAMING_GROUP}
  4. Property (dans le Group), type {PVMT_STREAMING_ENUM_TYPE} : {PVMT_STREAMING_PROPERTY}
  5. Scope : couche(s) ou vivent vos interfaces (Logical/Physical...)

Relancez ce script une fois cette structure creee.
""")
        return False


LAYER_CHOICES = {"oa": "Operational Analysis", "sa": "System Analysis",
                  "la": "Logical Architecture", "pa": "Physical Architecture"}


def get_layer(model, layer_code):
    """Recupere la couche (model.oa / model.sa / model.la / model.pa)
    a partir du code court passe en argument."""
    if layer_code not in LAYER_CHOICES:
        raise ValueError(f"--layer doit etre l'un de : {', '.join(LAYER_CHOICES)}")
    return getattr(model, layer_code)


def referenced_primitive_capella_names(proto_model):
    """Noms Capella des types primitifs reellement references par les
    champs de TOUS les fichiers (cible + imports). Les types message/
    enum ne sont pas concernes, ils deviennent des Classes/Enumerations
    creees par l'import lui-meme, sur n'importe quel fichier."""
    names = set()
    for file_info in proto_model["files"]:
        for msg in file_info["messages"]:
            for field in msg["fields"]:
                if field["type"] in PROTO_TO_CAPELLA_PRIMITIVE:
                    names.add(PROTO_TO_CAPELLA_PRIMITIVE[field["type"]])
    return names


def check_types_ready(data_pkg, proto_model):
    """Mode --strict-types : n'auto-cree rien, arrete l'import si un
    type primitif necessaire manque. NB : les types primitifs restent
    tous crees dans le DataPkg RACINE (pas dans les sous-packages par
    dossier), quel que soit le fichier qui les utilise -- ce sont des
    types partages, pas specifiques a un module proto."""
    needed = referenced_primitive_capella_names(proto_model)
    missing = []
    for name in sorted(needed):
        try:
            data_pkg.data_types.by_name(name)
        except KeyError:
            missing.append(name)
    if missing:
        print(f"""
ARRET (--strict-types) : les DataTypes primitifs suivants sont requis
par ce .proto (ou ses imports) mais absents du DataPkg : {', '.join(missing)}

Lancez 'python setup_primitive_types.py {{Model.aird}} --layer=...'
pour les creer, ou retirez --strict-types pour laisser l'import les
creer automatiquement.
""")
        return False
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import .proto -> Capella")
    parser.add_argument("proto_path", help="Fichier .proto, OU dossier a parcourir "
                         "recursivement pour importer tous les .proto qu'il contient "
                         "(mode arborescence, detecte automatiquement).")
    parser.add_argument("model_path", help="Chemin vers le fichier .aird du modele Capella")
    parser.add_argument("--layer", default="la", choices=list(LAYER_CHOICES),
                         help="Couche d'architecture cible (defaut: la = Logical Architecture). "
                              "Choix : " + ", ".join(f"{k}={v}" for k, v in LAYER_CHOICES.items()))
    parser.add_argument("--proto-root", default=None,
                         help="Dossier racine par rapport auquel vos directives 'import' "
                              "relatives se resolvent (ex: si vos protos font "
                              "'import \"service_base_api/ServiceB.proto\";', --proto-root "
                              "doit etre le dossier qui CONTIENT service_base_api/). En mode "
                              "arborescence (proto_path est un dossier), --proto-root vaut "
                              "par defaut ce dossier lui-meme si non precise -- c'est "
                              "generalement ce que vous voulez.")
    parser.add_argument("--strict-types", action="store_true",
                         help="N'auto-cree pas les DataTypes primitifs manquants ; "
                              "arrete l'import si l'un d'eux est absent (defaut : "
                              "auto-creation idempotente, avec message informatif).")
    args = parser.parse_args()

    if os.path.isdir(args.proto_path):
        proto_files = []
        for root, _dirs, filenames in os.walk(args.proto_path):
            for fn in sorted(filenames):
                if fn.endswith(".proto"):
                    proto_files.append(os.path.join(root, fn))
        proto_files.sort()
        if not proto_files:
            print(f"ERREUR : aucun fichier .proto trouve sous '{args.proto_path}'.")
            sys.exit(1)
        proto_root = args.proto_root or args.proto_path
        print(f"INFO : mode arborescence -- {len(proto_files)} fichier(s) .proto trouve(s) "
              f"sous '{args.proto_path}' (proto-root='{proto_root}') :")
        for pf in proto_files:
            print(f"    {pf}")
    else:
        proto_files = [args.proto_path]
        proto_root = args.proto_root

    # --- Phase 1 : validation de TOUS les fichiers par protoc, AVANT
    #     d'ouvrir le modele. Un fichier invalide n'interrompt plus
    #     l'import au milieu d'une arborescence : toutes les erreurs sont
    #     listees d'un coup, et le modele n'est pas touche. protoc ecrit
    #     ses erreurs lui-meme (chemin:ligne:colonne) juste au-dessus.
    print("\nValidation des fichiers .proto (protoc)...", flush=True)
    parsed, invalid = [], []
    for proto_path in proto_files:
        try:
            parsed.append((proto_path, parse_proto_file(proto_path, proto_root=proto_root)))
        except RuntimeError:
            invalid.append(proto_path)
    if invalid:
        print(f"\nARRET : {len(invalid)} fichier(s) .proto invalide(s) (erreurs protoc "
              f"ci-dessus) -- modele NON modifie :")
        for pf in invalid:
            print(f"    {pf}")
        print("Corrigez ces fichiers puis relancez. Rappel : un type d'un AUTRE package "
              "proto doit etre qualifie (ex: routeguide.Point), meme si son fichier est "
              "dans le meme dossier.")
        sys.exit(1)
    print(f"OK : {len(parsed)} fichier(s) valide(s).")

    # --- Phase 2 : import dans le modele
    model = capellambse.MelodyModel(args.model_path)

    if not check_pvmt_ready(model):
        sys.exit(1)

    layer = get_layer(model, args.layer)
    data_pkg = layer.data_pkg
    interface_pkg = layer.interface_pkg

    all_created = {}
    total_services = 0
    total_files_processed = 0

    for proto_path, proto_model in parsed:
        print(f"\n=== {proto_path} ===")

        if args.strict_types:
            if not check_types_ready(data_pkg, proto_model):
                sys.exit(1)
        else:
            types_created, types_existing = ensure_primitive_types(data_pkg)
            if types_created:
                print(f"INFO : DataTypes primitifs crees automatiquement : "
                      f"{', '.join(types_created)}")

        created = import_proto_model(proto_model, data_pkg, interface_pkg, model)
        all_created.update(created)
        total_services += sum(len(fi["services"]) for fi in proto_model["files"])
        total_files_processed += len(proto_model["files"])

    model.save()
    print("\nImport termine dans %s : %d fichier(s) .proto traite(s) (cibles + imports "
          "transitifs confondus), %d classe(s)/enum(s) au total, %d service(s) crees/mis "
          "a jour." % (LAYER_CHOICES[args.layer], total_files_processed,
                        len(all_created), total_services))