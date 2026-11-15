#!/usr/bin/env python3
"""Jardinier des dossiers : entretient un dépôt git de dossiers Markdown (un fichier topics/<slug>.md par sujet,
en-tête YAML avec slug, status, updated, aliases, links) comme un graphe.

Ce que fait une passe :
  - rend les liens [[ ]] réciproques (A cite B -> B cite A dans son en-tête `links`) ;
  - répare les liens cassés quand la cible se retrouve sans doute (redirection, alias, nom approchant) ;
  - relie chaque dossier isolé au dossier le plus proche (choix confirmé par Claude) ;
  - fusionne les doublons sans rien perdre (tout le contenu passe dans le dossier gardé, l'ancien slug devient un alias,
    l'ancien fichier devient une page de redirection dans archives/redirections/) ;
  - classe clos (status: done) les dossiers périmés, sans jamais rien effacer ;
  - régénère l'index par le générateur du dépôt (scripts/gen-index.py) ;
  - vérifie avant tout push qu'aucune ligne de contenu n'a disparu.
Claude ne touche jamais aux fichiers : il reçoit une liste courte de candidats et rend des décisions JSON ; ce programme
les applique, de façon déterministe et vérifiée.

Commandes (DEPOT = racine d'une copie de travail du dépôt des dossiers) :
  jardinier.py analyser DEPOT SORTIE.json          graphe, réparations sûres et candidats à juger
  jardinier.py consigne SORTIE.json CONSIGNE.md     texte à donner à Claude (stdout)
  jardinier.py appliquer DEPOT SORTIE.json [DECISIONS.json] [--max-fusions N] [--max-clos N] [--date AAAA-MM-JJ]
                                                     modifie les fichiers et commite (un commit par dossier)
  jardinier.py verifier DEPOT REF_AVANT             aucune ligne perdue entre REF_AVANT et HEAD (code 0 = rien perdu)
  jardinier.py rapport RESULTAT.json                quelques lignes pour le rapport du matin (stdout)
"""
import datetime, difflib, glob, json, math, os, re, subprocess, sys, unicodedata
from collections import Counter, defaultdict

import yaml

LIEN = re.compile(r"\[\[([^\]\|#\n]+)((?:[#|][^\]\n]*)?)\]\]")
DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b|\b(\d\d)/(\d\d)/(20\d\d)\b")
CLOS = ("done", "parked", "redirect")
SECTIONS_PROJETEES = ("Pending", "RDV", "Alarme")   # sections lues par le projecteur (tâches, agenda, alarmes)
MOTS_VIDES = set("""a au aux avec ce ces cet cette d dans de des du elle en est et il ils je l la le les leur lui ma mais me mes
mon ne nos notre nous on ou où par pas pour qu que qui sa se ses si son sur ta te tes ton tu un une vos votre vous y à été être
fait faire plus sans tout tous toute toutes aussi comme the of and to in for on with is are be by an or at from this that it
dossier dossiers etat état actuel log pending notes rdv""".split())


# ------------------------------------------------------------------------------------------------ lecture des fichiers
def _lire(p):
    with open(p, encoding="utf-8") as h:
        return h.read()


def _ecrire(p, t):
    with open(p, "w", encoding="utf-8") as h:
        h.write(t)


def lire(path):
    t = _lire(path)
    if not t.startswith("---"):
        return None
    brut, sep, corps = t[3:].partition("\n---")
    if not sep:
        return None
    repare = False
    try:
        fm = yaml.safe_load(brut) or {}
    except yaml.YAMLError:
        brut2 = reparer_entete(brut)
        try:
            fm = yaml.safe_load(brut2) or {}
        except yaml.YAMLError:
            return None
        brut, repare = brut2, True
    if not isinstance(fm, dict):
        return None
    return {"texte": t, "brut": brut, "corps": corps, "fm": fm, "entete_repare": repare}


def reparer_entete(brut):
    """En-tête YAML illisible à cause d'une valeur en clair contenant « : » (souvent la description) : on met la
    valeur entre guillemets, sans en changer un caractère."""
    out = []
    for l in brut.split("\n"):
        m = re.match(r"^([A-Za-z_][\w-]*): (.+)$", l)
        if m and ": " in m.group(2) and m.group(2)[:1] not in "[{'\"|>":
            l = f"{m.group(1)}: " + json.dumps(m.group(2), ensure_ascii=False)
        out.append(l)
    return "\n".join(out)


def en_liste(v):
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if x not in (None, "")]
    v = str(v).strip().strip("[]")
    return [x.strip().strip("'\"") for x in v.split(",") if x.strip()]


def cible(lien):
    return str(lien).strip().strip("'\"").strip("[]").strip()


def plat(t):
    t = unicodedata.normalize("NFKD", str(t).lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in t if not unicodedata.combining(c)))


