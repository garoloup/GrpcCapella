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


def extract_leading(lines, elem_line):
    """Commentaire colle AU-DESSUS de la ligne elem_line (0-based).
    Retourne (texte_propre, style) ou ("", None)."""
    i = elem_line - 1
    if i < 0 or not lines[i].strip():
        return "", None
    last = lines[i].strip()

    # --- Commentaires // ou /// sur lignes successives ---------------
    if last.startswith("//"):
        block = []
        while i >= 0 and lines[i].strip().startswith("//"):
            block.insert(0, lines[i].strip())
            i -= 1
        style = "///" if all(l.startswith("///") for l in block) else "//"
        prefix = len(style)
        text = [l[prefix:][1:] if l[prefix:].startswith(" ") else l[prefix:] for l in block]
        return "\n".join(t.rstrip() for t in text).strip("\n"), style

    if not last.endswith("*/"):
        return "", None

    # --- Encadre : un /* ... */ complet par ligne ---------------------
    if last.startswith("/*"):
        boxed = []
        j = i
        while j >= 0:
            s = lines[j].strip()
            if s.startswith("/*") and s.endswith("*/") and len(s) >= 4:
                boxed.insert(0, s)
                j -= 1
            else:
                break
        if len(boxed) > 1:
            inners = [b[2:-2] for b in boxed]
            has_border = _is_border(inners[0]) and _is_border(inners[-1])
            if has_border:
                inners = inners[1:-1]
            text = "\n".join(x.strip() for x in inners if not _is_border(x))
            return text, ("boxed_border" if has_border else "boxed")
        # un seul /* ... */ sur une ligne : bloc simple sur une ligne
        inner = last[2:-2]
        if inner.startswith("*"):  # /** texte */
            inner = inner[1:]
        return inner.strip(), "block"

    # --- Bloc /* ... */ englobant plusieurs lignes --------------------
    block = []
    j = i
    while j >= 0:
        block.insert(0, lines[j].rstrip())
        if "/*" in lines[j]:
            break
        j -= 1
    else:
        return "", None
    first = block[0].strip()
    is_javadoc = first.startswith("/**")
    body = []
    for k, l in enumerate(block):
        s = l.strip()
        if k == 0:
            s = s[3:] if is_javadoc else s[2:]
        if k == len(block) - 1:
            s = s[:-2] if s.endswith("*/") else s
        s = s.rstrip()
        if is_javadoc and s.lstrip().startswith("*"):
            s = s.lstrip()[1:]
            s = s[1:] if s.startswith(" ") else s
        body.append(s.strip() if not is_javadoc else s.rstrip())
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(b.strip() for b in body), ("javadoc" if is_javadoc else "block")


def extract_trailing(lines, elem_end_line):
    """Commentaire en FIN de la ligne elem_end_line (0-based), apres le
    code. Retourne (texte_propre, style) ou ("", None)."""
    if elem_end_line < 0 or elem_end_line >= len(lines):
        return "", None
    line = lines[elem_end_line]
    idx = _find_comment_start(line)
    if idx < 0 or not line[:idx].strip():
        return "", None  # pas de code avant : ce n'est pas un commentaire de fin de ligne
    c = line[idx:].rstrip()
    if c.startswith("///"):
        return c[3:].strip(), "///"
    if c.startswith("//"):
        return c[2:].strip(), "//"
    inner = c[2:-2] if c.endswith("*/") else c[2:]
    return inner.strip(), "block"


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