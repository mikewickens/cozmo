"""
Cozmo web control panel — Anki SDK edition.

Runs the Cozmo SDK in a worker thread, exposes a Flask web UI for camera,
drive, head, lift, lights, volume and TTS (real Cozmo voice).

Launch with the Python 3.7 venv:
    $env:PATH = "C:\\Cozmo\\platform-tools;$env:PATH"
    .\\.venv37\\Scripts\\python.exe webapp_sdk.py
"""

import io
import os
import queue
import random
import threading
import time
import urllib.request
import urllib.parse
import json

from flask import Flask, Response, jsonify, render_template, request

import cozmo
from cozmo.anim import Triggers
from cozmo.behavior import BehaviorTypes
from cozmo.objects import (
    LightCube1Id, LightCube2Id, LightCube3Id,
    EvtObjectTapped, EvtObjectConnectChanged,
    EvtObjectMovingStarted, EvtObjectMovingStopped,
)
from cozmo.song import SongNote, NoteTypes, NoteDurations
from cozmo.util import degrees, distance_mm, speed_mmps


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

_cmd_queue: "queue.Queue[tuple]" = queue.Queue()
_connected = threading.Event()
_robot: "cozmo.robot.Robot | None" = None
_current_behavior = None
_freeplay_on = False

_frame_lock = threading.Lock()
_latest_jpeg: bytes = b""

# 0.0–1.0 ratio applied before every say_text call. 1.0 = full volume.
_speech_volume = 1.0


# ---------------------------------------------------------------------------
# Camera handler — called from SDK thread
# ---------------------------------------------------------------------------
def _on_new_camera_image(evt, **kwargs):
    global _latest_jpeg
    pil_image = evt.image.raw_image  # PIL.Image, grayscale 320x240
    buf = io.BytesIO()
    pil_image.convert("RGB").save(buf, format="JPEG", quality=70)
    with _frame_lock:
        _latest_jpeg = buf.getvalue()


# ---------------------------------------------------------------------------
# Command dispatcher
# ---------------------------------------------------------------------------
DRIVE_SPEED = 80      # mm/s straight
TURN_SPEED  = 60      # wheel diff for turn

_LIGHT_MAP = {
    "green": cozmo.lights.green_light,
    "red":   cozmo.lights.red_light,
    "blue":  cozmo.lights.blue_light,
    "white": cozmo.lights.white_light,
    "off":   cozmo.lights.off_light,
}


_BEHAVIOR_MAP = {
    "find_faces":     BehaviorTypes.FindFaces,
    "look_around":    BehaviorTypes.LookAroundInPlace,
    "pounce":         BehaviorTypes.PounceOnMotion,
    "roll_block":     BehaviorTypes.RollBlock,
    "stack_blocks":   BehaviorTypes.StackBlocks,
    "knock_cubes":    BehaviorTypes.KnockOverCubes,
}


# ---------------------------------------------------------------------------
# Powered-cube subsystem (batteries installed!)
# ---------------------------------------------------------------------------
_CUBE_IDS = (LightCube1Id, LightCube2Id, LightCube3Id)
_CUBE_SLOT = {LightCube1Id: 1, LightCube2Id: 2, LightCube3Id: 3}

def _mk_color(rgb):
    return cozmo.lights.Light(on_color=cozmo.lights.Color(rgb=rgb))

_CUBE_COLORS = {
    "red":     cozmo.lights.red_light,
    "green":   cozmo.lights.green_light,
    "blue":    cozmo.lights.blue_light,
    "white":   cozmo.lights.white_light,
    "yellow":  _mk_color((255, 200, 0)),
    "magenta": _mk_color((255, 0, 180)),
    "cyan":    _mk_color((0, 220, 220)),
    "orange":  _mk_color((255, 90, 0)),
    "purple":  _mk_color((140, 0, 255)),
    "off":     cozmo.lights.off_light,
}

_cube_state = {
    1: {"connected": False, "taps": 0, "last_tap_ms": 0, "moving": False, "color": "off"},
    2: {"connected": False, "taps": 0, "last_tap_ms": 0, "moving": False, "color": "off"},
    3: {"connected": False, "taps": 0, "last_tap_ms": 0, "moving": False, "color": "off"},
}
_cube_state_lock = threading.Lock()
_cube_react_enabled = False
_cube_anim_stop = threading.Event()
_cube_anim_thread = None  # type: threading.Thread | None


def _cube_slot(obj):
    """Return 1/2/3 for a cube object, or None."""
    return _CUBE_SLOT.get(getattr(obj, "cube_id", None) or getattr(obj, "object_id", None))


def _on_cube_tapped(evt, obj=None, **kw):
    slot = _cube_slot(obj)
    if slot is None:
        return
    with _cube_state_lock:
        _cube_state[slot]["taps"] += 1
        _cube_state[slot]["last_tap_ms"] = int(time.time() * 1000)
    if _cube_react_enabled and _robot is not None:
        try:
            _robot.play_anim_trigger(Triggers.CodeLabHappy, in_parallel=True)
        except Exception:
            pass


def _on_cube_connect(evt, obj=None, connected=None, **kw):
    slot = _cube_slot(obj)
    if slot is None:
        return
    with _cube_state_lock:
        _cube_state[slot]["connected"] = bool(connected)
    print(f"[cube] slot {slot} connected={connected}")


