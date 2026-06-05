"""LLM planner -- REUSES the resident qwen model (no new model spun up).

Turns a natural-language request into a strict assembly spec via vLLM guided-JSON, so the model
*must* return valid structure (the doctrine: AI for semantics, deterministic geometry for the rest).
qwen3.6 is a reasoning model -> we disable thinking, or the answer lands in the `reasoning` field.
"""
from __future__ import annotations

import json
import os

import httpx

# JSON Schema the model is constrained to. Keep the vocabulary tight; expand with PART_GENS.
SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parts", "mates"],
    "properties": {
        "parts": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "type", "params", "material"],
                "properties": {
                    "id": {"type": "string"},
                    "type": {"enum": ["plate", "panel", "link", "housing", "adapter", "sheet_bracket", "spur_gear",
                                      "ring_gear", "planetary_gearset",
                                      "spacer", "shaft", "four_bar", "bolt", "nut", "washer", "bearing",
                                      "extrusion", "ball_bearing", "screw", "threaded_rod",
                                      "bearing_block", "sprocket", "flange", "pipe"]},
                    "material": {"enum": ["steel", "stainless", "aluminum", "brass",
                                          "abs", "pla", "petg", "nylon"]},
                    "params": {"type": "object"},
                },
            },
        },
        "mates": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["a", "b"],
                "properties": {
                    "a": {"type": "string"}, "b": {"type": "string"},
                    "a_joint": {"type": "string"}, "b_joint": {"type": "string"},
                    "type": {"enum": ["coincident", "rigid", "contact", "mesh",
                                      "revolute", "cylindrical"]},
                    # mate-by-intent (T2.2): name HOW the parts connect and the resolver derives the frames
                    # + mate type deterministically. Either give an "intent" OR explicit a_joint/b_joint+type.
                    "intent": {"enum": ["seat_on", "insert", "fasten", "mesh"]},
                    "hole_index": {"type": "integer"},
                    "angle_deg": {"type": "number"},
                    "slide": {"type": "number"},
                },
            },
        },
    },
}

