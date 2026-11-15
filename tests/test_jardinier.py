#!/usr/bin/env python3
"""Essais du jardinier sur un petit dépôt fictif (aucune donnée réelle). Lancer : python3 -m unittest tests/test_jardinier.py"""
import json, os, subprocess, sys, tempfile, unittest

ICI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ICI)
import jardinier as J  # noqa: E402

DOSSIERS = {
    "project_jardin": """---
slug: project_jardin
type: project
status: active
updated: 2026-11-01
aliases: [jardin, potager]
links: ['[[project_tomates]]']
description: Le potager de la maison
---

## État actuel
- Potager en place, voir [[project_tomate]] pour les tomates.

## Log
- 2026-11-01 : bacs posés.
- 2026-10-20 : terre achetée.
""",
    "project_tomates": """---
slug: project_tomates
type: project
status: active
updated: 2026-11-02
aliases: [tomates, semis de tomates]
links: []
description: Les semis : variétés et calendrier
---

## État actuel
- Semis en godets.

## Pending
- 2026-12-01 : repiquer les plants
""",
    "project_semis_tomates": """---
slug: project_semis_tomates
type: project
status: active
updated: 2026-10-01
aliases: [semis de tomates, tomates]
links: ['[[project_jardin]]']
description: Semis de tomates
---

## État actuel
- Variété cœur de bœuf retenue.

## RDV
- 2026-12-05 10:00 : foire aux plants
""",
    "project_vieux_velo": """---
slug: project_vieux_velo
type: project
status: active
updated: 2026-03-01
aliases: [vélo]
links: []
description: Réparer le vieux vélo
---

## État actuel
- Vélo donné en mars, plus rien à faire.
""",
}


def depot():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "topics"))
    for s, t in DOSSIERS.items():
        open(os.path.join(d, "topics", s + ".md"), "w", encoding="utf-8").write(t)
    for c in (["init", "-q", "-b", "main"], ["config", "user.email", "essai@example.org"], ["config", "user.name", "Essai"],
              ["add", "-A"], ["commit", "-q", "-m", "Départ : dossiers fictifs"]):
        subprocess.run(["git", "-C", d, *c], check=True, capture_output=True)
    return d


class Essais(unittest.TestCase):
    def test_passe_complete(self):
        d = depot()
        avant = J.git(d, "rev-parse", "HEAD").strip()
        a = J.analyser(d, J.datetime.date(2026, 11, 15))
        # lien cassé « project_tomate » retrouvé sans ambiguïté (même nom à une lettre près ? non : par alias seulement)
        self.assertIn({"dossier": "project_tomates", "ajouter": "project_jardin"}, a["reciproques"])
        self.assertTrue(any(x["dossier"] == "project_vieux_velo" for x in a["perimes"]))
        self.assertTrue(any({x["a"], x["b"]} == {"project_tomates", "project_semis_tomates"} for x in a["doublons"]))
        dec = {"doublons": [{"garder": "project_tomates", "absorber": "project_semis_tomates", "pourquoi": "même sujet"}],
               "perimes": [{"dossier": "project_vieux_velo", "pourquoi": "vélo donné"}],
               "isoles": [], "liens_casses": []}
        r = J.appliquer(d, a, dec, date="2026-11-15")
        self.assertEqual(J.verifier(d, avant), [])
        tom = open(os.path.join(d, "topics", "project_tomates.md"), encoding="utf-8").read()
        self.assertIn("project_semis_tomates", tom)               # ancien slug en alias
        self.assertIn("2026-12-05 10:00 : foire aux plants", tom)  # RDV repris dans la section RDV
        self.assertIn("Variété cœur de bœuf retenue.", tom)        # le reste en note
        self.assertIn("[[project_jardin]]", tom)                   # lien réciproque
        self.assertTrue(os.path.exists(os.path.join(d, "archives", "redirections", "project_semis_tomates.md")))
        self.assertFalse(os.path.exists(os.path.join(d, "topics", "project_semis_tomates.md")))
        velo = open(os.path.join(d, "topics", "project_vieux_velo.md"), encoding="utf-8").read()
        self.assertIn("status: done", velo)
        # un commit par dossier, titre « Sujet : ce qui a changé »
        for t in J.git(d, "log", "--format=%s", f"{avant}..HEAD").splitlines():
            self.assertRegex(t, r"^\S[^:\n]{0,79}\S : \S")
        for c in J.git(d, "rev-list", f"{avant}..HEAD").split():
            fichiers = J.git(d, "show", "--name-only", "--format=", c).split()
            noms = {os.path.basename(f) for f in fichiers}
            self.assertEqual(len(noms), 1, fichiers)
        self.assertTrue(r["fusions"] and r["clos"])

    def test_perte_detectee(self):
        d = depot()
        avant = J.git(d, "rev-parse", "HEAD").strip()
        p = os.path.join(d, "topics", "project_jardin.md")
        t = open(p, encoding="utf-8").read().replace("- 2026-10-20 : terre achetée.\n", "")
        open(p, "w", encoding="utf-8").write(t)
        J.git(d, "commit", "-qam", "Jardin : perte volontaire")
        self.assertTrue(J.verifier(d, avant))

    def test_entete_illisible(self):
        b = "slug: x\ndescription: Agent « X » : chaque nuit\nstatus: active"
        r = J.reparer_entete(b)
        self.assertEqual(J.yaml.safe_load(r)["description"], "Agent « X » : chaque nuit")

    def test_decisions_filtrees(self):
        a = {"liens_a_juger": [], "isoles": [{"dossier": "a", "proches": ["b"]}], "slugs": ["a", "b", "c"],
             "doublons": [{"a": "a", "b": "b"}], "perimes": [{"dossier": "c"}]}
        t = json.dumps({"isoles": [{"dossier": "a", "relier_a": "inconnu"}], "doublons": [{"a": "a", "b": "b", "doublon": True, "garder": "z"}],
                        "perimes": [{"dossier": "c", "clore": True, "pourquoi": "fini"}]})
        d = J.lire_decisions("bla " + t + " bla", a)
        self.assertEqual(d["isoles"], [])
        self.assertEqual(d["doublons"], [])
        self.assertEqual(d["perimes"][0]["dossier"], "c")


if __name__ == "__main__":
    unittest.main()
