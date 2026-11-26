"""Bolt data, fastening types, printed pins, flange parameters and the rules that derive defaults.

All ISO data lives in this file. All values are millimeters.

To add a bolt size: add one `Bolt(...)` line to BOLTS.
To add a fastening type: add it to FASTENINGS, give it a grip rule in `bolt_length()`,
and (if it changes the solid) a cutter in `cutter._fastening_cutters()`.
To add a printed fastener: see docs/ARCHITECTURE.md, Extending.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import CutError, pins, warning


@dataclass(frozen=True)
class Bolt:
    """One metric bolt size with its hole, washer and nut."""

    name: str
    d: float          # nominal diameter
    hole: float       # clearance hole, ISO 273 medium series
    washer: float     # washer outside diameter, ISO 7089
    washer_t: float   # washer thickness, ISO 7089
    nut_af: float     # nut width across flats, ISO 4032
    nut_h: float      # nut height, ISO 4032


BOLTS: dict[str, Bolt] = {b.name: b for b in (
    Bolt("M3", 3, 3.4, 7.0, 0.5, 5.5, 2.4),
    Bolt("M4", 4, 4.5, 9.0, 0.8, 7.0, 3.2),
    Bolt("M5", 5, 5.5, 10.0, 1.0, 8.0, 4.7),
    Bolt("M6", 6, 6.6, 12.0, 1.6, 10.0, 5.2),
)}

STANDARD_LENGTHS = (8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50)
BOLT_PROTRUSION = 2.0      # bolt end sticks out this far past the nut
POCKET_AF_PLAY = 0.3       # nut pocket across flats = nut AF + this
POCKET_DEPTH_PLAY = 0.4    # nut pocket depth = nut height + this
POCKET_FLOOR = 1.5         # with a nut pocket, flange thickness >= nut height + this
MIN_THICKNESS = 3.5        # automatic thickness = max(this, nominal diameter)
SPACING_PLAY = 2.0         # bolts at least washer diameter + this apart
SUGGEST_PITCH = 50.0       # suggested count: one bolt per this much bolt line
MAX_CUTS = 6

PIN_KEY = "pin"                 # the value of a cut's "bolt" key that means printed snap pins
PIN_LABEL = "Pin"
PIN_HELP = "Printed snap pins instead of bolts and nuts. Flange Cutter makes them with the parts."
PIN_SPARES = 2                  # owner: every pin file has 2 spare pins
PIN_SUGGEST_PITCH = 35.0        # suggested count: one pin per this much pin line (bolts: SUGGEST_PITCH)
PIN_MIN_THICKNESS = 6.0         # automatic thickness with pins = max(this, ceil(0.6 d))
PIN_THICKNESS_PER_D = 0.6
PIN_D_MAX = 12.0                # the largest pin (the pin_weak advice tries it)


@dataclass(frozen=True)
class Fastening:
    """How the bolts are held. Keep this registry small and explicit."""

    key: str
    label: str
    help: str
    washers_per_bolt: int
    nut_pocket: bool      # a hexagonal pocket in one flange holds the nut


FASTENINGS: dict[str, Fastening] = {f.key: f for f in (
    Fastening("through_nut", "Through-bolt and nut",
              "A bolt through both flanges, with a washer on each side and a nut.", 2, False),
    Fastening("nut_pocket", "Nut pocket",
              "The nut sits in a hexagonal pocket in one flange, so you need only one tool.", 1, True),
)}


@dataclass(frozen=True)
class Param:
    """One number the user can change. default=None means 'automatic'."""

    key: str
    label: str
    unit: str
    lo: float
    hi: float
    default: float | None
    help: str
    advanced: bool = True
    kinds: tuple[str, ...] = ("bolt", "pin")   # the fasteners that use it; the page hides it for the others
    pin_label: str | None = None               # wording for pin cuts, where it differs
    pin_help: str | None = None


PARAMS: tuple[Param, ...] = (
    Param("thickness", "Flange thickness", "mm", 2.0, 20.0, None,
          "Thickness of the flange on each part. Empty = automatic.", advanced=False,
          pin_help="Thickness of the flange on each part. Empty = automatic (6 mm for pins up to 10 mm, 7 or 8 mm "
                   "for larger pins). The pins are made to fit it. Thicker or thinner, they hold less."),
    Param("width", "Flange width", "mm", 4.0, 80.0, None,
          "How far the flange sticks out from the wall. Empty = automatic.", advanced=False),
    Param("hole_diameter", "Hole diameter", "mm", 2.0, 14.0, None,
          "Diameter of the bolt holes. Empty = automatic: ISO hole plus the extra hole size.", advanced=False,
          kinds=("bolt",)),
    Param("wall_gap", "Gap washer to wall", "mm", 0.0, 10.0, 1.0,
          "Free space between the washer and the wall.",
          pin_label="Gap pin head to wall", pin_help="Free space between the pin head and the wall."),
    Param("edge", "Edge margin", "mm", 0.0, 20.0, 2.0,
          "Flange material outside the washer.", pin_help="Flange material outside the pin head."),
    Param("hole_clearance", "Extra hole size", "mm", 0.0, 1.0, 0.2,
          "Added to the ISO hole size so the bolt fits through a printed hole.", kinds=("bolt",)),
    Param("max_wall_angle", "Wall angle limit", "deg", 0.0, 60.0, 25.0,
          "Warn when the wall meets the cut more than this far from square."),
    Param("tool_clearance", "Tool clearance", "mm", 0.0, 40.0, 12.0,
          "Free space needed above each bolt head and nut for a tool.",
          pin_help="Free space needed beyond each flange face for pliers. On the side the pins go in from, Flange "
                   "Cutter also keeps room for the whole pin and your thumb: the pin length plus 8 mm, or this value "
                   "if it is larger."),
    Param("end_margin", "End distance", "mm", 0.0, 30.0, 3.0,
          "Extra distance from an open end of the flange to the first bolt.",
          pin_help="Extra distance from an open end of the flange to the first pin."),
    Param("pin_diameter", "Pin diameter", "mm", 8.0, 12.0, 8.0,
          "Diameter of the pin shaft. The pin head is 1.5 times as wide.", advanced=False, kinds=("pin",)),
    Param("pin_hole_clearance", "Extra pin hole size", "mm", -0.2, 0.5, 0.1,
          "Added to the pin diameter for the holes, because printed holes come out a little small. Less is "
          "tighter, more is looser. Check it with the fit test. Use less than 0 only if the fit-test pin is loose "
          "at 0.", kinds=("pin",)),
    Param("pin_preload", "Pin preload", "mm", 0.0, 0.4, 0.2,
          "How hard each pin pulls the two flanges together. More holds tighter, but if the flanges print a "
          "little thick, the pin may not click in.", kinds=("pin",)),
)
PARAM_BY_KEY = {p.key: p for p in PARAMS}
COUNT_RANGE = (1, 100)
SPACING_RANGE = (10.0, 500.0)
DEFAULT_SPACING = 50.0


@dataclass
class Derived:
    """Resolved values for one cut: user values where given, automatic values elsewhere.

    For pins `bolt` and `fastening` are None (so a missed bolt read fails in the tests instead of printing
    "M4") and `pin` holds the pin."""

    bolt: Bolt | None
    fastening: Fastening | None
    thickness: float                  # T, per part
    width: float | None               # W set by the user, or None = follows the bolt line
    wall_gap: float
    edge: float
    hole_d: float
    max_wall_angle: float
    tool_clearance: float
    end_margin: float
    grip: float
    bolt_length: int | None           # None: longer than every standard length (or pins)
    pocket_af: float | None = None
    pocket_depth: float | None = None
    warnings: list[dict] = field(default_factory=list)
    pin: pins.PinSpec | None = None

    @property
    def kind(self) -> str:
        return "pin" if self.pin else "bolt"

    @property
    def noun(self) -> str:
        """For messages: "bolt" or "pin"."""
        return self.kind

    @property
    def footprint(self) -> float:
        """Diameter of what sits on the flange face around a hole: the washer, or the pin head."""
        return self.pin.head_d if self.pin else self.bolt.washer

    @property
    def suggest_pitch(self) -> float:
        return PIN_SUGGEST_PITCH if self.pin else SUGGEST_PITCH

    def bolt_line(self, extra_gap: float = 0.0) -> float:
        """c: distance from the wall to the bolt (or pin) axis."""
        return self.wall_gap + extra_gap + self.footprint / 2

    def flange_width(self, c: float) -> float:
        """W: the user's width, or bolt line + washer (pin head) radius + edge margin."""
        return self.width if self.width else c + self.footprint / 2 + self.edge

    @property
    def envelope_r(self) -> float:
        """Radius of the free space a tool needs around a bolt or pin (POC: washer/2 + 0.5)."""
        return self.footprint / 2 + 0.5

    @property
    def min_spacing(self) -> float:
        return self.footprint + SPACING_PLAY

    @property
    def protrusion(self) -> float:
        """How far hardware sticks out of a flange face: bolt end (behind washer and nut) or head; for pins the
        pin head or its tip past the far face.

        Head height taken as the nominal diameter (socket head, ISO 4762); a hex head is lower.
        """
        if self.pin:
            return self.pin.protrusion
        length = self.bolt_length or (self.grip + BOLT_PROTRUSION)
        return max(length - 2 * self.thickness - self.bolt.washer_t, self.bolt.washer_t + self.bolt.d)

    def as_dict(self) -> dict:
        pin = None if self.pin is None else {
            "diameter": self.pin.d, "head": self.pin.head_d, "head_height": self.pin.head_h,
            "barb": self.pin.barb_d, "length": self.pin.length}
        return {"thickness": round(self.thickness, 2), "width": self.width,
                "hole_diameter": round(self.hole_d, 2), "washer": self.footprint,
                "bolt_length": self.bolt_length, "grip": round(self.grip, 2),
                "pocket_af": self.pocket_af, "pocket_depth": self.pocket_depth, "kind": self.kind, "pin": pin}


