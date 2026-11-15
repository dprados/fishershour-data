"""Type -> modalities, bottom and probable species (heuristics, documented in README).

Species keys match the app catalogue (`species.json`, data-schema §4). ASCII only.
"""

SEA_SPECIES = ["lubina", "sargo", "dorada", "herrera", "baila", "palometa", "choco", "calamar", "pulpo",
               "congrio", "corvina", "pargo", "denton", "lisa", "anjova", "chicharro", "caballa", "sama",
               "vieja"]
FRESH_SPECIES = ["trucha", "black_bass", "lucio", "lucioperca", "carpa", "barbo", "siluro", "boga", "tenca",
                 "perca"]
ALL_SPECIES = SEA_SPECIES + FRESH_SPECIES

SEA_MODES = {
    "beach": ["surf", "spin", "kayak"],
    "rock": ["rock", "spin", "eging", "spear"],
    "pier": ["spin", "rock", "eging"],
    "harbour": ["eging", "spin", "rock"],
    "rivermouth": ["spin", "surf", "kayak"],
    "estuary": ["spin", "eging", "kayak"],
}
SEA_BOTTOM = {"beach": "sand", "rock": "rock", "pier": "sand-rock", "harbour": "sand-rock",
              "rivermouth": "sand", "estuary": "mud"}


def climate(cc, lat, lon):
    """Climate zone of a sea spot: atl, med, can (Canaries, Madeira, Azores)."""
    if cc == "PT" and (lon < -15):
        return "can"
    if cc == "ES" and lat < 30:
        return "can"
    if is_med(lat, lon):
        return "med"
    return "atl"


def is_med(lat, lon):
    """Mediterranean side (incl. Alborán, Baleares, Gulf of Lion, Corsica). Gibraltar ~ -5.6."""
    if lon > -5.6 and lat < 43.0:
        return True
    return lon > 2.9 and lat < 44.0


def sea_species(clim, typ, lat, cc):
    south = lat < 38.5 or cc == "MA"
    if clim == "can":
        base = {
            "beach": ["sargo", "baila", "herrera", "palometa", "corvina", "lisa"],
            "rock": ["vieja", "sargo", "sama", "pargo", "pulpo", "chicharro"],
            "pier": ["chicharro", "caballa", "calamar", "sargo", "lisa", "palometa", "anjova"],
            "harbour": ["chicharro", "caballa", "calamar", "lisa", "sargo"],
            "rivermouth": ["sargo", "lisa", "baila", "palometa"],
            "estuary": ["sargo", "lisa", "baila"],
        }[typ]
    elif clim == "med":
        base = {
            "beach": ["dorada", "herrera", "lubina", "sargo", "palometa", "baila", "lisa"],
            "rock": ["sargo", "dorada", "lubina", "denton", "pargo", "pulpo", "calamar"],
            "pier": ["sargo", "dorada", "lisa", "lubina", "calamar", "choco", "chicharro", "caballa", "anjova"],
            "harbour": ["lisa", "sargo", "dorada", "calamar", "choco", "chicharro", "caballa"],
            "rivermouth": ["lubina", "dorada", "lisa", "anjova", "palometa"],
            "estuary": ["dorada", "lubina", "lisa", "anjova", "choco"],
        }[typ]
    else:
        base = {
            "beach": ["lubina", "sargo", "dorada", "corvina", "lisa"],
            "rock": ["sargo", "lubina", "congrio", "pulpo", "pargo"],
            "pier": ["lubina", "sargo", "lisa", "choco", "calamar", "chicharro", "caballa", "congrio"],
            "harbour": ["lisa", "lubina", "choco", "calamar", "chicharro", "caballa", "congrio"],
            "rivermouth": ["lubina", "lisa", "dorada", "corvina"],
            "estuary": ["lubina", "dorada", "lisa", "choco", "corvina"],
        }[typ]
        if south:
            extra = {"beach": ["baila", "herrera", "palometa"], "rock": ["sama", "denton"],
                     "pier": ["anjova", "baila"], "harbour": ["anjova"], "rivermouth": ["baila", "anjova"],
                     "estuary": ["baila"]}[typ]
            base = base + extra
        else:
            base = [s for s in base if s != "corvina"] if lat > 44.5 else base
    return base


def fresh_species(cc, kind, salmonid, lat, lon):
    ebro_tajo = cc == "ES" and ((40.0 < lat < 42.9 and lon > -3.5) or (39.4 < lat < 40.9 and -6.5 < lon < -2.5))
    if kind == "river":
        if salmonid:
            sp = ["trucha", "barbo", "boga"] if cc != "FR" else ["trucha", "perca", "lucio"]
        elif cc == "FR":
            sp = ["lucio", "lucioperca", "perca", "carpa", "black_bass", "siluro", "barbo", "tenca"]
        elif cc == "PT":
            sp = ["barbo", "boga", "black_bass", "carpa", "lucioperca"]
        else:
            sp = ["barbo", "black_bass", "carpa", "boga", "lucio", "lucioperca"]
            if ebro_tajo:
                sp = ["siluro"] + sp
    else:  # reservoir / lake
        if cc == "FR":
            sp = ["lucio", "lucioperca", "perca", "carpa", "black_bass", "tenca", "siluro"]
        elif cc == "PT":
            sp = ["black_bass", "carpa", "barbo", "lucioperca", "boga"]
        else:
            sp = ["black_bass", "carpa", "barbo", "lucioperca", "lucio"]
            if ebro_tajo:
                sp = ["siluro"] + sp + ["perca"]
        if salmonid:
            sp = ["trucha"] + sp
    return sp


def salmonid(cc, region_iso, kind, elev, lat):
    """Cold/mountain water heuristic: decides whether `fly` makes sense.

    Rivers: Atlantic-north Iberia (GA, AS, CB, PV, NA) and Brittany/Normandy at any altitude >= 50 m;
    elsewhere by model altitude (FR >= 350 m, PT north >= 300 m, ES >= 800 m).
    Lakes/reservoirs: only high ones (FR >= 900 m, ES/PT >= 1000 m, north Iberia >= 400 m).
    """
    e = elev if elev is not None else 0.0
    north_es = region_iso in ("ES-GA", "ES-AS", "ES-CB", "ES-PV", "ES-NC")
    atl_fr = region_iso in ("FR-BRE", "FR-NOR")
    if kind == "river":
        if north_es or atl_fr:
            return e >= 50
        if cc == "FR":
            return e >= 350
        if cc == "PT":
            return lat >= 40.3 and e >= 300
        return e >= 800
    if north_es:
        return e >= 400
    if cc == "FR":
        return e >= 900
    return e >= 1000


def fresh_modes(kind, is_salmonid):
    if kind == "river":
        return ["fly", "fspin", "bottom"] if is_salmonid else ["fspin", "bottom"]
    m = ["fspin", "bottom"]
    if is_salmonid:
        m.append("fly")
    return m