def _on_cube_move_start(evt, obj=None, **kw):
    slot = _cube_slot(obj)
    if slot is not None:
        with _cube_state_lock:
            _cube_state[slot]["moving"] = True


def _on_cube_move_stop(evt, obj=None, **kw):
    slot = _cube_slot(obj)
    if slot is not None:
        with _cube_state_lock:
            _cube_state[slot]["moving"] = False


def _refresh_cube_connected_flags(robot):
    for cid, slot in _CUBE_SLOT.items():
        cube = robot.world.get_light_cube(cid)
        with _cube_state_lock:
            _cube_state[slot]["connected"] = bool(cube and cube.is_connected)


def _connected_cubes(robot):
    out = []
    for cid in _CUBE_IDS:
        cube = robot.world.get_light_cube(cid)
        if cube and cube.is_connected:
            out.append(cube)
    return out


def _set_cube_color(robot, slot, color_name):
    """slot: 1/2/3/'all'. color_name from _CUBE_COLORS."""
    light = _CUBE_COLORS.get(color_name, cozmo.lights.off_light)
    if slot == "all":
        targets = _connected_cubes(robot)
    else:
        try:
            slot_i = int(slot)
        except (TypeError, ValueError):
            return
        cid = {1: LightCube1Id, 2: LightCube2Id, 3: LightCube3Id}.get(slot_i)
        cube = robot.world.get_light_cube(cid) if cid else None
        targets = [cube] if cube and cube.is_connected else []
    for cube in targets:
        try:
            cube.set_lights(light)
            with _cube_state_lock:
                _cube_state[_CUBE_SLOT[cube.cube_id]]["color"] = color_name
        except Exception as e:
            print(f"[cube_light] {e}")


def _stop_cube_anim():
    global _cube_anim_thread
    _cube_anim_stop.set()
    if _cube_anim_thread and _cube_anim_thread.is_alive():
        _cube_anim_thread.join(timeout=1.5)
    _cube_anim_thread = None
    _cube_anim_stop.clear()


def _start_cube_anim(robot, pattern):
    global _cube_anim_thread
    _stop_cube_anim()
    cubes = _connected_cubes(robot)
    if not cubes:
        return
    colors_cycle = [
        _CUBE_COLORS["red"], _CUBE_COLORS["orange"], _CUBE_COLORS["yellow"],
        _CUBE_COLORS["green"], _CUBE_COLORS["cyan"], _CUBE_COLORS["blue"],
        _CUBE_COLORS["purple"], _CUBE_COLORS["magenta"],
    ]

    def _loop():
        step = 0
        try:
            while not _cube_anim_stop.is_set():
                cs = _connected_cubes(robot)
                if not cs:
                    time.sleep(0.3); continue
                if pattern == "rainbow":
                    for i, c in enumerate(cs):
                        c.set_lights(colors_cycle[(step + i) % len(colors_cycle)])
                    delay = 0.25
                elif pattern == "chase":
                    for i, c in enumerate(cs):
                        c.set_lights(_CUBE_COLORS["green"] if i == (step % len(cs)) else cozmo.lights.off_light)
                    delay = 0.30
                elif pattern == "breathe":
                    # alternating on/off pulse
                    on = (step % 2 == 0)
                    light = _CUBE_COLORS["blue"] if on else cozmo.lights.off_light
                    for c in cs:
                        c.set_lights(light)
                    delay = 0.55
                elif pattern == "party":
                    import random as _r
                    for c in cs:
                        c.set_lights(_r.choice(colors_cycle))
                    delay = 0.18
                else:
                    break
                step += 1
                _cube_anim_stop.wait(delay)
        except Exception as e:
            print(f"[cube_anim] {e}")
        finally:
            for c in cs if cs else []:
                try: c.set_lights_off()
                except Exception: pass

    _cube_anim_thread = threading.Thread(target=_loop, daemon=True)
    _cube_anim_thread.start()


# ---------- Mini-games ----------

def _game_simon(robot, args):
    rounds = max(1, min(20, int(args.get("rounds", 8))))
    cubes = _connected_cubes(robot)
    if len(cubes) < 2:
        try:
            robot.say_text("I need at least two cubes connected", in_parallel=True).wait_for_completed()
        except Exception: pass
        return
    base_colors = [_CUBE_COLORS["red"], _CUBE_COLORS["green"], _CUBE_COLORS["blue"]]
    color_by_id = {c.object_id: base_colors[i % 3] for i, c in enumerate(cubes)}
    for c in cubes:
        c.set_lights(color_by_id[c.object_id])
    sequence = []
    try:
        robot.say_text("Simon says, watch closely", in_parallel=True).wait_for_completed()
    except Exception: pass
    for r_i in range(1, rounds + 1):
        sequence.append(random.choice(cubes))
        # Playback
        for c in cubes: c.set_lights_off()
        time.sleep(0.45)
        for c in sequence:
            c.set_lights(cozmo.lights.white_light)
            time.sleep(0.55)
            c.set_lights(color_by_id[c.object_id])
            time.sleep(0.20)
        # User input
        for expected in sequence:
            try:
                ev = robot.world.wait_for(EvtObjectTapped, timeout=8.0)
                tapped_id = ev.obj.object_id
            except Exception:
                tapped_id = None
            if tapped_id != expected.object_id:
                for c in cubes: c.set_lights(_CUBE_COLORS["red"])
                try:
                    robot.play_anim_trigger(Triggers.MajorFail, in_parallel=True)
                    robot.say_text(f"Game over. You got {r_i - 1}", in_parallel=True).wait_for_completed()
                except Exception: pass
                time.sleep(1.0)
                for c in cubes: c.set_lights_off()
                return
        # round won
        try:
            robot.play_anim_trigger(Triggers.CodeLabHappy, in_parallel=True)
        except Exception: pass
        time.sleep(0.8)
    try:
        robot.play_anim_trigger(Triggers.MajorWin, in_parallel=True)
        robot.say_text("You win!", in_parallel=True).wait_for_completed()
    except Exception: pass
    for c in cubes: c.set_lights_off()


