"""Connecteur MCP pour Micromedia ALERT, via son serveur OPC DA (Alert.OPCServer)
et ses fichiers de configuration / traces (lecture seule).

Outils exposés :
  - alert_statut      : santé de la chaîne d'alerte (système, drivers, serveurs de données) + défauts
  - alert_alarmes     : état des alarmes (active, acquittée, masquée, dernier appelé, groupe)
  - alert_astreinte   : équipe en service par groupe d'appel, état des opérateurs
  - alert_journal     : événements des traces (appels, échecs, masquages…), filtrables
  - alert_config      : configuration (utilisateurs, équipes, groupes, variables…) du poste ou d'un .abf
  - alert_parcourir   : arborescence OPC d'Alert
  - alert_lire        : lecture brute d'items OPC
  - alert_ecrire      : écriture d'un item OPC (liste blanche + journal, désactivée par défaut)

Le connecteur doit tourner avec le MÊME niveau de droits qu'Alert.

Usage :
  py server.py            -> serveur MCP (stdio)
  py server.py --test     -> test de connexion en ligne de commande
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.types import ToolAnnotations

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

import opcda

logging.basicConfig(stream=sys.stderr, level=logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "progid": "Alert.OPCServer",
    "process_image": "Alert.exe",
    "station": "",
    "dossier_alert": r"C:\ProgramData\Micromedia\Alert",
    "masquer_numeros": True,
    "taille_lot": 200,
    "ecriture_active": False,
    "items_ecriture": {},
    "journal_ecriture": "journal_ecritures.jsonl",
}

TAG_PROPS = ("Value", "AlarmActive", "AlarmAck", "AlarmAckUser", "AlarmMask", "Message",
             "CallGroup", "CallStatus", "LastCallUser", "LastCallNum", "LinkStatus")
USER_PROPS = ("Identifier", "OnDuty", "ClockIn", "CallStatus", "CallFail")
GROUP_PROPS = ("Status", "Team", "Derogation", "DerogTeam", "CallStatus", "ReliefStatus")
TAGGROUP_PROPS = ("Status", "Alarm", "Ack", "Mask", "TagCount")
DATASERVER_PROPS = ("Status", "LinkFail", "Disabled", "Alarm", "Ack", "TagCount", "TagFail")
DRIVER_PROPS = ("Name", "Error", "LinkFail", "Status")

# Codes des traces Alert dont le sens a été établi en comparant les traces aux
# événements (les autres codes sont renvoyés bruts).
TRACE_CODES = {
    "00": "Démarrage d'Alert",
    "04": "Appel lancé",
    "05": "Appel lancé",
    "07": "Appel en échec (essais épuisés)",
    "08": "Erreur d'appel",
    "09": "Apparition d'alarme",
    "12": "Appel acquitté par l'appelé",
    "13": "Apparition d'alarme (variable)",
    "16": "Passage au groupe de relève",
    "24": "Information",
    "27": "Alarme masquée",
    "28": "Alarme démasquée",
    "33": "Défaut / qualité de variable",
}
JOURS = ("lun", "mar", "mer", "jeu", "ven", "sam", "dim", "férié")


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def _masquer(texte: Any, cfg) -> Any:
    """Masque le milieu des numéros de téléphone (≥ 9 chiffres) si demandé."""
    if not cfg["masquer_numeros"] or not isinstance(texte, str):
        return texte
    return re.sub(r"\+?\d{9,}", lambda m: m.group(0)[:4] + "x" * (len(m.group(0)) - 6) + m.group(0)[-2:], texte)


# --------------------------------------------------------------------------
# Configuration Alert (fichiers XML du poste ou d'une sauvegarde .abf)
# --------------------------------------------------------------------------

class ConfigSource:
    def __init__(self, cfg, fichier_abf: str = ""):
        self.abf = Path(fichier_abf) if fichier_abf else None
        self.dir = Path(cfg["dossier_alert"])
        if self.abf and not self.abf.is_file():
            raise FileNotFoundError(f"Sauvegarde introuvable : {self.abf}")

    def xml(self, name: str) -> ET.Element | None:
        if self.abf:
            with zipfile.ZipFile(self.abf) as z:
                if name not in z.namelist():
                    return None
                return ET.fromstring(z.read(name))
        p = self.dir / name
        return ET.parse(p).getroot() if p.exists() else None


def _ls(node: ET.Element | None) -> str:
    if node is None:
        return ""
    s = node.find("langstring")
    return (s.text or "").strip() if s is not None else (node.text or "").strip()


def _txt(node: ET.Element, tag: str) -> str:
    e = node.find(tag)
    return (e.text or "").strip() if e is not None and e.text else ""


def _dataservers(src: ConfigSource) -> dict[str, str]:
    root = src.xml("dataserverlist.xml")
    return {d.get("id"): d.get("name") for d in root.findall("DATASERVER")} if root is not None else {}


def _tags(src: ConfigSource) -> list[dict[str, Any]]:
    root = src.xml("taglist.xml")
    if root is None:
        return []
    ds = _dataservers(src)
    out = []
    for t in root.findall("TAG"):
        mc = t.find("maskcondition")
        out.append({
            "id": int(t.get("id")),
            "nom": t.get("name"),
            "serveur_donnees": ds.get(_txt(t, "dataserver"), _txt(t, "dataserver")),
            "message": _ls(t.find("alarmmessage")),
            "message_vocal": _ls(t.find("alarmsynthmessage")),
            "condition": (t.find("eventcondition").text or "").strip() if t.find("eventcondition") is not None else "",
            "masquage": {"variable": mc.get("maskident"), "condition": (mc.text or "").strip()}
                        if mc is not None and (mc.get("maskident") or mc.text) else None,
            "priorite": _txt(t, "priority") or None,
            "groupes": [g.get("name") for g in t.findall("grouplist/taggroup")],
            "appels": [a.get("actionid") for a in t.findall("setactionlist/action")],
        })
    return out


def _programme(prog: ET.Element | None) -> list[dict[str, Any]]:
    out = []
    for days in (prog.findall("days") if prog is not None else []):
        mask = int(days.get("mask"), 16)
        jours = [j for i, j in enumerate(JOURS) if mask & (1 << i)]
        periodes = [{"a_partir_de": p.get("time"), "equipe_id": p.get("team")} for p in days.findall("period")]
        out.append({"jours": jours, "periodes": periodes})
    return out


def _section(src: ConfigSource, section: str, cfg) -> Any:
    if section == "utilisateurs":
        root = src.xml("userlist.xml")
        return [{
            "id": u.get("id"), "nom": u.get("name"), "prenom": u.get("firstname") or None,
            "identifiant": _txt(u, "identifier"),
            "profil": u.find("profile").get("name") if u.find("profile") is not None else None,
            "statut": u.find("status").get("name") if u.find("status") is not None else None,
            "numeros": [{"driver": n.get("drivername"), "numero": _masquer(n.get("address"), cfg),
                         "valide": n.get("valid") == "1", "acquit_auto": n.get("ackautodef") == "1"}
                        for n in u.findall("numberlist/number")],
            "appel_sur_retombee": _txt(u, "callonreset") == "1", "appel_sur_acquit": _txt(u, "callonack") == "1",
        } for u in root.findall("USER")]
    if section == "equipes":
        root = src.xml("teamlist.xml")
        return [{"id": t.get("id"), "nom": t.get("name"),
                 "groupe": t.find("ownergroup").get("name") if t.find("ownergroup") is not None else None,
                 "membres": [m.get("name") for m in t.findall("userlist/member")]} for t in root.findall("TEAM")]
    if section == "groupes_appel":
        root = src.xml("grouplist.xml")
        team_root = src.xml("teamlist.xml")
        teams = {t.get("id"): t.get("name") for t in team_root.findall("TEAM")} if team_root is not None else {}
        out = []
        for g in root.findall("CALLGROUP"):
            prog = _programme(g.find("program"))
            for p in prog:
                for per in p["periodes"]:
                    per["equipe"] = teams.get(per.pop("equipe_id"), "(aucune)")
            rg = g.find("reliefgroup")
            out.append({"id": g.get("id"), "nom": g.get("name"),
                        "groupe_releve": rg.get("name") if rg is not None and rg.get("name") else None,
                        "membres": [m.get("name") for m in g.findall("userlist/member")],
                        "programme_hebdo": prog})
        return {"groupes": out, "note": "Jours décodés du masque : bit 0 = lundi … bit 6 = dimanche, bit 7 = férié."}
    if section == "variables":
        return _tags(src)
    if section == "groupes_variables":
        root = src.xml("taggrouplist.xml")
        tags = _tags(src)
        return [{"id": g.get("id"), "nom": g.get("name"),
                 "fichier_audio": _ls(g.find("audiofile")) or (g.find("audiofile/langfile").text
                                                               if g.find("audiofile/langfile") is not None else None),
                 "variables": [t["nom"] for t in tags if g.get("name") in t["groupes"]]}
                for g in root.findall("TAGGROUP")]
    if section == "serveurs_donnees":
        root = src.xml("dataserverlist.xml")
        return [{"id": d.get("id"), "nom": d.get("name"), "desactive": _txt(d, "disabled") == "1",
                 "serveurs": [_txt(s, "url") for s in d.findall("dataconnector/serverlist/server")]}
                for d in root.findall("DATASERVER")]
    if section == "jours_feries":
        root = src.xml("holiday.xml")
        return [ET.tostring(h, encoding="unicode") for h in root] if root is not None else []
    if section == "options":
        root = src.xml("optionslist.xml")
        return {o.get("name"): (o.find("value").text if o.find("value") is not None else None)
                for o in root.findall("OPTION") if o.get("local") != "2"}
    raise ValueError(f"Section inconnue : {section}")


SECTIONS = ("utilisateurs", "equipes", "groupes_appel", "variables", "groupes_variables",
            "serveurs_donnees", "jours_feries", "options")


# --------------------------------------------------------------------------
# Accès OPC (exécuté dans le thread COM dédié)
# --------------------------------------------------------------------------

_worker = opcda.OpcDaWorker()


async def _run(fn, *args):
    """Exécute fn(client, cfg, *args) dans le thread COM, connexion ouverte."""
    cfg = load_config()

    def job():
        with opcda.OpcDaClient(cfg["progid"], cfg["process_image"]) as c:
            return fn(c, cfg, *args)

    try:
        return await asyncio.wrap_future(_worker.submit(job))
    except Exception as e:
        return {"erreur": f"{type(e).__name__}: {e}"}


def _read(c, cfg, ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    n = cfg["taille_lot"]
    for k in range(0, len(ids), n):
        out.update(c.read(ids[k:k + n]))
    return out


def _station(c, cfg) -> str:
    return cfg["station"] or c.browse_names(["Station"])["dossiers"][0]


def _objects(c, cfg, prefix: list[str], props: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Lit `props` pour chaque sous-dossier de `prefix` (utilisateurs, groupes…)."""
    noms = c.browse_names(prefix)["dossiers"]
    base = ".".join(prefix)
    vals = _read(c, cfg, [f"{base}.{n}.{p}" for n in noms for p in props])
    return {n: {p: vals.get(f"{base}.{n}.{p}", {}).get("valeur") for p in props} for n in noms}


