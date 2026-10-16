"""Compile/run the ACTUAL pure Java policy against deterministic and blocked readers."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parent
jdk = Path('/workspace/android-drive-kit/build-tools/jdk-17.0.16+8')
with tempfile.TemporaryDirectory(prefix='ui-probe-root-policy-') as directory:
    subprocess.run([str(jdk / 'bin/javac'), '--release', '8', '-d', directory,
                    str(root / 'src/com/codestick/uiprobe/RootPoller.java'),
                    str(root / 'tests/RootPollerFixtures.java')], check=True, timeout=30)
    subprocess.run([str(jdk / 'bin/java'), '-cp', directory, 'com.codestick.uiprobe.RootPollerFixtures'],
                   check=True, timeout=5)
