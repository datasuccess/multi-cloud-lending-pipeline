# Streamlit puts ``streamlit_app/`` on ``sys.path`` (not the project root), so
# ``from lambdas.shared...`` fails unless the user happened to install the
# project editable into the *same* interpreter Streamlit was installed under.
# Adding the project root here makes the app self-bootstrapping regardless of
# which Python `streamlit` was wired to.
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