def _game_quicktap(robot, args):
    rounds = max(1, min(10, int(args.get("rounds", 5))))
    cubes = _connected_cubes(robot)
    if not cubes:
        try:
            robot.say_text("No cubes connected", in_parallel=True).wait_for_completed()
        except Exception: pass
        return
    try:
        robot.say_text("Quick tap. Tap on green, not on red", in_parallel=True).wait_for_completed()
    except Exception: pass
    times = []
    score = 0
    for r_i in range(rounds):
        for c in cubes: c.set_lights(_CUBE_COLORS["red"])
        time.sleep(random.uniform(1.5, 3.5))
        for c in cubes: c.set_lights(_CUBE_COLORS["green"])
        t0 = time.time()
        try:
            ev = robot.world.wait_for(EvtObjectTapped, timeout=3.0)
            dt = time.time() - t0
            times.append(dt)
            score += 1
            for c in cubes: c.set_lights(_CUBE_COLORS["blue"])
        except Exception:
            for c in cubes: c.set_lights(_CUBE_COLORS["red"])
        time.sleep(0.7)
    for c in cubes: c.set_lights_off()
    if times:
        avg_ms = int(sum(times) / len(times) * 1000)
        try:
            robot.say_text(f"You scored {score} out of {rounds}. Average {avg_ms} milliseconds", in_parallel=True).wait_for_completed()
        except Exception: pass
    else:
        try:
            robot.say_text("Too slow!", in_parallel=True).wait_for_completed()
        except Exception: pass



# ---------------------------------------------------------------------------
# Songs — sequences of (pitch, duration) tuples for robot.play_song()
# Cozmo's vocal range is C2..C3#.  Notes outside that get clamped silently.
# ---------------------------------------------------------------------------
def _n(pitch, dur="Quarter"):
    return SongNote(getattr(NoteTypes, pitch), getattr(NoteDurations, dur))


SONGS = {
    "twinkle": [  # Twinkle Twinkle Little Star
        _n("C2"), _n("C2"), _n("G2"), _n("G2"), _n("A2"), _n("A2"), _n("G2", "Half"),
        _n("F2"), _n("F2"), _n("E2"), _n("E2"), _n("D2"), _n("D2"), _n("C2", "Half"),
        _n("G2"), _n("G2"), _n("F2"), _n("F2"), _n("E2"), _n("E2"), _n("D2", "Half"),
        _n("G2"), _n("G2"), _n("F2"), _n("F2"), _n("E2"), _n("E2"), _n("D2", "Half"),
        _n("C2"), _n("C2"), _n("G2"), _n("G2"), _n("A2"), _n("A2"), _n("G2", "Half"),
        _n("F2"), _n("F2"), _n("E2"), _n("E2"), _n("D2"), _n("D2"), _n("C2", "Whole"),
    ],
    "mary": [  # Mary Had A Little Lamb
        _n("E2"), _n("D2"), _n("C2"), _n("D2"), _n("E2"), _n("E2"), _n("E2", "Half"),
        _n("D2"), _n("D2"), _n("D2", "Half"), _n("E2"), _n("G2"), _n("G2", "Half"),
        _n("E2"), _n("D2"), _n("C2"), _n("D2"), _n("E2"), _n("E2"), _n("E2"), _n("E2"),
        _n("D2"), _n("D2"), _n("E2"), _n("D2"), _n("C2", "Whole"),
    ],
    "happy_bday": [  # Happy Birthday
        _n("C2"), _n("C2"), _n("D2", "Half"), _n("C2", "Half"), _n("F2", "Half"), _n("E2", "Whole"),
        _n("C2"), _n("C2"), _n("D2", "Half"), _n("C2", "Half"), _n("G2", "Half"), _n("F2", "Whole"),
        _n("C2"), _n("C2"), _n("C3", "Half"), _n("A2", "Half"), _n("F2", "Half"), _n("E2", "Half"), _n("D2", "Whole"),
        _n("A2_Sharp"), _n("A2_Sharp"), _n("A2", "Half"), _n("F2", "Half"), _n("G2", "Half"), _n("F2", "Whole"),
    ],
    "ode_to_joy": [  # Ode to Joy
        _n("E2"), _n("E2"), _n("F2"), _n("G2"),
        _n("G2"), _n("F2"), _n("E2"), _n("D2"),
        _n("C2"), _n("C2"), _n("D2"), _n("E2"),
        _n("E2", "ThreeQuarter"), _n("D2", "Quarter"), _n("D2", "Half"),
        _n("E2"), _n("E2"), _n("F2"), _n("G2"),
        _n("G2"), _n("F2"), _n("E2"), _n("D2"),
        _n("C2"), _n("C2"), _n("D2"), _n("E2"),
        _n("D2", "ThreeQuarter"), _n("C2", "Quarter"), _n("C2", "Half"),
    ],
    "jingle": [  # Jingle Bells (chorus)
        _n("E2"), _n("E2"), _n("E2", "Half"),
        _n("E2"), _n("E2"), _n("E2", "Half"),
        _n("E2"), _n("G2"), _n("C2", "ThreeQuarter"), _n("D2", "Quarter"), _n("E2", "Whole"),
        _n("F2"), _n("F2"), _n("F2"), _n("F2"),
        _n("F2"), _n("E2"), _n("E2"), _n("E2"),
        _n("E2"), _n("D2"), _n("D2"), _n("E2"),
        _n("D2", "Half"), _n("G2", "Half"),
    ],
    "scale_up": [  # Ascending scale
        _n("C2"), _n("D2"), _n("E2"), _n("F2"),
        _n("G2"), _n("A2"), _n("B2"), _n("C3", "Half"),
    ],
    "scale_down": [
        _n("C3"), _n("B2"), _n("A2"), _n("G2"),
        _n("F2"), _n("E2"), _n("D2"), _n("C2", "Half"),
    ],
    "fanfare": [  # Quick triumphant fanfare
        _n("C2"), _n("E2"), _n("G2"), _n("C3", "Half"),
        _n("G2", "Quarter"), _n("C3", "Whole"),
    ],
    "doorbell": [
        _n("E2", "Half"), _n("C2", "Whole"),
    ],
    "alarm": [
        _n("C3"), _n("Rest"), _n("C3"), _n("Rest"),
        _n("C3"), _n("Rest"), _n("C3"), _n("Rest"),
    ],
}