def pocket_size(bolt: Bolt) -> tuple[float, float]:
    """Across flats and depth of the hexagonal nut pocket."""
    return bolt.nut_af + POCKET_AF_PLAY, bolt.nut_h + POCKET_DEPTH_PLAY


def bolt_length(bolt: Bolt, fastening: Fastening, thickness: float) -> tuple[float, int | None]:
    """Grip and the shortest standard bolt that covers grip + 2 mm.

    The grip is everything the bolt must pass through, from under the head to the far face of the nut.
    """
    if fastening.nut_pocket:
        # head washer, the head-side flange, the pocket-side flange up to the pocket floor, the nut
        _, depth = pocket_size(bolt)
        grip = bolt.washer_t + thickness + (thickness - depth) + bolt.nut_h
    else:
        # head washer, both flanges, nut washer, nut
        grip = 2 * thickness + 2 * bolt.washer_t + bolt.nut_h
    need = grip + BOLT_PROTRUSION
    return grip, next((n for n in STANDARD_LENGTHS if n >= need - 1e-9), None)


def pin_thickness(d: float) -> float:
    """Automatic flange thickness with pins: 6 mm up to d 10, then ceil(0.6 d) (8 mm at d 12)."""
    return max(PIN_MIN_THICKNESS, float(math.ceil(round(PIN_THICKNESS_PER_D * d, 6))))