# --------------------------------------------------------------------------
# Serveur MCP
# --------------------------------------------------------------------------

mcp = MCPServer("alert", instructions=(
    "Connecteur Micromedia ALERT (télésurveillance / appel d'astreinte). "
    "alert_statut donne la santé de la chaîne d'appel ; alert_alarmes l'état des alarmes ; "
    "alert_astreinte qui est en service ; alert_journal l'historique des appels et événements ; "
    "alert_config la configuration (utilisateurs, groupes, variables), y compris d'une sauvegarde .abf. "
    "alert_ecrire agit sur la télésurveillance réelle (acquittement, désactivation d'appels…) : "
    "ne l'appeler que sur demande explicite."))
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@mcp.tool(annotations=READ_ONLY)
async def alert_statut() -> dict[str, Any]:
    """Santé de la chaîne d'alerte : état du serveur Alert, variables système
    (liaisons, ports, passerelle, appels désactivés), drivers d'appel, serveurs de
    données, groupes de variables, avec la liste des défauts détectés."""
    def fn(c, cfg):
        st = c.status()
        sys_names = c.browse_names(["System"])["feuilles"]
        sysv = {k[len("System."):]: v.get("valeur")
                for k, v in _read(c, cfg, [f"System.{n}" for n in sys_names]).items()}
        drivers = _objects(c, cfg, ["Driver"], DRIVER_PROPS)
        s = _station(c, cfg)
        poste = {k.split(".")[-1]: v.get("valeur") for k, v in
                 _read(c, cfg, [f"Station.{s}.{p}" for p in ("InAlarm", "ToAck", "CallDisabled")]).items()}
        serveurs = _objects(c, cfg, ["Station", s, "DataServer"], DATASERVER_PROPS)
        groupes_var = _objects(c, cfg, ["Station", s, "TagGroup"], TAGGROUP_PROPS)

        defauts = []
        if sysv.get("CallDisabled") or poste.get("CallDisabled"):
            defauts.append("Appels DÉSACTIVÉS")
        if sysv.get("AlertLock"):
            defauts.append("Alert verrouillé")
        if sysv.get("PortFail"):
            defauts.append(f"Port(s) d'appel en défaut ({sysv['PortFail']})")
        for k, v in sysv.items():
            if k.endswith(".Fail") and v:
                defauts.append(f"{k[:-5]} en défaut")
        if sysv.get("DBStatus"):
            defauts.append(f"Base de données en défaut (code {sysv['DBStatus']})")
        if sysv.get("RedundancyFail"):
            defauts.append("Défaut de redondance")
        for n, d in drivers.items():
            if d["LinkFail"] or d["Error"]:
                defauts.append(f"Driver {n} : " + (d["Error"] or "défaut de liaison"))
        for n, d in serveurs.items():
            if d["Disabled"]:
                continue
            if d["LinkFail"]:
                defauts.append(f"Serveur de données {n} : défaut de liaison")
            if d["TagFail"]:
                defauts.append(f"Serveur de données {n} : {d['TagFail']} variable(s) en défaut")
        return {
            "serveur_alert": st, "station": s, "poste": poste,
            "defauts": defauts, "chaine_appel_ok": not defauts,
            "systeme": sysv, "drivers": drivers, "serveurs_donnees": serveurs,
            "groupes_variables": groupes_var,
            "note": "Les codes Status / LinkStatus / PortFail sont les valeurs brutes d'Alert.",
        }
    return await _run(fn)