def _stop_current_behavior():
    global _current_behavior
    if _current_behavior is not None:
        try:
            _current_behavior.stop()
        except Exception:
            pass
        _current_behavior = None


def _prep_for_cube_action(robot):
    """Lift down, head looking forward — required posture for pickup/place/stack.
    Cancel any in-flight actions first so the next non-parallel action isn't blocked."""
    try:
        if robot.is_on_charger:
            robot.drive_off_charger_contacts().wait_for_completed()
    except Exception:
        pass
    try:
        robot.abort_all_actions(log_abort_messages=False)
    except Exception:
        pass
    try: robot.stop_all_motors()
    except Exception: pass
    # Sequential so they're DONE before the next action starts
    try: robot.set_lift_height(0.0).wait_for_completed()
    except Exception: pass
    try: robot.set_head_angle(degrees(0)).wait_for_completed()
    except Exception: pass


def _visible_cubes(robot):
    """Currently-observable LightCube objects with a valid pose."""
    out = []
    for cid in _CUBE_IDS:
        c = robot.world.get_light_cube(cid)
        if c and c.pose and c.pose.is_comparable(robot.pose):
            out.append(c)
    return out


def _find_cube(robot, timeout=4.0):
    """Return any one observable cube, scanning briefly if none seen yet."""
    cs = _visible_cubes(robot)
    if cs:
        return cs[0]
    try:
        return robot.world.wait_for_observed_light_cube(timeout=timeout, include_existing=True)
    except Exception:
        return None


def _find_n_cubes(robot, n=2, timeout=10.0):
    """Look around in place until at least n cubes are localized. Returns the list."""
    cs = _visible_cubes(robot)
    if len(cs) >= n:
        return cs[:n]
    look = None
    try:
        look = robot.start_behavior(BehaviorTypes.LookAroundInPlace)
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(0.4)
            cs = _visible_cubes(robot)
            if len(cs) >= n:
                break
    finally:
        if look is not None:
            try: look.stop()
            except Exception: pass
            try: robot.abort_all_actions(log_abort_messages=False)
            except Exception: pass
    return _visible_cubes(robot)[:n]


def _highlight_cube(cube, color="white"):
    try:
        cube.set_lights(_CUBE_COLORS.get(color, cozmo.lights.white_light))
    except Exception:
        pass


def _clear_cube_lights(cubes):
    for c in cubes:
        try: c.set_lights_off()
        except Exception: pass


# ---------- Advanced cube tricks ----------

