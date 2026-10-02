from __future__ import annotations

import re

COMMANDS = {
    " xuống dòng ": "\n", " chấm ": ". ", " phẩy ": ", ", " hai chấm ": ": ",
    " chấm phẩy ": "; ", " mở ngoặc ": " (", " đóng ngoặc ": ") ",
}
MEDICAL = {
    "t n f alpha": "TNF-α", "a pa chi hai": "APACHE II", "a patch hai": "APACHE II",
    "bi sap": "BISAP", "sô pha": "SOFA", "c r p": "CRP", "p value": "p-value",
    "ô r": "OR", "r r": "RR", "h r": "HR", "a u c": "AUC",
}

def normalize_vi_medical(text: str) -> str:
    x = " " + re.sub(r"\s+", " ", text.strip()) + " "
    low = x.lower()
    for key, value in COMMANDS.items(): low = low.replace(key, value)
    x = low.strip()
    for key, value in MEDICAL.items(): x = re.sub(rf"\b{re.escape(key)}\b", value, x, flags=re.I)
    return x[:1].upper() + x[1:] if x else x