@mcp.tool(annotations=READ_ONLY)
async def alert_alarmes(inclure_inactives: bool = False, filtre: str = "") -> dict[str, Any]:
    """État temps réel des alarmes Alert : active, acquittée (et par qui), masquée,
    message, groupe d'appel, dernier opérateur et numéro appelés, qualité de la valeur.
    Par défaut seules les alarmes actives ou masquées sont renvoyées.
    `filtre` restreint aux noms contenant ce texte."""
    def fn(c, cfg):
        tags = [t for t in _tags(ConfigSource(cfg)) if filtre.lower() in t["nom"].lower()]
        ids = {t["nom"]: f"Tag.{t['serveur_donnees']}.{t['nom']}" for t in tags}
        vals = _read(c, cfg, [f"{base}.{p}" for base in ids.values() for p in TAG_PROPS])
        out = []
        for t in tags:
            g = lambda p: vals.get(f"{ids[t['nom']]}.{p}", {})
            a = {"variable": t["nom"], "id": t["id"],
                 "active": g("AlarmActive").get("valeur"), "acquittee": g("AlarmAck").get("valeur"),
                 "acquittee_par": g("AlarmAckUser").get("valeur") or None,
                 "masquee": g("AlarmMask").get("valeur"),
                 "valeur": g("Value").get("valeur"), "qualite": g("Value").get("qualite"),
                 "message": g("Message").get("valeur") or t["message"] or None,
                 "groupe_appel": g("CallGroup").get("valeur") or None,
                 "etat_appel": g("CallStatus").get("valeur"),
                 "dernier_appele": g("LastCallUser").get("valeur") or None,
                 "dernier_numero": _masquer(g("LastCallNum").get("valeur"), cfg) or None,
                 "masquage_configure": t["masquage"]}
            if "erreur" in g("Value"):
                a["erreur"] = g("Value")["erreur"]
            if inclure_inactives or a["active"] or a["masquee"]:
                out.append(a)
        out.sort(key=lambda x: (not x["active"], x["variable"]))
        return {"nombre": len(out),
                "actives": sum(bool(a["active"]) for a in out),
                "non_acquittees": sum(bool(a["active"]) and not a["acquittee"] for a in out),
                "masquees": sum(bool(a["masquee"]) for a in out),
                "qualite_mauvaise": [a["variable"] for a in out if a["qualite"] not in (None, "Good")],
                "alarmes": out}
    return await _run(fn)