def _derive_pin(p: dict) -> Derived:
    """The values of a pin cut. The fastening and the bolt hole fields are ignored."""
    d = pins.q(p["pin_diameter"])
    auto = pin_thickness(d)
    thickness = float(p["thickness"] or auto)
    spec = pins.PinSpec(d, pins.q(2 * thickness), pins.q(p["pin_hole_clearance"]), pins.q(p["pin_preload"]))
    m, warnings = pins.mechanics(spec), []
    empty = f"leave Flange thickness empty (automatic: {auto:g} mm)"
    if m["strain"] > pins.STRAIN_LIMIT:
        warnings.append(warning("pin_flange_thin", f"The flange is too thin for {d:g} mm pins. They may crack "
                                                   "when you press them in.", f"Make the flange thicker, or {empty}."))
    elif m["pull_out"] < pins.PULL_MIN:
        if thickness < auto:
            action = f"Make the flange thicker, or {empty}."
        else:   # a longer prong is softer: the slot cannot close any further (SLOT_MIN)
            big = pins.mechanics(pins.PinSpec(PIN_D_MAX, spec.grip, spec.hole_clearance, spec.preload))
            larger = d < PIN_D_MAX and big["pull_out"] >= pins.PULL_MIN and big["strain"] <= pins.STRAIN_LIMIT
            action = f"{empty[0].upper()}{empty[1:]}" + (", or use a larger pin diameter." if larger else ".")
        warnings.append(warning("pin_weak", f"In this flange ({thickness:g} mm thick) the {d:g} mm pins hold only "
                                            f"about {m['pull_out']:.0f} N each, against about 100 N at the "
                                            "automatic thickness.", action))
    return Derived(bolt=None, fastening=None, thickness=thickness, width=p["width"], wall_gap=p["wall_gap"],
                   edge=p["edge"], hole_d=spec.hole_d, max_wall_angle=p["max_wall_angle"],
                   tool_clearance=p["tool_clearance"], end_margin=p["end_margin"], grip=2 * thickness,
                   bolt_length=None, warnings=warnings, pin=spec)


