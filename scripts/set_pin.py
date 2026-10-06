"""Set the fixed API PIN without echoing it or writing it to shell history."""

import getpass
import os
import tempfile
from pathlib import Path


def main():
    pin = getpass.getpass("Fixed PIN (6-12 digits): ")
    if not pin.isascii() or not pin.isdigit() or not 6 <= len(pin) <= 12:
        raise SystemExit("PIN must contain 6-12 ASCII digits")
    if pin != getpass.getpass("Confirm PIN: "):
        raise SystemExit("PINs do not match")
    directory = Path.home() / ".config/agentcall"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pin-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(f"AGENTCALL_TOKEN={pin}\n")
        os.replace(name, directory / "pin.env")
    finally:
        Path(name).unlink(missing_ok=True)
    print("PIN saved privately. Restart the AgentCall service to apply it.")


if __name__ == "__main__":
    main()
