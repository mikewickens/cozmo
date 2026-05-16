"""Smoke test for the Anki Cozmo SDK over adb.

Run with the Python 3.7 venv:
    .\.venv37\Scripts\python.exe say_hello.py
"""
import cozmo


def run(robot: cozmo.robot.Robot):
    robot.say_text("Hello, I am Cozmo.").wait_for_completed()


if __name__ == "__main__":
    cozmo.run_program(run)
