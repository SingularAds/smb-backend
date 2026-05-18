import logging
import firebase_admin
from firebase_admin import credentials

logger = logging.getLogger(__name__)

# Load your private key
cred = credentials.Certificate("firebase-secret.json")

# Initialize app
firebase_admin.initialize_app(cred)

logger.info("Firebase connected successfully")
