"""Alturas de referencia de cada camada (metros).

Manter uma folga de alguns centimetros entre superficies horizontais evita
z-fighting sem precisar de polygon offset no renderizador.
"""

# --- agua ---
# O leito fica bem abaixo do solo: e o que faz o rio parecer escavado em vez de
# uma fita azul colada no chao, e o que da sentido as pontes.
Z_WATER_BED = -1.60
Z_WATER = -0.55
BANK_WIDTH = 3.2  # largura do talude entre o solo e o leito

Z_GROUND = 0.00
Z_GREEN = 0.02
Z_PARKING = 0.03
Z_ROAD = 0.05
Z_ROAD_LINE = 0.065  # sinalizacao horizontal, logo acima do asfalto
Z_RAIL_BED = 0.07
Z_RAIL = 0.22
Z_CURB = 0.16

# --- estruturas elevadas ---
Z_BRIDGE = 5.00
Z_BRIDGE_DECK = 0.55
# Ponte de nivel: a pista segue no nivel do solo e ganha uma laje por baixo,
# porque quem esta rebaixado e o rio. Nao precisa de rampa.
DECK_THICKNESS = 0.65
DECK_OVERHANG = 4.0  # quanto a laje avanca sobre as margens

# Deslocamento por nivel (tag layer=*) para viadutos empilhados.
LAYER_STEP = 4.5