@mcp.tool(annotations=READ_ONLY)
async def alert_astreinte() -> dict[str, Any]:
    """Qui est d'astreinte maintenant : équipe en service pour chaque groupe d'appel
    (avec dérogation éventuelle) et état de chaque opérateur (en service, échec d'appel)."""
    def fn(c, cfg):
        s = _station(c, cfg)
        groupes = _objects(c, cfg, ["Station", s, "Group"], GROUP_PROPS)
        users = _objects(c, cfg, ["Station", s, "User"], USER_PROPS)
        return {
            "groupes_appel": [{"groupe": n, "equipe_en_service": g["Team"] or None,
                               "derogation": g["Derogation"], "equipe_derogation": g["DerogTeam"] or None,
                               "etat": g["Status"], "etat_appel": g["CallStatus"], "etat_releve": g["ReliefStatus"]}
                              for n, g in groupes.items()],
            "operateurs": [{"operateur": n, "identifiant": u["Identifier"], "en_service": u["OnDuty"],
                            "echec_appel": u["CallFail"], "etat_appel": u["CallStatus"], "pointage": u["ClockIn"]}
                           for n, u in users.items()],
            "aucune_equipe": [n for n, g in groupes.items() if not g["Team"]],
        }
    return await _run(fn)


def _trace_lines(cfg, jour: date):
    p = Path(cfg["dossier_alert"]) / "trace" / f"t{jour:%y%m%d}.log"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-16", errors="replace").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            yield parts[1], parts[2], "\t".join(parts[3:]).strip()


