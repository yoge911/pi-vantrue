import subprocess
import time

# Replace these with the connection names shown by:
# nmcli connection show
TEST_NETWORK = "iPhone"
HOME_NETWORK = "o2-WLAN96-2.4"


def connect(network):
    print(f"Connecting to: {network}")

    result = subprocess.run(
        ["sudo", "nmcli", "connection", "up", network],
        capture_output=True,
        text=True
    )

    if result.returncode == 0:
        print(f"Connected to {network}")
        return True

    print("Connection failed:")
    print(result.stderr)
    return False


print("Switching to test network...")

if connect(TEST_NETWORK):

    # Your SSH connection will probably disappear here.
    # That's expected because the Pi changes Wi-Fi.
    time.sleep(30)

    print("Switching back to home Wi-Fi...")
    connect(HOME_NETWORK)

print("Finished.")
