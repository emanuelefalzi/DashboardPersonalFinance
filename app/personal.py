"""
Dati personali usati nei prompt AI (nomi, importo della paghetta, ecc.).

I valori reali stanno in data/personal.json, escluso da Git.
Se manca, si usano i segnaposto di data/personal.example.json.
I prompt contengono token tipo <<OWNER_UP>> che personalize() sostituisce.
"""

import json
import os

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")


def _load():
    for name in ("personal.json", "personal.example.json"):
        path = os.path.join(_DATA, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    return {}


def _tokens(cfg):
    of, ol = cfg.get("owner_first", "Mario"), cfg.get("owner_last", "Rossi")
    pf, pl = cfg.get("parent_first", "Giuseppe"), cfg.get("parent_last", "Rossi")
    return {
        "OWNER_FIRST":       of,
        "OWNER_UP":          f"{ol} {of}".upper(),     # ROSSI MARIO
        "OWNER_TITLE":       f"{ol} {of}".title(),     # Rossi Mario
        "OWNER_FL_UP":       f"{of} {ol}".upper(),     # MARIO ROSSI
        "OWNER_FL":          f"{of} {ol}".title(),     # Mario Rossi
        "PARENT_UP":         f"{pl} {pf}".upper(),     # ROSSI GIUSEPPE
        "PARENT_TITLE":      f"{pl} {pf}".title(),     # Rossi Giuseppe
        "PARENT_SHORT":      f"{pl} {pf[:5]}".upper(), # ROSSI GIUSE (troncato come in Banca Generali)
        "ALLOW":             str(cfg.get("allowance_amount", 500)),
        "ALLOW_MIN":         str(cfg.get("allowance_min", 450)),
        "ALLOW_MAX":         str(cfg.get("allowance_max", 550)),
        "REIMB_UNCERTAIN":   cfg.get("reimburse_uncertain", "spesa al supermercato"),
    }


TOKENS = _tokens(_load())


def personalize(text):
    for k, v in TOKENS.items():
        text = text.replace(f"<<{k}>>", v)
    return text
