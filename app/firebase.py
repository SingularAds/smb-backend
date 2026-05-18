"""Firebase Admin SDK initialisation

Initialised once at application startup. All other modules import
`firebase_app` or use `firebase_admin.auth` / `firebase_admin.firestore`
directly — they will automatically use this initialised app.
"""

import os
import firebase_admin
from firebase_admin import credentials

_firebase_app: firebase_admin.App | None = None


def init_firebase() -> firebase_admin.App:
    """
    Initialise Firebase Admin SDK using the service account JSON.
    Safe to call multiple times — returns the existing app if already
    initialised.
    """
    global _firebase_app

    if _firebase_app is not None:
        return _firebase_app

    # Already initialised by another module (e.g. during testing)
    if firebase_admin._apps:
        _firebase_app = firebase_admin.get_app()
        return _firebase_app

    # Resolve path to service account file
    sa_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not sa_path:
        # Default: serviceAccount.json in the project root (next to app/)
        _project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sa_path = os.path.join(_project_root, "serviceAccount.json")

    if not os.path.isfile(sa_path):
        raise FileNotFoundError(
            f"Firebase service account not found at '{sa_path}'. "
            "Set GOOGLE_APPLICATION_CREDENTIALS in your .env or place "
            "serviceAccount.json in the project root."
        )

    cred = credentials.Certificate(sa_path)
    _firebase_app = firebase_admin.initialize_app(cred, {
        "projectId": os.environ.get("FIRESTORE_PROJECT_ID", "smbaicallz"),
    })

    return _firebase_app


def get_firebase_app() -> firebase_admin.App:
    """Return the initialised Firebase app (raises if not yet initialised)."""
    if _firebase_app is None:
        raise RuntimeError("Firebase has not been initialised. Call init_firebase() first.")
    return _firebase_app