def derive(bolt_name: str, fastening_key: str, params: dict) -> Derived:
    """Turn validated user parameters into the values the geometry uses."""
    p = {k.key: (params.get(k.key) if params.get(k.key) is not None else k.default) for k in PARAMS}
    if bolt_name == PIN_KEY:
        return _derive_pin(p)
    bolt, fastening = BOLTS[bolt_name], FASTENINGS[fastening_key]
    warnings = []
    thickness = p["thickness"] or max(MIN_THICKNESS, float(bolt.d))
    af = depth = None
    if fastening.nut_pocket:
        af, depth = pocket_size(bolt)
        need = bolt.nut_h + POCKET_FLOOR
        if thickness < need - 1e-9:
            thickness = need
            warnings.append(warning(
                "thickness_raised", f"The flange was made {need:.1f} mm thick so the nut pocket fits.",
                "No action needed. Choose a smaller bolt for a thinner flange."))
    grip, length = bolt_length(bolt, fastening, thickness)
    if length is None:
        warnings.append(warning(
            "no_bolt_length", f"No standard bolt up to {STANDARD_LENGTHS[-1]} mm is long enough "
                              f"(you need {grip + BOLT_PROTRUSION:.0f} mm).",
            "Make the flange thinner, or buy longer bolts."))
    hole_d = p["hole_diameter"] or bolt.hole + p["hole_clearance"]
    if hole_d < bolt.d + 0.1:
        warnings.append(warning("hole_small", f"The holes ({hole_d:g} mm) are not bigger than the "
                                              f"{bolt.name} bolt.", "Make the holes bigger, or leave the hole "
                                                                     "diameter empty for the automatic size."))
    elif hole_d > bolt.washer - 1.0:
        warnings.append(warning("hole_large", f"The holes ({hole_d:g} mm) are almost as big as the washer.",
                                "Make the holes smaller, or use a bigger washer."))
    return Derived(bolt=bolt, fastening=fastening, thickness=float(thickness), width=p["width"],
                   wall_gap=p["wall_gap"], edge=p["edge"], hole_d=float(hole_d),
                   max_wall_angle=p["max_wall_angle"],
                   tool_clearance=p["tool_clearance"], end_margin=p["end_margin"],
                   grip=grip, bolt_length=length, pocket_af=af, pocket_depth=depth, warnings=warnings)


def check_params(params: dict | None, label: str, kind: str = "bolt") -> dict:
    """Validate the 'params' object of one cut. Unknown keys are ignored. Every key is filled for every cut
    (a bolt cut also carries the pin defaults), so saved settings keep one key set; the error texts use the
    pin wording for pin cuts."""
    out = {}
    for prm in PARAMS:
        name = (prm.pin_label if kind == "pin" and prm.pin_label else prm.label).lower()
        v = (params or {}).get(prm.key)
        if v is None or v == "":
            if prm.default is None:
                out[prm.key] = None
                continue
            v = prm.default
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise CutError(f"{label}: {name} must be a number.", f"{prm.key}={v!r}") from None
        if not prm.lo <= v <= prm.hi:
            raise CutError(f"{label}: {name} must be between {prm.lo:g} and {prm.hi:g} {prm.unit}.",
                           f"{prm.key}={v}")
        out[prm.key] = v
    return out


def catalog() -> dict:
    """Everything the web page needs to build its form (GET /api/bolts)."""
    def auto(name: str) -> dict:
        d = derive(name, "through_nut", {})
        c = d.bolt_line()
        return {"thickness": d.thickness, "bolt_line": c, "width": d.flange_width(c), "hole_diameter": d.hole_d}

    return {
        "bolts": [{"name": b.name, "d": b.d, "hole": b.hole, "washer": b.washer, "washer_t": b.washer_t,
                   "nut_af": b.nut_af, "nut_h": b.nut_h, "auto": auto(b.name)} for b in BOLTS.values()],
        "pin": {"key": PIN_KEY, "label": PIN_LABEL, "help": PIN_HELP, "spares": PIN_SPARES,
                "suggest_pitch": PIN_SUGGEST_PITCH, "auto": auto(PIN_KEY)},
        "fastenings": [{"key": f.key, "label": f.label, "help": f.help} for f in FASTENINGS.values()],
        "params": [{"key": p.key, "label": p.label, "unit": p.unit, "min": p.lo, "max": p.hi,
                    "default": p.default, "help": p.help, "advanced": p.advanced, "kinds": list(p.kinds),
                    "pin_label": p.pin_label, "pin_help": p.pin_help} for p in PARAMS],
        "count": {"min": COUNT_RANGE[0], "max": COUNT_RANGE[1], "default": 8, "suggest_pitch": SUGGEST_PITCH},
        "spacing": {"min": SPACING_RANGE[0], "max": SPACING_RANGE[1], "default": DEFAULT_SPACING},
        "max_cuts": MAX_CUTS,
        "defaults": {"side": "outside", "bolt": "M4", "fastening": "through_nut", "nut_pocket_half": "B"},
    }