SYSTEM = """You are a CAD assembly planner. Convert the user's request into a parametric assembly spec.

Available part types and their params (mm):
- plate: width, depth, thick, boss_d, boss_h, bolt_d, bolt_circle, bolt_count.
    anchors: "mount" (top-center of boss), "base" (bottom), "boss_axis".
- spur_gear: module, teeth, width, bore_d, pressure_angle.
    anchors: "bore_base" (z=0 bore center), "bore_top".
- ring_gear: module, teeth, width, rim_width. Internal gear. anchors: "bore_base","bore_top","axis".
- planetary_gearset: module, sun_teeth, planet_teeth, width, rim_width, n_planets. A complete meshed
    planetary set (sun+planets+ring), correctly phased. anchors: "base","top","axis". (Ring teeth =
    sun + 2*planet; (sun+ring) must be divisible by n_planets.)
- spacer: outer_d, bore_d, height (a bored standoff for stacking parts off the plate).
    anchors: "bottom" (z=0 center), "top" (z=height center).
- bolt: shank_d, length, head_d, head_h.   anchors: "seat" (under-head), "tip", "head_top".
- nut: af (width across flats), height, bore_d.   anchors: "bottom", "top".
- washer: outer_d, bore_d, thick.   anchors: "bottom", "top".
- bearing: outer_d, bore_d, width (simplified envelope).   anchors: "bore_base", "bore_top".
Real catalog parts (build123d/bd_warehouse) — prefer these when the user names a standard part:
- extrusion: rail_size in {"20x20","20x40","20x60","20x80","40x40"}, length. Aluminum V-slot, along +Z.
    anchors: "base","top","center".
- ball_bearing: size "bore-OD-width" e.g. "M8-22-7" (=608). anchors: "bore_base","bore_top".
- screw: size e.g. "M5-0.8", length, simple(bool). ISO socket-head cap screw, head up / shank down.
    anchors: "seat" (under-head), "tip", "head_top".
- bearing_block: width, depth, height, bore_d, bolt_d, bolt_spacing. A pillow block with a vertical
    bearing bore and two horizontal screw holes. JOINTS (oriented): "mount" (back face — coincides with a
    rail slot), "top" (front face, screw heads seat here), "bore" (+Z = bore axis, a bearing 'bore_mid'
    aligns here), "bolt0"/"bolt1" (screw axis = the through-hole).
- ball_bearing also has "bore_mid" (axis-aligned center seat) besides bore_base/bore_top.
- extrusion also has slot-mount joints "slot_xp/xn/yp/yn" on its four faces (a block's "mount" coincides
    with one of these to bolt onto the rail).

- panel: width, depth, height. A plain rectangular board/member/box (stud, wall plate, floor slab,
    sheathing). anchors: "base","top","center","x_pos","x_neg","y_pos","y_neg" (side-face centers).
- link: length, width, thickness. A planar linkage bar (rounded ends + a pivot bore at each end) for posing
    mechanisms. anchors: "p0" (pivot x=0), "p1" (pivot x=length), "center".
- sheet_bracket: thickness, base_length, flange_length, width, bend_radius. A constant-thickness sheet-metal
    L-bracket (one 90° bend). anchors: "base","mount" (base top),"flange_end","flange_face","bend".
- shaft: diameter, length. A round shaft along +Z. anchors: "base","top","mid", and "key" (off-axis).
- threaded_rod: major_diameter, pitch, length, or just `designation` (e.g. "M6x1") + length with
    resolve_specs — a REAL helical thread whose pitch comes from the resolved spec. anchors: "base","top","axis".
- four_bar: ground, crank, coupler, rocker, input_angle_deg. A planar 4-bar linkage posed by the
    SolveSpace geometric solver (bars in z=0). anchors: joints "A","B","C","D".
- sprocket: num_teeth, chain_pitch, thickness, bore_d. Roller-chain sprocket, axis Z. anchors: "face_a","face_b","center".
- flange: nps (e.g. '1','2'), flange_class (150/300/...), kind (weld_neck/slip_on/blind). Pipe flange. anchors: "bore_base","bore_top".
- pipe: nps, length, material, identifier ('40'). Straight pipe along +Z. anchors: "end_a","end_b","center".

Mate type "rigid" is a joint mate that aligns oriented joint frames (position AND axis) — use it for
joint-to-joint connections (e.g. block.mount -> extrusion.slot_yp, bearing.bore_mid -> block.bore,
screw.seat -> block.bolt0). It behaves like coincident but signals joint intent.
Kinematic mates: "revolute" = rigid + a rotation of "angle_deg" about the shared axis (a shaft spinning
in a bore, a hinge); "cylindrical" = revolute + a "slide" (mm) along the axis. Use these to mate a shaft
into a bearing/bore: a="bearing_or_block" a_joint="bore" b="shaft" b_joint="mid" type="revolute" angle_deg=…

IMPORTANT: make the FIRST part in the list the fixed BASE (e.g. the extrusion/frame), and in each mate
let A be the already-placed part and B the new part being added, so parts attach to the base in order.

The plate ALSO exposes per-hole anchors "bolt0","bolt1",... at each bolt-circle hole on its top face,
so a bolt can seat in a specific hole: a="plate" a_joint="bolt0" b="bolt" b_joint="seat" type="coincident".
Size bolt `length` to what it fastens (~plate thickness + any stacked parts); avoid long shanks that
poke far out the back. Make a bolt's shank_d match the plate bolt_d, and washer/nut bore_d match shank_d.

Mate types:
- "coincident": lands B's b_joint frame EXACTLY on A's a_joint frame (use to seat parts by anchor).
- "contact": drops B so its lowest face rests on A's a_joint plane, centered there (geometry seat;
    b_joint is ignored — use when B has no clean base anchor).
- "mesh": for TWO spur_gears only — places gear B meshing with gear A at the correct center distance
    (auto-computed from module+teeth), coplanar, at "angle_deg" around A's axis (default 0). The two
    gears MUST share the same module to mesh. Use a_joint="bore_base", b_joint="bore_base".

Mate-by-INTENT (preferred when you know HOW parts connect but not the exact frame names): instead of
a_joint/b_joint/type, give an "intent" and the resolver derives the frames + mate type deterministically:
- "seat_on": B sits on top of A (gear on a plate boss, a stacked spacer). a="plate" b="gear" intent="seat_on".
- "insert": B's axis drops into A's bore (a shaft journalled in a block/bearing bore). Add angle_deg + set
    type="revolute" if B should SPIN. a="block" b="shaft" intent="insert".
- "fasten": fastener B seats in A's bolt hole (optionally hole_index=N). a="plate" b="bolt" intent="fasten".
- "mesh": two spur_gears mesh. a="gear1" b="gear2" intent="mesh" angle_deg=…
Use intent OR explicit a_joint/b_joint+type, not both unless overriding one frame.

Rules: the FIRST part is fixed at origin. Mates are applied in order, and B is placed relative to A's
CURRENT position, so chains compose (plate->spacer->gear). To seat a gear on a plate boss:
a="plate" a_joint="mount" b="gear" b_joint="bore_base" (or simply intent="seat_on"). To stack via a spacer:
plate.mount->spacer.bottom, then spacer.top->gear.bore_base. Match mating diameters (plate boss_d ==
spacer bore_d/gear bore_d). Choose sensible engineering values. Respond with ONLY the JSON spec."""


LAYOUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parts", "mates"],
    "properties": {
        "parts": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "type", "params"],
                "properties": {
                    "id": {"type": "string"},
                    "type": {"enum": ["rect_panel", "disc", "bracket"]},
                    "material": {"type": "string"},
                    "params": {"type": "object"},
                },
            },
        },
        "mates": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["a", "a_joint", "b", "b_joint"],
                "properties": {
                    "a": {"type": "string"}, "b": {"type": "string"},
                    "a_joint": {"type": "string"}, "b_joint": {"type": "string"},
                    "offset": {"type": "array", "items": {"type": "number"}},
                },
            },
        },
    },
}

SYSTEM_LAYOUT = """You are a 2D CAD layout planner (parts cut flat from sheet -> DXF for laser/CNC).
Convert the request into a planar layout spec.

Part types and params (mm), each with named 2D anchor points:
- rect_panel: width, height, hole_d, hole_inset, rotate(deg).
    anchors: center, left, right, top, bottom (edge midpoints), tl, tr, bl, br (corners).
- disc: diameter, bore_d, rotate.   anchors: center, left, right, top, bottom (on the rim).
- bracket: arm, width, rotate.       anchors: corner, x_end, y_end, center.

Placement: each mate lands B's b_joint point on A's a_joint point, plus an optional "offset":[dx,dy]
gap (mm). The FIRST part is fixed at origin; mates apply in order; B is placed relative to A's current
position, so chains compose. To put panel B to the RIGHT of panel A with a 10mm gap, top-aligned:
a=A a_joint="right" b=B b_joint="left" offset=[10,0]. Keep parts from overlapping by choosing offsets
>= the gap you want. Respond with ONLY the JSON spec."""


# INTENT schema (the design front-end): the model emits a structured INTENT, not geometry. `kind` is a
# FREE STRING on purpose — so it can name a part we don't have (synchronizer, helical_gear) and the
# deterministic resolver honestly DECLINES it, rather than the schema forcing a wrong-but-valid substitution.
INTENT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["entities"],
    "properties": {
        "entities": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False, "required": ["id", "kind", "requirements"],
                "properties": {
                    "id": {"type": "string"}, "kind": {"type": "string"},
                    "requirements": {"type": "object"}, "designation": {"type": "string"},
                },
            },
        },
        "interfaces": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False, "required": ["a", "b", "relation"],
                "properties": {"a": {"type": "string"}, "b": {"type": "string"},
                               "relation": {"enum": ["seat_on", "insert", "fasten", "mesh"]}},
            },
        },
    },
}

