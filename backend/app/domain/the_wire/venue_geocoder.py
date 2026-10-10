"""Where a fixture is played (Group 78): the venue that sets its weather.

Resolution, first hit wins (``app/services/the_wire/venues.py`` runs it):

1. An indoor sport (``WIRE_INDOOR_SPORT_PREFIXES``: basketball, ice hockey, MMA, boxing, esports): no venue is
   needed, the weather cannot reach the match.
2. A stored venue (``wire_venue_locations``): what ESPN's scoreboard named for the fixture and Open-Meteo's
   geocoder placed, or what an administrator entered.
3. The seed registry below, by the home side's name (exact, then a fuzzy match inside the same sport).
4. A tennis tournament named in the feed's sport key (``tennis_atp_wimbledon`` -> the All England Club).

No hit: the fixture has no venue and no weather, and the fortress's weather pillar stays UNVERIFIED. A neutral
or default climate is never assumed.

The seed holds grounds whose roof matters and whose location is settled. Coordinates are kept to two decimals
(about a kilometre: the forecast grid is coarser); elevation is not seeded, Open-Meteo returns the terrain height
with every forecast. Clubs that move ground change here or through ``POST /the-wire/venues``.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum


class Roof(StrEnum):
    OPEN_AIR = "OPEN_AIR"
    RETRACTABLE = "RETRACTABLE"  # open or closed on the day: the forecast applies only if it is open
    FIXED_DOME = "FIXED_DOME"  # a permanent roof (a canopy over the field counts)
    INDOOR = "INDOOR"  # an arena sport


class Surface(StrEnum):
    NATURAL_GRASS = "NATURAL_GRASS"
    HYBRID = "HYBRID"
    ARTIFICIAL_TURF = "ARTIFICIAL_TURF"
    HARDCOURT = "HARDCOURT"
    CLAY = "CLAY"


@dataclass(frozen=True, slots=True)
class Venue:
    name: str
    city: str
    country: str
    sport: str  # sport key prefix: soccer, americanfootball, baseball, cricket, tennis
    latitude: float
    longitude: float
    roof: Roof
    surface: Surface | None = None
    teams: tuple[str, ...] = field(default=())  # home sides (normalised by ``normalize``); a tournament for tennis
    elevation_m: float | None = None

    @property
    def indoor(self) -> bool:
        return self.roof in (Roof.FIXED_DOME, Roof.INDOOR)

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "city": self.city, "country": self.country, "sport": self.sport, "latitude": self.latitude, "longitude": self.longitude,
                "roof": self.roof.value, "surface": None if self.surface is None else self.surface.value, "elevation_m": self.elevation_m, "indoor": self.indoor}


_STOP = re.compile(r"\b(fc|cf|afc|sc|ssc|ac|as|rc|club|calcio|the|cd|ud|sd|rcd|ca|real club)\b")
_NON_WORD = re.compile(r"[^\w\s]")


def normalize(name: str) -> str:
    """'Brighton & Hove Albion FC' -> 'brighton hove albion', 'Atlético' -> 'atletico': accents folded, punctuation and club affixes dropped."""
    folded = "".join(c for c in unicodedata.normalize("NFKD", name.casefold()) if not unicodedata.combining(c))
    s = _NON_WORD.sub(" ", folded)
    s = _STOP.sub(" ", s)
    return " ".join(s.split())


def sport_family(sport_key: str | None) -> str:
    """'soccer_epl' -> 'soccer'; 'americanfootball_nfl' -> 'americanfootball'."""
    return (sport_key or "").split("_", 1)[0].casefold()


def is_indoor_sport(sport_key: str | None, indoor_prefixes: list[str] | tuple[str, ...]) -> bool:
    family = sport_family(sport_key)
    return bool(family) and any(family.startswith(p) for p in indoor_prefixes)


def _v(name: str, city: str, country: str, sport: str, lat: float, lon: float, roof: Roof, surface: Surface | None, *teams: str) -> Venue:
    return Venue(name, city, country, sport, lat, lon, roof, surface, tuple(normalize(t) for t in teams))


O, R, D = Roof.OPEN_AIR, Roof.RETRACTABLE, Roof.FIXED_DOME
G, H, T, HC, C = Surface.NATURAL_GRASS, Surface.HYBRID, Surface.ARTIFICIAL_TURF, Surface.HARDCOURT, Surface.CLAY

SEED_VENUES: tuple[Venue, ...] = (
    # ---- football: England
    _v("Emirates Stadium", "London", "England", "soccer", 51.55, -0.11, O, H, "Arsenal"),
    _v("Villa Park", "Birmingham", "England", "soccer", 52.51, -1.88, O, H, "Aston Villa"),
    _v("Vitality Stadium", "Bournemouth", "England", "soccer", 50.74, -1.84, O, H, "Bournemouth", "AFC Bournemouth"),
    _v("Gtech Community Stadium", "London", "England", "soccer", 51.49, -0.29, O, H, "Brentford"),
    _v("Amex Stadium", "Brighton", "England", "soccer", 50.86, -0.08, O, H, "Brighton", "Brighton and Hove Albion", "Brighton & Hove Albion"),
    _v("Turf Moor", "Burnley", "England", "soccer", 53.79, -2.23, O, H, "Burnley"),
    _v("Stamford Bridge", "London", "England", "soccer", 51.48, -0.19, O, H, "Chelsea"),
    _v("Selhurst Park", "London", "England", "soccer", 51.40, -0.09, O, H, "Crystal Palace"),
    _v("Hill Dickinson Stadium", "Liverpool", "England", "soccer", 53.42, -3.00, O, H, "Everton"),
    _v("Craven Cottage", "London", "England", "soccer", 51.47, -0.22, O, H, "Fulham"),
    _v("Elland Road", "Leeds", "England", "soccer", 53.78, -1.57, O, H, "Leeds", "Leeds United"),
    _v("Anfield", "Liverpool", "England", "soccer", 53.43, -2.96, O, H, "Liverpool"),
    _v("Etihad Stadium", "Manchester", "England", "soccer", 53.48, -2.20, O, H, "Manchester City", "Man City"),
    _v("Old Trafford", "Manchester", "England", "soccer", 53.46, -2.29, O, H, "Manchester United", "Man United", "Man Utd"),
    _v("St James' Park", "Newcastle", "England", "soccer", 54.98, -1.62, O, H, "Newcastle", "Newcastle United"),
    _v("City Ground", "Nottingham", "England", "soccer", 52.94, -1.13, O, H, "Nottingham Forest", "Nott'm Forest"),
    _v("Stadium of Light", "Sunderland", "England", "soccer", 54.91, -1.39, O, H, "Sunderland"),
    _v("Tottenham Hotspur Stadium", "London", "England", "soccer", 51.60, -0.07, O, H, "Tottenham", "Tottenham Hotspur", "Spurs"),
    _v("London Stadium", "London", "England", "soccer", 51.54, -0.02, O, H, "West Ham", "West Ham United"),
    _v("Molineux", "Wolverhampton", "England", "soccer", 52.59, -2.13, O, H, "Wolves", "Wolverhampton Wanderers"),
    _v("King Power Stadium", "Leicester", "England", "soccer", 52.62, -1.14, O, H, "Leicester", "Leicester City"),
    _v("Portman Road", "Ipswich", "England", "soccer", 52.05, 1.14, O, H, "Ipswich", "Ipswich Town"),
    _v("St Mary's Stadium", "Southampton", "England", "soccer", 50.91, -1.39, O, H, "Southampton"),
    # ---- football: Spain, Italy, Germany, France
    _v("Santiago Bernabéu", "Madrid", "Spain", "soccer", 40.45, -3.69, R, H, "Real Madrid"),
    _v("Spotify Camp Nou", "Barcelona", "Spain", "soccer", 41.38, 2.12, O, H, "Barcelona"),
    _v("Riyadh Air Metropolitano", "Madrid", "Spain", "soccer", 40.44, -3.60, O, H, "Atletico Madrid", "Atlético Madrid"),
    _v("San Mamés", "Bilbao", "Spain", "soccer", 43.26, -2.95, O, H, "Athletic Bilbao", "Athletic Club"),
    _v("Mestalla", "Valencia", "Spain", "soccer", 39.47, -0.36, O, G, "Valencia"),
    _v("Ramón Sánchez-Pizjuán", "Seville", "Spain", "soccer", 37.38, -5.97, O, G, "Sevilla"),
    _v("Benito Villamarín", "Seville", "Spain", "soccer", 37.36, -5.98, O, G, "Real Betis", "Betis"),
    _v("Reale Arena", "San Sebastián", "Spain", "soccer", 43.30, -1.97, O, H, "Real Sociedad"),
    _v("Estadio de la Cerámica", "Villarreal", "Spain", "soccer", 39.94, -0.10, O, G, "Villarreal"),
    _v("San Siro", "Milan", "Italy", "soccer", 45.48, 9.12, O, H, "Inter", "Inter Milan", "Internazionale", "AC Milan", "Milan"),
    _v("Allianz Stadium", "Turin", "Italy", "soccer", 45.11, 7.64, O, H, "Juventus"),
    _v("Stadio Olimpico", "Rome", "Italy", "soccer", 41.93, 12.45, O, G, "AS Roma", "Roma", "Lazio"),
    _v("Stadio Diego Armando Maradona", "Naples", "Italy", "soccer", 40.83, 14.19, O, G, "Napoli"),
    _v("Gewiss Stadium", "Bergamo", "Italy", "soccer", 45.71, 9.68, O, G, "Atalanta"),
    _v("Allianz Arena", "Munich", "Germany", "soccer", 48.22, 11.62, O, H, "Bayern Munich", "Bayern München", "FC Bayern"),
    _v("Signal Iduna Park", "Dortmund", "Germany", "soccer", 51.49, 7.45, O, G, "Borussia Dortmund", "Dortmund"),
    _v("Red Bull Arena", "Leipzig", "Germany", "soccer", 51.35, 12.35, O, G, "RB Leipzig", "Leipzig"),
    _v("BayArena", "Leverkusen", "Germany", "soccer", 51.04, 7.00, O, H, "Bayer Leverkusen", "Leverkusen"),
    _v("Parc des Princes", "Paris", "France", "soccer", 48.84, 2.25, O, H, "Paris Saint-Germain", "Paris Saint Germain", "PSG"),
    _v("Orange Vélodrome", "Marseille", "France", "soccer", 43.27, 5.40, O, H, "Marseille", "Olympique Marseille"),
    _v("Groupama Stadium", "Lyon", "France", "soccer", 45.77, 4.98, O, H, "Lyon", "Olympique Lyonnais"),
    # ---- American football (NFL)
    _v("State Farm Stadium", "Glendale", "USA", "americanfootball", 33.53, -112.26, R, None, "Arizona Cardinals"),
    _v("Mercedes-Benz Stadium", "Atlanta", "USA", "americanfootball", 33.76, -84.40, R, T, "Atlanta Falcons"),
    _v("M&T Bank Stadium", "Baltimore", "USA", "americanfootball", 39.28, -76.62, O, None, "Baltimore Ravens"),
    _v("Highmark Stadium", "Orchard Park", "USA", "americanfootball", 42.77, -78.79, O, None, "Buffalo Bills"),
    _v("Bank of America Stadium", "Charlotte", "USA", "americanfootball", 35.23, -80.85, O, None, "Carolina Panthers"),
    _v("Soldier Field", "Chicago", "USA", "americanfootball", 41.86, -87.62, O, G, "Chicago Bears"),
    _v("Paycor Stadium", "Cincinnati", "USA", "americanfootball", 39.10, -84.52, O, None, "Cincinnati Bengals"),
    _v("Huntington Bank Field", "Cleveland", "USA", "americanfootball", 41.51, -81.70, O, None, "Cleveland Browns"),
    _v("AT&T Stadium", "Arlington", "USA", "americanfootball", 32.75, -97.09, R, T, "Dallas Cowboys"),
    _v("Empower Field at Mile High", "Denver", "USA", "americanfootball", 39.74, -105.02, O, None, "Denver Broncos"),
    _v("Ford Field", "Detroit", "USA", "americanfootball", 42.34, -83.05, D, T, "Detroit Lions"),
    _v("Lambeau Field", "Green Bay", "USA", "americanfootball", 44.50, -88.06, O, H, "Green Bay Packers"),
    _v("NRG Stadium", "Houston", "USA", "americanfootball", 29.68, -95.41, R, None, "Houston Texans"),
    _v("Lucas Oil Stadium", "Indianapolis", "USA", "americanfootball", 39.76, -86.16, R, T, "Indianapolis Colts"),
    _v("EverBank Stadium", "Jacksonville", "USA", "americanfootball", 30.32, -81.64, O, None, "Jacksonville Jaguars"),
    _v("GEHA Field at Arrowhead Stadium", "Kansas City", "USA", "americanfootball", 39.05, -94.48, O, G, "Kansas City Chiefs"),
    _v("Allegiant Stadium", "Las Vegas", "USA", "americanfootball", 36.09, -115.18, D, None, "Las Vegas Raiders"),
    _v("SoFi Stadium", "Inglewood", "USA", "americanfootball", 33.95, -118.34, D, T, "Los Angeles Chargers", "Los Angeles Rams"),
    _v("Hard Rock Stadium", "Miami Gardens", "USA", "americanfootball", 25.96, -80.24, O, G, "Miami Dolphins"),
    _v("U.S. Bank Stadium", "Minneapolis", "USA", "americanfootball", 44.97, -93.26, D, T, "Minnesota Vikings"),
    _v("Gillette Stadium", "Foxborough", "USA", "americanfootball", 42.09, -71.26, O, T, "New England Patriots"),
    _v("Caesars Superdome", "New Orleans", "USA", "americanfootball", 29.95, -90.08, D, T, "New Orleans Saints"),
    _v("MetLife Stadium", "East Rutherford", "USA", "americanfootball", 40.81, -74.07, O, T, "New York Giants", "New York Jets"),
    _v("Lincoln Financial Field", "Philadelphia", "USA", "americanfootball", 39.90, -75.17, O, None, "Philadelphia Eagles"),
    _v("Acrisure Stadium", "Pittsburgh", "USA", "americanfootball", 40.45, -80.02, O, G, "Pittsburgh Steelers"),
    _v("Levi's Stadium", "Santa Clara", "USA", "americanfootball", 37.40, -121.97, O, G, "San Francisco 49ers"),
    _v("Lumen Field", "Seattle", "USA", "americanfootball", 47.60, -122.33, O, T, "Seattle Seahawks"),
    _v("Raymond James Stadium", "Tampa", "USA", "americanfootball", 27.98, -82.50, O, G, "Tampa Bay Buccaneers"),
    _v("Nissan Stadium", "Nashville", "USA", "americanfootball", 36.17, -86.77, O, None, "Tennessee Titans"),
    _v("Northwest Stadium", "Landover", "USA", "americanfootball", 38.91, -76.86, O, None, "Washington Commanders"),
    # ---- baseball (MLB)
    _v("Chase Field", "Phoenix", "USA", "baseball", 33.45, -112.07, R, T, "Arizona Diamondbacks"),
    _v("Truist Park", "Atlanta", "USA", "baseball", 33.89, -84.47, O, G, "Atlanta Braves"),
    _v("Oriole Park at Camden Yards", "Baltimore", "USA", "baseball", 39.28, -76.62, O, G, "Baltimore Orioles"),
    _v("Fenway Park", "Boston", "USA", "baseball", 42.35, -71.10, O, G, "Boston Red Sox"),
    _v("Wrigley Field", "Chicago", "USA", "baseball", 41.95, -87.66, O, G, "Chicago Cubs"),
    _v("Rate Field", "Chicago", "USA", "baseball", 41.83, -87.63, O, G, "Chicago White Sox"),
    _v("Great American Ball Park", "Cincinnati", "USA", "baseball", 39.10, -84.51, O, G, "Cincinnati Reds"),
    _v("Progressive Field", "Cleveland", "USA", "baseball", 41.50, -81.69, O, G, "Cleveland Guardians"),
    _v("Coors Field", "Denver", "USA", "baseball", 39.76, -104.99, O, G, "Colorado Rockies"),
    _v("Comerica Park", "Detroit", "USA", "baseball", 42.34, -83.05, O, G, "Detroit Tigers"),
    _v("Daikin Park", "Houston", "USA", "baseball", 29.76, -95.36, R, G, "Houston Astros"),
    _v("Kauffman Stadium", "Kansas City", "USA", "baseball", 39.05, -94.48, O, G, "Kansas City Royals"),
    _v("Angel Stadium", "Anaheim", "USA", "baseball", 33.80, -117.88, O, G, "Los Angeles Angels"),
    _v("Dodger Stadium", "Los Angeles", "USA", "baseball", 34.07, -118.24, O, G, "Los Angeles Dodgers"),
    _v("loanDepot park", "Miami", "USA", "baseball", 25.78, -80.22, R, T, "Miami Marlins"),
    _v("American Family Field", "Milwaukee", "USA", "baseball", 43.03, -87.97, R, G, "Milwaukee Brewers"),
    _v("Target Field", "Minneapolis", "USA", "baseball", 44.98, -93.28, O, G, "Minnesota Twins"),
    _v("Citi Field", "New York", "USA", "baseball", 40.76, -73.85, O, G, "New York Mets"),
    _v("Yankee Stadium", "New York", "USA", "baseball", 40.83, -73.93, O, G, "New York Yankees"),
    _v("Sutter Health Park", "West Sacramento", "USA", "baseball", 38.58, -121.51, O, G, "Athletics", "Oakland Athletics"),
    _v("Citizens Bank Park", "Philadelphia", "USA", "baseball", 39.91, -75.17, O, G, "Philadelphia Phillies"),
    _v("PNC Park", "Pittsburgh", "USA", "baseball", 40.45, -80.01, O, G, "Pittsburgh Pirates"),
    _v("Petco Park", "San Diego", "USA", "baseball", 32.71, -117.16, O, G, "San Diego Padres"),
    _v("Oracle Park", "San Francisco", "USA", "baseball", 37.78, -122.39, O, G, "San Francisco Giants"),
    _v("T-Mobile Park", "Seattle", "USA", "baseball", 47.59, -122.33, R, G, "Seattle Mariners"),
    _v("Busch Stadium", "St. Louis", "USA", "baseball", 38.62, -90.19, O, G, "St. Louis Cardinals", "St Louis Cardinals"),
    _v("Globe Life Field", "Arlington", "USA", "baseball", 32.75, -97.08, R, T, "Texas Rangers"),
    _v("Rogers Centre", "Toronto", "Canada", "baseball", 43.64, -79.39, R, T, "Toronto Blue Jays"),
    _v("Nationals Park", "Washington", "USA", "baseball", 38.87, -77.01, O, G, "Washington Nationals"),
    # ---- cricket: IPL home grounds and the major Test grounds (by ground name for internationals)
    _v("Wankhede Stadium", "Mumbai", "India", "cricket", 18.94, 72.83, O, G, "Mumbai Indians", "Wankhede Stadium"),
    _v("MA Chidambaram Stadium", "Chennai", "India", "cricket", 13.06, 80.28, O, G, "Chennai Super Kings", "MA Chidambaram Stadium", "Chepauk"),
    _v("Eden Gardens", "Kolkata", "India", "cricket", 22.56, 88.34, O, G, "Kolkata Knight Riders", "Eden Gardens"),
    _v("M. Chinnaswamy Stadium", "Bengaluru", "India", "cricket", 12.98, 77.60, O, G, "Royal Challengers Bengaluru", "Royal Challengers Bangalore", "M Chinnaswamy Stadium"),
    _v("Arun Jaitley Stadium", "Delhi", "India", "cricket", 28.64, 77.24, O, G, "Delhi Capitals", "Arun Jaitley Stadium"),
    _v("Rajiv Gandhi International Stadium", "Hyderabad", "India", "cricket", 17.41, 78.55, O, G, "Sunrisers Hyderabad", "Rajiv Gandhi International Stadium"),
    _v("Sawai Mansingh Stadium", "Jaipur", "India", "cricket", 26.89, 75.80, O, G, "Rajasthan Royals", "Sawai Mansingh Stadium"),
    _v("Narendra Modi Stadium", "Ahmedabad", "India", "cricket", 23.09, 72.60, O, G, "Gujarat Titans", "Narendra Modi Stadium"),
    _v("Ekana Cricket Stadium", "Lucknow", "India", "cricket", 26.81, 81.02, O, G, "Lucknow Super Giants", "Ekana Cricket Stadium"),
    _v("Lord's", "London", "England", "cricket", 51.53, -0.17, O, G, "Lord's", "Lords", "Middlesex"),
    _v("The Oval", "London", "England", "cricket", 51.48, -0.12, O, G, "The Oval", "Kennington Oval", "Surrey"),
    _v("Edgbaston", "Birmingham", "England", "cricket", 52.46, -1.90, O, G, "Edgbaston", "Warwickshire"),
    _v("Headingley", "Leeds", "England", "cricket", 53.82, -1.58, O, G, "Headingley", "Yorkshire"),
    _v("Old Trafford Cricket Ground", "Manchester", "England", "cricket", 53.46, -2.29, O, G, "Old Trafford Cricket Ground", "Lancashire"),
    _v("Melbourne Cricket Ground", "Melbourne", "Australia", "cricket", -37.82, 144.98, O, G, "Melbourne Cricket Ground", "MCG", "Melbourne Stars"),
    _v("Sydney Cricket Ground", "Sydney", "Australia", "cricket", -33.89, 151.22, O, G, "Sydney Cricket Ground", "SCG", "Sydney Sixers"),
    _v("Adelaide Oval", "Adelaide", "Australia", "cricket", -34.92, 138.60, O, G, "Adelaide Oval", "Adelaide Strikers"),
    _v("Newlands", "Cape Town", "South Africa", "cricket", -33.97, 18.47, O, G, "Newlands"),
    _v("The Wanderers", "Johannesburg", "South Africa", "cricket", -26.13, 28.06, O, G, "The Wanderers", "Wanderers Stadium"),
    # ---- tennis: the Grand Slams, by the tournament named in the sport key
    _v("All England Lawn Tennis Club", "London", "England", "tennis", 51.43, -0.21, R, G, "wimbledon"),
    _v("Stade Roland Garros", "Paris", "France", "tennis", 48.85, 2.25, R, C, "french open", "roland garros"),
    _v("USTA Billie Jean King National Tennis Center", "New York", "USA", "tennis", 40.75, -73.85, R, HC, "us open"),
    _v("Melbourne Park", "Melbourne", "Australia", "tennis", -37.82, 144.98, R, HC, "aus open", "australian open"),
)


def _index(venues: tuple[Venue, ...]) -> dict[str, dict[str, Venue]]:
    out: dict[str, dict[str, Venue]] = {}
    for venue in venues:
        for team in venue.teams:
            out.setdefault(venue.sport, {})[team] = venue
    return out


SEED_INDEX = _index(SEED_VENUES)


def resolve_seed(team: str, sport_key: str | None, cutoff: float) -> Venue | None:
    """The seed venue of ``team`` in its sport: exact on the normalised name, then the closest name at or over ``cutoff``."""
    family = sport_family(sport_key)
    pool = SEED_INDEX.get(family)
    if not pool:
        return None
    clean = normalize(team)
    if clean in pool:
        return pool[clean]
    close = difflib.get_close_matches(clean, list(pool), n=1, cutoff=cutoff)
    return pool[close[0]] if close else None


def resolve_tournament(sport_key: str | None) -> Venue | None:
    """``tennis_atp_wimbledon`` -> the All England Club: a tournament's venue from its sport key."""
    if sport_family(sport_key) != "tennis":
        return None
    words = f" {' '.join((sport_key or '').casefold().split('_')[1:])} "
    for team, venue in SEED_INDEX.get("tennis", {}).items():
        if f" {team} " in words:  # whole words: "aus open" must not read as "us open"
            return venue
    return None