def _do_pickup(robot, cube):
    _prep_for_cube_action(robot)
    _highlight_cube(cube, "green")
    try:
        robot.pickup_object(cube, num_retries=3, use_pre_dock_pose=True).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _do_place_ground(robot, cube):
    try:
        robot.place_object_on_ground_here(cube, num_retries=2).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _do_wheelie(robot, cube):
    _prep_for_cube_action(robot)
    _highlight_cube(cube, "magenta")
    try:
        robot.pop_a_wheelie(cube, num_retries=2).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _do_roll(robot, cube):
    _prep_for_cube_action(robot)
    _highlight_cube(cube, "yellow")
    try:
        robot.roll_cube(cube, num_retries=3).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _do_stack(robot, args):
    """Pick up one cube and stack it onto a second cube."""
    _prep_for_cube_action(robot)
    cubes = _find_n_cubes(robot, n=2, timeout=10.0)
    if len(cubes) < 2:
        try:
            robot.say_text("I need to see two cubes", in_parallel=True).wait_for_completed()
        except Exception: pass
        return
    pick, target = cubes[0], cubes[1]
    _highlight_cube(pick,   "blue")
    _highlight_cube(target, "green")
    try:
        robot.pickup_object(pick, num_retries=3).wait_for_completed()
        robot.place_on_object(target, num_retries=2).wait_for_completed()
        robot.play_anim_trigger(Triggers.CodeLabHappy, in_parallel=True)
    except Exception as e:
        print(f"[stack] {e}")
        try: robot.play_anim_trigger(Triggers.CodeLabUnhappy, in_parallel=True)
        except Exception: pass
    finally:
        _clear_cube_lights(cubes)


def _do_pyramid(robot, args):
    """Stack all 3 cubes in a tower: c0 base, c1 on c0, c2 on c1."""
    _prep_for_cube_action(robot)
    cubes = _find_n_cubes(robot, n=3, timeout=14.0)
    if len(cubes) < 3:
        try:
            robot.say_text("I need to see all three cubes", in_parallel=True).wait_for_completed()
        except Exception: pass
        # fall back to 2-cube stack if possible
        if len(cubes) >= 2:
            return _do_stack(robot, args)
        return
    base, mid, top = cubes
    _highlight_cube(base, "green")
    _highlight_cube(mid,  "yellow")
    _highlight_cube(top,  "blue")
    try:
        # mid -> on base
        robot.pickup_object(mid, num_retries=3).wait_for_completed()
        robot.place_on_object(base, num_retries=2).wait_for_completed()
        # top -> on mid (now on top of base)
        robot.pickup_object(top, num_retries=3).wait_for_completed()
        robot.place_on_object(mid, num_retries=2).wait_for_completed()
        robot.play_anim_trigger(Triggers.MajorWin, in_parallel=True)
    except Exception as e:
        print(f"[pyramid] {e}")
        try: robot.play_anim_trigger(Triggers.CodeLabUnhappy, in_parallel=True)
        except Exception: pass
    finally:
        _clear_cube_lights(cubes)


def _do_align(robot, cube):
    """Drive to face the cube square-on at ~80mm."""
    try:
        robot.go_to_object(cube, distance_mm(80.0), num_retries=2).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _do_drive_to(robot, cube, dist=60.0):
    try:
        robot.go_to_object(cube, distance_mm(dist), num_retries=2).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _do_tap_cube(robot, cube):
    """Cozmo taps the cube with his lift."""
    _prep_for_cube_action(robot)
    _highlight_cube(cube, "cyan")
    try:
        # Drive close, then snap lift up & down a couple of times to tap it
        robot.go_to_object(cube, distance_mm(55.0), num_retries=2).wait_for_completed()
        for _ in range(2):
            robot.set_lift_height(0.9, duration=0.25).wait_for_completed()
            robot.set_lift_height(0.0, duration=0.25).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _do_push(robot, cube):
    """Lower lift, drive forward into the cube, then back off."""
    _prep_for_cube_action(robot)
    _highlight_cube(cube, "orange")
    try:
        robot.go_to_object(cube, distance_mm(40.0), num_retries=2).wait_for_completed()
        robot.drive_straight(distance_mm(120), speed_mmps(80)).wait_for_completed()
        robot.drive_straight(distance_mm(-60), speed_mmps(60)).wait_for_completed()
    finally:
        _clear_cube_lights([cube])


def _find_charger(robot, timeout=10.0):
    """Scan in place until the charger is seen, returns charger or None."""
    if robot.world.charger and robot.world.charger.pose and robot.world.charger.pose.is_comparable(robot.pose):
        return robot.world.charger
    # Sweep: turn in 60deg steps looking for the charger
    look = robot.start_behavior(BehaviorTypes.LookAroundInPlace)
    try:
        charger = robot.world.wait_for_observed_charger(timeout=timeout)
        return charger
    except Exception:
        return None
    finally:
        try: look.stop()
        except Exception: pass


def _go_home(robot):
    """Find the charger and dock onto it."""
    if robot.is_on_charger:
        robot.play_anim_trigger(Triggers.CodeLabHappy, in_parallel=True)
        return
    robot.set_lift_height(0.0, in_parallel=True)
    robot.set_head_angle(degrees(0), in_parallel=True)
    charger = _find_charger(robot, timeout=12.0)
    if charger is None:
        print("[go_home] charger not found")
        robot.play_anim_trigger(Triggers.CodeLabUnhappy, in_parallel=True)
        try:
            robot.say_text("I cannot find my charger", in_parallel=True).wait_for_completed()
        except Exception:
            pass
        return
    # Approach to ~60mm in front of charger marker, then turn 180 and back on
    try:
        action = robot.go_to_object(charger, distance_mm(60.0), num_retries=2)
        action.wait_for_completed()
    except Exception as exc:
        print(f"[go_home] approach failed: {exc}")
        return
    try:
        robot.turn_in_place(degrees(180)).wait_for_completed()
        robot.drive_straight(distance_mm(-100), speed_mmps(50)).wait_for_completed()
    except Exception as exc:
        print(f"[go_home] dock failed: {exc}")
    if robot.is_on_charger:
        robot.play_anim_trigger(Triggers.CodeLabHappy, in_parallel=True)
    else:
        # nudge back a bit more
        try:
            robot.drive_straight(distance_mm(-40), speed_mmps(30)).wait_for_completed()
        except Exception:
            pass


