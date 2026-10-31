import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://geofat:geofat@localhost:5433/geofat")
FOTOS_DIR = Path(os.environ.get("FOTOS_DIR", RAIZ / "data" / "fotos"))
# Bonsai servido localmente pelo llama-server (ou LM Studio), API compatível com OpenAI
BONSAI_URL = os.environ.get("BONSAI_URL", "http://localhost:1234/v1")
BONSAI_MODEL = os.environ.get("BONSAI_MODEL", "Ternary-Bonsai-2-9B-Q2_0")
# Declinação magnética em São Luís (~21° W em 2026), para bússolas que gravam norte magnético.
# Trocar ao usar em outra região: https://www.ngdc.noaa.gov/geomag/calculators/magcalc.shtml
DECLINACAO_MAGNETICA = float(os.environ.get("DECLINACAO_MAGNETICA", "-21"))