@mcp.tool(annotations=READ_ONLY)
async def alert_journal(date_debut: str = "", jours: int = 1, filtre: str = "",
                        codes: list[str] | None = None, limite: int = 200,
                        resume: bool = False) -> dict[str, Any]:
    """Événements des traces Alert (appels, échecs et leur cause SIP, acquittements,
    masquages, alarmes, démarrages…). `date_debut` au format AAAA-MM-JJ (défaut :
    aujourd'hui), sur `jours` jours (max 31). `filtre` : texte à rechercher ;
    `codes` : codes de trace à garder (ex. ["07","08"] pour les échecs d'appel).
    Avec `resume`, renvoie des compteurs (par type, par alarme, par appelé) au lieu des lignes."""
    cfg = load_config()
    try:
        debut = date.fromisoformat(date_debut) if date_debut else date.today()
    except ValueError:
        return {"erreur": "date_debut doit être au format AAAA-MM-JJ"}
    jours = max(1, min(int(jours), 31))
    evts = []
    for k in range(jours):
        j = debut + timedelta(days=k)
        for h, code, msg in _trace_lines(cfg, j):
            if codes and code not in codes:
                continue
            if filtre and filtre.lower() not in msg.lower():
                continue
            evts.append({"date": f"{j.isoformat()} {h[:2]}:{h[2:4]}:{h[4:6]}", "code": code,
                         "type": TRACE_CODES.get(code), "message": _masquer(msg, cfg)})
    if resume:
        appeles = Counter()
        for e in evts:
            m = re.match(r"^([^:]+?)\s*:\s*\S+ \(VIRTUAL", e["message"])
            if e["code"] in ("04", "05") and m:
                appeles[(m.group(1), "appels")] += 1
            if e["code"] == "07":
                appeles[(e["message"], "echecs")] += 1
            if e["code"] == "12":
                appeles[(e["message"], "acquittements")] += 1
        par_appele: dict[str, dict[str, int]] = {}
        for (u, k), n in appeles.items():
            par_appele.setdefault(u, {})[k] = n
        causes = Counter(f"{m.group(1)} {m.group(2).strip()}" for e in evts if e["code"] == "08"
                         for m in [re.search(r"\((\d{3})\)\s*([^()]*)$", e["message"])] if m)
        return {"periode": f"{debut} + {jours} j", "nombre": len(evts),
                "par_type": Counter(f"{e['code']} {e['type'] or ''}".strip() for e in evts).most_common(),
                "alarmes": Counter(e["message"] for e in evts if e["code"] in ("09", "13")).most_common(30),
                "par_appele": par_appele, "causes_echec_sip": causes.most_common()}
    return {"periode": f"{debut} + {jours} j", "nombre": len(evts), "tronque": len(evts) > limite,
            "evenements": evts[-limite:]}


@mcp.tool(annotations=READ_ONLY)
async def alert_config(section: str, fichier_abf: str = "") -> Any:
    """Configuration Alert, lue dans les fichiers du poste ou dans une sauvegarde
    `fichier_abf` (chemin complet d'un .abf). `section` parmi : utilisateurs, equipes,
    groupes_appel (avec programme hebdomadaire), variables (conditions, masquages,
    appels), groupes_variables, serveurs_donnees, jours_feries, options."""
    cfg = load_config()
    if section not in SECTIONS:
        return {"erreur": f"Section inconnue. Choisir parmi : {', '.join(SECTIONS)}"}
    try:
        return _section(ConfigSource(cfg, fichier_abf), section, cfg)
    except Exception as e:
        return {"erreur": f"{type(e).__name__}: {e}"}