def _dispatch(robot: cozmo.robot.Robot, cmd: str, args: dict) -> None:
    global _current_behavior, _freeplay_on
    try:
        if cmd == "forward":
            robot.drive_wheels(DRIVE_SPEED, DRIVE_SPEED)
        elif cmd == "backward":
            robot.drive_wheels(-DRIVE_SPEED, -DRIVE_SPEED)
        elif cmd == "left":
            robot.drive_wheels(-TURN_SPEED, TURN_SPEED)
        elif cmd == "right":
            robot.drive_wheels(TURN_SPEED, -TURN_SPEED)
        elif cmd == "stop":
            robot.drive_wheels(0, 0)
        elif cmd == "head":
            angle = float(args.get("angle", 0))
            robot.set_head_angle(degrees(angle), in_parallel=True)
        elif cmd == "lift":
            ratio = float(args.get("ratio", 0))
            robot.set_lift_height(ratio, in_parallel=True)
        elif cmd == "lights":
            color = args.get("color", "off")
            robot.set_all_backpack_lights(_LIGHT_MAP.get(color, cozmo.lights.off_light))
        elif cmd == "volume":
            level = float(args.get("level", 32768)) / 65535.0
            robot.set_robot_volume(level)
        elif cmd == "speech_volume":
            global _speech_volume
            _speech_volume = max(0.0, min(1.0, float(args.get("level", 1.0))))
            print(f"[speech_volume] {_speech_volume:.2f}")
        elif cmd == "say":
            text = str(args.get("text", "")).strip()[:200]
            if text:
                try:
                    robot.set_robot_volume(_speech_volume)
                except Exception:
                    pass
                robot.say_text(
                    text,
                    in_parallel=True,
                    duration_scalar=float(args.get("duration_scalar", 1.0)),
                    voice_pitch=float(args.get("voice_pitch", 0.0)),
                ).wait_for_completed()

        # ---------- Animation triggers ----------
        elif cmd == "anim":
            name = args.get("trigger", "")
            trigger = getattr(Triggers, name, None)
            if trigger is None:
                print(f"[anim] unknown trigger: {name}")
            else:
                _stop_current_behavior()
                robot.play_anim_trigger(trigger, in_parallel=True, ignore_body_track=False)

        # ---------- Behaviors ----------
        elif cmd == "behavior":
            name = args.get("name", "")
            btype = _BEHAVIOR_MAP.get(name)
            if btype is None:
                print(f"[behavior] unknown: {name}")
            else:
                _stop_current_behavior()
                if _freeplay_on:
                    robot.stop_freeplay_behaviors()
                    _freeplay_on = False
                _current_behavior = robot.start_behavior(btype)

        elif cmd == "behavior_stop":
            _stop_current_behavior()
            if _freeplay_on:
                robot.stop_freeplay_behaviors()
                _freeplay_on = False
            robot.drive_wheels(0, 0)

        # ---------- Freeplay (Cozmo plays autonomously) ----------
        elif cmd == "freeplay":
            enable = bool(args.get("enable", True))
            _stop_current_behavior()
            if enable and not _freeplay_on:
                robot.start_freeplay_behaviors()
                _freeplay_on = True
            elif not enable and _freeplay_on:
                robot.stop_freeplay_behaviors()
                _freeplay_on = False

        # ---------- Quick canned moves ----------
        elif cmd == "move":
            kind = args.get("kind", "")
            if kind == "spin_left":
                robot.turn_in_place(degrees(90), in_parallel=True)
            elif kind == "spin_right":
                robot.turn_in_place(degrees(-90), in_parallel=True)
            elif kind == "spin360":
                robot.turn_in_place(degrees(360), in_parallel=True)
            elif kind == "fwd_short":
                robot.drive_straight(distance_mm(80), speed_mmps(80), in_parallel=True)
            elif kind == "back_short":
                robot.drive_straight(distance_mm(-80), speed_mmps(80), in_parallel=True)

        # ---------- Charger / Home ----------
        elif cmd == "go_home":
            _stop_current_behavior()
            if _freeplay_on:
                robot.stop_freeplay_behaviors()
                _freeplay_on = False
            _go_home(robot)

        elif cmd == "leave_charger":
            if robot.is_on_charger:
                try:
                    robot.drive_off_charger_contacts().wait_for_completed()
                except Exception as exc:
                    print(f"[leave_charger] {exc}")

        # ---------- Songs (pitched notes) ----------
        elif cmd == "sing":
            name = args.get("song", "")
            notes = SONGS.get(name)
            if not notes:
                print(f"[sing] unknown song: {name}")
            else:
                robot.play_song(notes, in_parallel=False).wait_for_completed()

        # ---------- Cube interactions ----------
        elif cmd == "cube":
            kind = args.get("kind", "")
            # Multi-cube actions don't need a single cube up front
            if kind == "stack":
                _do_stack(robot, args); return
            if kind == "pyramid":
                _do_pyramid(robot, args); return

            cube = _find_cube(robot, timeout=5.0)
            if cube is None:
                print("[cube] no cube visible")
                try: robot.play_anim_trigger(Triggers.CodeLabUnhappy, in_parallel=True)
                except Exception: pass
                try: robot.say_text("I cannot see a cube", in_parallel=True).wait_for_completed()
                except Exception: pass
                return

            if   kind == "pickup":  _do_pickup(robot, cube)
            elif kind == "place":   _do_place_ground(robot, cube)
            elif kind == "roll":    _do_roll(robot, cube)
            elif kind == "wheelie": _do_wheelie(robot, cube)
            elif kind == "align":   _do_align(robot, cube)
            elif kind == "drive_to":_do_drive_to(robot, cube)
            elif kind == "tap":     _do_tap_cube(robot, cube)
            elif kind == "push":    _do_push(robot, cube)
            else:
                print(f"[cube] unknown kind: {kind}")

        # ---------- Powered-cube lights ----------
        elif cmd == "cube_light":
            slot = args.get("slot", "all")
            color = args.get("color", "off")
            if color in ("rainbow", "chase", "breathe", "party"):
                _start_cube_anim(robot, color)
            else:
                _stop_cube_anim()
                _set_cube_color(robot, slot, color)

        elif cmd == "cube_lights_off":
            _stop_cube_anim()
            for c in _connected_cubes(robot):
                try: c.set_lights_off()
                except Exception: pass
            with _cube_state_lock:
                for s in _cube_state:
                    _cube_state[s]["color"] = "off"

        elif cmd == "cube_react":
            global _cube_react_enabled
            _cube_react_enabled = bool(args.get("enable", True))
            print(f"[cube] tap-react: {_cube_react_enabled}")

        elif cmd == "cube_connect":
            try:
                robot.world.connect_to_cubes()
            except Exception as e:
                print(f"[cube_connect] {e}")
            _refresh_cube_connected_flags(robot)

        elif cmd == "cube_reset_taps":
            with _cube_state_lock:
                for s in _cube_state:
                    _cube_state[s]["taps"] = 0

        # ---------- Cube mini-games ----------
        elif cmd == "game":
            kind = args.get("kind", "")
            _stop_cube_anim()
            if kind == "simon":
                _game_simon(robot, args)
            elif kind == "quicktap":
                _game_quicktap(robot, args)
            else:
                print(f"[game] unknown: {kind}")

    except Exception as exc:
        print(f"[dispatch:{cmd}] {exc}")


