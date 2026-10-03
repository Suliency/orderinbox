"""Freight ontology — canonical concepts and the messy-terminology mapping.

Section 12 of the proposal: the model's job is to turn *messy human
terminology* into the *canonical freight ontology*. The ontology is the single
source of truth for what a charge *is*, so that two agents quoting "PCS" from
two different forwarders resolve to the same concept.

    messy human terminology
            ↓
    canonical freight ontology

Canonical charge concepts (Section 12), the equipment vocabulary, the Incoterm
set, and a port registry (UN/LOCODE) live here. Everything else in the freight
package (normalization, validation, alias learning) builds on these tables.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


# --------------------------------------------------------------------------
# charges
# --------------------------------------------------------------------------

# Canonical charge concept codes (Section 12). These are stable identifiers the
# rest of the system (validation, margin math, MCP tools) reasons about.
CHARGE_TYPES: dict[str, str] = {
    "OCEAN_FREIGHT": "Base ocean/container freight",
    "AIR_FREIGHT": "Base air freight",
    "ORIGIN_THC": "Terminal handling charge at the origin port",
    "DESTINATION_THC": "Terminal handling charge at the destination port",
    "DOCUMENTATION_FEE": "Documentation / B/L preparation fee",
    "BILL_OF_LADING_FEE": "Bill of lading fee",
    "CUSTOMS_CLEARANCE": "Customs clearance / brokerage",
    "ISPS": "International Ship and Port Facility Security fee",
    "BAF": "Bunker Adjustment Factor (fuel surcharge)",
    "CAF": "Currency Adjustment Factor",
    "PSS": "Peak Season Surcharge",
    "GRI": "General Rate Increase",
    "DEMURRAGE": "Demurrage (port storage of the container)",
    "DETENTION": "Detention (out-of-port container usage)",
    "CHASSIS": "Chassis / trailer fee",
    "DELIVERY": "Delivery / final-mile (e.g. drayage, rail)",
    "WAREHOUSE_HANDLING": "Warehouse / CFS handling",
    "DANGEROUS_GOODS": "Dangerous goods / DG surcharge",
    "REEFER_SURCHARGE": "Reefer / refrigerated surcharge",
    "PORT_CONGESTION_SURCHARGE": "Port congestion surcharge",
    "SECURITY_SURCHARGE": "Security surcharge",
    "FUMIGATION": "Fumigation / treatment",
    "LIFT_GATE": "Lift-gate delivery",
    "HOLDING": "Container holding / storage",
}

#: Surcharge codes that are normally *conditional* — present but only under
#: certain conditions, so they must trigger a review flag (Section 9).
CONDITIONAL_CHARGES = {"PSS", "GRI", "BAF", "CAF", "PORT_CONGESTION_SURCHARGE",
                       "SECURITY_SURCHARGE"}

#: Charges a complete ocean quote is expected to account for explicitly
#: (either as a charge or as an explicit inclusion/exclusion). Used by the
#: completeness check in validate.py.
EXPECTED_CHARGES = {
    "OCEAN_FREIGHT", "ORIGIN_THC", "DESTINATION_THC", "DOCUMENTATION_FEE",
    "DELIVERY",
}


# --------------------------------------------------------------------------
# messy-terminology -> canonical mapping
# --------------------------------------------------------------------------

# A curated table of the abbreviations and free-text phrasing that actually
# appear in freight documents, mapped to canonical charge codes. Lookup is
# case-insensitive; longer keys are tried first so "DTHC" wins over "THC".
_ALIASES: dict[str, str] = {
    # ocean / air base
    "ocean freight": "OCEAN_FREIGHT", "o/f": "OCEAN_FREIGHT", "of": "OCEAN_FREIGHT",
    "freight": "OCEAN_FREIGHT", "container freight": "OCEAN_FREIGHT",
    "air freight": "AIR_FREIGHT", "air": "AIR_FREIGHT",
    # terminal handling — end-specific shorthand
    "othc": "ORIGIN_THC", "pol thc": "ORIGIN_THC", "origin thc": "ORIGIN_THC",
    "load thc": "ORIGIN_THC", "origin terminal": "ORIGIN_THC",
    "dthc": "DESTINATION_THC", "pod thc": "DESTINATION_THC",
    "destination thc": "DESTINATION_THC", "discharge thc": "DESTINATION_THC",
    "dest thc": "DESTINATION_THC",
    "thc": "DESTINATION_THC",   # bare "THC" defaults to destination in this
                                # market; counterparty alias learning refines it
    # documentation
    "doc": "DOCUMENTATION_FEE", "docs": "DOCUMENTATION_FEE",
    "documentation": "DOCUMENTATION_FEE", "doc fee": "DOCUMENTATION_FEE",
    "bl fee": "BILL_OF_LADING_FEE", "b/l fee": "BILL_OF_LADING_FEE",
    "bl": "BILL_OF_LADING_FEE", "bill of lading fee": "BILL_OF_LADING_FEE",
    # customs / security
    "customs": "CUSTOMS_CLEARANCE", "customs clearance": "CUSTOMS_CLEARANCE",
    "brokerage": "CUSTOMS_CLEARANCE", "clearance": "CUSTOMS_CLEARANCE",
    "isps": "ISPS", "isps fee": "ISPS",
    # surcharges
    "baf": "BAF", "fuel surcharge": "BAF", "bunker": "BAF",
    "caf": "CAF", "currency surcharge": "CAF",
    "pss": "PSS", "peak season": "PSS", "peak season surcharge": "PSS",
    "gri": "GRI", "general rate increase": "GRI",
    "pcs": "PORT_CONGESTION_SURCHARGE",  # default; alias learning may override
    "port congestion": "PORT_CONGESTION_SURCHARGE",
    "congestion surcharge": "PORT_CONGESTION_SURCHARGE",
    "security": "SECURITY_SURCHARGE", "security surcharge": "SECURITY_SURCHARGE",
    "war risk": "SECURITY_SURCHARGE",
    # time-based
    "demurrage": "DEMURRAGE", "dem": "DEMURRAGE",
    "detention": "DETENTION", "det": "DETENTION",
    "holding": "HOLDING", "storage": "HOLDING",
    # equipment / service
    "chassis": "CHASSIS", "trailer": "CHASSIS",
    "delivery": "DELIVERY", "drayage": "DELIVERY", "dray": "DELIVERY",
    "rail": "DELIVERY", "final delivery": "DELIVERY", "last mile": "DELIVERY",
    "warehouse": "WAREHOUSE_HANDLING", "cfs": "WAREHOUSE_HANDLING",
    "cargo handling": "WAREHOUSE_HANDLING",
    "dg": "DANGEROUS_GOODS", "dangerous goods": "DANGEROUS_GOODS",
    "hazmat": "DANGEROUS_GOODS", "imdg": "DANGEROUS_GOODS",
    "reefer": "REEFER_SURCHARGE", "ref": "REEFER_SURCHARGE",
    "refrigerated": "REEFER_SURCHARGE",
    "fumigation": "FUMIGATION", "treatment": "FUMIGATION",
    "lift gate": "LIFT_GATE", "liftgate": "LIFT_GATE",
}

# Pre-sorted longest-first so "b/l fee" is not stolen by "b".
_ALIASES_BY_LEN = sorted(_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)


def _fold(term: str) -> str:
    """Normalize separators/whitespace so 'ORIGIN_THC', 'Origin-THC' and
    'origin thc' all compare the same."""
    t = re.sub(r"[_\-]+", " ", (term or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def normalize_charge_term(term: str) -> str | None:
    """Map a raw charge phrase to a canonical charge code (or None).

    Tries exact (case/separator-normalized) matches against the curated alias
    table, longest phrase first, then falls back to scanning the phrase for a
    known key.
    """
    t = _fold(term)
    if not t:
        return None
    if t.upper() in CHARGE_TYPES:
        return t.upper()
    if t in _ALIASES:
        return _ALIASES[t]
    # longest-known-key contained in the phrase
    for key, code in _ALIASES_BY_LEN:
        if len(key) >= 2 and key in t:
            return code
    return None


def known_charges() -> list[str]:
    return sorted(CHARGE_TYPES)


# --------------------------------------------------------------------------
# equipment
# --------------------------------------------------------------------------

EQUIPMENT_TYPES: dict[str, str] = {
    "20GP": "20-foot dry general purpose",
    "40GP": "40-foot dry general purpose",
    "40HC": "40-foot high cube",
    "45HC": "45-foot high cube",
    "20RF": "20-foot reefer",
    "40RF": "40-foot reefer",
    "OT": "Open top",
    "FR": "Flat rack",
    "TK": "Tank container",
    "LCL": "Less than container load",
    "FCL": "Full container load",
}

# Normalise the many ways a container is written (40HQ == 40HC, etc.)
_EQUIP_ALIASES = {
    "40hq": "40HC", "40hc": "40HC", "40 high cube": "40HC", "high cube": "40HC",
    "40'hc": "40HC", "40' hq": "40HC",
    "40gp": "40GP", "40'": "40GP", "40' gp": "40GP", "40 standard": "40GP",
    "20gp": "20GP", "20'": "20GP", "20' gp": "20GP",
    "45hc": "45HC", "45'": "45HC",
    "20rf": "20RF", "40rf": "40RF", "reefer": "40RF",
    "ot": "OT", "open top": "OT",
    "fr": "FR", "flat rack": "FR",
    "tk": "TK", "tank": "TK",
}


def normalize_equipment(term: str) -> str | None:
    t = re.sub(r"\s+", "", (term or "").lower())
    if t in _EQUIP_ALIASES:
        return _EQUIP_ALIASES[t]
    if t in EQUIPMENT_TYPES:
        return t
    # e.g. "40hc reefer"
    for key, code in _EQUIP_ALIASES.items():
        if key in t:
            return code
    return None


def known_equipment() -> list[str]:
    return sorted(EQUIPMENT_TYPES)


# --------------------------------------------------------------------------
# incoterms
# --------------------------------------------------------------------------

INCOTERMS = ["EXW", "FCA", "FAS", "FOB", "CFR", "CIF", "CPT", "CIP",
             "DAP", "DAT", "DPU", "DDP"]

_INCOTERM_ALIASES = {
    "free on board": "FOB", "fob": "FOB",
    "cost and freight": "CFR", "cfr": "CFR",
    "cost insurance freight": "CIF", "cif": "CIF",
    "delivered duty paid": "DDP", "ddp": "DDP",
    "ex works": "EXW", "exw": "EXW",
    "free carrier": "FCA", "fca": "FCA",
    "delivered at place": "DAP", "dap": "DAP",
}


def normalize_incoterm(term: str) -> str | None:
    t = (term or "").strip().upper()
    if not t:
        return None
    if t in INCOTERMS:
        return t
    return _INCOTERM_ALIASES.get(t.lower())


# --------------------------------------------------------------------------
# ports
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Port:
    un_locode: str          # e.g. CNSHA
    name: str              # e.g. Shanghai
    country: str = ""      # ISO alpha-2, e.g. CN
    city: str = ""


# A small built-in port registry for common lanes. Production deployments
# extend this (or load it from the rate database); the shape is what matters.
BUILTIN_PORTS: list[Port] = [
    Port("CNSHA", "Shanghai", "CN", "Shanghai"),
    Port("CNSZX", "Shenzhen", "CN", "Shenzhen"),
    Port("CNNGB", "Ningbo", "CN", "Ningbo"),
    Port("CNDLG", "Dalian", "CN", "Dalian"),
    Port("CNQIN", "Qingdao", "CN", "Qingdao"),
    Port("HKHKG", "Hong Kong", "HK", "Hong Kong"),
    Port("SGSIN", "Singapore", "SG", "Singapore"),
    Port("USLAX", "Los Angeles", "US", "Los Angeles"),
    Port("USLGB", "Long Beach", "US", "Long Beach"),
    Port("USNYC", "New York / New Jersey", "US", "New York"),
    Port("USSEA", "Seattle / Tacoma", "US", "Seattle"),
    Port("USVAN", "Vancouver", "CA", "Vancouver"),
    Port("CAYVR", "Vancouver", "CA", "Vancouver"),
    Port("CATOR", "Toronto", "CA", "Toronto"),
    Port("CAMAQ", "Montreal / Quebec", "CA", "Montreal"),
    Port("NLRRT", "Rotterdam", "NL", "Rotterdam"),
    Port("DEHAM", "Hamburg", "DE", "Hamburg"),
    Port("GBFXT", "Felixstowe", "GB", "Felixstowe"),
    Port("JPTYO", "Yokohama", "JP", "Yokohama"),
    Port("KRPUS", "Busan", "KR", "Busan"),
]

# City / name shorthands that appear in documents -> UN/LOCODE.
_PORT_ALIASES = {
    "shanghai": "CNSHA", "sha": "CNSHA", "shanghai port": "CNSHA",
    "shenzhen": "CNSZX", "szx": "CNSZX",
    "ningbo": "CNNGB",
    "hong kong": "HKHKG", "hk": "HKHKG",
    "singapore": "SGSIN",
    "los angeles": "USLAX", "lax": "USLAX", "la": "USLAX",
    "long beach": "USLGB", "lgb": "USLGB",
    "new york": "USNYC", "nyc": "USNYC",
    "seattle": "USSEA", "tacoma": "USSEA",
    "vancouver": "CAYVR", "van": "CAYVR",
    "toronto": "CATOR", "tor": "CATOR",
    "montreal": "CAMAQ",
    "rotterdam": "NLRRT",
    "hamburg": "DEHAM",
    "felixstowe": "GBFXT",
    "yokohama": "JPTYO",
    "busan": "KRPUS",
}


def lookup_port(term: str) -> Port | None:
    """Resolve a port reference (name, city, or UN/LOCODE) to a Port record."""
    t = (term or "").strip()
    if not t:
        return None
    up = t.upper()
    if len(up) == 5 and up.isalpha():
        for p in BUILTIN_PORTS:
            if p.un_locode == up:
                return p
    key = t.lower()
    code = _PORT_ALIASES.get(key)
    if code:
        for p in BUILTIN_PORTS:
            if p.un_locode == code:
                return p
    # fall back to name/city match
    for p in BUILTIN_PORTS:
        if p.name.lower() == key or p.city.lower() == key:
            return p
    return None


def known_ports() -> list[str]:
    return sorted({p.un_locode for p in BUILTIN_PORTS})


# --------------------------------------------------------------------------
# lane matching (RFQ destination vs quoted destination)
# --------------------------------------------------------------------------

# Final destinations commonly served via a hub port (onward rail/truck).
# E.g. Toronto from Asia is served via Vancouver; a quote on SHA->CAYVR with
# "rail VAN-TOR excluded" answers a Toronto RFQ.
HUB_LANES: dict[str, list[str]] = {
    "CATOR": ["CAYVR"],
    "CAMAQ": ["CAYVR", "USNYC", "USSEA"],
    "USLAX": ["USLGB"],
    "USLGB": ["USLAX"],
}


def lane_matches(origin: str, dest: str, rfq_origin: str, rfq_dest: str) -> bool:
    """Does a quote's lane answer an RFQ's lane?

    Origin must match. Destination must match, or be a known hub serving the
    RFQ destination (or vice versa).
    """
    o, d = (origin or "").upper(), (dest or "").upper()
    ro, rd = (rfq_origin or "").upper(), (rfq_dest or "").upper()
    if not o or not ro or o != ro:
        return False
    if not d or not rd:
        return True
    if d == rd:
        return True
    return d in HUB_LANES.get(rd, []) or rd in HUB_LANES.get(d, [])
