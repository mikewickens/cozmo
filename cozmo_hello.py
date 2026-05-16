import cozmo
from cozmo.run import TCPConnector

# Connect directly to the phone via the Cozmo WiFi network (172.31.1.x)
# The phone's IP on Cozmo_XXXXXX WiFi is 172.31.1.120
connector = TCPConnector(tcp_port=5106, ip_addr='10.166.115.204')

def run(sdk_conn):
    robot = sdk_conn.wait_for_robot()
    print("Connected to COZMO!")

    robot.say_text("Hello! I am ready.").wait_for_completed()
    robot.drive_straight(cozmo.util.distance_mm(100),
                         cozmo.util.speed_mmps(50)).wait_for_completed()
    robot.turn_in_place(cozmo.util.degrees(360)).wait_for_completed()
    robot.say_text("That was fun!").wait_for_completed()

cozmo.run_program(run, conn_factory=cozmo.conn.CozmoConnection, connector=connector)