# ---------------------------------------------------------------------------
# Cozmo worker — connects via SDK and processes the command queue
# ---------------------------------------------------------------------------
def _cozmo_main(conn):
    global _robot
    robot = conn.wait_for_robot()
    _robot = robot

    robot.camera.image_stream_enabled = True
    robot.camera.color_image_enabled = False
    robot.add_event_handler(cozmo.world.EvtNewCameraImage, _on_new_camera_image)

    # Powered-cube events
    robot.world.add_event_handler(EvtObjectTapped, _on_cube_tapped)
    robot.world.add_event_handler(EvtObjectConnectChanged, _on_cube_connect)
    robot.world.add_event_handler(EvtObjectMovingStarted, _on_cube_move_start)
    robot.world.add_event_handler(EvtObjectMovingStopped, _on_cube_move_stop)
    try:
        robot.world.connect_to_cubes()
    except Exception as e:
        print(f"[cubes] connect_to_cubes: {e}")
    _refresh_cube_connected_flags(robot)

    _connected.set()
    print("[cozmo] Connected via SDK — ready.")

    try:
        while True:
            try:
                cmd, args = _cmd_queue.get(timeout=0.1)
                _dispatch(robot, cmd, args)
            except queue.Empty:
                pass
    except Exception as exc:
        print(f"[cozmo] worker exception: {exc}")
    finally:
        _connected.clear()
        _robot = None


def _cozmo_thread():
    while True:
        try:
            cozmo.connect(_cozmo_main)
        except Exception as exc:
            print(f"[cozmo] connection lost: {exc}. Retrying in 5s…")
        _connected.clear()
        time.sleep(5)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/status")
def status():
    info = {"connected": _connected.is_set()}
    r = _robot
    if r is not None:
        try:
            v = r.battery_voltage  # ~3.5 (low) to ~4.2 (full)
            info["battery_v"] = round(float(v), 2) if v else None
            # rough % from voltage curve
            if v:
                pct = max(0, min(100, int(round((v - 3.5) / (4.2 - 3.5) * 100))))
                info["battery_pct"] = pct
                info["battery_low"] = v < 3.6
            info["on_charger"] = bool(r.is_on_charger)
        except Exception:
            pass
    return jsonify(**info)


@app.route("/cubes")
def cubes_route():
    # Live cube state (connected / tap counts / motion / color)
    r = _robot
    if r is not None:
        # update connected flags lazily
        for cid, slot in _CUBE_SLOT.items():
            try:
                cube = r.world.get_light_cube(cid)
                with _cube_state_lock:
                    _cube_state[slot]["connected"] = bool(cube and cube.is_connected)
            except Exception:
                pass
    with _cube_state_lock:
        snap = {str(k): dict(v) for k, v in _cube_state.items()}
    return jsonify(cubes=snap, react=_cube_react_enabled)


