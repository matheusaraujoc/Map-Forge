#!/usr/bin/env python
"""Ponto de entrada do MapForge.

    py -3.11 run.py generate --place "Ouro Preto, MG" --radius 500 --style cartoon
"""

import sys

from mapforge.cli import main

if __name__ == "__main__":
    sys.exit(main())
