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
import threading
import time

from flask import Flask, Response, jsonify, render_template, request

import cozmo
from cozmo.anim import Triggers
from cozmo.behavior import BehaviorTypes
from cozmo.song import SongNote, NoteTypes, NoteDurations
from cozmo.util import degrees, distance_mm, speed_mmps


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
app = Flask(__name__)

_cmd_queue: "queue.Queue[tuple]" = queue.Queue()
_connected = threading.Event()
_robot: "cozmo.robot.Robot | None" = None
_current_behavior = None
_freeplay_on = False

_frame_lock = threading.Lock()
_latest_jpeg: bytes = b""


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


def _find_cube(robot, timeout=4.0):
    """Look around briefly and return the first visible cube, or None."""
    try:
        return robot.world.wait_for_observed_light_cube(timeout=timeout)
    except Exception:
        return None


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
        elif cmd == "say":
            text = str(args.get("text", "")).strip()[:200]
            if text:
                robot.say_text(text, in_parallel=True).wait_for_completed()

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
            cube = _find_cube(robot, timeout=5.0)
            if cube is None:
                print("[cube] no cube visible")
                robot.play_anim_trigger(Triggers.CodeLabUnhappy, in_parallel=True)
                return
            if kind == "pickup":
                robot.pickup_object(cube, num_retries=2).wait_for_completed()
            elif kind == "place":
                robot.place_object_on_ground_here(cube, num_retries=1).wait_for_completed()
            elif kind == "roll":
                robot.roll_cube(cube, num_retries=2).wait_for_completed()
            elif kind == "wheelie":
                robot.pop_a_wheelie(cube, num_retries=1).wait_for_completed()

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
    return render_template("index.html")


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
# Entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Make sure adb is on PATH so the SDK can find the tablet
    os.environ["PATH"] = r"C:\Cozmo\platform-tools;" + os.environ.get("PATH", "")

    t = threading.Thread(target=_cozmo_thread, daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