@mcp.tool(annotations=READ_ONLY)
async def alert_parcourir(chemin: str = "") -> dict[str, Any]:
    """Liste les sous-dossiers et items de l'arborescence OPC d'Alert.
    `chemin` avec des points (ex. 'Station.POSTE1.Group'), vide = racine."""
    path = [p for p in chemin.split(".") if p]

    def fn(c, cfg):
        n = c.browse_names(path)
        base = ".".join(path)
        n["items"] = [f"{base}.{f}" if base else f for f in n.pop("feuilles")]
        return n
    return await _run(fn)


@mcp.tool(annotations=READ_ONLY)
async def alert_lire(items: list[str]) -> dict[str, Any]:
    """Lit des items OPC d'Alert (valeur, qualité, horodatage, type, écriture possible).
    Ex. 'System.LinkFail', 'Station.POSTE1.Group.ASTREINTE.Team',
    'Tag.PCVue_.<variable>.AlarmActive'."""
    def fn(c, cfg):
        return {k: {**v, "valeur": _masquer(v.get("valeur"), cfg)} if "valeur" in v else v
                for k, v in _read(c, cfg, items).items()}
    return await _run(fn)


def _journaliser(cfg: dict[str, Any], entree: dict[str, Any]) -> None:
    with (BASE_DIR / cfg["journal_ecriture"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entree, ensure_ascii=False, default=str) + "\n")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                      idempotentHint=True, openWorldHint=True))
async def alert_ecrire(item: str, valeur: float | int | bool | str, motif: str) -> dict[str, Any]:
    """ÉCRIT un item OPC d'Alert (ex. acquittement d'alarme, dérogation, désactivation
    des appels). Action réelle sur la télésurveillance : ne l'utiliser qu'à la demande
    explicite de l'utilisateur, après confirmation. Seuls les items de la liste blanche
    `items_ecriture` (config.json) sont modifiables, avec les valeurs autorisées.
    Chaque tentative est journalisée avec son `motif`."""
    cfg = load_config()
    entree = {"date": datetime.now().astimezone().isoformat(timespec="seconds"),
              "item": item, "valeur_demandee": valeur, "motif": motif}
    regles = cfg["items_ecriture"].get(item)
    refus = None
    if not cfg["ecriture_active"]:
        refus = "L'écriture est désactivée (ecriture_active = false dans config.json)."
    elif regles is None:
        refus = f"'{item}' n'est pas dans la liste blanche items_ecriture."
    elif "valeurs" in regles and valeur not in regles["valeurs"]:
        refus = f"Valeur {valeur!r} non autorisée (autorisées : {regles['valeurs']})."
    if refus:
        entree["resultat"] = f"REFUS {refus}"
        _journaliser(cfg, entree)
        return {"refuse": refus, "autorises": list(cfg["items_ecriture"])}

    def fn(c, cfg):
        avant = c.read([item])[item]
        if "erreur" in avant:
            raise opcda.OpcError(avant["erreur"])
        c.write(item, valeur)
        apres = c.read([item])[item]
        return {"ok": True, "item": item, "avant": avant, "apres": apres}

    res = await _run(fn)
    if res.get("ok"):
        entree.update(valeur_avant=res["avant"]["valeur"], valeur_apres=res["apres"]["valeur"], resultat="OK")
    else:
        entree["resultat"] = f"ECHEC {res.get('erreur')}"
    _journaliser(cfg, entree)
    return res


# --------------------------------------------------------------------------

async def _cli_test() -> None:
    def show(titre, obj, n=2500):
        print(f"===== {titre}")
        print(json.dumps(obj, ensure_ascii=False, indent=1, default=str)[:n])
    show("alert_statut", await alert_statut(), 4000)
    show("alert_alarmes", await alert_alarmes())
    show("alert_astreinte", await alert_astreinte())
    show("alert_journal (résumé du jour)", await alert_journal(resume=True))
    show("alert_config groupes_appel", await alert_config("groupes_appel"))


if __name__ == "__main__":
    if "--test" in sys.argv:
        asyncio.run(_cli_test())
    else:
        mcp.run()
