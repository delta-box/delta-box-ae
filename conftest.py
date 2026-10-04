"""Keep the test suite independent of the hosted reviewer login environment."""
import os

# ~/.deltabox-ae-env selects hosted mode. Under SSH, non-interactive Bash children
# may source ~/.bashrc and re-export it, so drop the SSH markers as well.
for name in ('AE_CONFIG', 'AE_HOSTED_LAUNCHER', 'SSH_CLIENT', 'SSH2_CLIENT'):
    os.environ.pop(name, None)

# Some tests put replay/guest on sys.path; its agent.py must not shadow agent/.
import agent  # noqa: E402,F401
