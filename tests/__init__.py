import os
import tempfile

# Keep a Proton export CLI installed on the developer's machine out of the tests' auto-detection.
os.environ["MAILOPHANT_CLI_DIR"] = os.path.join(tempfile.gettempdir(), f"mailophant-test-no-cli-{os.getpid()}")