@app.route("/video_feed")
def video_feed():
    def generate():
        while True:
            with _frame_lock:
                frame = _latest_jpeg
            if frame:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                )
            time.sleep(0.05)
    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/cmd", methods=["POST"])
def cmd_route():
    data = request.get_json(silent=True) or {}
    action = data.pop("action", None)
    if not action:
        return jsonify(ok=False, error="no action"), 400
    _cmd_queue.put((action, data))
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# Smart helpers: weather, jokes, random phrases — Cozmo speaks the results.
# ---------------------------------------------------------------------------
def _http_get(url, headers=None, timeout=6):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def _geolocate():
    """Best-effort IP geolocation — returns (lat, lon, city)."""
    try:
        data = json.loads(_http_get("http://ip-api.com/json/?fields=status,lat,lon,city"))
        if data.get("status") == "success":
            return float(data["lat"]), float(data["lon"]), data.get("city", "")
    except Exception as e:
        print(f"[geo] {e}")
    # fallback: London
    return 51.5074, -0.1278, "London"


_WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm",
}


def _fetch_weather_phrase():
    lat, lon, city = _geolocate()
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code,wind_speed_10m"
        "&temperature_unit=celsius&wind_speed_unit=kmh"
    )
    data = json.loads(_http_get(url))
    cur = data["current"]
    temp = round(cur["temperature_2m"])
    code = int(cur["weather_code"])
    desc = _WEATHER_CODES.get(code, "interesting weather")
    place = city or "your area"
    return f"In {place} it is {temp} degrees and {desc}."


def _fetch_joke():
    return _http_get(
        "https://icanhazdadjoke.com/",
        headers={"Accept": "text/plain", "User-Agent": "Cozmo Web Panel"},
    ).strip()


_RANDOM_PHRASES = [
    "Hey Mike, how are you today?",
    "Beep boop. I am thinking deep thoughts.",
    "Did you know I have a tiny computer for a brain?",
    "I love sunny days and cozy rooms.",
    "If I had legs, I would dance.",
    "Have you fed me electricity today?",
    "Robots have feelings too, you know.",
    "I dream of electric sheep.",
    "Beep. That was a polite beep.",
    "I am ninety nine percent sure that was a good idea.",
]


def _say(text, max_len=180):
    text = (text or "").strip()
    if not text:
        return False
    _cmd_queue.put(("say", {"text": text[:max_len]}))
    return True


# --- PC voice (Windows SAPI) — louder & clearer for triggering Alexa/Google ---
import subprocess

_pc_voice_lock = threading.Lock()


def _pc_say(text, rate=0, volume=100):
    """Speak `text` through the PC's default audio output using Windows SAPI.
    Runs in a background thread so HTTP returns immediately."""
    text = (text or "").strip()
    if not text:
        return False
    # Escape single quotes for PowerShell single-quoted string
    safe = text.replace("'", "''")
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Rate = {int(rate)}; "
        f"$s.Volume = {int(volume)}; "
        f"$s.Speak('{safe}')"
    )

    def _run():
        with _pc_voice_lock:
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=30,
                )
            except Exception as e:
                print(f"[pc_say] {e}")

    threading.Thread(target=_run, daemon=True).start()
    return True


@app.route("/pcsay", methods=["POST"])
def pcsay_route():
    """Speak through the host PC speakers (Windows SAPI).
    Body: {"text": "...", "rate": -3..3, "volume": 0..100}"""
    data = request.get_json(silent=True) or {}
    text   = data.get("text", "")
    rate   = int(data.get("rate", 0))
    volume = int(data.get("volume", 100))
    ok = _pc_say(text, rate=rate, volume=volume)
    return jsonify(ok=ok, said=text)


@app.route("/quicksay", methods=["POST"])
def quicksay_route():
    """Speak a preset/custom string. Body: {"text": "..."}"""
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    ok = _say(text)
    return jsonify(ok=ok, said=text)


@app.route("/weather", methods=["POST"])
def weather_route():
    try:
        phrase = _fetch_weather_phrase()
        _say(phrase)
        return jsonify(ok=True, said=phrase)
    except Exception as e:
        msg = "I cannot get the weather right now."
        _say(msg)
        return jsonify(ok=False, error=str(e), said=msg)


@app.route("/joke", methods=["POST"])
def joke_route():
    try:
        joke = _fetch_joke()
        _say(joke)
        return jsonify(ok=True, said=joke)
    except Exception as e:
        msg = "My joke book is closed right now."
        _say(msg)
        return jsonify(ok=False, error=str(e), said=msg)


@app.route("/random_phrase", methods=["POST"])
def random_phrase_route():
    phrase = random.choice(_RANDOM_PHRASES)
    _say(phrase)
    return jsonify(ok=True, said=phrase)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Make sure adb is on PATH so the SDK can find the tablet
    os.environ["PATH"] = r"C:\Cozmo\platform-tools;" + os.environ.get("PATH", "")

    t = threading.Thread(target=_cozmo_thread, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
