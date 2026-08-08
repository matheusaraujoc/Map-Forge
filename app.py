#!/usr/bin/env python
"""Abre a interface grafica do MapForge.

    py -3.11 app.py
"""

import sys

from mapforge.ui import run

if __name__ == "__main__":
    sys.exit(run(sys.argv))
