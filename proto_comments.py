# -*- coding: utf-8 -*-
"""
Detection et rendu des STYLES de commentaires .proto, partage entre
l'import et l'export pour garantir la symetrie.

Pourquoi lire le source brut : protoc (SourceCodeInfo) ne conserve pas
la syntaxe des commentaires, et en degrade meme le texte (verifie par
test direct) :
  - '/// texte'           -> '/ texte'   (slash parasite)
  - '/** ... */' javadoc  -> '*' + texte (etoile parasite)
  - '/* l1 */' '/* l2 */' -> seule la derniere ligne est conservee
On n'utilise donc protoc que pour LOCALISER les elements (numero de
ligne via loc.span) ; le commentaire est relu dans le fichier source.

Styles reconnus (valeur stockee dans le PVMT CommentStyle) :
  "//"           // texte              (defaut)
  "///"          /// texte
  "block"        /* texte              (bloc unique englobant les lignes)
                    suite */
  "javadoc"      /**
                  * texte
                  */
  "boxed"        /* ligne 1  */        (un /* */ par ligne, alignes)
                 /* ligne 2  */
  "boxed_border" idem "boxed" + lignes de bordure /*****/ avant/apres
"""

DEFAULT_STYLE = "//"
KNOWN_STYLES = ("//", "///", "block", "javadoc", "boxed", "boxed_border")


# ------------------------------------------------------------------
# Detection (import)
# ------------------------------------------------------------------

def _is_border(inner):
    """Ligne de bordure d'un encadre : uniquement des '*' (au moins 3)."""
    s = inner.strip()
    return len(s) >= 3 and set(s) == {"*"}


def _find_comment_start(line):
    """Index du debut d'un commentaire (// ou /*) dans une ligne de code,
    en ignorant le contenu des chaines entre guillemets. -1 si aucun."""
    in_str = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif line.startswith("//", i) or line.startswith("/*", i):
            return i
        i += 1
    return -1


def _block_start(lines, j):
    """Index de la ligne ouvrant ('/*') le bloc qui se ferme en ligne j, ou
    None. Le bloc doit commencer EN DEBUT DE LIGNE (sans code avant) : une
    ligne 'int32 b = 2;   /* fin de ligne */' se termine aussi par '*/',
    mais c'est un commentaire de fin de ligne du champ b, pas un
    commentaire place au-dessus de l'element suivant."""
    k = j
    while k >= 0:
        if "/*" in lines[k]:
            return k if lines[k].strip().startswith("/*") else None
        k -= 1
    return None


def leading_block(lines, elem_line):
    """Lignes BRUTES du commentaire colle au-dessus de elem_line (0-based) :
    (index_de_debut, [lignes]) ou (elem_line, []) s'il n'y en a pas.
    Regroupe des lignes // successives, ou des blocs /* */ successifs
    (un bloc multi-lignes, ou plusieurs /* */ d'une ligne = encadre)."""
    i = elem_line - 1
    if i < 0 or not lines[i].strip():
        return elem_line, []
    s = lines[i].strip()
    if s.startswith("//"):
        j = i
        while j >= 0 and lines[j].strip().startswith("//"):
            j -= 1
        return j + 1, lines[j + 1:elem_line]
    if s.endswith("*/"):
        j = i
        start = None
        while j >= 0 and lines[j].strip().endswith("*/"):
            k = _block_start(lines, j)
            if k is None:
                break
            start = k
            j = k - 1
            # on ne regroupe que des blocs ADJACENTS (pas de ligne vide)
            if j < 0 or not lines[j].strip():
                break
        if start is None:
            return elem_line, []
        return start, lines[start:elem_line]
    return elem_line, []


