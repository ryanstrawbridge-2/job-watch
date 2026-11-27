"""Straight-line miles from downtown Seattle for Washington place names that show up in job listings."""
import math
import re

SEATTLE = (47.606, -122.332)

# name -> (lat, lon). Regions and counties map to their main city.
PLACES = {
    'seattle': SEATTLE, 'greater seattle': SEATTLE, 'seattle area': SEATTLE, 'puget sound': SEATTLE,
    'king county': SEATTLE, 'western wa': SEATTLE, 'western washington': SEATTLE,
    'bellevue': (47.610, -122.201), 'redmond': (47.674, -122.121), 'kirkland': (47.681, -122.209),
    'bothell': (47.762, -122.205), 'woodinville': (47.754, -122.163), 'issaquah': (47.530, -122.033),
    'sammamish': (47.616, -122.036), 'mercer island': (47.571, -122.222), 'newcastle': (47.539, -122.156),
    'medina': (47.620, -122.233), 'kenmore': (47.757, -122.244), 'lake forest park': (47.757, -122.281),
    'shoreline': (47.756, -122.341), 'mountlake terrace': (47.788, -122.308), 'edmonds': (47.811, -122.377),
    'lynnwood': (47.821, -122.315), 'mill creek': (47.860, -122.204), 'mukilteo': (47.945, -122.305),
    'everett': (47.979, -122.202), 'paine field': (47.906, -122.282), 'snohomish county': (47.979, -122.202),
    'snohomish': (47.913, -122.098), 'monroe': (47.855, -121.971), 'lake stevens': (48.015, -122.064),
    'marysville': (48.052, -122.177), 'arlington': (48.199, -122.125), 'stanwood': (48.241, -122.370),
    'granite falls': (48.084, -121.968), 'duvall': (47.742, -121.986), 'carnation': (47.648, -121.914),
    'snoqualmie': (47.529, -121.825), 'north bend': (47.496, -121.787),
    'renton': (47.483, -122.217), 'tukwila': (47.474, -122.261), 'seatac': (47.444, -122.301),
    'sea tac': (47.444, -122.301), 'burien': (47.470, -122.347), 'des moines': (47.402, -122.324),
    'kent': (47.381, -122.235), 'covington': (47.358, -122.122), 'maple valley': (47.393, -122.047),
    'federal way': (47.322, -122.313), 'auburn': (47.307, -122.229), 'algona': (47.279, -122.252),
    'pacific, wa': (47.264, -122.250), 'black diamond': (47.309, -122.003), 'enumclaw': (47.204, -121.992),
    'tacoma': (47.253, -122.444), 'pierce county': (47.253, -122.444), 'fife': (47.239, -122.357),
    'puyallup': (47.185, -122.293), 'sumner': (47.203, -122.240), 'bonney lake': (47.177, -122.186),
    'frederickson': (47.096, -122.359), 'spanaway': (47.104, -122.435), 'lakewood': (47.172, -122.518),
    'university place': (47.236, -122.550), 'gig harbor': (47.329, -122.580), 'dupont': (47.097, -122.631),
    'jblm': (47.110, -122.570), 'joint base lewis': (47.110, -122.570), 'fort lewis': (47.110, -122.570),
    'olympia': (47.038, -122.900), 'lacey': (47.034, -122.823), 'tumwater': (47.007, -122.909),
    'thurston county': (47.038, -122.900), 'yelm': (46.942, -122.606),
    'bremerton': (47.567, -122.633), 'kitsap': (47.567, -122.633), 'port orchard': (47.540, -122.636),
    'silverdale': (47.645, -122.695), 'bangor': (47.726, -122.714), 'keyport': (47.700, -122.623),
    'poulsbo': (47.736, -122.646), 'suquamish': (47.731, -122.552), 'kingston': (47.799, -122.498),
    'bainbridge': (47.626, -122.521), 'vashon': (47.447, -122.460),
    'indian island': (48.040, -122.720), 'port hadlock': (48.030, -122.750), 'port townsend': (48.117, -122.760),
    'oak harbor': (48.293, -122.643), 'whidbey': (48.250, -122.650), 'coupeville': (48.220, -122.686),
    'langley': (48.040, -122.406), 'mount vernon': (48.421, -122.334), 'skagit': (48.421, -122.334),
    'burlington': (48.476, -122.325), 'sedro woolley': (48.504, -122.236), 'anacortes': (48.512, -122.613),
    'la conner': (48.392, -122.496), 'hamilton': (48.523, -121.985), 'bellingham': (48.750, -122.479),
    'whatcom': (48.750, -122.479), 'ferndale': (48.847, -122.591), 'lynden': (48.946, -122.452),
    'blaine': (48.994, -122.747), 'shelton': (47.215, -123.100), 'centralia': (46.716, -122.954),
    'chehalis': (46.662, -122.964), 'aberdeen': (46.975, -123.816), 'port angeles': (48.118, -123.430),
    'sequim': (48.080, -123.102), 'wenatchee': (47.423, -120.310), 'malaga': (47.360, -120.200),
    'ellensburg': (46.997, -120.548), 'cle elum': (47.195, -120.939), 'yakima': (46.602, -120.506),
    'moses lake': (47.130, -119.278), 'ephrata': (47.318, -119.553), 'quincy': (47.234, -119.852),
    'bridgeport': (48.007, -119.671), 'spokane': (47.659, -117.426), 'airway heights': (47.644, -117.593),
    'fairchild': (47.615, -117.656), 'cheney': (47.487, -117.576), 'liberty lake': (47.675, -117.111),
    'pullman': (46.731, -117.180), 'clarkston': (46.416, -117.045), 'pomeroy': (46.475, -117.603),
    'richland': (46.286, -119.284), 'kennewick': (46.211, -119.137), 'pasco': (46.240, -119.101),
    'walla walla': (46.065, -118.343), 'vancouver, wa': (45.639, -122.661), 'longview': (46.138, -122.938),
}
_RX = re.compile(r'\b(' + '|'.join(sorted(map(re.escape, PLACES), key=len, reverse=True)) + r')\b')


def miles_to(p):
    (a, b), (c, d) = SEATTLE, p
    a, b, c, d = map(math.radians, (a, b, c, d))
    h = math.sin((c - a) / 2) ** 2 + math.cos(a) * math.cos(c) * math.sin((d - b) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def place_miles(text):
    """Smallest distance among places named in text, or None if none are recognized."""
    t = re.sub(r'[-–]', ' ', text.lower())
    t = re.sub(r'\s+', ' ', t)
    hits = [miles_to(PLACES[m]) for m in _RX.findall(t)]
    return round(min(hits)) if hits else None