# The parse rules MEASURED to take the intent path's assembly score 6/12 -> 12/12 (fixed-base-first,
# interface direction, encode negations). Same doctrine as plan(): qwen for semantics, kernel for the rest.
SYSTEM_INTENT = """You convert a natural-language part/assembly request into a structured design INTENT (NOT geometry).

An intent lists ENTITIES (parts/subsystems) and INTERFACES (how they connect). For each entity:
- id: a short unique name.
- kind: a part type you know, OR — if you have NO matching type — the REAL name of the thing
  (e.g. "synchronizer", "helical_gear", "tapered_bearing", "shift_fork"). NEVER substitute a different
  part for one you lack; name it honestly and it will be declined downstream.
- requirements: ONLY the values the user explicitly stated; OMIT anything unstated (defaults fill downstream).
  Use these param names (mm): spacer{outer_d,bore_d,height}; spur_gear{module,teeth,width,bore_d};
  plate{width,depth,thick,boss_d,boss_h,bolt_d,bolt_circle,bolt_count}; shaft{diameter,length};
  washer{outer_d,bore_d,thick}; nut{af,height,bore_d}; bolt{shank_d,length,head_d,head_h};
  panel{width,depth,height}; link{length,width,thickness}; housing{width,depth,height,wall,bore_d};
  sheet_bracket{thickness,base_length,flange_length,width}; bearing{outer_d,bore_d,width};
  ring_gear{module,teeth,width,rim_width}; planetary_gearset{module,sun_teeth,planet_teeth,width,n_planets};
  extrusion{rail_size,length}; bearing_block{width,depth,height,bore_d,bolt_d,bolt_spacing};
  ball_bearing{size}; screw{size,length}; sprocket{num_teeth,chain_pitch,thickness,bore_d};
  flange{nps,flange_class,kind}; pipe{nps,length}.
  Use these EXACT kind names: a V-slot rail is "extrusion" (NOT "2020_extrusion"); a pillow block is
  "bearing_block" (NOT "pillow_block_bearing"); a pipe flange is "flange" (NOT "weld_neck_flange" — put the
  type in params). For a standard part add "designation" (e.g. "M6x1").

INTERFACES connect entities; relation in [seat_on, insert, fasten, mesh].

RULES:
(1) List the FIXED BASE entity FIRST — the part others attach to (a plate, a housing, the ground/largest part).
(2) In an interface, "a" is that base/target and "b" is the part placed onto it (a plate is "a", the
    gear/bolt/washer seated on it is "b"; for fasten the holed part like the plate is "a", the fastener is "b").
(3) Capture EVERY stated number, and encode negations/explicit values: "no bore"/"solid" -> bore_d:0.

Examples:
- "a gear" -> {"entities":[{"id":"g","kind":"spur_gear","requirements":{}}],"interfaces":[]}
- "a solid spacer, 20mm OD, 10mm tall, no bore" -> {"entities":[{"id":"s","kind":"spacer","requirements":{"outer_d":20,"bore_d":0,"height":10}}],"interfaces":[]}
- "a 24-tooth m1 gear seated on a plate's boss" -> {"entities":[{"id":"plate","kind":"plate","requirements":{}},{"id":"g","kind":"spur_gear","requirements":{"module":1,"teeth":24}}],"interfaces":[{"a":"plate","b":"g","relation":"seat_on"}]}
- "a synchronizer ring" -> {"entities":[{"id":"s","kind":"synchronizer","requirements":{}}],"interfaces":[]}

Respond with ONLY the JSON intent."""


def plan_intent(request: str, *, url: str | None = None, model: str | None = None,
                timeout: float = 120.0) -> dict:
    """NL -> structured design INTENT (the parse stage of the intent layer), on the resident qwen model."""
    return _chat(SYSTEM_INTENT, request, INTENT_SCHEMA, url, model, timeout)


def _chat(system: str, request: str, schema: dict, url: str | None, model: str | None,
          timeout: float) -> dict:
    url = (url or os.environ.get("PLANNER_URL", "http://localhost:8000/v1")).rstrip("/")
    model = model or os.environ.get("MODEL_PLANNER", "qwen27b")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": request}],
        "temperature": 0.1, "max_tokens": 1800,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "spec", "schema": schema, "strict": True}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = httpx.post(f"{url}/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def plan_layout(request: str, *, url: str | None = None, model: str | None = None,
                timeout: float = 120.0) -> dict:
    return _chat(SYSTEM_LAYOUT, request, LAYOUT_SCHEMA, url, model, timeout)


def plan(request: str, *, url: str | None = None, model: str | None = None,
         timeout: float = 120.0) -> dict:
    url = (url or os.environ.get("PLANNER_URL", "http://localhost:8000/v1")).rstrip("/")
    model = model or os.environ.get("MODEL_PLANNER", "qwen27b")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": request},
        ],
        "temperature": 0.1,
        "max_tokens": 1800,
        # OpenAI-standard structured output (vLLM maps it to guided decoding). More widely honored
        # than vLLM's top-level `guided_json` extension, which this server silently ignored.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "assembly_spec", "schema": SPEC_SCHEMA, "strict": True},
        },
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = httpx.post(f"{url}/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    return json.loads(content)