def clean_leading(raw_lines):
    """Texte propre + style d'un commentaire brut (liste de lignes).
    Style = celui dont le rendu s'en approche le plus ; si le rendu ne
    reproduit pas le brut a l'identique, l'appelant conserve le brut."""
    block = [l.rstrip() for l in raw_lines]
    stripped = [l.strip() for l in block]
    if not block:
        return "", None

    if all(l.startswith("//") for l in stripped):
        style = "///" if all(l.startswith("///") for l in stripped) else "//"
        text = []
        for l in stripped:
            t = l[len(style):]
            text.append(t[1:] if t.startswith(" ") else t)
        return "\n".join(t.rstrip() for t in text).strip("\n"), style

    # encadre : plusieurs /* ... */ complets, un par ligne
    if len(stripped) > 1 and all(l.startswith("/*") and l.endswith("*/") and len(l) >= 4
                                 for l in stripped):
        inners = [l[2:-2] for l in stripped]
        has_border = _is_border(inners[0]) and _is_border(inners[-1])
        if has_border:
            inners = inners[1:-1]
        text = "\n".join(x.strip() for x in inners if not _is_border(x))
        return text, ("boxed_border" if has_border else "boxed")

    # bloc /* ... */ (une ou plusieurs lignes, marqueurs n'importe ou)
    body = "\n".join(stripped)
    is_javadoc = body.startswith("/**") and not body.startswith("/**/")
    body = body[3:] if is_javadoc else body[2:]
    if body.endswith("*/"):
        body = body[:-2]
    lines_ = [l.strip() for l in body.split("\n")]
    inner = [l for l in lines_ if l]
    # lignes prefixees par '*' (style javadoc, meme avec une ouverture '/*')
    if inner and all(l.startswith("*") for l in inner):
        lines_ = [(l[1:][1:] if l[1:].startswith(" ") else l[1:]) if l.startswith("*") else l
                  for l in lines_]
    while lines_ and not lines_[0].strip():
        lines_.pop(0)
    while lines_ and not lines_[-1].strip():
        lines_.pop()
    return "\n".join(l.rstrip() for l in lines_), ("javadoc" if is_javadoc else "block")


def extract_leading(lines, elem_line):
    """Commentaire colle AU-DESSUS de la ligne elem_line (0-based).
    Retourne (texte_propre, style) ou ("", None)."""
    _, raw = leading_block(lines, elem_line)
    if not raw:
        return "", None
    text, style = clean_leading(raw)
    return (text, style) if text else ("", None)


def detached_block(lines, first_line):
    """Commentaire(s) DETACHE(S) au-dessus d'un element : separes de lui
    (ou de son commentaire colle, qui commence en first_line) par au moins
    une ligne vide, et remontant jusqu'a la ligne de code precedente
    (package, import, '}'...). Ex : le bloc de presentation d'un fichier,
    place apres les imports. Retourne (lignes_brutes, nb_lignes_vides_entre
    ce bloc et l'element) ou ([], 0)."""
    i = first_line - 1
    if i < 0 or lines[i].strip():
        return [], 0
    gap = 0
    while i >= 0 and not lines[i].strip():
        gap += 1
        i -= 1
    end = i
    collected_start = None
    while i >= 0:
        s = lines[i].strip()
        if not s:
            i -= 1
            continue
        if s.startswith("//"):
            collected_start = i
            i -= 1
            continue
        if s.endswith("*/"):
            k = _block_start(lines, i)
            if k is None:
                break
            collected_start = k
            i = k - 1
            continue
        break
    if collected_start is None or end < 0:
        return [], 0
    return [l.rstrip() for l in lines[collected_start:end + 1]], gap


def dedent(raw_lines):
    """Retire l'indentation commune (conservee relative), pour stocker un
    commentaire brut independamment de sa profondeur d'imbrication."""
    widths = [len(l) - len(l.lstrip()) for l in raw_lines if l.strip()]
    cut = min(widths) if widths else 0
    return [l[cut:] if l.strip() else "" for l in raw_lines]


def reindent(raw_text, indent):
    return [f"{indent}{l}" if l.strip() else "" for l in raw_text.split("\n")]


def text_of_raw_leading(raw_text):
    """Texte propre d'un commentaire brut stocke (pour detecter si la
    description Capella a ete modifiee depuis l'import)."""
    return clean_leading(raw_text.split("\n"))[0]


def extract_trailing(lines, elem_end_line):
    """Commentaire en FIN de la ligne elem_end_line (0-based), apres le
    code. Retourne (texte_propre, style) ou ("", None)."""
    if elem_end_line < 0 or elem_end_line >= len(lines):
        return "", None
    line = lines[elem_end_line]
    idx = _find_comment_start(line)
    if idx < 0 or not line[:idx].strip():
        return "", None  # pas de code avant : ce n'est pas un commentaire de fin de ligne
    return _clean_trailing(line[idx:].rstrip())