def mots(t):
    t = unicodedata.normalize("NFKD", str(t).lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return [m for m in re.findall(r"[a-z0-9]{3,}", t) if m not in MOTS_VIDES and not m.isdigit()]


def section(corps, titre):
    m = re.search(r"^## " + re.escape(titre) + r"[^\n]*\n(.*?)(?=^## |\Z)", corps, re.S | re.M)
    return m.group(1) if m else ""


def charger(depot):
    """Tous les dossiers de topics/ + les noms valides ailleurs dans le dépôt (journaux, préférences, archives)."""
    dossiers = {}
    illisibles = []
    for p in sorted(glob.glob(os.path.join(depot, "topics", "*.md"))):
        nom = os.path.basename(p)[:-3]
        if nom.startswith("_"):
            continue
        d = lire(p)
        if d is None:
            illisibles.append(nom)
            continue
        fm = d["fm"]
        slug = str(fm.get("slug") or nom)
        d.update(slug=nom, chemin=p, status=str(fm.get("status", "active")), type=str(fm.get("type", "")),
                 updated=str(fm.get("updated", ""))[:10], aliases=en_liste(fm.get("aliases")),
                 liens_entete=[cible(x) for x in en_liste(fm.get("links"))],
                 liens_corps=[m.group(1).strip() for m in LIEN.finditer(d["corps"])],
                 description=str(fm.get("description", "") or ""))
        d["slug_declare"] = slug
        dossiers[nom] = d
    autres = {}   # nom -> chemin relatif, pour les fichiers hors topics/ qui sont des cibles de lien valides
    for p in glob.glob(os.path.join(depot, "**", "*.md"), recursive=True):
        r = os.path.relpath(p, depot)
        if r.startswith(("topics/", ".git/")) or "/.git/" in r:
            continue
        autres.setdefault(os.path.basename(p)[:-3], r)
    redirections = {}
    for p in glob.glob(os.path.join(depot, "archives", "redirections", "*.md")):
        d = lire(p)
        if d:
            liens = [cible(x) for x in en_liste(d["fm"].get("links"))] or [m.group(1).strip() for m in LIEN.finditer(d["corps"])]
            if liens:
                redirections[os.path.basename(p)[:-3]] = liens[0]
    return dossiers, autres, redirections, illisibles


# ------------------------------------------------------------------------------------------------ résolution des liens
class Resolveur:
    def __init__(self, dossiers, autres, redirections):
        self.dossiers, self.autres, self.redirections = dossiers, autres, redirections
        self.par_plat = {}
        self.par_alias = defaultdict(set)
        for s, d in dossiers.items():
            self.par_plat.setdefault(plat(s), s)
            self.par_plat.setdefault(plat(re.sub(r"^(project|reference|feedback|user)_", "", s)), s)
            if d["slug_declare"] != s:
                self.par_plat.setdefault(plat(d["slug_declare"]), s)
            for a in d["aliases"]:
                self.par_alias[plat(a)].add(s)

    def valide(self, c):
        return c in self.dossiers or c in self.autres

    def resoudre(self, c):
        """Cible sûre d'un lien cassé, ou None. Seulement des correspondances sans ambiguïté."""
        c = c[:-3] if c.endswith(".md") else c
        vu = set()
        while c in self.redirections and c not in vu and c not in self.dossiers:
            vu.add(c)
            c = self.redirections[c]
        if c in self.dossiers:
            return c
        for essai in (c, c.replace("-", "_"), "project_" + c, "reference_" + c, "project_" + c.replace("-", "_"),
                      "reference_" + c.replace("-", "_")):
            if essai in self.dossiers:
                return essai
        p = plat(c)
        if p in self.par_plat:
            return self.par_plat[p]
        if len(self.par_alias.get(p, ())) == 1:
            return next(iter(self.par_alias[p]))
        autres = [n for n in self.autres if plat(n) == p]   # préférence ou journal écrit avec des tirets
        if len(autres) == 1:
            return autres[0]
        return None

    def proches(self, c, n=5):
        noms = list(self.dossiers)
        return difflib.get_close_matches(c, noms, n=n, cutoff=0.4)


# ------------------------------------------------------------------------------------------------ similarité
def vecteurs(dossiers):
    docs = {}
    for s, d in dossiers.items():
        etat = section(d["corps"], "État actuel")[:3000]
        txt = " ".join([s.replace("_", " ")] * 3 + d["aliases"] * 2 + [d["description"]] * 2 + [etat])
        docs[s] = Counter(mots(txt))
    df = Counter()
    for c in docs.values():
        df.update(c.keys())
    n = len(docs) or 1
    vec = {}
    for s, c in docs.items():
        v = {m: (1 + math.log(k)) * math.log(n / df[m]) for m, k in c.items() if df[m] < n * 0.5}
        norme = math.sqrt(sum(x * x for x in v.values())) or 1
        vec[s] = {m: x / norme for m, x in v.items()}
    return vec


def cosinus(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(m, 0) for m, x in a.items())


# ------------------------------------------------------------------------------------------------ analyse
def dates_futures(corps, aujourdhui):
    for t in SECTIONS_PROJETEES:
        for ligne in section(corps, t).splitlines():
            if ligne.strip().startswith("- ~~") or "~~" in ligne[:6]:
                continue
            for m in DATE.finditer(ligne):
                try:
                    j = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m.group(1) else \
                        datetime.date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
                except ValueError:
                    continue
                if j >= aujourdhui:
                    return True
    return False


def derniere_activite(d, aujourdhui):
    """La date la plus récente parmi `updated` et les dates écrites dans le dossier (Log, État actuel), sans futur."""
    vus = []
    try:
        vus.append(datetime.date.fromisoformat(d["updated"]))
    except ValueError:
        pass
    for m in DATE.finditer(d["corps"]):
        try:
            j = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m.group(1) else \
                datetime.date(int(m.group(6)), int(m.group(5)), int(m.group(4)))
        except ValueError:
            continue
        if j <= aujourdhui:
            vus.append(j)
    return max(vus) if vus else aujourdhui


def analyser(depot, aujourdhui=None, jours_perime=60, seuil_doublon=0.45, seuil_isole=0.08):
    aujourdhui = aujourdhui or datetime.date.today()
    dossiers, autres, redirections, illisibles = charger(depot)
    R = Resolveur(dossiers, autres, redirections)
    # 1. liens cassés : réparations sûres, puis à juger
    reparations, a_juger_liens = [], []
    for s, d in dossiers.items():
        for c in sorted(set(d["liens_entete"]) | set(d["liens_corps"])):
            if not c or R.valide(c):
                continue
            r = R.resoudre(c)
            if r and r != s:
                reparations.append({"dossier": s, "cible": c, "remplacer_par": r, "pourquoi": "cible retrouvée"})
            elif r != s:
                a_juger_liens.append({"dossier": s, "cible": c, "proches": R.proches(c)})
    corrige = {(x["dossier"], x["cible"]): x["remplacer_par"] for x in reparations}
    # 2. graphe entre dossiers (après réparation sûre)
    sortants = defaultdict(set)
    for s, d in dossiers.items():
        for c in set(d["liens_entete"]) | set(d["liens_corps"]):
            c = corrige.get((s, c), c)
            if c in dossiers and c != s:
                sortants[s].add(c)
    entrants = defaultdict(set)
    for s, cs in sortants.items():
        for c in cs:
            entrants[c].add(s)
    reciproques = []
    for s, cs in sorted(sortants.items()):
        for c in sorted(cs):
            if s not in sortants[c]:
                reciproques.append({"dossier": c, "ajouter": s})
    # 3. dossiers isolés (hors redirections)
    vec = vecteurs(dossiers)
    actifs = [s for s, d in dossiers.items() if d["status"] != "redirect"]
    isoles = []
    for s in actifs:
        if sortants[s] or entrants[s]:
            continue
        scores = sorted(((cosinus(vec[s], vec[o]), o) for o in actifs if o != s), reverse=True)[:4]
        cands = [o for sc, o in scores if sc >= seuil_isole]
        if cands:
            isoles.append({"dossier": s, "proches": cands, "scores": [round(sc, 3) for sc, o in scores[:len(cands)]]})
    # 4. doublons possibles (deux dossiers non clos, très proches, ou alias communs)
    doublons, vus = [], set()
    ouverts = [s for s in actifs if dossiers[s]["status"] not in CLOS]
    for i, a in enumerate(ouverts):
        for b in ouverts[i + 1:]:
            sc = cosinus(vec[a], vec[b])
            communs = {plat(x) for x in dossiers[a]["aliases"]} & {plat(x) for x in dossiers[b]["aliases"]}
            communs.discard("")
            nom_croise = plat(a) in {plat(x) for x in dossiers[b]["aliases"]} or plat(b) in {plat(x) for x in dossiers[a]["aliases"]}
            if sc >= seuil_doublon or len(communs) >= 2 or nom_croise:
                doublons.append({"a": a, "b": b, "score": round(sc, 3), "alias_communs": sorted(communs)[:6]})
                vus.add((a, b))
    doublons.sort(key=lambda x: -x["score"])
    doublons = doublons[:12]
    # 5. dossiers périmés (actifs, pas touchés depuis longtemps, rien de prévu)
    perimes = []
    for s in ouverts:
        d = dossiers[s]
        if d["status"] != "active" or d["type"] in ("user", "feedback") or s.startswith(("user_", "feedback_")):
            continue
        age = (aujourdhui - derniere_activite(d, aujourdhui)).days
        if age >= jours_perime and not dates_futures(d["corps"], aujourdhui):
            recents = sorted(o for o in entrants[s] | sortants[s] if dossiers[o]["status"] not in CLOS
                             and (aujourdhui - derniere_activite(dossiers[o], aujourdhui)).days < 30)
            perimes.append({"dossier": s, "jours": age, "voisins_actifs_recents": recents[:6]})
    perimes.sort(key=lambda x: -x["jours"])
    perimes = perimes[:15]
    entetes = [s for s, d in dossiers.items() if d.get("entete_repare")]
    slugs = sorted(s for s in actifs)
    return {"date": aujourdhui.isoformat(), "nb_dossiers": len(dossiers), "illisibles": illisibles, "entetes": entetes, "slugs": slugs,
            "reparations": reparations, "liens_a_juger": a_juger_liens[:40], "reciproques": reciproques,
            "isoles": isoles, "doublons": doublons, "perimes": perimes}


# ------------------------------------------------------------------------------------------------ consigne pour Claude
def fiche(d, n=700):
    etat = section(d["corps"], "État actuel").strip()
    return {"slug": d["slug"], "status": d["status"], "updated": d["updated"], "description": d["description"][:300],
            "aliases": ", ".join(d["aliases"])[:250], "etat_actuel": etat[:n]}


def consigne(depot, analyse, gabarit):
    dossiers, *_ = charger(depot)
    besoin = set()
    for x in analyse["isoles"]:
        besoin |= {x["dossier"], *x["proches"]}
    for x in analyse["doublons"]:
        besoin |= {x["a"], x["b"]}
    for x in analyse["perimes"]:
        besoin.add(x["dossier"])
    for x in analyse["liens_a_juger"]:
        besoin.add(x["dossier"])
    candidats = {
        "liens_casses": [{"dossier": x["dossier"], "cible": x["cible"], "proches": x["proches"]} for x in analyse["liens_a_juger"]],
        "isoles": [{"dossier": x["dossier"], "proches": x["proches"]} for x in analyse["isoles"]],
        "doublons": [{"a": x["a"], "b": x["b"]} for x in analyse["doublons"]],
        "perimes": [{"dossier": x["dossier"], "jours_sans_mise_a_jour": x["jours"],
                     "relie_a_des_dossiers_actifs_recents": x.get("voisins_actifs_recents", [])} for x in analyse["perimes"]],
    }
    fiches = [fiche(dossiers[s], 500 if s not in {y["a"] for y in analyse["doublons"]} | {y["b"] for y in analyse["doublons"]} else 1200)
              for s in sorted(besoin) if s in dossiers]
    catalogue = "\n".join(f"- {s} : {(dossiers[s]['description'] or ', '.join(dossiers[s]['aliases']))[:110]}"
                          for s in analyse.get("slugs", []) if s in dossiers) if analyse["isoles"] else "(aucun isolé)"
    return (gabarit.replace("{{DATE}}", analyse["date"]).replace("{{CATALOGUE}}", catalogue)
            .replace("{{CANDIDATS}}", json.dumps(candidats, ensure_ascii=False, indent=1))
            .replace("{{FICHES}}", json.dumps(fiches, ensure_ascii=False, indent=1)))


def lire_decisions(texte, analyse):
    """Décisions de Claude, filtrées : seuls les slugs proposés sont acceptés."""
    m = re.search(r"\{.*\}", texte or "", re.S)
    if not m:
        return {}
    try:
        brut = json.loads(m.group(0))
    except ValueError:
        return {}
    ok = {"liens_casses": [], "isoles": [], "doublons": [], "perimes": []}
    lc = {(x["dossier"], x["cible"]): set(x["proches"]) for x in analyse["liens_a_juger"]}
    for x in brut.get("liens_casses", []) or []:
        k = (x.get("dossier"), x.get("cible"))
        if k in lc and x.get("remplacer_par") in lc[k]:
            ok["liens_casses"].append({"dossier": k[0], "cible": k[1], "remplacer_par": x["remplacer_par"],
                                       "pourquoi": str(x.get("pourquoi", ""))[:200]})
    iso = {x["dossier"]: set(analyse.get("slugs", [])) - {x["dossier"]} or set(x["proches"]) for x in analyse["isoles"]}
    for x in brut.get("isoles", []) or []:
        if x.get("dossier") in iso and x.get("relier_a") in iso[x["dossier"]]:
            ok["isoles"].append({"dossier": x["dossier"], "relier_a": x["relier_a"], "pourquoi": str(x.get("pourquoi", ""))[:200]})
    db = {frozenset((x["a"], x["b"])) for x in analyse["doublons"]}
    for x in brut.get("doublons", []) or []:
        p = frozenset((x.get("a"), x.get("b")))
        if p in db and x.get("doublon") is True and x.get("garder") in p:
            autre = next(iter(p - {x["garder"]}))
            ok["doublons"].append({"garder": x["garder"], "absorber": autre, "pourquoi": str(x.get("pourquoi", ""))[:200]})
    pe = {x["dossier"] for x in analyse["perimes"]}
    for x in brut.get("perimes", []) or []:
        if x.get("dossier") in pe and x.get("clore") is True:
            ok["perimes"].append({"dossier": x["dossier"], "pourquoi": str(x.get("pourquoi", ""))[:200]})
    return ok


# ------------------------------------------------------------------------------------------------ écriture
def liste_flow(v):
    return yaml.safe_dump(v, default_flow_style=True, allow_unicode=True, width=100000).strip()


def remplacer_cle(brut, cle, ligne):
    """Remplace la clé `cle` de l'en-tête brut (forme en ligne ou liste multi-ligne) ; l'ajoute à la fin sinon."""
    lignes = brut.split("\n")
    out, i, fait = [], 0, False
    while i < len(lignes):
        l = lignes[i]
        if re.match(r"^" + re.escape(cle) + r"\s*:", l):
            i += 1
            while i < len(lignes) and (lignes[i].startswith((" ", "\t")) or lignes[i].strip().startswith("- ")) and lignes[i].strip():
                i += 1
            out.append(ligne)
            fait = True
            continue
        out.append(l)
        i += 1
    if not fait:
        while out and not out[-1].strip():
            out.pop()
        out.append(ligne)
        out.append("")
    return "\n".join(out)


class Fichier:
    def __init__(self, chemin):
        self.chemin = chemin
        d = lire(chemin)
        self.brut, self.corps, self.fm = d["brut"], d["corps"], d["fm"]
        self.changements = []

    def liens(self):
        return [cible(x) for x in en_liste(self.fm.get("links"))]

    def poser(self, cle, valeur):
        self.fm[cle] = valeur
        if isinstance(valeur, list):
            self.brut = remplacer_cle(self.brut, cle, f"{cle}: {liste_flow(valeur)}")
        else:
            self.brut = remplacer_cle(self.brut, cle, f"{cle}: {valeur}")

    def ajouter_lien(self, slug):
        ls = self.liens()
        if slug in ls:
            return False
        self.poser("links", [f"[[{x}]]" for x in ls + [slug]])
        return True

    def remplacer_lien(self, ancien, nouveau):
        n = 0
        ls = self.liens()
        soi = os.path.basename(self.chemin)[:-3]
        if ancien in ls:
            nv = []
            for x in ls:
                y = nouveau if x == ancien else x
                if y not in nv and y != soi:   # jamais de lien vers soi-même dans l'en-tête
                    nv.append(y)
            self.poser("links", [f"[[{x}]]" for x in nv])
            n += 1

        def sub(m):
            return f"[[{nouveau}{m.group(2)}]]" if m.group(1).strip() == ancien else m.group(0)
        corps = LIEN.sub(sub, self.corps)
        if corps != self.corps:
            self.corps = corps
            n += 1
        return n > 0

    def ajouter_alias(self, valeurs):
        als = en_liste(self.fm.get("aliases"))
        connus = {plat(a) for a in als}
        ajout = [v for v in valeurs if v and plat(v) not in connus and not connus.add(plat(v))]
        if ajout:
            self.poser("aliases", als + ajout)
        return ajout

    def ajouter_log(self, ligne):
        m = re.search(r"^## Log[^\n]*\n", self.corps, re.M)
        if not m:
            self.corps = self.corps.rstrip("\n") + "\n\n## Log\n" + ligne + "\n"
            return
        sec = section(self.corps, "Log")
        dates = []
        for l in sec.splitlines():
            dm = DATE.search(l[:20])
            if dm:
                dates.append(dm.group(0))
        def cle(x):
            mm = DATE.match(x)
            return (mm.group(1), mm.group(2), mm.group(3)) if mm.group(1) else (mm.group(6), mm.group(5), mm.group(4))
        recent_en_tete = len(dates) < 2 or cle(dates[0]) >= cle(dates[-1])
        if recent_en_tete:
            self.corps = self.corps[:m.end()] + ligne + "\n" + self.corps[m.end():]
        else:
            fin = m.end() + len(sec.rstrip("\n"))
            self.corps = self.corps[:fin] + "\n" + ligne + self.corps[fin:]

    def ecrire(self):
        _ecrire(self.chemin, "---" + self.brut + "\n---" + self.corps)


def git(depot, *a, check=True):
    return subprocess.run(["git", "-C", depot, *a], capture_output=True, text=True, check=check).stdout


_SUJET = None


def nommeur(depot):
    """Le dépôt peut fournir son propre nom lisible des dossiers (scripts/commit-par-fichier.py, fonction sujet)."""
    global _SUJET
    p = os.path.join(os.path.abspath(depot), "scripts", "commit-par-fichier.py")
    if _SUJET is None and os.path.exists(p):
        import importlib.util
        ici = os.getcwd()
        try:
            os.chdir(os.path.abspath(depot))
            spec = importlib.util.spec_from_file_location("cpf", p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _SUJET = lambda slug: m.sujet("dossier:" + slug)
        except Exception:
            _SUJET = False
        finally:
            os.chdir(ici)
    return _SUJET


def humain(slug):
    if _SUJET:
        try:
            n = str(_SUJET(slug)).replace(":", " ").strip()
            if n:
                return n[:78]
        except Exception:
            pass
    s = re.sub(r"^(project|reference|feedback|user)_", "", slug).replace("_", " ").replace("-", " ").strip()
    s = s[:1].upper() + s[1:]
    return s[:70].replace(":", " ")


def commiter(depot, chemins, titre):
    presents = [c for c in chemins if os.path.exists(os.path.join(depot, c))]
    if presents:
        git(depot, "add", "-A", "--", *presents)
    for c in chemins:
        if c not in presents:
            git(depot, "rm", "-q", "--cached", "--ignore-unmatch", "--", c)
    if not git(depot, "diff", "--cached", "--name-only").strip():
        return None
    titre = titre[:118]
    git(depot, "commit", "-q", "-m", titre + "\n\nPassage du jardinier des dossiers.")
    return git(depot, "rev-parse", "--short", "HEAD").strip()


def fr(d):
    return datetime.date.fromisoformat(d).strftime("%d/%m/%Y")


def fusionner(depot, garder, absorber, pourquoi, date):
    """Tout le contenu de `absorber` passe dans `garder` ; `absorber` devient une redirection. Deux commits."""
    pa = os.path.join(depot, "topics", garder + ".md")
    pb = os.path.join(depot, "topics", absorber + ".md")
    A, B = Fichier(pa), Fichier(pb)
    # alias : l'ancien slug et tous ses alias
    A.ajouter_alias([absorber] + en_liste(B.fm.get("aliases")))
    for l in B.liens():
        if l not in (garder, absorber):
            A.ajouter_lien(l)
    # sections projetées (tâches, agenda, alarmes) : leurs lignes rejoignent la même section du dossier gardé
    corps_b = B.corps
    for t in SECTIONS_PROJETEES:
        sec = section(corps_b, t)
        if not sec.strip():
            continue
        corps_b = re.sub(r"^## " + re.escape(t) + r"[^\n]*\n" + re.escape(sec), "", corps_b, count=1, flags=re.M)
        lignes = sec.strip("\n")
        if re.search(r"^## " + re.escape(t) + r"\b", A.corps, re.M):
            m = re.search(r"^## " + re.escape(t) + r"[^\n]*\n", A.corps, re.M)
            exist = section(A.corps, t)
            fin = m.end() + len(exist.rstrip("\n"))
            A.corps = A.corps[:fin] + ("\n" if exist.strip() else "") + lignes + "\n" + A.corps[fin:]
        else:
            A.corps = A.corps.rstrip("\n") + f"\n\n## {t}\n{lignes}\n"
    # le reste du corps, entier, en note datée (titres décalés de deux niveaux)
    reste = re.sub(r"^(#{1,4}) ", lambda m: "#" * min(6, len(m.group(1)) + 2) + " ", corps_b.strip("\n"), flags=re.M)
    desc = str(B.fm.get("description", "") or "").strip()
    note = (f"### {fr(date)} — Fusionné depuis {absorber}\n**En un mot** : dossier « {absorber} » fusionné ici par le "
            f"jardinier des dossiers ({pourquoi}). Contenu repris en entier ci-dessous ; historique : "
            f"`git log -- topics/{absorber}.md`.\n" + (f"Description d'origine : {desc}\n" if desc else "") + "\n" + reste + "\n")
    if re.search(r"^## Notes\b", A.corps, re.M):
        A.corps = A.corps.rstrip("\n") + "\n\n" + note
    else:
        A.corps = A.corps.rstrip("\n") + "\n\n## Notes\n" + note
    A.ajouter_log(f"- {date} : dossier [[{absorber}]] fusionné ici par le jardinier des dossiers ({pourquoi}).")
    A.poser("updated", date)
    A.ecrire()
    jA = os.path.join("journal", garder + "-journal.md")
    c1 = commiter(depot, [os.path.relpath(pa, depot)], f"{humain(garder)} : absorbe le dossier {absorber} (jardinier)")
    # la page de redirection
    os.makedirs(os.path.join(depot, "archives", "redirections"), exist_ok=True)
    pr = os.path.join(depot, "archives", "redirections", absorber + ".md")
    fm = dict(B.fm)
    entete = [f"slug: {absorber}", f"type: {fm.get('type', 'project')}", "status: redirect", f"updated: {date}",
              f"aliases: {liste_flow(en_liste(fm.get('aliases')))}", f"links: {liste_flow(['[[' + garder + ']]'])}",
              "description: " + json.dumps("REDIRECTION — fusionné dans " + garder + " le " + fr(date) + " par le jardinier des dossiers.", ensure_ascii=False)]
    texte = ("---\n" + "\n".join(entete) + "\n---\n\n# " + humain(absorber) + " → " + garder + " (page de redirection)\n\n"
             "## État actuel\n\n" f"- Fusionné le {fr(date)} dans [[{garder}]] ({pourquoi}) : tout le contenu y est repris "
             f"(sections Pending/RDV/Alarme dans les mêmes sections, le reste dans la note « Fusionné depuis {absorber} »).\n"
             f"- Ancien contenu complet : `git log -- topics/{absorber}.md`.\n")
    git(depot, "mv", os.path.relpath(pb, depot), os.path.relpath(pr, depot))
    _ecrire(pr, texte)
    c2 = commiter(depot, [os.path.relpath(pb, depot), os.path.relpath(pr, depot)],
                  f"{humain(absorber)} : fusionné dans {garder} (jardinier)")
    return [c for c in (c1, c2) if c]


def appliquer(depot, analyse, decisions, max_fusions=3, max_clos=5, date=None):
    date = date or analyse["date"]
    nommeur(depot)
    res = {"date": date, "commits": [], "reciproques": 0, "dossiers_reciproques": 0, "liens_repares": [],
           "isoles": [], "fusions": [], "clos": [], "non_repares": [], "index": None, "illisibles": analyse["illisibles"]}
    dossiers, *_ = charger(depot)
    # 1. fusions d'abord (les autres changements suivent les noms d'après fusion)
    absorbes = {}
    for f in (decisions.get("doublons") or [])[:max_fusions]:
        g, a = f["garder"], f["absorber"]
        if g in absorbes or a in absorbes or g not in dossiers or a not in dossiers:
            continue
        res["commits"] += fusionner(depot, g, a, f["pourquoi"] or "même sujet", date)
        absorbes[a] = g
        res["fusions"].append({"garder": g, "absorber": a, "pourquoi": f["pourquoi"]})
    dossiers, *_ = charger(depot)
    # 2. par dossier : liens réparés, liens réciproques, isolés, clôture -> un commit par dossier
    travail = defaultdict(lambda: {"reparer": [], "ajouter": [], "clore": None, "entete": False})
    for s in analyse.get("entetes", []):
        travail[absorbes.get(s, s)]["entete"] = True
    for x in analyse["reparations"] + (decisions.get("liens_casses") or []):
        s = absorbes.get(x["dossier"], x["dossier"])
        travail[s]["reparer"].append((x["cible"], absorbes.get(x["remplacer_par"], x["remplacer_par"])))
    for a, g in absorbes.items():   # tout lien vers un dossier absorbé pointe désormais vers le dossier gardé
        for s, d in dossiers.items():
            if a in set(d["liens_entete"]) | set(d["liens_corps"]):
                travail[s]["reparer"].append((a, g))
    for x in analyse["reciproques"]:
        s, aj = absorbes.get(x["dossier"], x["dossier"]), absorbes.get(x["ajouter"], x["ajouter"])
        if s != aj:
            travail[s]["ajouter"].append((aj, "réciproque"))
    for x in decisions.get("isoles") or []:
        s, c = absorbes.get(x["dossier"], x["dossier"]), absorbes.get(x["relier_a"], x["relier_a"])
        if s != c:
            travail[s]["ajouter"].append((c, "isolé"))
            travail[c]["ajouter"].append((s, "isolé"))
            res["isoles"].append({"dossier": s, "relie_a": c, "pourquoi": x["pourquoi"]})
    for x in (decisions.get("perimes") or [])[:max_clos]:
        s = x["dossier"]
        if s in dossiers and s not in absorbes and dossiers[s]["status"] == "active":
            travail[s]["clore"] = x["pourquoi"] or "plus rien en cours"
    for s in sorted(travail):
        if s not in dossiers:
            continue
        w = travail[s]
        F = Fichier(dossiers[s]["chemin"])
        faits = ["en-tête rendu lisible"] if w["entete"] and dossiers[s].get("entete_repare") else []
        if faits:
            res.setdefault("entetes", []).append(s)
        rep = [(a, b) for a, b in dict.fromkeys(w["reparer"]) if F.remplacer_lien(a, b)]
        if rep:
            faits.append(f"{len(rep)} lien(s) réparé(s)")
            res["liens_repares"] += [{"dossier": s, "cible": a, "remplace_par": b} for a, b in rep]
        n_rec = n_iso = 0
        for c, pourquoi in dict.fromkeys(w["ajouter"]):
            if c in dossiers and c != s and F.ajouter_lien(c):
                if pourquoi == "réciproque":
                    n_rec += 1
                else:
                    n_iso += 1
        if n_rec:
            faits.append(f"{n_rec} lien(s) réciproque(s)")
            res["reciproques"] += n_rec
            res["dossiers_reciproques"] += 1
        if n_iso:
            faits.append("relié à un dossier proche")
        if w["clore"]:
            F.poser("status", "done")
            F.ajouter_log(f"- {date} : classé clos par le jardinier des dossiers ({w['clore']}). Pour le rouvrir : status: active.")
            faits.append("classé clos")
            res["clos"].append({"dossier": s, "pourquoi": w["clore"]})
        if not faits:
            continue
        F.ecrire()
        c = commiter(depot, [os.path.relpath(F.chemin, depot)], f"{humain(s)} : {', '.join(faits)} (jardinier)")
        if c:
            res["commits"].append(c)
    # 3. index, par le générateur du dépôt
    gen = os.path.join(os.path.abspath(depot), "scripts", "gen-index.py")
    if os.path.exists(gen):
        g = subprocess.run([sys.executable, gen], cwd=os.path.abspath(depot), capture_output=True, text=True, check=True)
        res["index"] = g.stdout.strip()[:200]
        suivi = subprocess.run(["git", "-C", depot, "check-ignore", "-q", "MEMORY.md"]).returncode != 0
        if suivi:   # index suivi par git : il entre dans un commit à lui ; sinon chaque copie le régénère
            c = commiter(depot, ["MEMORY.md"], "Index des dossiers : régénéré après le passage du jardinier")
            if c:
                res["commits"].append(c)
    # restes à signaler
    faits_lc = {(x["dossier"], x["cible"]) for x in res["liens_repares"]}
    res["non_repares"] = [x for x in analyse["liens_a_juger"] if (x["dossier"], x["cible"]) not in faits_lc]
    return res


# ------------------------------------------------------------------------------------------------ contrôle « rien perdu »
def lignes_normalisees(texte):
    out = set()
    corps = texte.split("\n---", 1)[1] if texte.startswith("---") and "\n---" in texte[3:] else texte
    for l in corps.splitlines():
        l = LIEN.sub("[[]]", l).strip().lstrip("#").strip()
        if l and l != "---":
            out.add(l)
    return out


def verifier(depot, avant):
    """Chaque ligne de contenu (hors en-tête) de topics/ et journal/ à `avant` existe encore à HEAD, dans un fichier
    de topics/, journal/ ou archives/ ; chaque alias d'avant est encore porté par son dossier ou par celui qui l'a absorbé.
    Rend la liste des pertes (vide = rien perdu)."""
    noms = git(depot, "diff", "--name-only", avant, "HEAD").split()
    touches = [n for n in noms if n.startswith(("topics/", "journal/"))]
    if not touches:
        return []
    apres = set()
    alias_apres = defaultdict(set)
    for p in glob.glob(os.path.join(depot, "**", "*.md"), recursive=True):
        r = os.path.relpath(p, depot)
        if r.startswith(("topics/", "journal/", "archives/")):
            t = _lire(p)
            apres |= lignes_normalisees(t)
            d = lire(p)
            if d:
                for a in en_liste(d["fm"].get("aliases")):
                    alias_apres[plat(a)].add(r)
    pertes = []
    for n in touches:
        try:
            t = git(depot, "show", f"{avant}:{n}")
        except subprocess.CalledProcessError:
            continue   # fichier nouveau
        for l in lignes_normalisees(t):
            if l not in apres:
                pertes.append(f"{n} : ligne disparue « {l[:100]} »")
        d = None
        if t.startswith("---"):
            brut, sep, _ = t[3:].partition("\n---")
            try:
                d = yaml.safe_load(brut) if sep else None
            except yaml.YAMLError:
                d = None
        if isinstance(d, dict):
            for a in en_liste(d.get("aliases")):
                if plat(a) and not alias_apres.get(plat(a)):
                    pertes.append(f"{n} : alias disparu « {a} »")
    return pertes


# ------------------------------------------------------------------------------------------------ rapport du matin
def masquer(texte, motifs):
    for m in motifs:
        texte = re.sub(r"[\w-]*" + re.escape(m) + r"[\w-]*", "un projet perso", texte, flags=re.I)
    return texte


def rapport(res, base_commits="", motifs=()):
    return masquer(_rapport(res, base_commits), motifs)


def _rapport(res, base_commits=""):
    L = []
    nb = len(res.get("commits", []))
    if res.get("erreur"):
        return f"Passage non poussé : {res['erreur']}\nRien n'a été modifié dans le dépôt des dossiers."
    if not nb:
        L.append("Rien à réparer cette nuit : les liens sont réciproques, aucun isolé, aucun doublon ni dossier périmé retenu.")
    else:
        etat = "poussé(s)" if base_commits else "préparé(s), non poussé(s) (essai)"
        L.append(f"{nb} commit(s) {etat} dans le dépôt des dossiers, aucun contenu perdu (contrôle ligne à ligne).")
    if res.get("reciproques"):
        L.append(f"• Liens réciproques : {res['reciproques']} ajoutés dans {res['dossiers_reciproques']} dossiers.")
    if res.get("liens_repares"):
        ex = ", ".join(f"{x['cible']} → {x['remplace_par']}" for x in res["liens_repares"][:4])
        L.append(f"• Liens cassés réparés : {len(res['liens_repares'])} ({ex}{'…' if len(res['liens_repares']) > 4 else ''}).")
    for x in res.get("isoles", []):
        L.append(f"• Isolé relié : {x['dossier']} ↔ {x['relie_a']} ({x['pourquoi']}).")
    for x in res.get("fusions", []):
        L.append(f"• Fusion : {x['absorber']} → {x['garder']} ({x['pourquoi']}) ; tout le contenu repris, ancien nom gardé en alias.")
    for x in res.get("clos", []):
        L.append(f"• Classé clos : {x['dossier']} ({x['pourquoi']}).")
    if res.get("non_repares"):
        ex = ", ".join(f"{x['dossier']} → {x['cible']}" for x in res["non_repares"][:4])
        L.append(f"• Liens cassés laissés tels quels (aucune cible sûre) : {len(res['non_repares'])} ({ex}{'…' if len(res['non_repares']) > 4 else ''}).")
    if res.get("entetes"):
        L.append(f"• En-têtes rendus lisibles (valeur mise entre guillemets, texte inchangé) : {', '.join(res['entetes'][:6])}.")
    if res.get("illisibles"):
        L.append(f"• En-tête illisible, dossier non traité : {', '.join(res['illisibles'][:5])}.")
    if res.get("claude"):
        L.append(f"• Avis de Claude : {res['claude'].rstrip('. ')}.")
    if nb and base_commits:
        L.append(f"Retour arrière : git revert {base_commits} (dans le dépôt des dossiers).")
    return "\n".join(L)


# ------------------------------------------------------------------------------------------------ ligne de commande
def main(a):
    if not a:
        sys.exit(__doc__)
    cmd = a[0]
    if cmd == "analyser":
        date = None
        if "--date" in a:
            date = datetime.date.fromisoformat(a[a.index("--date") + 1])
        r = analyser(a[1], date)
        json.dump(r, open(a[2], "w"), ensure_ascii=False, indent=1)
        print(f"{r['nb_dossiers']} dossiers ; {len(r['reciproques'])} liens à rendre réciproques ; {len(r['reparations'])} liens "
              f"réparables ; {len(r['entetes'])} en-têtes à rendre lisibles ; à juger : {len(r['liens_a_juger'])} liens, {len(r['isoles'])} isolés, {len(r['doublons'])} doublons "
              f"possibles, {len(r['perimes'])} périmés possibles")
    elif cmd == "consigne":
        print(consigne(a[1], json.load(open(a[2])), _lire(a[3])))
    elif cmd == "decisions":   # jardinier.py decisions SORTIE.json REPONSE.txt -> décisions filtrées (stdout)
        print(json.dumps(lire_decisions(_lire(a[2]), json.load(open(a[1]))), ensure_ascii=False, indent=1))
    elif cmd == "appliquer":
        dec = json.load(open(a[3])) if len(a) > 3 and not a[3].startswith("--") else {}
        opt = lambda k, d: int(a[a.index(k) + 1]) if k in a else d
        date = a[a.index("--date") + 1] if "--date" in a else None
        r = appliquer(a[1], json.load(open(a[2])), dec, opt("--max-fusions", 3), opt("--max-clos", 5), date)
        print(json.dumps(r, ensure_ascii=False, indent=1))
    elif cmd == "verifier":
        p = verifier(a[1], a[2])
        for x in p[:50]:
            print(x)
        sys.exit(1 if p else 0)
    elif cmd == "rapport":
        r = json.load(open(a[1]))
        f = os.environ.get("JARDINIER_MASQUER", "")
        motifs = [l.strip() for l in open(f, encoding="utf-8") if l.strip() and not l.startswith("#")] if f and os.path.exists(f) else []
        print(rapport(r, a[2] if len(a) > 2 else "", motifs))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