def _clean_trailing(c):
    if c.startswith("///"):
        return c[3:].strip(), "///"
    if c.startswith("//"):
        return c[2:].strip(), "//"
    inner = c[2:-2] if c.endswith("*/") else c[2:]
    return inner.strip(), "block"


def trailing_raw(lines, elem_end_line):
    """(colonne, texte_brut) du commentaire de fin de ligne, ou (None, "")."""
    if elem_end_line < 0 or elem_end_line >= len(lines):
        return None, ""
    line = lines[elem_end_line]
    idx = _find_comment_start(line)
    if idx < 0 or not line[:idx].strip():
        return None, ""
    return idx, line[idx:].rstrip()


def text_of_raw_trailing(raw):
    return _clean_trailing(raw)[0]


def extract_header(lines, syntax_line):
    """Cartouche d'en-tete BRUT (marqueurs compris), = tout ce qui
    precede la ligne 'syntax = ...;', lignes vides finales comprises
    (elles determinent si le cartouche est colle ou separe de syntax).
    Retourne "" si rien d'autre que des lignes vides."""
    if syntax_line is None or syntax_line <= 0:
        return ""
    raw = lines[:syntax_line]
    if not any(l.strip() for l in raw):
        return ""
    return "\n".join(l.rstrip() for l in raw)


# ------------------------------------------------------------------
# Rendu (export)
# ------------------------------------------------------------------

def render_leading(text, style, indent=""):
    """Lignes de commentaire placees AU-DESSUS d'un element."""
    text = (text or "").strip("\n")
    if not text.strip():
        return []
    style = style if style in KNOWN_STYLES else DEFAULT_STYLE
    raw_lines = [l.rstrip() for l in text.splitlines()]

    if style in ("//", "///"):
        return [f"{indent}{style} {l}" if l else f"{indent}{style}" for l in raw_lines]

    if style == "block":
        if len(raw_lines) == 1:
            return [f"{indent}/* {raw_lines[0]} */"]
        out = [f"{indent}/* {raw_lines[0]}"]
        out += [f"{indent}   {l}" if l else "" for l in raw_lines[1:-1]]
        out.append(f"{indent}   {raw_lines[-1]} */")
        return out

    if style == "javadoc":
        out = [f"{indent}/**"]
        out += [f"{indent} * {l}" if l else f"{indent} *" for l in raw_lines]
        out.append(f"{indent} */")
        return out

    # boxed / boxed_border : un /* */ par ligne, fermetures alignees
    width = max(len(l) for l in raw_lines)
    out = []
    border = f"{indent}/*{'*' * (width + 2)}*/"
    if style == "boxed_border":
        out.append(border)
    out += [f"{indent}/* {l.ljust(width)} */" for l in raw_lines]
    if style == "boxed_border":
        out.append(border)
    return out


def render_trailing(text, style):
    """Commentaire de fin de ligne (texte sur une seule ligne)."""
    text = (text or "").strip()
    if style == "///":
        return f"/// {text}"
    if style in ("block", "javadoc", "boxed", "boxed_border"):
        return f"/* {text} */"
    return f"// {text}"


def render_header(stored):
    """Cartouche : stocke BRUT depuis cette version (marqueurs compris)
    -> restitue a l'identique. Compatibilite avec les modeles importes
    avant : si aucune ligne ne porte de marqueur de commentaire, c'est
    l'ancien format (texte nettoye) -> on le prefixe de '// ' et on le
    separe de 'syntax' par une ligne vide, comme auparavant."""
    if not stored:
        return []
    # split("\n") et non splitlines() : une ligne vide FINALE (cartouche
    # separe de 'syntax' par une ligne blanche) doit etre conservee.
    lines = stored.split("\n")
    markers = ("//", "/*", "*")
    if any(l.strip().startswith(markers) for l in lines):
        return lines
    return [f"// {l}" if l else "//" for l in lines] + [""]